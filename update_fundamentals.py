#!/usr/bin/env python3
"""
update_fundamentals.py
─────────────────────────────────────────────────────────────────────────
Pulls per-stock fundamentals from Yahoo Finance via yfinance and writes
`output/fundamentals.json`, keyed by ticker. Designed to run daily after
market close — fundamentals update at quarterly earnings releases, so
once-a-day is plenty.

What each ticker carries:

  summary               headline metrics + identity blurb
    marketCap, sharesOutstanding, trailingPE, priceToBook,
    priceToSales, dividendYield, payoutRatio, returnOnEquity,
    returnOnAssets, profitMargins, beta, fiftyTwoWeekHigh,
    fiftyTwoWeekLow, fiftyDayAverage, twoHundredDayAverage,
    targetMeanPrice, targetHighPrice, targetLowPrice,
    numberOfAnalystOpinions, recommendationKey,
    fullTimeEmployees, industry, longBusinessSummary
  annualIncome    [{ fiscalYear, totalRevenue, operatingExpense,
                     pretaxIncome, netIncome, dilutedEPS }, …]
  quarterlyIncome  same shape, period = "YYYY-Q#"
  annualBalance   [{ fiscalYear, totalAssets, totalLiabilities,
                     totalEquity, cashAndEquivalents, totalDebt }, …]
  annualCashFlow  [{ fiscalYear, operatingCashFlow, investingCashFlow,
                     financingCashFlow, freeCashFlow }, …]

The `update_prices.py` merge step (see `load_fundamentals_by_ticker`)
attaches each ticker's record to its `StockMetadata.fundamentals`
field, deployed via the existing OTA pipeline.

Usage:
    python3 update_fundamentals.py                   # full refresh
    python3 update_fundamentals.py --ticker NBK      # one-stock test
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import yfinance as yf

SCRIPT_DIR = Path(__file__).resolve().parent
STOCKS_FILE = SCRIPT_DIR / "boursa-kuwait-stocks.json"
OUTPUT_FILE = SCRIPT_DIR / "output" / "fundamentals.json"
THROTTLE_SECONDS = 0.4


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Field plucking helpers

def _safe(value):
    """yfinance returns numpy/pandas types; collapse to JSON-friendly forms.

    `NaN` and `Inf` are valid in numpy but JSON.dump explodes on them, so
    they collapse to `None`. Yahoo sometimes hands back the *strings*
    "Infinity", "-Infinity", or "NaN" for unusual PE / yield values
    (e.g. URC's `trailingPE`); those would break the Swift `Double?`
    decoder, so they also collapse to `None`. Decimals stay as floats —
    same precision the Yahoo source uses.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if value in ("Infinity", "-Infinity", "NaN"):
            return None
        return value  # legitimate string fields (longName, sector, etc.)
    try:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        # numpy types have an .item() method
        if hasattr(value, "item"):
            v = value.item()
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v
        return value
    except Exception:
        return None


def _info_pick(info: dict, keys: list[str]) -> dict:
    return {k: _safe(info.get(k)) for k in keys}


def _df_row(df, row_names: list[str]):
    """Return the first row from `df` whose index matches any of row_names.

    yfinance line-item labels drift between stocks (e.g. NBK has
    `Operating Revenue`, ZAIN has `Total Revenue`). We accept a list of
    candidates and take whichever shows up first.
    """
    if df is None or df.empty:
        return None
    for name in row_names:
        if name in df.index:
            return df.loc[name]
    return None


def _columns_to_years_or_periods(df, mode: str) -> list[str]:
    """Convert dataframe columns (Timestamps) to fiscal-year ints or
    quarter strings, in newest-first order.
    """
    cols = list(df.columns)
    if mode == "year":
        return [int(c.year) for c in cols]
    # quarterly → "YYYY-Q#"
    return [f"{c.year}-Q{((c.month - 1) // 3) + 1}" for c in cols]


# ---------------------------------------------------------------------------
# Per-statement extraction

_INCOME_ROWS = {
    "totalRevenue":     ["Total Revenue", "Operating Revenue"],
    "operatingExpense": ["Operating Expense", "Total Operating Expenses",
                         "Selling General And Administration"],
    "pretaxIncome":     ["Pretax Income", "Income Before Tax"],
    "netIncome":        ["Net Income", "Net Income Common Stockholders",
                         "Net Income From Continuing Operation Net Minority Interest"],
    "dilutedEPS":       ["Diluted EPS", "Basic EPS"],
}

_BALANCE_ROWS = {
    "totalAssets":         ["Total Assets"],
    "totalLiabilities":    ["Total Liabilities Net Minority Interest",
                            "Total Liab"],
    "totalEquity":         ["Stockholders Equity", "Total Equity Gross Minority Interest",
                            "Common Stock Equity"],
    "cashAndEquivalents":  ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    "totalDebt":           ["Total Debt", "Long Term Debt"],
}

_CASHFLOW_ROWS = {
    "operatingCashFlow": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "investingCashFlow": ["Investing Cash Flow", "Cash Flow From Continuing Investing Activities"],
    "financingCashFlow": ["Financing Cash Flow", "Cash Flow From Continuing Financing Activities"],
    "freeCashFlow":      ["Free Cash Flow"],
}


def _extract_statement(df, rows_map: dict, period_mode: str) -> list[dict]:
    """Turn a yfinance financials/balance/cashflow dataframe into a
    period-keyed list of dicts — one entry per column (year or quarter)."""
    if df is None or df.empty:
        return []
    periods = _columns_to_years_or_periods(df, period_mode)
    out: list[dict] = []
    # Pull each metric row once
    metric_series = {field: _df_row(df, candidates)
                     for field, candidates in rows_map.items()}
    for i, period in enumerate(periods):
        entry = {"fiscalYear" if period_mode == "year" else "period": period}
        for field, series in metric_series.items():
            if series is None:
                entry[field] = None
                continue
            try:
                entry[field] = _safe(series.iloc[i])
            except Exception:
                entry[field] = None
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Summary fields

_SUMMARY_KEYS = [
    "longName", "shortName", "sector", "industry", "fullTimeEmployees",
    "longBusinessSummary", "country", "currency",
    "marketCap", "enterpriseValue", "sharesOutstanding", "floatShares",
    "trailingPE", "forwardPE", "priceToBook", "priceToSalesTrailing12Months",
    "trailingEps", "forwardEps", "earningsGrowth", "revenueGrowth",
    "totalRevenue", "grossProfits", "totalCash", "totalDebt", "bookValue",
    "returnOnEquity", "returnOnAssets", "profitMargins", "operatingMargins",
    "ebitda", "ebitdaMargins",
    "dividendRate", "dividendYield", "payoutRatio",
    "fiveYearAvgDividendYield", "trailingAnnualDividendYield",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyDayAverage",
    "twoHundredDayAverage", "beta",
    "targetMeanPrice", "targetHighPrice", "targetLowPrice",
    "numberOfAnalystOpinions", "recommendationKey", "recommendationMean",
    "heldPercentInsiders", "heldPercentInstitutions",
]


# ---------------------------------------------------------------------------
# Per-ticker fetch

def fetch_one(ticker: str) -> dict | None:
    """Returns the full fundamentals record for one ticker, or None on
    error. Always rate-limit before calling this."""
    yticker = f"{ticker}.KW"
    t = yf.Ticker(yticker)

    info = t.info or {}
    if not info or info.get("quoteType") not in (None, "EQUITY"):
        # Non-equity (mutual fund tagged) — Yahoo doesn't have it as a stock
        log(f"  {ticker:14} skip: quoteType={info.get('quoteType')!r}")
        return None

    summary = _info_pick(info, _SUMMARY_KEYS)

    record = {
        "summary": summary,
        "annualIncome": _extract_statement(t.financials, _INCOME_ROWS, "year"),
        "quarterlyIncome": _extract_statement(t.quarterly_financials, _INCOME_ROWS, "quarter"),
        "annualBalance": _extract_statement(t.balance_sheet, _BALANCE_ROWS, "year"),
        "annualCashFlow": _extract_statement(t.cashflow, _CASHFLOW_ROWS, "year"),
    }
    return record


# ---------------------------------------------------------------------------
# Orchestration

def load_our_stocks() -> list[dict]:
    with open(STOCKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["stocks"] if isinstance(data, dict) else data


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--ticker", help="Restrict to a single ticker (testing)")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    stocks = load_our_stocks()
    if args.ticker:
        stocks = [s for s in stocks if s["ticker"] == args.ticker.upper()]
        if not stocks:
            log(f"No stock matching --ticker {args.ticker!r}")
            return 1

    fundamentals: dict[str, dict] = {}
    ok = err = skipped = 0
    for s in stocks:
        ticker = s["ticker"]
        try:
            record = fetch_one(ticker)
            if record is None:
                skipped += 1
            else:
                fundamentals[ticker] = record
                ai = len(record.get("annualIncome") or [])
                qi = len(record.get("quarterlyIncome") or [])
                ab = len(record.get("annualBalance") or [])
                cf = len(record.get("annualCashFlow") or [])
                log(f"  {ticker:14} ann={ai} qtr={qi} bs={ab} cf={cf}")
                ok += 1
        except Exception as e:
            log(f"  {ticker:14} ERROR {str(e)[:90]}")
            err += 1
        time.sleep(THROTTLE_SECONDS)

    log("")
    log("Summary:")
    log(f"  OK     : {ok}")
    log(f"  Skipped: {skipped}")
    log(f"  Errors : {err}")

    output = {
        "_meta": {
            "generatedAt": datetime.utcnow().isoformat() + "Z",
            "source": "Yahoo Finance (yfinance)",
            "stocks": ok,
        },
        "fundamentalsByTicker": fundamentals,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log(f"Wrote {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
