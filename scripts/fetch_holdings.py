import json
import os
import sys
import requests
from datetime import date, timedelta
from io import StringIO
import pandas as pd
import pandas_market_calendars as mcal

BASE_URL = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
    "product-data/api/v1/get-fund-document"
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

ETFS = {
    "DYNF": {"portfolio_id": "307283", "target_site": "one",                    "user_type": "individual",    "name": "iShares U.S. Equity Factor Rotation Active ETF"},
    "BAI":  {"portfolio_id": "339081", "target_site": "financial-professionals", "user_type": "intermediaries","name": "iShares A.I. Innovation and Tech Active ETF"},
    "IALT": {"portfolio_id": "346898", "target_site": "one",                    "user_type": "individual",    "name": "iShares International Equity Factor Rotation Active ETF"},
    "TEK":  {"portfolio_id": "339083", "target_site": "one",                    "user_type": "individual",    "name": "iShares Technology Opportunities Active ETF"},
    "CORO": {"portfolio_id": "340366", "target_site": "one",                    "user_type": "individual",    "name": "iShares International Country Rotation Active ETF"},
    "BMED": {"portfolio_id": "316007", "target_site": "one",                    "user_type": "individual",    "name": "iShares Health Innovation Active ETF"},
    "BILT": {"portfolio_id": "345073", "target_site": "one",                    "user_type": "individual",    "name": "iShares Infrastructure Active ETF"},
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.blackrock.com/",
    "Accept": "text/csv,application/json,*/*",
}


def is_nyse_trading_day(d):
    nyse = mcal.get_calendar("NYSE")
    return not nyse.schedule(start_date=d.isoformat(), end_date=d.isoformat()).empty


def fetch_csv(portfolio_id, target_site, user_type, as_of_date):
    params = {
        "appType":     "PRODUCT_PAGE",
        "appSubType":  "ONE",
        "targetSite":  target_site,
        "locale":      "en_US",
        "portfolioId": portfolio_id,
        "userType":    user_type,
        "asOfDate":    as_of_date,
        "component":   "holdings",
    }
    resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_latest_csv(portfolio_id, target_site, user_type):
    d = date.today()
    for _ in range(10):
        date_str = d.strftime("%Y%m%d")
        try:
            text = fetch_csv(portfolio_id, target_site, user_type, date_str)
            if "Ticker" in text and len(text) > 500:
                print("  Data found for {}".format(d.isoformat()), file=sys.stderr)
                return text, d.isoformat()
        except Exception as e:
            print("  {} not available: {}".format(date_str, str(e)[:40]), file=sys.stderr)
        d -= timedelta(days=1)
    return None, None


def find_header_indices(lines):
    """Find all rows that look like the Ticker header row."""
    indices = []
    for i, line in enumerate(lines):
        # strip whitespace and leading/trailing quotes
        clean = line.strip().lstrip('"')
        if clean.startswith("Ticker,"):
            indices.append(i)
    return indices


