"""
stream.py — Live WebSocket market feed with a rich in-place terminal dashboard.

Uses Upstox V3 protobuf WebSocket (wss://api.upstox.com/v3/feed/market-data-feed).
Bootstraps prev_close from a one-shot REST call, then streams LTPC ticks and
updates a rich live table in-place for all instruments in watchlist.json.

Usage:
    python scripts/stream.py
"""

import json
import os
import ssl
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import websocket
from dotenv import load_dotenv
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text
from upstox_client.feeder.proto.MarketDataFeedV3_pb2 import FeedResponse

# ── Paths / env ───────────────────────────────────────────────────────────────

ROOT        = Path(__file__).resolve().parent.parent
WATCHLIST   = ROOT / "watchlist.json"
INSTRUMENTS = ROOT / "data" / "instruments.json"
KEY_CACHE   = ROOT / "data" / "watchlist_key_map.json"
BASE_URL    = "https://api.upstox.com/v2"
WS_URL      = "wss://api.upstox.com/v3/feed/market-data-feed"
IST         = timezone(timedelta(hours=5, minutes=30))

load_dotenv(ROOT / ".env")
TOKEN   = os.getenv("UPSTOX_ACCESS_TOKEN", "")
console = Console()

# ── Shared state ──────────────────────────────────────────────────────────────

_lock      = threading.Lock()
quotes: dict[str, dict] = {}   # wl_key → {ltp, prev_close, ltt_str}
tick_count = 0
connected  = False
status_msg = "Starting up..."
running    = True

# ── Key mapping (same logic as watch.py, reuses cached file) ─────────────────

def _derive_api_key(seg: str, inst_key: str, trading_symbol: str) -> str:
    """Compute the key format the Upstox REST API returns in responses."""
    if seg == "NSE_INDEX":
        return inst_key
    if seg == "NSE_EQ":
        return f"NSE_EQ|{trading_symbol}"
    if seg == "MCX_FO" and " FUT " in trading_symbol:
        parts = trading_symbol.split()
        return f"MCX_FO|{parts[0]}{parts[4]}{parts[3]}FUT"
    return inst_key


def build_key_map(watchlist_keys: list[str]) -> dict[str, str]:
    needed = set(watchlist_keys)
    if KEY_CACHE.exists() and INSTRUMENTS.exists():
        if KEY_CACHE.stat().st_mtime >= INSTRUMENTS.stat().st_mtime:
            with open(KEY_CACHE, encoding="utf-8") as f:
                cached = json.load(f)
            if needed.issubset(cached):
                return cached
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
    with open(KEY_CACHE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)
    return mapping

# ── REST bootstrap ────────────────────────────────────────────────────────────

def bootstrap(all_keys: list[str], rev_map: dict[str, str]) -> int:
    """Seed quotes dict with LTP + prev_close from one REST call."""
    url     = f"{BASE_URL}/market-quote/quotes"
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    params  = {"instrument_key": ",".join(all_keys)}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if not resp.ok:
            return 0
        body = resp.json()
        if body.get("status") != "success":
            return 0
        raw   = {k.replace(":", "|", 1): v for k, v in body.get("data", {}).items()}
        count = 0
        with _lock:
            for api_key, q in raw.items():
                wl_key    = rev_map.get(api_key, api_key)
                ltp       = q.get("last_price", 0.0) or 0.0
                net_chg   = q.get("net_change", 0.0) or 0.0
                quotes[wl_key] = {
                    "ltp":        ltp,
                    "prev_close": ltp - net_chg,
                    "ltt_str":    "—",
                }
                count += 1
        return count
    except Exception:
        return 0

# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_inr(n: float) -> str:
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

# ── WebSocket handlers ────────────────────────────────────────────────────────

def _on_open(ws, all_keys: list[str]) -> None:
    global connected, status_msg
    connected  = True
    status_msg = f"Subscribed to {len(all_keys)} instruments"
    # V3: subscription sent as a binary WebSocket frame containing UTF-8 JSON
    request = json.dumps({
        "guid":   str(uuid.uuid4()),
        "method": "sub",
        "data":   {"mode": "ltpc", "instrumentKeys": all_keys},
    }).encode("utf-8")
    ws.send(request, opcode=websocket.ABNF.OPCODE_BINARY)


def _on_message(ws, message: bytes, rev_map: dict[str, str]) -> None:
    global tick_count
    if not isinstance(message, bytes):
        return
    feed_response = FeedResponse()
    try:
        feed_response.ParseFromString(message)
    except Exception:
        return
    # Type 0=initial_feed, 1=live_feed; skip 2=market_info
    if feed_response.type not in (0, 1):
        return
    now_str = datetime.now(IST).strftime("%H:%M:%S")
    with _lock:
        for key, feed in feed_response.feeds.items():
            if not feed.HasField("ltpc"):
                continue
            ltp = feed.ltpc.ltp
            cp  = feed.ltpc.cp
            # V3 may return the same key as subscribed (ISIN/token) OR the
            # trading_symbol key (like V2 REST).  rev_map handles the latter;
            # if key is already a watchlist key it falls through unchanged.
            wl_key     = rev_map.get(key, key)
            existing   = quotes.get(wl_key, {})
            prev_close = cp if cp > 0 else existing.get("prev_close", ltp)
            quotes[wl_key] = {"ltp": ltp, "prev_close": prev_close, "ltt_str": now_str}
            tick_count += 1


