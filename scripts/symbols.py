"""
symbols.py — Search Upstox instrument keys for stocks, indices, and F&O contracts.

Upstox exposes a complete instrument master as a gzipped JSON file (~50 MB uncompressed).
This script downloads it once, caches it locally for 12 hours, then lets you search
by name or trading symbol from the command line.

Usage:
    python scripts/symbols.py reliance
    python scripts/symbols.py "nifty 50" --segment NSE_INDEX
    python scripts/symbols.py banknifty --segment NSE_FO --limit 20
"""

import gzip
import json
import sys
import time
import argparse
from pathlib import Path

import requests
from tabulate import tabulate

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT_DIR   = Path(__file__).resolve().parent.parent
DATA_DIR   = ROOT_DIR / "data"
CACHE_FILE = DATA_DIR / "instruments.json"

INSTRUMENTS_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)
CACHE_MAX_AGE_HOURS = 12


# ── Download / cache ──────────────────────────────────────────────────────────

def load_instruments() -> list[dict]:
    """
    Return the full instrument list as a Python list of dicts.

    Strategy:
      - If data/instruments.json exists and is < 12 hours old → use it (fast, no network).
      - Otherwise download the .gz from Upstox, decompress, save, and return it.
    """
    DATA_DIR.mkdir(exist_ok=True)

    if CACHE_FILE.exists():
        age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
        if age_hours < CACHE_MAX_AGE_HOURS:
            print(f"Using cached instruments ({age_hours:.1f}h old).")
            # json.loads on the whole file is faster than json.load with streaming
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))

    print("Downloading fresh instrument master from Upstox...")
    resp = requests.get(INSTRUMENTS_URL, timeout=60)
    resp.raise_for_status()

    # The response body is gzip-compressed; decompress in memory before parsing.
    instruments = json.loads(gzip.decompress(resp.content))

    CACHE_FILE.write_text(json.dumps(instruments), encoding="utf-8")
    print(f"Saved {len(instruments):,} instruments to {CACHE_FILE.relative_to(ROOT_DIR)}.")
    return instruments


# ── Search ────────────────────────────────────────────────────────────────────

def search(
    query: str,
    instruments: list[dict],
    segment: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Case-insensitive substring match on trading_symbol and name.

    Args:
        query:       The search term, e.g. "reliance" or "nifty 50".
        instruments: The full instrument list from load_instruments().
        segment:     Optional filter, e.g. "NSE_EQ", "NSE_INDEX", "NSE_FO".
        limit:       Maximum number of results to return.

    Returns:
        A list of matching instrument dicts (up to `limit` items).
    """
    q = query.lower()
    results = []

    for inst in instruments:
        # Skip mismatched segments early — cheap string compare before the substring search.
        if segment and inst.get("segment", "").upper() != segment.upper():
            continue

        symbol = (inst.get("trading_symbol") or "").lower()
        name   = (inst.get("name") or "").lower()

        if q in symbol or q in name:
            results.append(inst)
            if len(results) >= limit:
                break

    return results


# ── Display ───────────────────────────────────────────────────────────────────

def print_results(results: list[dict], query: str) -> None:
    """Render search results as a pretty table using tabulate."""
    if not results:
        print(f"No instruments found matching '{query}'.")
        return

    rows = [
        [
            r.get("instrument_key", ""),
            r.get("trading_symbol", ""),
            (r.get("name") or "")[:40],   # truncate very long names to keep the table tidy
            r.get("segment", ""),
            r.get("exchange", ""),
        ]
        for r in results
    ]

    print(tabulate(
        rows,
        headers=["instrument_key", "trading_symbol", "name", "segment", "exchange"],
        tablefmt="rounded_outline",
    ))
    print(f"\n{len(results)} result(s) for '{query}'.")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    # Reconfigure stdout to UTF-8 so Unicode table borders print correctly on Windows.
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Search Upstox instruments by name or trading symbol.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/symbols.py reliance\n"
            "  python scripts/symbols.py \"nifty 50\" --segment NSE_INDEX\n"
            "  python scripts/symbols.py banknifty --segment NSE_FO --limit 20\n"
        ),
    )
    parser.add_argument("query", help="Search term (e.g. 'reliance', 'nifty 50')")
    parser.add_argument(
        "--segment",
        help="Filter by segment: NSE_EQ, BSE_EQ, NSE_INDEX, BSE_INDEX, NSE_FO, BSE_FO, MCX_FO",
    )
    parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    args = parser.parse_args()

    instruments = load_instruments()
    results = search(args.query, instruments, segment=args.segment, limit=args.limit)
    print()
    print_results(results, args.query)


if __name__ == "__main__":
    main()
