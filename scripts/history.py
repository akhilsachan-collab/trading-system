"""
history.py — Download OHLC historical candle data from Upstox and save as CSV.

Candles are cached in data/history/ and smart-merged: only missing date ranges
are re-fetched on subsequent runs.

Usage:
    python scripts/history.py "NSE_INDEX|Nifty 50"
    python scripts/history.py "NSE_INDEX|Nifty 50" --interval day --from 2024-01-01 --to 2024-12-31
    python scripts/history.py "NSE_EQ|INE002A01018" --interval 5minute --force
"""

import os
import sys
import argparse
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
from dotenv import load_dotenv

# ── Environment ───────────────────────────────────────────────────────────────

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

TOKEN    = os.getenv("UPSTOX_ACCESS_TOKEN", "")
BASE_URL = "https://api.upstox.com/v3"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "history"

# ── Interval mapping ──────────────────────────────────────────────────────────

INTERVAL_MAP: dict[str, tuple[str, str]] = {
    "1minute":  ("minutes", "1"),
    "3minute":  ("minutes", "3"),
    "5minute":  ("minutes", "5"),
    "15minute": ("minutes", "15"),
    "30minute": ("minutes", "30"),
    "1hour":    ("hours", "1"),
    "2hour":    ("hours", "2"),
    "4hour":    ("hours", "4"),
    "day":      ("days", "1"),
    "week":     ("weeks", "1"),
    "month":    ("months", "1"),
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize_key(instrument_key: str) -> str:
    return instrument_key.replace("|", "_").replace(" ", "_")


def csv_path(instrument_key: str, interval: str) -> Path:
    return DATA_DIR / f"{sanitize_key(instrument_key)}_{interval}.csv"


def candles_to_df(candles: list) -> pd.DataFrame:
    """Convert raw Upstox candle arrays [[ts, o, h, l, c, vol, oi], ...] to a DataFrame."""
    rows = [
        {
            "timestamp":     c[0],
            "open":          c[1],
            "high":          c[2],
            "low":           c[3],
            "close":         c[4],
            "volume":        c[5],
            "open_interest": c[6] if len(c) > 6 else 0,
        }
        for c in candles
    ]
    df = pd.DataFrame(rows)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def fetch_candles(
    instrument_key: str,
    unit: str,
    interval: str,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    """Call Upstox V3 historical-candle API and return a DataFrame."""
    encoded_key = quote(instrument_key, safe="")
    url = f"{BASE_URL}/historical-candle/{encoded_key}/{unit}/{interval}/{to_date}/{from_date}"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept":        "application/json",
    }

    resp = requests.get(url, headers=headers, timeout=30)

    if resp.status_code in (401, 403):
        print("[ERROR] Token expired or invalid.")
        print("  → Run: python scripts/login.py")
        sys.exit(1)

    if not resp.ok:
        print(f"[ERROR] API returned HTTP {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)

    body = resp.json()
    if body.get("status") != "success":
        print(f"[ERROR] Unexpected API response: {body}")
        sys.exit(1)

    candles = body.get("data", {}).get("candles", [])
    return candles_to_df(candles)


def load_cached(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def save_df(df: pd.DataFrame, path: Path) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


# ── Core download/cache logic ─────────────────────────────────────────────────

def get_candles(
    instrument_key: str,
    interval: str,
    from_date: date,
    to_date: date,
    force: bool,
) -> pd.DataFrame:
    unit, api_interval = INTERVAL_MAP[interval]
    path     = csv_path(instrument_key, interval)
    from_str = from_date.isoformat()
    to_str   = to_date.isoformat()

    if path.exists() and not force:
        cached = load_cached(path)

        if not cached.empty:
            cached_min = cached["timestamp"].dt.date.min()
            cached_max = cached["timestamp"].dt.date.max()

            # Full cache hit — no download needed
            if cached_min <= from_date and cached_max >= to_date:
                mask   = (cached["timestamp"].dt.date >= from_date) & \
                         (cached["timestamp"].dt.date <= to_date)
                result = cached[mask].reset_index(drop=True)
                print(f"Using cached data: {path.name} ({len(result)} rows)")
                return result

            # Partial coverage — fetch only the missing portions and merge
            frames: list[pd.DataFrame] = [cached]

            if from_date < cached_min:
                gap_to = (cached_min - timedelta(days=1)).isoformat()
                print(f"Downloading missing range: {from_str} → {gap_to}")
                frames.append(
                    fetch_candles(instrument_key, unit, api_interval, from_str, gap_to)
                )

            if to_date > cached_max:
                gap_from = (cached_max + timedelta(days=1)).isoformat()
                print(f"Downloading missing range: {gap_from} → {to_str}")
                frames.append(
                    fetch_candles(instrument_key, unit, api_interval, gap_from, to_str)
                )

            merged = (
                pd.concat(frames, ignore_index=True)
                .assign(timestamp=lambda d: pd.to_datetime(d["timestamp"]))
                .drop_duplicates(subset=["timestamp"])
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            save_df(merged, path)
            print(f"Updated cache: {path.name} ({len(merged)} rows total)")

            mask = (merged["timestamp"].dt.date >= from_date) & \
                   (merged["timestamp"].dt.date <= to_date)
            return merged[mask].reset_index(drop=True)

    # No cache or --force: full download
    print(f"Downloading {instrument_key}  [{interval}]  {from_str} → {to_str} ...")
    df = fetch_candles(instrument_key, unit, api_interval, from_str, to_str)

    if df.empty:
        print("[WARN] No candles returned for the requested range.")
        return df

    save_df(df, path)
    print(f"Saved {len(df)} rows → {path}")
    return df


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    if not TOKEN:
        print("[ERROR] UPSTOX_ACCESS_TOKEN not set in .env.")
        print("  → Run: python scripts/login.py")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Download historical OHLC candle data from Upstox.",
        epilog=(
            'Examples:\n'
            '  python scripts/history.py "NSE_INDEX|Nifty 50"\n'
            '  python scripts/history.py "NSE_EQ|INE002A01018" --interval 5minute --from 2025-01-01\n'
            '  python scripts/history.py "NSE_INDEX|Nifty 50" --force'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "instrument_key",
        help='Instrument key e.g. "NSE_INDEX|Nifty 50". Use symbols.py to look them up.',
    )
    parser.add_argument(
        "--interval",
        default="day",
        choices=list(INTERVAL_MAP.keys()),
        metavar="INTERVAL",
        help=(
            f"Candle interval. Choices: {', '.join(INTERVAL_MAP)}. "
            "(default: day)"
        ),
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        default=(date.today() - timedelta(days=365)).isoformat(),
        metavar="YYYY-MM-DD",
        help="Start date inclusive (default: 1 year ago)",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        default=date.today().isoformat(),
        metavar="YYYY-MM-DD",
        help="End date inclusive (default: today)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Redownload even if a cached CSV already exists",
    )
    args = parser.parse_args()

    try:
        from_date = date.fromisoformat(args.from_date)
        to_date   = date.fromisoformat(args.to_date)
    except ValueError as exc:
        print(f"[ERROR] Invalid date: {exc}")
        sys.exit(1)

    if from_date > to_date:
        print("[ERROR] --from must be on or before --to")
        sys.exit(1)

    df = get_candles(args.instrument_key, args.interval, from_date, to_date, args.force)

    if not df.empty:
        print(f"\n  Rows  : {len(df)}")
        print(f"  From  : {df['timestamp'].min()}")
        print(f"  To    : {df['timestamp'].max()}")
        pd.set_option("display.float_format", "{:.2f}".format)
        print(f"\n{df.tail(5).to_string(index=False)}")


if __name__ == "__main__":
    main()
