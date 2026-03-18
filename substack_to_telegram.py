#!/usr/bin/env python3
"""
Substack to Telegram Digest Bot (Railway Edition)

User Hierarchy:
- Owner: Full control (add/remove admins, block users, broadcast, stats)
- Admin: Free Pro access only (no admin commands)
- User: Regular free/paid users
"""

import os
import sys
import time
import threading
import schedule
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as date_parser
from typing import Optional
from flask import Flask, request, jsonify

from manage_feeds import (
    list_feeds,
    add_feed,
    remove_feed,
    ensure_user,
    get_last_sent_date,
    set_last_sent_date,
    get_all_users,
    is_user_blocked,
    check_rate_limit,
    get_subscription,
    get_tier_limits,
    get_user_stats,
    get_all_stats,
    upgrade_subscription,
    TIERS,
    # Hierarchy functions
    is_owner,
    is_admin,
    is_privileged,
    set_owner_id,
    get_owner_id,
    add_admin,
    remove_admin,
    list_admins,
    block_user,
    unblock_user,
    register_user,
    get_user_id_by_username,
    get_all_known_users,
    # Analytics functions
    record_payment,
    record_event,
    get_recent_payments,
    get_payment_stats_by_period,
    # Summary format functions
    get_summary_format,
    set_summary_format,
    set_custom_prompt,
    clear_custom_prompt,
)

from ai_summarizer import (
    generate_batch_summaries,
    clean_html,
    get_available_formats,
    validate_custom_prompt,
    SUMMARY_FORMATS,
)


# ---------------- CONFIG ----------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
PORT = int(os.environ.get("PORT", 8080))

# Telegram Stars pricing
PRO_PRICE_STARS = 50
PRO_DURATION_DAYS = 30

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

LOOKBACK_HOURS = 48  # 2 days
DIGEST_HOUR_UTC = 0
DIGEST_MINUTE_UTC = 0

app = Flask(__name__)


# ---------------- TELEGRAM HELPERS ----------------

def send_message(chat_id: str, text: str, html: bool = False, reply_markup: dict = None) -> bool:
    """Send a message via Telegram Bot API. Splits long messages automatically."""
    MAX_LENGTH = 4096  # Telegram's limit
    
    if len(text) <= MAX_LENGTH:
        return _send_single_message(chat_id, text, html, reply_markup)
    
    # Split long messages at logical break points
    messages = []
    current = ""
    
    # Split by the separator line
    parts = text.split("━━━━━━━━━━━━━━━━━━━━")
    
    for i, part in enumerate(parts):
        separator = "━━━━━━━━━━━━━━━━━━━━" if i < len(parts) - 1 else ""
        
        if len(current) + len(part) + len(separator) < MAX_LENGTH - 100:
            current += part + separator
        else:
            if current:
                messages.append(current)
            current = part + separator
    
    if current:
        messages.append(current)
    
    # Send all parts
    success = True
    for msg in messages:
        if not _send_single_message(chat_id, msg, html, reply_markup if msg == messages[-1] else None):
            success = False
        time.sleep(0.3)  # Small delay between messages
    
    return success


def _send_single_message(chat_id: str, text: str, html: bool = False, reply_markup: dict = None) -> bool:
    """Send a single message via Telegram Bot API."""
    if len(text) > 4096:
        text = text[:4090] + "\n\n..."
    
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if html:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup
    
    try:
        resp = requests.post(f"{TELEGRAM_API_BASE}/sendMessage", json=payload, timeout=30)
        return resp.ok
    except requests.RequestException as e:
        print(f"Error sending message: {e}")
        return False


def send_invoice(chat_id: str, user_id: str) -> bool:
    """Send a Telegram Stars invoice for Pro subscription."""
    payload = {
        "chat_id": chat_id,
        "title": "Pro Subscription",
        "description": f"Unlock Pro features for {PRO_DURATION_DAYS} days:\n• 50 feeds (vs 3)\n• AI-powered SCQR summaries\n• Priority support",
        "payload": f"pro_subscription_{user_id}",
        "currency": "XTR",
        "prices": [{"label": "Pro (30 days)", "amount": PRO_PRICE_STARS}],
    }
    
    try:
        resp = requests.post(f"{TELEGRAM_API_BASE}/sendInvoice", json=payload, timeout=30)
        result = resp.json()
        print(f"Invoice response: {result}")
        return resp.ok
    except requests.RequestException as e:
        print(f"Error sending invoice: {e}")
        return False


def answer_pre_checkout(pre_checkout_query_id: str, ok: bool = True, error_message: str = None) -> bool:
    """Answer a pre-checkout query."""
    payload = {
        "pre_checkout_query_id": pre_checkout_query_id,
        "ok": ok,
    }
    if error_message:
        payload["error_message"] = error_message
    
    try:
        resp = requests.post(f"{TELEGRAM_API_BASE}/answerPreCheckoutQuery", json=payload, timeout=30)
        return resp.ok
    except requests.RequestException as e:
        print(f"Error answering pre-checkout: {e}")
        return False


def set_webhook(url: str) -> bool:
    """Set Telegram webhook URL."""
    try:
        resp = requests.post(
            f"{TELEGRAM_API_BASE}/setWebhook",
            json={
                "url": url,
                "allowed_updates": ["message", "pre_checkout_query"]
            },
            timeout=30
        )
        print(f"Webhook set response: {resp.json()}")
        return resp.ok
    except requests.RequestException as e:
        print(f"Error setting webhook: {e}")
        return False


# ---------------- RSS FEED PROCESSING ----------------

def fetch_entries_for_user(user_id: str, since: datetime) -> list:
    """Fetch all new RSS entries for a user's feeds since the given datetime."""
    feeds = list_feeds(user_id)
    all_entries = []
    
    for feed_url in feeds:
        try:
            parsed = feedparser.parse(feed_url)
            feed_title = parsed.feed.get("title", feed_url)
            
            for entry in parsed.entries:
                published = None
                if hasattr(entry, "published"):
                    published = date_parser.parse(entry.published)
                elif hasattr(entry, "updated"):
                    published = date_parser.parse(entry.updated)
                
                if not published:
                    continue
                
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                
                if published > since:
                    content = ""
                    if hasattr(entry, "content") and entry.content:
                        content = entry.content[0].get("value", "")
                    elif hasattr(entry, "summary"):
                        content = entry.summary
                    
                    all_entries.append({
                        "title": entry.get("title", "Untitled"),
                        "link": entry.get("link", ""),
                        "published": published,
                        "summary": content[:2000],
                        "feed_name": feed_title,
                    })
        except Exception as e:
            print(f"Error parsing feed {feed_url}: {e}")
    
    return sorted(all_entries, key=lambda e: e["published"], reverse=True)


# ---------------- DIGEST BUILDER ----------------

