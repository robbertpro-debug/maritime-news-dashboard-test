# Maritime News Dashboard

Local-only starter dashboard for maritime news relevant to Novatug and Multraship.

## What it does

- pulls RSS feeds with a local Python script
- normalizes articles into `data/articles.json`
- generates `data/articles.js` so `index.html` works directly from your filesystem
- renders one dense dashboard with Multraship, Novatug, and shared items

## Usage

1. Refresh the data:

```bash
python3 scripts/fetch_feeds.py
```

2. For the dashboard with in-page refresh support, start the local server:

```bash
python3 scripts/local_server.py
```

3. Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

You can still open [index.html](/Users/robbertprovoost/Documents/Playground/Maritime News Dashboard/index.html) directly, but the refresh button only works through the local server.

## Files

- [config/dashboard_config.json](/Users/robbertprovoost/Documents/Playground/Maritime News Dashboard/config/dashboard_config.json): sources, keywords, topic rules
- [scripts/fetch_feeds.py](/Users/robbertprovoost/Documents/Playground/Maritime News Dashboard/scripts/fetch_feeds.py): feed ingestion and data generation
- [scripts/local_server.py](/Users/robbertprovoost/Documents/Playground/Maritime News Dashboard/scripts/local_server.py): local-only HTTP server with refresh endpoint
- [data/articles.json](/Users/robbertprovoost/Documents/Playground/Maritime News Dashboard/data/articles.json): normalized output
- [data/articles.js](/Users/robbertprovoost/Documents/Playground/Maritime News Dashboard/data/articles.js): browser-friendly local export
- [index.html](/Users/robbertprovoost/Documents/Playground/Maritime News Dashboard/index.html): dashboard

## Notes

- Everything stays local.
- Feed content is sanitized to plain text before display.
- If a feed fails, the script keeps going and reports the failure in the output metadata.
