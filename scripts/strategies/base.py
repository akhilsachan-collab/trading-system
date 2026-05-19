"""
base.py — Abstract base class for all trading strategies.

Provides shared data access (Upstox REST + local history cache), manual
technical indicator computation (pure pandas — no pandas-ta dependency),
and a propose() helper that feeds ProposedTrade objects to the RiskEngine
before returning them to the caller.

All concrete strategies inherit from BaseStrategy and implement evaluate().
"""

import json
import logging
import logging.handlers
import os
import sys
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote as url_quote

import pandas as pd
import requests
from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────
# scripts/strategies/base.py  →  parent = strategies/  →  parent = scripts/  →  parent = project root
_STRATEGIES_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR    = _STRATEGIES_DIR.parent
PROJECT_ROOT    = _SCRIPTS_DIR.parent

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from risk_engine import RiskEngine, ProposedTrade  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────
ENV_PATH         = PROJECT_ROOT / ".env"
WATCHLIST_PATH   = PROJECT_ROOT / "watchlist.json"
HISTORY_DIR      = PROJECT_ROOT / "data" / "history"
INSTRUMENTS_PATH = PROJECT_ROOT / "data" / "instruments.json"
LOGS_DIR         = PROJECT_ROOT / "logs"
UPSTOX_V2        = "https://api.upstox.com/v2"
UPSTOX_V3        = "https://api.upstox.com/v3"

IST = timezone(timedelta(hours=5, minutes=30))

# Maps our interval strings to the (unit, api_interval) pair used by Upstox V3
INTERVAL_MAP: Dict[str, Tuple[str, str]] = {
    "1minute":  ("minutes", "1"),
    "3minute":  ("minutes", "3"),
    "5minute":  ("minutes", "5"),
    "15minute": ("minutes", "15"),
    "30minute": ("minutes", "30"),
    "1hour":    ("hours",   "1"),
    "day":      ("days",    "1"),
    "week":     ("weeks",   "1"),
}

# ── Logging ───────────────────────────────────────────────────────────────────
LOGS_DIR.mkdir(exist_ok=True)
_handler = logging.handlers.RotatingFileHandler(
    LOGS_DIR / "strategies.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s: %(message)s")
)
logger = logging.getLogger("strategies")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    logger.addHandler(_handler)


# ── Manual indicator functions ────────────────────────────────────────────────
# Pure pandas — avoids numpy.bool / Python 3.14 incompatibility in pandas-ta.

