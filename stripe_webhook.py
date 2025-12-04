"""
Stripe Webhook Handler for Substack Digest Bot

Handles subscription payments via Stripe webhooks.
Deploy this as a separate service (e.g., Vercel, Railway, or AWS Lambda).

Required environment variables:
- STRIPE_SECRET_KEY: Your Stripe secret key
- STRIPE_WEBHOOK_SECRET: Webhook signing secret
- TELEGRAM_BOT_TOKEN: For sending notifications to users
"""

import os
import json
import hmac
import hashlib
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import requests

# Try to import Flask (for webhook server)
try:
    from flask import Flask, request, jsonify
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

from manage_feeds import (
    upgrade_subscription,
    downgrade_to_free,
    get_stripe_customer_id,
    set_stripe_customer_id,
    ensure_user,
    TIERS,
)

# Configuration
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

STRIPE_API_BASE = "https://api.stripe.com/v1"

# Price IDs for each tier (set these in your Stripe dashboard)
STRIPE_PRICE_IDS = {
    "basic": os.environ.get("STRIPE_PRICE_BASIC", "price_basic_monthly"),
    "pro": os.environ.get("STRIPE_PRICE_PRO", "price_pro_monthly"),
}


# -----------------------------
#  Stripe API Helpers
# -----------------------------

def stripe_request(method: str, endpoint: str, data: dict = None) -> Optional[dict]:
    """Make an authenticated request to Stripe API."""
    if not STRIPE_SECRET_KEY:
        print("STRIPE_SECRET_KEY not configured")
        return None
    
    url = f"{STRIPE_API_BASE}/{endpoint}"
    
    try:
        response = requests.request(
            method=method,
            url=url,
            auth=(STRIPE_SECRET_KEY, ""),
            data=data,
            timeout=30,
        )
        
        if response.status_code in (200, 201):
            return response.json()
        else:
            print(f"Stripe API error: {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Stripe request error: {e}")
        return None


def create_customer(telegram_user_id: str, email: str = None) -> Optional[str]:
    """Create a Stripe customer for a Telegram user."""
    data = {
        "metadata[telegram_user_id]": telegram_user_id,
    }
    if email:
        data["email"] = email
    
    result = stripe_request("POST", "customers", data)
    if result:
        customer_id = result.get("id")
        set_stripe_customer_id(telegram_user_id, customer_id)
        return customer_id
    return None


def create_checkout_session(
    telegram_user_id: str,
    tier: str,
    success_url: str,
    cancel_url: str,
) -> Optional[str]:
    """
    Create a Stripe Checkout session for subscription.
    
    Returns:
        Checkout URL or None
    """
    if tier not in STRIPE_PRICE_IDS:
        return None
    
    price_id = STRIPE_PRICE_IDS[tier]
    
    # Get or create Stripe customer
    customer_id = get_stripe_customer_id(telegram_user_id)
    if not customer_id:
        customer_id = create_customer(telegram_user_id)
        if not customer_id:
            return None
    
    data = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[telegram_user_id]": telegram_user_id,
        "metadata[tier]": tier,
        "subscription_data[metadata][telegram_user_id]": telegram_user_id,
        "subscription_data[metadata][tier]": tier,
    }
    
    result = stripe_request("POST", "checkout/sessions", data)
    if result:
        return result.get("url")
    return None


def create_billing_portal_session(telegram_user_id: str, return_url: str) -> Optional[str]:
    """
    Create a Stripe Billing Portal session for managing subscription.
    
    Returns:
        Portal URL or None
    """
    customer_id = get_stripe_customer_id(telegram_user_id)
    if not customer_id:
        return None
    
    data = {
        "customer": customer_id,
        "return_url": return_url,
    }
    
    result = stripe_request("POST", "billing_portal/sessions", data)
    if result:
        return result.get("url")
    return None


# -----------------------------
#  Webhook Verification
# -----------------------------

