import os
import json
import re
import requests

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # optional filter
FEEDS_FILE = "feeds.json"
STATE_FILE = "feed_state.json"


def load_feeds():
    try:
        with open(FEEDS_FILE, "r") as f:
            return [str(x).strip() for x in json.load(f) if str(x).strip()]
    except FileNotFoundError:
        return []


def save_feeds(feeds):
    with open(FEEDS_FILE, "w") as f:
        json.dump(sorted(list(set(feeds))), f, indent=2)


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"last_update_id": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    requests.post(url, json=payload, timeout=15)


def normalise_feed_url(text):
    text = text.strip()
    # If user pastes a substack URL like https://something.substack.com/ or /posts
    # try to normalise to its /feed
    if "substack.com" in text and "/feed" not in text:
        text = re.sub(r"/posts/?$", "", text)
        if not text.endswith("/"):
            text += "/"
        text += "feed"
    return text

def list_feeds():
    """Return the current list of feeds."""
    return load_feeds()


def add_feed(url):
    """Add a new feed if it doesn't already exist.

    Returns (added: bool, message_or_url: str)
    """
    feeds = load_feeds()
    url = normalise_feed_url(url)
    if url in feeds:
        return False, "Feed already present."
    feeds.append(url)
    save_feeds(feeds)
    return True, url


def remove_feed(arg):
    """Remove a feed by URL or by 1-based index.

    Returns (removed: bool, message_or_url: str)
    """
    feeds = load_feeds()
    removed = None

    # Allow numeric index: /removefeed 3
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(feeds):
            removed = feeds.pop(idx)
    else:
        url = normalise_feed_url(arg)
        if url in feeds:
            feeds.remove(url)
            removed = url

    if removed:
        save_feeds(feeds)
        return True, removed
    else:
        return False, "Could not find that feed to remove."


def handle_command(chat_id, text, feeds):
    text = text.strip()
    lowered = text.lower()

    if lowered.startswith("/start"):
        send_message(
            chat_id,
            "Hi! Send me a Substack (or RSS) URL with /add to subscribe.\n\n"
            "Commands:\n"
            "/add <url> – add a new feed\n"
            "/feedlist – list current feeds\n"
            "/remove <url or index> – remove a feed\n",
        )
        return feeds, False

    if lowered.startswith("/feedlist"):
        if not feeds:
            send_message(chat_id, "No feeds are configured yet.")
            return feeds, False
        lines = ["Current feeds:"]
        for i, url in enumerate(feeds, start=1):
            lines.append(f"{i}. {url}")
        send_message(chat_id, "\n".join(lines))
        return feeds, False

    if lowered.startswith("/add"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Usage: /add <feed_url>")
            return feeds, False
        url = normalise_feed_url(parts[1])
        if url in feeds:
            send_message(chat_id, f"Feed already present:\n{url}")
            return feeds, False
        feeds.append(url)
        save_feeds(feeds)
        send_message(chat_id, f"✅ Added feed:\n{url}")
        return feeds, True

    if lowered.startswith("/remove"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Usage: /remove <feed_url or index>")
            return feeds, False

        arg = parts[1].strip()
        removed = None

        # Allow numeric index: /remove 3
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(feeds):
                removed = feeds.pop(idx)
        else:
            url = normalise_feed_url(arg)
            if url in feeds:
                feeds.remove(url)
                removed = url

        if removed:
            save_feeds(feeds)
            send_message(chat_id, f"🗑 Removed feed:\n{removed}")
            return feeds, True
        else:
            send_message(chat_id, "Could not find that feed to remove.")
            return feeds, False

    # Also support bare URL: user just pastes a link
    if "http://" in text or "https://" in text:
        url = normalise_feed_url(text)
        if url not in feeds:
            feeds.append(url)
            save_feeds(feeds)
            send_message(
                chat_id,
                f"✅ Added feed (from plain URL):\n{url}\n\n"
                "Use /feedlist to see all feeds.",
            )
            return feeds, True

    # Unknown command; you could send help or ignore
    return feeds, False


def main():
    state = load_state()
    last_update_id = state.get("last_update_id", 0)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 5}
    if last_update_id:
        params["offset"] = last_update_id + 1

    resp = requests.get(url, params=params, timeout=20)
    data = resp.json()
    if not data.get("ok"):
        return

    updates = data.get("result", [])
    if not updates:
        return

    feeds = load_feeds()
    changed = False
    max_update_id = last_update_id

    for update in updates:
        update_id = update["update_id"]
        max_update_id = max(max_update_id, update_id)

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue

        chat_id = msg["chat"]["id"]
        # Optional: ignore other chats if you want
        if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
            continue

        text = msg.get("text", "")
        if not text:
            continue

        feeds, did_change = handle_command(chat_id, text, feeds)
        if did_change:
            changed = True

    state["last_update_id"] = max_update_id
    save_state(state)

    # The GitHub Action will handle committing & pushing FEEDS_FILE/STATE_FILE
    if changed:
        print("Feeds updated.")
    else:
        print("No feed changes.")


if __name__ == "__main__":
    main()