def _calc_sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def _calc_ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _calc_rsi(series: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI using EWM with alpha = 1/n."""
    delta    = series.diff()
    gain     = delta.clip(lower=0.0)
    loss     = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0.0, 1e-10)
    return 100.0 - (100.0 / (1.0 + rs))


def _calc_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range using Wilder's EWM."""
    h    = df["high"]
    l    = df["low"]
    prev = df["close"].shift(1)
    tr   = pd.concat(
        [h - l, (h - prev).abs(), (l - prev).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()


def _calc_adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average Directional Index."""
    h, l, prev_h, prev_l, prev_c = (
        df["high"], df["low"],
        df["high"].shift(1), df["low"].shift(1), df["close"].shift(1),
    )
    up   = h - prev_h
    down = prev_l - l

    plus_dm  = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up > down)   & (up   > 0)] = up[(up > down)   & (up   > 0)]
    minus_dm[(down > up)  & (down > 0)] = down[(down > up) & (down > 0)]

    tr = pd.concat(
        [h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1
    ).max(axis=1)
    sm_tr    = tr.ewm(alpha=1.0 / n,        min_periods=n, adjust=False).mean()
    sm_plus  = plus_dm.ewm(alpha=1.0 / n,  min_periods=n, adjust=False).mean()
    sm_minus = minus_dm.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()

    plus_di  = 100.0 * sm_plus  / sm_tr.replace(0.0, 1e-10)
    minus_di = 100.0 * sm_minus / sm_tr.replace(0.0, 1e-10)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-10)
    return dx.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()


def _calc_bbands(
    series: pd.Series, n: int = 20, std: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (lower, mid, upper) Bollinger Bands."""
    mid   = series.rolling(n, min_periods=n).mean()
    sigma = series.rolling(n, min_periods=n).std()
    return mid - std * sigma, mid, mid + std * sigma


def _sanitize_key(instrument_key: str) -> str:
    """Convert 'NSE_INDEX|Nifty 50' → 'NSE_INDEX_Nifty_50' for use in filenames."""
    return instrument_key.replace("|", "_").replace(" ", "_")


# ── BaseStrategy ──────────────────────────────────────────────────────────────

class BaseStrategy(ABC):
    """
    Abstract base for ORB, Momentum, and MeanReversion strategies.

    Subclasses must implement:
        evaluate() -> List[ProposedTrade]
        name       -> str   (property)
        segments   -> List[str]  (property)
    """

    def __init__(self, engine: RiskEngine) -> None:
        load_dotenv(ENV_PATH)
        self.engine: RiskEngine = engine

        wl = self.load_watchlist()
        self.watchlist:  dict       = wl
        self.equity_keys: List[str] = [s["key"] for s in wl.get("stocks", [])]
        # Only Nifty 50 and Nifty Bank are in scope for strategy signals
        self.index_keys: List[str] = [
            i["key"] for i in wl.get("indices", [])
            if i["key"] in ("NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank")
        ]

        self._triggered_today: set = set()
        self._triggered_date:  date = date.today()

    # ── Abstract interface ────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def segments(self) -> List[str]: ...

    @abstractmethod
    def evaluate(self) -> List[ProposedTrade]: ...

    # ── One-trade-per-instrument-per-day ──────────────────────────────────────

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if today != self._triggered_date:
            self._triggered_today.clear()
            self._triggered_date = today

    def _is_triggered(self, key: str) -> bool:
        self._reset_if_new_day()
        return key in self._triggered_today

    def _mark_triggered(self, key: str) -> None:
        self._triggered_today.add(key)

    # ── Watchlist ─────────────────────────────────────────────────────────────

    @staticmethod
    def load_watchlist() -> dict:
        return json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))

    def _token(self) -> str:
        return os.getenv("UPSTOX_ACCESS_TOKEN", "")

    # ── Historical candles (daily cache + optional API refresh) ───────────────

    def get_historical_candles(
        self,
        instrument_key: str,
        interval: str = "day",
        lookback_days: int = 30,
    ) -> pd.DataFrame:
        """
        Return a DataFrame of OHLCV candles covering approximately `lookback_days`.
        Reads from data/history/ CSV cache; calls the Upstox V3 API if the cache
        is missing or stale (latest date > 4 calendar days ago).
        """
        today     = date.today()
        from_date = today - timedelta(days=lookback_days + 10)  # pad for weekends/holidays
        path      = HISTORY_DIR / f"{_sanitize_key(instrument_key)}_{interval}.csv"

        cached = self._load_cache(path, from_date, today)
        if cached is not None:
            return cached

        df = self._fetch_candles_api(instrument_key, interval, from_date.isoformat(), today.isoformat())
        if not df.empty:
            self._save_to_cache(path, df)
        return df.tail(lookback_days + 5).reset_index(drop=True)

    def get_intraday_candles(
        self,
        instrument_key: str,
        interval: str = "1minute",
    ) -> pd.DataFrame:
        """
        Fetch today's intraday candles live from the Upstox API.
        Not cached — always fresh.
        """
        today = date.today().isoformat()
        return self._fetch_candles_api(instrument_key, interval, today, today)

    def _fetch_candles_api(
        self,
        instrument_key: str,
        interval: str,
        from_str: str,
        to_str: str,
    ) -> pd.DataFrame:
        token = self._token()
        if not token:
            logger.warning("%s: no access token — candle fetch skipped for %s", self.name, instrument_key)
            return pd.DataFrame()
        try:
            unit, api_iv = INTERVAL_MAP.get(interval, ("days", "1"))
            encoded = url_quote(instrument_key, safe="")
            url = f"{UPSTOX_V3}/historical-candle/{encoded}/{unit}/{api_iv}/{to_str}/{from_str}"
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=15,
            )
            if not resp.ok:
                logger.warning(
                    "%s: candle API HTTP %d for %s", self.name, resp.status_code, instrument_key
                )
                return pd.DataFrame()
            body = resp.json()
            if body.get("status") != "success":
                logger.warning("%s: unexpected candle API response for %s", self.name, instrument_key)
                return pd.DataFrame()
            candles = body.get("data", {}).get("candles", [])
            if not candles:
                return pd.DataFrame()
            rows = [
                {
                    "timestamp":     c[0],
                    "open":          float(c[1]),
                    "high":          float(c[2]),
                    "low":           float(c[3]),
                    "close":         float(c[4]),
                    "volume":        float(c[5]),
                    "open_interest": float(c[6]) if len(c) > 6 else 0.0,
                }
                for c in candles
            ]
            df = pd.DataFrame(rows)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            return df.sort_values("timestamp").reset_index(drop=True)
        except Exception as exc:
            logger.warning("%s: candle fetch error for %s: %s", self.name, instrument_key, exc)
            return pd.DataFrame()

    @staticmethod
    def _load_cache(
        path: Path, from_date: date, to_date: date
    ) -> Optional[pd.DataFrame]:
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            if df.empty:
                return None
            latest = df["timestamp"].dt.date.max()
            # Accept if within 4 calendar days (handles long weekends)
            if (to_date - latest).days > 4:
                return None
            return df[df["timestamp"].dt.date >= from_date].reset_index(drop=True)
        except Exception:
            return None

    @staticmethod
    def _save_to_cache(path: Path, new_df: pd.DataFrame) -> None:
        try:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            if path.exists():
                existing = pd.read_csv(path)
                existing["timestamp"] = pd.to_datetime(existing["timestamp"])
                combined = (
                    pd.concat([existing, new_df], ignore_index=True)
                    .drop_duplicates(subset=["timestamp"])
                    .sort_values("timestamp")
                    .reset_index(drop=True)
                )
                combined.to_csv(path, index=False)
            else:
                new_df.to_csv(path, index=False)
        except Exception as exc:
            logger.warning("Cache write failed for %s: %s", path.name, exc)

    # ── Live quotes ───────────────────────────────────────────────────────────

    def get_current_quote(self, instrument_key: str) -> dict:
        result = self.get_current_quotes_batch([instrument_key])
        return result.get(instrument_key, {})

    def get_current_quotes_batch(self, instrument_keys: List[str]) -> Dict[str, dict]:
        token = self._token()
        if not token:
            return {}
        try:
            resp = requests.get(
                f"{UPSTOX_V2}/market-quote/quotes",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"instrument_key": ",".join(instrument_keys)},
                timeout=10,
            )
            if not resp.ok:
                logger.warning("%s: quote API HTTP %d", self.name, resp.status_code)
                return {}
            raw = resp.json().get("data", {})
            # Upstox returns keys with ":" separator; normalise to "|"
            return {k.replace(":", "|", 1): v for k, v in raw.items()}
        except Exception as exc:
            logger.warning("%s: quote fetch error: %s", self.name, exc)
            return {}

    # ── Indicators ────────────────────────────────────────────────────────────

    def calculate_indicators(
        self, df: pd.DataFrame, intraday: bool = False
    ) -> pd.DataFrame:
        """
        Add indicator columns to a copy of df. Returns the copy.
        Requires: open, high, low, close, volume columns.
        """
        df = df.copy()
        df["rsi_14"]   = _calc_rsi(df["close"])
        df["atr_14"]   = _calc_atr(df)
        df["adx_14"]   = _calc_adx(df)
        df["sma_20"]   = _calc_sma(df["close"], 20)
        df["sma_50"]   = _calc_sma(df["close"], 50)
        df["ema_9"]    = _calc_ema(df["close"], 9)
        df["ema_21"]   = _calc_ema(df["close"], 21)
        ll, mid, uu    = _calc_bbands(df["close"])
        df["bb_lower"] = ll
        df["bb_mid"]   = mid
        df["bb_upper"] = uu
        if intraday and "volume" in df.columns:
            tp = (df["high"] + df["low"] + df["close"]) / 3.0
            cumvol = df["volume"].cumsum().replace(0.0, 1e-10)
            df["vwap"] = (tp * df["volume"]).cumsum() / cumvol
        return df

    # ── Trade proposal ────────────────────────────────────────────────────────

    def propose(
        self,
        instrument_key: str,
        side: str,
        entry: float,
        sl: float,
        target: float,
        segment: str,
        signal_timestamp: Optional[datetime] = None,
    ) -> Optional[ProposedTrade]:
        """
        Size a trade using the RiskEngine's suggestion, then run it through
        validate(). Returns the ProposedTrade only if the engine allows it.
        """
        quantity = self.engine.get_position_size_suggestion(
            instrument_key=instrument_key,
            entry_price=entry,
            stop_loss=sl,
            strategy=self.name,
            segment=segment,
        )
        if quantity <= 0:
            logger.debug(
                "%s: sizing=0 for %s (entry=%.2f sl=%.2f) — skipped",
                self.name, instrument_key, entry, sl,
            )
            return None

        proposal = ProposedTrade(
            instrument_key=instrument_key,
            side=side,
            quantity=quantity,
            entry_price=entry,
            stop_loss=sl,
            target=target,
            strategy=self.name,
            segment=segment,
            signal_timestamp=signal_timestamp or datetime.now(tz=IST),
        )
        result = self.engine.validate(proposal)
        if result.allowed:
            logger.info(
                "%s: PROPOSED %s %s ×%d  entry=%.2f  sl=%.2f  target=%.2f  risk=₹%.0f",
                self.name, side, instrument_key, quantity, entry, sl, target,
                proposal.trade_risk,
            )
            return proposal
        logger.info(
            "%s: blocked for %s — %s",
            self.name, instrument_key, result.reason,
        )
        return None
