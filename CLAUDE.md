# Multraship Board Watch — AI Context

## What this project is

A private maritime news dashboard for Multraship and Novatug board members. Hosted on GitHub Pages (static). Data is refreshed every ~30 min via a GitHub Actions cron job that runs `scripts/fetch_feeds.py`, commits the output, and pushes to `main`. The page is password-protected with a SHA-256 hash stored in localStorage.

Two audiences are tracked: **Multraship** (towage, salvage, port ops) and **Novatug** (harbour towage). Articles are classified as relevant to one or both. The Novatug view is simpler (no entity panels, no view mode split).

---

## Architecture

```
GitHub Actions (every ~30 min, often delayed)
  └── scripts/fetch_feeds.py
        ├── reads config/dashboard_config.json
        ├── fetches all RSS/Atom feeds
        ├── classifies + scores articles
        ├── writes data/articles.json       ← full JSON
        ├── writes data/articles.js         ← window.__DASHBOARD_DATA__ = {...}
        ├── writes data/articles-history.json ← rolling 180-day archive
        └── writes data/stocks.json         ← live stock quotes (yfinance)

GitHub Pages serves index.html
  └── <script src="./data/articles.js">    ← embeds data at page load
        window.__DASHBOARD_DATA__ = {
          articles, sources, topics, locations, errors,
          entityProfiles, entityCategories, generatedAt, ...
        }
```

`articles-history.json` and `stocks.json` are lazy-loaded by the frontend when needed (entity panels at 90d/6mo range, entity overlay stock widget).

---

## Key files

| File | Role |
|------|------|
| `config/dashboard_config.json` | Single source of truth: RSS sources, classification rules, entity lists, entity profiles, stock tickers |
| `scripts/fetch_feeds.py` | Feed fetcher, classifier, scorer, output writer |
| `scripts/backscrape_entities.py` | One-off script: Google News RSS backscrape for all entities with a date filter |
| `index.html` | Entire frontend: HTML + CSS + JS in one file (~2000 lines) |
| `data/articles.js` | Auto-generated; do not edit manually |
| `data/articles.json` | Auto-generated; do not edit manually |
| `data/articles-history.json` | Auto-generated; do not edit manually |
| `data/stocks.json` | Auto-generated; do not edit manually |
| `.github/workflows/fetch.yml` | GitHub Actions cron + push workflow |

---

## config/dashboard_config.json structure

```jsonc
{
  "request": { "timeout_seconds": 10, "user_agent": "..." },
  "output": {
    "json_path": "data/articles.json",
    "js_path": "data/articles.js",
    "history_json_path": "data/articles-history.json",
    "stocks_json_path": "data/stocks.json",
    "max_total_items": 180,           // cap on articles.js (dashboard)
    "max_items_per_source": 50,
    "lookback_days": 30,
    "history_lookback_days": 180
  },
  "sources": [                        // 87 sources total (RSS/Atom/Google News)
    {
      "id": "unique-id",
      "name": "Display Name",
      "url": "https://...",
      "default_tags": ["towage"],
      "audience_bias": { "Multraship": 5, "Novatug": 3 }
    }
  ],
  "classification": {
    "audiences": {
      "Multraship": { "keywords": [...], "locations": [...], ... },
      "Novatug": { ... }
    },
    "entities": {                     // name → category mapping source
      "suppliers": ["Damen Shipyards Group", ...],
      "clients":   ["Rijkswaterstaat", ...],
      "competitors": ["Boskalis", ...]
    },
    "entity_aliases": {               // canonical name → list of alternate spellings
      "Damen Shipyards Group": ["Damen Group", "Damen Shipbuilding"]
    },
    "topics": { ... },
    "locations": { ... },
    "priority_rules": { ... }
  },
  "entity_profiles": {               // 49 entities pre-populated
    "Damen Shipyards Group": {
      "website": "https://...",
      "linkedin": "https://...",
      "hq": "Gorinchem, Netherlands",
      "description": "...",
      "founded": 1927,
      "stock": null
    },
    "Kongsberg Maritime": {
      "stock": { "ticker": "KOG.OL", "exchange": "Oslo Børs", "note": "Parent: Kongsberg Gruppen ASA" }
    }
  }
}
```

**Stock tickers in entity_profiles (17 total):** CAT, KOG.OL, BVI.PA, WRT1V.HE, SHEL.L, BP.L, RWE.DE, ORSTED.CO, SSE.L, TTE.PA, DEME.BR, MT.AS, EQNR.OL, YAR.OL, XOM, TATASTEEL.NS, DFDS.CO — all fetched via `yfinance`.

---

## scripts/fetch_feeds.py — key functions

| Function | What it does |
|----------|-------------|
| `load_config()` | Reads dashboard_config.json |
| `fetch_feed(url, timeout, ua)` | HTTP GET with retries |
| `parse_feed(payload, source, config, max_items)` | RSS/Atom → list of article dicts |
| `classify_article(article, config)` | Sets audience, priorityBand, priorityScore, entities, locations, tags |
| `sort_articles(articles)` | Sort by priorityScore desc then date desc |
| `dedupe_articles(articles)` | Dedup by URL |
| `build_output(articles, config, errors, generated_at)` | Builds the full data dict including `entityProfiles` and `entityCategories` |
| `write_output(output, config)` | Writes articles.json + articles.js |
| `load_history / merge_with_history / prune_history / write_history` | Rolling 180-day history management |
| `fetch_stocks(profiles, generated_at)` | yfinance quotes for all entities with a stock ticker |
| `write_stocks(stocks, config, generated_at)` | Writes stocks.json as `{ "generatedAt": "...", "stocks": { "EntityName": {...} } }` |

