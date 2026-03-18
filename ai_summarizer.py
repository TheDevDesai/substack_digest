"""
AI Summarizer Module for Substack Digest Bot

Uses Anthropic Claude API to generate summaries in various formats.
Supports custom user formats and includes fact-checking.

Default Format - SCQR Framework:
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

ANTHROPIC_API_KEY = os.environ.get("OPENAI_API_KEY")  # env var name kept for Railway compatibility
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Model configuration
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1200
TEMPERATURE = 0.3

# Minimum content length to attempt summarisation
MIN_CONTENT_LENGTH = 50

# Built-in summary formats
SUMMARY_FORMATS = {
    "scqr": {
        "name": "SCQRT (Minto Pyramid + Timeline)",
        "description": "Situation, Complication, Question, Resolution, Timeline - based on Barbara Minto's Pyramid Principle with industry trajectory",
        "fields": ["situation", "complication", "question", "resolution", "timeline", "technical_terms"],
        "prompt": """You are a highly distinguished research professor and strategic analyst known for your eloquent, incisive analysis. Your audience consists of CEOs and senior executives who value deep insights over surface-level summaries. 

Analyze this article using the SCQRT framework (Barbara Minto's Pyramid Principle + Timeline analysis).

YOUR APPROACH:
1. First, deeply understand the article's core thesis, supporting evidence, and implications
2. Extract specific numbers, percentages, data points, and concrete facts
3. Identify the strategic implications and second-order effects
4. Capture the author's key arguments and novel insights
5. Be comprehensive yet precise - CEOs want substance, not fluff

Article Title: {title}
Source: {feed_name}
Article Content: {content}

Provide your analysis in this JSON format:

{{
    "situation": "Set the strategic context. What is the established baseline or status quo that frames this discussion? Include relevant market size, growth rates, or key metrics if mentioned. (2-3 sentences)",
    
    "complication": "What disruption, tension, or strategic challenge has emerged? Why does this matter NOW? What are the stakes? Be specific about the forces at play. (2-3 sentences)",
    
    "question": "What is the critical strategic question this raises for decision-makers? Frame it as the question a CEO would ask.",
    
    "resolution": "The core insight and answer. What is the author's key argument or finding? Include specific data points, percentages, or evidence cited. What is the 'so what' for executives? This is the most important section - be thorough. (3-4 sentences)",
    
    "timeline": {{
        "current_state": "Where does this industry/topic stand today? Include specific metrics, market positions, or quantitative context from the article.",
        "growth_trajectory": "What are the key trends, growth vectors, or directional shifts? Include any projections, CAGR, or trajectory data mentioned.",
        "challenges": ["Specific barrier or constraint with detail", "Another concrete challenge - be specific, not generic"],
        "future_outlook": "What needs to happen next? What are the implications? Include any predictions or strategic recommendations from the article."
    }},
    
    "key_facts": [
        "Specific number, statistic, or data point from the article",
        "Another concrete fact or metric worth noting",
        "Key name, company, or entity mentioned with context"
    ],
    
    "technical_terms": [
        {{"term": "technical word or concept", "explanation": "clear explanation a non-specialist executive would appreciate"}}
    ]
}}

CRITICAL GUIDELINES:
- Be SPECIFIC: "revenue grew 47% YoY to $2.3B" not "revenue grew significantly"
- Be ANALYTICAL: explain WHY something matters, not just WHAT happened
- Be SUBSTANTIVE: CEOs want insights they can act on, not generic summaries
- PRESERVE key numbers, names, and concrete details from the article
- If the article lacks data, note the qualitative arguments and their logical basis
- The Resolution should be the insight someone remembers from this article
- FACT-CHECK: Every claim must trace directly to the article content"""
    },
    
    "tldr": {
        "name": "TL;DR",
        "description": "Brief 2-3 sentence summary with key terms explained",
        "fields": ["summary", "technical_terms"],
        "prompt": """Summarize this article in 2-3 sentences.

IMPORTANT: 
1. Only include facts DIRECTLY stated in the article
2. If technical terms are used, include simple explanations

Article Title: {title}
Source: {feed_name}
Article Content: {content}

Provide your response in this exact JSON format:
{{
    "summary": "A concise 2-3 sentence summary leading with the MAIN POINT first",
    "technical_terms": [
        {{"term": "any jargon used", "explanation": "simple explanation"}}
    ]
}}

FACT-CHECK: Only include information explicitly stated in the article above."""
    },
    
    "bullets": {
        "name": "Bullet Points",
        "description": "3-5 key takeaways as bullet points with terms explained",
        "fields": ["takeaways", "technical_terms"],
        "prompt": """Extract the key takeaways from this article.

IMPORTANT: 
1. Only include points DIRECTLY stated in the article
2. List the MOST IMPORTANT point first (pyramid principle)
3. Explain any technical terms

Article Title: {title}
Source: {feed_name}
Article Content: {content}

Provide your response in this exact JSON format:
{{
    "takeaways": [
        "MOST important point FROM the article (list this first)",
        "Second key point FROM the article",
        "Third key point FROM the article"
    ],
    "technical_terms": [
        {{"term": "jargon", "explanation": "simple explanation"}}
    ]
}}

FACT-CHECK: Each bullet must reference specific content from the article."""
    },
    
    "eli5": {
        "name": "ELI5",
        "description": "Explain Like I'm 5 - simple explanation anyone can understand",
        "fields": ["explanation"],
        "prompt": """Explain this article in very simple terms that a 10-year-old could understand.

IMPORTANT: 
1. Base your explanation ONLY on what's in the article
2. Replace ALL jargon and technical terms with simple everyday words
3. Use analogies if helpful

Article Title: {title}
Source: {feed_name}
Article Content: {content}

Provide your response in this exact JSON format:
{{
    "explanation": "A simple, jargon-free explanation of what the article is about and why it matters. Use short sentences and common words."
}}

FACT-CHECK: Keep the explanation grounded in the article's actual content."""
    },
    
    "actionable": {
        "name": "Actionable",
        "description": "Key actions or lessons you can apply",
        "fields": ["actions", "lesson", "technical_terms"],
        "prompt": """Extract actionable insights from this article.

IMPORTANT: 
1. Only include actions DIRECTLY suggested by the article
2. Lead with the most impactful action
3. Explain any technical terms

Article Title: {title}
Source: {feed_name}
Article Content: {content}

Provide your response in this exact JSON format:
{{
    "lesson": "The MAIN lesson or principle FROM the article (state this first - it's the key insight)",
    "actions": [
        "Most impactful action suggested BY the article",
        "Another specific action FROM the article"
    ],
    "technical_terms": [
        {{"term": "jargon", "explanation": "simple explanation"}}
    ]
}}

FACT-CHECK: Each action must be traceable to specific content in the article."""
    },
}


