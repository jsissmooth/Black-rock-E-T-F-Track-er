#!/usr/bin/env python3
"""
BlackRock ETF Holdings Tracker
Fetches daily holdings from BlackRock's CSV API.
Writes JSON snapshots to data/{TICKER}/.
"""

import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

try:
    import pandas_market_calendars as mcal
    _HAS_MCAL = True
except ImportError:
    _HAS_MCAL = False

# ─── Configuration ─────────────────────────────────────────────────────────────

# Portfolio IDs for each ETF
ETF_CONFIG = {
    "DYNF": "307283",
    "BAI":  "339081",
    "IALT": "346898",
    "TEK":  "339083",
    "CORO": "340366",
    "BMED": "316007",
    "BILT": "345073",
}

# BlackRock serves the same underlying API with two different parameter
# combinations.  Some ETFs respond to ISHARES/us-ishares, others to ONE/one.
# We try both for every date so either one works.
BLACKROCK_URLS = [
    (
        "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
        "product-data/api/v1/get-fund-document"
        "?appType=PRODUCT_PAGE&appSubType=ISHARES"
        "&targetSite=us-ishares&locale=en_US"
        "&portfolioId={portfolio_id}&userType=individual"
        "&asOfDate={date}&component=holdings"
    ),
    (
        "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
        "product-data/api/v1/get-fund-document"
        "?appType=PRODUCT_PAGE&appSubType=ONE"
        "&targetSite=one&locale=en_US"
        "&portfolioId={portfolio_id}&userType=individual"
        "&asOfDate={date}&component=holdings"
    ),
]

# Minimum data lines after the first Ticker header to accept a response as
# real holdings (not the placeholder asset-allocation summary).
# Placeholder: ≤ 11 data lines.  Smallest real fund: ≥ 20 holdings.
MIN_HOLDINGS_LINES = 20

# How many calendar days to look back for the most recent published date.
LOOKBACK_DAYS = 30

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.blackrock.com/",
}

DATA_DIR = Path("data")

# First-field values that mark a metadata/summary row, not a real holding
METADATA_VALUES = {
    "Ticker", "Stock", "Bond", "Cash", "Other",
    "Shares Outstanding", "Inception Date",
    "Fund Holdings as of", "Total", "Subtotal",
    "N/A", "-", "",
}

# ─── Market calendar ───────────────────────────────────────────────────────────

def is_trading_day(date_str: str) -> bool:
    """Return True if date_str (YYYY-MM-DD) is an NYSE trading day."""
    if _HAS_MCAL:
        try:
            nyse = mcal.get_calendar("NYSE")
            sched = nyse.schedule(start_date=date_str, end_date=date_str)
            return not sched.empty
        except Exception as e:
            print(f"  [warn] Market calendar error: {e} — falling back to weekday check")
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.weekday() < 5   # Mon–Fri

# ─── HTTP fetching ─────────────────────────────────────────────────────────────

def _count_data_lines_after_header(text: str) -> int:
    """
    Count the number of data lines that follow the first Ticker header row.
    The placeholder CSV has ≤ 11 data lines; real holdings always have ≥ 20.
    Stops counting when it hits a second Ticker header (another section start).
    """
    in_data = False
    count   = 0
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw.lstrip("﻿").replace('"', '').strip()
        is_header = (line.lower().startswith("ticker,")
                     and "name" in line.lower()
                     and "sector" in line.lower())
        if is_header:
            if not in_data:
                in_data = True   # found the first Ticker header; start counting
            else:
                break            # second header = new section; stop
        elif in_data and line:
            count += 1
    return count


def fetch_csv(ticker: str, portfolio_id: str, today: datetime):
    """
    Try today then up to LOOKBACK_DAYS prior calendar days, returning the
    first date+CSV that contains real individual holdings.

    BlackRock returns a short asset-allocation summary (~20 lines) for dates
    whose full holdings haven't been published yet.  We skip those by requiring
    at least MIN_HOLDINGS_LINES non-empty lines.  Both URL patterns are tried
    for every date so the script works regardless of which endpoint a fund uses.

    Returns (date_str YYYY-MM-DD, csv_text) or None.
    """
    for days_back in range(0, LOOKBACK_DAYS + 1):
        d = today - timedelta(days=days_back)
        date_str = d.strftime("%Y%m%d")
        d_label  = d.strftime("%Y-%m-%d")

        for url_template in BLACKROCK_URLS:
            url = url_template.format(portfolio_id=portfolio_id, date=date_str)
            try:
                resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
                if resp.status_code == 200:
                    text = resp.text.strip()
                    if "Ticker" not in text:
                        continue
                    # Count data lines after the FIRST Ticker header.
                    # The placeholder has ≤ 11 data lines per section;
                    # real holdings always have ≥ 20 (even the smallest fund).
                    data_lines = _count_data_lines_after_header(text)
                    if data_lines >= MIN_HOLDINGS_LINES:
                        print(f"  Found data for {d_label} "
                              f"({len(text):,} chars, {data_lines} data lines)")
                        return d_label, text
                    else:
                        print(f"  {d_label}: summary-only "
                              f"({data_lines} data lines < {MIN_HOLDINGS_LINES})")
                        # Both URLs return the same summary for unpublished
                        # dates – no point trying the second URL.
                        break
                elif resp.status_code not in (400, 404):
                    print(f"  {d_label}: HTTP {resp.status_code}")
            except requests.exceptions.Timeout:
                print(f"  {d_label}: request timed out")
            except Exception as e:
                print(f"  {d_label}: {type(e).__name__}: {e}")

    print(f"  No CSV found for {ticker} in the past {LOOKBACK_DAYS} days.")
    return None

