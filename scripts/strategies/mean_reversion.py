"""
mean_reversion.py — Mean Reversion strategy.

Signal: RSI(14) < 30 (long) or > 70 (short), confirmed by Bollinger Band
extremes, volume, low ADX (non-trending), and no 3-day same-direction streak.

Universe: 10 equity stocks. Index instruments are deferred to v2.
Direction: Both long and short.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from strategies.base import BaseStrategy, logger, IST
from risk_engine import RiskEngine, ProposedTrade

ADX_THRESHOLD  = 25.0
RSI_LONG_MAX   = 30.0
RSI_SHORT_MIN  = 70.0
VOLUME_MULT    = 1.5
LOOKBACK_DAYS  = 35


class MeanReversion(BaseStrategy):
    """
    Generates long and short proposals when price is at Bollinger Band extremes
    with RSI confirmation and a non-trending (low ADX) environment.
    """

    @property
    def name(self) -> str:
        return "MeanReversion"

    @property
    def segments(self) -> List[str]:
        return ["EQUITY_INTRADAY"]

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _streak_direction(df: pd.DataFrame) -> str:
        """
        Return 'up', 'down', or 'neutral' based on the last 3 daily closes.
        'up'   → 3 consecutive higher closes  (blocks short signal)
        'down' → 3 consecutive lower closes   (blocks long signal)
        """
        if len(df) < 4:
            return "neutral"
        diffs = df["close"].tail(4).diff().dropna()
        if len(diffs) < 3:
            return "neutral"
        last3 = diffs.tail(3)
        if (last3 > 0).all():
            return "up"
        if (last3 < 0).all():
            return "down"
        return "neutral"

    # ── Per-instrument evaluation ─────────────────────────────────────────────

    def _evaluate_instrument(self, key: str) -> Optional[ProposedTrade]:
        df = self.get_historical_candles(key, "day", LOOKBACK_DAYS)
        if len(df) < 25:
            logger.debug("MeanReversion: insufficient history for %s (%d rows)", key, len(df))
            return None

        df   = self.calculate_indicators(df)
        last = df.iloc[-1]

        rsi    = last.get("rsi_14")
        adx    = last.get("adx_14")
        atr    = last.get("atr_14")
        bb_lo  = last.get("bb_lower")
        bb_hi  = last.get("bb_upper")
        close  = float(last["close"])
        volume = float(last["volume"])

        # Any NaN indicator means insufficient history — skip
        if any(pd.isna(v) for v in [rsi, adx, atr, bb_lo, bb_hi]):
            return None

        rsi, adx, atr, bb_lo, bb_hi = (
            float(rsi), float(adx), float(atr), float(bb_lo), float(bb_hi)
        )

        # ADX filter: skip if market is trending
        if adx > ADX_THRESHOLD:
            logger.debug(
                "MeanReversion: %s ADX=%.1f > %.0f — trending, skip", key, adx, ADX_THRESHOLD
            )
            return None

        # Volume filter
        avg_vol = df["volume"].tail(20).mean()
        if avg_vol > 0 and volume < VOLUME_MULT * avg_vol:
            logger.debug("MeanReversion: %s volume not confirmed", key)
            return None

        streak = self._streak_direction(df)

        # ── Long signal ───────────────────────────────────────────────────────
        if rsi < RSI_LONG_MAX and close < bb_lo and streak != "down":
            quote = self.get_current_quote(key)
            entry = float(quote.get("last_price", 0.0) or 0.0)
            if entry <= 0:
                return None

            sig_low = float(df["low"].tail(20).min())
            sl      = max(entry - 1.5 * atr, sig_low)
            if sl >= entry:
                return None

            target = entry + 1.0 * (entry - sl)   # 1:1 RR
            logger.info(
                "MeanReversion: LONG signal %s  rsi=%.1f  adx=%.1f  close=%.2f  bb_lo=%.2f",
                key, rsi, adx, close, bb_lo,
            )
            return self.propose(key, "BUY", entry, sl, target, "EQUITY_INTRADAY")

        # ── Short signal ──────────────────────────────────────────────────────
        if rsi > RSI_SHORT_MIN and close > bb_hi and streak != "up":
            quote = self.get_current_quote(key)
            entry = float(quote.get("last_price", 0.0) or 0.0)
            if entry <= 0:
                return None

            sig_high = float(df["high"].tail(20).max())
            sl       = min(entry + 1.5 * atr, sig_high)
            if sl <= entry:
                return None

            target = entry - 1.0 * (sl - entry)   # 1:1 RR
            logger.info(
                "MeanReversion: SHORT signal %s  rsi=%.1f  adx=%.1f  close=%.2f  bb_hi=%.2f",
                key, rsi, adx, close, bb_hi,
            )
            return self.propose(key, "SELL", entry, sl, target, "EQUITY_INTRADAY")

        return None

    # ── Main evaluation entry-point ───────────────────────────────────────────

    def evaluate(self) -> List[ProposedTrade]:
        proposals: List[ProposedTrade] = []

        for key in self.equity_keys:
            if self._is_triggered(key):
                continue
            # Index instruments deferred to v2 (requires options routing complexity)
            if key.startswith("NSE_INDEX"):
                logger.debug("MeanReversion: skipping index %s (v2 feature)", key)
                continue
            try:
                p = self._evaluate_instrument(key)
                if p is not None:
                    proposals.append(p)
                    self._mark_triggered(key)
            except Exception as exc:
                logger.error("MeanReversion: unhandled error for %s: %s", key, exc, exc_info=True)

        return proposals


# ── Tests ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import logging
    sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger("strategies").setLevel(logging.CRITICAL)
    logging.getLogger("risk_engine").setLevel(logging.CRITICAL)

    import numpy as np

    PASS = "\033[32mPASS\033[0m"
    FAIL = "\033[31mFAIL\033[0m"
    T_MARKET = datetime(2026, 5, 20, 11, 0, 0, tzinfo=IST)

    def _make_oversold_df(n=35):
        """
        Candles designed to produce RSI < 30, ADX < 25, price < lower BB.

        Structure:
          - First 20 bars: range-bound ~100 (gives low ADX, BB settled)
          - Next 12 bars: steep decline from 100 → 80 (drives RSI very low)
          - Volume: last bar is 3× average (confirms volume filter)
        """
        from datetime import timedelta
        start = datetime(2026, 3, 1, 0, 0, 0, tzinfo=IST)
        rows = []
        # Range-bound phase
        for i in range(20):
            c = 100.0 + (i % 4 - 2) * 0.5   # oscillates ±1 around 100
            rows.append({
                "timestamp": start + timedelta(days=i),
                "open": c - 0.3, "high": c + 0.8, "low": c - 0.8, "close": c,
                "volume": 1_000_000.0, "open_interest": 0.0,
            })
        # Sharp decline phase
        for i in range(15):
            c = 100.0 - (i + 1) * 1.35    # 100 → ~79.75
            rows.append({
                "timestamp": start + timedelta(days=20 + i),
                "open": c + 0.3, "high": c + 0.5, "low": c - 0.5, "close": c,
                "volume": 1_000_000.0 if i < 14 else 3_000_000.0,  # spike on last bar
                "open_interest": 0.0,
            })
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df

    def _make_overbought_df(n=35):
        """Mirror of oversold: range then steep rise → RSI > 70."""
        from datetime import timedelta
        start = datetime(2026, 3, 1, 0, 0, 0, tzinfo=IST)
        rows = []
        for i in range(20):
            c = 100.0 + (i % 4 - 2) * 0.5
            rows.append({
                "timestamp": start + timedelta(days=i),
                "open": c - 0.3, "high": c + 0.8, "low": c - 0.8, "close": c,
                "volume": 1_000_000.0, "open_interest": 0.0,
            })
        for i in range(15):
            c = 100.0 + (i + 1) * 1.35
            rows.append({
                "timestamp": start + timedelta(days=20 + i),
                "open": c - 0.3, "high": c + 0.5, "low": c - 0.5, "close": c,
                "volume": 1_000_000.0 if i < 14 else 3_000_000.0,
                "open_interest": 0.0,
            })
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df

    def _make_trending_df(n=35):
        """Steadily rising candles → ADX > 25, should not trigger."""
        from datetime import timedelta
        start = datetime(2026, 3, 1, 0, 0, 0, tzinfo=IST)
        rows = []
        for i in range(n):
            c = 100.0 + i * 0.8
            rows.append({
                "timestamp": start + timedelta(days=i),
                "open": c - 0.3, "high": c + 1.5, "low": c - 1.5, "close": c,
                "volume": 1_000_000.0, "open_interest": 0.0,
            })
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df

    def _verify_indicators(df_raw):
        """Show indicator values on the last row for test debugging."""
        from strategies.base import _calc_rsi, _calc_atr, _calc_adx, _calc_bbands
        df = df_raw.copy()
        df["rsi"] = _calc_rsi(df["close"])
        df["atr"] = _calc_atr(df)
        df["adx"] = _calc_adx(df)
        ll, mid, uu = _calc_bbands(df["close"])
        df["bb_lo"] = ll
        last = df.iloc[-1]
        return last["rsi"], last["adx"], last["close"], last["bb_lo"]

    def make_strategy(daily_df, ltp=None):
        engine = RiskEngine(db_path=":memory:")
        engine._kill_switch_override = True
        engine._vix_override         = 15.0
        engine._time_override        = T_MARKET

        strat = MeanReversion(engine)
        strat.equity_keys = ["NSE_EQ|INE002A01018"]
        strat.index_keys  = []

        effective_ltp = ltp if ltp is not None else float(daily_df["close"].iloc[-1])
        strat.get_historical_candles = lambda key, interval="day", lookback_days=35: daily_df
        strat.get_current_quote      = lambda key: {"last_price": effective_ltp}
        return strat

    results = []

    def run(name, strat, expect_count):
        proposals = strat.evaluate()
        ok = len(proposals) == expect_count
        results.append((name, ok))
        verdict = PASS if ok else FAIL
        print(f"  {verdict}  {name}")
        print(f"         → got {len(proposals)} proposal(s), expected {expect_count}")
        if proposals:
            p = proposals[0]
            print(f"            {p.side}  entry={p.entry_price:.2f}  sl={p.stop_loss:.2f}  "
                  f"target={p.target:.2f}  qty={p.quantity}")
        print()

    print("\n" + "═" * 55)
    print("  Mean Reversion Strategy — Test Suite")
    print("═" * 55 + "\n")

    oversold_df   = _make_oversold_df()
    overbought_df = _make_overbought_df()
    trending_df   = _make_trending_df()

    # Print indicator values for transparency
    rsi, adx, close, bb_lo = _verify_indicators(oversold_df)
    print(f"  [info] Oversold data last row: RSI={rsi:.1f}  ADX={adx:.1f}  "
          f"close={close:.2f}  bb_lower={bb_lo:.2f}\n")

    rsi2, adx2, close2, bb_lo2 = _verify_indicators(overbought_df)
    print(f"  [info] Overbought data last row: RSI={rsi2:.1f}  ADX={adx2:.1f}  "
          f"close={close2:.2f}  bb_lower={bb_lo2:.2f}\n")

    rsi3, adx3, close3, bb_lo3 = _verify_indicators(trending_df)
    print(f"  [info] Trending data last row: RSI={rsi3:.1f}  ADX={adx3:.1f}  "
          f"close={close3:.2f}\n")

    # T1: Oversold → long signal
    run("T1 Oversold (RSI<30, price<BB_lower) → long",
        make_strategy(oversold_df), 1)

    # T2: Overbought → short signal
    run("T2 Overbought (RSI>70, price>BB_upper) → short",
        make_strategy(overbought_df), 1)

    # T3: Trending (ADX>25) → no signal
    run("T3 Trending market (ADX>25) → no signal",
        make_strategy(trending_df), 0)

    # T4: Already triggered → no signal
    strat4 = make_strategy(oversold_df)
    strat4._mark_triggered("NSE_EQ|INE002A01018")
    run("T4 Already triggered today", strat4, 0)

    print("─" * 55)
    passed = sum(1 for _, ok in results if ok)
    if passed == len(results):
        print(f"  \033[32mAll {len(results)} MeanReversion tests passed.\033[0m\n")
    else:
        failed = [n for n, ok in results if not ok]
        # Show what indicators we actually got to help diagnose
        print(f"  \033[31m{len(results) - passed} failed: {', '.join(failed)}\033[0m")
        print("  Tip: check indicator values printed above — adjust _make_*_df() if")
        print("  RSI/ADX thresholds aren't crossing the expected boundaries.\n")
        sys.exit(1)