def parse_holdings(csv_text):
    """Parse BlackRock CSV — find first data section and parse it."""
    lines = csv_text.splitlines()

    header_indices = find_header_indices(lines)

    if not header_indices:
        print("  Could not find Ticker header row.", file=sys.stderr)
        # debug: print first 15 lines
        for i, l in enumerate(lines[:15]):
            print("  line {}: {}".format(i, l[:80]), file=sys.stderr)
        return []

    start_idx = header_indices[0]
    # If two header sections exist (e.g. CORO has ETF basket + look-through),
    # use only the first section
    end_idx = header_indices[1] if len(header_indices) > 1 else len(lines)

    print("  Header at line {}, section ends at line {}".format(
        start_idx, end_idx), file=sys.stderr)

    # Grab the section — let pandas handle blank/bad rows naturally
    section_text = "\n".join(lines[start_idx:end_idx])

    try:
        df = pd.read_csv(StringIO(section_text), on_bad_lines="skip")
        df.columns = [c.strip() for c in df.columns]
        print("  Raw rows parsed: {}".format(len(df)), file=sys.stderr)
    except Exception as e:
        print("  CSV parse error: {}".format(e), file=sys.stderr)
        return []

    def safe_float(val):
        try:
            import math
            s = str(val).strip().replace(",", "").replace("%", "").replace("$", "")
            v = float(s)
            return None if math.isnan(v) else v
        except (ValueError, TypeError):
            return None

    def find_col(keywords):
        for kw in keywords:
            for col in df.columns:
                if kw.lower() == col.lower().strip():
                    return col
        for kw in keywords:
            for col in df.columns:
                if kw.lower() in col.lower():
                    return col
        return None

    ticker_col = find_col(["Ticker"])
    name_col   = find_col(["Name"])
    sector_col = find_col(["Sector"])
    weight_col = find_col(["Weight (%)", "Weight"])
    shares_col = find_col(["Shares"])
    mv_col     = find_col(["Market Value"])
    price_col  = find_col(["Price"])

    print("  Columns: {}".format(list(df.columns[:6])), file=sys.stderr)

    records = []
    for _, row in df.iterrows():
        ticker = str(row[ticker_col]).strip() if ticker_col else ""
        name   = str(row[name_col]).strip()   if name_col   else ""
        sector = str(row[sector_col]).strip() if sector_col else ""

        # Skip blank, header-repeat, or obviously bad rows
        if not ticker or ticker.lower() in ("nan", "ticker", "-"):
            continue
        # Skip rows where ticker looks like a fund description (very long)
        if len(ticker) > 50:
            continue
        if name.lower()   == "nan": name = ""
        if sector.lower() == "nan": sector = ""

        records.append({
            "ticker":       ticker,
            "name":         name,
            "identifier":   ticker,
            "sector":       sector,
            "pct_of_fund":  safe_float(row[weight_col]) if weight_col else None,
            "quantity":     safe_float(row[shares_col]) if shares_col else None,
            "market_value": safe_float(row[mv_col])     if mv_col     else None,
            "price":        safe_float(row[price_col])  if price_col  else None,
        })

    return records


def get_etf_data_dir(ticker):
    d = os.path.join(DATA_DIR, ticker)
    os.makedirs(d, exist_ok=True)
    return d


def save_snapshot(records, today_str, ticker):
    data_dir = get_etf_data_dir(ticker)
    payload = {"date": today_str, "ticker": ticker, "holdings": records}
    with open(os.path.join(data_dir, "{}.json".format(today_str)), "w") as f:
        json.dump(payload, f, indent=2)
    with open(os.path.join(data_dir, "latest.json"), "w") as f:
        json.dump(payload, f, indent=2)


def find_prior_snapshot(today_str, ticker):
    data_dir = get_etf_data_dir(ticker)
    files = sorted(
        f for f in os.listdir(data_dir)
        if f.endswith(".json") and f not in ("latest.json", "diff.json", "history.json")
    )
    prior = [f for f in files if f.replace(".json", "") < today_str]
    return os.path.join(data_dir, prior[-1]) if prior else None


def compute_diff(today_records, prior_records, today_str, prior_date_str, etf_ticker):
    today_map = {r["ticker"]: r for r in today_records}
    prior_map = {r["ticker"]: r for r in prior_records}
    all_keys  = sorted(set(today_map) | set(prior_map))
    rows = []
    for key in all_keys:
        t = today_map.get(key)
        p = prior_map.get(key)
        if t and p:
            q_today   = t["quantity"]    or 0
            q_prior   = p["quantity"]    or 0
            pct_today = t["pct_of_fund"] or 0
            pct_prior = p["pct_of_fund"] or 0
            qty_chg   = ((q_today - q_prior) / q_prior * 100) if q_prior != 0 else 0
            rows.append({
                "ticker":              t["ticker"],
                "name":                t.get("name") or p.get("name") or "",
                "identifier":          t.get("identifier") or "",
                "sector":              t.get("sector") or "",
                "status":              "changed" if round(qty_chg, 6) != 0 else "unchanged",
                "quantity_today":      q_today,
                "quantity_prior":      q_prior,
                "quantity_pct_change": round(qty_chg, 4),
                "pct_of_fund_today":   pct_today,
                "pct_of_fund_prior":   pct_prior,
                "pct_of_fund_change":  round(pct_today - pct_prior, 4),
                "market_value_today":  t.get("market_value"),
                "price_today":         t.get("price"),
            })
        elif t:
            rows.append({
                "ticker": t["ticker"], "name": t.get("name") or "",
                "identifier": t.get("identifier") or "", "sector": t.get("sector") or "",
                "status": "added",
                "quantity_today": t["quantity"] or 0, "quantity_prior": None,
                "quantity_pct_change": None,
                "pct_of_fund_today": t["pct_of_fund"] or 0, "pct_of_fund_prior": None,
                "pct_of_fund_change": None,
                "market_value_today": t.get("market_value"), "price_today": t.get("price"),
            })
        else:
            rows.append({
                "ticker": p["ticker"], "name": p.get("name") or "",
                "identifier": p.get("identifier") or "", "sector": p.get("sector") or "",
                "status": "removed",
                "quantity_today": None, "quantity_prior": p["quantity"] or 0,
                "quantity_pct_change": None, "pct_of_fund_today": None,
                "pct_of_fund_prior": p["pct_of_fund"] or 0,
                "pct_of_fund_change": None, "market_value_today": None, "price_today": None,
            })
    return {"date": today_str, "ticker": etf_ticker, "prior_date": prior_date_str, "diff": rows}


