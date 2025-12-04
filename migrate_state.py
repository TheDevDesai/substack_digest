#!/usr/bin/env python3
"""
Migration script to convert old user_state.json to new format.
Run this once after updating the codebase.

Usage:
    python migrate_state.py <YOUR_TELEGRAM_CHAT_ID>
    
Example:
    python migrate_state.py 123456789
"""

import json
import sys
from datetime import datetime, timezone

OLD_STATE_FILE = "user_state.json"
BACKUP_FILE = "user_state.backup.json"


def migrate(chat_id: str):
    """Migrate old state format to new format."""
    
    # Load existing state
    try:
        with open(OLD_STATE_FILE, "r") as f:
            old_state = json.load(f)
    except FileNotFoundError:
        print("No existing user_state.json found. Creating fresh state.")
        old_state = {}
    
    # Backup old state
    with open(BACKUP_FILE, "w") as f:
        json.dump(old_state, f, indent=2)
    print(f"✅ Backed up old state to {BACKUP_FILE}")
    
    # Find feeds from old format
    feeds = []
    old_digest_time = "08:00"
    
    # Check for placeholder key
    if "YOUR_CHAT_ID_HERE" in old_state:
        old_data = old_state["YOUR_CHAT_ID_HERE"]
        feeds = old_data.get("feeds", [])
        old_digest_time = old_data.get("digest_time", "08:00")
        print(f"📋 Found {len(feeds)} feeds from placeholder user")
    
    # Check if chat_id already exists
    if chat_id in old_state:
        existing = old_state[chat_id]
        feeds = existing.get("feeds", feeds)
        old_digest_time = existing.get("digest_time", old_digest_time)
        print(f"📋 Found existing data for chat ID {chat_id}")
    
    # Create new state structure
    new_state = {
        chat_id: {
            "feeds": feeds,
            "digest_time": old_digest_time,
            "last_sent_date": None,
            "subscription": {
                "tier": "pro",  # Give yourself pro tier!
                "stripe_customer_id": None,
                "stripe_subscription_id": None,
                "expires_at": None,  # None = never expires for owner
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
    }
    
    # Save new state
    with open(OLD_STATE_FILE, "w") as f:
        json.dump(new_state, f, indent=2)
    
    print(f"✅ Migrated state for chat ID: {chat_id}")
    print(f"   - Feeds: {len(feeds)}")
    print(f"   - Digest time: {old_digest_time}")
    print(f"   - Tier: pro (owner)")
    print(f"\n📄 New state saved to {OLD_STATE_FILE}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python migrate_state.py <YOUR_TELEGRAM_CHAT_ID>")
        print("\nTo find your chat ID:")
        print("1. Send a message to your bot")
        print("2. Visit: https://api.telegram.org/bot<TOKEN>/getUpdates")
        print("3. Find 'chat':{'id': XXXXXXX}")
        sys.exit(1)
    
    chat_id = sys.argv[1]
    
    if not chat_id.lstrip('-').isdigit():
        print(f"Error: '{chat_id}' doesn't look like a valid chat ID")
        print("Chat IDs are numbers (can be negative for groups)")
        sys.exit(1)
    
    migrate(chat_id)


if __name__ == "__main__":
    main()