def build_digest(entries: list, user_id: str) -> str:
    """Build a formatted daily digest message with summaries in user's preferred format."""
    if not entries:
        return "📭 <b>No new posts</b> in the last 24 hours."
    
    tier_limits = get_tier_limits(user_id)
    use_ai_summaries = tier_limits.get("ai_summaries", False) and OPENAI_API_KEY
    
    # Get user's preferred format
    format_type, custom_prompt = get_summary_format(user_id)
    
    text = f"📚 <b>Daily Digest</b> — {len(entries)} new post(s) from last 2 days\n"
    text += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    
    if use_ai_summaries:
        entries = generate_batch_summaries(
            entries, 
            max_articles=10,
            format_type=format_type,
            custom_prompt=custom_prompt,
        )
    
    for i, entry in enumerate(entries, start=1):
        pub_date = entry["published"].strftime("%b %d, %H:%M")
        title = escape_html(entry["title"])
        feed_name = escape_html(entry["feed_name"])
        
        text += f"<b>{i}. {title}</b>\n"
        text += f"📰 {feed_name} • {pub_date}\n"
        text += f"🔗 {entry['link']}\n\n"
        
        scqr = entry.get("scqr")
        if scqr:
            # Render based on format type
            if format_type == "scqr" or (format_type == "custom" and "situation" in scqr):
                text += f"<b>📋 Analysis:</b>\n"
                if "situation" in scqr:
                    text += f"<b>S:</b> {escape_html(scqr.get('situation', 'N/A'))}\n\n"
                if "complication" in scqr:
                    text += f"<b>C:</b> {escape_html(scqr.get('complication', 'N/A'))}\n\n"
                if "question" in scqr:
                    text += f"<b>Q:</b> {escape_html(scqr.get('question', 'N/A'))}\n\n"
                if "resolution" in scqr:
                    text += f"<b>R:</b> {escape_html(scqr.get('resolution', 'N/A'))}\n"
                
                # Show Timeline if present
                timeline = scqr.get("timeline")
                if timeline and isinstance(timeline, dict):
                    text += f"\n<b>📈 T (Timeline):</b>\n"
                    if timeline.get("current_state"):
                        text += f"<b>Now:</b> {escape_html(timeline['current_state'])}\n"
                    if timeline.get("growth_trajectory"):
                        text += f"<b>Trend:</b> {escape_html(timeline['growth_trajectory'])}\n"
                    if timeline.get("challenges") and isinstance(timeline["challenges"], list):
                        challenges = [escape_html(c) for c in timeline["challenges"] if c]
                        if challenges:
                            text += f"<b>Gates:</b> {'; '.join(challenges)}\n"
                    if timeline.get("future_outlook"):
                        text += f"<b>Path Forward:</b> {escape_html(timeline['future_outlook'])}\n"
                
                # Show Key Facts if present
                key_facts = scqr.get("key_facts", [])
                if key_facts and isinstance(key_facts, list) and len(key_facts) > 0:
                    text += f"\n<b>📊 Key Facts:</b>\n"
                    for fact in key_facts:
                        if fact:
                            text += f"• {escape_html(fact)}\n"
            elif format_type == "tldr" and "summary" in scqr:
                text += f"<b>📋 TL;DR:</b> {escape_html(scqr.get('summary', ''))}\n"
            elif format_type == "bullets" and "takeaways" in scqr:
                text += f"<b>📋 Key Takeaways:</b>\n"
                for takeaway in scqr.get("takeaways", []):
                    text += f"• {escape_html(takeaway)}\n"
            elif format_type == "eli5" and "explanation" in scqr:
                text += f"<b>📋 ELI5:</b> {escape_html(scqr.get('explanation', ''))}\n"
            elif format_type == "actionable":
                if "lesson" in scqr:
                    text += f"<b>📋 Lesson:</b> {escape_html(scqr.get('lesson', ''))}\n"
                if "actions" in scqr:
                    text += f"<b>Actions:</b>\n"
                    for action in scqr.get("actions", []):
                        text += f"• {escape_html(action)}\n"
            else:
                # Generic rendering for custom formats
                text += f"<b>📋 Summary:</b>\n"
                for key, value in scqr.items():
                    if key == "technical_terms":
                        continue  # Handle separately below
                    if isinstance(value, list):
                        text += f"<b>{key.title()}:</b>\n"
                        for item in value:
                            text += f"• {escape_html(str(item))}\n"
                    else:
                        text += f"<b>{key.title()}:</b> {escape_html(str(value))}\n"
            
            # Show technical terms if present
            tech_terms = scqr.get("technical_terms", [])
            if tech_terms and isinstance(tech_terms, list) and len(tech_terms) > 0:
                text += f"\n<b>📖 Terms:</b> "
                term_strs = []
                for term_obj in tech_terms:
                    if isinstance(term_obj, dict) and "term" in term_obj:
                        term_strs.append(f"<i>{escape_html(term_obj['term'])}</i>: {escape_html(term_obj.get('explanation', ''))}")
                if term_strs:
                    text += " | ".join(term_strs) + "\n"
        else:
            # No AI summary - show preview (for free users or if AI failed)
            summary = clean_html(entry.get("summary", ""))
            if summary:
                if len(summary) > 300:
                    summary = summary[:297] + "..."
                text += f"<i>{escape_html(summary)}</i>\n"
            
            # If Pro user but no summary, note the issue
            if use_ai_summaries:
                raw_summary = entry.get("summary", "")
                if not clean_html(raw_summary).strip():
                    text += f"<i>(Full article not available in RSS feed — <a href=\"{entry.get('link', '')}\">read here</a>)</i>\n"
                else:
                    text += f"<i>(Summary unavailable)</i>\n"
        
        text += "\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Upgrade prompt for regular free users only
    if not is_privileged(user_id):
        sub = get_subscription(user_id)
        if sub.get("tier") == "free":
            text += "\n💡 <i>Upgrade to Pro for AI summaries! /upgrade</i>"
    
    return text


def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------- PAYMENT HANDLERS ----------------

def handle_pre_checkout(pre_checkout_query: dict) -> None:
    """Handle pre-checkout query."""
    query_id = pre_checkout_query["id"]
    user_id = str(pre_checkout_query["from"]["id"])
    payload = pre_checkout_query.get("invoice_payload", "")
    
    print(f"Pre-checkout from {user_id}: {payload}")
    
    if payload.startswith("pro_subscription_") or payload == "test_payment":
        answer_pre_checkout(query_id, ok=True)
    else:
        answer_pre_checkout(query_id, ok=False, error_message="Invalid subscription")


def handle_successful_payment(message: dict) -> None:
    """Handle successful payment."""
    chat_id = str(message["chat"]["id"])
    user_id = str(message["from"]["id"])
    payment = message.get("successful_payment", {})
    
    total_amount = payment.get("total_amount", 0)
    currency = payment.get("currency", "")
    payload = payment.get("invoice_payload", "")
    payment_id = payment.get("telegram_payment_charge_id", "")
    
    print(f"Payment success! User {user_id} paid {total_amount} {currency}")
    
    if payload == "test_payment":
        send_message(
            chat_id,
            "✅ <b>Test Payment Successful!</b>\n\n"
            "The payment flow is working correctly.",
            html=True,
        )
        return
    
    if payload.startswith("pro_subscription_"):
        # Record payment for analytics
        record_payment(user_id, total_amount, payment_id)
        record_event("subscription_purchase", user_id, f"Pro {PRO_DURATION_DAYS} days")
        
        expires_at = (datetime.now(timezone.utc) + timedelta(days=PRO_DURATION_DAYS)).isoformat()
        
        upgrade_subscription(
            user_id=user_id,
            tier="pro",
            stripe_customer_id=None,
            stripe_subscription_id=f"telegram_stars_{payment_id}",
            expires_at=expires_at,
        )
        
        send_message(
            chat_id,
            f"🎉 <b>Welcome to Pro!</b>\n\n"
            f"Your subscription is active for {PRO_DURATION_DAYS} days.\n\n"
            f"<b>You now have:</b>\n"
            f"• Up to 50 feeds\n"
            f"• AI-powered SCQR summaries\n\n"
            f"Enjoy! 📚",
            html=True,
        )


# ---------------- USER COMMAND HANDLERS ----------------

def handle_start(chat_id: str, user_id: str) -> None:
    """Welcome message for new/returning users."""
    ensure_user(user_id)
    
    # Role indicator
    if is_owner(user_id):
        role_note = "\n\n👑 <i>You are the owner</i>"
    elif is_admin(user_id):
        role_note = "\n\n⭐ <i>You have Pro access</i>"
    else:
        role_note = ""
    
    send_message(
        chat_id,
        "👋 <b>Welcome to Substack Digest Bot!</b>\n\n"
        "Get daily digests of your favorite Substack newsletters "
        "with AI-powered summaries.\n\n"
        "Use /help to see available commands."
        f"{role_note}",
        html=True,
    )