def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verify Stripe webhook signature."""
    if not STRIPE_WEBHOOK_SECRET:
        print("STRIPE_WEBHOOK_SECRET not configured")
        return False
    
    try:
        # Parse the signature header
        sig_parts = dict(item.split("=") for item in signature.split(","))
        timestamp = sig_parts.get("t")
        v1_signature = sig_parts.get("v1")
        
        if not timestamp or not v1_signature:
            return False
        
        # Check timestamp (within 5 minutes)
        if abs(time.time() - int(timestamp)) > 300:
            print("Webhook timestamp too old")
            return False
        
        # Compute expected signature
        signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
        expected_sig = hmac.new(
            STRIPE_WEBHOOK_SECRET.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        
        return hmac.compare_digest(expected_sig, v1_signature)
        
    except Exception as e:
        print(f"Signature verification error: {e}")
        return False


# -----------------------------
#  Webhook Event Handlers
# -----------------------------

def handle_checkout_completed(event: dict) -> bool:
    """Handle successful checkout."""
    session = event.get("data", {}).get("object", {})
    
    telegram_user_id = session.get("metadata", {}).get("telegram_user_id")
    tier = session.get("metadata", {}).get("tier")
    subscription_id = session.get("subscription")
    customer_id = session.get("customer")
    
    if not all([telegram_user_id, tier, subscription_id]):
        print("Missing required metadata in checkout session")
        return False
    
    # Calculate expiry (1 month from now)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    
    # Upgrade the user
    success = upgrade_subscription(
        user_id=telegram_user_id,
        tier=tier,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
        expires_at=expires_at,
    )
    
    if success:
        # Notify user via Telegram
        notify_user(
            telegram_user_id,
            f"🎉 <b>Subscription Activated!</b>\n\n"
            f"You're now on the <b>{tier.upper()}</b> plan.\n"
            f"• Max feeds: {TIERS[tier]['max_feeds']}\n"
            f"• AI summaries: {'✅' if TIERS[tier]['ai_summaries'] else '❌'}\n\n"
            f"Enjoy your enhanced digest experience!",
        )
    
    return success


def handle_subscription_updated(event: dict) -> bool:
    """Handle subscription updates (plan changes, renewals)."""
    subscription = event.get("data", {}).get("object", {})
    
    telegram_user_id = subscription.get("metadata", {}).get("telegram_user_id")
    tier = subscription.get("metadata", {}).get("tier", "basic")
    status = subscription.get("status")
    subscription_id = subscription.get("id")
    customer_id = subscription.get("customer")
    
    if not telegram_user_id:
        print("No telegram_user_id in subscription metadata")
        return False
    
    if status == "active":
        # Get current period end
        current_period_end = subscription.get("current_period_end")
        if current_period_end:
            expires_at = datetime.fromtimestamp(
                current_period_end, tz=timezone.utc
            ).isoformat()
        else:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        
        return upgrade_subscription(
            user_id=telegram_user_id,
            tier=tier,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            expires_at=expires_at,
        )
    
    return True


def handle_subscription_deleted(event: dict) -> bool:
    """Handle subscription cancellation."""
    subscription = event.get("data", {}).get("object", {})
    
    telegram_user_id = subscription.get("metadata", {}).get("telegram_user_id")
    
    if not telegram_user_id:
        print("No telegram_user_id in subscription metadata")
        return False
    
    # Downgrade to free tier
    downgrade_to_free(telegram_user_id)
    
    # Notify user
    notify_user(
        telegram_user_id,
        "😢 <b>Subscription Cancelled</b>\n\n"
        "Your subscription has ended. You've been moved to the free plan.\n"
        "• Max feeds: 3\n"
        "• AI summaries: ❌\n\n"
        "Use /upgrade anytime to resubscribe!",
    )
    
    return True


def handle_payment_failed(event: dict) -> bool:
    """Handle failed payment."""
    invoice = event.get("data", {}).get("object", {})
    
    customer_id = invoice.get("customer")
    
    # Look up user by customer ID
    # This is a simplified approach - in production, maintain a customer->user mapping
    from manage_feeds import load_state
    state = load_state()
    
    telegram_user_id = None
    for user_id, user_data in state.items():
        sub = user_data.get("subscription", {})
        if sub.get("stripe_customer_id") == customer_id:
            telegram_user_id = user_id
            break
    
    if telegram_user_id:
        notify_user(
            telegram_user_id,
            "⚠️ <b>Payment Failed</b>\n\n"
            "We couldn't process your subscription payment.\n"
            "Please update your payment method to avoid service interruption.\n\n"
            "Use /manage to update your billing info.",
        )
    
    return True


def process_webhook_event(event: dict) -> bool:
    """Route webhook event to appropriate handler."""
    event_type = event.get("type", "")
    
    handlers = {
        "checkout.session.completed": handle_checkout_completed,
        "customer.subscription.updated": handle_subscription_updated,
        "customer.subscription.deleted": handle_subscription_deleted,
        "invoice.payment_failed": handle_payment_failed,
    }
    
    handler = handlers.get(event_type)
    if handler:
        print(f"Processing webhook event: {event_type}")
        return handler(event)
    else:
        print(f"Unhandled webhook event type: {event_type}")
        return True


# -----------------------------
#  Telegram Notifications
# -----------------------------

def notify_user(telegram_user_id: str, message: str) -> bool:
    """Send a notification to a user via Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not configured")
        return False
    
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": telegram_user_id,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        return response.ok
    except requests.RequestException as e:
        print(f"Error notifying user: {e}")
        return False


# -----------------------------
#  Flask Webhook Server
# -----------------------------

if FLASK_AVAILABLE:
    app = Flask(__name__)
    
    @app.route("/webhook/stripe", methods=["POST"])
    def stripe_webhook():
        """Stripe webhook endpoint."""
        payload = request.get_data()
        signature = request.headers.get("Stripe-Signature", "")
        
        # Verify signature
        if not verify_webhook_signature(payload, signature):
            return jsonify({"error": "Invalid signature"}), 400
        
        # Parse event
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON"}), 400
        
        # Process event
        success = process_webhook_event(event)
        
        if success:
            return jsonify({"status": "ok"}), 200
        else:
            return jsonify({"error": "Event processing failed"}), 500
    
    @app.route("/health", methods=["GET"])
    def health_check():
        """Health check endpoint."""
        return jsonify({"status": "healthy"}), 200


# -----------------------------
#  CLI / Lambda Handler
# -----------------------------

def lambda_handler(event, context):
    """AWS Lambda handler for webhook."""
    body = event.get("body", "")
    if isinstance(body, str):
        payload = body.encode()
    else:
        payload = body
    
    signature = event.get("headers", {}).get("Stripe-Signature", "")
    
    if not verify_webhook_signature(payload, signature):
        return {"statusCode": 400, "body": "Invalid signature"}
    
    try:
        webhook_event = json.loads(payload)
        success = process_webhook_event(webhook_event)
        
        if success:
            return {"statusCode": 200, "body": "OK"}
        else:
            return {"statusCode": 500, "body": "Processing failed"}
            
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "Invalid JSON"}


if __name__ == "__main__":
    if FLASK_AVAILABLE:
        # Run Flask development server
        print("Starting Stripe webhook server...")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    else:
        print("Flask not installed. Install with: pip install flask")
