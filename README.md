# 📬 Substack Digest Bot

A Telegram bot that aggregates your favorite Substack newsletters and sends you daily digests with **AI-powered SCQR summaries**. Features subscription tiers, security measures, and runs on GitHub Actions.

## ✨ Features

- **Daily Digests**: Automated summary of new posts at your preferred time
- **AI SCQR Summaries**: Situation-Complication-Question-Resolution analysis (paid tiers)
- **Per-User Feeds**: Each user manages their own subscriptions
- **Subscription Tiers**: Free, Basic ($5/mo), and Pro ($12/mo) plans
- **Security**: Rate limiting, URL validation, user blocking
- **Stripe Integration**: Webhook-based subscription payments
- **Serverless**: Runs on GitHub Actions, no hosting needed

## 🎯 SCQR Summary Format

The bot uses the SCQR framework to analyze articles:

- **S (Situation)**: What is the current context?
- **C (Complication)**: What problem or challenge exists?
- **Q (Question)**: What key question does this raise?
- **R (Resolution)**: What insight or answer does the article provide?

Example output:
```
📚 Daily Digest — 3 new post(s)
━━━━━━━━━━━━━━━━━━━━

1. The Future of AI in Healthcare
📰 Tech Insider • Nov 21, 14:30
🔗 https://example.substack.com/p/ai-healthcare

📋 SCQR Summary:
S: AI adoption in healthcare has accelerated post-pandemic
C: Regulatory hurdles and data privacy concerns slow implementation
Q: How can healthcare providers safely integrate AI while maintaining patient trust?
R: A hybrid approach combining AI efficiency with human oversight shows the most promise
```

## 💰 Subscription Tiers

| Feature | Free | Basic ($5/mo) | Pro ($12/mo) |
|---------|------|---------------|--------------|
| Max Feeds | 3 | 15 | 50 |
| AI Summaries | ❌ | ✅ | ✅ |
| Digest Frequency | Daily | Daily | Custom |

## 🚀 Quick Start

### 1. Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Save the **Bot Token**

### 2. Get Your Chat ID

1. Start a chat with your bot
2. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find your `chat.id` in the response

### 3. Configure GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Required | Description |
|--------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Your Telegram chat ID |
| `OPENAI_API_KEY` | For AI | OpenAI API key for summaries |
| `STRIPE_SECRET_KEY` | For payments | Stripe secret key |
| `STRIPE_WEBHOOK_SECRET` | For payments | Stripe webhook signing secret |

### 4. Enable GitHub Actions

Go to the **Actions** tab and enable workflows.

## 📱 Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and help |
| `/feedlist` | Show your subscribed feeds |
| `/addfeed <url>` | Add a new Substack feed |
| `/removefeed <# or url>` | Remove a feed |
| `/digest` | Get your digest now |
| `/status` | View subscription status |
| `/upgrade [tier]` | Upgrade your plan |
| `/manage` | Manage billing (Stripe) |

## 🔒 Security Features

### URL Validation
- Blocks internal/private IPs (localhost, 192.168.x.x, etc.)
- Enforces HTTPS for known platforms
- Auto-appends `/feed` to Substack URLs

### Rate Limiting
- 10 commands per minute
- 20 feed additions per hour
- 5 digest requests per hour

### User Management
- Block/unblock users
- Failed attempt tracking
- Subscription expiry handling

## 💳 Setting Up Payments (Stripe)

### 1. Create Stripe Products

In your Stripe Dashboard, create two products:
- **Basic Plan**: $5/month subscription
- **Pro Plan**: $12/month subscription

### 2. Get Price IDs

Set these as environment variables:
```
STRIPE_PRICE_BASIC=price_xxxxx
STRIPE_PRICE_PRO=price_xxxxx
```

### 3. Deploy Webhook Handler

Deploy `stripe_webhook.py` as a web service:

**Option A: Vercel**
```bash
vercel deploy
```

**Option B: Railway**
```bash
railway up
```

**Option C: AWS Lambda**
Use the included `lambda_handler` function.

### 4. Configure Webhook in Stripe

1. Go to Stripe Dashboard → Webhooks
2. Add endpoint: `https://your-app.com/webhook/stripe`
3. Select events:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
4. Copy the signing secret to `STRIPE_WEBHOOK_SECRET`

## 📁 Project Structure

```
├── substack_to_telegram.py   # Main bot logic
├── manage_feeds.py           # Feed & subscription management
├── ai_summarizer.py          # OpenAI SCQR summary generation
├── stripe_webhook.py         # Payment webhook handler
├── user_state.json           # User data storage
├── requirements.txt          # Python dependencies
├── .github/
│   └── workflows/
│       └── substack-digest.yml
└── README.md
```

## ⚙️ Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Default chat for digests |
| `OPENAI_API_KEY` | OpenAI API key |
| `STRIPE_SECRET_KEY` | Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | Webhook signing secret |
| `STRIPE_PRICE_BASIC` | Basic tier price ID |
| `STRIPE_PRICE_PRO` | Pro tier price ID |
| `WEBHOOK_BASE_URL` | Your webhook server URL |

### Customizing Digest Time

Edit `.github/workflows/substack-digest.yml`:

```yaml
schedule:
  - cron: "0 0 * * *"  # 00:00 UTC
```

## 🛠️ Development

### Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
export OPENAI_API_KEY="your_openai_key"

# Run digest mode
python substack_to_telegram.py

# Run command mode
python substack_to_telegram.py --commands --duration=60
```

### Running Webhook Server Locally

```bash
# Install Flask
pip install flask

# Run webhook server
python stripe_webhook.py

# Use ngrok for testing
ngrok http 8080
```

## 📊 API Cost Estimates

Using `gpt-4o-mini` for summaries:
- ~800 input tokens per article
- ~200 output tokens per summary
- **Cost: ~$0.0002 per article** (~$0.002 for 10 articles)

## 🔧 Troubleshooting

**Bot not responding?**
- Check `TELEGRAM_BOT_TOKEN` is correct
- Ensure GitHub Actions are enabled
- Check Actions logs for errors

**No AI summaries?**
- Verify `OPENAI_API_KEY` is set
- Check user is on paid tier (`/status`)
- Check OpenAI API credits

**Payments not working?**
- Verify webhook URL is accessible
- Check Stripe webhook logs
- Ensure signing secret matches

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

## 🤝 Contributing

Pull requests welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests if applicable
4. Submit a PR with description

---

Made with ❤️ for newsletter enthusiasts
# Fix bulk add
