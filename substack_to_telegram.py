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
)

from ai_summarizer import (
    generate_batch_summaries,
    clean_html,
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

LOOKBACK_HOURS = 24
DIGEST_HOUR_UTC = 0
DIGEST_MINUTE_UTC = 0

app = Flask(__name__)


# ---------------- TELEGRAM HELPERS ----------------

def send_message(chat_id: str, text: str, html: bool = False, reply_markup: dict = None) -> bool:
    """Send a message via Telegram Bot API."""
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"
    
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
    """Build a formatted daily digest message with SCQR summaries."""
    if not entries:
        return "📭 <b>No new posts</b> in the last 24 hours."
    
    tier_limits = get_tier_limits(user_id)
    use_ai_summaries = tier_limits.get("ai_summaries", False) and OPENAI_API_KEY
    
    text = f"📚 <b>Daily Digest</b> — {len(entries)} new post(s)\n"
    text += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    
    if use_ai_summaries:
        entries = generate_batch_summaries(entries, max_articles=10)
    
    for i, entry in enumerate(entries, start=1):
        pub_date = entry["published"].strftime("%b %d, %H:%M")
        title = escape_html(entry["title"])
        feed_name = escape_html(entry["feed_name"])
        
        text += f"<b>{i}. {title}</b>\n"
        text += f"📰 {feed_name} • {pub_date}\n"
        text += f"🔗 {entry['link']}\n\n"
        
        scqr = entry.get("scqr")
        if scqr:
            text += f"<b>📋 SCQR Summary:</b>\n"
            text += f"<b>S:</b> {escape_html(scqr.get('situation', 'N/A'))}\n"
            text += f"<b>C:</b> {escape_html(scqr.get('complication', 'N/A'))}\n"
            text += f"<b>Q:</b> {escape_html(scqr.get('question', 'N/A'))}\n"
            text += f"<b>R:</b> {escape_html(scqr.get('resolution', 'N/A'))}\n"
        else:
            summary = clean_html(entry.get("summary", ""))
            if summary:
                if len(summary) > 200:
                    summary = summary[:197] + "..."
                text += f"<i>{summary}</i>\n"
        
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
    text += "/removefeed &lt;#&gt; — Remove a feed by number\n\n"
    
    text += "<b>📚 Digest:</b>\n"
    text += "/digest — Get your digest now\n\n"
    
    text += "<b>👤 Account:</b>\n"
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


def handle_digest(chat_id: str, user_id: str) -> None:
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
    
    send_message(chat_id, "⏳ Fetching your feeds...")
    
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    entries = fetch_entries_for_user(user_id, since)
    digest = build_digest(entries, user_id)
    
    send_message(chat_id, digest, html=True)


def handle_status(chat_id: str, user_id: str) -> None:
    stats = get_user_stats(user_id)
    limits = stats["tier_limits"]
    
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
            
            "<b>📊 Analytics:</b>\n"
            "/owner stats — Full analytics dashboard\n"
            "/owner payments — Recent payments\n\n"
            
            "<b>🛠 Tools:</b>\n"
            "/owner testpayment — Test Stars payment\n"
            "/owner broadcast &lt;msg&gt; — Message all users"
        )
        send_message(chat_id, text, html=True)
        return
    
    parts = args.split(maxsplit=2)
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
        send_message(chat_id, text, html=True)
    
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
        "/digest": lambda: handle_digest(chat_id, user_id),
        "/dailydigest": lambda: handle_digest(chat_id, user_id),
        "/status": lambda: handle_status(chat_id, user_id),
        "/upgrade": lambda: handle_upgrade(chat_id, user_id),
        "/owner": lambda: handle_owner(chat_id, user_id, args),
    }
    
    handler = handlers.get(command)
    if handler:
        handler()
    else:
        send_message(chat_id, "Unknown command. Try /help")


# ---------------- SCHEDULED DIGEST ----------------

def send_scheduled_digests():
    """Send daily digest to all users."""
    print(f"[{datetime.now(timezone.utc)}] Running scheduled digest...")
    
    users = get_all_users()
    if not users and TELEGRAM_CHAT_ID:
        users = [TELEGRAM_CHAT_ID]
    
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    
    for user_id in users:
        try:
            last_sent = get_last_sent_date(user_id)
            if last_sent == today:
                continue
            
            feeds = list_feeds(user_id)
            if not feeds:
                continue
            
            entries = fetch_entries_for_user(user_id, since)
            digest = build_digest(entries, user_id)
            
            if send_message(user_id, digest, html=True):
                set_last_sent_date(user_id, today)
                print(f"Digest sent to {user_id}")
        except Exception as e:
            print(f"Error sending digest to {user_id}: {e}")


def run_scheduler():
    """Run the scheduler in a background thread."""
    schedule.every().day.at(f"{DIGEST_HOUR_UTC:02d}:{DIGEST_MINUTE_UTC:02d}").do(send_scheduled_digests)
    print(f"Scheduler started. Digest at {DIGEST_HOUR_UTC:02d}:{DIGEST_MINUTE_UTC:02d} UTC")
    
    while True:
        schedule.run_pending()
        time.sleep(60)


# ---------------- FLASK ROUTES ----------------

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "bot": "Substack Digest Bot",
        "time": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        update = request.get_json()
        print(f"Received update: {update.get('update_id', 'N/A')}")
        
        if "pre_checkout_query" in update:
            handle_pre_checkout(update["pre_checkout_query"])
        
        if "message" in update:
            handle_message(update["message"])
        
        return jsonify({"ok": True})
    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


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
