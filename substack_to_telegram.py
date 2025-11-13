import os
import html
import json
import datetime as dt
from dateutil import parser as date_parser
from datetime import timezone

import feedparser
import requests
from openai import OpenAI

from manage_feeds import load_feeds, add_feed, remove_feed, list_feeds

# ---------- CONFIG ----------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # your user chat id
LAST_RUN_FILE = "last_run.txt"

client = OpenAI(api_key=OPENAI_API_KEY)

TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
TELEGRAM_UPDATES_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"


# ---------- TELEGRAM HELPERS & COMMANDS ----------

def reply(chat_id: int, text: str) -> None:
    """Send a simple text reply to a Telegram chat."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        requests.post(TELEGRAM_SEND_URL, json=payload, timeout=15)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")


def handle_bot_commands(message: dict) -> None:
    """Handle /addfeed, /removefeed, /feedlist commands."""
    chat_id = message["chat"]["id"]
    # (optional) only respond to your own chat
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        return

    text = message.get("text", "") or ""
    text = text.strip()

    if not text.startswith("/"):
        # not a command – ignore
        return

    if text.startswith("/start"):
        reply(
            chat_id,
            "Hi! I can manage your Substack feeds.\n\n"
            "Commands:\n"
            "/addfeed <url> – add a new feed\n"
            "/removefeed <url or index> – remove a feed\n"
            "/feedlist – list current feeds",
        )
        return

    if text.startswith("/feedlist"):
        feeds = list_feeds()
        if not feeds:
            reply(chat_id, "No feeds configured yet. Use /addfeed <url> to add one.")
            return
        lines = ["📚 <b>Current feeds</b>:"]
        for i, url in enumerate(feeds, start=1):
            lines.append(f"{i}. {url}")
        reply(chat_id, "\n".join(lines))
        return

if text.startswith("/dailydigest"):
    entries = fetch_new_entries()
    digest = build_daily_digest(entries)
    reply(chat_id, digest[:3500])  # send first chunk
    # If digest is long, send rest:
    if len(digest) > 3500:
        reply(chat_id, digest[3500:])
    return

    if text.startswith("/addfeed"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            reply(chat_id, "Usage: /addfeed <feed_url>")
            return
        url = parts[1].strip()
        added, msg = add_feed(url)
        if added:
            reply(chat_id, f"✅ Added feed:\n{msg}")
        else:
            reply(chat_id, f"ℹ️ {msg}")
        return

    if text.startswith("/removefeed"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            reply(chat_id, "Usage: /removefeed <feed_url or index>")
            return
        arg = parts[1].strip()
        removed, msg = remove_feed(arg)
        if removed:
            reply(chat_id, f"🗑 Removed feed:\n{msg}")
        else:
            reply(chat_id, f"⚠️ {msg}")
        return

    # Unknown command
    reply(chat_id, "Unknown command. Try /feedlist, /addfeed, or /removefeed.")


def listen_for_commands() -> None:
    """Poll Telegram for updates and process commands."""
    try:
        resp = requests.get(TELEGRAM_UPDATES_URL, timeout=20).json()
    except Exception as e:
        print(f"Error fetching Telegram updates: {e}")
        return

    if not resp.get("ok"):
        print("Telegram getUpdates not ok:", resp)
        return

    updates = resp.get("result", [])
    if not updates:
        return

    for update in updates:
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue
        if "text" not in message:
            continue
        handle_bot_commands(message)

    # Acknowledge updates so we don't re-process them
    last_id = updates[-1]["update_id"] + 1
    try:
        requests.get(
            TELEGRAM_UPDATES_URL,
            params={"offset": last_id},
            timeout=10,
        )
    except Exception:
        pass


# ---------- LAST RUN TIMESTAMP ----------

def get_last_run() -> dt.datetime:
    """Read last run time from file, default to 24h ago."""
    try:
        with open(LAST_RUN_FILE, "r") as f:
            ts = f.read().strip()
            if not ts:
                raise ValueError("empty last_run")
            dt_obj = dt.datetime.fromisoformat(ts)
    except Exception:
        # default: 24 hours ago
        dt_obj = dt.datetime.now(timezone.utc) - dt.timedelta(days=1)

    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=timezone.utc)
    return dt_obj


def set_last_run() -> None:
    """Store current time as last run."""
    now = dt.datetime.now(timezone.utc)
    with open(LAST_RUN_FILE, "w") as f:
        f.write(now.isoformat())


# ---------- FETCH SUBSTACK POSTS ----------

def fetch_new_entries() -> list[dict]:
    cutoff = get_last_run()
    all_entries: list[dict] = []

    feeds = load_feeds()
    print(f"Using {len(feeds)} feeds")
    for feed_url in feeds:
        print(f"Fetching feed: {feed_url}")
        parsed = feedparser.parse(feed_url)

        for entry in parsed.entries:
            # Parse published/updated time
            if hasattr(entry, "published"):
                published = date_parser.parse(entry.published)
            elif hasattr(entry, "updated"):
                published = date_parser.parse(entry.updated)
            else:
                continue

            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)

            if published <= cutoff:
                continue

            content = ""
            if "content" in entry and entry.content:
                content = entry.content[0].get("value", "")
            summary = getattr(entry, "summary", "")

            all_entries.append(
                {
                    "title": entry.title,
                    "link": entry.link,
                    "published": published,
                    "source": feed_url,
                    "content": content,
                    "summary": summary,
                }
            )

    # newest first
    all_entries.sort(key=lambda x: x["published"], reverse=True)
    return all_entries


# ---------- SUMMARISATION WITH SCQR ----------

def summarise_article(article: dict) -> str:
    """Use ChatGPT to summarise one article in SCQR + timeline format."""
    title = article["title"]
    link = article["link"]
    raw = article["content"] or article["summary"] or ""
    raw = raw[:8000]  # keep prompt manageable

    system_msg = (
        "You are a research expert creating concise but rich summaries of Substack "
        "articles for a busy reader. Use Barbara Minto's Pyramid Principle and SCQR "
        "(Situation, Complication, Question, Resolution). Where relevant, highlight "
        "historical context and a timeline of key events or milestones."
        "For upcoming trends, analyse and determine what are the current prevalent issues that need to be resolved for another economic, technological or society boom within that industry or sector."
        "For any technical jargon, make sure to provide definitions or simple explanations to ensure the reader understands what is being discussed. Every point should have evidence to back it up."
    )

    user_msg = f"""
