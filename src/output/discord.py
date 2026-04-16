"""Discord webhook delivery for Horizon channel digests."""

import os
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import httpx

from ..models import ContentItem


def format_digest_embed(
    channel_name: str,
    items: List[ContentItem],
    total_fetched: int,
    source_counts: dict,
) -> dict:
    """Format scored items into a Discord embed object.

    Args:
        channel_name: Display name for the channel (e.g. "bess-daily")
        items: Scored and filtered items, sorted by score descending
        total_fetched: Total items fetched before filtering
        source_counts: Dict like {"rss": 11, "reddit": 3} for the footer

    Returns:
        Discord embed dict ready for the webhook payload
    """
    sgt = timezone(timedelta(hours=8))
    today = datetime.now(sgt).strftime("%B %-d, %Y")
    title_name = channel_name.upper().replace("-", " ")

    if not items:
        return {
            "title": f"\U0001f4ca {title_name} \u2014 {today}",
            "description": "No significant developments today. Sources were checked but nothing met the score threshold.",
            "color": 0x00b4d8,
            "footer": {"text": "Curated by Horizon + Gemini Flash"},
        }

    lines = []
    for item in items:
        score = item.ai_score or 0
        star = "\u2b50" if score >= 8.0 else "\u2022"
        summary = item.ai_summary or item.title
        url = str(item.url)
        domain = url.split("//")[-1].split("/")[0].replace("www.", "")
        lines.append(
            f"{star} **{score:.1f}** | {summary}\n"
            f"       \U0001f517 [{domain}]({url})"
        )

    description = "\n\n".join(lines)

    # Discord embed description limit is 4096 chars
    if len(description) > 4000:
        description = description[:3997] + "..."

    source_parts = []
    if source_counts.get("rss"):
        source_parts.append(f"{source_counts['rss']} RSS")
    if source_counts.get("reddit"):
        source_parts.append(f"{source_counts['reddit']} subreddits")
    if source_counts.get("github"):
        source_parts.append(f"{source_counts['github']} repos")
    sources_str = ", ".join(source_parts) if source_parts else "configured sources"

    return {
        "title": f"\U0001f4ca {title_name} \u2014 {today}",
        "description": description,
        "color": 0x00b4d8,
        "footer": {
            "text": f"Curated by Horizon + Gemini Flash | Sources: {sources_str} | {total_fetched} items scanned"
        },
    }


def format_enriched_message(item: ContentItem) -> Optional[str]:
    """Format an enriched item as a Discord follow-up message.

    Args:
        item: A high-scoring enriched item

    Returns:
        Formatted message string, or None if no enrichment data
    """
    meta = item.metadata
    score = item.ai_score or 0
    title = meta.get("title_en") or item.title
    url = str(item.url)

    parts = [f"**\u2b50 {score:.1f} | {title}**"]
    parts.append(f"\U0001f517 {url}\n")

    # Build enriched summary from structured fields
    detailed = meta.get("detailed_summary_en") or meta.get("detailed_summary") or ""
    background = meta.get("background_en") or meta.get("background") or ""
    discussion = meta.get("community_discussion_en") or meta.get("community_discussion") or ""

    if detailed:
        parts.append(detailed)
    if background:
        parts.append(f"\n**Background:** {background}")
    if discussion:
        parts.append(f"\n**Community Discussion:** {discussion}")

    tags = item.ai_tags
    if tags:
        parts.append("\n" + " ".join(f"`#{t}`" for t in tags))

    message = "\n".join(parts)

    # Discord message limit is 2000 chars
    if len(message) > 1950:
        message = message[:1947] + "..."

    return message


async def post_to_discord(
    webhook_env: str,
    embed: dict,
    enriched_messages: List[str],
) -> None:
    """Post digest embed and enriched follow-ups to Discord webhook.

    Args:
        webhook_env: Environment variable name containing the webhook URL
        embed: Discord embed dict for the main digest
        enriched_messages: List of formatted strings for follow-up messages
    """
    webhook_url = os.environ.get(webhook_env)
    if not webhook_url:
        raise ValueError(f"Missing environment variable: {webhook_env}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Post main embed
        resp = await client.post(
            webhook_url,
            json={"embeds": [embed]},
        )
        resp.raise_for_status()

        # Post enriched items as follow-up messages
        for msg in enriched_messages:
            resp = await client.post(
                webhook_url,
                json={"content": msg},
            )
            resp.raise_for_status()
