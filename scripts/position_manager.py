"""
position_manager.py — Open position lifecycle management (Phase 5C).

Owns every state transition in the positions table: OPEN → CLOSED.
Works alongside RiskEngine (which owns validation and P&L accumulators)
and Broker (which owns the API calls).

DB migration: adds five columns to the existing positions table on first
import (order_id, exit_price, exit_reason, trailing_activated, original_sl).
The risk engine's existing queries are unaffected — all new columns are nullable
or have DEFAULT 0.

Exit condition priority (per check_open_positions):
    1. Stop loss hit
    2. Target hit
    3. Trailing stop (activated or updated)
    4. Time stop (strategy-specific)
    5. Force close (15:15 IST for intraday)

Usage:
    pm = PositionManager(engine, broker)
    actions = pm.check_open_positions()
    for action in actions:
        if action.action_type.startswith("CLOSE"):
            pm.close_position(action.position, action.suggested_exit_price, action.action_type)
        elif action.action_type == "UPDATE_TRAIL":
            pm.update_trailing_stop(action.position, action.suggested_exit_price)
"""

import csv
import json
import logging
import logging.handlers
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────

_SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _SCRIPTS_DIR.parent
ENV_PATH     = PROJECT_ROOT / ".env"
DB_PATH      = PROJECT_ROOT / "data" / "trading_state.db"
LOGS_DIR     = PROJECT_ROOT / "logs"

load_dotenv(ENV_PATH)

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from risk_engine import RiskEngine, ProposedTrade   # noqa: E402
from broker import Broker, OrderResult, OrderStatus  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# ── Logging ───────────────────────────────────────────────────────────────────

LOGS_DIR.mkdir(exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOGS_DIR / "position_manager.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s: %(message)s")
)
logger = logging.getLogger("position_manager")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    logger.addHandler(_handler)

# ── Constants ─────────────────────────────────────────────────────────────────

# Minutes after entry before a position is closed by time stop
_STRATEGY_TIME_STOPS = {"ORB": 60, "Momentum": 90, "MeanReversion": 30}

# Price must move this many R-multiples before trailing activates
_TRAIL_ACTIVATION_R = {"ORB": 1.0, "Momentum": 2.5}

_INTRADAY_SEGMENTS = {"EQUITY_INTRADAY", "BUY_OPTIONS"}

# Must match INTRADAY_FORCE_CLOSE in TRADING_RULES.md
_FORCE_CLOSE_H, _FORCE_CLOSE_M = 15, 15


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class ManagedPosition:
    id:                 int
    instrument_key:     str
    side:               str
    quantity:           int
    entry_price:        float
    current_sl:         float
    current_target:     float
    strategy:           str
    segment:            str
    opened_at:          datetime
    status:             str
    order_id:           Optional[str]
    original_sl:        float
    trailing_activated: bool
    exit_price:         Optional[float] = None
    exit_reason:        Optional[str]   = None
    closed_at:          Optional[datetime] = None
    pnl:                Optional[float] = None

    @property
    def original_risk_per_unit(self) -> float:
        """Initial 1R in price terms (always positive)."""
        return abs(self.entry_price - self.original_sl)


@dataclass
class PositionAction:
    position:             ManagedPosition
    action_type:          str     # CLOSE_SL / CLOSE_TARGET / CLOSE_TRAIL /
                                  # CLOSE_TIME / CLOSE_FORCE / UPDATE_TRAIL
    suggested_exit_price: float   # exit price for CLOSE_*; new SL for UPDATE_TRAIL


@dataclass
class RealizedPnL:
    position_id:    int
    instrument_key: str
    strategy:       str
    entry_price:    float
    exit_price:     float
    quantity:       int
    side:           str
    pnl:            float
    exit_reason:    str
    opened_at:      datetime
    closed_at:      datetime


@dataclass
class ClosedPosition:
    position_id:    int
    instrument_key: str
    pnl:            float
    exit_reason:    str


# ── PositionManager ───────────────────────────────────────────────────────────


