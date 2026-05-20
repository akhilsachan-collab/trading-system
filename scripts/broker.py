"""
broker.py — Upstox order execution layer (Phase 5C).

Mode-gated: TRADING_MODE must be 'sandbox' or 'live' in .env.
Raises ConfigError at import time if the value is missing or invalid —
no script can accidentally connect to the wrong environment.

Sandbox base : https://api-sandbox.upstox.com
Live base    : https://api.upstox.com

Market-data calls (get_ltp_batch) always use the live API so that exit
decisions are driven by real prices even during paper trading.

Usage:
    from scripts.broker import Broker, MODE, OrderResult
    MODE.print_startup_banner()
    broker = Broker(access_token=os.getenv("UPSTOX_ACCESS_TOKEN"))
    result = broker.place_order(proposal)

Upstox v2 API reference:
    https://upstox.com/developer/api-documentation/v2/
"""

import csv
import enum
import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

# ── Paths ─────────────────────────────────────────────────────────────────────

_SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _SCRIPTS_DIR.parent
ENV_PATH     = PROJECT_ROOT / ".env"
DB_PATH      = PROJECT_ROOT / "data" / "trading_state.db"
LOGS_DIR     = PROJECT_ROOT / "logs"

load_dotenv(ENV_PATH)

IST = timezone(timedelta(hours=5, minutes=30))

_LIVE_API_BASE    = "https://api.upstox.com"
_SANDBOX_API_BASE = "https://api-sandbox.upstox.com"

# ── Logging ───────────────────────────────────────────────────────────────────

LOGS_DIR.mkdir(exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOGS_DIR / "broker.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s: %(message)s")
)
logger = logging.getLogger("broker")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    logger.addHandler(_handler)

# ── Exceptions ────────────────────────────────────────────────────────────────


class ConfigError(RuntimeError):
    """TRADING_MODE is missing or not 'sandbox'/'live' in .env."""


class BrokerError(RuntimeError):
    """All API retry attempts exhausted."""


# ── Mode ─────────────────────────────────────────────────────────────────────


class Mode:
    """
    Reads TRADING_MODE from .env exactly once at module import.

    Any script that imports broker.py must call MODE.print_startup_banner()
    at startup so the operator always sees which environment is active.
    """

    def __init__(self) -> None:
        raw = os.getenv("TRADING_MODE", "").strip().lower()
        if raw not in ("sandbox", "live"):
            raise ConfigError(
                f"TRADING_MODE='{raw}' is not valid. "
                "Set TRADING_MODE=sandbox or TRADING_MODE=live in .env before importing broker."
            )
        self._mode = raw
        logger.info("Mode initialised: %s", raw.upper())

    @property
    def is_live(self) -> bool:
        return self._mode == "live"

    @property
    def is_sandbox(self) -> bool:
        return self._mode == "sandbox"

    @property
    def api_base_url(self) -> str:
        return _LIVE_API_BASE if self.is_live else _SANDBOX_API_BASE

    def print_startup_banner(self) -> None:
        """
        Write a coloured mode banner to stdout.
        Uses sys.stdout.buffer.write so UTF-8 box-drawing characters and emoji
        are sent as raw bytes, bypassing Windows CP1252 encoding.
        Live mode sleeps 10 seconds after the banner — last chance to Ctrl+C.
        """
        h  = b"\xe2\x94\x80"   # ─  (U+2500)
        tl = b"\xe2\x94\x8c"   # ┌
        tr = b"\xe2\x94\x90"   # ┐
        bl = b"\xe2\x94\x94"   # └
        br = b"\xe2\x94\x98"   # ┘
        vb = b"\xe2\x94\x82"   # │
        em = b"\xe2\x80\x94"   # —

        if self.is_sandbox:
            grn  = b"\xf0\x9f\x9f\xa2"   # 🟢
            w    = 50
            top  = tl + h * w + tr
            mid  = vb + b"  " + grn + b"  SANDBOX MODE " + em + b" Fake Orders Only                " + vb
            bot  = bl + h * w + br
            out  = b"\n" + top + b"\n" + mid + b"\n" + bot + b"\n\n"
            sys.stdout.buffer.write(out)
            sys.stdout.buffer.flush()
        else:
            skl  = b"\xf0\x9f\x92\x80"   # 💀
            w    = 58
            top  = tl + h * w + tr
            mid1 = vb + b"  " + skl + b"  LIVE TRADING MODE " + em + b" REAL MONEY  " + skl + b"              " + vb
            mid2 = vb + b"  Real orders will be placed with your broker.                " + vb
            mid3 = vb + b"  You have 10 seconds to abort (Ctrl+C).                     " + vb
            bot  = bl + h * w + br
            out  = b"\n" + top + b"\n" + mid1 + b"\n" + mid2 + b"\n" + mid3 + b"\n" + bot + b"\n\n"
            sys.stdout.buffer.write(out)
            sys.stdout.buffer.flush()
            for i in range(10, 0, -1):
                sys.stdout.write(f"\r  Proceeding in {i}s ...  ")
                sys.stdout.flush()
                _time.sleep(1)
            sys.stdout.write("\r" + " " * 30 + "\r")
            sys.stdout.flush()


