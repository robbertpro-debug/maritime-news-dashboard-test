#!/usr/bin/env python3

import hashlib
import html
import json
import re
import ssl
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
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
    "authorities": "Authority & Class",
}
TRACKING_QUERY_PARAMS = {
    "fbclid",
    "gad_source",
    "gclid",
    "mc_cid",
    "mc_eid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
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
    text = html.unescape(url.strip())
    parsed = urllib.parse.urlparse(text)
    query = urllib.parse.parse_qs(parsed.query)
    for wrapper_key in ("url", "u"):
        wrapped = query.get(wrapper_key, [""])[0]
        if wrapped.startswith(("http://", "https://")):
            return canonicalize_url(wrapped)

    clean_query = [
        (key, value)
        for key, values in query.items()
        if key.lower() not in TRACKING_QUERY_PARAMS
        for value in values
    ]
    return urllib.parse.urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or parsed.path,
            "",
            urllib.parse.urlencode(clean_query),
            "",
        )
    )


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


def lookup_near_context(
    normalized_text: str,
    phrase: str,
    context_token_sets: List[List[str]],
    window: int = 4,
) -> bool:
    phrase_tokens = normalize_lookup(phrase).strip().split()
    tokens = normalized_text.split()
    if not tokens or not phrase_tokens or not context_token_sets:
        return False

    phrase_length = len(phrase_tokens)
    for index in range(len(tokens) - phrase_length + 1):
        if tokens[index : index + phrase_length] != phrase_tokens:
            continue
        start = max(0, index - window)
        end = min(len(tokens), index + phrase_length + window)
        nearby_tokens = tokens[start:end]
        for context_tokens in context_token_sets:
            context_length = len(context_tokens)
            for context_index in range(len(nearby_tokens) - context_length + 1):
                if nearby_tokens[context_index : context_index + context_length] == context_tokens:
                    return True
    return False


def parse_datetime(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        pass
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


def article_matches_source(text: str, source: Dict) -> bool:
    includes = source.get("article_include_patterns", [])
    excludes = source.get("article_exclude_patterns", [])
    if includes and not any(re.search(pattern, text, re.IGNORECASE) for pattern in includes):
        return False
    if excludes and any(re.search(pattern, text, re.IGNORECASE) for pattern in excludes):
        return False
    return True


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


def detect_locations(
    normalized_text: str,
    location_watchlist: List[Dict],
    context_keywords: Optional[List[str]] = None,
) -> List[Dict]:
    matches = []
    seen = set()
    context_token_sets = [
        tokens
        for tokens in (normalize_lookup(keyword).strip().split() for keyword in (context_keywords or []))
        if tokens
    ]
    for location in location_watchlist:
        strong_terms = location.get("aliases", [location["name"]])
        context_terms = location.get("context_aliases", [])
        has_strong_match = any(lookup_contains(normalized_text, term) for term in strong_terms)
        has_context_match = any(
            lookup_near_context(normalized_text, term, context_token_sets)
            for term in context_terms
        )
        if has_strong_match or has_context_match:
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
        elif entity["category"] == "authorities":
            score += int(weights.get("authority", 0))

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
    if "authorities" in categories:
        return "Regulation, Safety & Incidents"
    if signal_slugs.intersection({"incident", "disruption", "regulation", "infrastructure", "terminal_expansion"}):
        return "Regulation, Safety & Incidents"
    return "Other Relevant"


def build_article_id(url: str, title: str, source_id: str) -> str:
    basis = url or f"{source_id}:{title}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def build_article(
    title: str,
    link: str,
    excerpt: str,
    published_at: Optional[str],
    source: Dict,
    config: Dict,
) -> Optional[Dict]:
    rules = config["classification"]
    watchlists = config.get("watchlists", {})
    priority_rules = config.get("priority_rules", {})
    clean_title = strip_html(title)
    if not clean_title:
        return None

    clean_link = canonicalize_url(link)
    clean_excerpt = truncate(strip_html(excerpt))
    article_text = " ".join(part for part in (clean_title, clean_excerpt) if part)
    if not article_matches_source(article_text, source):
        return None

    combined_text = " ".join(part for part in (article_text, source.get("name", "")) if part)
    normalized_article_text = normalize_lookup(article_text)
    audience = classify_article(combined_text, rules, source)
    tags = detect_topics(combined_text, rules["topic_keywords"], source.get("default_tags", []))
    paywalled = detect_paywall(combined_text, rules["paywall_keywords"])
    entities = match_entities(
        normalized_article_text,
        rules.get("entities", {}),
        watchlists.get("entity_aliases", {}),
    )
    locations = detect_locations(
        normalized_article_text,
        watchlists.get("locations", []),
        watchlists.get("location_context_keywords", []),
    )
    signals = detect_signal_groups(normalized_article_text, priority_rules.get("keyword_groups", {}))
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
        "id": build_article_id(clean_link, clean_title, source["id"]),
        "title": clean_title,
        "source": source["name"],
        "sourceId": source["id"],
        "url": clean_link,
        "publishedAt": published_at,
        "excerpt": clean_excerpt,
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
        "titleKey": normalize_title(clean_title),
    }


