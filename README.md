# Hamoor data

Public stock data feeding the Hamoor iOS app
(<https://github.com/alialm98/hamoor> — coming soon).

## What's here

- `boursa-kuwait-stocks.json` — master list of Boursa Kuwait tickers with
  sector / market metadata.
- `fy2025-dividends.json` — curated dividend amounts and dates for the
  past few fiscal years (until we can extract them from the official PDFs
  programmatically).
- `output/` — generated data, regenerated nightly by the updater scripts:
  - `output/stocks.json` — single-file blob with every stock's metadata
    plus the last 90 days of price history. This is what the iOS app
    fetches on launch and every 15 minutes to refresh prices.
  - `output/prices/<TICKER>.json` — full daily price history per stock
    (5+ years). Fetched on-demand when the user opens a chart.
  - `output/manifest.json` — index of available per-stock price files.
- `fetch_boursa_history.py` — one-shot historical backfill for every
  stock. Pulls 5 years of daily closes from Yahoo Finance.
- `update_prices.py` — daily incremental updater. Merges the last few
  days into both the canonical store (`output/prices/`) and the slim
  copy that lives in the app bundle (`../Hamoor/Resources/prices/`).
- `launchd/` — macOS LaunchAgent plist that runs the daily updater
  Sun–Thu at 14:00 local time.

## Source

Closing prices come from Yahoo Finance via the `yfinance` Python package.
Boursa Kuwait disclosures, board members, and dividend event dates are
fetched live in-app from the public `boursakuwait.com.kw/data-api`
endpoints — no caching in this repo.

## Updating

Manual:

```bash
cd data
python3 update_prices.py            # rolling 10-day window
python3 update_prices.py --from 2026-05-13 --to 2026-05-14   # explicit range
```

Automated (Sun–Thu 14:00 Kuwait time): see [`launchd/README.md`](launchd/README.md).

After each update, commit + push so the iOS app sees the new data:

```bash
cd data
git add output/
git commit -m "Daily price update $(date -u +%Y-%m-%d)"
git push
```
