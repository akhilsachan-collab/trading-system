"""
orb.py — Opening Range Breakout (ORB) strategy.

Range: first 30 minutes of the session (09:15–09:44 IST).
Breakout: LTP > range_high × 1.001 with volume confirmation.
Direction: Long only (v1).
Universe: 10 equity stocks in watchlist.json.
"""

import logging
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from strategies.base import BaseStrategy, logger, IST
from risk_engine import RiskEngine, ProposedTrade


class OpeningRangeBreakout(BaseStrategy):
    """
    Generates long intraday proposals when price breaks above the first-30-min
    range with volume confirmation. One signal per stock per day.
    """

    @property
    def name(self) -> str:
        return "ORB"

    @property
    def segments(self) -> List[str]:
        return ["EQUITY_INTRADAY"]

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _build_orb_range(
        df: pd.DataFrame,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Compute ORB high and low from 1-minute candles in the 09:15–09:44 window.
        Returns (None, None) if no candles exist in that window.
        """
        ts = df["timestamp"]
        mask = (
            (ts.dt.hour == 9)
            & (ts.dt.minute >= 15)
            & (ts.dt.minute < 45)
        )
        orb = df[mask]
        if orb.empty:
            return None, None
        return float(orb["high"].max()), float(orb["low"].min())

    @staticmethod
    def _resample_to_5min(df: pd.DataFrame) -> pd.DataFrame:
        """Aggregate 1-minute candles into 5-minute bars."""
        return (
            df.set_index("timestamp")
            .resample("5min")
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            )
            .dropna(subset=["close"])
            .reset_index()
        )

    def _volume_confirmed(
        self,
        df_intra: pd.DataFrame,
        df_daily: pd.DataFrame,
    ) -> bool:
        """
        Return True if the latest 5-min bar's volume exceeds
        1.5 × (20-day avg daily volume ÷ 75 five-min bars per session).
        """
        if len(df_daily) < 20:
            return False
        avg_daily = df_daily["volume"].tail(20).mean()
        if avg_daily <= 0:
            return False
        threshold = avg_daily / 75.0

        df5 = self._resample_to_5min(df_intra)
        if df5.empty:
            return False
        return float(df5["volume"].iloc[-1]) > 1.5 * threshold

    # ── Per-stock evaluation ──────────────────────────────────────────────────

    def _evaluate_stock(self, key: str) -> Optional[ProposedTrade]:
        # ── 1-minute intraday candles ─────────────────────────────────────────
        df_intra = self.get_intraday_candles(key, "1minute")
        if df_intra.empty or len(df_intra) < 5:
            logger.debug("ORB: no intraday data for %s", key)
            return None

        # ── ORB range ─────────────────────────────────────────────────────────
        range_high, range_low = self._build_orb_range(df_intra)
        if range_high is None:
            logger.debug("ORB: no candles in 09:15–09:44 window for %s", key)
            return None

        # ── Live quote ────────────────────────────────────────────────────────
        quote = self.get_current_quote(key)
        ltp   = float(quote.get("last_price", 0.0) or 0.0)
        if ltp <= 0:
            return None

        # ── Breakout check ────────────────────────────────────────────────────
        breakout_level = range_high * 1.001
        if ltp <= breakout_level:
            logger.debug(
                "ORB: %s no breakout  ltp=%.2f  level=%.2f", key, ltp, breakout_level
            )
            return None

        # ── Daily candles for ATR(14) and volume baseline ─────────────────────
        df_daily = self.get_historical_candles(key, "day", 25)
        if len(df_daily) < 15:
            logger.debug("ORB: insufficient daily history for %s (%d rows)", key, len(df_daily))
            return None
        df_daily = self.calculate_indicators(df_daily)

        # ── Volume confirmation ───────────────────────────────────────────────
        if not self._volume_confirmed(df_intra, df_daily):
            logger.debug("ORB: volume not confirmed for %s", key)
            return None

        # ── ATR-based SL ──────────────────────────────────────────────────────
        atr_series = df_daily["atr_14"].dropna()
        if atr_series.empty:
            return None
        atr = float(atr_series.iloc[-1])
        if atr <= 0:
            return None

        # SL = tighter (higher) of: entry − 1.5×ATR  OR  range_low
        sl = max(ltp - 1.5 * atr, range_low)
        if sl >= ltp:
            logger.debug("ORB: degenerate SL (%.2f ≥ ltp %.2f) for %s", sl, ltp, key)
            return None

        target = ltp + 2.0 * (ltp - sl)

        logger.info(
            "ORB: signal %s  ltp=%.2f  range=[%.2f–%.2f]  sl=%.2f  target=%.2f",
            key, ltp, range_low, range_high, sl, target,
        )
        return self.propose(key, "BUY", ltp, sl, target, "EQUITY_INTRADAY")

    # ── Main evaluation entry-point ───────────────────────────────────────────

    def evaluate(self) -> List[ProposedTrade]:
        # Use engine's time oracle so test overrides are respected
        now = self.engine._now()

        if now.time() < time(9, 45):
            logger.debug("ORB: range still building (before 09:45 IST)")
            return []

        proposals: List[ProposedTrade] = []
        for key in self.equity_keys:
            if self._is_triggered(key):
                continue
            try:
                p = self._evaluate_stock(key)
                if p is not None:
                    proposals.append(p)
                    self._mark_triggered(key)
            except Exception as exc:
                logger.error("ORB: unhandled error for %s: %s", key, exc, exc_info=True)

        return proposals


# ── Tests ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger("strategies").setLevel(logging.CRITICAL)
    logging.getLogger("risk_engine").setLevel(logging.CRITICAL)

    PASS = "\033[32mPASS\033[0m"
    FAIL = "\033[31mFAIL\033[0m"

    T_MARKET = datetime(2026, 5, 20, 10, 30, 0, tzinfo=IST)

    def _make_intraday_df(range_high=1400.0, range_low=1380.0, ltp=1415.0, daily_avg_vol=2_000_000):
        """
        Build a fake 1-minute candle DataFrame.
        ORB range (09:15–09:44): high=range_high, low=range_low
        Current candle (~10:00): close=ltp, volume=daily_avg_vol/10 (well above 1.5× threshold)
        """
        import numpy as np
        from datetime import timedelta

        base = datetime(2026, 5, 20, 9, 15, 0, tzinfo=IST)
        rows = []
        # 30 range candles (09:15–09:44)
        for i in range(30):
            ts = base + timedelta(minutes=i)
            rows.append({
                "timestamp": ts,
                "open":  range_low + 5.0,
                "high":  range_high - 1.0 + (5.0 if i == 15 else 0.0),
                "low":   range_low,
                "close": range_low + 10.0,
                "volume": daily_avg_vol / 75 * 0.8,  # below threshold in range
                "open_interest": 0.0,
            })
        # Breakout candles (09:45–10:30)
        breakout_vol = daily_avg_vol / 75 * 3.0   # 3× threshold → passes 1.5× check
        for i in range(46):
            ts = base + timedelta(minutes=30 + i)
            rows.append({
                "timestamp": ts,
                "open":  ltp - 2.0,
                "high":  ltp + 1.0,
                "low":   ltp - 3.0,
                "close": ltp,
                "volume": breakout_vol,
                "open_interest": 0.0,
            })
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df

    def _make_daily_df(n=25, avg_vol=2_000_000, atr_approx=10.0):
        """Build fake daily candles with a consistent price/volume baseline."""
        import numpy as np
        from datetime import timedelta

        base_date = datetime(2026, 4, 1, 0, 0, 0, tzinfo=IST)
        closes = [1380.0 + i * 0.5 for i in range(n)]
        rows = []
        for i, c in enumerate(closes):
            rows.append({
                "timestamp": base_date + timedelta(days=i),
                "open":   c - 5.0,
                "high":   c + atr_approx,
                "low":    c - atr_approx,
                "close":  c,
                "volume": avg_vol * (0.9 + 0.2 * (i % 3 == 0)),
                "open_interest": 0.0,
            })
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df

    # ── Test helpers ──────────────────────────────────────────────────────────

    def make_strategy(intra_df, daily_df, ltp):
        """Build a testable ORB strategy with all live calls mocked."""
        from risk_engine import RiskEngine
        engine = RiskEngine(db_path=":memory:")
        engine._kill_switch_override = True
        engine._vix_override         = 15.0
        engine._time_override        = T_MARKET

        strat = OpeningRangeBreakout(engine)
        strat._triggered_today.clear()

        # Override only the first equity key for the test
        strat.equity_keys = ["NSE_EQ|INE002A01018"]

        strat.get_intraday_candles  = lambda key, interval="1minute": intra_df
        strat.get_historical_candles = lambda key, interval="day", lookback_days=25: daily_df
        strat.get_current_quote     = lambda key: {"last_price": ltp}

        return strat

    results = []

    def run(name, strat, expect_count):
        proposals = strat.evaluate()
        ok = len(proposals) == expect_count
        results.append((name, ok))
        verdict = PASS if ok else FAIL
        detail = f"got {len(proposals)} proposal(s), expected {expect_count}"
        print(f"  {verdict}  {name}")
        print(f"         → {detail}")
        if proposals:
            p = proposals[0]
            print(f"            entry={p.entry_price:.2f}  sl={p.stop_loss:.2f}  "
                  f"target={p.target:.2f}  qty={p.quantity}  risk=₹{p.trade_risk:.0f}")
        print()

    print("\n" + "═" * 55)
    print("  ORB Strategy — Test Suite")
    print("═" * 55 + "\n")

    # T1: Clean breakout — expect 1 proposal
    intra = _make_intraday_df(range_high=1400.0, range_low=1380.0, ltp=1415.0)
    daily = _make_daily_df()
    run("T1 Clean breakout above range", make_strategy(intra, daily, 1415.0), 1)

    # T2: LTP below breakout level — expect 0
    intra2 = _make_intraday_df(range_high=1400.0, range_low=1380.0, ltp=1399.0)
    run("T2 No breakout (LTP below range high)", make_strategy(intra2, daily, 1399.0), 0)

    # T3: Already triggered today — expect 0
    strat3 = make_strategy(intra, daily, 1415.0)
    strat3._mark_triggered("NSE_EQ|INE002A01018")
    run("T3 Already triggered today", strat3, 0)

    # T4: Breakout but low volume — expect 0
    intra4 = _make_intraday_df(
        range_high=1400.0, range_low=1380.0, ltp=1415.0,
        daily_avg_vol=2_000_000,
    )
    # Zero ALL candle volumes so every 5-min bar is well below the threshold
    intra4["volume"] = 50.0
    run("T4 Volume not confirmed", make_strategy(intra4, daily, 1415.0), 0)

    # T5: Too early (before 09:45) — expect 0
    strat5 = make_strategy(intra, daily, 1415.0)
    strat5.engine._time_override = datetime(2026, 5, 20, 9, 30, 0, tzinfo=IST)
    run("T5 Before 09:45 — range not complete", strat5, 0)

    print("─" * 55)
    passed = sum(1 for _, ok in results if ok)
    if passed == len(results):
        print(f"  \033[32mAll {len(results)} ORB tests passed.\033[0m\n")
    else:
        failed = [n for n, ok in results if not ok]
        print(f"  \033[31m{len(results) - passed} failed: {', '.join(failed)}\033[0m\n")
        sys.exit(1)
