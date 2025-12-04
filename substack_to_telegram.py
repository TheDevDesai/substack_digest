#!/usr/bin/env python3
"""
Substack to Telegram Digest Bot (Railway Edition)

Features:
- Admin and normal user profiles
- Free tier (3 feeds) and Pro tier ($1/month, 50 feeds + AI summaries)
- 24/7 operation with Telegram webhooks
- Scheduled daily digests
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
    TIERS,
    # Admin functions
    is_admin,
    add_admin,
    remove_admin,
    list_admins,
    block_user,
    unblock_user,
)

from ai_summarizer import (
    generate_scqr_summary,
    generate_batch_summaries,
    clean_html,
)


# ---------------- CONFIG ----------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
PORT = int(os.environ.get("PORT", 8080))

# Stripe configuration
STRIPE_PAYMENT_LINK = os.environ.get("STRIPE_PAYMENT_LINK", "")

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


def set_webhook(url: str) -> bool:
    """Set Telegram webhook URL."""
    try:
        resp = requests.post(
            f"{TELEGRAM_API_BASE}/setWebhook",
            json={"url": url, "allowed_updates": ["message"]},
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
    
    # Upgrade prompt for free users
    sub = get_subscription(user_id)
    if sub.get("tier") == "free" and not sub.get("is_admin"):
        text += "\n💡 <i>Upgrade to Pro for just $1/month to get AI summaries and 50 feeds! /upgrade</i>"
    
    return text


def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------- USER COMMAND HANDLERS ----------------

def handle_start(chat_id: str, user_id: str) -> None:
    ensure_user(user_id)
    admin_note = "\n\n🔑 <i>You are an admin</i>" if is_admin(user_id) else ""
    
    send_message(
        chat_id,
        "👋 <b>Welcome to Substack Digest Bot!</b>\n\n"
        "Get daily digests of your favorite Substack newsletters.\n\n"
        "<b>📋 Commands:</b>\n"
        "/feedlist — Show your subscribed feeds\n"
        "/addfeed &lt;url&gt; — Add a new feed\n"
        "/removefeed &lt;# or url&gt; — Remove a feed\n"
        "/digest — Get your digest now\n"
        "/status — View your subscription\n"
        "/upgrade — Upgrade to Pro ($1/month)\n"
        "/help — Show this message"
        f"{admin_note}",
        html=True,
    )


def handle_feedlist(chat_id: str, user_id: str) -> None:
    feeds = list_feeds(user_id)
    tier_limits = get_tier_limits(user_id)
    stats = get_user_stats(user_id)
    tier_name = "Pro 👑" if stats["is_admin"] else stats["tier"].upper()
    
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
    
    if stats["is_admin"]:
        tier_display = "👑 Admin (Pro)"
        status_emoji = "✅"
    elif stats["tier"] == "pro":
        tier_display = "⭐ Pro"
        status_emoji = "✅" if stats["subscription_active"] else "⚠️"
    else:
        tier_display = "Free"
        status_emoji = "✅"
    
    text = f"📊 <b>Your Subscription</b>\n\n"
    text += f"<b>Plan:</b> {tier_display} {status_emoji}\n"
    text += f"<b>Feeds:</b> {stats['feed_count']}/{limits['max_feeds']}\n"
    text += f"<b>AI Summaries:</b> {'✅' if limits['ai_summaries'] else '❌'}\n"
    
    if stats.get("expires_at") and stats["tier"] == "pro" and not stats["is_admin"]:
        text += f"<b>Renews:</b> {stats['expires_at'][:10]}\n"
    
    if stats["tier"] == "free" and not stats["is_admin"]:
        text += f"\n<b>💡 Upgrade to Pro for just $1/month:</b>\n"
        text += f"• 50 feeds (vs 3)\n"
        text += f"• AI-powered SCQR summaries\n"
        text += f"\nUse /upgrade to subscribe!"
    
    send_message(chat_id, text, html=True)


def handle_upgrade(chat_id: str, user_id: str) -> None:
    stats = get_user_stats(user_id)
    
    if stats["is_admin"]:
        send_message(chat_id, "👑 You're an admin with Pro features already!")
        return
    
    if stats["tier"] == "pro" and stats["subscription_active"]:
        send_message(chat_id, "⭐ You're already on the Pro plan!")
        return
    
    if STRIPE_PAYMENT_LINK:
        text = (
            "⭐ <b>Upgrade to Pro — $1/month</b>\n\n"
            "<b>Pro features:</b>\n"
            "• Up to 50 feeds (vs 3 on free)\n"
            "• AI-powered SCQR summaries\n"
            "• Priority support\n\n"
            f"👉 <a href=\"{STRIPE_PAYMENT_LINK}\">Click here to subscribe</a>\n\n"
            "<i>After payment, send /status to confirm your upgrade.</i>"
        )
    else:
        text = (
            "⭐ <b>Upgrade to Pro — $1/month</b>\n\n"
            "<b>Pro features:</b>\n"
            "• Up to 50 feeds (vs 3 on free)\n"
            "• AI-powered SCQR summaries\n"
            "• Priority support\n\n"
            "<i>Payment link coming soon! Contact admin for early access.</i>"
        )
    
    send_message(chat_id, text, html=True)


# ---------------- ADMIN COMMAND HANDLERS ----------------

def handle_admin(chat_id: str, user_id: str, args: str) -> None:
    """Admin command hub."""
    if not is_admin(user_id):
        send_message(chat_id, "⛔ This command is for admins only.")
        return
    
    if not args:
        text = (
            "🔑 <b>Admin Commands</b>\n\n"
            "/admin stats — Bot statistics\n"
            "/admin addadmin &lt;user_id&gt; — Add new admin\n"
            "/admin removeadmin &lt;user_id&gt; — Remove admin\n"
            "/admin listadmins — List all admins\n"
            "/admin block &lt;user_id&gt; &lt;reason&gt; — Block user\n"
            "/admin unblock &lt;user_id&gt; — Unblock user\n"
            "/admin broadcast &lt;message&gt; — Message all users"
        )
        send_message(chat_id, text, html=True)
        return
    
    parts = args.split(maxsplit=2)
    subcommand = parts[0].lower()
    subargs = parts[1:] if len(parts) > 1 else []
    
    if subcommand == "stats":
        stats = get_all_stats()
        text = (
            "📊 <b>Bot Statistics</b>\n\n"
            f"<b>Total Users:</b> {stats['total_users']}\n"
            f"<b>Pro Users:</b> {stats['pro_users']}\n"
            f"<b>Free Users:</b> {stats['free_users']}\n"
            f"<b>Admins:</b> {stats['admin_count']}\n"
            f"<b>Total Feeds:</b> {stats['total_feeds']}"
        )
        send_message(chat_id, text, html=True)
    
    elif subcommand == "addadmin":
        if not subargs:
            send_message(chat_id, "Usage: /admin addadmin <user_id>")
            return
        target_id = subargs[0]
        success, msg = add_admin(target_id)
        send_message(chat_id, f"{'✅' if success else '⚠️'} {msg}")
    
    elif subcommand == "removeadmin":
        if not subargs:
            send_message(chat_id, "Usage: /admin removeadmin <user_id>")
            return
        target_id = subargs[0]
        success, msg = remove_admin(target_id)
        send_message(chat_id, f"{'✅' if success else '⚠️'} {msg}")
    
    elif subcommand == "listadmins":
        admins = list_admins()
        if not admins:
            send_message(chat_id, "No admins configured.")
        else:
            text = "<b>👑 Admins:</b>\n" + "\n".join(f"• {a}" for a in admins)
            send_message(chat_id, text, html=True)
    
    elif subcommand == "block":
        if len(subargs) < 2:
            send_message(chat_id, "Usage: /admin block <user_id> <reason>")
            return
        target_id = subargs[0]
        reason = " ".join(parts[2:]) if len(parts) > 2 else "Blocked by admin"
        block_user(target_id, reason)
        send_message(chat_id, f"✅ User {target_id} blocked: {reason}")
    
    elif subcommand == "unblock":
        if not subargs:
            send_message(chat_id, "Usage: /admin unblock <user_id>")
            return
        target_id = subargs[0]
        unblock_user(target_id)
        send_message(chat_id, f"✅ User {target_id} unblocked.")
    
    elif subcommand == "broadcast":
        if not subargs:
            send_message(chat_id, "Usage: /admin broadcast <message>")
            return
        message = " ".join(parts[1:])
        users = get_all_users()
        sent = 0
        for uid in users:
            if send_message(uid, f"📢 <b>Announcement</b>\n\n{message}", html=True):
                sent += 1
        send_message(chat_id, f"✅ Broadcast sent to {sent}/{len(users)} users.")
    
    else:
        send_message(chat_id, "Unknown admin command. Use /admin for help.")


# ---------------- MESSAGE ROUTER ----------------

def handle_message(message: dict) -> None:
    """Route incoming Telegram message to appropriate handler."""
    chat_id = str(message["chat"]["id"])
    user_id = str(message["from"]["id"])
    text = message.get("text", "").strip()
    
    # Check if blocked
    blocked, reason = is_user_blocked(user_id)
    if blocked:
        send_message(chat_id, f"⛔ {reason}")
        return
    
    # Rate limit (admins bypass)
    if not is_admin(user_id):
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
        "/help": lambda: handle_start(chat_id, user_id),
        "/feedlist": lambda: handle_feedlist(chat_id, user_id),
        "/addfeed": lambda: handle_addfeed(chat_id, user_id, args),
        "/removefeed": lambda: handle_removefeed(chat_id, user_id, args),
        "/digest": lambda: handle_digest(chat_id, user_id),
        "/dailydigest": lambda: handle_digest(chat_id, user_id),
        "/status": lambda: handle_status(chat_id, user_id),
        "/upgrade": lambda: handle_upgrade(chat_id, user_id),
        "/admin": lambda: handle_admin(chat_id, user_id, args),
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
    
    # Set up webhook
    if RAILWAY_PUBLIC_DOMAIN:
        webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"
        print(f"Setting webhook to: {webhook_url}")
        set_webhook(webhook_url)
    
    # Start scheduler
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    print(f"Starting server on port {PORT}...")
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
