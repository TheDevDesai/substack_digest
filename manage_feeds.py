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

import json
import os

STATE_FILE = "user_state.json"

# -----------------------------
#  Load & Save State
# -----------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# -----------------------------
#  Helper: Ensure user exists
# -----------------------------

def ensure_user(user_id):
    state = load_state()
    if str(user_id) not in state:
        state[str(user_id)] = {
            "feeds": [],
            "digest_time": "08:00",
            "last_sent_date": ""
        }
        save_state(state)
    return state

# -----------------------------
#  Feed Management
# -----------------------------

def list_feeds(user_id):
    state = ensure_user(user_id)
    return state[str(user_id)]["feeds"]

def add_feed(user_id, url):
    state = ensure_user(user_id)
    url = url.strip()

    if url in state[str(user_id)]["feeds"]:
        return False, "Feed already added."

    state[str(user_id)]["feeds"].append(url)
    save_state(state)
    return True, url

def remove_feed(user_id, url_or_index):
    state = ensure_user(user_id)
    feeds = state[str(user_id)]["feeds"]

    # Remove by index (1-based)
    if url_or_index.isdigit():
        idx = int(url_or_index) - 1
        if 0 <= idx < len(feeds):
            removed = feeds.pop(idx)
            save_state(state)
            return True, removed
        return False, "Invalid index."

    # Remove by URL
    if url_or_index in feeds:
        feeds.remove(url_or_index)
        save_state(state)
        return True, url_or_index

    return False, "Feed not found."

# -----------------------------
#  Digest Time
# -----------------------------

def set_digest_time(user_id, time_str):
    """time_str format: HH:MM"""
    state = ensure_user(user_id)
    state[str(user_id)]["digest_time"] = time_str
    save_state(state)
    return True

def get_digest_time(user_id):
    state = ensure_user(user_id)
    return state[str(user_id)]["digest_time"]

