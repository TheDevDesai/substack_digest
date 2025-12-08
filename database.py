"""
Database Module for Substack Digest Bot

Uses PostgreSQL when DATABASE_URL is set (Railway), falls back to JSON files locally.
This ensures data persists across deployments.
"""

import os
import json
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

# Check if we have a database URL (Railway PostgreSQL)
DATABASE_URL = os.environ.get("DATABASE_URL")

# Will be set to True if PostgreSQL is available
USE_POSTGRES = False
_db_connection = None

if DATABASE_URL:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        USE_POSTGRES = True
        print("[Database] PostgreSQL mode enabled")
    except ImportError:
        print("[Database] psycopg2 not installed, using JSON files")
        USE_POSTGRES = False
else:
    print("[Database] No DATABASE_URL, using JSON files")


# ============================================
#  PostgreSQL Connection Management
# ============================================

def get_db_connection():
    """Get a database connection."""
    global _db_connection
    if not USE_POSTGRES:
        return None
    
    try:
        if _db_connection is None or _db_connection.closed:
            _db_connection = psycopg2.connect(DATABASE_URL)
        return _db_connection
    except Exception as e:
        print(f"[Database] Connection error: {e}")
        return None


def init_database():
    """Initialize database tables if they don't exist."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            # Users table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id VARCHAR(50) PRIMARY KEY,
                    username VARCHAR(100),
                    first_name VARCHAR(100),
                    digest_time VARCHAR(10) DEFAULT '08:00',
                    summary_format VARCHAR(20) DEFAULT 'scqr',
                    custom_prompt TEXT,
                    tier VARCHAR(20) DEFAULT 'free',
                    stripe_customer_id VARCHAR(100),
                    stripe_subscription_id VARCHAR(100),
                    subscription_expires_at TIMESTAMP,
                    subscription_created_at TIMESTAMP DEFAULT NOW(),
                    is_blocked BOOLEAN DEFAULT FALSE,
                    block_reason TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Feeds table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS feeds (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(50) REFERENCES users(user_id) ON DELETE CASCADE,
                    feed_url TEXT NOT NULL,
                    added_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(user_id, feed_url)
                )
            """)
            
            # Seen articles table (to prevent duplicates)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seen_articles (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(50) REFERENCES users(user_id) ON DELETE CASCADE,
                    article_url TEXT NOT NULL,
                    seen_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(user_id, article_url)
                )
            """)
            
            # Bot config table (owner, admins)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key VARCHAR(50) PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Payments/Analytics table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(50),
                    username VARCHAR(100),
                    amount INTEGER,
                    currency VARCHAR(10),
                    payment_id VARCHAR(100),
                    payment_type VARCHAR(50),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # User activity/engagement tracking
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_activity (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(50),
                    action VARCHAR(50),
                    details JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Digest delivery tracking
            cur.execute("""
                CREATE TABLE IF NOT EXISTS digest_logs (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(50),
                    articles_count INTEGER,
                    feeds_count INTEGER,
                    format_used VARCHAR(50),
                    delivery_type VARCHAR(20),
                    processing_time_ms INTEGER,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Feed popularity tracking
            cur.execute("""
                CREATE TABLE IF NOT EXISTS feed_stats (
                    id SERIAL PRIMARY KEY,
                    feed_url TEXT,
                    user_id VARCHAR(50),
                    articles_fetched INTEGER DEFAULT 0,
                    last_fetched_at TIMESTAMP,
                    added_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Article engagement (which articles users receive)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS article_deliveries (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(50),
                    article_url TEXT,
                    article_title TEXT,
                    feed_url TEXT,
                    published_at TIMESTAMP,
                    delivered_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Bot events (errors, milestones, etc.)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(50),
                    event_data JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Rate limits table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rate_limits (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(50),
                    action_type VARCHAR(50),
                    timestamp TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Create indexes for performance
            cur.execute("CREATE INDEX IF NOT EXISTS idx_feeds_user_id ON feeds(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_seen_articles_user_id ON seen_articles(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_user_action ON rate_limits(user_id, action_type)")
            
            conn.commit()
            print("[Database] Tables initialized successfully")
            return True
    except Exception as e:
        print(f"[Database] Init error: {e}")
        conn.rollback()
        return False


# ============================================
#  User Management
# ============================================

def db_ensure_user(user_id: str, username: str = None, first_name: str = None) -> bool:
    """Ensure a user exists in the database."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username, first_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, users.username),
                    first_name = COALESCE(EXCLUDED.first_name, users.first_name),
                    updated_at = NOW()
            """, (str(user_id), username, first_name))
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error ensuring user: {e}")
        conn.rollback()
        return False


def db_get_user(user_id: str) -> Optional[Dict]:
    """Get user data from database."""
    if not USE_POSTGRES:
        return None
    
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (str(user_id),))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[Database] Error getting user: {e}")
        return None


def db_update_user(user_id: str, **kwargs) -> bool:
    """Update user fields."""
    if not USE_POSTGRES or not kwargs:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        # Build dynamic UPDATE query
        fields = ", ".join([f"{k} = %s" for k in kwargs.keys()])
        values = list(kwargs.values()) + [str(user_id)]
        
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE users SET {fields}, updated_at = NOW()
                WHERE user_id = %s
            """, values)
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error updating user: {e}")
        conn.rollback()
        return False


def db_get_all_users() -> List[str]:
    """Get all user IDs."""
    if not USE_POSTGRES:
        return []
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE is_blocked = FALSE")
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        print(f"[Database] Error getting all users: {e}")
        return []


# ============================================
#  Feed Management
# ============================================

def db_add_feed(user_id: str, feed_url: str) -> bool:
    """Add a feed for a user."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO feeds (user_id, feed_url)
                VALUES (%s, %s)
                ON CONFLICT (user_id, feed_url) DO NOTHING
            """, (str(user_id), feed_url))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        print(f"[Database] Error adding feed: {e}")
        conn.rollback()
        return False


