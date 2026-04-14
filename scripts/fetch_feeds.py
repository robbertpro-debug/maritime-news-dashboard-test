#!/usr/bin/env python3

import hashlib
import html
import json
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
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
    topics = []
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


def build_article_id(url: str, title: str, source_id: str) -> str:
    basis = url or f"{source_id}:{title}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def parse_entry(entry: ET.Element, source: Dict, rules: Dict) -> Optional[Dict]:
    namespaces = {
        "content": "http://purl.org/rss/1.0/modules/content/",
        "atom": "http://www.w3.org/2005/Atom",
    }
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
    audience = classify_article(combined_text, rules, source)
    tags = detect_topics(combined_text, rules["topic_keywords"], source.get("default_tags", []))
    paywalled = detect_paywall(combined_text, rules["paywall_keywords"])

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
        "titleKey": normalize_title(title),
    }


def fetch_feed(url: str, timeout: int, user_agent: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.read()


def parse_feed(payload: bytes, source: Dict, rules: Dict, max_items: int) -> List[Dict]:
    root = ET.fromstring(payload)
    entries = root.findall(".//item")
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    articles = []
    for entry in entries[:max_items]:
        parsed = parse_entry(entry, source, rules)
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
    def sort_key(article: Dict) -> Tuple[int, str]:
        published = article.get("publishedAt")
        if published:
            return (1, published)
        return (0, "")

    return sorted(articles, key=sort_key, reverse=True)


def build_output(articles: List[Dict], config: Dict, errors: List[Dict]) -> Dict:
    trimmed_articles = [article for article in articles if article["audience"] != "Irrelevant"]
    trimmed_articles = trimmed_articles[: config["output"]["max_total_items"]]
    topics = sorted({tag for article in trimmed_articles for tag in article["tags"]})
    sources = sorted({article["source"] for article in trimmed_articles})

    clean_articles = []
    for article in trimmed_articles:
        clean_articles.append(
            {
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
            }
        )

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "articleCount": len(clean_articles),
        "sourceCount": len(config["sources"]),
        "sources": sources,
        "topics": topics,
        "errors": errors,
        "articles": clean_articles,
    }


def write_output(output: Dict, config: Dict) -> None:
    json_path = ROOT / config["output"]["json_path"]
    js_path = ROOT / config["output"]["js_path"]
    json_path.parent.mkdir(parents=True, exist_ok=True)

    serialized = json.dumps(output, ensure_ascii=True, indent=2)
    json_path.write_text(serialized + "\n", encoding="utf-8")
    js_path.write_text("window.__DASHBOARD_DATA__ = " + serialized + ";\n", encoding="utf-8")


def main() -> int:
    config = load_config()
    timeout = config["request"]["timeout_seconds"]
    user_agent = config["request"]["user_agent"]
    max_items = config["output"]["max_items_per_source"]
    rules = config["classification"]

    articles: List[Dict] = []
    errors: List[Dict] = []

    for source in config["sources"]:
        try:
            payload = fetch_feed(source["url"], timeout=timeout, user_agent=user_agent)
            parsed_articles = parse_feed(payload, source, rules, max_items)
            articles.extend(parsed_articles)
            print(f"[ok] {source['name']}: {len(parsed_articles)} items")
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": source["name"], "message": str(exc)})
            print(f"[error] {source['name']}: {exc}", file=sys.stderr)

    deduped = dedupe_articles(sort_articles(articles))
    output = build_output(deduped, config, errors)
    write_output(output, config)

    print(f"[done] wrote {output['articleCount']} articles to {config['output']['json_path']}")
    if errors:
        print(f"[done] {len(errors)} source errors recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