# ─── CSV parsing ───────────────────────────────────────────────────────────────

def _header_indices(lines: list) -> list:
    """Return line numbers of every 'Ticker,…' header row."""
    result = []
    for i, raw in enumerate(lines):
        line = raw.lstrip("﻿").replace('"', '').strip()
        if (line.lower().startswith("ticker,")
                and "Name" in line and "Sector" in line):
            result.append(i)
    return result

def _col(col_map: dict, *names: str):
    for name in names:
        if name in col_map:
            return col_map[name]
    return None

def _to_float(val: str):
    if not val or val.strip() in ("", "-", "N/A", "nan", "None"):
        return None
    try:
        return float(val.replace(",", "").replace("%", "").strip())
    except ValueError:
        return None

def _valid(ticker: str, name: str, weight: str, mv: str) -> bool:
    t = (ticker or "").strip()
    if not t or t in METADATA_VALUES:
        return False
    if len(t) > 30 or not any(c.isalpha() for c in t):
        return False
    if (name or "").strip() in ("", "-"):
        return False
    return _to_float(weight) is not None or _to_float(mv) is not None

def _parse_rows(header_line: str, data_lines: list, stop_at_header: bool = True) -> list:
    """
    Parse a holdings section using csv.reader (handles variable column counts).
    header_line     – the Ticker,Name,… row (quotes and BOM already stripped)
    data_lines      – lines that follow the header
    stop_at_header  – if True, stop when another Ticker,… row is found;
                      if False (brute-force), skip such rows and keep going
    """
    try:
        columns = [c.strip() for c in next(csv.reader([header_line]))]
    except StopIteration:
        return []
    cmap = {c.lower(): i for i, c in enumerate(columns)}

    ticker_i = _col(cmap, "ticker")
    name_i   = _col(cmap, "name")
    sect_i   = _col(cmap, "sector")
    asset_i  = _col(cmap, "asset class")
    wt_i     = _col(cmap, "weight (%)", "weight(%)", "weight")
    mv_i     = _col(cmap, "market value", "market value (usd)")
    shr_i    = _col(cmap, "notional value", "shares", "quantity")
    loc_i    = _col(cmap, "location", "exchange")

    if ticker_i is None:
        return []

    def g(row, idx, default=""):
        return row[idx].strip() if idx is not None and idx < len(row) else default

    holdings = []
    seen = {}   # ticker -> index in holdings

    for row in csv.reader(data_lines):
        if not row:
            continue
        ticker = g(row, ticker_i)

        # Hit a second header row
        if ticker.lower() == "ticker" and len(row) > 3:
            if stop_at_header:
                break
            else:
                continue   # brute-force: skip the sub-header and keep reading

        name   = g(row, name_i)
        weight = g(row, wt_i)
        mv     = g(row, mv_i)

        if not _valid(ticker, name, weight, mv):
            continue

        h = {
            "ticker":       ticker,
            "name":         name,
            "sector":       g(row, sect_i),
            "asset_class":  g(row, asset_i),
            "weight":       _to_float(weight),
            "market_value": _to_float(mv),
            "shares":       _to_float(g(row, shr_i)),
            "location":     g(row, loc_i),
        }
        if ticker in seen:
            prev_w = holdings[seen[ticker]].get("weight") or 0
            curr_w = h["weight"] or 0
            if curr_w > prev_w:
                holdings[seen[ticker]] = h
        else:
            seen[ticker] = len(holdings)
            holdings.append(h)

    return holdings