**Article dict shape (internal, before `to_clean_article`):**
```python
{
  "title", "url", "publishedAt", "source", "excerpt",
  "audience",        # "Multraship" | "Novatug" | "Both" | "Irrelevant"
  "priorityBand",    # "critical" | "high" | "medium" | "low"
  "priorityScore",   # int
  "priorityReasons", # list of strings
  "boardBucket",     # "High Priority" | "Port Watch" | "Clients & Projects" | ...
  "entities",        # [{"name": str, "category": "suppliers"|"clients"|"competitors"}]
  "locations",       # [{"name": str, "type": "port"|"region"}]
  "tags",            # topic tags
  "businessTags",    # signal tags (Incident, Disruption, etc.)
  "paywalled",       # bool
}
```

---

## index.html — frontend architecture

Single-file frontend, no build step, no framework. All state in a `const state = {...}` object.

### State shape
```javascript
state = {
  data,              // window.__DASHBOARD_DATA__ (articles.js)
  activeAudience,    // "Multraship" | "Novatug" | "Both"
  viewMode,          // "all" | "procurement" | "management" (localStorage: mnd_view_mode)
  activeView,        // "dashboard" | "entities"
  openEntityName,    // string | null (entity overlay)
  overlayTab,        // "profile" | "news" | "events"
  stocksData,        // null | {} | { "EntityName": { price, change, changePct, ... } }
  stocksLoading,     // bool
  historyData,       // null | { articles: [...] } (lazy-loaded)
  historyLoading,    // bool
  bucketVisibility,  // { "AudienceKey:BucketKey": limit }
  selectedEntities,  // { suppliers: name|null, clients: name|null, competitors: name|null }
  entityDateRange,   // { suppliers: "30d", clients: "30d", competitors: "30d" }
  entityVisibility,  // { suppliers: 10, clients: 10, competitors: 10 }
}
```

### Render flow
```
boot()
  └── syncDataToUi()         populate filter dropdowns, update stats
  └── render()
        ├── updateAudienceMeta()   toggle buttons, hide/show viewMode
        ├── renderBoardSummary()   4 summary cards (top strip)
        ├── renderBuckets()        3-column bucket grid
        └── renderEntitySections() 1-3 entity panels (suppliers/clients/competitors)
```

### View mode
- **All**: suppliers + clients + competitors panels shown
- **Procurement**: suppliers panel only
- **Management**: clients + competitors panels
- Novatug audience: entity panels hidden entirely

### Entity directory + overlay
- "Entities" button in top bar → `switchView("entities")` hides dashboard, shows `#entityDirectoryView`
- `renderEntityDirectory()`: grid of entity cards grouped by category, with search + sort (A-Z / Most Mentioned)
- Clicking a card → `openEntityOverlay(name)` → centered modal (900px wide)
- Overlay tabs: **Profile** (website, LinkedIn, HQ, founded, description, stock widget) | **News** (up to 50 articles) | **Events** (placeholder)
- `loadStocksData()` lazy-fetches `data/stocks.json` → `state.stocksData = data.stocks`
- `loadHistoryData()` lazy-fetches `data/articles-history.json` → used for 90d/6mo entity panel ranges and overlay News tab

### Entity chips in article rows
Entity name chips (yellow=supplier, blue=client, red=competitor) appear in the left meta column of each article, next to the priority pill. Clicking opens the entity overlay directly.

### Article sort order
Priority band enum first (`PRIORITY_LEVELS = { critical:3, high:2, medium:1, low:0 }`), then date descending within each band. Raw `priorityScore` is NOT used for display sorting.

### Authentication
SHA-256 of password compared against `PASSWORD_HASH` constant. Hash stored in localStorage under `mnd_auth`. No server involved.

---

## Data flow gotcha: rebase conflicts

GitHub Actions auto-commits `data/articles.js`, `data/articles.json`, `data/articles-history.json`, `data/stocks.json` after every run. When rebasing local changes on top of remote:

- `--ours` during rebase = the **remote** branch (counterintuitive)
- `--theirs` during rebase = your local commit

Always prefer locally-generated data files (they're more recent). Use `git checkout --theirs <file>` during a rebase conflict on data files.

---

## Feed sources overview

- ~56 original sources (Bing/Google News RSS, trade press Atom feeds)
- ~32 Google News RSS feeds added for smaller zero-result entities
- All Bing feeds use `&count=50` for more results
- Broken sources discovered so far: TED EU Rijkswaterstaat (dropped RSS in eForms migration — removed)
- Preferred fallback for broken feeds: Google News RSS `https://news.google.com/rss/search?q=...&hl=en-US&gl=US&ceid=US:en`

## GitHub Actions cron reliability

Scheduled at `*/30 * * * *` (fires at :00 and :30 of every hour) but GitHub's scheduler is unreliable — gaps of 1–3 hours are common. Manual trigger available via "Run workflow" in the Actions tab. The workflow only commits/pushes if data files actually changed.
