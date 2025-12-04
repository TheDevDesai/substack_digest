"""
Feed Management Module for Substack Digest Bot

Handles per-user feed subscriptions, subscription tiers, and security.
"""

import json
import os
import re
import hashlib
import time
from datetime import datetime, timezone
from typing import Optional

STATE_FILE = "user_state.json"

# Subscription tiers and limits
TIERS = {
    "free": {
        "max_feeds": 3,
        "digest_frequency": "daily",
        "ai_summaries": False,
        "price_monthly": 0,
    },
    "basic": {
        "max_feeds": 15,
        "digest_frequency": "daily",
        "ai_summaries": True,
        "price_monthly": 5,  # $5/month
    },
    "pro": {
        "max_feeds": 50,
        "digest_frequency": "custom",
        "ai_summaries": True,
        "price_monthly": 12,  # $12/month
    },
}

# Rate limiting settings
RATE_LIMITS = {
    "commands_per_minute": 10,
    "feeds_add_per_hour": 20,
    "digest_requests_per_hour": 5,
}


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
    """
    Validate that a URL is a legitimate RSS feed URL.
    
    Returns:
        (is_valid, error_message or cleaned_url)
    """
    url = url.strip()
    
    # Basic URL pattern
    url_pattern = re.compile(
        r'^https?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain
        r'localhost|'  # localhost
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # or IP
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE
    )
    
    if not url_pattern.match(url):
        return False, "Invalid URL format."
    
    # Block potentially dangerous URLs
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
    
    # Normalize Substack URLs
    if "substack.com" in url.lower() and not url.endswith("/feed"):
        url = url.rstrip("/") + "/feed"
    
    # Ensure HTTPS for known platforms
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
    """
    Check if user has exceeded rate limit for an action.
    
    Actions: 'command', 'feed_add', 'digest_request'
    
    Returns:
        (is_allowed, error_message if not allowed)
    """
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
    
    # Filter to only timestamps within the window
    timestamps = [ts for ts in timestamps if now - ts < window_seconds]
    
    if len(timestamps) >= max_requests:
        wait_time = int(window_seconds - (now - timestamps[0]))
        return False, f"Rate limit exceeded. Try again in {wait_time} seconds."
    
    # Record this request
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
    sub = get_subscription(user_id)
    tier = sub.get("tier", "free")
    
    # Check if subscription is expired
    expires_at = sub.get("expires_at")
    if expires_at and tier != "free":
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expiry:
                # Subscription expired, downgrade to free
                downgrade_to_free(user_id)
                tier = "free"
        except (ValueError, TypeError):
            pass
    
    return TIERS.get(tier, TIERS["free"])


def is_subscription_active(user_id: str) -> bool:
    """Check if user has an active paid subscription."""
    sub = get_subscription(user_id)
    tier = sub.get("tier", "free")
    
    if tier == "free":
        return True  # Free is always "active"
    
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
    """Upgrade user's subscription (called from Stripe webhook)."""
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
    state = ensure_user(user_id)
    user_id = str(user_id)
    
    state[user_id]["subscription"]["tier"] = "free"
    state[user_id]["subscription"]["expires_at"] = None
    state[user_id]["subscription"]["stripe_subscription_id"] = None
    save_state(state)
    
    # Trim feeds to free tier limit
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
    """
    Add a feed URL for a user.
    
    Returns:
        (success, message) tuple
    """
    # Check rate limit
    allowed, error = check_rate_limit(user_id, "feed_add")
    if not allowed:
        return False, error
    
    # Validate URL
    valid, result = validate_feed_url(url)
    if not valid:
        return False, result
    
    url = result  # Use the cleaned/normalized URL
    
    state = ensure_user(user_id)
    user_id = str(user_id)
    
    # Check tier limits
    tier_limits = get_tier_limits(user_id)
    max_feeds = tier_limits["max_feeds"]
    
    if len(state[user_id]["feeds"]) >= max_feeds:
        tier = get_subscription(user_id).get("tier", "free")
        return False, f"Feed limit reached ({max_feeds} for {tier} tier). Upgrade for more!"
    
    if url in state[user_id]["feeds"]:
        return False, "Feed already added."
    
    state[user_id]["feeds"].append(url)
    save_state(state)
    return True, url


def remove_feed(user_id: str, url_or_index: str) -> tuple[bool, str]:
    """
    Remove a feed by URL or 1-based index.
    
    Returns:
        (success, message) tuple
    """
    state = ensure_user(user_id)
    user_id = str(user_id)
    feeds = state[user_id]["feeds"]
    
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
#  Digest Time Settings
# -----------------------------

def set_digest_time(user_id: str, time_str: str) -> bool:
    """Set preferred digest time (HH:MM format)."""
    # Validate time format
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
        "tier_limits": get_tier_limits(user_id),
        "subscription_active": is_subscription_active(user_id),
        "expires_at": sub.get("expires_at"),
    }