def _on_error(ws, error) -> None:
    global connected
    connected = False


def _on_close(ws, close_status_code, close_msg) -> None:
    global connected
    connected = False


def run_ws(all_keys: list[str], rev_map: dict[str, str]) -> None:
    global connected, status_msg, running
    backoff = 1
    while running:
        ws = websocket.WebSocketApp(
            WS_URL,
            header={"Authorization": f"Bearer {TOKEN}"},
            on_open=lambda w: _on_open(w, all_keys),
            on_message=lambda w, m: _on_message(w, m, rev_map),
            on_error=_on_error,
            on_close=_on_close,
        )
        try:
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False})
        except Exception as exc:
            err = str(exc)
            if "401" in err or "403" in err or getattr(exc, "status_code", 0) in (401, 403):
                status_msg = "Token expired. Run: python scripts\\login.py"
                running    = False
                return
        if not running:
            break
        status_msg = f"Disconnected — reconnecting in {backoff}s..."
        connected  = False
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

# ── Rich live display ─────────────────────────────────────────────────────────

def build_display(sections: list) -> Group:
    now = datetime.now(IST)
    ts  = now.strftime("%H:%M:%S IST")

    if connected:
        hdr = Text.from_markup(
            f"[bold green]●[/bold green] [bold]LIVE STREAM[/bold]"
            f"  |  Updated: {ts}"
            f"  |  Ticks received: {tick_count}"
        )
    else:
        hdr = Text.from_markup(
            f"[bold yellow]○[/bold yellow] [bold yellow]{status_msg}[/bold yellow]"
            f"  |  {ts}"
            f"  |  Ticks received: {tick_count}"
        )

    tbl = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold dim",
        padding=(0, 1),
        expand=False,
    )
    tbl.add_column("Label",   width=22, no_wrap=True)
    tbl.add_column("LTP",     width=14, justify="right")
    tbl.add_column("Change",  width=12, justify="right")
    tbl.add_column("%Chg",    width=9,  justify="right")
    tbl.add_column("Updated", width=10, justify="right")

    with _lock:
        for section_name, items in sections:
            if not items:
                continue
            tbl.add_row(f"[bold cyan]{section_name}[/bold cyan]", "", "", "", "")
            for item in items:
                q = quotes.get(item["key"])
                if q is None:
                    tbl.add_row(
                        item["label"],
                        "[dim]—[/dim]", "[dim]—[/dim]", "[dim]—[/dim]", "[dim]—[/dim]",
                    )
                    continue
                ltp  = q["ltp"]
                pc   = q["prev_close"] or ltp
                chg  = ltp - pc
                pct  = (chg / pc * 100) if pc else 0.0
                sign = "+" if chg >= 0 else ""
                col  = "green" if chg > 0 else ("red" if chg < 0 else "white")
                tbl.add_row(
                    item["label"],
                    fmt_inr(ltp),
                    f"[{col}]{sign}{chg:.2f}[/{col}]",
                    f"[{col}]{sign}{pct:.2f}%[/{col}]",
                    f"[dim]{q['ltt_str']}[/dim]",
                )

    footer = Text("Press Ctrl+C to stop", style="dim italic")
    return Group(hdr, Text(""), tbl, footer)

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global running

    if not TOKEN:
        console.print("[red][ERROR] UPSTOX_ACCESS_TOKEN not set in .env[/red]")
        console.print("  Run: python scripts\\login.py")
        sys.exit(1)

    with open(WATCHLIST, encoding="utf-8") as f:
        wl = json.load(f)

    sections = [
        ("INDICES",     wl.get("indices", [])),
        ("STOCKS",      wl.get("stocks", [])),
        ("COMMODITIES", wl.get("commodities", [])),
    ]
    all_keys = [item["key"] for _, items in sections for item in items]

    console.print("Building instrument key map...", end="")
    key_map = build_key_map(all_keys)
    console.print(" [green]done[/green]")

    rev_map = {v: k for k, v in key_map.items()}

    console.print("Fetching initial quotes via REST...", end="")
    seeded = bootstrap(all_keys, rev_map)
    console.print(f" [green]done[/green] ({seeded} instruments seeded)")

    ws_thread = threading.Thread(target=run_ws, args=(all_keys, rev_map), daemon=True)
    ws_thread.start()

    final_count = 0
    try:
        with Live(
            build_display(sections),
            console=console,
            auto_refresh=False,
            refresh_per_second=4,
        ) as live:
            while running:
                live.update(build_display(sections))
                live.refresh()
                time.sleep(0.25)
    except KeyboardInterrupt:
        running = False
        final_count = tick_count

    console.print(f"\n[yellow]Stream stopped. Received {final_count} ticks total.[/yellow]")


if __name__ == "__main__":
    main()
