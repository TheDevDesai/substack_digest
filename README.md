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
.github/workflows/substack-digest.yml


It automatically:

1. Installs dependencies  
2. Decides if it's running in **digest** or **commands** mode  
3. Runs the correct Python entrypoint  

---

## 🧩 Project Structure

📁 substack_digest
│
├── substack_to_telegram.py # Main bot logic
├── manage_feeds.py # Add/remove/list feed management
├── feeds.json # Your dynamic feed list
├── feed_state.json # Tracks last-run timestamp
│
📁 .github/workflows
│ └── substack-digest.yml # GitHub Actions workflow
│
├── README.md
└── LICENSE


---

## 🔧 Requirements

This project uses:

- Python 3.11
- `feedparser`
- `requests`
- `python-dateutil`
- `openai` (optional for summarization)

---

## 🔐 Required Secrets

Configure these in:



Settings → Secrets & Variables → Actions


| Secret | Description |
|--------|-------------|
| `OPENAI_API_KEY` | Used for summaries (optional) |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Chat/group ID where digest is sent |

---

## 📌 Usage in Telegram

### Show feeds


/feedlist


### Add feed


/addfeed https://example.substack.com/feed


### Remove feed


/removefeed 3


### Request digest manually


/dailydigest


---

## 📝 License

MIT License — see `LICENSE` for full text.

---

## 🤝 Contributing

PRs welcome!  
You may fork and customize it for any personal or research use.

---
