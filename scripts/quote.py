"""
quote.py — Fetch live market quotes from Upstox for one or more instruments.

The Upstox market-quote API returns a snapshot: LTP, OHLC, order-book depth,
volume, and timestamps. This script formats it into a readable per-instrument block.

Usage:
    python scripts/quote.py "NSE_INDEX|Nifty 50"
    python scripts/quote.py "NSE_INDEX|Nifty 50" "NSE_INDEX|Nifty Bank" "NSE_EQ|INE002A01018"

Use scripts/symbols.py to look up instrument_keys.
"""

import os
import sys
import time
import argparse
from pathlib import Path

import requests
from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv

# ── Environment ───────────────────────────────────────────────────────────────

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

TOKEN    = os.getenv("UPSTOX_ACCESS_TOKEN", "")
BASE_URL = "https://api.upstox.com/v2"


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_inr(n: float) -> str:
    """
    Format a number with Indian comma grouping.
    The rightmost 3 digits form one group; everything left groups in 2s.
    e.g.  24532.5   → "24,532.50"
          125000.0  → "1,25,000.00"
          1234567.8 → "12,34,567.80"
    """
    sign = "-" if n < 0 else ""
    integer_part, _, decimal_part = f"{abs(n):.2f}".partition(".")

    if len(integer_part) <= 3:
        return f"{sign}{integer_part}.{decimal_part}"

    # Rightmost 3 digits stay together; head groups in 2s from the right.
    last3  = integer_part[-3:]
    head   = integer_part[:-3]
    groups = []
    while head:
        groups.append(head[-2:])
        head = head[:-2]
    groups.reverse()
    return f"{sign}{','.join(groups)},{last3}.{decimal_part}"


def colored_change(value: float) -> str:
    """Return value string with ANSI color: green for positive, red for negative."""
    sign = "+" if value > 0 else ""
    s = f"{sign}{value:.2f}"
    if value > 0:
        return f"{Fore.GREEN}{s}{Style.RESET_ALL}"
    elif value < 0:
        return f"{Fore.RED}{s}{Style.RESET_ALL}"
    return s


def parse_timestamp(ts) -> str:
    """
    Convert last_trade_time to a readable string.
    Upstox returns epoch milliseconds as a string (e.g. "1779100200000"),
    an integer, or occasionally an ISO-8601 string.
    """
    if not ts:
        return "N/A"
    # String that looks like an integer → epoch ms
    if isinstance(ts, str) and ts.isdigit():
        ts = int(ts)
    if isinstance(ts, (int, float)):
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone()
        return dt.strftime("%d %b %Y  %H:%M:%S")
    # ISO string — trim timezone and microseconds for readability
    return str(ts).replace("T", "  ").split("+")[0].split(".")[0]


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_quotes(keys: list[str]) -> dict:
    """
    GET /v2/market-quote/quotes for all requested keys in one call.
    Retries once on connection error. Returns the 'data' dict from the response.
    """
    url     = f"{BASE_URL}/market-quote/quotes"
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    params  = {"instrument_key": ",".join(keys)}

    resp = None
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            break
        except requests.ConnectionError as exc:
            if attempt == 0:
                print("Network error — retrying...")
                time.sleep(2)
            else:
                print(f"[ERROR] Could not connect to Upstox API: {exc}")
                sys.exit(1)

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

    # Upstox returns response keys with ":" as the separator (e.g. "NSE_INDEX:Nifty 50")
    # but callers use "|" (e.g. "NSE_INDEX|Nifty 50"). Normalise to "|" so lookups work.
    raw = body.get("data", {})
    return {k.replace(":", "|", 1): v for k, v in raw.items()}


# ── Display ───────────────────────────────────────────────────────────────────

def print_quote(key: str, q: dict) -> None:
    """Render one instrument's quote as a formatted section block."""
    # The Upstox quotes endpoint doesn't return a name field; derive it from the key.
    # "NSE_INDEX|Nifty 50" → "Nifty 50",  "NSE_EQ|INE002A01018" → "INE002A01018"
    display = key.split("|")[-1] if "|" in key else key

    ltp     = q.get("last_price", 0.0) or 0.0
    ohlc    = q.get("ohlc", {})
    open_   = ohlc.get("open",  0.0) or 0.0
    high    = ohlc.get("high",  0.0) or 0.0
    low     = ohlc.get("low",   0.0) or 0.0
    volume  = q.get("volume") or 0

    # Upstox's ohlc.close is the current day's closing price (equals LTP after close),
    # NOT the previous day's close. Use the API's pre-calculated net_change to derive it.
    net_chg    = q.get("net_change", 0.0) or 0.0
    prev_close = ltp - net_chg
    pct_chg    = (net_chg / prev_close * 100) if prev_close else 0.0

    # Prefer last_trade_time (time of the last tick) over the snapshot timestamp.
    ts = parse_timestamp(q.get("last_trade_time") or q.get("timestamp"))

    depth = q.get("depth", {})
    bid   = (depth.get("buy")  or [{}])[0].get("price", 0.0)
    ask   = (depth.get("sell") or [{}])[0].get("price", 0.0)

    # Color the LTP and header based on direction of change
    ltp_color = Fore.GREEN if net_chg >= 0 else Fore.RED

    print(f"\n{'─' * 52}")
    print(f"  {Style.BRIGHT}{display}{Style.RESET_ALL}   {Style.DIM}[{key}]{Style.RESET_ALL}")
    print(f"{'─' * 52}")
    print(f"  LTP        :  {ltp_color}{Style.BRIGHT}₹{fmt_inr(ltp)}{Style.RESET_ALL}")
    print(f"  Change     :  {colored_change(net_chg)}   ({colored_change(pct_chg)}%)")
    print(f"  Open       :  ₹{fmt_inr(open_)}")
    print(f"  High       :  ₹{fmt_inr(high)}")
    print(f"  Low        :  ₹{fmt_inr(low)}")
    print(f"  Prev Close :  ₹{fmt_inr(prev_close)}")
    if volume:
        print(f"  Volume     :  {volume:,}")
    if bid or ask:
        bid_str = f"₹{fmt_inr(bid)}" if bid else "—"
        ask_str = f"₹{fmt_inr(ask)}" if ask else "—"
        print(f"  Bid / Ask  :  {bid_str}  /  {ask_str}")
    print(f"  Updated    :  {ts}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    # Reconfigure stdout to UTF-8 so ₹ and emojis print correctly on Windows.
    sys.stdout.reconfigure(encoding="utf-8")
    # Init colorama — on Windows 10/11 this enables native VT ANSI support
    # without wrapping stdout, so it cooperates with the reconfigure above.
    colorama_init()

    if not TOKEN:
        print("[ERROR] UPSTOX_ACCESS_TOKEN not set in .env.")
        print("  → Run: python scripts/login.py")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Fetch live market quotes from Upstox.",
        epilog='Example: python scripts/quote.py "NSE_INDEX|Nifty 50" "NSE_EQ|INE002A01018"',
    )
    parser.add_argument(
        "instrument_keys",
        nargs="+",
        metavar="instrument_key",
        help='Instrument key(s) e.g. "NSE_INDEX|Nifty 50". Use symbols.py to look them up.',
    )
    args = parser.parse_args()

    data = fetch_quotes(args.instrument_keys)

    for key in args.instrument_keys:
        q = data.get(key)
        if q is None:
            print(f"\n[ERROR] No data returned for '{key}'.")
            print("  → Check the instrument key with: python scripts/symbols.py <name>")
            continue
        print_quote(key, q)

    print()


if __name__ == "__main__":
    main()
