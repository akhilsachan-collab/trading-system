"""
momentum.py — Momentum Breakout strategy.

Trigger: price breaks the rolling 20-day high (excluding today) with volume
confirmation and a passing Nifty 50 trend filter.

Routing:
  - Stocks (10 names)  → EQUITY_INTRADAY
  - Indices (Nifty 50, Nifty Bank) → BUY_OPTIONS (nearest ATM weekly call)

Direction: Long only (v1). Shorts gated behind Phase 6 backtesting.
"""

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from strategies.base import BaseStrategy, INSTRUMENTS_PATH, logger, IST
from risk_engine import RiskEngine, ProposedTrade


class MomentumBreakout(BaseStrategy):
    """
    Generates long proposals when an instrument breaks its 20-day high
    with volume and broad-market confirmation.
    """

    @property
    def name(self) -> str:
        return "Momentum"

    @property
    def segments(self) -> List[str]:
        return ["EQUITY_INTRADAY", "BUY_OPTIONS"]

    # ── Nifty trend filter ────────────────────────────────────────────────────

    def _check_nifty_filter(self) -> bool:
        """
        Long entries are allowed only if:
          - Nifty 50 close > 20-day SMA
          - Nifty 50 closed up vs the previous day
        Returns True (pass) on any data error to avoid blocking on missing history.
        """
        try:
            df = self.get_historical_candles("NSE_INDEX|Nifty 50", "day", 25)
            if len(df) < 22:
                logger.warning("Momentum: insufficient Nifty history — filter defaults to PASS")
                return True
            df = self.calculate_indicators(df)
            last = df.iloc[-1]
            prev = df.iloc[-2]
            above_sma = bool(last["close"] > last["sma_20"])
            closed_up = bool(last["close"] > prev["close"])
            passed = above_sma and closed_up
            logger.debug(
                "Momentum: Nifty filter — above_sma=%s closed_up=%s → %s",
                above_sma, closed_up, "PASS" if passed else "FAIL",
            )
            return passed
        except Exception as exc:
            logger.warning("Momentum: Nifty filter error (%s) — defaulting to PASS", exc)
            return True

    # ── ATM call lookup ───────────────────────────────────────────────────────

    def _find_atm_call(self, index_key: str, spot: float) -> Optional[str]:
        """
        Search instruments.json for the nearest-expiry ATM call option for
        the given index. Returns instrument_key (e.g. 'NSE_FO|50973') or None.
        """
        if "Nifty Bank" in index_key:
            underlying_key = "NSE_INDEX|Nifty Bank"
            strike_step    = 100
        elif "Nifty 50" in index_key:
            underlying_key = "NSE_INDEX|Nifty 50"
            strike_step    = 50
        else:
            return None

        atm_strike = round(spot / strike_step) * strike_step
        now_ms     = datetime.now().timestamp() * 1000

        try:
            instruments = json.loads(INSTRUMENTS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Momentum: instruments.json unavailable: %s", exc)
            return None

        best_key    = None
        best_expiry = None

        for inst in instruments:
            if inst.get("segment") != "NSE_FO":
                continue
            if inst.get("instrument_type") != "CE":
                continue
            if inst.get("underlying_key") != underlying_key:
                continue

            strike = float(inst.get("strike_price") or 0)
            if abs(strike - atm_strike) > strike_step:
                continue

            expiry_ms = inst.get("expiry") or 0
            if expiry_ms <= now_ms:
                continue

            if best_expiry is None or expiry_ms < best_expiry:
                best_expiry = expiry_ms
                best_key    = inst.get("instrument_key")

        if best_key:
            logger.debug(
                "Momentum: ATM call for %s spot=%.0f atm_strike=%.0f → %s",
                index_key, spot, atm_strike, best_key,
            )
        else:
            logger.warning(
                "Momentum: no ATM call found for %s (spot=%.0f atm=%.0f) — "
                "index signal skipped (refresh instruments.json if stale)",
                index_key, spot, atm_strike,
            )
        return best_key

    # ── Signal evaluation ─────────────────────────────────────────────────────

    def _evaluate_stock(self, key: str) -> Optional[ProposedTrade]:
        df = self.get_historical_candles(key, "day", 25)
        if len(df) < 22:
            logger.debug("Momentum: insufficient history for %s (%d rows)", key, len(df))
            return None

        # 20-day high of previous bars (exclude today's bar)
        rolling_high = df["high"].iloc[:-1].rolling(20).max().iloc[-1]
        today_high   = float(df["high"].iloc[-1])
        if today_high <= rolling_high:
            return None

        # Volume: today vs 20-day average of previous bars
        avg_vol   = df["volume"].iloc[-21:-1].mean()
        today_vol = float(df["volume"].iloc[-1])
        if avg_vol <= 0 or today_vol < 1.5 * avg_vol:
            logger.debug("Momentum: volume not confirmed for %s", key)
            return None

        df = self.calculate_indicators(df)
        last = df.iloc[-1]
        atr  = float(last["atr_14"]) if pd.notna(last.get("atr_14")) else 0.0
        if atr <= 0:
            return None

        quote = self.get_current_quote(key)
        entry = float(quote.get("last_price", 0.0) or 0.0)
        if entry <= 0:
            return None

        swing_low = float(df["low"].iloc[-21:-1].min())
        sl        = max(entry - 1.5 * atr, swing_low)
        if sl >= entry:
            return None

        target = entry + 2.5 * (entry - sl)
        logger.info(
            "Momentum: signal %s  entry=%.2f  sl=%.2f  target=%.2f  20d_high=%.2f",
            key, entry, sl, target, rolling_high,
        )
        return self.propose(key, "BUY", entry, sl, target, "EQUITY_INTRADAY")

    def _evaluate_index(self, key: str) -> Optional[ProposedTrade]:
        df = self.get_historical_candles(key, "day", 25)
        if len(df) < 22:
            return None

        rolling_high = df["high"].iloc[:-1].rolling(20).max().iloc[-1]
        today_high   = float(df["high"].iloc[-1])
        if today_high <= rolling_high:
            return None

        avg_vol   = df["volume"].iloc[-21:-1].mean()
        today_vol = float(df["volume"].iloc[-1])
        # Indices often have zero reported volume — skip volume check when avg is 0
        if avg_vol > 0 and today_vol < 1.5 * avg_vol:
            logger.debug("Momentum: index volume not confirmed for %s", key)
            return None

        # Get spot price
        quote = self.get_current_quote(key)
        spot  = float(quote.get("last_price", 0.0) or 0.0)
        if spot <= 0:
            return None

        option_key = self._find_atm_call(key, spot)
        if not option_key:
            return None

        # Get option premium
        opt_quote = self.get_current_quote(option_key)
        premium   = float(opt_quote.get("last_price", 0.0) or 0.0)
        if premium <= 0:
            logger.warning("Momentum: option %s has zero or missing premium", option_key)
            return None

        # SL at 50% of premium; target at 2.5:1 on the risk (premium × 0.5)
        sl     = premium * 0.5
        target = premium + 2.5 * (premium - sl)

        logger.info(
            "Momentum: index signal %s → option %s  premium=%.2f  sl=%.2f  target=%.2f",
            key, option_key, premium, sl, target,
        )
        return self.propose(option_key, "BUY", premium, sl, target, "BUY_OPTIONS")

    # ── Main evaluation entry-point ───────────────────────────────────────────

    def evaluate(self) -> List[ProposedTrade]:
        if not self._check_nifty_filter():
            logger.info("Momentum: Nifty filter failed — no entries this cycle")
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
                logger.error("Momentum: unhandled error for %s: %s", key, exc, exc_info=True)

        for key in self.index_keys:
            if self._is_triggered(key):
                continue
            try:
                p = self._evaluate_index(key)
                if p is not None:
                    proposals.append(p)
                    self._mark_triggered(key)
            except Exception as exc:
                logger.error("Momentum: unhandled error for %s: %s", key, exc, exc_info=True)

        return proposals


# ── Tests ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger("strategies").setLevel(logging.CRITICAL)
    logging.getLogger("risk_engine").setLevel(logging.CRITICAL)
    PASS = "\033[32mPASS\033[0m"
    FAIL = "\033[31mFAIL\033[0m"

    T_MARKET = datetime(2026, 5, 20, 11, 0, 0, tzinfo=IST)

    def _make_breakout_daily(n=25, base=1400.0, vol=1_000_000):
        """
        Daily candles where the last bar's high exceeds the rolling 20-day max.
        Previous 24 bars range: base to base+20. Last bar high: base+50.
        """
        from datetime import timedelta
        rows = []
        start = datetime(2026, 4, 1, 0, 0, 0, tzinfo=IST)
        for i in range(n - 1):
            c = base + i * (20.0 / (n - 1))
            rows.append({
                "timestamp": start + timedelta(days=i),
                "open": c - 3, "high": c + 3, "low": c - 5,
                "close": c, "volume": vol, "open_interest": 0,
            })
        # Final bar: breakout high
        last = start + timedelta(days=n - 1)
        rows.append({
            "timestamp": last,
            "open": base + 20, "high": base + 55, "low": base + 18,
            "close": base + 50,
            "volume": vol * 2.5,   # 2.5× avg — confirms volume
            "open_interest": 0,
        })
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df

    def _make_no_breakout_daily(n=25, base=1400.0, vol=1_000_000):
        """Daily candles where today's high is BELOW the 20-day rolling max."""
        from datetime import timedelta
        rows = []
        start = datetime(2026, 4, 1, 0, 0, 0, tzinfo=IST)
        for i in range(n):
            c = base + (n - 1 - i) * 1.0   # Declining — last bar is lowest
            rows.append({
                "timestamp": start + timedelta(days=i),
                "open": c - 2, "high": c + 2, "low": c - 4,
                "close": c, "volume": vol, "open_interest": 0,
            })
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df

    def make_strategy(daily_df, ltp=1450.0, nifty_passes=True):
        engine = RiskEngine(db_path=":memory:")
        engine._kill_switch_override = True
        engine._vix_override         = 15.0
        engine._time_override        = T_MARKET

        strat = MomentumBreakout(engine)
        strat.equity_keys = ["NSE_EQ|INE002A01018"]
        strat.index_keys  = []  # skip index path in basic tests

        strat.get_historical_candles = lambda key, interval="day", lookback_days=25: daily_df
        strat.get_current_quote      = lambda key: {"last_price": ltp}
        strat._check_nifty_filter    = lambda: nifty_passes

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
            print(f"            entry={p.entry_price:.2f}  sl={p.stop_loss:.2f}  "
                  f"target={p.target:.2f}  qty={p.quantity}")
        print()

    print("\n" + "═" * 55)
    print("  Momentum Strategy — Test Suite")
    print("═" * 55 + "\n")

    # T1: 20-day high breakout with volume + Nifty passes → 1 proposal
    run("T1 20-day high breakout + Nifty filter pass",
        make_strategy(_make_breakout_daily(), ltp=1455.0, nifty_passes=True), 1)

    # T2: No breakout → 0 proposals
    run("T2 No 20-day high breakout",
        make_strategy(_make_no_breakout_daily(), ltp=1395.0, nifty_passes=True), 0)

    # T3: Nifty filter fails → 0 proposals even with valid breakout
    run("T3 Valid breakout but Nifty filter fails",
        make_strategy(_make_breakout_daily(), ltp=1455.0, nifty_passes=False), 0)

    # T4: Already triggered → 0
    strat4 = make_strategy(_make_breakout_daily(), ltp=1455.0, nifty_passes=True)
    strat4._mark_triggered("NSE_EQ|INE002A01018")
    run("T4 Already triggered today", strat4, 0)

    print("─" * 55)
    passed = sum(1 for _, ok in results if ok)
    if passed == len(results):
        print(f"  \033[32mAll {len(results)} Momentum tests passed.\033[0m\n")
    else:
        failed = [n for n, ok in results if not ok]
        print(f"  \033[31m{len(results) - passed} failed: {', '.join(failed)}\033[0m\n")
        sys.exit(1)
