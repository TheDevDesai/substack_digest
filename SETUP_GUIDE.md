# 🚀 Setup Guide: Substack Digest Bot

## Step 1: Push to GitHub

### Option A: Replace files manually (Easiest)

1. Download the zip file from this conversation
2. Extract it locally
3. In your local `substack_digest` repo folder, replace all files:

```bash
# Navigate to your repo
cd path/to/substack_digest

# Remove old files (keep .git folder!)
rm -rf *.py *.json *.txt *.md .github LICENSE
rm -rf __pycache__

# Copy new files from extracted zip
cp -r path/to/substack_digest_clean/* .

# Stage, commit, and push
git add -A
git commit -m "Refactor: Add AI summaries, security, and subscriptions"
git push origin main
```

### Option B: Quick terminal commands

```bash
# Clone your repo fresh
git clone https://github.com/TheDevDesai/substack_digest.git
cd substack_digest

# Download and extract new code (replace URL with actual download link)
# Then copy all files into the repo

git add -A
git commit -m "Refactor: Add AI summaries, security, and subscriptions"
git push origin main
```

---

## Step 2: Configure GitHub Secrets

Go to: https://github.com/TheDevDesai/substack_digest/settings/secrets/actions

Add these secrets:

| Secret Name | Required | How to Get |
|-------------|----------|------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | From @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Your Telegram user ID |
| `OPENAI_API_KEY` | For AI summaries | https://platform.openai.com/api-keys |
| `STRIPE_SECRET_KEY` | For payments | https://dashboard.stripe.com/apikeys |
| `STRIPE_WEBHOOK_SECRET` | For payments | After creating webhook |
| `STRIPE_PRICE_BASIC` | For payments | Your Stripe price ID |
| `STRIPE_PRICE_PRO` | For payments | Your Stripe price ID |
| `WEBHOOK_BASE_URL` | For payments | Your deployed webhook URL |

---

## Step 3: Get Your Telegram Chat ID

If you don't have it:

1. Send any message to your bot
2. Open this URL in browser (replace TOKEN):
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
3. Find `"chat":{"id":XXXXXXXX}` - that's your chat ID

---

## Step 4: Set Up OpenAI API (for AI Summaries)

1. Go to https://platform.openai.com/api-keys
2. Create a new API key
3. Add billing/credits (~$5 is plenty for months of use)
4. Add the key to GitHub secrets as `OPENAI_API_KEY`

---

## Step 5: Test the Bot

### Manual Test (GitHub Actions)

1. Go to: https://github.com/TheDevDesai/substack_digest/actions
2. Click "Substack Digest Bot" workflow
3. Click "Run workflow" → Select "commands" mode → Run
4. In Telegram, send `/start` to your bot

### Add Your Feeds

In Telegram:
```
/addfeed https://notboring.co/feed
/addfeed https://stratechery.com/feed
/feedlist
/digest
```

---

## Step 6: Set Up Stripe Payments (Optional)

### 6a. Create Stripe Products

1. Go to https://dashboard.stripe.com/products
2. Create "Basic Plan" - $5/month recurring
3. Create "Pro Plan" - $12/month recurring
4. Copy the Price IDs (start with `price_`)

### 6b. Deploy Webhook Server

**Option 1: Vercel (Free)**

1. Create `vercel.json` in your repo:
```json
{
  "builds": [{"src": "stripe_webhook.py", "use": "@vercel/python"}],
  "routes": [{"src": "/(.*)", "dest": "stripe_webhook.py"}]
}
```

2. Deploy:
```bash
npm i -g vercel
vercel deploy --prod
```

3. Your webhook URL: `https://your-project.vercel.app/webhook/stripe`

**Option 2: Railway**

1. Go to https://railway.app
2. New Project → Deploy from GitHub repo
3. Add environment variables
4. Your webhook URL: `https://your-app.up.railway.app/webhook/stripe`

### 6c. Configure Stripe Webhook

1. Go to https://dashboard.stripe.com/webhooks
2. Add endpoint: `https://your-webhook-url/webhook/stripe`
3. Select events:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
4. Copy signing secret → Add to GitHub as `STRIPE_WEBHOOK_SECRET`

---

## Step 7: Migrate Your Existing Feeds

Your current feeds from `user_state.json` need to be migrated to the new format.

Current format:
```json
{
  "YOUR_CHAT_ID_HERE": {
    "feeds": ["url1", "url2", ...]
  }
}
```

New format (the bot will auto-create this structure):
```json
{
  "YOUR_ACTUAL_CHAT_ID": {
    "feeds": ["url1", "url2", ...],
    "digest_time": "08:00",
    "last_sent_date": null,
    "subscription": {
      "tier": "free",
      "stripe_customer_id": null,
      "stripe_subscription_id": null,
      "expires_at": null,
      "created_at": "2024-01-01T00:00:00+00:00"
    },
    "rate_limits": {
      "command_timestamps": [],
      "feed_add_timestamps": [],
      "digest_request_timestamps": []
    },
    "security": {
      "blocked": false,
      "block_reason": null,
      "failed_attempts": 0
    }
  }
}
```

**Quick migration**: Just replace `YOUR_CHAT_ID_HERE` with your actual Telegram chat ID, and the bot will auto-populate the rest when you interact with it.

---

## Troubleshooting

**Bot not responding?**
- Check Actions tab for errors
- Verify TELEGRAM_BOT_TOKEN is correct
- Make sure workflow is enabled

**No AI summaries?**
- Check OPENAI_API_KEY is set
- Verify you have OpenAI credits
- Free tier users don't get AI (upgrade with /upgrade)

**Actions failing?**
- Check the logs in GitHub Actions
- Common issue: missing secrets

---

## Daily Schedule

The bot automatically sends digests at:
- **00:00 UTC** (08:00 SGT)

To change this, edit `.github/workflows/substack-digest.yml`:
```yaml
schedule:
  - cron: "0 0 * * *"  # Change this line
```

Use https://crontab.guru to generate cron expressions.

---

## Questions?

Open an issue on GitHub or check the README for more details!