def db_remove_feed(user_id: str, feed_url: str) -> bool:
    """Remove a feed for a user."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM feeds WHERE user_id = %s AND feed_url = %s
            """, (str(user_id), feed_url))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        print(f"[Database] Error removing feed: {e}")
        conn.rollback()
        return False


def db_list_feeds(user_id: str) -> List[str]:
    """Get all feeds for a user."""
    if not USE_POSTGRES:
        return []
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT feed_url FROM feeds WHERE user_id = %s ORDER BY added_at
            """, (str(user_id),))
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        print(f"[Database] Error listing feeds: {e}")
        return []


def db_count_feeds(user_id: str) -> int:
    """Count feeds for a user."""
    if not USE_POSTGRES:
        return 0
    
    conn = get_db_connection()
    if not conn:
        return 0
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM feeds WHERE user_id = %s", (str(user_id),))
            return cur.fetchone()[0]
    except Exception as e:
        print(f"[Database] Error counting feeds: {e}")
        return 0


# ============================================
#  Seen Articles (Deduplication)
# ============================================

def db_get_seen_articles(user_id: str) -> set:
    """Get set of article URLs user has already seen."""
    if not USE_POSTGRES:
        return set()
    
    conn = get_db_connection()
    if not conn:
        return set()
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT article_url FROM seen_articles 
                WHERE user_id = %s
            """, (str(user_id),))
            return {row[0] for row in cur.fetchall()}
    except Exception as e:
        print(f"[Database] Error getting seen articles: {e}")
        return set()


def db_mark_articles_seen(user_id: str, article_urls: List[str]) -> bool:
    """Mark articles as seen."""
    if not USE_POSTGRES or not article_urls:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            for url in article_urls:
                cur.execute("""
                    INSERT INTO seen_articles (user_id, article_url)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id, article_url) DO NOTHING
                """, (str(user_id), url))
            
            # Clean up old entries (keep last 500)
            cur.execute("""
                DELETE FROM seen_articles 
                WHERE user_id = %s AND id NOT IN (
                    SELECT id FROM seen_articles 
                    WHERE user_id = %s 
                    ORDER BY seen_at DESC LIMIT 500
                )
            """, (str(user_id), str(user_id)))
            
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error marking articles seen: {e}")
        conn.rollback()
        return False


def db_clear_seen_articles(user_id: str) -> bool:
    """Clear user's seen articles."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seen_articles WHERE user_id = %s", (str(user_id),))
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error clearing seen articles: {e}")
        conn.rollback()
        return False


# ============================================
#  Bot Config (Owner, Admins)
# ============================================

def db_get_config(key: str) -> Any:
    """Get a config value."""
    if not USE_POSTGRES:
        return None
    
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_config WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"[Database] Error getting config: {e}")
        return None


def db_set_config(key: str, value: Any) -> bool:
    """Set a config value."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_config (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = NOW()
            """, (key, json.dumps(value), json.dumps(value)))
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error setting config: {e}")
        conn.rollback()
        return False


def db_get_owner_id() -> Optional[str]:
    """Get owner ID from database."""
    value = db_get_config("owner_id")
    return value if value else None


def db_set_owner_id(user_id: str) -> bool:
    """Set owner ID in database."""
    return db_set_config("owner_id", str(user_id))


def db_get_admins() -> List[str]:
    """Get list of admin IDs."""
    value = db_get_config("admins")
    return value if value else []


def db_add_admin(user_id: str) -> bool:
    """Add an admin."""
    admins = db_get_admins()
    if str(user_id) not in admins:
        admins.append(str(user_id))
        return db_set_config("admins", admins)
    return True


def db_remove_admin(user_id: str) -> bool:
    """Remove an admin."""
    admins = db_get_admins()
    admins = [a for a in admins if a != str(user_id)]
    return db_set_config("admins", admins)


# ============================================
#  Payments/Analytics
# ============================================

def db_record_payment(user_id: str, username: str, amount: int, 
                      currency: str, payment_id: str, payment_type: str = "subscription") -> bool:
    """Record a payment."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO payments (user_id, username, amount, currency, payment_id, payment_type)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (str(user_id), username, amount, currency, payment_id, payment_type))
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error recording payment: {e}")
        conn.rollback()
        return False


def db_get_recent_payments(limit: int = 10) -> List[Dict]:
    """Get recent payments."""
    if not USE_POSTGRES:
        return []
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM payments ORDER BY created_at DESC LIMIT %s
            """, (limit,))
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        print(f"[Database] Error getting payments: {e}")
        return []


def db_get_payment_stats() -> Dict:
    """Get payment statistics."""
    if not USE_POSTGRES:
        return {}
    
    conn = get_db_connection()
    if not conn:
        return {}
    
    try:
        with conn.cursor() as cur:
            stats = {}
            
            # Today
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM payments 
                WHERE created_at >= CURRENT_DATE
            """)
            row = cur.fetchone()
            stats["today"] = {"amount": row[0], "count": row[1]}
            
            # This week
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM payments 
                WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            """)
            row = cur.fetchone()
            stats["week"] = {"amount": row[0], "count": row[1]}
            
            # This month
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM payments 
                WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
            """)
            row = cur.fetchone()
            stats["month"] = {"amount": row[0], "count": row[1]}
            
            # All time
            cur.execute("SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM payments")
            row = cur.fetchone()
            stats["all_time"] = {"amount": row[0], "count": row[1]}
            
            return stats
    except Exception as e:
        print(f"[Database] Error getting payment stats: {e}")
        return {}


# ============================================
#  User Activity Tracking
# ============================================

def db_track_activity(user_id: str, action: str, details: Dict = None) -> bool:
    """Track user activity for analytics."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_activity (user_id, action, details)
                VALUES (%s, %s, %s)
            """, (str(user_id), action, json.dumps(details or {})))
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error tracking activity: {e}")
        conn.rollback()
        return False


def db_log_digest(user_id: str, articles_count: int, feeds_count: int, 
                  format_used: str, delivery_type: str, processing_time_ms: int = 0) -> bool:
    """Log digest delivery for analytics."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO digest_logs (user_id, articles_count, feeds_count, 
                                        format_used, delivery_type, processing_time_ms)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (str(user_id), articles_count, feeds_count, 
                  format_used, delivery_type, processing_time_ms))
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error logging digest: {e}")
        conn.rollback()
        return False


def db_log_article_delivery(user_id: str, article_url: str, article_title: str,
                            feed_url: str, published_at: str = None) -> bool:
    """Log article delivery for engagement tracking."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO article_deliveries (user_id, article_url, article_title, 
                                               feed_url, published_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (str(user_id), article_url, article_title[:500] if article_title else None,
                  feed_url, published_at))
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error logging article delivery: {e}")
        conn.rollback()
        return False


