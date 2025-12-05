"""
AI Summarizer Module for Substack Digest Bot

Uses OpenAI API to generate summaries in various formats.
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

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# Model configuration
DEFAULT_MODEL = "gpt-4o-mini"
MAX_TOKENS = 800  # Increased for full SCQRT with timeline
TEMPERATURE = 0.3

# Built-in summary formats
SUMMARY_FORMATS = {
    "scqr": {
        "name": "SCQRT (Minto Pyramid + Timeline)",
        "description": "Situation, Complication, Question, Resolution, Timeline - based on Barbara Minto's Pyramid Principle with industry trajectory",
        "fields": ["situation", "complication", "question", "resolution", "timeline", "technical_terms"],
        "prompt": """Analyze this article using the SCQRT framework (Barbara Minto's Pyramid Principle + Timeline analysis).

IMPORTANT: 
1. Only include facts and claims DIRECTLY stated in the article content below
2. If the article contains technical terms, jargon, or concepts that a general reader might not understand, explain them simply
3. Lead with the answer/resolution (pyramid principle: answer first, then supporting logic)

Article Title: {title}
Source: {feed_name}
Article Content: {content}

Provide a summary in this exact JSON format:
{{
    "situation": "The stable context or background that the reader would agree with (1-2 sentences). This sets up what we already know.",
    "complication": "The change, problem, or tension that disrupts the situation and creates a need for action/understanding (1-2 sentences)",
    "question": "The logical question that arises from the complication - what the reader would naturally ask",
    "resolution": "The KEY ANSWER or insight - state this clearly and directly as it's the most important part (2-3 sentences)",
    "timeline": {{
        "current_state": "Where the industry/topic stands NOW based on the article",
        "growth_trajectory": "How it's developing or evolving (trends, momentum, direction)",
        "challenges": ["Key challenge or gate to further development", "Another barrier mentioned"],
        "future_outlook": "Potential solutions or what needs to happen to overcome the gates"
    }},
    "technical_terms": [
        {{"term": "technical word or concept", "explanation": "simple plain-English explanation"}},
        {{"term": "another term", "explanation": "simple explanation"}}
    ]
}}

GUIDELINES:
- The Resolution should be THE MAIN POINT - what someone should remember if they read nothing else
- Situation → Complication → Question should flow logically (each triggers the next)
- Timeline should capture the industry/topic trajectory IF discussed in the article
- If the article doesn't discuss trajectory/challenges, use null for timeline
- Technical terms array can be empty [] if no jargon needs explaining
- FACT-CHECK: Every point must reference specific content from the article"""
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


def generate_summary(
    title: str,
    content: str,
    feed_name: str = "",
    format_type: str = "scqr",
    custom_prompt: str = None,
) -> Optional[dict]:
    """
    Generate a summary for an article in the specified format.
    
    Args:
        title: Article title
        content: Article content/summary from RSS
        feed_name: Name of the source feed
        format_type: One of the built-in formats or "custom"
        custom_prompt: User's custom prompt (if format_type is "custom")
    
    Returns:
        Dict with summary fields or None if generation fails
    """
    if not OPENAI_API_KEY:
        return None
    
    # Clean and truncate content
    content = clean_html(content)
    if len(content) > 2500:
        content = content[:2500] + "..."
    
    # Get the appropriate prompt
    if custom_prompt:
        prompt = custom_prompt.format(
            title=title,
            feed_name=feed_name,
            content=content
        )
        system_msg = "You are an expert at analyzing articles. Always respond with valid JSON only. Only include information that is directly stated in or clearly supported by the provided article content - do not add external information or assumptions."
    elif format_type in SUMMARY_FORMATS:
        prompt = SUMMARY_FORMATS[format_type]["prompt"].format(
            title=title,
            feed_name=feed_name,
            content=content
        )
        system_msg = "You are an expert at analyzing articles and extracting key insights. Always respond with valid JSON only. CRITICAL: Only include facts and claims that are directly stated in the article - never add external information, assumptions, or inferences beyond what's written."
    else:
        # Default to SCQR
        prompt = SUMMARY_FORMATS["scqr"]["prompt"].format(
            title=title,
            feed_name=feed_name,
            content=content
        )
        system_msg = "You are an expert at analyzing articles and extracting key insights. Always respond with valid JSON only. Only include information directly from the article."
    
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
                    {"role": "system", "content": system_msg},
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
        response_content = data["choices"][0]["message"]["content"].strip()
        
        # Parse JSON from response
        if response_content.startswith("```"):
            response_content = re.sub(r'^```(?:json)?\n?', '', response_content)
            response_content = re.sub(r'\n?```$', '', response_content)
        
        summary = json.loads(response_content)
        return summary
        
    except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
        print(f"Error generating summary: {e}")
        return None


def generate_scqr_summary(
    title: str,
    content: str,
    feed_name: str = "",
) -> Optional[dict]:
    """
    Generate an SCQR-format summary for an article.
    Wrapper for backwards compatibility.
    """
    return generate_summary(title, content, feed_name, format_type="scqr")


def generate_batch_summaries(
    articles: list[dict],
    max_articles: int = 10,
    format_type: str = "scqr",
    custom_prompt: str = None,
) -> list[dict]:
    """
    Generate summaries for a batch of articles.
    
    Args:
        articles: List of article dicts with 'title', 'summary', 'feed_name'
        max_articles: Maximum number of articles to summarize (for cost control)
        format_type: Summary format to use
        custom_prompt: Optional custom prompt
    
    Returns:
        Same list with 'scqr' key added to each article
    """
    if not OPENAI_API_KEY:
        for article in articles:
            article["scqr"] = None
        return articles
    
    for i, article in enumerate(articles[:max_articles]):
        summary = generate_summary(
            title=article.get("title", ""),
            content=article.get("summary", ""),
            feed_name=article.get("feed_name", ""),
            format_type=format_type,
            custom_prompt=custom_prompt,
        )
        article["scqr"] = summary
    
    for article in articles[max_articles:]:
        article["scqr"] = None
    
    return articles


def generate_quick_summary(title: str, content: str) -> Optional[str]:
    """
    Generate a quick one-paragraph summary (non-SCQR format).
    Useful for free tier users or fallback.
    """
    if not OPENAI_API_KEY:
        return None
    
    content = clean_html(content)
    if len(content) > 1500:
        content = content[:1500] + "..."
    
    prompt = f"""Summarize this article in 2-3 sentences, focusing on the key insight or takeaway.

IMPORTANT: Only include information that is DIRECTLY stated in the article below. Do not add external knowledge.

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
    clean = re.sub(r'<[^>]+>', '', text)
    clean = ' '.join(clean.split())
    clean = clean.replace('&amp;', '&')
    clean = clean.replace('&lt;', '<')
    clean = clean.replace('&gt;', '>')
    clean = clean.replace('&quot;', '"')
    clean = clean.replace('&#39;', "'")
    clean = clean.replace('&nbsp;', ' ')
    return clean.strip()


def validate_custom_prompt(prompt: str) -> tuple[bool, str]:
    """
    Validate a custom prompt.
    
    Returns:
        (is_valid, error_message_or_prompt)
    """
    if not prompt or len(prompt.strip()) < 20:
        return False, "Prompt is too short. Please provide more detail."
    
    if len(prompt) > 1000:
        return False, "Prompt is too long. Keep it under 1000 characters."
    
    # Check for required placeholders
    if "{content}" not in prompt:
        prompt += "\n\nArticle Content: {content}"
    
    # Ensure JSON response is requested
    if "json" not in prompt.lower():
        prompt += "\n\nRespond with valid JSON only."
    
    return True, prompt


def estimate_api_cost(num_articles: int) -> dict:
    """
    Estimate OpenAI API cost for summarizing articles.
    """
    avg_input_tokens = 900
    avg_output_tokens = 250
    
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
