#!/usr/bin/env python3
"""
One-off backscrape: queries Google News RSS for each tracked entity with a
date filter and merges results into the history file.

Usage:
    python3 scripts/backscrape_entities.py
    python3 scripts/backscrape_entities.py --after 2026-01-01
"""

import sys
import time
import argparse
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_feeds import (
    load_config,
    fetch_feed,
    parse_feed,
    to_clean_article,
    load_history,
    merge_with_history,
    prune_history,
    cap_history,
    write_history,
    dedupe_articles,
    sort_articles,
)

ROOT = Path(__file__).resolve().parent.parent
GNEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
DELAY = 1.5  # seconds between requests
MAX_ITEMS = 50


def google_news_url(name: str, after: str) -> str:
    query = urllib.parse.quote(f'"{name}" after:{after}')
    return GNEWS_RSS.format(query=query)


def audience_bias_for(category: str) -> dict:
    return {
        "suppliers": {"Multraship": 3},
        "clients": {"Multraship": 4},
        "competitors": {"Multraship": 4},
    }.get(category, {"Multraship": 2})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--after",
        default=(datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d"),
        help="Only fetch articles published after this date (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    config = load_config()
    timeout = config["request"]["timeout_seconds"]
    user_agent = config["request"]["user_agent"]
    entities = config["classification"].get("entities", {})

    searches = [
        (category, name)
        for category, names in entities.items()
        for name in names
    ]

    print(f"Backscraping {len(searches)} entities via Google News (after {args.after}) ...")
    print()

    all_articles = []
    error_count = 0

    for i, (category, name) in enumerate(searches, 1):
        url = google_news_url(name, args.after)
        source = {
            "id": f"gnews-{name[:30].lower().replace(' ', '-')}",
            "name": f"{name} (Google News)",
            "url": url,
            "default_tags": [category.rstrip("s")],  # "supplier", "client", "competitor"
            "audience_bias": audience_bias_for(category),
        }
        try:
            payload = fetch_feed(url, timeout, user_agent)
            articles = parse_feed(payload, source, config, MAX_ITEMS)
            all_articles.extend(articles)
            print(f"[{i:3}/{len(searches)}] {name}: {len(articles)} items")
        except Exception as exc:
            error_count += 1
            print(f"[{i:3}/{len(searches)}] {name}: ERROR — {exc}", file=sys.stderr)

        if i < len(searches):
            time.sleep(DELAY)

    print()
    print(f"Fetched {len(all_articles)} raw articles ({error_count} errors)")

    deduped = dedupe_articles(sort_articles(all_articles))
    relevant = [a for a in deduped if a["audience"] != "Irrelevant"]
    print(f"After dedup + relevance filter: {len(relevant)} articles")

    generated_at = datetime.now(timezone.utc)
    history_path = ROOT / config["output"].get("history_json_path", "data/articles-history.json")
    history_lookback_days = int(config["output"].get("history_lookback_days", 180))
    history_max_items = int(config["output"].get("history_max_items", 0))

    clean = [to_clean_article(a) for a in relevant]
    existing = load_history(history_path)
    merged = merge_with_history(clean, existing)
    pruned = prune_history(merged, history_lookback_days, generated_at)
    pruned = cap_history(pruned, history_max_items)
    write_history(pruned, history_path, generated_at)

    added = len(pruned) - len(existing)
    print(f"History: {len(existing)} → {len(pruned)} articles (+{added} new)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
