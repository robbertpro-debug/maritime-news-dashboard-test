#!/usr/bin/env python3

import hashlib
import html
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "dashboard_config.json"
STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
WORD_RE = re.compile(r"[a-z0-9]+")
LOOKUP_RE = re.compile(r"[^a-z0-9]+")
CATEGORY_LABELS = {
    "suppliers": "Supplier",
    "clients": "Client",
    "competitors": "Competitor",
}


def load_config() -> Dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def strip_html(value: str) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = HTML_TAG_RE.sub(" ", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text


def truncate(value: str, limit: int = 240) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    return url.strip()


def normalize_title(value: str) -> str:
    lowered = strip_html(value).lower()
    words = [word for word in WORD_RE.findall(lowered) if word not in STOPWORDS]
    return " ".join(words[:12])


def normalize_lookup(value: str) -> str:
    cleaned = strip_html(value)
    if not cleaned:
        return ""
    normalized = unicodedata.normalize("NFKD", cleaned)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    folded = LOOKUP_RE.sub(" ", ascii_text.lower())
    folded = WHITESPACE_RE.sub(" ", folded).strip()
    if not folded:
        return ""
    return f" {folded} "


def lookup_contains(normalized_text: str, phrase: str) -> bool:
    normalized_phrase = normalize_lookup(phrase).strip()
    if not normalized_text or not normalized_phrase:
        return False
    return f" {normalized_phrase} " in normalized_text


def parse_datetime(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def score_keywords(text: str, keywords: List[str]) -> int:
    lowered = text.lower()
    return sum(1 for keyword in keywords if keyword.lower() in lowered)


def normalize_tag(tag: str) -> str:
    cleaned = strip_html(tag).strip()
    if not cleaned:
        return ""
    if cleaned.islower():
        return cleaned.title()
    return cleaned


def append_unique(items: List[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def classify_article(text: str, rules: Dict, source: Dict) -> str:
    multraship_score = score_keywords(text, rules["multraship_keywords"])
    novatug_score = score_keywords(text, rules["novatug_keywords"])
    multraship_noise = score_keywords(text, rules.get("multraship_exclude_keywords", []))
    novatug_noise = score_keywords(text, rules.get("novatug_exclude_keywords", []))
    multraship_score = max(0, multraship_score - multraship_noise)
    novatug_score = max(0, novatug_score - novatug_noise)
    audience_bias = source.get("audience_bias", {})
    multraship_score += int(audience_bias.get("Multraship", 0))
    novatug_score += int(audience_bias.get("Novatug", 0))
    shared_threshold = rules.get("shared_threshold", 2)
    both_difference_max = rules.get("both_difference_max", 1)
    multraship_min_score = rules.get("multraship_min_score", 1)
    novatug_min_score = rules.get("novatug_min_score", 1)

    if multraship_score < multraship_min_score and novatug_score < novatug_min_score:
        return "Irrelevant"
    if (
        multraship_score >= shared_threshold
        and novatug_score >= shared_threshold
        and abs(multraship_score - novatug_score) <= both_difference_max
    ):
        return "Both"
    if multraship_score >= multraship_min_score and multraship_score > novatug_score:
        return "Multraship"
    if novatug_score >= novatug_min_score and novatug_score > multraship_score:
        return "Novatug"
    if multraship_score >= multraship_min_score and novatug_score >= novatug_min_score:
        return "Both"
    return "Irrelevant"


def detect_topics(text: str, topic_rules: Dict[str, List[str]], default_tags: List[str]) -> List[str]:
    lowered = text.lower()
    topics: List[str] = []
    seen = set()
    for topic, keywords in topic_rules.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            normalized = normalize_tag(topic)
            key = normalized.lower()
            if normalized and key not in seen:
                seen.add(key)
                topics.append(normalized)
    for tag in default_tags:
        normalized = normalize_tag(tag)
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            topics.append(normalized)
    return topics


def detect_paywall(text: str, keywords: List[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def match_entities(normalized_text: str, entities: Dict[str, List[str]], aliases: Dict[str, Dict[str, List[str]]]) -> List[Dict]:
    matches = []
    seen = set()
    for category, names in entities.items():
        alias_map = aliases.get(category, {})
        for name in names:
            search_terms = [name] + alias_map.get(name, [])
            if any(lookup_contains(normalized_text, term) for term in search_terms):
                key = (category, name)
                if key in seen:
                    continue
                seen.add(key)
                matches.append({"name": name, "category": category})
    return matches


def detect_locations(normalized_text: str, location_watchlist: List[Dict]) -> List[Dict]:
    matches = []
    seen = set()
    for location in location_watchlist:
        search_terms = [location["name"]] + location.get("aliases", [])
        if any(lookup_contains(normalized_text, term) for term in search_terms):
            if location["name"] in seen:
                continue
            seen.add(location["name"])
            matches.append({"name": location["name"], "type": location.get("type", "region")})
    return matches


def detect_signal_groups(normalized_text: str, groups: Dict[str, Dict]) -> List[Dict]:
    matches = []
    for slug, group in groups.items():
        keywords = group.get("keywords", [])
        if any(lookup_contains(normalized_text, keyword) for keyword in keywords):
            matches.append({"slug": slug, "label": group.get("label", normalize_tag(slug))})
    return matches


def build_business_tags(locations: List[Dict], entities: List[Dict], signals: List[Dict]) -> List[str]:
    tags: List[str] = []

    for location in locations:
        prefix = "Port" if location["type"] != "region" else "Region"
        append_unique(tags, f"{prefix}: {location['name']}")

    for entity in entities:
        append_unique(tags, CATEGORY_LABELS.get(entity["category"], normalize_tag(entity["category"])))

    for signal in signals:
        append_unique(tags, signal["label"])

    return tags


def build_priority_reasons(locations: List[Dict], entities: List[Dict], signals: List[Dict]) -> List[str]:
    reasons: List[str] = []

    for location in locations:
        if location["type"] == "region":
            append_unique(reasons, f"Regional watch: {location['name']}")
        else:
            append_unique(reasons, f"Core port watch: {location['name']}")

    for entity in entities:
        label = CATEGORY_LABELS.get(entity["category"], normalize_tag(entity["category"]))
        append_unique(reasons, f"{label} mention: {entity['name']}")

    for signal in signals:
        append_unique(reasons, f"Signal: {signal['label']}")

    return reasons[:6]


def compute_priority_score(
    audience: str,
    locations: List[Dict],
    entities: List[Dict],
    signals: List[Dict],
    weights: Dict[str, int],
) -> int:
    score = 0

    if audience == "Multraship":
        score += int(weights.get("audience_multraship", 0))
    elif audience == "Both":
        score += int(weights.get("audience_both", 0))

    for location in locations:
        if location["type"] == "region":
            score += int(weights.get("location_region", 0))
        else:
            score += int(weights.get("location_core_port", 0))

    for entity in entities:
        if entity["category"] == "clients":
            score += int(weights.get("client", 0))
        elif entity["category"] == "competitors":
            score += int(weights.get("competitor", 0))
        elif entity["category"] == "suppliers":
            score += int(weights.get("supplier", 0))

    for signal in signals:
        score += int(weights.get(signal["slug"], 0))

    return score


def classify_priority_band(score: int, bands: Dict[str, int]) -> str:
    if score >= int(bands.get("critical", 999)):
        return "critical"
    if score >= int(bands.get("high", 999)):
        return "high"
    if score >= int(bands.get("medium", 999)):
        return "medium"
    return "low"


def assign_board_bucket(priority_band: str, locations: List[Dict], entities: List[Dict], signals: List[Dict]) -> str:
    categories = {entity["category"] for entity in entities}
    signal_slugs = {signal["slug"] for signal in signals}

    if priority_band in {"critical", "high"}:
        return "High Priority"
    if locations:
        return "Port Watch"
    if "clients" in categories:
        return "Clients & Projects"
    if "competitors" in categories:
        return "Competitors & Market"
    if signal_slugs.intersection({"incident", "disruption", "regulation", "infrastructure", "terminal_expansion"}):
        return "Regulation, Safety & Incidents"
    return "Other Relevant"


def build_article_id(url: str, title: str, source_id: str) -> str:
    basis = url or f"{source_id}:{title}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def parse_entry(entry: ET.Element, source: Dict, config: Dict) -> Optional[Dict]:
    namespaces = {
        "content": "http://purl.org/rss/1.0/modules/content/",
        "atom": "http://www.w3.org/2005/Atom",
    }
    rules = config["classification"]
    watchlists = config.get("watchlists", {})
    priority_rules = config.get("priority_rules", {})
    title = (
        entry.findtext("title")
        or entry.findtext("{http://www.w3.org/2005/Atom}title")
        or ""
    ).strip()
    if not title:
        return None

    link = entry.findtext("link") or ""
    if not link:
        atom_link = entry.find("atom:link[@rel='alternate']", namespaces)
        if atom_link is not None:
            link = atom_link.attrib.get("href", "")
    link = canonicalize_url(link)

    summary = (
        entry.findtext("description")
        or entry.findtext("content:encoded", namespaces=namespaces)
        or entry.findtext("{http://www.w3.org/2005/Atom}summary")
        or entry.findtext("{http://www.w3.org/2005/Atom}content")
        or ""
    )
    excerpt = truncate(strip_html(summary))
    published_at = parse_datetime(
        entry.findtext("pubDate")
        or entry.findtext("published")
        or entry.findtext("updated")
        or entry.findtext("{http://www.w3.org/2005/Atom}published")
        or entry.findtext("{http://www.w3.org/2005/Atom}updated")
    )

    combined_text = " ".join(part for part in (title, excerpt) if part)
    normalized_text = normalize_lookup(combined_text)
    audience = classify_article(combined_text, rules, source)
    tags = detect_topics(combined_text, rules["topic_keywords"], source.get("default_tags", []))
    paywalled = detect_paywall(combined_text, rules["paywall_keywords"])
    entities = match_entities(
        normalized_text,
        rules.get("entities", {}),
        watchlists.get("entity_aliases", {}),
    )
    locations = detect_locations(normalized_text, watchlists.get("locations", []))
    signals = detect_signal_groups(normalized_text, priority_rules.get("keyword_groups", {}))
    business_tags = build_business_tags(locations, entities, signals)
    priority_score = compute_priority_score(
        audience,
        locations,
        entities,
        signals,
        priority_rules.get("weights", {}),
    )
    priority_band = classify_priority_band(priority_score, priority_rules.get("bands", {}))
    priority_reasons = build_priority_reasons(locations, entities, signals)
    board_bucket = assign_board_bucket(priority_band, locations, entities, signals)

    return {
        "id": build_article_id(link, title, source["id"]),
        "title": strip_html(title),
        "source": source["name"],
        "sourceId": source["id"],
        "url": link,
        "publishedAt": published_at,
        "excerpt": excerpt,
        "tags": tags,
        "audience": audience,
        "paywalled": paywalled,
        "entities": entities,
        "locations": locations,
        "businessTags": business_tags,
        "priorityScore": priority_score,
        "priorityBand": priority_band,
        "priorityReasons": priority_reasons,
        "boardBucket": board_bucket,
        "titleKey": normalize_title(title),
    }


def fetch_feed(url: str, timeout: int, user_agent: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.read()


def parse_feed(payload: bytes, source: Dict, config: Dict, max_items: int) -> List[Dict]:
    root = ET.fromstring(payload)
    entries = root.findall(".//item")
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    articles = []
    for entry in entries[:max_items]:
        parsed = parse_entry(entry, source, config)
        if parsed:
            articles.append(parsed)
    return articles


def dedupe_articles(articles: List[Dict]) -> List[Dict]:
    kept: List[Dict] = []
    seen_urls = set()
    seen_titles = set()

    for article in articles:
        url = article["url"]
        title_key = article["titleKey"]
        if url and url in seen_urls:
            continue
        if title_key and title_key in seen_titles:
            continue
        kept.append(article)
        if url:
            seen_urls.add(url)
        if title_key:
            seen_titles.add(title_key)
    return kept


def sort_articles(articles: List[Dict]) -> List[Dict]:
    def sort_key(article: Dict) -> Tuple[int, int, str]:
        published = article.get("publishedAt")
        if published:
            return (int(article.get("priorityScore", 0)), 1, published)
        return (int(article.get("priorityScore", 0)), 0, "")

    return sorted(articles, key=sort_key, reverse=True)


def filter_recent_articles(articles: List[Dict], lookback_days: int, now: datetime) -> List[Dict]:
    if lookback_days <= 0:
        return articles

    cutoff = now - timedelta(days=lookback_days)
    filtered = []
    for article in articles:
        published_at = parse_iso_datetime(article.get("publishedAt"))
        if published_at and published_at >= cutoff:
            filtered.append(article)
    return filtered


def to_clean_article(article: Dict) -> Dict:
    return {
        "id": article["id"],
        "title": article["title"],
        "source": article["source"],
        "sourceId": article["sourceId"],
        "url": article["url"],
        "publishedAt": article["publishedAt"],
        "excerpt": article["excerpt"],
        "tags": article["tags"],
        "audience": article["audience"],
        "paywalled": article["paywalled"],
        "entities": article.get("entities", []),
        "locations": article.get("locations", []),
        "businessTags": article.get("businessTags", []),
        "priorityScore": article.get("priorityScore", 0),
        "priorityBand": article.get("priorityBand", "low"),
        "priorityReasons": article.get("priorityReasons", []),
        "boardBucket": article.get("boardBucket", "Other Relevant"),
    }


def load_history(history_path: Path) -> List[Dict]:
    if history_path.exists():
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            return data.get("articles", [])
        except (json.JSONDecodeError, KeyError):
            return []
    return []


def merge_with_history(new_articles: List[Dict], existing_history: List[Dict]) -> List[Dict]:
    seen_ids = {article["id"] for article in new_articles}
    merged = list(new_articles)
    for article in existing_history:
        if article["id"] not in seen_ids:
            seen_ids.add(article["id"])
            merged.append(article)
    return merged


def prune_history(articles: List[Dict], lookback_days: int, now: datetime) -> List[Dict]:
    if lookback_days <= 0:
        return articles
    cutoff = now - timedelta(days=lookback_days)
    return [
        article for article in articles
        if parse_iso_datetime(article.get("publishedAt")) is not None
        and parse_iso_datetime(article["publishedAt"]) >= cutoff
    ]


def write_history(articles: List[Dict], history_path: Path, generated_at: datetime) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
        "articleCount": len(articles),
        "articles": articles,
    }
    history_path.write_text(json.dumps(output, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def build_output(articles: List[Dict], config: Dict, errors: List[Dict], generated_at: datetime) -> Dict:
    lookback_days = int(config["output"].get("lookback_days", 0))
    cutoff_at = None
    if lookback_days > 0:
        cutoff_at = (generated_at - timedelta(days=lookback_days)).isoformat().replace("+00:00", "Z")

    trimmed_articles = [article for article in articles if article["audience"] != "Irrelevant"]
    trimmed_articles = trimmed_articles[: config["output"]["max_total_items"]]
    topics = sorted({tag for article in trimmed_articles for tag in article["tags"]})
    sources = sorted({article["source"] for article in trimmed_articles})
    locations = sorted(
        {
            location["name"]
            for article in trimmed_articles
            for location in article.get("locations", [])
        }
    )

    clean_articles = [to_clean_article(article) for article in trimmed_articles]

    return {
        "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
        "lookbackDays": lookback_days,
        "cutoffAt": cutoff_at,
        "articleCount": len(clean_articles),
        "sourceCount": len(config["sources"]),
        "sources": sources,
        "topics": topics,
        "locations": locations,
        "errors": errors,
        "articles": clean_articles,
        "entityProfiles": config.get("entity_profiles", {}),
        "entityCategories": {
            name: category
            for category, names in config.get("classification", {}).get("entities", {}).items()
            for name in names
        },
    }


def write_output(output: Dict, config: Dict) -> None:
    json_path = ROOT / config["output"]["json_path"]
    js_path = ROOT / config["output"]["js_path"]
    json_path.parent.mkdir(parents=True, exist_ok=True)

    serialized = json.dumps(output, ensure_ascii=True, indent=2)
    json_path.write_text(serialized + "\n", encoding="utf-8")
    js_path.write_text("window.__DASHBOARD_DATA__ = " + serialized + ";\n", encoding="utf-8")


def fetch_stocks(profiles: Dict, generated_at: datetime) -> Dict:
    try:
        import yfinance as yf  # optional dependency
    except ImportError:
        print("[stocks] yfinance not installed, skipping", file=sys.stderr)
        return {}

    results: Dict = {}
    for entity, profile in profiles.items():
        stock = profile.get("stock")
        if not stock or not stock.get("ticker"):
            continue
        ticker = stock["ticker"]
        try:
            info = yf.Ticker(ticker).fast_info
            price = getattr(info, "last_price", None)
            prev = getattr(info, "previous_close", None)
            currency = getattr(info, "currency", None)
            change = round(price - prev, 4) if price and prev else None
            change_pct = round((price - prev) / prev * 100, 2) if price and prev else None
            results[entity] = {
                "ticker": ticker,
                "exchange": stock.get("exchange", ""),
                "note": stock.get("note"),
                "price": round(price, 4) if price else None,
                "currency": currency,
                "change": change,
                "changePct": change_pct,
                "updatedAt": generated_at.isoformat().replace("+00:00", "Z"),
            }
            direction = "▲" if (change or 0) >= 0 else "▼"
            print(f"[stock] {ticker}: {currency} {price:.2f} {direction}{abs(change_pct or 0):.2f}%")
        except Exception as exc:  # noqa: BLE001
            print(f"[stock] {ticker}: {exc}", file=sys.stderr)
    return results


def write_stocks(stocks: Dict, config: Dict, generated_at: datetime) -> None:
    stocks_path = ROOT / config["output"].get("stocks_json_path", "data/stocks.json")
    stocks_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
        "stocks": stocks,
    }
    stocks_path.write_text(json.dumps(output, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    config = load_config()
    timeout = config["request"]["timeout_seconds"]
    user_agent = config["request"]["user_agent"]
    max_items = config["output"]["max_items_per_source"]
    lookback_days = int(config["output"].get("lookback_days", 0))
    generated_at = datetime.now(timezone.utc)

    articles: List[Dict] = []
    errors: List[Dict] = []

    for source in config["sources"]:
        try:
            payload = fetch_feed(source["url"], timeout, user_agent)
            parsed_articles = parse_feed(payload, source, config, max_items)
            articles.extend(parsed_articles)
            print(f"[ok] {source['name']}: {len(parsed_articles)} items")
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": source["name"], "message": str(exc)})
            print(f"[error] {source['name']}: {exc}", file=sys.stderr)

    deduped = dedupe_articles(sort_articles(articles))
    recent_articles = filter_recent_articles(deduped, lookback_days, generated_at)
    output = build_output(recent_articles, config, errors, generated_at)
    write_output(output, config)

    print(f"[done] wrote {output['articleCount']} articles to {config['output']['json_path']}")

    # History accumulation: merge new relevant articles with existing history, prune to 180 days
    history_path = ROOT / config["output"].get("history_json_path", "data/articles-history.json")
    history_lookback_days = int(config["output"].get("history_lookback_days", 180))
    new_clean = [to_clean_article(a) for a in deduped if a["audience"] != "Irrelevant"]
    existing_history = load_history(history_path)
    merged = merge_with_history(new_clean, existing_history)
    pruned = prune_history(merged, history_lookback_days, generated_at)
    write_history(pruned, history_path, generated_at)

    print(f"[done] wrote {len(pruned)} articles to history ({history_path.name})")
    if errors:
        print(f"[done] {len(errors)} source errors recorded")

    # Stock quotes
    profiles = config.get("entity_profiles", {})
    if profiles:
        stocks = fetch_stocks(profiles, generated_at)
        if stocks:
            write_stocks(stocks, config, generated_at)
            print(f"[done] wrote {len(stocks)} stock quotes to data/stocks.json")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