class PositionManager:
    """
    Manages the full lifecycle of open positions.

    Single source of truth for when to exit and why. The Broker places
    the actual orders; RiskEngine keeps P&L accumulators accurate.
    """

    def __init__(self, engine: RiskEngine, broker: Broker) -> None:
        self._engine = engine
        self._broker = broker

        self._db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.commit()

        self._migrate_db()
        logger.info("PositionManager ready")

    # ── DB migration ──────────────────────────────────────────────────────────

    def _migrate_db(self) -> None:
        """Add Phase 5C columns to positions if they don't already exist."""
        new_cols = [
            ("order_id",           "TEXT"),
            ("exit_price",         "REAL"),
            ("exit_reason",        "TEXT"),
            ("trailing_activated", "INTEGER DEFAULT 0"),
            ("original_sl",        "REAL"),
        ]
        for col_name, col_def in new_cols:
            try:
                self._db.execute(
                    f"ALTER TABLE positions ADD COLUMN {col_name} {col_def}"
                )
                self._db.commit()
                logger.info("DB migration: added positions.%s", col_name)
            except sqlite3.OperationalError:
                pass    # column already exists

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_open_positions(self) -> List[ManagedPosition]:
        rows = self._db.execute(
            "SELECT * FROM positions WHERE status = 'OPEN'"
        ).fetchall()
        result = []
        for r in rows:
            result.append(ManagedPosition(
                id=r["id"],
                instrument_key=r["instrument_key"],
                side=r["side"],
                quantity=r["quantity"],
                entry_price=r["entry_price"],
                current_sl=r["current_sl"],
                current_target=r["current_target"],
                strategy=r["strategy"],
                segment=r["segment"],
                opened_at=datetime.fromisoformat(r["opened_at"]),
                status=r["status"],
                order_id=r["order_id"],
                original_sl=r["original_sl"] if r["original_sl"] else r["current_sl"],
                trailing_activated=bool(r["trailing_activated"]),
                exit_price=r["exit_price"],
                exit_reason=r["exit_reason"],
                closed_at=datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None,
                pnl=r["pnl"],
            ))
        return result

    def _check_one(
        self,
        pos: ManagedPosition,
        ltp: float,
        now: datetime,
    ) -> Optional[PositionAction]:
        """
        Evaluate exit conditions for one position in strict priority order.
        Returns None if no action is needed.
        ltp of 0.0 means the quote is stale — skip all price-based checks.
        """
        is_buy = pos.side == "BUY"

        if ltp <= 0:
            return None

        # 1. Stop loss
        sl_hit = (ltp <= pos.current_sl) if is_buy else (ltp >= pos.current_sl)
        if sl_hit:
            action = "CLOSE_TRAIL" if pos.trailing_activated else "CLOSE_SL"
            return PositionAction(pos, action, pos.current_sl)

        # 2. Target
        target_hit = (ltp >= pos.current_target) if is_buy else (ltp <= pos.current_target)
        if target_hit:
            return PositionAction(pos, "CLOSE_TARGET", pos.current_target)

        # 3. Trailing stop (ORB and Momentum only; MeanReversion has no trailing)
        if pos.strategy in _TRAIL_ACTIVATION_R and pos.original_risk_per_unit > 0:
            activation_r = _TRAIL_ACTIVATION_R[pos.strategy]
            if is_buy:
                activation_price = pos.entry_price + activation_r * pos.original_risk_per_unit
                trail_sl = round(ltp * 0.99, 2)
                if ltp >= activation_price and trail_sl > pos.current_sl:
                    return PositionAction(pos, "UPDATE_TRAIL", trail_sl)
            else:
                activation_price = pos.entry_price - activation_r * pos.original_risk_per_unit
                trail_sl = round(ltp * 1.01, 2)
                if ltp <= activation_price and trail_sl < pos.current_sl:
                    return PositionAction(pos, "UPDATE_TRAIL", trail_sl)

        # 4. Time stop
        time_stop_min = _STRATEGY_TIME_STOPS.get(pos.strategy, 60)
        elapsed_min   = (now - pos.opened_at).total_seconds() / 60
        if elapsed_min >= time_stop_min:
            return PositionAction(pos, "CLOSE_TIME", ltp)

        # 5. Force close (intraday only at 15:15 IST)
        if (
            pos.segment in _INTRADAY_SEGMENTS
            and now.time() >= time(_FORCE_CLOSE_H, _FORCE_CLOSE_M)
        ):
            return PositionAction(pos, "CLOSE_FORCE", ltp)

        return None

    def _write_audit(
        self,
        action_type: str,
        pos: ManagedPosition,
        price: float,
        pnl: Optional[float] = None,
        reason: str = "",
    ) -> None:
        now = datetime.now(tz=IST)
        ts  = now.isoformat()
        try:
            self._db.execute(
                """INSERT INTO audit_log
                   (timestamp, action_type, instrument_key, side, quantity,
                    price, strategy, segment, reasoning, sl, target, pnl)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts, action_type, pos.instrument_key, pos.side, pos.quantity,
                    price, pos.strategy, pos.segment, reason,
                    pos.current_sl, pos.current_target, pnl,
                ),
            )
            self._db.commit()
        except Exception as exc:
            logger.warning("audit_log write failed: %s", exc)

        csv_path     = LOGS_DIR / f"audit_{now.strftime('%Y-%m-%d')}.csv"
        write_header = not csv_path.exists()
        row = {
            "timestamp":      ts,
            "action_type":    action_type,
            "instrument_key": pos.instrument_key,
            "side":           pos.side,
            "quantity":       pos.quantity,
            "price":          price,
            "strategy":       pos.strategy,
            "segment":        pos.segment,
            "reason":         reason,
            "pnl":            pnl or "",
        }
        try:
            with csv_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                if write_header:
                    w.writeheader()
                w.writerow(row)
        except Exception as exc:
            logger.warning("audit CSV write failed: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def open_position(
        self,
        proposal: "ProposedTrade",
        order_result: "OrderResult",
    ) -> ManagedPosition:
        """
        Register a newly placed order as an open position.

        Tries to get the actual fill price from the broker; falls back to
        proposal.entry_price (acceptable for paper trading where fills are
        synthetic).

        Raises ValueError if order_result has an error (caller should check
        order_result.error_msg before calling).
        """
        if order_result.error_msg or not order_result.order_id:
            raise ValueError(
                f"Cannot open position for {proposal.instrument_key}: "
                f"{order_result.error_msg or 'no order_id returned'}"
            )

        # Try to get actual fill price
        avg_price = proposal.entry_price
        if order_result.order_id:
            details = self._broker.get_order_details(order_result.order_id)
            raw_avg = float(details.get("average_price", 0) or 0)
            if raw_avg > 0:
                avg_price = raw_avg

        now = datetime.now(tz=IST)
        self._db.execute(
            """INSERT INTO positions
               (instrument_key, side, quantity, entry_price, current_sl, current_target,
                strategy, segment, opened_at, status,
                order_id, original_sl, trailing_activated, exit_price, exit_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, 0, NULL, NULL)""",
            (
                proposal.instrument_key,
                proposal.side,
                proposal.quantity,
                avg_price,
                proposal.stop_loss,
                proposal.target,
                proposal.strategy,
                proposal.segment,
                now.isoformat(),
                order_result.order_id,
                proposal.stop_loss,     # original_sl — never changes after entry
            ),
        )
        row_id = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Increment trades_taken for today
        today = now.date().isoformat()
        self._db.execute(
            "UPDATE daily_state SET trades_taken = trades_taken + 1 WHERE date = ?",
            (today,),
        )
        self._db.commit()

        pos = ManagedPosition(
            id=row_id,
            instrument_key=proposal.instrument_key,
            side=proposal.side,
            quantity=proposal.quantity,
            entry_price=avg_price,
            current_sl=proposal.stop_loss,
            current_target=proposal.target,
            strategy=proposal.strategy,
            segment=proposal.segment,
            opened_at=now,
            status="OPEN",
            order_id=order_result.order_id,
            original_sl=proposal.stop_loss,
            trailing_activated=False,
        )
        self._write_audit("OPEN_POSITION", pos, avg_price, reason=order_result.order_id)
        logger.info(
            "open_position: %s %s ×%d  entry=%.2f  sl=%.2f  target=%.2f  id=%d",
            pos.side, pos.instrument_key, pos.quantity,
            pos.entry_price, pos.current_sl, pos.current_target, pos.id,
        )
        return pos

    def check_open_positions(self) -> List[PositionAction]:
        """
        For each OPEN position: fetch LTP (batch), evaluate exit conditions in
        priority order, return the list of actions the agent should take this cycle.

        Positions with stale LTP (0.0) are skipped — we never exit based on
        a missing quote.
        """
        positions = self._get_open_positions()
        if not positions:
            return []

        keys = list({p.instrument_key for p in positions})
        ltps = self._broker.get_ltp_batch(keys)
        now  = datetime.now(tz=IST)

        actions: List[PositionAction] = []
        for pos in positions:
            ltp = ltps.get(pos.instrument_key, 0.0)
            action = self._check_one(pos, ltp, now)
            if action:
                logger.info(
                    "check_open_positions: %s → %s  ltp=%.2f  suggested=%.2f",
                    pos.instrument_key, action.action_type, ltp,
                    action.suggested_exit_price,
                )
                actions.append(action)
        return actions

    def close_position(
        self,
        position: ManagedPosition,
        exit_price: float,
        reason: str,
    ) -> RealizedPnL:
        """
        Place a market exit order, update the DB, and record P&L in the
        risk engine's daily/weekly/monthly accumulators.
        """
        close_side = "SELL" if position.side == "BUY" else "BUY"

        close_result = self._broker.place_close_order(
            instrument_key=position.instrument_key,
            side=close_side,
            quantity=position.quantity,
            segment=position.segment,
            order_type="MARKET",
        )

        # Prefer actual fill price; fall back to the suggested price
        actual_exit = exit_price
        if close_result.order_id:
            details = self._broker.get_order_details(close_result.order_id)
            raw_avg = float(details.get("average_price", 0) or 0)
            if raw_avg > 0:
                actual_exit = raw_avg

        direction = 1 if position.side == "BUY" else -1
        pnl = round(
            (actual_exit - position.entry_price) * position.quantity * direction, 2
        )
        now = datetime.now(tz=IST)

        self._db.execute(
            """UPDATE positions
               SET status='CLOSED', closed_at=?, pnl=?, exit_price=?, exit_reason=?
               WHERE id=?""",
            (now.isoformat(), pnl, actual_exit, reason, position.id),
        )
        self._db.commit()

        self._engine.record_pnl(pnl, realized=True)

        self._write_audit("CLOSE_POSITION", position, actual_exit, pnl, reason)
        logger.info(
            "close_position: %s %s ×%d  entry=%.2f  exit=%.2f  pnl=%.2f  reason=%s",
            position.side, position.instrument_key, position.quantity,
            position.entry_price, actual_exit, pnl, reason,
        )

        if close_result.error_msg:
            logger.error(
                "close order for %s returned error: %s — manual intervention needed",
                position.instrument_key, close_result.error_msg,
            )
            # Mark as needing attention so the next cycle skips it
            self._db.execute(
                "UPDATE positions SET status='INTERVENTION_NEEDED' WHERE id=?",
                (position.id,),
            )
            self._db.commit()

        return RealizedPnL(
            position_id=position.id,
            instrument_key=position.instrument_key,
            strategy=position.strategy,
            entry_price=position.entry_price,
            exit_price=actual_exit,
            quantity=position.quantity,
            side=position.side,
            pnl=pnl,
            exit_reason=reason,
            opened_at=position.opened_at,
            closed_at=now,
        )

    def update_trailing_stop(
        self,
        position: ManagedPosition,
        new_sl: float,
    ) -> None:
        """
        Tighten the trailing stop to new_sl.
        Updates trailing_activated=1 and current_sl in the DB.

        In Phase 5C (paper trading), the entry order is already filled so
        broker.modify_order has no live SL order to update — it's a no-op
        but is included so the code is ready for hard SL order management.
        """
        self._db.execute(
            """UPDATE positions
               SET current_sl=?, trailing_activated=1
               WHERE id=?""",
            (new_sl, position.id),
        )
        self._db.commit()

        if position.order_id:
            self._broker.modify_order(position.order_id, new_sl_trigger=new_sl)

        logger.info(
            "update_trailing_stop: %s  %.2f → %.2f",
            position.instrument_key, position.current_sl, new_sl,
        )

    def force_close_intraday(self) -> List[ClosedPosition]:
        """
        Close all OPEN intraday positions at market price.
        Called at 15:15 IST or on emergency shutdown (Ctrl+C).
        """
        positions = [
            p for p in self._get_open_positions()
            if p.segment in _INTRADAY_SEGMENTS
        ]
        if not positions:
            return []

        keys = list({p.instrument_key for p in positions})
        ltps = self._broker.get_ltp_batch(keys)
        closed: List[ClosedPosition] = []

        for pos in positions:
            ltp = ltps.get(pos.instrument_key, pos.entry_price)
            try:
                rpnl = self.close_position(pos, ltp, "CLOSE_FORCE")
                closed.append(
                    ClosedPosition(rpnl.position_id, rpnl.instrument_key, rpnl.pnl, "CLOSE_FORCE")
                )
            except Exception as exc:
                logger.error(
                    "force_close_intraday: failed to close %s: %s",
                    pos.instrument_key, exc,
                )

        logger.info("force_close_intraday: closed %d position(s)", len(closed))
        return closed


# ── Self-test (no API calls) ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")

    print("=== position_manager.py self-test (simulated — no API calls) ===\n")

    # ── Setup ────────────────────────────────────────────────────────────────
    from broker import MODE  # prints no banner in test
    from risk_engine import RiskEngine

    engine = RiskEngine()
    _token = __import__("os").getenv("UPSTOX_ACCESS_TOKEN", "")
    broker = Broker(access_token=_token)
    pm     = PositionManager(engine, broker)

    # ── Fake proposal ────────────────────────────────────────────────────────
    from dataclasses import dataclass as _dc
    from datetime import datetime as _dt

    @_dc
    class _FakeTrade:
        instrument_key: str  = "NSE_EQ|INE040A01034"
        side:           str  = "BUY"
        quantity:       int  = 5
        entry_price:    float = 1600.00
        stop_loss:      float = 1580.00   # 1R = ₹20
        target:         float = 1640.00   # 2R
        strategy:       str  = "ORB"
        segment:        str  = "EQUITY_INTRADAY"
        signal_timestamp = None

    proposal = _FakeTrade()
    fake_order = OrderResult(
        order_id="TEST_ORDER_001",
        status=OrderStatus.FILLED.value,
        filled_quantity=5,
        avg_price=1600.00,
    )

    print("[1] Opening fake position …")
    pos = pm.open_position(proposal, fake_order)
    print(f"    id={pos.id}  entry={pos.entry_price}  sl={pos.current_sl}  target={pos.current_target}\n")

    # ── Simulate trailing stop activation ────────────────────────────────────
    print("[2] Simulating trailing stop activation (ltp=1625 → 1R=20, ORB activates at entry+1R=1620)")
    ltp_trail = 1625.0
    new_sl = round(ltp_trail * 0.99, 2)
    print(f"    new_sl would be: {new_sl}")
    pm.update_trailing_stop(pos, new_sl)
    print(f"    trailing_stop updated to {new_sl}\n")

    # ── Simulate SL hit ───────────────────────────────────────────────────────
    print("[3] Simulating SL hit (ltp=1607 < new_sl=1608.75)")
    print("    check_one result:")
    pos_refreshed = pm._get_open_positions()
    if pos_refreshed:
        p = pos_refreshed[0]
        action = pm._check_one(p, ltp=1607.0, now=datetime.now(tz=IST))
        print(f"    action_type: {action.action_type if action else None}")
        print(f"    suggested_exit: {action.suggested_exit_price if action else None}")
    print()

    # ── Close the position ────────────────────────────────────────────────────
    print("[4] Closing position (simulated — broker call will fail/no-op in test)")
    if pos_refreshed:
        try:
            rpnl = pm.close_position(pos_refreshed[0], 1607.0, "CLOSE_TRAIL")
            print(f"    pnl=₹{rpnl.pnl:.2f}  reason={rpnl.exit_reason}")
        except Exception as e:
            print(f"    (expected in sandbox without live broker: {e})")
    print()
    print("Self-test complete.")
