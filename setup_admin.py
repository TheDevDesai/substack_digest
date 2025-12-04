#!/usr/bin/env python3
"""
First-time setup: Add yourself as the first admin.

Usage:
    python setup_admin.py <YOUR_TELEGRAM_CHAT_ID>

Example:
    python setup_admin.py 123456789
"""

import json
import sys

ADMIN_FILE = "admins.json"

def main():
    if len(sys.argv) < 2:
        print("Usage: python setup_admin.py <YOUR_TELEGRAM_CHAT_ID>")
        print("\nTo find your chat ID:")
        print("1. Message @userinfobot or @RawDataBot on Telegram")
        print("2. It will reply with your User ID")
        sys.exit(1)
    
    chat_id = sys.argv[1]
    
    if not chat_id.lstrip('-').isdigit():
        print(f"Error: '{chat_id}' doesn't look like a valid chat ID")
        sys.exit(1)
    
    # Load existing admins
    try:
        with open(ADMIN_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"admins": []}
    
    # Add new admin
    if chat_id not in data["admins"]:
        data["admins"].append(chat_id)
        with open(ADMIN_FILE, "w") as f:
            json.dump(data, f, indent=2)
        print(f"✅ Added {chat_id} as admin!")
        print(f"\nCurrent admins: {data['admins']}")
    else:
        print(f"ℹ️ {chat_id} is already an admin")
    
    print("\n📋 Next steps:")
    print("1. Commit and push this change to GitHub")
    print("2. Railway will auto-redeploy")
    print("3. Send /start to your bot - you'll see 'You are an admin'")

if __name__ == "__main__":
    main()
