#!/usr/bin/env python3
"""
Substack to Telegram Digest Bot

Fetches RSS feeds from Substack newsletters and sends daily digests to Telegram.
Supports per-user feed management, AI-powered SCQR summaries, and subscriptions.

Modes:
    - Daily digest (scheduled): Sends digest to all subscribed users
    - Commands (manual): Polls for Telegram commands to manage feeds
"""

import os
import sys
import time
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as date_parser
from typing import Optional

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
    TIERS,
)

from ai_summarizer import (
    generate_scqr_summary,
    generate_batch_summaries,
    clean_html,
)

# Try to import Stripe functions (optional)
try:
    from stripe_webhook import create_checkout_session, create_billing_portal_session
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False


# ---------------- CONFIG ----------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Webhook URLs for subscription flow
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "https://your-app.com")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# How far back to look for articles (in hours)
LOOKBACK_HOURS = 24


# ---------------- TELEGRAM HELPERS ----------------

def send_message(chat_id: str, text: str, html: bool = False, reply_markup: dict = None) -> bool:
    """Send a message via Telegram Bot API."""
    # Telegram has a 4096 character limit
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


def get_updates(offset: int = None, timeout: int = 20) -> list:
    """Get updates from Telegram (long polling)."""
    params = {"timeout": timeout}
    if offset:
        params["offset"] = offset
    
    try:
        resp = requests.get(f"{TELEGRAM_API_BASE}/getUpdates", params=params, timeout=timeout + 5)
        data = resp.json()
        return data.get("result", [])
    except requests.RequestException as e:
        print(f"Error getting updates: {e}")
        return []


# ---------------- RSS FEED PROCESSING ----------------

def fetch_entries_for_user(user_id: str, since: datetime) -> list:
    """
    Fetch all new RSS entries for a user's feeds since the given datetime.
    
    Returns:
        List of entry dicts sorted by published date (newest first)
    """
    feeds = list_feeds(user_id)
    all_entries = []
    
    for feed_url in feeds:
        try:
            parsed = feedparser.parse(feed_url)
            feed_title = parsed.feed.get("title", feed_url)
            
            for entry in parsed.entries:
                # Get published date
                published = None
                if hasattr(entry, "published"):
                    published = date_parser.parse(entry.published)
                elif hasattr(entry, "updated"):
                    published = date_parser.parse(entry.updated)
                
                if not published:
                    continue
                
                # Ensure timezone-aware
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                
                # Filter by date
                if published > since:
                    # Get content for summarization
                    content = ""
                    if hasattr(entry, "content") and entry.content:
                        content = entry.content[0].get("value", "")
                    elif hasattr(entry, "summary"):
                        content = entry.summary
                    
                    all_entries.append({
                        "title": entry.get("title", "Untitled"),
                        "link": entry.get("link", ""),
                        "published": published,
                        "summary": content[:2000],  # Limit for API
                        "feed_name": feed_title,
                    })
        except Exception as e:
            print(f"Error parsing feed {feed_url}: {e}")
    
    # Sort by date, newest first
    return sorted(all_entries, key=lambda e: e["published"], reverse=True)


# ---------------- DIGEST BUILDER ----------------

