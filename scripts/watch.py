"""
watch.py — Live market dashboard: indices, stocks, and commodities.

Loads watchlist.json, fetches all quotes in one API call, and prints a
grouped table with LTP, change, and direction arrow per instrument.

Usage:
    python scripts/watch.py
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta, time as time_
from pathlib import Path

import requests
from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv

# ── Paths / env ───────────────────────────────────────────────────────────────

ROOT        = Path(__file__).resolve().parent.parent
ENV_PATH    = ROOT / ".env"
WATCHLIST   = ROOT / "watchlist.json"
INSTRUMENTS = ROOT / "data" / "instruments.json"
KEY_CACHE   = ROOT / "data" / "watchlist_key_map.json"
BASE_URL    = "https://api.upstox.com/v2"
IST         = timezone(timedelta(hours=5, minutes=30))

load_dotenv(ENV_PATH)
TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")

# ── Column layout ─────────────────────────────────────────────────────────────

C_LABEL = 22   # visible chars
C_LTP   = 14
C_CHG   = 12
C_PCT   =  9
# total visible row width: 2+22+2+14+2+12+2+9+2+1 = 68
WIDTH   = 68

# ── Output ────────────────────────────────────────────────────────────────────

def out(text: str = "") -> None:
    """Write a line to stdout as UTF-8 bytes, bypassing the default console codec."""
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_inr(n: float) -> str:
    """Indian comma-grouping: 1,23,456.78"""
    sign = "-" if n < 0 else ""
    integer_part, _, dec = f"{abs(n):.2f}".partition(".")
    if len(integer_part) <= 3:
        return f"{sign}{integer_part}.{dec}"
    last3  = integer_part[-3:]
    head   = integer_part[:-3]
    groups = []
    while head:
        groups.append(head[-2:])
        head = head[:-2]
    groups.reverse()
    return f"{sign}{','.join(groups)},{last3}.{dec}"


def format_row(label: str, q: dict | None) -> str:
    if q is None:
        return (
            f"  {label:<{C_LABEL}}"
            f"  {'—':>{C_LTP}}"
            f"  {'—':>{C_CHG}}"
            f"  {'—':>{C_PCT}}"
            f"  —"
        )

    ltp     = q.get("last_price", 0.0) or 0.0
    net_chg = q.get("net_change", 0.0) or 0.0
    prev_cl = ltp - net_chg
    pct     = (net_chg / prev_cl * 100) if prev_cl else 0.0

    sign  = "+" if net_chg >= 0 else ""
    chg_s = f"{sign}{net_chg:.2f}"
    pct_s = f"{sign}{pct:.2f}%"
    arrow = "▲" if net_chg > 0 else ("▼" if net_chg < 0 else " ")

    color = Fore.GREEN if net_chg > 0 else (Fore.RED if net_chg < 0 else "")
    rst   = Style.RESET_ALL if color else ""

    # Pad plain strings first, then wrap in color so ANSI doesn't skew width
    return (
        f"  {label:<{C_LABEL}}"
        f"  {fmt_inr(ltp):>{C_LTP}}"
        f"  {color}{chg_s:>{C_CHG}}{rst}"
        f"  {color}{pct_s:>{C_PCT}}{rst}"
        f"  {arrow}"
    )


# ── Key mapping ───────────────────────────────────────────────────────────────
#
# The Upstox market-quote API accepts ISIN-based keys (NSE_EQ|INE002A01018) and
# token-based keys (MCX_FO|459277) but RETURNS data keyed by trading_symbol
# (NSE_EQ|RELIANCE, MCX_FO|GOLD26JUNFUT).  We build a mapping once and cache it
# so every subsequent run is instant.

def _derive_api_key(seg: str, inst_key: str, trading_symbol: str) -> str:
    """Return the key format that the API uses in its response for this instrument."""
    if seg == "NSE_INDEX":
        return inst_key  # indices returned as-is
    if seg == "NSE_EQ":
        return f"NSE_EQ|{trading_symbol}"
    if seg == "MCX_FO" and " FUT " in trading_symbol:
        # "GOLD FUT 05 JUN 26" → "MCX_FO|GOLD26JUNFUT"
        parts = trading_symbol.split()
        return f"MCX_FO|{parts[0]}{parts[4]}{parts[3]}FUT"
    return inst_key  # fallback: assume no translation needed


def build_key_map(watchlist_keys: list[str]) -> dict[str, str]:
    """
    Return {instrument_key → api_response_key} for every key in the watchlist.
    Result is cached in data/watchlist_key_map.json and rebuilt only when
    instruments.json is newer than the cache.
    """
    needed = set(watchlist_keys)

    # Use cache when fresh
    if KEY_CACHE.exists() and INSTRUMENTS.exists():
        if KEY_CACHE.stat().st_mtime >= INSTRUMENTS.stat().st_mtime:
            with open(KEY_CACHE, encoding="utf-8") as f:
                cached = json.load(f)
            if needed.issubset(cached):
                return cached

    # Build from instruments.json (~1.2 s)
    mapping: dict[str, str] = {}
    with open(INSTRUMENTS, encoding="utf-8") as f:
        for inst in json.load(f):
            ikey = inst.get("instrument_key", "")
            if ikey not in needed:
                continue
            mapping[ikey] = _derive_api_key(
                inst.get("segment", ""),
                ikey,
                inst.get("trading_symbol", ""),
            )

    # Save cache
    with open(KEY_CACHE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)

    return mapping


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_quotes(keys: list[str]) -> dict:
    url     = f"{BASE_URL}/market-quote/quotes"
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    params  = {"instrument_key": ",".join(keys)}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
    except requests.ConnectionError as exc:
        out(f"[ERROR] Could not connect to Upstox API: {exc}")
        sys.exit(1)

    if resp.status_code in (401, 403):
        out("Token expired. Run: python scripts\\login.py")
        sys.exit(1)

    if not resp.ok:
        out(f"[ERROR] HTTP {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)

    body = resp.json()
    if body.get("status") != "success":
        out(f"[ERROR] Unexpected API response: {body}")
        sys.exit(1)

    raw = body.get("data", {})
    # Upstox returns keys with ":" separator; normalise to "|"
    return {k.replace(":", "|", 1): v for k, v in raw.items()}


# ── Market status ─────────────────────────────────────────────────────────────

def market_status_line() -> str:
    now = datetime.now(IST)
    wd  = now.weekday()   # 0 = Mon
    t   = now.time()

    nse_open = (wd < 5) and (time_(9, 15) <= t <= time_(15, 30))
    mcx_open = (wd < 5) and (time_(9, 0)  <= t <= time_(23, 30))

    def badge(flag: bool) -> str:
        return (Fore.GREEN + "OPEN" + Style.RESET_ALL) if flag else (Fore.RED + "CLOSED" + Style.RESET_ALL)

    return f"  Market:  NSE {badge(nse_open)}   MCX {badge(mcx_open)}"


# ── Dashboard ─────────────────────────────────────────────────────────────────

THICK = "═" * WIDTH
THIN  = "  " + "─" * (WIDTH - 2)
HDR_ROW = (
    f"  {'LABEL':<{C_LABEL}}"
    f"  {'LTP':>{C_LTP}}"
    f"  {'CHANGE':>{C_CHG}}"
    f"  {'%CHG':>{C_PCT}}"
    f"  DIR"
)


def main() -> None:
    # Enable VT100 on Windows 10+ so ANSI codes render natively in the console
    colorama_init()

    if not TOKEN:
        out("[ERROR] UPSTOX_ACCESS_TOKEN not set in .env")
        out("  → Run: python scripts\\login.py")
        sys.exit(1)

    with open(WATCHLIST, encoding="utf-8") as f:
        wl = json.load(f)

    sections = [
        ("INDICES",      wl.get("indices", [])),
        ("STOCKS",       wl.get("stocks", [])),
        ("COMMODITIES",  wl.get("commodities", [])),
    ]

    all_keys = [item["key"] for _, items in sections for item in items]

    # Build key map (cached after first run; takes ~1.2s on first run)
    key_map = build_key_map(all_keys)

    data = fetch_quotes(all_keys)

    # ── Header banner ──────────────────────────────────────────────────────────
    now_ist  = datetime.now(IST)
    ts_str   = now_ist.strftime("%d %b %Y  %H:%M IST")
    title    = f"  {Style.BRIGHT}MARKET DASHBOARD{Style.RESET_ALL}"
    # visible title length is len("  MARKET DASHBOARD") = 18
    padding  = WIDTH - 18 - len(ts_str)
    out()
    out(THICK)
    out(title + " " * padding + ts_str)
    out(THICK)

    # ── Sections ───────────────────────────────────────────────────────────────
    for title, items in sections:
        if not items:
            continue
        out()
        out(f"  {Style.BRIGHT}{title}{Style.RESET_ALL}")
        out(HDR_ROW)
        out(THIN)
        for item in items:
            api_key = key_map.get(item["key"], item["key"])
            out(format_row(item["label"], data.get(api_key)))

    # ── Footer ─────────────────────────────────────────────────────────────────
    out()
    out(THIN)
    out(market_status_line())
    out(THICK)
    out()


if __name__ == "__main__":
    main()
