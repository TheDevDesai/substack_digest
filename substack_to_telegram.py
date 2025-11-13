import os
import datetime as dt
from dateutil import parser as date_parser
from datetime import timezone

import html

import feedparser
import requests
from openai import OpenAI

# ---------- CONFIG ----------
SUBSTACK_FEEDS = [
    "https://digitalnative.tech/feed",                     # Digital Native (Rex Woodbury)
    "https://riskpremiumresearch.substack.com/feed",       # Risk Premium: Research
    "https://riskpremium.substack.com/feed",               # Risk Premium
    "https://www.forkable.io/feed",                        # {forkable}
    "https://www.notboring.co/feed",                       # Not Boring
    "https://accessiblearthistory.substack.com/feed",      # Accessible Art History
    "https://a16z.substack.com/feed",                      # a16z
]

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

LAST_RUN_FILE = "last_run.txt"

client = OpenAI(api_key=OPENAI_API_KEY)

def escape_html(s: str) -> str:
    return html.escape(s, quote=True)

# ---------- LAST RUN HELPERS ----------
def get_last_run():
    """Return last run datetime in UTC; default = now - 7 days."""
    if not os.path.exists(LAST_RUN_FILE):
        return dt.datetime.now(timezone.utc) - dt.timedelta(days=1)

    with open(LAST_RUN_FILE, "r") as f:
        parsed = date_parser.parse(f.read().strip())

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def set_last_run():
    now = dt.datetime.now(timezone.utc).isoformat()
    with open(LAST_RUN_FILE, "w") as f:
        f.write(now)


# ---------- FETCH SUBSTACK POSTS ----------
def fetch_new_entries():
    cutoff = get_last_run()
    all_entries = []

    for feed_url in SUBSTACK_FEEDS:
        parsed = feedparser.parse(feed_url)

        for entry in parsed.entries:
            # Parse published/updated time
            if hasattr(entry, "published"):
                published = date_parser.parse(entry.published)
            elif hasattr(entry, "updated"):
                published = date_parser.parse(entry.updated)
            else:
                continue

            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)

            if published > cutoff:
                content = ""
                if "content" in entry and entry.content:
                    content = entry.content[0].get("value", "")
                summary = getattr(entry, "summary", "")

                all_entries.append(
                    {
                        "title": entry.title,
                        "link": entry.link,
                        "published": published,
                        "summary": summary,
                        "content": content,
                        "source": feed_url,
                    }
                )

    # Sort by published time
    all_entries.sort(key=lambda e: e["published"])
    return all_entries


# ---------- SUMMARISE ARTICLE (SCQR STYLE) ----------
def summarise_article(article):
    text = article["content"] or article["summary"] or ""
    text = text[:9000]  # safety limit

    prompt = f"""
You are summarising a Substack article for a very analytical reader who prefers Barbara Minto's Pyramid Principle and the SCQR (Situation–Complication–Question–Resolution) structure.

Article title: {article['title']}
URL: {article['link']}
Source feed: {article['source']}

Article content (may be HTML or partial):
{text}

TASK:
- Produce a structured summary in this EXACT format (no extra headings, no preamble):

S: <1–3 sentences describing the current situation / context>
C: <2–4 sentences explaining the complication, tension, or change>
Q: <1–2 sentences stating the key question or decision implied by the article>
R: <2–4 sentences summarising the author's main answer, argument, or recommendations>

Discussion:
- <bullet 1 with a key insight, nuance, or implication>
- <bullet 2 ...>
- <optional bullet 3 ...>

Timeline & Gates:
- <bullet 1 describing past or present milestone OR a forward-looking checkpoint ("gate")>
- <bullet 2 ...>
- <optional bullet 3 ...>

CONSTRAINTS:
- Break down any complex, technical jargon into simple explanations with the perspective and knowledge of an expert in the field.
- Use clear, concise language but keep the content substantive (roughly 220–320 words total).
- Where relevant, briefly introduce historical or background context in S or C.
- Timeline & Gates should focus on 2–4 concrete milestones (past or future) that matter for the thesis.
- Do NOT repeat the URL; it's handled outside the summary.
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
    )

    return response.choices[0].message.content.strip()


# ---------- BUILD DIGEST ----------
def build_daily_digest(entries):
    if not entries:
        return "📚 Substack Daily Digest\n\nNo new articles since last run."

    today = dt.datetime.now().strftime("%Y-%m-%d")
    text = f"📚 Substack Daily Digest — {today}\n"

    for i, article in enumerate(entries, start=1):
        summary = summarise_article(article)
        pub_utc = article["published"].strftime("%Y-%m-%d %H:%M UTC")

        title_html = (
            f'<a href="{escape_html(article["link"])}">'
            f'Article {i}: {escape_html(article["title"])}'
            f"</a>"
        )

        text += (
            f"\n\n====================\n"
            f"Article {i} (link: {article['link']}):\n"
            f"Source: {article['source']}\n"
            f"Published: {pub_utc}\n\n"
            f"{summary}"
        )

    return text


# ---------- SEND TO TELEGRAM ----------
def send_long_message(text, chunk_size=3500):
    """Split long digest into multiple Telegram messages."""
    paragraphs = text.split("\n\n")
    current = ""

    def send_chunk(chunk):
        if not chunk.strip():
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML",}
        requests.post(url, json=payload)

    for p in paragraphs:
        if len(current) + len(p) + 2 <= chunk_size:
            current = p if not current else current + "\n\n" + p
        else:
            send_chunk(current)
            current = p

    if current:
        send_chunk(current)


# ---------- MAIN ----------
def main():
    if not (OPENAI_API_KEY and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise RuntimeError("Missing environment variables")

    entries = fetch_new_entries()
    digest = build_daily_digest(entries)
    send_long_message(digest)
    set_last_run()


if __name__ == "__main__":
    main()