# Module-level singleton — raises ConfigError at import if .env is misconfigured
MODE = Mode()


# ── Data models ───────────────────────────────────────────────────────────────


class OrderStatus(enum.Enum):
    PENDING   = "PENDING"
    FILLED    = "FILLED"
    PARTIAL   = "PARTIAL"
    REJECTED  = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class OrderResult:
    order_id:        str
    status:          str            # OrderStatus.value
    filled_quantity: int
    avg_price:       float
    error_msg:       Optional[str] = None


@dataclass
class BrokerPosition:
    instrument_key: str
    quantity:       int
    avg_price:      float
    ltp:            float
    pnl:            float
    product:        str             # "I" intraday / "D" delivery


# ── Broker ────────────────────────────────────────────────────────────────────


class Broker:
    """
    Thin wrapper over the Upstox v2 order and portfolio API.

    All order / position calls go to MODE.api_base_url (sandbox or live).
    Quote calls (get_ltp_batch) always go to the live Upstox API so that
    price-based exit decisions use real market data even during paper trading.

    Every request and response is logged to logs/broker.log.
    Every order action is also written to data/trading_state.db audit_log
    and logs/audit_YYYY-MM-DD.csv.
    """

    # Upstox order status strings → OrderStatus enum
    _STATUS_MAP: Dict[str, "OrderStatus"] = {
        "open":                     OrderStatus.PENDING,
        "complete":                 OrderStatus.FILLED,
        "cancelled":                OrderStatus.CANCELLED,
        "rejected":                 OrderStatus.REJECTED,
        "trigger pending":          OrderStatus.PENDING,
        "validation pending":       OrderStatus.PENDING,
        "put order req received":   OrderStatus.PENDING,
        "open pending":             OrderStatus.PENDING,
        "modified pending":         OrderStatus.PENDING,
        "cancel pending":           OrderStatus.PENDING,
        "modify pending":           OrderStatus.PENDING,
        "not modified":             OrderStatus.PENDING,
        "not cancelled":            OrderStatus.CANCELLED,
    }

    def __init__(
        self,
        access_token: str,
        sebi_algo_tagging: bool = False,
    ) -> None:
        self._token    = access_token
        self._algo_tag = sebi_algo_tagging
        self._base     = MODE.api_base_url

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept":        "application/json",
            "Content-Type":  "application/json",
        })

        self._db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.commit()

        logger.info(
            "Broker ready — mode=%s  base=%s  algo_tag=%s",
            "LIVE" if MODE.is_live else "SANDBOX",
            self._base,
            self._algo_tag,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    def _api_call(
        self,
        method: str,
        endpoint: str,
        use_live_base: bool = False,
        **kwargs,
    ) -> dict:
        """
        3 attempts, exponential backoff (1 s → 2 s → 4 s), 10 s timeout per call.
        use_live_base=True routes to the live Upstox API regardless of trading mode
        (used for market-data calls that sandbox doesn't serve).
        Raises BrokerError if all 3 attempts fail.
        """
        base = _LIVE_API_BASE if use_live_base else self._base
        url  = f"{base}{endpoint}"
        last_exc: Optional[Exception] = None

        for attempt in range(1, 4):
            try:
                resp = self._session.request(method, url, timeout=10, **kwargs)
                logger.debug("API %s %s → HTTP %d", method, endpoint, resp.status_code)
                if not resp.ok:
                    logger.warning(
                        "API %s %s HTTP %d: %s",
                        method, endpoint, resp.status_code, resp.text[:300],
                    )
                    resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    wait = 2 ** (attempt - 1)   # 1 s, 2 s
                    logger.warning(
                        "API %s %s attempt %d failed (%s) — retry in %ds",
                        method, endpoint, attempt, exc, wait,
                    )
                    _time.sleep(wait)

        logger.error(
            "API %s %s failed after 3 attempts: %s", method, endpoint, last_exc
        )
        raise BrokerError(
            f"{method} {endpoint} — 3 retries exhausted: {last_exc}"
        ) from last_exc

    def _map_product(self, segment: str) -> str:
        return "D" if segment == "SWING_EQUITY" else "I"

    def _write_audit(
        self,
        action_type: str,
        instrument_key: str,
        payload: dict,
        response: dict,
    ) -> None:
        now    = datetime.now(tz=IST)
        ts     = now.isoformat()
        detail = json.dumps({"payload": payload, "response": response})[:1000]

        try:
            self._db.execute(
                "INSERT INTO audit_log (timestamp, action_type, instrument_key, reasoning)"
                " VALUES (?, ?, ?, ?)",
                (ts, action_type, instrument_key, detail),
            )
            self._db.commit()
        except Exception as exc:
            logger.warning("audit_log DB write failed: %s", exc)

        csv_path    = LOGS_DIR / f"audit_{now.strftime('%Y-%m-%d')}.csv"
        write_header = not csv_path.exists()
        row = {
            "timestamp":      ts,
            "action_type":    action_type,
            "instrument_key": instrument_key,
            "payload":        json.dumps(payload),
            "response":       json.dumps(response)[:500],
        }
        try:
            with csv_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                if write_header:
                    w.writeheader()
                w.writerow(row)
        except Exception as exc:
            logger.warning("audit CSV write failed: %s", exc)

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(self, proposal, order_type: str = "LIMIT") -> OrderResult:
        """
        Map a ProposedTrade to a POST /v2/order/place request.

        Field names match the Upstox v2 REST API.
        Verify against: https://upstox.com/developer/api-documentation/v2/
        """
        payload = {
            "quantity":           proposal.quantity,
            "product":            self._map_product(proposal.segment),
            "validity":           "DAY",
            "price":              round(proposal.entry_price, 2) if order_type == "LIMIT" else 0,
            "tag":                "algo" if self._algo_tag else "",
            "instrument_token":   proposal.instrument_key,
            "order_type":         order_type,
            "transaction_type":   proposal.side,
            "disclosed_quantity": 0,
            "trigger_price":      0,
            "is_amo":             False,
        }
        logger.info(
            "place_order: %s %s ×%d @ %.2f  segment=%s  env=%s",
            proposal.side, proposal.instrument_key, proposal.quantity,
            proposal.entry_price, proposal.segment,
            "SANDBOX" if MODE.is_sandbox else "LIVE",
        )
        try:
            resp     = self._api_call("POST", "/v2/order/place", json=payload)
            order_id = resp.get("data", {}).get("order_id", "")
            self._write_audit("PLACE_ORDER", proposal.instrument_key, payload, resp)
            logger.info("place_order OK: order_id=%s", order_id)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.PENDING.value,
                filled_quantity=0,
                avg_price=0.0,
            )
        except BrokerError as exc:
            logger.error("place_order failed for %s: %s", proposal.instrument_key, exc)
            return OrderResult(
                order_id="",
                status=OrderStatus.REJECTED.value,
                filled_quantity=0,
                avg_price=0.0,
                error_msg=str(exc),
            )

    def place_close_order(
        self,
        instrument_key: str,
        side: str,
        quantity: int,
        segment: str,
        order_type: str = "MARKET",
    ) -> OrderResult:
        """
        Exit order. side must be opposite to the entry side.
        Defaults to MARKET so exits are immediate (no limit slip on force-close).
        """
        payload = {
            "quantity":           quantity,
            "product":            self._map_product(segment),
            "validity":           "DAY",
            "price":              0,
            "tag":                "algo" if self._algo_tag else "",
            "instrument_token":   instrument_key,
            "order_type":         order_type,
            "transaction_type":   side,
            "disclosed_quantity": 0,
            "trigger_price":      0,
            "is_amo":             False,
        }
        logger.info(
            "place_close_order: %s %s ×%d  type=%s  env=%s",
            side, instrument_key, quantity, order_type,
            "SANDBOX" if MODE.is_sandbox else "LIVE",
        )
        try:
            resp     = self._api_call("POST", "/v2/order/place", json=payload)
            order_id = resp.get("data", {}).get("order_id", "")
            self._write_audit("CLOSE_ORDER", instrument_key, payload, resp)
            logger.info("place_close_order OK: order_id=%s", order_id)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.PENDING.value,
                filled_quantity=0,
                avg_price=0.0,
            )
        except BrokerError as exc:
            logger.error("place_close_order failed for %s: %s", instrument_key, exc)
            return OrderResult(
                order_id="",
                status=OrderStatus.REJECTED.value,
                filled_quantity=0,
                avg_price=0.0,
                error_msg=str(exc),
            )

    def modify_order(
        self,
        order_id: str,
        new_price: Optional[float] = None,
        new_sl_trigger: Optional[float] = None,
    ) -> OrderResult:
        """Modify price or trigger of an open order (used for trailing stop updates)."""
        payload: dict = {"order_id": order_id}
        if new_price is not None:
            payload["price"] = round(new_price, 2)
        if new_sl_trigger is not None:
            payload["trigger_price"] = round(new_sl_trigger, 2)
        logger.info(
            "modify_order: %s  new_price=%s  new_trigger=%s",
            order_id, new_price, new_sl_trigger,
        )
        try:
            resp = self._api_call("PUT", "/v2/order/modify", json=payload)
            self._write_audit("MODIFY_ORDER", "", payload, resp)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.PENDING.value,
                filled_quantity=0,
                avg_price=0.0,
            )
        except BrokerError as exc:
            logger.error("modify_order %s failed: %s", order_id, exc)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED.value,
                filled_quantity=0,
                avg_price=0.0,
                error_msg=str(exc),
            )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open limit order. Returns True on success."""
        logger.info("cancel_order: %s", order_id)
        try:
            resp = self._api_call(
                "DELETE", "/v2/order/cancel", params={"order_id": order_id}
            )
            self._write_audit("CANCEL_ORDER", "", {"order_id": order_id}, resp)
            return True
        except BrokerError as exc:
            logger.error("cancel_order %s failed: %s", order_id, exc)
            return False

    def get_order_status(self, order_id: str) -> "OrderStatus":
        details = self.get_order_details(order_id)
        if not details:
            return OrderStatus.PENDING
        raw_st = str(details.get("status", "")).lower()
        filled = int(details.get("filled_quantity", 0) or 0)
        total  = int(details.get("quantity", 1) or 1)
        if raw_st == "open" and 0 < filled < total:
            return OrderStatus.PARTIAL
        return self._STATUS_MAP.get(raw_st, OrderStatus.PENDING)

    def get_order_details(self, order_id: str) -> dict:
        """Raw Upstox order record. Fields: status, filled_quantity, average_price, …"""
        try:
            resp = self._api_call(
                "GET", "/v2/order/details", params={"order_id": order_id}
            )
            return resp.get("data", {}) or {}
        except BrokerError:
            return {}

    # ── Quotes ────────────────────────────────────────────────────────────────

    def get_ltp(self, instrument_key: str) -> float:
        return self.get_ltp_batch([instrument_key]).get(instrument_key, 0.0)

    def get_ltp_batch(self, instrument_keys: List[str]) -> Dict[str, float]:
        """
        Batch last-traded-price fetch.
        Always uses the live Upstox API — the sandbox does not serve real prices.
        Returns {instrument_key: ltp} for each key that comes back.
        Missing keys are omitted (caller should treat absence as stale/unknown).
        """
        if not instrument_keys:
            return {}
        try:
            resp = self._api_call(
                "GET", "/v2/market-quote/quotes",
                use_live_base=True,
                params={"instrument_key": ",".join(instrument_keys)},
            )
            raw    = resp.get("data", {}) or {}
            result = {}
            for k, v in raw.items():
                norm_k = k.replace(":", "|", 1)    # "NSE_EQ:INE..." → "NSE_EQ|INE..."
                ltp    = v.get("last_price") or v.get("ltp") or 0.0
                result[norm_k] = float(ltp)
            return result
        except BrokerError:
            return {}

    # ── Portfolio ─────────────────────────────────────────────────────────────

    def get_positions(self) -> List[BrokerPosition]:
        """
        Merge short-term intraday positions and long-term holdings.
        Both calls go to MODE.api_base_url (sandbox returns synthetic positions).
        """
        result: List[BrokerPosition] = []
        for endpoint, product in [
            ("/v2/portfolio/short-term-positions", "I"),
            ("/v2/portfolio/long-term-holdings",   "D"),
        ]:
            try:
                resp = self._api_call("GET", endpoint)
                for item in resp.get("data", []) or []:
                    result.append(BrokerPosition(
                        instrument_key=str(
                            item.get("instrument_token", "")
                        ).replace(":", "|", 1),
                        quantity=int(item.get("quantity", 0) or 0),
                        avg_price=float(item.get("average_price", 0) or 0),
                        ltp=float(item.get("last_price", 0) or 0),
                        pnl=float(item.get("pnl", 0) or 0),
                        product=product,
                    ))
            except BrokerError as exc:
                logger.warning("get_positions %s failed: %s", endpoint, exc)
        return result


# ── Sandbox self-test (ask before running: calls Upstox API) ─────────────────

if __name__ == "__main__":
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")

    print("=== broker.py self-test (sandbox) ===\n")
    MODE.print_startup_banner()

    _token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if not _token:
        print("ERROR: UPSTOX_ACCESS_TOKEN not set in .env")
        _sys.exit(1)

    _broker = Broker(access_token=_token)

    # Synthetic ProposedTrade (duck-typed — no import needed)
    class _FakeTrade:
        instrument_key = "NSE_EQ|INE040A01034"   # HDFCBANK
        side           = "BUY"
        quantity       = 1
        entry_price    = 1600.00
        stop_loss      = 1585.00
        target         = 1630.00
        strategy       = "ORB"
        segment        = "EQUITY_INTRADAY"

    _p = _FakeTrade()
    print(f"[1] Placing sandbox LIMIT order: {_p.side} {_p.instrument_key} ×{_p.quantity} @ {_p.entry_price}")
    _r = _broker.place_order(_p)
    print(f"    OrderResult: {_r}\n")

    if _r.order_id:
        print(f"[2] Checking order status: {_r.order_id}")
        _st = _broker.get_order_status(_r.order_id)
        print(f"    Status: {_st}\n")

        print(f"[3] Modifying order to price 1601.00")
        _m = _broker.modify_order(_r.order_id, new_price=1601.00)
        print(f"    ModifyResult: {_m}\n")

        print(f"[4] Cancelling order {_r.order_id}")
        _ok = _broker.cancel_order(_r.order_id)
        print(f"    Cancelled: {_ok}\n")
    else:
        print("    (no order_id returned — check sandbox credentials)\n")

    print("[5] LTP batch (live market data):")
    _ltps = _broker.get_ltp_batch(["NSE_EQ|INE040A01034", "NSE_INDEX|Nifty 50"])
    for _k, _v in _ltps.items():
        print(f"    {_k}: {_v:.2f}")

    print("\nSelf-test complete.")