def handle_help(chat_id: str, user_id: str) -> None:
    """Show commands based on user's role."""
    ensure_user(user_id)
    
    # Base commands for all users
    text = "<b>📋 Commands</b>\n\n"
    
    text += "<b>📰 Feed Management:</b>\n"
    text += "/feedlist — Show your subscribed feeds\n"
    text += "/addfeed &lt;url&gt; — Add a new feed\n"
    text += "/bulkadd — Add multiple feeds at once\n"
    text += "/removefeed &lt;#&gt; — Remove a feed by number\n"
    text += "/testfeed — Check all feeds are working\n"
    text += "/testfeed &lt;url&gt; — Test a specific feed\n\n"
    
    text += "<b>📚 Digest:</b>\n"
    text += "/digest — Get your digest now\n"
    text += "/settime HH:MM — Set daily digest time\n"
    
    # Show format command for Pro users
    tier_limits = get_tier_limits(user_id)
    if tier_limits.get("ai_summaries", False):
        text += "/format — Customize summary format\n"
    
    text += "\n<b>👤 Account:</b>\n"
    text += "/status — View your subscription\n"
    
    # Show upgrade only for non-privileged users
    if not is_privileged(user_id):
        text += "/upgrade — Upgrade to Pro (⭐50 Stars)\n"
    
    text += "/help — Show this message\n"
    
    # Admin section (admins just see their status, no extra commands)
    if is_admin(user_id) and not is_owner(user_id):
        text += "\n━━━━━━━━━━━━━━━━━━━━\n"
        text += "⭐ <b>Admin Status</b>\n"
        text += "<i>You have free Pro access.</i>\n"
    
    # Owner section
    if is_owner(user_id):
        text += "\n━━━━━━━━━━━━━━━━━━━━\n"
        text += "👑 <b>Owner Commands:</b>\n"
        text += "/owner — Show owner command menu\n"
    
    send_message(chat_id, text, html=True)


def handle_feedlist(chat_id: str, user_id: str) -> None:
    feeds = list_feeds(user_id)
    tier_limits = get_tier_limits(user_id)
    stats = get_user_stats(user_id)
    
    if stats["is_owner"]:
        tier_name = "👑 Owner"
    elif stats["is_admin"]:
        tier_name = "⭐ Admin"
    elif stats["tier"] == "pro":
        tier_name = "⭐ Pro"
    else:
        tier_name = "Free"
    
    if not feeds:
        send_message(
            chat_id,
            f"📭 You haven't added any feeds yet.\n\n"
            f"Use /addfeed &lt;url&gt; to add your first Substack!\n\n"
            f"<i>Plan: {tier_name} (0/{tier_limits['max_feeds']} feeds)</i>",
            html=True,
        )
        return
    
    lines = [f"<b>Your Feeds ({len(feeds)}/{tier_limits['max_feeds']}):</b>\n"]
    for i, feed in enumerate(feeds, start=1):
        lines.append(f"{i}. {feed}")
    lines.append(f"\n<i>Plan: {tier_name}</i>")
    
    send_message(chat_id, "\n".join(lines), html=True)


def handle_addfeed(chat_id: str, user_id: str, args: str) -> None:
    url = args.strip()
    
    if not url:
        send_message(
            chat_id,
            "Usage: /addfeed &lt;url&gt;\n\n"
            "Examples:\n"
            "• /addfeed https://example.substack.com\n"
            "• /addfeed https://example.substack.com/feed",
            html=True,
        )
        return
    
    success, msg = add_feed(user_id, url)
    if success:
        send_message(chat_id, f"✅ Added feed:\n{msg}")
    else:
        send_message(chat_id, f"⚠️ {msg}", html=True)


def handle_removefeed(chat_id: str, user_id: str, args: str) -> None:
    arg = args.strip()
    
    if not arg:
        send_message(
            chat_id,
            "Usage: /removefeed &lt;number or url&gt;\n\n"
            "Use /feedlist to see feed numbers.",
            html=True,
        )
        return
    
    success, msg = remove_feed(user_id, arg)
    if success:
        send_message(chat_id, f"❌ Removed:\n{msg}")
    else:
        send_message(chat_id, f"⚠️ {msg}")


def handle_bulkadd(chat_id: str, user_id: str, full_text: str) -> None:
    """Handle bulk adding feeds - works for all users."""
    import re
    
    # Extract everything after /bulkadd
    match = re.match(r'^/bulkadd\s*(.*)', full_text, re.DOTALL | re.IGNORECASE)
    if not match or not match.group(1).strip():
        send_message(
            chat_id,
            "<b>📰 Bulk Add Feeds</b>\n\n"
            "Add multiple feeds at once. Send:\n\n"
            "<code>/bulkadd\n"
            "https://example1.substack.com/feed\n"
            "https://example2.substack.com/feed\n"
            "https://newsletter.com/feed</code>\n\n"
            "<i>Each URL on a new line</i>",
            html=True
        )
        return
    
    feed_input = match.group(1).strip()
    
    # Extract all URLs from the input using regex
    url_pattern = r'https?://[^\s<>"\',]+'
    feed_urls = re.findall(url_pattern, feed_input)
    
    # Clean up URLs (remove trailing punctuation)
    cleaned_urls = []
    for url in feed_urls:
        # Remove trailing punctuation that might have been captured
        url = url.rstrip('.,;:!?)>')
        cleaned_urls.append(url)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_urls = []
    for url in cleaned_urls:
        if url.lower() not in seen:
            seen.add(url.lower())
            unique_urls.append(url)
    feed_urls = unique_urls
    
    if not feed_urls:
        send_message(chat_id, "⚠️ No valid URLs found. URLs must start with http:// or https://")
        return
    
    # Check user's feed limit
    tier_limits = get_tier_limits(user_id)
    current_feeds = list_feeds(user_id)
    max_feeds = tier_limits.get("max_feeds", 3)
    available_slots = max_feeds - len(current_feeds)
    
    if available_slots <= 0:
        send_message(
            chat_id,
            f"⚠️ You've reached your limit of {max_feeds} feeds.\n\n"
            f"Use /removefeed to remove some, or /upgrade for more!",
            html=True
        )
        return
    
    # Limit to available slots
    if len(feed_urls) > available_slots:
        feed_urls = feed_urls[:available_slots]
        send_message(
            chat_id,
            f"⚠️ You can only add {available_slots} more feed(s). Processing first {available_slots}...",
        )
    else:
        send_message(chat_id, f"⏳ Adding {len(feed_urls)} feeds...")
    
    added = []
    failed = []
    
    for url in feed_urls:
        success, msg = add_feed(user_id, url)
        if success:
            added.append(msg)
        else:
            failed.append(f"{url}: {msg}")
    
    text = f"<b>📰 Bulk Add Results</b>\n\n"
    text += f"✅ Added: {len(added)}\n"
    text += f"❌ Failed: {len(failed)}\n"
    
    if added and len(added) <= 15:
        text += f"\n<b>Added:</b>\n"
        for a in added:
            text += f"• {escape_html(a)}\n"
    elif added:
        text += f"\n<b>Added:</b>\n"
        for a in added[:10]:
            text += f"• {escape_html(a)}\n"
        text += f"<i>...and {len(added) - 10} more</i>\n"
    
    if failed and len(failed) <= 5:
        text += f"\n<b>Failed:</b>\n"
        for f in failed[:5]:
            text += f"• {escape_html(f)}\n"
    elif failed:
        text += f"\n<b>Failed:</b> {len(failed)} feeds (check URLs)\n"
    
    send_message(chat_id, text, html=True)