Summarise the following article in this structured format:

1. <b>Title</b>: {title}
2. <b>Situation (S)</b>: 2–3 sentences.
3. <b>Complication (C)</b>: 2–3 sentences. What tension/problem/shift?
4. <b>Question (Q)</b>: 1–2 sentences. What question is the article implicitly answering?
5. <b>Resolution (R)</b>: 3–5 sentences. The core argument, answer, or takeaway.
6. <b>Discussion / Implications</b>: 3–6 bullet points.
7. <b>Timeline</b>: bullet list of dated or logical steps if possible.

Article link: {link}

Article content (HTML, may be truncated):
\"\"\"{raw}\"\"\"
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.4,
    )

    return response.choices[0].message.content.strip()


def build_daily_digest(entries: list[dict]) -> str:
    if not entries:
        return "📚 Substack Daily Digest\n\nNo new articles since last run."

    lines = ["📚 <b>Substack Daily Digest</b>", ""]

    for i, article in enumerate(entries, start=1):
        pub_str = article["published"].astimezone(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        try:
            summary = summarise_article(article)
        except Exception as e:
            summary = f"(Error summarising article: {e})"

        lines.append(
            f"\n======================\n"
            f"<b>Article {i}: <a href=\"{article['link']}\">{article['title']}</a></b>\n"
            f"Source: {article['source']}\n"
            f"Published: {pub_str}\n\n"
            f"{summary}"
        )

    return "\n".join(lines)


# ---------- SEND TO TELEGRAM (CHUNKED) ----------

def send_long_message(text: str, chunk_size: int = 3500) -> None:
    """Split long digest into multiple Telegram messages."""
    paragraphs = text.split("\n\n")
    current = ""

    def send_chunk(chunk: str) -> None:
        if not chunk.strip():
            return
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
        }
        try:
            requests.post(TELEGRAM_SEND_URL, json=payload, timeout=20)
        except Exception as e:
            print(f"Error sending chunk: {e}")

    for p in paragraphs:
        if len(current) + len(p) + 2 <= chunk_size:
            current = p if not current else current + "\n\n" + p
        else:
            send_chunk(current)
            current = p

    if current:
        send_chunk(current)


# ----------- MAIN -----------
import sys

def main():
    commands_only = "--commands-only" in sys.argv

    # Always check Telegram for commands
    listen_for_commands()

    if commands_only:
        # Only process /feedlist, /addfeed, /removefeed, /dailydigest
        print("Processed commands only.")
        return

    # Daily digest workflow:
    entries = fetch_new_entries()
    digest = build_daily_digest(entries)
    send_long_message(digest)
    set_last_run()


if __name__ == "__main__":
    main()