def build_digest(entries: list, user_id: str) -> str:
    """
    Build a formatted daily digest message with SCQR summaries.
    
    Args:
        entries: List of RSS entry dicts
        user_id: User ID for checking subscription tier
    
    Returns:
        HTML-formatted message string
    """
    if not entries:
        return "📭 <b>No new posts</b> in the last 24 hours."
    
    # Check if user has AI summaries enabled
    tier_limits = get_tier_limits(user_id)
    use_ai_summaries = tier_limits.get("ai_summaries", False) and OPENAI_API_KEY
    
    text = f"📚 <b>Daily Digest</b> — {len(entries)} new post(s)\n"
    text += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Generate AI summaries if enabled
    if use_ai_summaries:
        entries = generate_batch_summaries(entries, max_articles=10)
    
    for i, entry in enumerate(entries, start=1):
        pub_date = entry["published"].strftime("%b %d, %H:%M")
        title = escape_html(entry["title"])
        feed_name = escape_html(entry["feed_name"])
        
        text += f"<b>{i}. {title}</b>\n"
        text += f"📰 {feed_name} • {pub_date}\n"
        text += f"🔗 {entry['link']}\n\n"
        
        # Add SCQR summary if available
        scqr = entry.get("scqr")
        if scqr:
            text += f"<b>📋 SCQR Summary:</b>\n"
            text += f"<b>S:</b> {escape_html(scqr.get('situation', 'N/A'))}\n"
            text += f"<b>C:</b> {escape_html(scqr.get('complication', 'N/A'))}\n"
            text += f"<b>Q:</b> {escape_html(scqr.get('question', 'N/A'))}\n"
            text += f"<b>R:</b> {escape_html(scqr.get('resolution', 'N/A'))}\n"
        else:
            # Fallback: show cleaned RSS summary
            summary = clean_html(entry.get("summary", ""))
            if summary:
                if len(summary) > 200:
                    summary = summary[:197] + "..."
                text += f"<i>{summary}</i>\n"
        
        text += "\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Add tier info for free users
    tier = get_subscription(user_id).get("tier", "free")
    if tier == "free" and OPENAI_API_KEY:
        text += "\n💡 <i>Upgrade to get AI-powered SCQR summaries! /upgrade</i>"
    
    return text


def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------- COMMAND HANDLERS ----------------

def handle_start(chat_id: str, user_id: str) -> None:
    """Handle /start command."""
    ensure_user(user_id)
    send_message(
        chat_id,
        "👋 <b>Welcome to Substack Digest Bot!</b>\n\n"
        "I'll send you daily digests of your favorite Substack newsletters "
        "with AI-powered summaries.\n\n"
        "<b>📋 Commands:</b>\n"
        "/feedlist — Show your subscribed feeds\n"
        "/addfeed &lt;url&gt; — Add a new feed\n"
        "/removefeed &lt;# or url&gt; — Remove a feed\n"
        "/digest — Get your digest now\n"
        "/status — View your subscription status\n"
        "/upgrade — Upgrade for more feeds & AI summaries\n"
        "/help — Show this message",
        html=True,
    )


def handle_feedlist(chat_id: str, user_id: str) -> None:
    """Handle /feedlist command."""
    feeds = list_feeds(user_id)
    tier_limits = get_tier_limits(user_id)
    tier = get_subscription(user_id).get("tier", "free")
    
    if not feeds:
        send_message(
            chat_id,
            f"📭 You haven't added any feeds yet.\n\n"
            f"Use /addfeed &lt;url&gt; to add your first Substack!\n\n"
            f"<i>Your plan: {tier.upper()} (0/{tier_limits['max_feeds']} feeds)</i>",
            html=True,
        )
        return
    
    lines = [f"<b>Your Feeds ({len(feeds)}/{tier_limits['max_feeds']}):</b>\n"]
    for i, feed in enumerate(feeds, start=1):
        lines.append(f"{i}. {feed}")
    
    lines.append(f"\n<i>Plan: {tier.upper()}</i>")
    
    send_message(chat_id, "\n".join(lines), html=True)


def handle_addfeed(chat_id: str, user_id: str, args: str) -> None:
    """Handle /addfeed command."""
    url = args.strip()
    
    if not url:
        send_message(
            chat_id,
            "Usage: /addfeed &lt;url&gt;\n\n"
            "Example: /addfeed https://example.substack.com/feed",
            html=True,
        )
        return
    
    success, msg = add_feed(user_id, url)
    
    if success:
        send_message(chat_id, f"✅ Added feed:\n{msg}")
    else:
        send_message(chat_id, f"⚠️ {msg}")


def handle_removefeed(chat_id: str, user_id: str, args: str) -> None:
    """Handle /removefeed command."""
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
    """Handle /digest command - send immediate digest."""
    # Check rate limit
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
    
    send_message(chat_id, "⏳ Fetching your feeds and generating summaries...")
    
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    entries = fetch_entries_for_user(user_id, since)
    digest = build_digest(entries, user_id)
    
    send_message(chat_id, digest, html=True)