def handle_digest(chat_id: str, user_id: str) -> None:
    global users_processing_digest
    
    # Prevent duplicate digest requests
    if user_id in users_processing_digest:
        send_message(chat_id, "⏳ Already fetching your digest, please wait...")
        return
    
    allowed, error = check_rate_limit(user_id, "digest_request")
    if not allowed:
        send_message(chat_id, f"⚠️ {error}")
        return
    
    feeds = list_feeds(user_id)
    
    if not feeds:
        send_message(
            chat_id,
            "📭 You haven't added any feeds yet.\n\n"
            "Use /addfeed to add Substacks first!",
            html=True,
        )
        return
    
    # Mark as processing
    users_processing_digest.add(user_id)
    start_time = time.time()
    
    try:
        send_message(chat_id, "⏳ Fetching your feeds...")
        
        since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        entries = fetch_entries_for_user(user_id, since)
        
        # Filter out articles user has already seen
        from manage_feeds import get_seen_articles, mark_articles_seen, get_summary_format
        seen_articles = get_seen_articles(user_id)
        new_entries = [e for e in entries if e.get("link") not in seen_articles]
        
        if not new_entries and entries:
            send_message(
                chat_id,
                "📭 <b>No new posts</b> since your last digest.\n\n"
                f"<i>({len(entries)} post(s) from last 2 days already sent)</i>",
                html=True,
            )
            return
        
        digest = build_digest(new_entries, user_id)
        
        # Mark these articles as seen
        article_urls = [e.get("link") for e in new_entries if e.get("link")]
        if article_urls:
            mark_articles_seen(user_id, article_urls)
        
        send_message(chat_id, digest, html=True)
        
        # Track analytics
        try:
            from database import USE_POSTGRES, db_log_digest, db_log_article_delivery, db_track_activity
            if USE_POSTGRES:
                processing_time = int((time.time() - start_time) * 1000)
                format_used, _ = get_summary_format(user_id)
                
                # Log digest delivery
                db_log_digest(
                    user_id=user_id,
                    articles_count=len(new_entries),
                    feeds_count=len(feeds),
                    format_used=format_used,
                    delivery_type="manual",
                    processing_time_ms=processing_time
                )
                
                # Log each article delivered
                for entry in new_entries:
                    db_log_article_delivery(
                        user_id=user_id,
                        article_url=entry.get("link", ""),
                        article_title=entry.get("title", ""),
                        feed_url=entry.get("feed_url", ""),
                        published_at=entry.get("published").isoformat() if entry.get("published") else None
                    )
                
                # Track user activity
                db_track_activity(user_id, "digest_requested", {
                    "articles": len(new_entries),
                    "feeds": len(feeds)
                })
        except Exception as e:
            print(f"[Analytics] Error tracking: {e}")
            
    finally:
        # Always remove from processing set
        users_processing_digest.discard(user_id)


def handle_status(chat_id: str, user_id: str) -> None:
    stats = get_user_stats(user_id)
    limits = stats["tier_limits"]
    
    from manage_feeds import get_digest_time, get_summary_format
    digest_time = get_digest_time(user_id)
    summary_format, _ = get_summary_format(user_id)
    
    if stats["is_owner"]:
        tier_display = "👑 Owner"
    elif stats["is_admin"]:
        tier_display = "⭐ Admin (Pro)"
    elif stats["tier"] == "pro":
        tier_display = "⭐ Pro"
        if not stats["subscription_active"]:
            tier_display += " ⚠️ (Expired)"
    else:
        tier_display = "Free"
    
    text = f"📊 <b>Your Subscription</b>\n\n"
    text += f"<b>Plan:</b> {tier_display}\n"
    text += f"<b>Feeds:</b> {stats['feed_count']}/{limits['max_feeds']}\n"
    text += f"<b>AI Summaries:</b> {'✅' if limits['ai_summaries'] else '❌'}\n"
    text += f"<b>Digest Time:</b> {digest_time}\n"
    
    if limits['ai_summaries']:
        text += f"<b>Summary Format:</b> {summary_format.upper()}\n"
    
    if stats.get("expires_at") and stats["tier"] == "pro" and not stats["is_privileged"]:
        expiry = stats["expires_at"][:10]
        text += f"<b>Expires:</b> {expiry}\n"
    
    if not stats["is_privileged"] and stats["tier"] == "free":
        text += f"\n<b>💡 Upgrade to Pro:</b>\n"
        text += f"• 50 feeds (vs 3)\n"
        text += f"• AI-powered summaries\n"
        text += f"• Only ⭐{PRO_PRICE_STARS} Stars/month\n"
        text += f"\nUse /upgrade to subscribe!"
    
    send_message(chat_id, text, html=True)


def handle_upgrade(chat_id: str, user_id: str) -> None:
    stats = get_user_stats(user_id)
    
    if stats["is_owner"]:
        send_message(chat_id, "👑 You're the owner with full Pro access!")
        return
    
    if stats["is_admin"]:
        send_message(chat_id, "⭐ You're an admin with Pro access!")
        return
    
    if stats["tier"] == "pro" and stats["subscription_active"]:
        expiry = stats.get("expires_at", "")[:10] if stats.get("expires_at") else "N/A"
        send_message(
            chat_id,
            f"⭐ You're already on Pro!\n\n"
            f"<b>Expires:</b> {expiry}\n\n"
            f"Use /upgrade near expiry to renew.",
            html=True
        )
        return
    
    send_message(
        chat_id,
        f"⭐ <b>Upgrade to Pro</b>\n\n"
        f"<b>Price:</b> {PRO_PRICE_STARS} Telegram Stars (~$1)\n"
        f"<b>Duration:</b> {PRO_DURATION_DAYS} days\n\n"
        f"<b>Pro features:</b>\n"
        f"• Up to 50 feeds (vs 3)\n"
        f"• AI-powered SCQR summaries\n\n"
        f"Tap the button below to pay! 👇",
        html=True,
    )
    
    send_invoice(chat_id, user_id)


# ---------------- FORMAT COMMAND HANDLER ----------------

def handle_testfeed(chat_id: str, user_id: str, args: str) -> None:
    """Test if a feed URL is valid and can be fetched."""
    url = args.strip()
    
    if not url:
        # Test all user's feeds
        feeds = list_feeds(user_id)
        if not feeds:
            send_message(chat_id, "📭 You have no feeds. Use /addfeed to add one first.")
            return
        
        send_message(chat_id, f"🔍 Testing {len(feeds)} feeds...")
        
        results = []
        for feed_url in feeds:
            try:
                parsed = feedparser.parse(feed_url)
                if parsed.bozo and not parsed.entries:
                    results.append(f"❌ {feed_url}\n   <i>Error: Could not parse feed</i>")
                elif not parsed.entries:
                    results.append(f"⚠️ {feed_url}\n   <i>Warning: No entries found</i>")
                else:
                    feed_title = parsed.feed.get("title", "Unknown")
                    latest = parsed.entries[0].get("title", "No title")[:50]
                    pub_date = ""
                    if hasattr(parsed.entries[0], "published"):
                        pub_date = f" ({parsed.entries[0].published[:16]})"
                    results.append(f"✅ <b>{escape_html(feed_title)}</b>\n   Latest: {escape_html(latest)}{pub_date}")
            except Exception as e:
                results.append(f"❌ {feed_url}\n   <i>Error: {escape_html(str(e)[:50])}</i>")
        
        text = "<b>📋 Feed Status Report:</b>\n\n" + "\n\n".join(results)
        send_message(chat_id, text, html=True)
        return
    
    # Test specific URL
    send_message(chat_id, f"🔍 Testing feed: {url}")
    
    # Auto-add /feed if it's a substack URL without it
    if "substack.com" in url.lower() and not url.endswith("/feed"):
        url = url.rstrip("/") + "/feed"
    
    try:
        parsed = feedparser.parse(url)
        
        if parsed.bozo and not parsed.entries:
            error_msg = str(parsed.bozo_exception)[:100] if hasattr(parsed, 'bozo_exception') else "Unknown error"
            send_message(
                chat_id,
                f"❌ <b>Feed Error</b>\n\n"
                f"URL: {url}\n"
                f"Error: {escape_html(error_msg)}\n\n"
                f"<i>This URL may not be a valid RSS feed.</i>",
                html=True
            )
            return
        
        if not parsed.entries:
            send_message(
                chat_id,
                f"⚠️ <b>No Entries</b>\n\n"
                f"URL: {url}\n"
                f"The feed was parsed but contains no entries.\n\n"
                f"<i>The author may not have published anything yet.</i>",
                html=True
            )
            return
        
        # Success - show feed info
        feed_title = parsed.feed.get("title", "Unknown")
        num_entries = len(parsed.entries)
        
        text = f"✅ <b>Feed Valid!</b>\n\n"
        text += f"<b>Title:</b> {escape_html(feed_title)}\n"
        text += f"<b>URL:</b> {url}\n"
        text += f"<b>Entries:</b> {num_entries}\n\n"
        
        text += f"<b>Recent posts:</b>\n"
        for entry in parsed.entries[:3]:
            title = entry.get("title", "No title")[:60]
            pub = ""
            if hasattr(entry, "published"):
                pub = f" <i>({entry.published[:16]})</i>"
            text += f"• {escape_html(title)}{pub}\n"
        
        # Check if already added
        feeds = list_feeds(user_id)
        if url in feeds:
            text += f"\n✅ <i>Already in your feed list</i>"
        else:
            text += f"\n➕ <i>Not in your list. Use /addfeed {url}</i>"
        
        send_message(chat_id, text, html=True)
        
    except Exception as e:
        send_message(
            chat_id,
            f"❌ <b>Error Testing Feed</b>\n\n"
            f"URL: {url}\n"
            f"Error: {escape_html(str(e))}\n\n"
            f"<i>Check the URL and try again.</i>",
            html=True
        )


