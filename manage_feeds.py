"""
Feed Management Module for Substack Digest Bot

Handles per-user feed subscriptions with three-tier hierarchy:
- Owner: Full control (add/remove admins, block users, broadcast, etc.)
- Admin: Free Pro access only (no admin commands)
- User: Regular free/paid users
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

STATE_FILE = "user_state.json"
CONFIG_FILE = "bot_config.json"
USERNAME_MAP_FILE = "username_map.json"

# Subscription tiers and limits
TIERS = {
    "free": {
        "max_feeds": 3,
        "digest_frequency": "daily",
        "ai_summaries": False,
        "price_monthly": 0,
    },
    "pro": {
        "max_feeds": 50,
        "digest_frequency": "custom",
        "ai_summaries": True,
        "price_monthly": 1,
    },
}

# Rate limiting settings
RATE_LIMITS = {
    "commands_per_minute": 10,
    "feeds_add_per_hour": 20,
    "digest_requests_per_hour": 5,
}


# -----------------------------
#  Bot Config (Owner & Admins)
# -----------------------------

def load_config() -> dict:
    """Load bot configuration (owner and admins)."""
    if not os.path.exists(CONFIG_FILE):
        return {"owner_id": None, "admins": []}
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"owner_id": None, "admins": []}


def save_config(config: dict) -> None:
    """Save bot configuration."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_owner_id() -> Optional[str]:
    """Get the owner's user ID."""
    config = load_config()
    return config.get("owner_id")


def set_owner_id(user_id: str) -> None:
    """Set the owner's user ID (only if not already set)."""
    config = load_config()
    if not config.get("owner_id"):
        config["owner_id"] = str(user_id)
        save_config(config)


def is_owner(user_id: str) -> bool:
    """Check if user is the owner."""
    owner_id = get_owner_id()
    return owner_id is not None and str(user_id) == str(owner_id)


def is_admin(user_id: str) -> bool:
    """Check if user is an admin (has free Pro access)."""
    config = load_config()
    admins = config.get("admins", [])
    return str(user_id) in [str(a) for a in admins]


def is_privileged(user_id: str) -> bool:
    """Check if user is owner OR admin (has Pro access)."""
    return is_owner(user_id) or is_admin(user_id)


def add_admin(identifier: str) -> tuple[bool, str]:
    """Add a user as admin (free Pro access). Only owner can do this."""
    config = load_config()
    admins = config.get("admins", [])
    
    # Check if it's a username
    if identifier.startswith("@"):
        user_id = get_user_id_by_username(identifier)
        if not user_id:
            return False, f"Username {identifier} not found. They need to message the bot first."
        username = identifier
    else:
        user_id = identifier
        username = get_username_by_user_id(user_id)
    
    user_id = str(user_id)
    
    # Can't add owner as admin
    if is_owner(user_id):
        return False, "Cannot add owner as admin."
    
    if user_id in [str(a) for a in admins]:
        display = f"@{username}" if username else user_id
        return False, f"{display} is already an admin."
    
    admins.append(user_id)
    config["admins"] = admins
    save_config(config)
    
    display = f"@{username}" if username else user_id
    return True, f"{display} now has free Pro access."


def remove_admin(identifier: str) -> tuple[bool, str]:
    """Remove admin status from a user."""
    config = load_config()
    admins = config.get("admins", [])
    
    if identifier.startswith("@"):
        user_id = get_user_id_by_username(identifier)
        if not user_id:
            return False, f"Username {identifier} not found."
        username = identifier
    else:
        user_id = identifier
        username = get_username_by_user_id(user_id)
    
    user_id = str(user_id)
    
    if user_id not in [str(a) for a in admins]:
        display = f"@{username}" if username else user_id
        return False, f"{display} is not an admin."
    
    admins = [a for a in admins if str(a) != user_id]
    config["admins"] = admins
    save_config(config)
    
    display = f"@{username}" if username else user_id
    return True, f"{display} no longer has admin access."


def list_admins() -> list:
    """Get list of all admins with their usernames."""
    config = load_config()
    admin_ids = config.get("admins", [])
    result = []
    
    for admin_id in admin_ids:
        username = get_username_by_user_id(admin_id)
        if username:
            result.append(f"@{username} ({admin_id})")
        else:
            result.append(str(admin_id))
    
    return result


# -----------------------------
#  Username <-> User ID Mapping
# -----------------------------