def parse_entry(entry: ET.Element, source: Dict, config: Dict) -> Optional[Dict]:
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
    published_at = parse_datetime(
        entry.findtext("pubDate")
        or entry.findtext("published")
        or entry.findtext("updated")
        or entry.findtext("{http://www.w3.org/2005/Atom}published")
        or entry.findtext("{http://www.w3.org/2005/Atom}updated")
    )

    return build_article(title, link, summary, published_at, source, config)


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


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[Dict[str, str]] = []
        self._href: Optional[str] = None
        self._text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        attrs_dict = {name.lower(): value for name, value in attrs}
        href = attrs_dict.get("href")
        if href:
            self._href = href
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        title = strip_html(" ".join(self._text_parts))
        self.links.append({"href": self._href, "title": title})
        self._href = None
        self._text_parts = []


def link_matches_source(link: Dict[str, str], source: Dict) -> bool:
    url = link["url"]
    title = link["title"]
    if url.rstrip("/") == canonicalize_url(source["url"]).rstrip("/"):
        return False
    if len(title) < int(source.get("min_title_length", 12)):
        return False

    haystack = f"{url} {title}"
    includes = source.get("link_include_patterns", [])
    excludes = source.get("link_exclude_patterns", [])
    if includes and not any(re.search(pattern, haystack, re.IGNORECASE) for pattern in includes):
        return False
    if excludes and any(re.search(pattern, haystack, re.IGNORECASE) for pattern in excludes):
        return False
    return True


def extract_links(payload: bytes, base_url: str) -> List[Dict[str, str]]:
    parser = LinkExtractor()
    parser.feed(payload.decode("utf-8", errors="replace"))
    links: List[Dict[str, str]] = []
    seen = set()
    for raw_link in parser.links:
        url = canonicalize_url(urllib.parse.urljoin(base_url, raw_link["href"]))
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        links.append({"url": url, "title": raw_link["title"]})
    return links


def parse_web_watch(
    payload: bytes,
    source: Dict,
    config: Dict,
    max_items: int,
    generated_at: datetime,
    state: Dict,
) -> List[Dict]:
    generated_at_iso = generated_at.isoformat().replace("+00:00", "Z")
    source_states = state.setdefault("sources", {})
    source_state = source_states.setdefault(source["id"], {})
    seen_urls = source_state.setdefault("seenUrls", {})
    payload_hash = hashlib.sha1(payload).hexdigest()
    articles: List[Dict] = []

    for link in extract_links(payload, source["url"]):
        if not link_matches_source(link, source):
            continue
        first_seen = seen_urls.setdefault(link["url"], generated_at_iso)
        article = build_article(
            link["title"],
            link["url"],
            "Tracked page link found on source page.",
            first_seen,
            source,
            config,
        )
        if article:
            articles.append(article)
        if len(articles) >= max_items:
            break

    if articles:
        source_state.pop("pageHash", None)
    elif source_state.get("pageHash") != payload_hash:
        change_url = f"{source['url']}?dashboardChange={payload_hash[:12]}"
        article = build_article(
            f"{source['name']} page updated",
            change_url,
            "Tracked page content changed.",
            generated_at_iso,
            source,
            config,
        )
        if article:
            articles.append(article)
        source_state["pageHash"] = payload_hash

    max_state_urls = int(source.get("max_state_urls", 200))
    if len(seen_urls) > max_state_urls:
        source_state["seenUrls"] = dict(
            sorted(seen_urls.items(), key=lambda item: item[1], reverse=True)[:max_state_urls]
        )
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


def load_source_state(state_path: Path) -> Dict:
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"sources": {}}


def write_source_state(state: Dict, state_path: Path) -> bool:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if state_path.exists() and state_path.read_text(encoding="utf-8") == serialized:
        return False
    state_path.write_text(serialized, encoding="utf-8")
    return True


def load_history(history_path: Path) -> List[Dict]:
    if history_path.exists():
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            return data.get("articles", [])
        except (json.JSONDecodeError, KeyError):
            return []
    return []