def handle_settime(chat_id: str, user_id: str, args: str) -> None:
    """Handle setting custom digest delivery time."""
    from manage_feeds import set_digest_time, get_digest_time
    
    if not args:
        current_time = get_digest_time(user_id)
        send_message(
            chat_id,
            f"⏰ <b>Digest Delivery Time</b>\n\n"
            f"<b>Current time:</b> {current_time} (your local time)\n\n"
            f"<b>To change:</b>\n"
            f"/settime HH:MM\n\n"
            f"<b>Examples:</b>\n"
            f"/settime 08:00 — Morning digest\n"
            f"/settime 18:30 — Evening digest\n"
            f"/settime 22:00 — Night digest\n\n"
            f"<i>Use 24-hour format (00:00 to 23:59)</i>",
            html=True
        )
        return
    
    time_str = args.strip()
    
    # Validate time format
    import re
    if not re.match(r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$', time_str):
        send_message(
            chat_id,
            "⚠️ Invalid time format.\n\n"
            "Use 24-hour format: HH:MM\n"
            "Examples: 08:00, 14:30, 22:00",
            html=True
        )
        return
    
    # Normalize to HH:MM format
    parts = time_str.split(':')
    time_str = f"{int(parts[0]):02d}:{parts[1]}"
    
    if set_digest_time(user_id, time_str):
        send_message(
            chat_id,
            f"✅ Digest time set to <b>{time_str}</b>\n\n"
            f"You'll receive your daily digest at this time.",
            html=True
        )
    else:
        send_message(chat_id, "⚠️ Failed to set time. Please try again.")


def handle_format(chat_id: str, user_id: str, args: str) -> None:
    """Handle summary format preferences."""
    # Check if user has Pro access
    tier_limits = get_tier_limits(user_id)
    if not tier_limits.get("ai_summaries", False):
        send_message(
            chat_id,
            "⚠️ Summary formats are a Pro feature.\n\n"
            "Upgrade to Pro to customize your summaries! /upgrade",
            html=True
        )
        return
    
    if not args:
        # Show current format and available options
        current_format, custom_prompt = get_summary_format(user_id)
        
        text = "<b>📝 Summary Format Settings</b>\n\n"
        text += f"<b>Current format:</b> {current_format.upper()}\n\n"
        text += "<b>Available formats:</b>\n"
        text += "• <code>scqr</code> — SCQRT Minto Pyramid + Timeline\n"
        text += "• <code>tldr</code> — Brief 2-3 sentence summary\n"
        text += "• <code>bullets</code> — Key takeaways as bullet points\n"
        text += "• <code>eli5</code> — Simple explanation anyone can understand\n"
        text += "• <code>actionable</code> — Actions and lessons to apply\n\n"
        text += "<b>Commands:</b>\n"
        text += "/format &lt;name&gt; — Set format (e.g., /format tldr)\n"
        text += "/format custom &lt;prompt&gt; — Set custom format\n"
        text += "/format templates — See custom prompt templates\n"
        text += "/format reset — Reset to default (SCQRT)\n"
        
        if custom_prompt:
            preview = custom_prompt[:150] + "..." if len(custom_prompt) > 150 else custom_prompt
            text += f"\n\n<b>Your custom prompt:</b>\n<i>{escape_html(preview)}</i>"
        
        send_message(chat_id, text, html=True)
        return
    
    parts = args.split(maxsplit=1)
    subcommand = parts[0].lower()
    
    if subcommand == "reset":
        clear_custom_prompt(user_id)
        send_message(chat_id, "✅ Reset to default SCQRT format.")
        return
    
    if subcommand == "templates":
        text = (
            "<b>📋 Custom Prompt Templates</b>\n\n"
            "Copy and modify these:\n\n"
            
            "<b>1. CEO Brief:</b>\n"
            "<code>/format custom Analyze for a CEO: What's the key insight? Why does it matter strategically? What's the market/competitive implication? Respond in JSON: {\"insight\": \"\", \"strategic_impact\": \"\", \"implication\": \"\"}</code>\n\n"
            
            "<b>2. Investment Lens:</b>\n"
            "<code>/format custom Analyze from an investor perspective: What's the thesis? What are the risks? What signals to watch? Include any numbers mentioned. JSON: {\"thesis\": \"\", \"risks\": [], \"signals\": [], \"key_numbers\": []}</code>\n\n"
            
            "<b>3. Tech Deep Dive:</b>\n"
            "<code>/format custom Technical analysis: What's the innovation? How does it work? What are limitations? What's the competitive landscape? JSON: {\"innovation\": \"\", \"how_it_works\": \"\", \"limitations\": [], \"competitors\": []}</code>\n\n"
            
            "<b>4. Contrarian View:</b>\n"
            "<code>/format custom Play devil's advocate: What's the main argument? What's the counter-argument? What's the author missing? JSON: {\"main_argument\": \"\", \"counter_argument\": \"\", \"blind_spots\": []}</code>\n\n"
            
            "<b>5. Learning Extract:</b>\n"
            "<code>/format custom Extract learnings: What's the core concept? What examples support it? How can I apply this? JSON: {\"concept\": \"\", \"examples\": [], \"applications\": []}</code>\n\n"
            
            "<i>Tip: Always request JSON output with specific keys for best results.</i>"
        )
        send_message(chat_id, text, html=True)
        return
    
    if subcommand == "custom":
        if len(parts) < 2:
            text = (
                "<b>📝 Custom Format</b>\n\n"
                "Create your own AI summary format!\n\n"
                "<b>Usage:</b>\n"
                "<code>/format custom [your prompt]</code>\n\n"
                "<b>Quick Example:</b>\n"
                "<code>/format custom Summarize for a busy executive: 1) Main point 2) Why it matters 3) Action to take. Respond in JSON: {\"main_point\": \"\", \"importance\": \"\", \"action\": \"\"}</code>\n\n"
                "<b>Tips:</b>\n"
                "• Request JSON output with specific keys\n"
                "• Be specific about your perspective (CEO, investor, etc.)\n"
                "• Ask for specific things (numbers, risks, actions)\n\n"
                "📋 <b>See more templates:</b> /format templates"
            )
            send_message(chat_id, text, html=True)
            return
        
        custom_prompt = parts[1]
        valid, result = validate_custom_prompt(custom_prompt)
        
        if not valid:
            send_message(chat_id, f"⚠️ {result}")
            return
        
        set_custom_prompt(user_id, result)
        send_message(
            chat_id,
            "✅ <b>Custom format saved!</b>\n\n"
            "Your next /digest will use this format.\n"
            "Use /format reset to go back to default.",
            html=True
        )
        return
    
    # Set a built-in format
    valid_formats = ["scqr", "tldr", "bullets", "eli5", "actionable"]
    if subcommand in valid_formats:
        set_summary_format(user_id, subcommand)
        format_info = SUMMARY_FORMATS[subcommand]
        send_message(
            chat_id,
            f"✅ Format set to <b>{format_info['name']}</b>\n\n"
            f"<i>{format_info['description']}</i>\n\n"
            f"Use /digest to see your new format!",
            html=True
        )
    else:
        send_message(
            chat_id,
            f"⚠️ Unknown format: {subcommand}\n\n"
            f"Available: scqr, tldr, bullets, eli5, actionable\n"
            f"Or use: /format custom <your prompt>",
            html=True
        )


# ---------------- OWNER COMMAND HANDLERS ----------------

def handle_owner(chat_id: str, user_id: str, args: str) -> None:
    """Owner-only command hub."""
    if not is_owner(user_id):
        send_message(chat_id, "⛔ This command is for the owner only.")
        return
    
    if not args:
        text = (
            "👑 <b>Owner Commands</b>\n\n"
            
            "<b>👥 Admin Management:</b>\n"
            "/owner addadmin &lt;@user&gt; — Give free Pro access\n"
            "/owner removeadmin &lt;@user&gt; — Remove Pro access\n"
            "/owner listadmins — List all admins\n\n"
            
            "<b>👤 User Management:</b>\n"
            "/owner users — List all known users\n"
            "/owner grant &lt;@user&gt; &lt;days&gt; — Gift Pro days\n"
            "/owner block &lt;@user&gt; &lt;reason&gt; — Block user\n"
            "/owner unblock &lt;@user&gt; — Unblock user\n\n"
            
            "<b>📰 Feed Management:</b>\n"
            "/owner bulkadd — Bulk add feeds (one per line)\n"
            "/owner exportfeeds — Export your feeds list\n"
            "/owner clearhistory — Reset seen articles (show all again)\n\n"
            
            "<b>📊 Analytics:</b>\n"
            "/owner stats — Overview dashboard\n"
            "/owner analytics — Detailed analytics\n"
            "/owner payments — Recent payments\n\n"
            
            "<b>🛠 Tools:</b>\n"
            "/owner testpayment — Test Stars payment\n"
            "/owner broadcast &lt;msg&gt; — Message all users"
        )
        send_message(chat_id, text, html=True)
        return
    
    parts = args.split(maxsplit=1)  # only split once so subargs gets everything after subcommand
    subcommand = parts[0].lower()
    subargs = parts[1:] if len(parts) > 1 else []
    
    if subcommand == "stats":
        stats = get_all_stats()
        payment_stats = get_payment_stats_by_period()
        
        text = (
            "📊 <b>Bot Analytics</b>\n\n"
            
            "<b>👥 Users:</b>\n"
            f"• Total: {stats['total_users']}\n"
            f"• New this month: {stats['new_users_this_month']}\n"
            f"• Pro: {stats['pro_users']} | Free: {stats['free_users']}\n"
            f"• Admins: {stats['admin_count']}\n\n"
            
            "<b>📰 Feeds:</b>\n"
            f"• Total subscribed: {stats['total_feeds']}\n\n"
            
            "<b>💰 Revenue (Stars):</b>\n"
            f"• Today: ⭐{payment_stats['today']['revenue']} ({payment_stats['today']['count']} payments)\n"
            f"• This week: ⭐{payment_stats['week']['revenue']} ({payment_stats['week']['count']} payments)\n"
            f"• This month: ⭐{payment_stats['month']['revenue']} ({payment_stats['month']['count']} payments)\n"
            f"• All time: ⭐{payment_stats['all_time']['revenue']} ({payment_stats['all_time']['count']} payments)\n\n"
            
            f"<i>~${payment_stats['all_time']['revenue'] * 0.02:.2f} USD total (before Telegram fees)</i>"
        )
        
        # Add database analytics if available
        try:
            from database import USE_POSTGRES, db_get_user_engagement_stats
            if USE_POSTGRES:
                db_stats = db_get_user_engagement_stats()
                if db_stats:
                    text += "\n\n<b>📈 Engagement:</b>\n"
                    text += f"• Active (7d): {db_stats.get('active_users_7d', 0)}\n"
                    text += f"• Digests today: {db_stats.get('digests_today', 0)}\n"
                    text += f"• Total digests: {db_stats.get('total_digests_sent', 0)}\n"
                    text += f"• Articles delivered: {db_stats.get('total_articles_delivered', 0)}\n"
                    text += f"• Avg feeds/user: {db_stats.get('avg_feeds_per_user', 0)}"
        except Exception as e:
            print(f"[Stats] Error fetching db stats: {e}")
        
        send_message(chat_id, text, html=True)
    
    elif subcommand == "analytics":
        # Detailed analytics from database
        try:
            from database import (USE_POSTGRES, db_get_user_engagement_stats, 
                                 db_get_popular_feeds, db_get_format_usage, db_get_retention_stats)
            if not USE_POSTGRES:
                send_message(chat_id, "⚠️ Database not configured. Add PostgreSQL for full analytics.")
                return
            
            engagement = db_get_user_engagement_stats()
            popular_feeds = db_get_popular_feeds(5)
            format_usage = db_get_format_usage()
            retention = db_get_retention_stats()
            
            # Get payment stats for revenue
            payment_stats = get_payment_stats()
            
            text = "<b>📊 Detailed Analytics</b>\n\n"
            
            # === COSTS & BREAKEVEN ===
            # Railway: ~$5/month (Hobby plan)
            # OpenAI: ~$0.002 per digest (gpt-4o-mini)
            # Assuming ~30 digests/user/month
            
            railway_cost = 5.00  # USD/month
            openai_per_digest = 0.002  # USD per digest
            digests_per_user_month = 30
            openai_cost_per_user = openai_per_digest * digests_per_user_month  # ~$0.06/user/month
            
            total_users = engagement.get('total_users', 0)
            pro_users = engagement.get('pro_users', 0)
            
            # Revenue: 50 Stars = $1, Telegram takes ~30%, you get ~$0.70
            # Monthly revenue = pro_users * $0.70
            revenue_per_pro = 0.70  # USD after Telegram fees
            monthly_revenue = pro_users * revenue_per_pro
            
            # Costs
            estimated_openai_cost = total_users * openai_cost_per_user
            total_monthly_cost = railway_cost + estimated_openai_cost
            
            # Breakeven calculation
            # Need: total_cost = pro_users * revenue_per_pro
            # pro_users_needed = total_cost / revenue_per_pro
            breakeven_pro_users = total_monthly_cost / revenue_per_pro if revenue_per_pro > 0 else 0
            
            # Profit/Loss
            profit_loss = monthly_revenue - total_monthly_cost
            
            text += "<b>💰 Costs & Revenue:</b>\n"
            text += f"• Railway: ${railway_cost:.2f}/mo\n"
            text += f"• OpenAI (est): ${estimated_openai_cost:.2f}/mo\n"
            text += f"• <b>Total cost: ${total_monthly_cost:.2f}/mo</b>\n\n"
            
            text += f"• Revenue: ${monthly_revenue:.2f}/mo ({pro_users} Pro)\n"
            if profit_loss >= 0:
                text += f"• ✅ Profit: <b>${profit_loss:.2f}/mo</b>\n\n"
            else:
                text += f"• ❌ Loss: <b>${abs(profit_loss):.2f}/mo</b>\n\n"
            
            text += "<b>🎯 Breakeven:</b>\n"
            text += f"• Need: <b>{int(breakeven_pro_users) + 1} Pro users</b>\n"
            text += f"• Have: {pro_users} Pro users\n"
            if pro_users >= breakeven_pro_users:
                text += f"• ✅ You're profitable!\n\n"
            else:
                needed = int(breakeven_pro_users) + 1 - pro_users
                text += f"• ⏳ Need {needed} more Pro users\n\n"
            
            # === ENGAGEMENT ===
            text += "<b>👥 User Engagement:</b>\n"
            text += f"• Total users: {total_users}\n"
            text += f"• Active (7d): {engagement.get('active_users_7d', 0)}\n"
            text += f"• Pro users: {pro_users}\n"
            conversion = (pro_users / total_users * 100) if total_users > 0 else 0
            text += f"• Conversion: {conversion:.1f}%\n"
            text += f"• Avg feeds/user: {engagement.get('avg_feeds_per_user', 0)}\n\n"
            
            text += "<b>📬 Digest Stats:</b>\n"
            text += f"• Total sent: {engagement.get('total_digests_sent', 0)}\n"
            text += f"• Today: {engagement.get('digests_today', 0)}\n"
            text += f"• Articles delivered: {engagement.get('total_articles_delivered', 0)}\n\n"
            
            if popular_feeds:
                text += "<b>🔥 Popular Feeds:</b>\n"
                for i, feed in enumerate(popular_feeds[:5], 1):
                    url = feed['feed_url']
                    # Extract domain for display
                    domain = url.split('/')[2] if '/' in url else url
                    text += f"{i}. {domain} ({feed['subscriber_count']} subs)\n"
                text += "\n"
            
            if format_usage:
                text += "<b>📝 Format Usage:</b>\n"
                for fmt in format_usage:
                    text += f"• {fmt['summary_format']}: {fmt['user_count']} users\n"
                text += "\n"
            
            if retention:
                text += "<b>📈 Retention:</b>\n"
                text += f"• Users received digest: {retention.get('users_received_digest', 0)}\n"
                text += f"• 7-day retention: {retention.get('retention_rate', 0)}%\n"
            
            send_message(chat_id, text, html=True)
        except Exception as e:
            send_message(chat_id, f"⚠️ Error fetching analytics: {e}")
    
    elif subcommand == "testpayment":
        send_message(chat_id, "🧪 Sending test payment invoice...")
        payload = {
            "chat_id": chat_id,
            "title": "Test Payment",
            "description": "This is a test payment to verify the flow.",
            "payload": "test_payment",
            "currency": "XTR",
            "prices": [{"label": "Test", "amount": 1}],
        }
        try:
            resp = requests.post(f"{TELEGRAM_API_BASE}/sendInvoice", json=payload, timeout=30)
            result = resp.json()
            if resp.ok:
                send_message(chat_id, "✅ Test invoice sent! Try paying 1 Star.")
            else:
                send_message(chat_id, f"❌ Error: {result.get('description', 'Unknown error')}")
        except Exception as e:
            send_message(chat_id, f"❌ Error: {e}")
    
    elif subcommand == "addadmin":
        if not subargs:
            send_message(chat_id, "Usage: /owner addadmin <@username or id>")
            return
        target = subargs[0]
        success, msg = add_admin(target)
        send_message(chat_id, f"{'✅' if success else '⚠️'} {msg}")
    
    elif subcommand == "removeadmin":
        if not subargs:
            send_message(chat_id, "Usage: /owner removeadmin <@username or id>")
            return
        target = subargs[0]
        success, msg = remove_admin(target)
        send_message(chat_id, f"{'✅' if success else '⚠️'} {msg}")
    
    elif subcommand == "listadmins":
        admins = list_admins()
        if not admins:
            send_message(chat_id, "No admins configured.")
        else:
            text = "<b>⭐ Admins (Pro Access):</b>\n" + "\n".join(f"• {a}" for a in admins)
            send_message(chat_id, text, html=True)
    
    elif subcommand == "users":
        users = get_all_known_users()
        if not users:
            send_message(chat_id, "No users have interacted with the bot yet.")
        else:
            lines = ["<b>👥 Known Users:</b>\n"]
            for u in users[:50]:
                username = f"@{u['username']}" if u.get('username') else ""
                name = u.get('first_name', '')
                lines.append(f"• {username} {name} (ID: {u['user_id']})")
            if len(users) > 50:
                lines.append(f"\n... and {len(users) - 50} more")
            send_message(chat_id, "\n".join(lines), html=True)
    
    elif subcommand == "payments":
        payments = get_recent_payments(10)
        if not payments:
            send_message(chat_id, "💰 No payments recorded yet.")
        else:
            lines = ["<b>💰 Recent Payments:</b>\n"]
            for p in payments:
                username = f"@{p['username']}" if p.get('username') else p['user_id']
                timestamp = p['timestamp'][:10]  # Just the date
                amount = p.get('amount', 0)
                lines.append(f"• {username}: ⭐{amount} ({timestamp})")
            send_message(chat_id, "\n".join(lines), html=True)
    
    elif subcommand == "bulkadd":
        if len(parts) < 2:
            text = (
                "<b>📰 Bulk Add Feeds</b>\n\n"
                "Add multiple feeds at once. Send:\n\n"
                "<code>/owner bulkadd\n"
                "https://example1.substack.com/feed\n"
                "https://example2.substack.com/feed\n"
                "https://example3.substack.com/feed</code>\n\n"
                "<i>Tip: Use /owner exportfeeds to get your current list for backup</i>"
            )
            send_message(chat_id, text, html=True)
            return
        
        # Parse feeds from input - handle newlines, commas, and spaces
        feed_input = parts[1]
        
        # Replace commas with newlines, then split
        feed_input = feed_input.replace(',', '\n')
        
        # Split by newlines and whitespace
        import re
        feed_urls = re.split(r'[\n\s]+', feed_input)
        
        # Filter to only valid URLs
        feed_urls = [url.strip() for url in feed_urls if url.strip().startswith('http')]
        
        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in feed_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        feed_urls = unique_urls
        
        if not feed_urls:
            send_message(chat_id, "⚠️ No valid URLs found. URLs must start with http:// or https://")
            return
        
        send_message(chat_id, f"⏳ Adding {len(feed_urls)} feeds...")
        
        added = []
        failed = []
        
        for url in feed_urls:
            success, msg = add_feed(user_id, url)
            if success:
                added.append(msg)  # msg contains the cleaned URL
            else:
                failed.append(f"{url}: {msg}")
        
        text = f"<b>📰 Bulk Add Results</b>\n\n"
        text += f"✅ Added: {len(added)}\n"
        text += f"❌ Failed: {len(failed)}\n"
        
        if added and len(added) <= 10:
            text += f"\n<b>Added:</b>\n"
            for a in added:
                text += f"• {escape_html(a)}\n"
        
        if failed and len(failed) <= 5:
            text += f"\n<b>Failed:</b>\n"
            for f in failed:
                text += f"• {escape_html(f)}\n"
        
        send_message(chat_id, text, html=True)
    
    elif subcommand == "exportfeeds":
        feeds = list_feeds(user_id)
        if not feeds:
            send_message(chat_id, "📭 You have no feeds to export.")
            return
        
        text = "<b>📰 Your Feeds (copy for backup):</b>\n\n"
        text += "<code>"
        text += "\n".join(feeds)
        text += "</code>\n\n"
        text += f"<i>Total: {len(feeds)} feeds</i>\n\n"
        text += "<i>To restore after deployment, use:\n/owner bulkadd [paste feeds]</i>"
        
        send_message(chat_id, text, html=True)
    
    elif subcommand == "clearhistory":
        from manage_feeds import clear_seen_articles
        clear_seen_articles(user_id)
        send_message(
            chat_id,
            "✅ <b>Article history cleared!</b>\n\n"
            "Your next /digest will show all articles from the last 2 days, "
            "including ones you've already seen.",
            html=True
        )
    
    elif subcommand == "block":
        if len(subargs) < 1:
            send_message(chat_id, "Usage: /owner block <@username or id> [reason]")
            return
        target = subargs[0]
        reason = parts[2] if len(parts) > 2 else "Blocked by owner"
        
        if target.startswith("@"):
            target_id = get_user_id_by_username(target)
            if not target_id:
                send_message(chat_id, f"⚠️ User {target} not found.")
                return
        else:
            target_id = target
        
        block_user(target_id, reason)
        send_message(chat_id, f"✅ Blocked {target}: {reason}")
    
    elif subcommand == "unblock":
        if not subargs:
            send_message(chat_id, "Usage: /owner unblock <@username or id>")
            return
        target = subargs[0]
        
        if target.startswith("@"):
            target_id = get_user_id_by_username(target)
            if not target_id:
                send_message(chat_id, f"⚠️ User {target} not found.")
                return
        else:
            target_id = target
        
        unblock_user(target_id)
        send_message(chat_id, f"✅ Unblocked {target}")
    
    elif subcommand == "broadcast":
        if len(parts) < 2:
            send_message(chat_id, "Usage: /owner broadcast <message>")
            return
        message = " ".join(parts[1:])
        users = get_all_users()
        sent = 0
        for uid in users:
            if send_message(uid, f"📢 <b>Announcement</b>\n\n{message}", html=True):
                sent += 1
        send_message(chat_id, f"✅ Broadcast sent to {sent}/{len(users)} users.")
    
    elif subcommand == "grant":
        if len(subargs) < 2:
            send_message(chat_id, "Usage: /owner grant <@username or id> <days>")
            return
        target = subargs[0]
        
        try:
            days = int(subargs[1])
        except ValueError:
            send_message(chat_id, "Days must be a number")
            return
        
        if target.startswith("@"):
            target_id = get_user_id_by_username(target)
            if not target_id:
                send_message(chat_id, f"⚠️ User {target} not found.")
                return
        else:
            target_id = target
        
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        upgrade_subscription(
            user_id=target_id,
            tier="pro",
            stripe_customer_id=None,
            stripe_subscription_id=f"owner_grant_{user_id}",
            expires_at=expires_at,
        )
        send_message(chat_id, f"✅ Granted Pro to {target} for {days} days.")
        send_message(target_id, f"🎁 You've been granted <b>Pro</b> for {days} days!", html=True)
    
    else:
        send_message(chat_id, "Unknown command. Use /owner for help.")


# ---------------- MESSAGE ROUTER ----------------

def handle_message(message: dict) -> None:
    """Route incoming Telegram message."""
    if "successful_payment" in message:
        handle_successful_payment(message)
        return
    
    chat_id = str(message["chat"]["id"])
    user_id = str(message["from"]["id"])
    
    # Register username
    user_data = message.get("from", {})
    username = user_data.get("username")
    first_name = user_data.get("first_name")
    register_user(user_id, username, first_name)
    
    # Auto-set owner if not set (first user becomes owner)
    if not get_owner_id():
        set_owner_id(user_id)
        print(f"Owner set to: {user_id}")
    
    text = message.get("text", "").strip()
    
    # Check if blocked
    blocked, reason = is_user_blocked(user_id)
    if blocked:
        send_message(chat_id, f"⛔ {reason}")
        return
    
    # Rate limit (owner/admins bypass)
    if not is_privileged(user_id):
        allowed, error = check_rate_limit(user_id, "command")
        if not allowed:
            send_message(chat_id, f"⚠️ {error}")
            return
    
    if not text.startswith("/"):
        return
    
    parts = text.split(maxsplit=1)
    command = parts[0].lower().split("@")[0]
    args = parts[1] if len(parts) > 1 else ""
    
    handlers = {
        "/start": lambda: handle_start(chat_id, user_id),
        "/help": lambda: handle_help(chat_id, user_id),
        "/feedlist": lambda: handle_feedlist(chat_id, user_id),
        "/addfeed": lambda: handle_addfeed(chat_id, user_id, args),
        "/removefeed": lambda: handle_removefeed(chat_id, user_id, args),
        "/bulkadd": lambda: handle_bulkadd(chat_id, user_id, text),  # Pass full text for URL parsing
        "/testfeed": lambda: handle_testfeed(chat_id, user_id, args),
        "/digest": lambda: handle_digest(chat_id, user_id),
        "/dailydigest": lambda: handle_digest(chat_id, user_id),
        "/status": lambda: handle_status(chat_id, user_id),
        "/upgrade": lambda: handle_upgrade(chat_id, user_id),
        "/format": lambda: handle_format(chat_id, user_id, args),
        "/settime": lambda: handle_settime(chat_id, user_id, args),
        "/owner": lambda: handle_owner(chat_id, user_id, args),
    }
    
    handler = handlers.get(command)
    if handler:
        handler()
    else:
        send_message(chat_id, "Unknown command. Try /help")


# ---------------- SCHEDULED DIGEST ----------------

def send_scheduled_digests():
    """Send daily digest to users whose scheduled time has arrived."""
    now = datetime.now(timezone.utc)
    current_hour = now.hour
    current_minute = now.minute
    
    print(f"[Scheduler] {now.strftime('%Y-%m-%d %H:%M')} UTC - Checking digests...")
    
    users = get_all_users()
    if not users:
        print("[Scheduler] No users found")
        return
    
    today = now.strftime("%Y-%m-%d")
    since = now - timedelta(hours=LOOKBACK_HOURS)
    
    sent_count = 0
    skipped_count = 0
    
    for user_id in users:
        try:
            # Check if already sent today - MOST IMPORTANT CHECK
            last_sent = get_last_sent_date(user_id)
            if last_sent == today:
                skipped_count += 1
                continue
            
            # Get user's preferred time
            from manage_feeds import get_digest_time
            user_time = get_digest_time(user_id)  # Returns "HH:MM"
            
            try:
                user_hour, user_minute = map(int, user_time.split(':'))
            except:
                user_hour, user_minute = 8, 0  # Default to 08:00
            
            # Check if it's time to send (must match the hour)
            if current_hour != user_hour:
                continue
            
            # Only send within first 15 minutes of the hour to avoid duplicates
            if current_minute > 15:
                continue
            
            feeds = list_feeds(user_id)
            if not feeds:
                continue
            
            print(f"[Scheduler] Sending digest to {user_id}...")
            
            entries = fetch_entries_for_user(user_id, since)
            
            # Filter out articles user has already seen
            from manage_feeds import get_seen_articles, mark_articles_seen
            seen_articles = get_seen_articles(user_id)
            new_entries = [e for e in entries if e.get("link") not in seen_articles]
            
            if not new_entries:
                print(f"[Scheduler] No new articles for {user_id}, skipping")
                set_last_sent_date(user_id, today)  # Still mark as sent today
                continue
            
            digest = build_digest(new_entries, user_id)
            
            if send_message(user_id, digest, html=True):
                # Mark articles as seen
                article_urls = [e.get("link") for e in new_entries if e.get("link")]
                if article_urls:
                    mark_articles_seen(user_id, article_urls)
                
                set_last_sent_date(user_id, today)
                sent_count += 1
                print(f"[Scheduler] ✅ Sent {len(new_entries)} articles to {user_id}")
            else:
                print(f"[Scheduler] ❌ Failed to send to {user_id}")
                
        except Exception as e:
            print(f"[Scheduler] Error for {user_id}: {e}")
    
    if sent_count > 0 or skipped_count > 0:
        print(f"[Scheduler] Done. Sent: {sent_count}, Skipped (already sent today): {skipped_count}")


def run_scheduler():
    """Run the scheduler in a background thread."""
    # Run at the start of each hour
    schedule.every().hour.at(":00").do(send_scheduled_digests)
    schedule.every().hour.at(":15").do(send_scheduled_digests)  # Backup check
    print(f"[Scheduler] Started. Will check at :00 and :15 of each hour.")
    
    while True:
        schedule.run_pending()
        time.sleep(30)


# ---------------- FLASK ROUTES ----------------

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "bot": "Substack Digest Bot",
        "time": datetime.now(timezone.utc).isoformat(),
    })