def parse_holdings(csv_text: str) -> list:
    """
    Parse all holdings from a BlackRock CSV.

    Strategy
    --------
    1. Find all "Ticker,…" header rows in the file.
    2. Try each bounded section (between consecutive headers) in order;
       return the first section that yields valid holdings.
    3. Brute-force fallback: start from the FIRST header, read to EOF,
       and skip (not stop at) any embedded sub-headers.
       This handles cases like CORO where the real holdings sit in a
       second section whose rows have column-count differences that caused
       pandas to silently drop them.
    """
    lines = csv_text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    hdrs  = _header_indices(lines)

    if not hdrs:
        print(f"  No Ticker header found in {len(lines)}-line CSV.")
        return []

    print(f"  Header row(s) at line(s): {hdrs}")

    # Pass 1 – try each bounded section
    for idx, hi in enumerate(hdrs):
        next_hi = hdrs[idx + 1] if idx + 1 < len(hdrs) else len(lines)
        hl   = lines[hi].lstrip("﻿").replace('"', '')
        data = lines[hi + 1 : next_hi]
        h    = _parse_rows(hl, data, stop_at_header=True)
        print(f"  Section {idx} (lines {hi+1}–{next_hi-1}, "
              f"{len(data)} data lines): {len(h)} valid holdings")
        if h:
            return h

    # Pass 2 – brute force: first header to EOF, don't stop at sub-headers
    if len(hdrs) >= 1:
        print("  No section had valid holdings; trying brute-force pass…")
        hl   = lines[hdrs[0]].lstrip("﻿").replace('"', '')
        data = lines[hdrs[0] + 1 :]
        h    = _parse_rows(hl, data, stop_at_header=False)
        print(f"  Brute-force: {len(h)} valid holdings")
        if h:
            return h

    print("  Could not parse any valid holdings.")
    return []

# ─── Persistence ───────────────────────────────────────────────────────────────

def load_latest(ticker: str):
    p = DATA_DIR / ticker / "latest.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None

def compute_diff(old: list, new: list) -> dict:
    old_m = {h["ticker"]: h for h in old}
    new_m = {h["ticker"]: h for h in new}
    added   = [h for t, h in new_m.items() if t not in old_m]
    removed = [h for t, h in old_m.items() if t not in new_m]
    changed = []
    for t in set(old_m) & set(new_m):
        diffs = {
            k: {"from": old_m[t].get(k), "to": new_m[t].get(k)}
            for k in ("weight", "market_value", "shares", "sector", "name")
            if old_m[t].get(k) != new_m[t].get(k)
        }
        if diffs:
            changed.append({"ticker": t, "changes": diffs})
    return {"added": added, "removed": removed, "changed": changed}

def _append_history(ticker: str, today_str: str, entry: dict) -> None:
    p = DATA_DIR / ticker / "history.json"
    history = []
    if p.exists():
        try:
            history = json.loads(p.read_text())
        except Exception:
            pass
    history = [e for e in history if e.get("date") != today_str]
    history.append(entry)
    p.write_text(json.dumps(history[-365:], indent=2))

def save_snapshot(ticker: str, run_date: str, data_date: str,
                  holdings: list, diff: dict) -> None:
    folder = DATA_DIR / ticker
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "date":      data_date,   # BlackRock publication date
        "run_date":  run_date,    # date this workflow ran
        "ticker":    ticker,
        "count":     len(holdings),
        "holdings":  holdings,
    }
    for path in [folder / f"{data_date}.json", folder / "latest.json"]:
        path.write_text(json.dumps(snapshot, indent=2))
    (folder / "diff.json").write_text(
        json.dumps({"date": data_date, "ticker": ticker, **diff}, indent=2))
    _append_history(ticker, data_date, {"date": data_date, "count": len(holdings)})
    print(f"  → Saved {len(holdings)} holdings ({data_date}) to data/{ticker}/")

# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    today     = datetime.utcnow()
    today_str = today.strftime("%Y-%m-%d")

    print(f"=== BlackRock Holdings Tracker  ({today_str}) ===\n")

    if not is_trading_day(today_str):
        print(f"{today_str} is not an NYSE trading day – nothing to do.")
        sys.exit(0)

    any_saved = False
    for ticker, portfolio_id in ETF_CONFIG.items():
        print(f"── {ticker} ──────────────────────────────────")
        prior = load_latest(ticker)
        # Skip if we already ran successfully today.  Use run_date (new field)
        # with a fallback to date (old field) for backwards compatibility.
        prior_run = prior.get("run_date") or prior.get("date") if prior else None
        if prior_run == today_str:
            print(f"  Already saved today's run ({today_str}), skipping.\n")
            continue

        result = fetch_csv(ticker, portfolio_id, today)
        if result is None:
            print()
            continue

        csv_date, csv_text = result
        holdings = parse_holdings(csv_text)

        if not holdings:
            print(f"  No holdings parsed – keeping prior data.\n")
            continue

        prior_holdings = prior.get("holdings", []) if prior else []
        diff = compute_diff(prior_holdings, holdings)
        print(f"  Diff: +{len(diff['added'])} added, "
              f"-{len(diff['removed'])} removed, "
              f"{len(diff['changed'])} changed")
        save_snapshot(ticker, today_str, csv_date, holdings, diff)
        any_saved = True
        print()

    print("Done." if any_saved else "No new data saved today.")

if __name__ == "__main__":
    main()