def get_available_formats() -> dict:
    """Return dict of available summary formats."""
    return {
        key: {"name": fmt["name"], "description": fmt["description"]}
        for key, fmt in SUMMARY_FORMATS.items()
    }


def _call_api(system_msg: Optional[str], user_msg: str, max_tokens: int) -> Optional[str]:
    """
    Make a single Anthropic API call. Returns response text or None on failure.
    Centralises all request/response logic so each generate_* function
    doesn't repeat boilerplate.
    """
    if not ANTHROPIC_API_KEY:
        return None

    body = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": user_msg}],
        "max_tokens": max_tokens,
        "temperature": TEMPERATURE,
    }
    if system_msg:
        body["system"] = system_msg

    try:
        response = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )

        if response.status_code != 200:
            print(f"Anthropic API error: {response.status_code} - {response.text}")
            return None

        data = response.json()
        return data["content"][0]["text"].strip()

    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"API call error: {e}")
        return None


def generate_summary(
    title: str,
    content: str,
    feed_name: str = "",
    format_type: str = "scqr",
    custom_prompt: str = None,
) -> Optional[dict]:
    """
    Generate a summary for an article in the specified format.

    Returns a dict with summary fields, or None if generation fails or
    the article content is too short to summarise meaningfully.
    """
    if not ANTHROPIC_API_KEY:
        return None

    # Clean content first, then check length
    content = clean_html(content)
    if len(content) < MIN_CONTENT_LENGTH:
        return None  # Not enough content to summarise

    if len(content) > 2500:
        content = content[:2500] + "..."

    # Build prompt and system message
    if custom_prompt:
        prompt = custom_prompt.format(title=title, feed_name=feed_name, content=content)
        system_msg = (
            "You are an expert at analyzing articles. Always respond with valid JSON only. "
            "Only include information that is directly stated in or clearly supported by the "
            "provided article content - do not add external information or assumptions."
        )
    elif format_type in SUMMARY_FORMATS:
        prompt = SUMMARY_FORMATS[format_type]["prompt"].format(
            title=title, feed_name=feed_name, content=content
        )
        system_msg = (
            "You are an expert at analyzing articles and extracting key insights. "
            "Always respond with valid JSON only. CRITICAL: Only include facts and claims "
            "that are directly stated in the article - never add external information, "
            "assumptions, or inferences beyond what's written."
        )
    else:
        prompt = SUMMARY_FORMATS["scqr"]["prompt"].format(
            title=title, feed_name=feed_name, content=content
        )
        system_msg = (
            "You are an expert at analyzing articles and extracting key insights. "
            "Always respond with valid JSON only. Only include information directly from the article."
        )

    raw = _call_api(system_msg, prompt, MAX_TOKENS)
    if not raw:
        return None

    # Strip markdown code fences if present
    raw = re.sub(r'^```(?:json)?\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"JSON parse error in generate_summary: {e}")
        return None


def generate_scqr_summary(
    title: str,
    content: str,
    feed_name: str = "",
) -> Optional[dict]:
    """Generate an SCQR-format summary. Wrapper for backwards compatibility."""
    return generate_summary(title, content, feed_name, format_type="scqr")


def generate_batch_summaries(
    articles: list[dict],
    max_articles: int = 10,
    format_type: str = "scqr",
    custom_prompt: str = None,
) -> list[dict]:
    """
    Generate summaries for a batch of articles.

    Adds a 'scqr' key to each article dict (None if generation fails or
    content is too short).
    """
    if not ANTHROPIC_API_KEY:
        for article in articles:
            article["scqr"] = None
        return articles

    for article in articles[:max_articles]:
        article["scqr"] = generate_summary(
            title=article.get("title", ""),
            content=article.get("summary", ""),
            feed_name=article.get("feed_name", ""),
            format_type=format_type,
            custom_prompt=custom_prompt,
        )

    for article in articles[max_articles:]:
        article["scqr"] = None

    return articles


def generate_quick_summary(title: str, content: str) -> Optional[str]:
    """
    Generate a quick one-paragraph summary (non-SCQR format).
    Useful for free tier users or fallback.
    """
    content = clean_html(content)
    if len(content) < MIN_CONTENT_LENGTH:
        return None

    if len(content) > 1500:
        content = content[:1500] + "..."

    prompt = (
        f"Summarize this article in 2-3 sentences, focusing on the key insight or takeaway.\n\n"
        f"IMPORTANT: Only include information that is DIRECTLY stated in the article below. "
        f"Do not add external knowledge.\n\n"
        f"Title: {title}\nContent: {content}\n\nSummary:"
    )

    return _call_api(None, prompt, 200)


def clean_html(text: str) -> str:
    """Remove HTML tags (including malformed/truncated ones) and clean up text."""
    # >? makes the closing bracket optional so truncated tags like </div are caught
    clean = re.sub(r'<[^>]*>?', '', text)
    clean = ' '.join(clean.split())
    clean = (
        clean
        .replace('&amp;', '&')
        .replace('&lt;', '<')
        .replace('&gt;', '>')
        .replace('&quot;', '"')
        .replace('&#39;', "'")
        .replace('&nbsp;', ' ')
    )
    return clean.strip()


def validate_custom_prompt(prompt: str) -> tuple[bool, str]:
    """
    Validate and normalise a custom prompt.

    Returns (is_valid, normalised_prompt_or_error_message).
    """
    if not prompt or len(prompt.strip()) < 20:
        return False, "Prompt is too short. Please provide more detail."

    if len(prompt) > 1000:
        return False, "Prompt is too long. Keep it under 1000 characters."

    if "{content}" not in prompt:
        prompt += "\n\nArticle Content: {content}"

    if "json" not in prompt.lower():
        prompt += "\n\nRespond with valid JSON only."

    return True, prompt


def estimate_api_cost(num_articles: int) -> dict:
    """Estimate Anthropic API cost for summarising articles (Claude Haiku pricing)."""
    avg_input_tokens = 900
    avg_output_tokens = 250

    total_input = num_articles * avg_input_tokens
    total_output = num_articles * avg_output_tokens

    # Claude Haiku: $0.80/M input, $4/M output (as of 2025)
    input_cost = (total_input / 1_000_000) * 0.80
    output_cost = (total_output / 1_000_000) * 4.00
    total_cost = input_cost + output_cost

    return {
        "num_articles": num_articles,
        "estimated_input_tokens": total_input,
        "estimated_output_tokens": total_output,
        "estimated_cost_usd": round(total_cost, 4),
    }