# Track processed updates to prevent duplicates
processed_updates = set()
MAX_PROCESSED_UPDATES = 1000

# Track users currently getting digests to prevent duplicate requests
users_processing_digest = set()

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    global processed_updates
    
    try:
        update = request.get_json()
        update_id = update.get('update_id')
        
        # Deduplicate - skip if already processed
        if update_id in processed_updates:
            return jsonify({"ok": True})
        
        # Track this update IMMEDIATELY
        processed_updates.add(update_id)
        
        # Limit memory usage
        if len(processed_updates) > MAX_PROCESSED_UPDATES:
            processed_updates = set(list(processed_updates)[MAX_PROCESSED_UPDATES//2:])
        
        if "pre_checkout_query" in update:
            handle_pre_checkout(update["pre_checkout_query"])
        
        if "message" in update:
            handle_message(update["message"])
        
        return jsonify({"ok": True})
    except Exception as e:
        print(f"Webhook error: {e}")
        # Still return OK to prevent Telegram retries
        return jsonify({"ok": True})


@app.route("/trigger-digest", methods=["POST"])
def trigger_digest():
    send_scheduled_digests()
    return jsonify({"ok": True, "message": "Digest triggered"})


# ---------------- MAIN ----------------

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is required")
        sys.exit(1)
    
    if RAILWAY_PUBLIC_DOMAIN:
        webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"
        print(f"Setting webhook to: {webhook_url}")
        set_webhook(webhook_url)
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    print(f"Starting server on port {PORT}...")
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
