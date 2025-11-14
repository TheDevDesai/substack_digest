# 📬 Substack Digest + Telegram Bot

This repository contains a GitHub Actions-powered bot that:

### ✅ Fetches the newest posts from your selected RSS/Substack feeds  
### ✅ Builds a clean daily digest  
### ✅ Sends the digest to a Telegram chat  
### ✅ Supports Telegram commands:
- `/feedlist` — Show the current feed list  
- `/addfeed <url>` — Add a new RSS/Substack feed  
- `/removefeed <url or index>` — Remove a feed  
- `/dailydigest` — Manually trigger a digest  

All commands run in **commands-only mode**, while the digest is sent **once per day** at 08:00 SGT (00:00 UTC).

---

## 🚀 Features

- RSS feed parsing via `feedparser`
- Daily digest with summaries, sources, timestamps
- Telegram Bot API integration
- GitHub Action automation
- Automatic feed management (`feeds.json`)
- Prevents duplicate runs via `concurrency` safeguards

---

## 📅 Automation Schedule

The workflow triggers:

- **Daily at 08:00 SGT (00:00 UTC)** — sends digest  
- **Manual trigger** — runs commands mode  
  (`/feedlist`, `/addfeed`, `/removefeed`, `/dailydigest`)

---

## 📦 GitHub Actions Workflow

The workflow is stored here:

