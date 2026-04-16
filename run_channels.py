#!/usr/bin/env python3
"""Run Horizon pipeline for all configured channels and post to Discord."""

import json
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from src.models import (
    Config, AIConfig, AIProvider, SourcesConfig,
    RSSSourceConfig, RedditConfig, RedditSubredditConfig,
    GitHubSourceConfig, HackerNewsConfig, TelegramConfig,
    FilteringConfig,
)
from src.storage.manager import StorageManager
from src.orchestrator import HorizonOrchestrator
from src.output.discord import (
    format_digest_embed,
    format_enriched_message,
    post_to_discord,
)

console = Console()


def load_channel_config(path: Path) -> dict:
    """Load and return a channel JSON config."""
    with open(path) as f:
        return json.load(f)


def build_horizon_config(channel: dict) -> Config:
    """Translate a channel config dict into a Horizon Config object."""
    ai_section = channel["ai"]
    ai_config = AIConfig(
        provider=AIProvider(ai_section["provider"]),
        model=ai_section["model"],
        api_key_env=ai_section["api_key_env"],
        base_url=ai_section.get("base_url"),
        temperature=ai_section.get("temperature", 0.3),
        max_tokens=ai_section.get("max_tokens", 4096),
        languages=ai_section.get("languages", ["en"]),
    )

    sources = channel.get("sources", {})

    # RSS feeds
    rss_configs = []
    for feed in sources.get("rss", []):
        rss_configs.append(RSSSourceConfig(
            name=feed["name"],
            url=feed["url"],
            enabled=feed.get("enabled", True),
            category=feed.get("category"),
        ))

    # Reddit
    reddit_section = sources.get("reddit", {})
    subreddit_configs = []
    for sub in reddit_section.get("subreddits", []):
        subreddit_configs.append(RedditSubredditConfig(
            subreddit=sub["subreddit"],
            sort=sub.get("sort", "hot"),
            fetch_limit=sub.get("fetch_limit", 25),
            min_score=sub.get("min_score", 10),
            enabled=sub.get("enabled", True),
        ))
    reddit_config = RedditConfig(
        enabled=reddit_section.get("enabled", bool(subreddit_configs)),
        subreddits=subreddit_configs,
        fetch_comments=reddit_section.get("fetch_comments", 5),
    )

    # GitHub
    github_configs = []
    for gh in sources.get("github", []):
        github_configs.append(GitHubSourceConfig(
            type=gh["type"],
            owner=gh.get("owner"),
            repo=gh.get("repo"),
            username=gh.get("username"),
            enabled=gh.get("enabled", True),
        ))

    # HackerNews
    hn_section = sources.get("hackernews", {})
    hn_config = HackerNewsConfig(
        enabled=hn_section.get("enabled", False),
        fetch_top_stories=hn_section.get("fetch_top_stories", 30),
        min_score=hn_section.get("min_score", 100),
    )

    # Filtering
    filt = channel.get("filtering", {})
    filtering_config = FilteringConfig(
        ai_score_threshold=filt.get("ai_score_threshold", 7.0),
        time_window_hours=filt.get("time_window_hours", 24),
    )

    return Config(
        ai=ai_config,
        sources=SourcesConfig(
            github=github_configs,
            hackernews=hn_config,
            rss=rss_configs,
            reddit=reddit_config,
            telegram=TelegramConfig(enabled=False),
        ),
        filtering=filtering_config,
    )


def count_sources(channel: dict) -> dict:
    """Count sources by type for the footer."""
    sources = channel.get("sources", {})
    counts = {}
    rss_list = [f for f in sources.get("rss", []) if f.get("enabled", True)]
    if rss_list:
        counts["rss"] = len(rss_list)
    subs = [s for s in sources.get("reddit", {}).get("subreddits", []) if s.get("enabled", True)]
    if subs:
        counts["reddit"] = len(subs)
    gh = [g for g in sources.get("github", []) if g.get("enabled", True)]
    if gh:
        counts["github"] = len(gh)
    return counts


async def run_channel(channel: dict, dry_run: bool = False) -> None:
    """Run the pipeline for a single channel and post to Discord."""
    channel_name = channel["channel_name"]
    console.print(f"\n{'='*60}")
    console.print(f"[bold]Processing channel: {channel_name}[/bold]")
    console.print(f"{'='*60}\n")

    config = build_horizon_config(channel)
    storage = StorageManager(data_dir="data")
    orchestrator = HorizonOrchestrator(config, storage)

    scoring_prompt = channel.get("scoring_prompt")
    max_items = channel.get("filtering", {}).get("max_items")
    enrich_threshold = channel.get("output", {}).get("enrich_threshold", 8.0)

    items, total_fetched = await orchestrator.run_pipeline(
        scoring_prompt=scoring_prompt,
        max_items=max_items,
    )

    source_counts = count_sources(channel)
    embed = format_digest_embed(channel_name, items, total_fetched, source_counts)

    # Build enriched follow-up messages for high-scoring items
    enriched_messages = []
    for item in items:
        if (item.ai_score or 0) >= enrich_threshold:
            msg = format_enriched_message(item)
            if msg:
                enriched_messages.append(msg)

    if dry_run:
        console.print("\n[bold yellow]--- DRY RUN: Discord embed ---[/bold yellow]")
        console.print(f"Title: {embed['title']}")
        console.print(f"Description ({len(embed['description'])} chars):")
        console.print(embed["description"][:1000])
        if len(embed["description"]) > 1000:
            console.print("... (truncated)")
        console.print(f"Footer: {embed['footer']['text']}")
        console.print(f"\n[bold yellow]Enriched follow-ups: {len(enriched_messages)}[/bold yellow]")
        for i, msg in enumerate(enriched_messages):
            console.print(f"\n--- Follow-up {i+1} ---")
            console.print(msg[:500])
        return

    webhook_env = channel["discord_webhook_url_env"]
    await post_to_discord(webhook_env, embed, enriched_messages)
    console.print(f"[bold green]✅ Posted to Discord: {channel_name}[/bold green]")


async def run_all_channels(dry_run: bool = False) -> None:
    """Run the pipeline for all channel configs."""
    channel_dir = Path("channels")
    if not channel_dir.exists():
        console.print("[bold red]No channels/ directory found.[/bold red]")
        sys.exit(1)

    channel_files = sorted(channel_dir.glob("*.json"))
    channel_files = [f for f in channel_files if "_template" not in f.name]

    if not channel_files:
        console.print("[bold red]No channel configs found in channels/[/bold red]")
        sys.exit(1)

    console.print(f"[bold cyan]Found {len(channel_files)} channel(s) to process[/bold cyan]")

    for cf in channel_files:
        try:
            channel = load_channel_config(cf)
            await run_channel(channel, dry_run=dry_run)
        except Exception as e:
            console.print(f"[bold red]❌ Error processing {cf.name}: {e}[/bold red]")
            import traceback
            traceback.print_exc()


def main():
    load_dotenv()
    dry_run = "--dry-run" in sys.argv
    asyncio.run(run_all_channels(dry_run=dry_run))


if __name__ == "__main__":
    main()