def history_keys(article: Dict) -> Tuple[str, str]:
    url = canonicalize_url(article.get("url", ""))
    title_key = normalize_title(article.get("title", ""))
    source_title_key = f"{article.get('sourceId', '')}:{title_key}" if title_key else ""
    return url, source_title_key


def merge_with_history(new_articles: List[Dict], existing_history: List[Dict]) -> List[Dict]:
    seen_ids = set()
    seen_urls = set()
    seen_source_titles = set()
    merged = []
    for article in list(new_articles) + list(existing_history):
        url, source_title = history_keys(article)
        article_id = article.get("id")
        if article_id and article_id in seen_ids:
            continue
        if url and url in seen_urls:
            continue
        if source_title and source_title in seen_source_titles:
            continue
        merged.append(article)
        if article_id:
            seen_ids.add(article_id)
        if url:
            seen_urls.add(url)
        if source_title:
            seen_source_titles.add(source_title)
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


def cap_history(articles: List[Dict], max_items: int) -> List[Dict]:
    if max_items <= 0 or len(articles) <= max_items:
        return articles

    def sort_key(article: Dict) -> Tuple[float, int]:
        published_at = parse_iso_datetime(article.get("publishedAt"))
        timestamp = published_at.timestamp() if published_at else 0
        return (timestamp, int(article.get("priorityScore", 0)))

    return sorted(articles, key=sort_key, reverse=True)[:max_items]


def write_history(articles: List[Dict], history_path: Path, generated_at: datetime) -> bool:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    if history_path.exists():
        try:
            existing = json.loads(history_path.read_text(encoding="utf-8"))
            if existing.get("articles", []) == articles:
                return False
        except json.JSONDecodeError:
            pass
    output = {
        "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
        "articleCount": len(articles),
        "articles": articles,
    }
    serialized = json.dumps(output, ensure_ascii=True, separators=(",", ":")) + "\n"
    history_path.write_text(serialized, encoding="utf-8")
    return True


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
        "departmentProfiles": config.get("department_profiles", []),
        "entityProfiles": config.get("entity_profiles", {}),
        "sourceCatalog": [
            {
                "id": source["id"],
                "name": source["name"],
                "type": source.get("adapter", "rss"),
                "tags": source.get("default_tags", []),
            }
            for source in config.get("sources", [])
        ],
        "sourceTypes": {
            source["id"]: source.get("adapter", "rss")
            for source in config.get("sources", [])
        },
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
    source_state_path = ROOT / config["output"].get("source_state_json_path", "data/source-state.json")
    source_state = load_source_state(source_state_path)

    articles: List[Dict] = []
    errors: List[Dict] = []

    for source in config["sources"]:
        try:
            payload = fetch_feed(source["url"], timeout, user_agent)
            adapter = source.get("adapter", "rss")
            if adapter == "rss":
                parsed_articles = parse_feed(payload, source, config, max_items)
            elif adapter == "web_watch":
                parsed_articles = parse_web_watch(
                    payload,
                    source,
                    config,
                    int(source.get("max_items", max_items)),
                    generated_at,
                    source_state,
                )
            else:
                raise ValueError(f"unknown source adapter: {adapter}")
            articles.extend(parsed_articles)
            print(f"[ok] {source['name']}: {len(parsed_articles)} items")
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": source["name"], "message": str(exc)})
            print(f"[error] {source['name']}: {exc}", file=sys.stderr)

    deduped = dedupe_articles(sort_articles(articles))
    recent_articles = filter_recent_articles(deduped, lookback_days, generated_at)
    output = build_output(recent_articles, config, errors, generated_at)
    write_output(output, config)
    if write_source_state(source_state, source_state_path):
        print(f"[done] wrote source state to {source_state_path.name}")

    print(f"[done] wrote {output['articleCount']} articles to {config['output']['json_path']}")

    # History accumulation: merge new relevant articles with existing history, then cap for Git.
    history_path = ROOT / config["output"].get("history_json_path", "data/articles-history.json")
    history_lookback_days = int(config["output"].get("history_lookback_days", 180))
    history_max_items = int(config["output"].get("history_max_items", 0))
    new_clean = [to_clean_article(a) for a in deduped if a["audience"] != "Irrelevant"]
    existing_history = load_history(history_path)
    merged = merge_with_history(new_clean, existing_history)
    pruned = prune_history(merged, history_lookback_days, generated_at)
    pruned = cap_history(pruned, history_max_items)
    history_changed = write_history(pruned, history_path, generated_at)

    if history_changed:
        print(f"[done] wrote {len(pruned)} articles to history ({history_path.name})")
    else:
        print(f"[done] history unchanged ({history_path.name})")
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
