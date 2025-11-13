import os
import sys
import time
import feedparser
import requests
from datetime import datetime, timezone
from dateutil import parser as date_parser

# Import feed management
from manage_feeds import load_feeds, add_feed, remove_feed, list_feeds


# ---------------- CONFIG ----------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
TELEGRAM_UPDATES_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
LAST_RUN_FILE = "last_run.txt"


# ---------------- HELPERS ----------------

def reply(chat_id, text, html=False):
    """Send a Telegram message."""
    payload = {"chat_id": chat_id, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    requests.post(TELEGRAM_SEND_URL, json=payload)


def get_last_run():
    """Returns last run datetime for digest mode."""
    if not os.path.exists(LAST_RUN_FILE):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        ts = float(open(LAST_RUN_FILE).read().strip())
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def set_last_run():
    """Saves timestamp after sending daily digest."""
    open(LAST_RUN_FILE, "w").write(str(time.time()))


# ---------------- FETCH SUBSTACK POSTS ----------------

def fetch_new_entries():
    cutoff = get_last_run()
    all_entries = []

    feeds = load_feeds()
    for feed_url in feeds:
        parsed = feedparser.parse(feed_url)

        for entry in parsed.entries:
            if hasattr(entry, "published"):
                published = date_parser.parse(entry.published)
            elif hasattr(entry, "updated"):
                published = date_parser.parse(entry.updated)
            else:
                continue

            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)

            if published > cutoff:
                all_entries.append({
                    "title": entry.title,
                    "link": entry.link,
                    "published": published,
                    "summary": getattr(entry, "summary", "")
                })

    return sorted(all_entries, key=lambda e: e["published"], reverse=True)


# ---------------- DIGEST BUILDER (SCQR FORMAT) ----------------

def build_daily_digest(entries):
    if not entries:
        return "📭 No new Substack posts since last update."

    text = "<b>📚 Substack Daily Digest</b>\n\n"

    for i, a in enumerate(entries, start=1):
        pub = a["published"].strftime("%Y-%m-%d %H:%M")
        text += (
            f"<b>Article {i}:</b> <a href=\"{a['link']}\">{a['title']}</a>\n"
            f"<b>Published:</b> {pub}\n"
            f"<b>SCQR Summary:</b>\n"
            f"S: {a['summary'][:200]}...\n"
            f"C: (context added later)\n"
            f"Q: (question added later)\n"
            f"R: (resolution added later)\n"
            "-----------------------\n"
        )

    return text


# ---------------- TELEGRAM COMMAND HANDLER ----------------

def handle_bot_commands(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    # /start help menu
    if text == "/start":
        reply(chat_id,
              "Commands:\n"
              "/addfeed <url> – add a new feed\n"
              "/removefeed <url or index> – remove a feed\n"
              "/feedlist – list current feeds\n"
              "/dailydigest – generate digest now")
        return

    # /feedlist
    if text.startswith("/feedlist"):
        feeds = list_feeds()
        if not feeds:
            reply(chat_id, "No feeds configured. Add with /addfeed <url>")
            return
        lines = ["<b>Current Feeds:</b>"]
        for i, f in enumerate(feeds, start=1):
            lines.append(f"{i}. {f}")
        reply(chat_id, "\n".join(lines), html=True)
        return

    # /dailydigest (manual trigger)
    if text.startswith("/dailydigest"):
        entries = fetch_new_entries()
        digest = build_daily_digest(entries)
        reply(chat_id, digest, html=True)
        return

    # /addfeed
    if text.startswith("/addfeed"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            reply(chat_id, "Usage: /addfeed <url>")
            return
        url = parts[1].strip()
        ok, msg = add_feed(url)
        if ok:
            reply(chat_id, f"✅ Added feed:\n{msg}")
        else:
            reply(chat_id, f"⚠️ {msg}")
        return

    # /removefeed
    if text.startswith("/removefeed"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            reply(chat_id, "Usage: /removefeed <index or url>")
            return
        arg = parts[1].strip()
        ok, msg = remove_feed(arg)
        if ok:
            reply(chat_id, f"❌ Removed feed:\n{msg}")
        else:
            reply(chat_id, f"⚠️ {msg}")
        return

    # Fallback
    reply(chat_id, "Unknown command. Try /feedlist or /addfeed.")


# ---------------- TELEGRAM POLLING LOOP ----------------

def listen_for_commands():
    """Polling loop for command mode."""
    last_update_id = None

    while True:
        try:
            resp = requests.get(TELEGRAM_UPDATES_URL, timeout=20).json()
            if "result" not in resp:
                time.sleep(2)
                continue

            for upd in resp["result"]:
                if last_update_id is None or upd["update_id"] > last_update_id:
                    last_update_id = upd["update_id"]

                    if "message" in upd:
                        handle_bot_commands(upd["message"])

            # Tell Telegram we've processed updates
            requests.get(f"{TELEGRAM_UPDATES_URL}?offset={last_update_id + 1}")

        except Exception as e:
            print("Error polling:", e)

        time.sleep(1)  # avoid spam


# ---------------- MAIN ----------------

def main():
    commands_mode = "--commands-only" in sys.argv

    # Always check commands when invoked
    if commands_mode:
        print("Running in COMMAND mode")
        listen_for_commands()
        return

    # DAILY DIGEST MODE
    if not (OPENAI_API_KEY and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise RuntimeError("Missing environment variables.")

    entries = fetch_new_entries()
    digest = build_daily_digest(entries)
    reply(TELEGRAM_CHAT_ID, digest, html=True)
    set_last_run()


if __name__ == "__main__":
    main()