def append_history(today_str, diff, ticker):
    data_dir = get_etf_data_dir(ticker)
    history_path = os.path.join(data_dir, "history.json")
    history = []
    if os.path.exists(history_path):
        with open(history_path) as f:
            history = json.load(f)
    entry = {"date": today_str, "prior_date": diff["prior_date"]}
    if entry not in history:
        history.append(entry)
        history.sort(key=lambda x: x["date"], reverse=True)
    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)


def process_etf(etf_ticker, info, today_str):
    print("Fetching {}...".format(etf_ticker), file=sys.stderr)
    try:
        csv_text, data_date = find_latest_csv(
            info["portfolio_id"], info["target_site"], info["user_type"]
        )
        if not csv_text:
            print("  No data found.", file=sys.stderr)
            return

        records = parse_holdings(csv_text)
        if not records:
            print("  No holdings parsed.", file=sys.stderr)
            return

        print("  {} holdings found (as of {}).".format(len(records), data_date), file=sys.stderr)
        save_snapshot(records, today_str, etf_ticker)

        prior_path = find_prior_snapshot(today_str, etf_ticker)
        if not prior_path:
            diff_rows = [{
                "ticker":              r["ticker"],
                "name":                r.get("name") or "",
                "identifier":          r.get("identifier") or "",
                "sector":              r.get("sector") or "",
                "status":              "unchanged",
                "quantity_today":      r["quantity"] or 0,
                "quantity_prior":      r["quantity"] or 0,
                "quantity_pct_change": 0,
                "pct_of_fund_today":   r["pct_of_fund"] or 0,
                "pct_of_fund_prior":   r["pct_of_fund"] or 0,
                "pct_of_fund_change":  0,
                "market_value_today":  r.get("market_value"),
                "price_today":         r.get("price"),
            } for r in records]
            diff = {"date": today_str, "ticker": etf_ticker, "prior_date": None, "diff": diff_rows}
        else:
            with open(prior_path) as f:
                prior_data = json.load(f)
            if prior_data["date"] == today_str:
                print("  Already have data -- skipping.", file=sys.stderr)
                return
            diff = compute_diff(records, prior_data["holdings"], today_str, prior_data["date"], etf_ticker)

        data_dir = get_etf_data_dir(etf_ticker)
        with open(os.path.join(data_dir, "diff.json"), "w") as f:
            json.dump(diff, f, indent=2)
        append_history(today_str, diff, etf_ticker)

        changed = sum(1 for r in diff["diff"] if r["status"] == "changed")
        added   = sum(1 for r in diff["diff"] if r["status"] == "added")
        removed = sum(1 for r in diff["diff"] if r["status"] == "removed")
        print("  Done -- {} holdings | {} changed | {} added | {} removed".format(
            len(records), changed, added, removed), file=sys.stderr)

    except Exception as e:
        import traceback
        print("  ERROR: {}".format(e), file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


def main():
    today_str = date.today().isoformat()
    today     = date.today()

    if not is_nyse_trading_day(today):
        print("{} is not a NYSE trading day -- skipping.".format(today_str), file=sys.stderr)
        sys.exit(0)

    print("Running for {}...".format(today_str), file=sys.stderr)
    for etf_ticker, info in ETFS.items():
        process_etf(etf_ticker, info, today_str)
    print("All done.", file=sys.stderr)


if __name__ == "__main__":
    main()