def handle_status(chat_id: str, user_id: str) -> None:
    """Handle /status command - show subscription status."""
    stats = get_user_stats(user_id)
    tier = stats["tier"]
    limits = stats["tier_limits"]
    
    status_emoji = "✅" if stats["subscription_active"] else "⚠️"
    
    text = f"📊 <b>Your Subscription Status</b>\n\n"
    text += f"<b>Plan:</b> {tier.upper()} {status_emoji}\n"
    text += f"<b>Feeds:</b> {stats['feed_count']}/{limits['max_feeds']}\n"
    text += f"<b>AI Summaries:</b> {'✅ Enabled' if limits['ai_summaries'] else '❌ Upgrade to enable'}\n"
    
    if stats.get("expires_at") and tier != "free":
        text += f"<b>Renews:</b> {stats['expires_at'][:10]}\n"
    
    text += f"\n<b>Available Plans:</b>\n"
    for plan_name, plan_limits in TIERS.items():
        if plan_name == tier:
            text += f"→ {plan_name.upper()}: {plan_limits['max_feeds']} feeds, "
        else:
            text += f"  {plan_name.upper()}: {plan_limits['max_feeds']} feeds, "
        text += f"AI: {'✅' if plan_limits['ai_summaries'] else '❌'}"
        if plan_limits['price_monthly'] > 0:
            text += f" — ${plan_limits['price_monthly']}/mo"
        text += "\n"
    
    if tier == "free":
        text += "\n💡 Use /upgrade to get more feeds and AI summaries!"
    
    send_message(chat_id, text, html=True)


def handle_upgrade(chat_id: str, user_id: str, args: str) -> None:
    """Handle /upgrade command - show upgrade options."""
    tier = args.strip().lower() if args else None
    
    if tier and tier in ["basic", "pro"]:
        if STRIPE_AVAILABLE:
            # Generate checkout URL
            checkout_url = create_checkout_session(
                telegram_user_id=user_id,
                tier=tier,
                success_url=f"{WEBHOOK_BASE_URL}/success?user={user_id}",
                cancel_url=f"{WEBHOOK_BASE_URL}/cancel",
            )
            
            if checkout_url:
                send_message(
                    chat_id,
                    f"🔗 <b>Upgrade to {tier.upper()}</b>\n\n"
                    f"Click below to complete your subscription:\n"
                    f"{checkout_url}",
                    html=True,
                )
            else:
                send_message(chat_id, "⚠️ Unable to create checkout. Please try again.")
        else:
            send_message(
                chat_id,
                f"💳 To upgrade to {tier.upper()}, please contact support.",
            )
    else:
        # Show upgrade options
        text = "🚀 <b>Upgrade Your Plan</b>\n\n"
        
        for plan_name, plan_limits in TIERS.items():
            if plan_name == "free":
                continue
            
            text += f"<b>{plan_name.upper()}</b> — ${plan_limits['price_monthly']}/month\n"
            text += f"  • Up to {plan_limits['max_feeds']} feeds\n"
            text += f"  • AI-powered SCQR summaries\n"
            text += f"  → /upgrade {plan_name}\n\n"
        
        text += "Choose a plan by typing /upgrade basic or /upgrade pro"
        
        send_message(chat_id, text, html=True)


def handle_manage(chat_id: str, user_id: str) -> None:
    """Handle /manage command - manage subscription."""
    if STRIPE_AVAILABLE:
        portal_url = create_billing_portal_session(
            telegram_user_id=user_id,
            return_url=f"{WEBHOOK_BASE_URL}/",
        )
        
        if portal_url:
            send_message(
                chat_id,
                f"⚙️ <b>Manage Subscription</b>\n\n"
                f"Click below to manage your billing:\n"
                f"{portal_url}",
                html=True,
            )
        else:
            send_message(
                chat_id,
                "⚠️ No active subscription found.\n"
                "Use /upgrade to subscribe!",
            )
    else:
        send_message(chat_id, "Billing management coming soon!")


