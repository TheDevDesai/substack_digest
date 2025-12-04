#!/bin/bash
# Substack Digest Bot - Quick Setup Script
# Run this in your terminal after downloading the zip file

set -e

echo "🚀 Substack Digest Bot Setup"
echo "============================"

# Check if git is available
if ! command -v git &> /dev/null; then
    echo "❌ Git is not installed. Please install git first."
    exit 1
fi

# Configuration
REPO_URL="https://github.com/TheDevDesai/substack_digest.git"
REPO_DIR="substack_digest"
ZIP_FILE="substack_digest_cleaned.zip"

# Check if zip file exists in current directory or Downloads
if [ -f "$ZIP_FILE" ]; then
    echo "✅ Found $ZIP_FILE in current directory"
elif [ -f "$HOME/Downloads/$ZIP_FILE" ]; then
    ZIP_FILE="$HOME/Downloads/$ZIP_FILE"
    echo "✅ Found $ZIP_FILE in Downloads"
else
    echo "❌ Cannot find $ZIP_FILE"
    echo "   Please download it and place it in current directory or Downloads"
    exit 1
fi

# Clone or update repo
if [ -d "$REPO_DIR" ]; then
    echo "📁 Found existing $REPO_DIR folder"
    cd "$REPO_DIR"
    git pull origin main || true
else
    echo "📥 Cloning repository..."
    git clone "$REPO_URL"
    cd "$REPO_DIR"
fi

# Remove old files (keep .git)
echo "🧹 Cleaning old files..."
find . -maxdepth 1 -type f -delete 2>/dev/null || true
rm -rf .github __pycache__ 2>/dev/null || true

# Extract new files
echo "📦 Extracting new files..."
unzip -o "$ZIP_FILE" -d .
# Handle if files are in a subdirectory
if [ -d "substack_digest_clean" ]; then
    mv substack_digest_clean/* . 2>/dev/null || true
    rm -rf substack_digest_clean
fi

# Git add and commit
echo "📝 Committing changes..."
git add -A
git commit -m "Refactor: Add AI SCQR summaries, security, and Stripe subscriptions" || echo "No changes to commit"

# Push
echo "⬆️  Pushing to GitHub..."
git push origin main

echo ""
echo "✅ Done! Code pushed to GitHub."
echo ""
echo "📋 Next steps:"
echo "   1. Go to: https://github.com/TheDevDesai/substack_digest/settings/secrets/actions"
echo "   2. Add these secrets:"
echo "      - TELEGRAM_BOT_TOKEN"
echo "      - TELEGRAM_CHAT_ID"  
echo "      - OPENAI_API_KEY"
echo ""
echo "   3. Then run: python migrate_state.py YOUR_CHAT_ID"
echo "   4. Commit and push user_state.json"
echo ""
echo "🤖 Test by running the workflow manually in GitHub Actions!"