def db_log_event(event_type: str, event_data: Dict = None) -> bool:
    """Log bot events (errors, milestones, etc.)."""
    if not USE_POSTGRES:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_events (event_type, event_data)
                VALUES (%s, %s)
            """, (event_type, json.dumps(event_data or {})))
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error logging event: {e}")
        conn.rollback()
        return False


# ============================================
#  Analytics Queries
# ============================================

def db_get_user_engagement_stats() -> Dict:
    """Get overall user engagement statistics."""
    if not USE_POSTGRES:
        return {}
    
    conn = get_db_connection()
    if not conn:
        return {}
    
    try:
        with conn.cursor() as cur:
            stats = {}
            
            # Total users
            cur.execute("SELECT COUNT(*) FROM users")
            stats["total_users"] = cur.fetchone()[0]
            
            # Active users (received digest in last 7 days)
            cur.execute("""
                SELECT COUNT(DISTINCT user_id) FROM digest_logs 
                WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            """)
            stats["active_users_7d"] = cur.fetchone()[0]
            
            # Pro users
            cur.execute("SELECT COUNT(*) FROM users WHERE tier = 'pro'")
            stats["pro_users"] = cur.fetchone()[0]
            
            # Total feeds
            cur.execute("SELECT COUNT(*) FROM feeds")
            stats["total_feeds"] = cur.fetchone()[0]
            
            # Average feeds per user
            cur.execute("""
                SELECT ROUND(AVG(feed_count)::numeric, 1) FROM (
                    SELECT user_id, COUNT(*) as feed_count FROM feeds GROUP BY user_id
                ) t
            """)
            row = cur.fetchone()
            stats["avg_feeds_per_user"] = float(row[0]) if row[0] else 0
            
            # Total digests sent (all time)
            cur.execute("SELECT COUNT(*) FROM digest_logs")
            stats["total_digests_sent"] = cur.fetchone()[0]
            
            # Digests sent today
            cur.execute("""
                SELECT COUNT(*) FROM digest_logs 
                WHERE created_at >= CURRENT_DATE
            """)
            stats["digests_today"] = cur.fetchone()[0]
            
            # Total articles delivered
            cur.execute("SELECT COUNT(*) FROM article_deliveries")
            stats["total_articles_delivered"] = cur.fetchone()[0]
            
            return stats
    except Exception as e:
        print(f"[Database] Error getting engagement stats: {e}")
        return {}


def db_get_popular_feeds(limit: int = 10) -> List[Dict]:
    """Get most popular feeds by subscriber count."""
    if not USE_POSTGRES:
        return []
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT feed_url, COUNT(*) as subscriber_count
                FROM feeds 
                GROUP BY feed_url 
                ORDER BY subscriber_count DESC 
                LIMIT %s
            """, (limit,))
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        print(f"[Database] Error getting popular feeds: {e}")
        return []


def db_get_user_growth(days: int = 30) -> List[Dict]:
    """Get user growth over time."""
    if not USE_POSTGRES:
        return []
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT DATE(created_at) as date, COUNT(*) as new_users
                FROM users 
                WHERE created_at >= CURRENT_DATE - INTERVAL '%s days'
                GROUP BY DATE(created_at)
                ORDER BY date
            """, (days,))
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        print(f"[Database] Error getting user growth: {e}")
        return []


def db_get_format_usage() -> List[Dict]:
    """Get breakdown of summary format usage."""
    if not USE_POSTGRES:
        return []
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT summary_format, COUNT(*) as user_count
                FROM users 
                WHERE summary_format IS NOT NULL
                GROUP BY summary_format 
                ORDER BY user_count DESC
            """)
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        print(f"[Database] Error getting format usage: {e}")
        return []


def db_get_retention_stats() -> Dict:
    """Get user retention statistics."""
    if not USE_POSTGRES:
        return {}
    
    conn = get_db_connection()
    if not conn:
        return {}
    
    try:
        with conn.cursor() as cur:
            stats = {}
            
            # Users who received at least one digest
            cur.execute("""
                SELECT COUNT(DISTINCT user_id) FROM digest_logs
            """)
            stats["users_received_digest"] = cur.fetchone()[0]
            
            # Users who received digest in last 7 days vs last 30 days (retention)
            cur.execute("""
                SELECT COUNT(DISTINCT user_id) FROM digest_logs 
                WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            """)
            active_7d = cur.fetchone()[0]
            
            cur.execute("""
                SELECT COUNT(DISTINCT user_id) FROM digest_logs 
                WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
                AND created_at < CURRENT_DATE - INTERVAL '7 days'
            """)
            active_prev_23d = cur.fetchone()[0]
            
            if active_prev_23d > 0:
                stats["retention_rate"] = round(active_7d / (active_7d + active_prev_23d) * 100, 1)
            else:
                stats["retention_rate"] = 100.0
            
            return stats
    except Exception as e:
        print(f"[Database] Error getting retention stats: {e}")
        return {}