def handle_message(message: dict) -> None:
    """Route incoming Telegram message to appropriate handler."""
    chat_id = str(message["chat"]["id"])
    user_id = str(message["from"]["id"])
    text = message.get("text", "").strip()
    
    # Check if user is blocked
    blocked, reason = is_user_blocked(user_id)
    if blocked:
        send_message(chat_id, f"⛔ {reason}")
        return
    
    # Check rate limit for commands
    allowed, error = check_rate_limit(user_id, "command")
    if not allowed:
        send_message(chat_id, f"⚠️ {error}")
        return
    
    if not text.startswith("/"):
        return
    
    # Parse command and arguments
    parts = text.split(maxsplit=1)
    command = parts[0].lower().split("@")[0]  # Handle /command@botname
    args = parts[1] if len(parts) > 1 else ""
    
    # Route to handler
    handlers = {
        "/start": lambda: handle_start(chat_id, user_id),
        "/help": lambda: handle_start(chat_id, user_id),
        "/feedlist": lambda: handle_feedlist(chat_id, user_id),
        "/addfeed": lambda: handle_addfeed(chat_id, user_id, args),
        "/removefeed": lambda: handle_removefeed(chat_id, user_id, args),
        "/digest": lambda: handle_digest(chat_id, user_id),
        "/dailydigest": lambda: handle_digest(chat_id, user_id),
        "/status": lambda: handle_status(chat_id, user_id),
        "/upgrade": lambda: handle_upgrade(chat_id, user_id, args),
        "/manage": lambda: handle_manage(chat_id, user_id),
    }
    
    handler = handlers.get(command)
    if handler:
        handler()
    else:
        send_message(
            chat_id,
            "Unknown command. Try /help for available commands.",
        )


# ---------------- MAIN MODES ----------------

def run_command_mode(duration_seconds: int = 300) -> None:
    """
    Poll for Telegram commands for a limited duration.
    
    Args:
        duration_seconds: How long to poll (default 5 minutes)
    """
    print(f"Starting command mode for {duration_seconds} seconds...")
    
    start_time = time.time()
    last_update_id = None
    
    while time.time() - start_time < duration_seconds:
        try:
            updates = get_updates(offset=last_update_id, timeout=10)
            
            for update in updates:
                last_update_id = update["update_id"] + 1
                
                if "message" in update:
                    handle_message(update["message"])
        
        except KeyboardInterrupt:
            print("Interrupted.")
            break
        except Exception as e:
            print(f"Error in command loop: {e}")
            time.sleep(2)
    
    print("Command mode finished.")


def run_digest_mode() -> None:
    """Send daily digest to all subscribed users."""
    print("Running digest mode...")
    
    # Get all users
    users = get_all_users()
    
    if not users:
        # Fallback to TELEGRAM_CHAT_ID if no users
        if TELEGRAM_CHAT_ID:
            users = [TELEGRAM_CHAT_ID]
        else:
            print("No users configured.")
            return
    
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    
    for user_id in users:
        try:
            # Check if already sent today
            last_sent = get_last_sent_date(user_id)
            if last_sent == today:
                print(f"Digest already sent to {user_id} today, skipping.")
                continue
            
            # Check if user has feeds
            feeds = list_feeds(user_id)
            if not feeds:
                print(f"User {user_id} has no feeds, skipping.")
                continue
            
            # Fetch and send digest
            entries = fetch_entries_for_user(user_id, since)
            digest = build_digest(entries, user_id)
            
            success = send_message(user_id, digest, html=True)
            
            if success:
                set_last_sent_date(user_id, today)
                print(f"Digest sent to {user_id} with {len(entries)} entries.")
            else:
                print(f"Failed to send digest to {user_id}.")
        
        except Exception as e:
            print(f"Error sending digest to {user_id}: {e}")
    
    print("Digest mode finished.")


def main() -> None:
    """Main entry point."""
    # Validate required env vars
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable is required.")
        sys.exit(1)
    
    # Parse command line args
    if "--commands" in sys.argv or "--commands-only" in sys.argv:
        # Command mode: poll for 5 minutes (suitable for manual GitHub Action trigger)
        duration = 300
        for arg in sys.argv:
            if arg.startswith("--duration="):
                duration = int(arg.split("=")[1])
        run_command_mode(duration_seconds=duration)
    else:
        # Default: digest mode
        run_digest_mode()


if __name__ == "__main__":
    main()
