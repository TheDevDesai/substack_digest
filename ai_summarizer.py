"""
AI Summarizer Module for Substack Digest Bot

Uses OpenAI API to generate SCQR-format summaries of articles.

SCQR Framework:
- Situation: What is the current state/context?
- Complication: What problem or challenge exists?
- Question: What key question does this raise?
- Resolution: What answer or insight does the article provide?
"""

import os
import json
import requests
from typing import Optional
import re

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# Model configuration
DEFAULT_MODEL = "gpt-4o-mini"  # Cost-effective for summaries
MAX_TOKENS = 300
TEMPERATURE = 0.3  # Lower = more focused/consistent


def generate_scqr_summary(
    title: str,
    content: str,
    feed_name: str = "",
) -> Optional[dict]:
    """
    Generate an SCQR-format summary for an article.
    
    Args:
        title: Article title
        content: Article content/summary from RSS
        feed_name: Name of the source feed
    
    Returns:
        Dict with 'situation', 'complication', 'question', 'resolution' keys
        or None if generation fails
    """
    if not OPENAI_API_KEY:
        return None
    
    # Clean and truncate content
    content = clean_html(content)
    if len(content) > 2000:
        content = content[:2000] + "..."
    
    prompt = f"""Analyze this article and create a concise SCQR summary.

Article Title: {title}
Source: {feed_name}
Content Preview: {content}

Provide a summary in this exact JSON format (keep each section to 1-2 sentences max):
{{
    "situation": "Brief context - what's the current state or background?",
    "complication": "The problem, challenge, or tension being addressed",
    "question": "The key question the article explores or answers",
    "resolution": "The main insight, answer, or takeaway from the article"
}}

Be concise and insightful. Focus on the core value of the article."""

    try:
        response = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEFAULT_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are an expert at analyzing articles and extracting key insights using the SCQR framework. Always respond with valid JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
            },
            timeout=30,
        )
        
        if response.status_code != 200:
            print(f"OpenAI API error: {response.status_code} - {response.text}")
            return None
        
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
        
        # Parse JSON from response
        # Handle potential markdown code blocks
        if content.startswith("```"):
            content = re.sub(r'^```(?:json)?\n?', '', content)
            content = re.sub(r'\n?```$', '', content)
        
        summary = json.loads(content)
        
        # Validate required keys
        required_keys = ["situation", "complication", "question", "resolution"]
        if all(key in summary for key in required_keys):
            return summary
        
        return None
        
    except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
        print(f"Error generating SCQR summary: {e}")
        return None


def generate_batch_summaries(
    articles: list[dict],
    max_articles: int = 10,
) -> list[dict]:
    """
    Generate SCQR summaries for a batch of articles.
    
    Args:
        articles: List of article dicts with 'title', 'summary', 'feed_name'
        max_articles: Maximum number of articles to summarize (for cost control)
    
    Returns:
        Same list with 'scqr' key added to each article
    """
    if not OPENAI_API_KEY:
        # Return articles without AI summaries
        for article in articles:
            article["scqr"] = None
        return articles
    
    # Limit to max_articles for cost control
    for i, article in enumerate(articles[:max_articles]):
        scqr = generate_scqr_summary(
            title=article.get("title", ""),
            content=article.get("summary", ""),
            feed_name=article.get("feed_name", ""),
        )
        article["scqr"] = scqr
    
    # Mark remaining articles as not summarized
    for article in articles[max_articles:]:
        article["scqr"] = None
    
    return articles


def generate_quick_summary(title: str, content: str) -> Optional[str]:
    """
    Generate a quick one-paragraph summary (non-SCQR format).
    Useful for free tier users or fallback.
    
    Returns:
        Summary string or None
    """
    if not OPENAI_API_KEY:
        return None
    
    content = clean_html(content)
    if len(content) > 1500:
        content = content[:1500] + "..."
    
    prompt = f"""Summarize this article in 2-3 sentences, focusing on the key insight or takeaway.

Title: {title}
Content: {content}

Summary:"""

    try:
        response = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEFAULT_MODEL,
                "messages": [
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 150,
                "temperature": 0.3,
            },
            timeout=20,
        )
        
        if response.status_code == 200:
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        
        return None
        
    except (requests.RequestException, KeyError) as e:
        print(f"Error generating quick summary: {e}")
        return None


def clean_html(text: str) -> str:
    """Remove HTML tags and clean up text."""
    # Remove HTML tags
    clean = re.sub(r'<[^>]+>', '', text)
    # Normalize whitespace
    clean = ' '.join(clean.split())
    # Decode common HTML entities
    clean = clean.replace('&amp;', '&')
    clean = clean.replace('&lt;', '<')
    clean = clean.replace('&gt;', '>')
    clean = clean.replace('&quot;', '"')
    clean = clean.replace('&#39;', "'")
    clean = clean.replace('&nbsp;', ' ')
    return clean.strip()


def estimate_api_cost(num_articles: int) -> dict:
    """
    Estimate OpenAI API cost for summarizing articles.
    
    Based on gpt-4o-mini pricing (as of 2024):
    - Input: $0.15 per 1M tokens
    - Output: $0.60 per 1M tokens
    """
    # Rough estimates
    avg_input_tokens = 800  # prompt + article content
    avg_output_tokens = 200  # SCQR response
    
    total_input = num_articles * avg_input_tokens
    total_output = num_articles * avg_output_tokens
    
    input_cost = (total_input / 1_000_000) * 0.15
    output_cost = (total_output / 1_000_000) * 0.60
    total_cost = input_cost + output_cost
    
    return {
        "num_articles": num_articles,
        "estimated_input_tokens": total_input,
        "estimated_output_tokens": total_output,
        "estimated_cost_usd": round(total_cost, 4),
    }