# ============================================
#  Rate Limiting
# ============================================

def db_check_rate_limit(user_id: str, action: str, window_seconds: int, max_requests: int) -> bool:
    """Check if user is within rate limit. Returns True if allowed."""
    if not USE_POSTGRES:
        return True  # Allow if no DB
    
    conn = get_db_connection()
    if not conn:
        return True
    
    try:
        with conn.cursor() as cur:
            # Clean old entries
            cur.execute("""
                DELETE FROM rate_limits 
                WHERE timestamp < NOW() - INTERVAL '%s seconds'
            """, (window_seconds,))
            
            # Count recent requests
            cur.execute("""
                SELECT COUNT(*) FROM rate_limits 
                WHERE user_id = %s AND action_type = %s 
                AND timestamp > NOW() - INTERVAL '%s seconds'
            """, (str(user_id), action, window_seconds))
            
            count = cur.fetchone()[0]
            
            if count >= max_requests:
                conn.commit()
                return False
            
            # Record this request
            cur.execute("""
                INSERT INTO rate_limits (user_id, action_type) VALUES (%s, %s)
            """, (str(user_id), action))
            
            conn.commit()
            return True
    except Exception as e:
        print(f"[Database] Error checking rate limit: {e}")
        return True


# ============================================
#  Migration: JSON to PostgreSQL
# ============================================

def migrate_json_to_postgres():
    """Migrate existing JSON data to PostgreSQL."""
    if not USE_POSTGRES:
        print("[Migration] PostgreSQL not available")
        return False
    
    print("[Migration] Starting JSON to PostgreSQL migration...")
    
    # Migrate bot_config.json
    if os.path.exists("bot_config.json"):
        try:
            with open("bot_config.json", "r") as f:
                config = json.load(f)
            
            if config.get("owner_id"):
                db_set_owner_id(config["owner_id"])
                print(f"[Migration] Migrated owner: {config['owner_id']}")
            
            if config.get("admins"):
                db_set_config("admins", config["admins"])
                print(f"[Migration] Migrated {len(config['admins'])} admins")
        except Exception as e:
            print(f"[Migration] Error migrating config: {e}")
    
    # Migrate user_state.json
    if os.path.exists("user_state.json"):
        try:
            with open("user_state.json", "r") as f:
                state = json.load(f)
            
            for user_id, data in state.items():
                # Create user
                db_ensure_user(user_id)
                
                # Update user fields
                sub = data.get("subscription", {})
                db_update_user(
                    user_id,
                    digest_time=data.get("digest_time", "08:00"),
                    summary_format=data.get("summary_format", "scqr"),
                    custom_prompt=data.get("custom_prompt"),
                    tier=sub.get("tier", "free"),
                    stripe_customer_id=sub.get("stripe_customer_id"),
                    stripe_subscription_id=sub.get("stripe_subscription_id"),
                    is_blocked=data.get("blocked", False),
                    block_reason=data.get("block_reason")
                )
                
                # Migrate feeds
                for feed_url in data.get("feeds", []):
                    db_add_feed(user_id, feed_url)
                
                # Migrate seen articles
                seen = data.get("seen_articles", [])
                if seen:
                    db_mark_articles_seen(user_id, seen)
                
                print(f"[Migration] Migrated user {user_id}")
            
            print(f"[Migration] Completed: {len(state)} users migrated")
        except Exception as e:
            print(f"[Migration] Error migrating users: {e}")
    
    # Migrate username_map.json
    if os.path.exists("username_map.json"):
        try:
            with open("username_map.json", "r") as f:
                mapping = json.load(f)
            
            for username, data in mapping.items():
                user_id = data.get("user_id")
                if user_id:
                    db_ensure_user(
                        user_id,
                        username=data.get("username"),
                        first_name=data.get("first_name")
                    )
            
            print(f"[Migration] Migrated {len(mapping)} username mappings")
        except Exception as e:
            print(f"[Migration] Error migrating usernames: {e}")
    
    return True


# Initialize on import if PostgreSQL is available
if USE_POSTGRES:
    if init_database():
        # Check if we need to migrate
        if os.path.exists("user_state.json"):
            # Only migrate if DB is empty
            if not db_get_all_users():
                migrate_json_to_postgres()