def load_username_map() -> dict:
    """Load username to user_id mapping."""
    if not os.path.exists(USERNAME_MAP_FILE):
        return {}
    try:
        with open(USERNAME_MAP_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_username_map(mapping: dict) -> None:
    """Save username to user_id mapping."""
    with open(USERNAME_MAP_FILE, "w") as f:
        json.dump(mapping, f, indent=2)


def register_user(user_id: str, username: str = None, first_name: str = None) -> None:
    """Register or update a user's username mapping."""
    if not username:
        return
    
    mapping = load_username_map()
    username_lower = username.lower().lstrip("@")
    
    mapping[username_lower] = {
        "user_id": str(user_id),
        "username": username,
        "first_name": first_name,
        "last_seen": datetime.now(timezone.utc).isoformat()
    }
    
    save_username_map(mapping)


def get_user_id_by_username(username: str) -> Optional[str]:
    """Look up user_id by username."""
    mapping = load_username_map()
    username_lower = username.lower().lstrip("@")
    
    if username_lower in mapping:
        return mapping[username_lower]["user_id"]
    return None


def get_username_by_user_id(user_id: str) -> Optional[str]:
    """Look up username by user_id."""
    mapping = load_username_map()
    user_id = str(user_id)
    
    for username, data in mapping.items():
        if data["user_id"] == user_id:
            return data.get("username", username)
    return None


def get_all_known_users() -> list:
    """Get all users who have interacted with the bot."""
    mapping = load_username_map()
    return [
        {
            "user_id": data["user_id"],
            "username": data.get("username"),
            "first_name": data.get("first_name"),
        }
        for data in mapping.values()
    ]


# -----------------------------
#  State Persistence
# -----------------------------

def load_state() -> dict:
    """Load the entire user state from disk."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_state(state: dict) -> None:
    """Save the entire user state to disk."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def ensure_user(user_id: str) -> dict:
    """Ensure a user exists in state, creating default if needed."""
    state = load_state()
    user_id = str(user_id)
    
    if user_id not in state:
        state[user_id] = {
            "feeds": [],
            "digest_time": "08:00",
            "last_sent_date": None,
            "subscription": {
                "tier": "free",
                "stripe_customer_id": None,
                "stripe_subscription_id": None,
                "expires_at": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            "rate_limits": {
                "command_timestamps": [],
                "feed_add_timestamps": [],
                "digest_request_timestamps": [],
            },
            "security": {
                "blocked": False,
                "block_reason": None,
                "failed_attempts": 0,
            },
        }
        save_state(state)
    
    return state


# -----------------------------
#  Security & Validation
# -----------------------------

def validate_feed_url(url: str) -> tuple[bool, str]:
    """Validate that a URL is a legitimate RSS feed URL."""
    url = url.strip()
    
    url_pattern = re.compile(
        r'^https?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE
    )
    
    if not url_pattern.match(url):
        return False, "Invalid URL format."
    
    blocked_patterns = [
        r'localhost',
        r'127\.0\.0\.1',
        r'192\.168\.',
        r'10\.',
        r'172\.(1[6-9]|2[0-9]|3[0-1])\.',
        r'0\.0\.0\.0',
        r'file://',
        r'ftp://',
    ]
    
    for pattern in blocked_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return False, "URL not allowed for security reasons."
    
    if "substack.com" in url.lower() and not url.endswith("/feed"):
        url = url.rstrip("/") + "/feed"
    
    if any(domain in url.lower() for domain in ["substack.com", "medium.com", "ghost.io"]):
        url = re.sub(r'^http://', 'https://', url)
    
    return True, url


def is_user_blocked(user_id: str) -> tuple[bool, Optional[str]]:
    """Check if a user is blocked."""
    state = ensure_user(user_id)
    security = state[str(user_id)].get("security", {})
    
    if security.get("blocked", False):
        return True, security.get("block_reason", "Account suspended.")
    
    return False, None


def block_user(user_id: str, reason: str) -> None:
    """Block a user from using the bot."""
    state = ensure_user(user_id)
    state[str(user_id)]["security"]["blocked"] = True
    state[str(user_id)]["security"]["block_reason"] = reason
    save_state(state)


def unblock_user(user_id: str) -> None:
    """Unblock a user."""
    state = ensure_user(user_id)
    state[str(user_id)]["security"]["blocked"] = False
    state[str(user_id)]["security"]["block_reason"] = None
    state[str(user_id)]["security"]["failed_attempts"] = 0
    save_state(state)


# -----------------------------
#  Rate Limiting
# -----------------------------

def check_rate_limit(user_id: str, action: str) -> tuple[bool, Optional[str]]:
    """Check if user has exceeded rate limit for an action."""
    # Owner and admins bypass rate limits
    if is_privileged(user_id):
        return True, None
    
    state = ensure_user(user_id)
    user_id = str(user_id)
    now = time.time()
    
    limits_config = {
        "command": ("command_timestamps", 60, RATE_LIMITS["commands_per_minute"]),
        "feed_add": ("feed_add_timestamps", 3600, RATE_LIMITS["feeds_add_per_hour"]),
        "digest_request": ("digest_request_timestamps", 3600, RATE_LIMITS["digest_requests_per_hour"]),
    }
    
    if action not in limits_config:
        return True, None
    
    key, window_seconds, max_requests = limits_config[action]
    rate_limits = state[user_id].get("rate_limits", {})
    timestamps = rate_limits.get(key, [])
    
    timestamps = [ts for ts in timestamps if now - ts < window_seconds]
    
    if len(timestamps) >= max_requests:
        wait_time = int(window_seconds - (now - timestamps[0]))
        return False, f"Rate limit exceeded. Try again in {wait_time} seconds."
    
    timestamps.append(now)
    state[user_id]["rate_limits"][key] = timestamps
    save_state(state)
    
    return True, None


# -----------------------------
#  Subscription Management
# -----------------------------

def get_subscription(user_id: str) -> dict:
    """Get user's subscription details."""
    state = ensure_user(user_id)
    return state[str(user_id)].get("subscription", {"tier": "free"})


def get_tier_limits(user_id: str) -> dict:
    """Get the limits for user's current tier."""
    # Owner and admins always get Pro
    if is_privileged(user_id):
        return TIERS["pro"]
    
    sub = get_subscription(user_id)
    tier = sub.get("tier", "free")
    
    expires_at = sub.get("expires_at")
    if expires_at and tier != "free":
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expiry:
                downgrade_to_free(user_id)
                tier = "free"
        except (ValueError, TypeError):
            pass
    
    return TIERS.get(tier, TIERS["free"])


def is_subscription_active(user_id: str) -> bool:
    """Check if user has an active paid subscription."""
    # Owner and admins are always active
    if is_privileged(user_id):
        return True
    
    sub = get_subscription(user_id)
    tier = sub.get("tier", "free")
    
    if tier == "free":
        return True
    
    expires_at = sub.get("expires_at")
    if not expires_at:
        return False
    
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < expiry
    except (ValueError, TypeError):
        return False


def upgrade_subscription(
    user_id: str,
    tier: str,
    stripe_customer_id: str,
    stripe_subscription_id: str,
    expires_at: str,
) -> bool:
    """Upgrade user's subscription."""
    if tier not in TIERS:
        return False
    
    state = ensure_user(user_id)
    user_id = str(user_id)
    
    state[user_id]["subscription"] = {
        "tier": tier,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "expires_at": expires_at,
        "created_at": state[user_id]["subscription"].get(
            "created_at", datetime.now(timezone.utc).isoformat()
        ),
    }
    save_state(state)
    return True


def downgrade_to_free(user_id: str) -> None:
    """Downgrade user to free tier."""
    # Don't downgrade owner or admins
    if is_privileged(user_id):
        return
    
    state = ensure_user(user_id)
    user_id = str(user_id)
    
    state[user_id]["subscription"]["tier"] = "free"
    state[user_id]["subscription"]["expires_at"] = None
    state[user_id]["subscription"]["stripe_subscription_id"] = None
    save_state(state)
    
    max_feeds = TIERS["free"]["max_feeds"]
    if len(state[user_id]["feeds"]) > max_feeds:
        state[user_id]["feeds"] = state[user_id]["feeds"][:max_feeds]
        save_state(state)


def get_stripe_customer_id(user_id: str) -> Optional[str]:
    """Get user's Stripe customer ID if exists."""
    sub = get_subscription(user_id)
    return sub.get("stripe_customer_id")


def set_stripe_customer_id(user_id: str, customer_id: str) -> None:
    """Set user's Stripe customer ID."""
    state = ensure_user(user_id)
    state[str(user_id)]["subscription"]["stripe_customer_id"] = customer_id
    save_state(state)


# -----------------------------
#  Feed Management
# -----------------------------

def list_feeds(user_id: str) -> list:
    """Get list of feeds for a user."""
    state = ensure_user(user_id)
    return state[str(user_id)]["feeds"]


def add_feed(user_id: str, url: str) -> tuple[bool, str]:
    """Add a feed URL for a user."""
    # Check rate limit (owner/admins bypass)
    if not is_privileged(user_id):
        allowed, error = check_rate_limit(user_id, "feed_add")
        if not allowed:
            return False, error
    
    valid, result = validate_feed_url(url)
    if not valid:
        return False, result
    
    url = result
    
    state = ensure_user(user_id)
    user_id = str(user_id)
    
    tier_limits = get_tier_limits(user_id)
    max_feeds = tier_limits["max_feeds"]
    
    if len(state[user_id]["feeds"]) >= max_feeds:
        sub = get_subscription(user_id)
        tier = sub.get("tier", "free")
        if tier == "free" and not is_privileged(user_id):
            return False, f"Feed limit reached ({max_feeds} for free tier). Upgrade to Pro! /upgrade"
        else:
            return False, f"Feed limit reached ({max_feeds})."
    
    if url in state[user_id]["feeds"]:
        return False, "Feed already added."
    
    state[user_id]["feeds"].append(url)
    save_state(state)
    return True, url


def remove_feed(user_id: str, url_or_index: str) -> tuple[bool, str]:
    """Remove a feed by URL or 1-based index."""
    state = ensure_user(user_id)
    user_id = str(user_id)
    feeds = state[user_id]["feeds"]
    
    if url_or_index.isdigit():
        idx = int(url_or_index) - 1
        if 0 <= idx < len(feeds):
            removed = feeds.pop(idx)
            save_state(state)
            return True, removed
        return False, "Invalid index."
    
    if url_or_index in feeds:
        feeds.remove(url_or_index)
        save_state(state)
        return True, url_or_index
    
    return False, "Feed not found."


# -----------------------------
#  Digest Time Settings
# -----------------------------

def set_digest_time(user_id: str, time_str: str) -> bool:
    """Set preferred digest time (HH:MM format)."""
    if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
        return False
    
    state = ensure_user(user_id)
    state[str(user_id)]["digest_time"] = time_str
    save_state(state)
    return True


def get_digest_time(user_id: str) -> str:
    """Get preferred digest time for a user."""
    state = ensure_user(user_id)
    return state[str(user_id)]["digest_time"]


def get_last_sent_date(user_id: str) -> Optional[str]:
    """Get the last date a digest was sent to this user."""
    state = ensure_user(user_id)
    return state[str(user_id)].get("last_sent_date")


def set_last_sent_date(user_id: str, date_str: str) -> None:
    """Record when digest was last sent to this user."""
    state = ensure_user(user_id)
    state[str(user_id)]["last_sent_date"] = date_str
    save_state(state)


# -----------------------------
#  Utility Functions
# -----------------------------

def get_all_users() -> list:
    """Get list of all user IDs in the system."""
    state = load_state()
    return list(state.keys())


def get_all_unique_feeds() -> list:
    """Get deduplicated list of all feeds across all users."""
    state = load_state()
    all_feeds = set()
    for user_data in state.values():
        all_feeds.update(user_data.get("feeds", []))
    return list(all_feeds)


def get_user_stats(user_id: str) -> dict:
    """Get statistics for a user."""
    state = ensure_user(user_id)
    user = state[str(user_id)]
    sub = user.get("subscription", {})
    
    return {
        "feed_count": len(user["feeds"]),
        "tier": sub.get("tier", "free"),
        "is_owner": is_owner(user_id),
        "is_admin": is_admin(user_id),
        "is_privileged": is_privileged(user_id),
        "tier_limits": get_tier_limits(user_id),
        "subscription_active": is_subscription_active(user_id),
        "expires_at": sub.get("expires_at"),
    }


def get_all_stats() -> dict:
    """Get overall bot statistics (for owner)."""
    state = load_state()
    config = load_config()
    
    total_users = len(state)
    total_feeds = sum(len(u.get("feeds", [])) for u in state.values())
    pro_users = sum(1 for u in state.values() if u.get("subscription", {}).get("tier") == "pro")
    free_users = total_users - pro_users
    admin_count = len(config.get("admins", []))
    
    return {
        "total_users": total_users,
        "total_feeds": total_feeds,
        "pro_users": pro_users,
        "free_users": free_users,
        "admin_count": admin_count,
    }
