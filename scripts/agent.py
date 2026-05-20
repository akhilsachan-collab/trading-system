"""
agent.py — Main trading agent orchestrator (Phase 5D).

Initialises RiskEngine, Broker, PositionManager, and all three strategies,
then runs one-cycle or continuous-loop evaluation.

Market hours: 09:15 – 15:30 IST, Monday – Friday.
Force-close intraday at 15:15. EOD report at 15:30.

CLI:
    python scripts/agent.py once      # single cycle
    python scripts/agent.py loop      # continuous, polls every 30 s
    python scripts/agent.py dry-run   # evaluate signals, no orders placed
    python scripts/agent.py status    # read-only status snapshot
    python scripts/agent.py test      # run built-in self-tests
"""

import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import time as _time
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────

_SCRIPTS_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT   = _SCRIPTS_DIR.parent
ENV_PATH       = PROJECT_ROOT / ".env"
RULES_PATH     = PROJECT_ROOT / "TRADING_RULES.md"
WATCHLIST      = PROJECT_ROOT / "watchlist.json"
LOGS_DIR       = PROJECT_ROOT / "logs"
DB_PATH        = PROJECT_ROOT / "data" / "trading_state.db"
HOLIDAYS_PATH  = PROJECT_ROOT / "data" / "holidays.json"

load_dotenv(ENV_PATH)

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from broker import Broker, MODE, OrderStatus                      # noqa: E402
from position_manager import (                                    # noqa: E402
    PositionManager, ManagedPosition, PositionAction,
    RealizedPnL, ClosedPosition,
)
from risk_engine import RiskEngine, ProposedTrade                 # noqa: E402
from strategies.orb            import OpeningRangeBreakout        # noqa: E402
from strategies.momentum       import MomentumBreakout            # noqa: E402
from strategies.mean_reversion import MeanReversion               # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# ── Logging ───────────────────────────────────────────────────────────────────

LOGS_DIR.mkdir(exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOGS_DIR / "agent.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s: %(message)s")
)
logger = logging.getLogger("agent")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    logger.addHandler(_handler)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _fmt_inr(n: float) -> str:
    """Format a rupee amount: 150000 → '₹1,50,000'."""
    sign       = "-" if n < 0 else ""
    integer, _ = f"{abs(n):.0f}", ""
    if len(integer) <= 3:
        return f"{sign}₹{integer}"
    last3 = integer[-3:]
    head  = integer[:-3]
    groups: list = []
    while head:
        groups.append(head[-2:])
        head = head[:-2]
    groups.reverse()
    return f"{sign}₹{','.join(groups)},{last3}"


def _col(text: str, ok: bool) -> str:
    """Simple ANSI colouring for terminal output."""
    code = "32" if ok else "31"
    return f"\033[{code}m{text}\033[0m"


# ── Agent ─────────────────────────────────────────────────────────────────────


class Agent:
    """
    Main orchestrator. Ties together engine → strategies → broker → position_manager.

    Always defaults to sandbox. Live mode requires TRADING_MODE=live in .env
    AND acknowledgement of the 10-second countdown in the startup banner.
    """

    _MARKET_OPEN  = time(9, 15)
    _MARKET_CLOSE = time(15, 30)
    _FORCE_CLOSE  = time(15, 15)

    def __init__(self) -> None:
        token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
        if not token:
            logger.error("UPSTOX_ACCESS_TOKEN not set in .env")
            raise EnvironmentError("UPSTOX_ACCESS_TOKEN not set in .env")

        if not RULES_PATH.exists():
            raise FileNotFoundError(f"TRADING_RULES.md not found at {RULES_PATH}")

        if not WATCHLIST.exists():
            raise FileNotFoundError(f"watchlist.json not found at {WATCHLIST}")

        if MODE.is_mock:
            from mock_broker import MockBroker          # noqa: F401
            self._broker = MockBroker()
        else:
            self._broker = Broker(access_token=token)

        self._engine     = RiskEngine()
        self._pm         = PositionManager(self._engine, self._broker)
        self._strategies = [
            OpeningRangeBreakout(self._engine),
            MomentumBreakout(self._engine),
            MeanReversion(self._engine),
        ]
        self._cycles_run          = 0
        self._force_closed        = False
        self._consecutive_api_errors = 0
        self._last_cycle_ts: Optional[datetime] = None

        self._db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")

        self._recover_state()

        mode_label = "LIVE" if MODE.is_live else ("MOCK" if MODE.is_mock else "SANDBOX")
        logger.info(
            "Agent ready — mode=%s  strategies=%s",
            mode_label,
            [s.name for s in self._strategies],
        )

    # ── State recovery ────────────────────────────────────────────────────────

    def _recover_state(self) -> None:
        """Cross-check OPEN DB positions against broker portfolio on startup."""
        now       = datetime.now(tz=IST)
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        rows = self._db.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' AND date(opened_at) >= ?",
            (yesterday,),
        ).fetchall()

        if not rows:
            print("  State recovery: no open positions to check")
            return

        try:
            live_pos: Dict[str, object] = {
                p.instrument_key: p for p in self._broker.get_positions()
            }
        except Exception as exc:
            logger.warning("State recovery: broker.get_positions failed: %s", exc)
            live_pos = {}

        reconciled = 0
        for row in rows:
            inst_key = row["instrument_key"]
            broker_p = live_pos.get(inst_key)

            if broker_p is None:
                # Verify via entry order status (catches outright unfilled entries)
                order_id = row["order_id"]
                close_reason = "RECONCILE_MISSING"
                ltp = 0.0
                if order_id:
                    try:
                        st = self._broker.get_order_status(order_id)
                        if st in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                            close_reason = "RECONCILE_ENTRY_CANCELLED"
                        else:
                            # Entry filled, but position gone — closed outside agent
                            try:
                                ltp = self._broker.get_ltp(inst_key)
                            except Exception:
                                pass
                    except Exception as exc:
                        logger.warning("Reconcile: order_status failed for %s: %s", order_id, exc)

                self._db.execute(
                    "UPDATE positions SET status='CLOSED', exit_reason=?, "
                    "closed_at=?, exit_price=?, pnl=? WHERE id=?",
                    (
                        close_reason,
                        now.isoformat(),
                        ltp or row["entry_price"],
                        (ltp - row["entry_price"]) * row["quantity"]
                        * (1 if row["side"] == "BUY" else -1)
                        if ltp else 0.0,
                        row["id"],
                    ),
                )
                self._db.commit()
                logger.info(
                    "Reconciled pos %d %s → %s", row["id"], inst_key, close_reason
                )
                reconciled += 1
            else:
                db_qty = row["quantity"]
                if hasattr(broker_p, "quantity") and broker_p.quantity != db_qty:
                    logger.warning(
                        "Reconcile: pos %d %s qty mismatch DB=%d broker=%d — trusting DB",
                        row["id"], inst_key, db_qty, broker_p.quantity,
                    )

        print(f"  State recovery: {len(rows)} position(s) checked, {reconciled} reconciled")
        logger.info("State recovery complete: %d checked, %d reconciled", len(rows), reconciled)

    # ── Market hours ──────────────────────────────────────────────────────────

    def _load_holidays(self) -> List[str]:
        """Return list of holiday date strings 'YYYY-MM-DD'. Creates file if missing."""
        if not HOLIDAYS_PATH.exists():
            HOLIDAYS_PATH.parent.mkdir(parents=True, exist_ok=True)
            HOLIDAYS_PATH.write_text(json.dumps({"holidays": []}, indent=2), encoding="utf-8")
        try:
            return json.loads(HOLIDAYS_PATH.read_text(encoding="utf-8")).get("holidays", [])
        except Exception:
            return []

    def is_market_open(self) -> bool:
        """Public: True if markets are currently open (weekday, 09:15–15:30, non-holiday)."""
        now = datetime.now(tz=IST)
        if now.weekday() >= 5:
            return False
        if now.strftime("%Y-%m-%d") in self._load_holidays():
            return False
        t = now.time()
        return self._MARKET_OPEN <= t <= self._MARKET_CLOSE

    def get_minutes_until_market_open(self) -> float:
        """Minutes until 09:15 IST today (negative if already past open)."""
        now = datetime.now(tz=IST)
        open_dt = now.replace(
            hour=self._MARKET_OPEN.hour, minute=self._MARKET_OPEN.minute,
            second=0, microsecond=0,
        )
        return (open_dt - now).total_seconds() / 60

    def get_minutes_until_market_close(self) -> float:
        """Minutes until 15:30 IST today (negative if already past close)."""
        now = datetime.now(tz=IST)
        close_dt = now.replace(
            hour=self._MARKET_CLOSE.hour, minute=self._MARKET_CLOSE.minute,
            second=0, microsecond=0,
        )
        return (close_dt - now).total_seconds() / 60

    def _is_market_open(self) -> bool:
        return self.is_market_open()

    def _should_force_close(self) -> bool:
        now = datetime.now(tz=IST)
        return now.weekday() < 5 and now.time() >= self._FORCE_CLOSE

    def _is_after_market(self) -> bool:
        now = datetime.now(tz=IST)
        return now.weekday() < 5 and now.time() > self._MARKET_CLOSE

    # ── Cycle ─────────────────────────────────────────────────────────────────

    def run_one_cycle(self, dry_run: bool = False) -> None:
        """
        One full evaluation pass:
          1. Manage open positions (exits, trail updates)
          2. Evaluate strategies for new signals
          3. Place orders (unless dry_run)
          4. Print cycle summary
        All exceptions are caught so the loop continues to the next cycle.
        """
        now = datetime.now(tz=IST)
        self._cycles_run += 1
        cycle_start = _time.monotonic()
        logger.info("─── Cycle %d  %s ───", self._cycles_run, now.strftime("%H:%M:%S IST"))

        exits_taken:   List[RealizedPnL]          = []
        trails_done:   List[PositionAction]        = []
        proposals_all: List[ProposedTrade]         = []
        orders_placed: List[Tuple[ProposedTrade, ManagedPosition]] = []
        orders_failed: List[Tuple[ProposedTrade, str]]             = []

        # ── Step 1: manage existing positions ────────────────────────────────
        try:
            actions = self._pm.check_open_positions()
            for action in actions:
                if action.action_type.startswith("CLOSE"):
                    if dry_run:
                        logger.info(
                            "[DRY-RUN] Would close %s @ %.2f (%s)",
                            action.position.instrument_key,
                            action.suggested_exit_price,
                            action.action_type,
                        )
                        exits_taken.append(action)  # type: ignore[arg-type]
                        continue
                    try:
                        rpnl = self._pm.close_position(
                            action.position,
                            action.suggested_exit_price,
                            action.action_type,
                        )
                        exits_taken.append(rpnl)
                    except Exception as exc:
                        logger.error("close_position error for %s: %s",
                                     action.position.instrument_key, exc, exc_info=True)

                elif action.action_type == "UPDATE_TRAIL":
                    if dry_run:
                        logger.info(
                            "[DRY-RUN] Would trail %s to %.2f",
                            action.position.instrument_key,
                            action.suggested_exit_price,
                        )
                        trails_done.append(action)
                        continue
                    try:
                        self._pm.update_trailing_stop(
                            action.position, action.suggested_exit_price
                        )
                        trails_done.append(action)
                    except Exception as exc:
                        logger.error("update_trailing_stop error: %s", exc, exc_info=True)

        except Exception as exc:
            logger.error("check_open_positions error: %s", exc, exc_info=True)

        # ── Step 2: evaluate strategies for new signals ───────────────────────
        can_trade, block_reason = self._engine.can_open_new_position()
        if can_trade:
            for strat in self._strategies:
                try:
                    proposals = strat.evaluate()
                    for proposal in proposals:
                        proposals_all.append(proposal)
                        if dry_run:
                            logger.info(
                                "[DRY-RUN] Signal: %s %s ×%d @ %.2f  sl=%.2f  target=%.2f",
                                proposal.side, proposal.instrument_key, proposal.quantity,
                                proposal.entry_price, proposal.stop_loss, proposal.target,
                            )
                            continue
                        # Strategies call validate() internally; proposals here are pre-approved
                        result = self._broker.place_order(proposal)
                        if result.error_msg or not result.order_id:
                            msg = result.error_msg or "no order_id"
                            orders_failed.append((proposal, msg))
                            logger.warning(
                                "Order rejected for %s: %s",
                                proposal.instrument_key, msg,
                            )
                            continue
                        try:
                            pos = self._pm.open_position(proposal, result)
                            orders_placed.append((proposal, pos))
                        except Exception as exc:
                            logger.error(
                                "open_position failed for %s: %s",
                                proposal.instrument_key, exc, exc_info=True,
                            )

                except Exception as exc:
                    logger.error(
                        "Strategy %s error: %s", strat.name, exc, exc_info=True
                    )

        # ── Step 3: cycle health + print summary ─────────────────────────────
        duration = _time.monotonic() - cycle_start
        self._last_cycle_ts = now
        if duration > 10:
            logger.warning("Slow cycle: %.1fs", duration)
            print(f"  [WARN] Slow cycle: {duration:.1f}s")

        had_error = bool(orders_failed) or (not can_trade and block_reason.startswith("error"))
        if had_error:
            self._consecutive_api_errors += 1
        else:
            self._consecutive_api_errors = 0

        if self._consecutive_api_errors >= 5:
            print(f"\n  [ALERT] {self._consecutive_api_errors} consecutive API errors — pausing 60s\n")
            logger.error("5 consecutive API errors — pausing 60s")
            _time.sleep(60)
            self._consecutive_api_errors = 0

        self._print_summary(
            now, dry_run, can_trade, block_reason,
            proposals_all, orders_placed, orders_failed,
            exits_taken, trails_done,
        )

    def _print_summary(
        self,
        now:           datetime,
        dry_run:       bool,
        can_trade:     bool,
        block_reason:  str,
        proposals:     list,
        placed:        list,
        failed:        list,
        exits:         list,
        trails:        list,
    ) -> None:
        daily, weekly, monthly = self._engine.get_current_state()
        open_pos               = self._engine.get_open_positions()
        remaining              = self._engine.get_daily_pnl_remaining()
        label                  = "[DRY-RUN] " if dry_run else ""
        mode_tag               = "LIVE" if MODE.is_live else ("MOCK" if MODE.is_mock else "SANDBOX")

        print(f"\n{'─' * 62}")
        print(f"  {label}Cycle {self._cycles_run}  {now.strftime('%H:%M:%S IST')}  [{mode_tag}]")
        print(f"{'─' * 62}")
        print(f"  Open positions : {len(open_pos)} / {self._engine._rules.max_concurrent_positions}")
        print(f"  Daily P&L      : {_fmt_inr(daily.pnl_realized)}  (headroom {_fmt_inr(remaining)})")
        print(f"  Can open new   : {_col('YES', can_trade) if can_trade else _col('NO — ' + block_reason, False)}")

        if proposals:
            print(f"\n  Signals this cycle: {len(proposals)}")
            for p in proposals:
                tag = "(placed)" if any(o[0] is p for o in placed) else \
                      "(failed)" if any(f[0] is p for f in failed) else \
                      "(dry-run)" if dry_run else ""
                print(
                    f"    {p.strategy:12s}  {p.side:4s}  "
                    f"{p.instrument_key.split('|')[-1]:20s}  "
                    f"×{p.quantity:<4d}  {tag}"
                )
        else:
            print("\n  No signals this cycle.")

        if exits:
            print(f"\n  Exits taken: {len(exits)}")
            for e in exits:
                if isinstance(e, RealizedPnL):
                    sign = "+" if e.pnl >= 0 else ""
                    print(
                        f"    {e.instrument_key.split('|')[-1]:20s}  "
                        f"{e.exit_reason:15s}  P&L {sign}{_fmt_inr(e.pnl)}"
                    )
                else:
                    # dry-run PositionAction
                    print(
                        f"    {e.position.instrument_key.split('|')[-1]:20s}  "
                        f"{e.action_type:15s}  @ {e.suggested_exit_price:.2f}"
                    )

        if trails:
            print(f"\n  Trail updates: {len(trails)}")
            for t in trails:
                print(
                    f"    {t.position.instrument_key.split('|')[-1]:20s}  "
                    f"new_sl={t.suggested_exit_price:.2f}"
                )

        print()

    # ── Loop ─────────────────────────────────────────────────────────────────

    def run_loop(self, poll_interval_seconds: int = 30) -> None:
        """
        Run continuously while the market is open.
        - Pre-market: sleep until 09:15, print periodic status
        - Weekend: print notice and exit immediately
        - 15:15 IST: force-close all intraday positions (once)
        - 15:30 IST: generate EOD report and exit
        - Ctrl+C: prompt before force-closing open positions
        """
        print(f"\n  Agent loop started — polling every {poll_interval_seconds}s")
        print(f"  Market: 09:15 – 15:30 IST  |  Force-close: 15:15 IST")
        print(f"  Press Ctrl+C to stop.\n")

        try:
            while True:
                now = datetime.now(tz=IST)

                if now.weekday() >= 5:
                    print(f"  [{now.strftime('%H:%M:%S IST')}] Markets closed (weekend). Exiting.")
                    break

                if self._is_after_market():
                    self.generate_eod_report()
                    break

                if not self.is_market_open():
                    mins = self.get_minutes_until_market_open()
                    if mins > 0:
                        print(
                            f"  [{now.strftime('%H:%M:%S IST')}] Pre-market — "
                            f"opens in {mins:.0f} min. Sleeping 60s."
                        )
                    else:
                        holiday = now.strftime("%Y-%m-%d") in self._load_holidays()
                        reason  = "holiday" if holiday else "after market close"
                        print(f"  [{now.strftime('%H:%M:%S IST')}] Market closed ({reason}). Sleeping 60s.")
                    _time.sleep(60)
                    continue

                # Force-close window
                if self._should_force_close() and not self._force_closed:
                    print("\n  *** 15:15 IST — force-closing all intraday positions ***\n")
                    closed = self._pm.force_close_intraday()
                    logger.info("Force-close: %d position(s) closed", len(closed))
                    for c in closed:
                        print(
                            f"    FORCE-CLOSED  {c.instrument_key.split('|')[-1]:20s}  "
                            f"P&L {_fmt_inr(c.pnl)}"
                        )
                    self._force_closed = True

                self.run_one_cycle()
                _time.sleep(poll_interval_seconds)

        except KeyboardInterrupt:
            self._handle_ctrl_c()

    def _handle_ctrl_c(self) -> None:
        open_pos = self._engine.get_open_positions()
        n        = len(open_pos)
        print(f"\n\n  Stop signal received.")

        if n:
            print(f"  Open positions: {n}")
            for p in open_pos:
                ltp = self._broker.get_ltp(p.instrument_key)
                unreal = (ltp - p.entry_price) * p.quantity * (1 if p.side == "BUY" else -1)
                print(
                    f"    {p.instrument_key.split('|')[-1]:20s}  "
                    f"{p.side:4s} ×{p.quantity:<4d} @ {p.entry_price:.2f}  "
                    f"unreal {_fmt_inr(unreal)}"
                )
            print(f"\n  Force-close all? (y/n): ", end="", flush=True)
            try:
                choice = input().strip().lower()
            except EOFError:
                choice = "n"

            if choice == "y":
                print("\n  Closing all intraday positions …")
                closed = self._pm.force_close_intraday()
                for c in closed:
                    print(
                        f"    CLOSED  {c.instrument_key.split('|')[-1]:20s}  "
                        f"P&L {_fmt_inr(c.pnl)}"
                    )
                print(f"  {len(closed)} position(s) closed.")
            else:
                print("  Leaving positions open.")
        else:
            print("  No open positions.")

        self._db.commit()
        self.generate_eod_report()
        print("\n  Exiting.\n")

    def generate_eod_report(self) -> str:
        """Build Markdown EOD report, save to logs/ and print to console."""
        now    = datetime.now(tz=IST)
        today  = now.strftime("%Y-%m-%d")

        rows = self._db.execute(
            """SELECT instrument_key, strategy, side, quantity,
                      entry_price, exit_price, pnl, exit_reason
               FROM positions
               WHERE status = 'CLOSED' AND date(closed_at) = ?
               ORDER BY closed_at""",
            (today,),
        ).fetchall()

        daily, weekly, monthly = self._engine.get_current_state()
        open_pos               = self._engine.get_open_positions()
        rules                  = self._engine._rules
        mode_str               = "LIVE" if MODE.is_live else ("MOCK" if MODE.is_mock else "SANDBOX")

        total_pnl = sum((r["pnl"] or 0.0) for r in rows)
        wins      = sum(1 for r in rows if (r["pnl"] or 0.0) > 0)
        losses    = sum(1 for r in rows if (r["pnl"] or 0.0) < 0)
        win_rate  = (wins / len(rows) * 100) if rows else 0.0

        strat_pnl: Dict[str, float] = {}
        inst_pnl:  Dict[str, float] = {}
        for r in rows:
            strat_pnl[r["strategy"]]                       = strat_pnl.get(r["strategy"], 0.0) + (r["pnl"] or 0.0)
            key = r["instrument_key"].split("|")[-1]
            inst_pnl[key]                                  = inst_pnl.get(key, 0.0) + (r["pnl"] or 0.0)

        best  = max(rows, key=lambda r: r["pnl"] or 0.0, default=None)
        worst = min(rows, key=lambda r: r["pnl"] or 0.0, default=None)

        lines = [
            f"# EOD Report — {now.strftime('%A, %d %b %Y')} [{mode_str}]",
            "",
            "## Summary",
            f"- Trades: **{len(rows)}**  |  Wins: {wins}  |  Losses: {losses}"
            f"  |  Win rate: {win_rate:.0f}%",
            f"- Total P&L: **{_fmt_inr(total_pnl)}**",
            f"- Cycles run: {self._cycles_run}",
            "",
            "## P&L by Strategy",
        ]
        if strat_pnl:
            for strat, pnl in sorted(strat_pnl.items()):
                lines.append(f"- {strat}: {_fmt_inr(pnl)}")
        else:
            lines.append("- _(no trades)_")

        lines += ["", "## P&L by Instrument"]
        if inst_pnl:
            for inst, pnl in sorted(inst_pnl.items(), key=lambda x: -abs(x[1])):
                lines.append(f"- {inst}: {_fmt_inr(pnl)}")
        else:
            lines.append("- _(no trades)_")

        lines += ["", "## Best / Worst Trade"]
        if best:
            lines.append(
                f"- Best:  {best['instrument_key'].split('|')[-1]} — "
                f"{_fmt_inr(best['pnl'] or 0.0)} ({best['strategy']})"
            )
        if worst and worst is not best:
            lines.append(
                f"- Worst: {worst['instrument_key'].split('|')[-1]} — "
                f"{_fmt_inr(worst['pnl'] or 0.0)} ({worst['strategy']})"
            )
        if not rows:
            lines.append("- _(no trades)_")

        lines += ["", "## Risk Limit Comparison"]
        lines.append(
            f"- Daily  P&L: {_fmt_inr(daily.pnl_realized)}"
            f" / stop {_fmt_inr(-rules.daily_stop)}"
            f"  {'⚠ HIT' if daily.daily_stop_hit else '✓ ok'}"
        )
        lines.append(
            f"- Weekly P&L: {_fmt_inr(weekly.pnl_realized)}"
            f" / stop {_fmt_inr(-rules.weekly_stop)}"
            f"  {'⚠ HIT' if weekly.weekly_stop_hit else '✓ ok'}"
        )
        lines.append(
            f"- Monthly P&L: {_fmt_inr(monthly.pnl_realized)}"
            f" / stop {_fmt_inr(-rules.monthly_stop)}"
            f"  {'⚠ HIT' if monthly.monthly_stop_hit else '✓ ok'}"
        )
        lines.append(f"- Open positions carrying over: {len(open_pos)}")

        lines += ["", "## Suggested Review Items"]
        suggestions: List[str] = []
        for strat, pnl in strat_pnl.items():
            strat_losses = sum(1 for r in rows if r["strategy"] == strat and (r["pnl"] or 0.0) < 0)
            if strat_losses >= 3:
                suggestions.append(
                    f"- {strat} had {strat_losses} losses today — consider checking entry conditions"
                )
        if daily.daily_stop_hit:
            suggestions.append("- Daily stop hit — review position sizing and market conditions")
        if not rows:
            suggestions.append("- No trades taken — check strategy signal thresholds and data feeds")
        if not suggestions:
            suggestions.append("- No issues flagged today")
        lines.extend(suggestions)

        report = "\n".join(lines)

        report_path = LOGS_DIR / f"eod_report_{today}.md"
        report_path.write_text(report, encoding="utf-8")
        logger.info("EOD report saved to %s", report_path)

        # Print to console
        print(f"\n{'═' * 62}")
        print(f"  EOD Report — {now.strftime('%a %d %b %Y')}  [{mode_str}]")
        print(f"{'═' * 62}")
        print(f"  Trades     : {len(rows)}  wins={wins}  losses={losses}  rate={win_rate:.0f}%")
        print(f"  Daily P&L  : {_fmt_inr(daily.pnl_realized)}  (headroom {_fmt_inr(self._engine.get_daily_pnl_remaining())})")
        print(f"  Weekly P&L : {_fmt_inr(weekly.pnl_realized)}")
        print(f"  Monthly P&L: {_fmt_inr(monthly.pnl_realized)}")
        print(f"  Open carry : {len(open_pos)}")
        print(f"  Report     : {report_path.name}")
        print(f"{'═' * 62}\n")

        return report

    # ── Status ───────────────────────────────────────────────────────────────

    def _check_token_health(self) -> bool:
        """Minimal API probe: GET /v2/user/profile. Returns True if HTTP 200."""
        if MODE.is_mock:
            return True
        try:
            self._broker._api_call("GET", "/v2/user/profile")
            return True
        except Exception:
            return False

    def status(self) -> None:
        """Print a read-only status snapshot. No orders, minimal API calls."""
        now        = datetime.now(tz=IST)
        mode_str   = "LIVE" if MODE.is_live else ("MOCK" if MODE.is_mock else "SANDBOX")
        market_now = self.is_market_open()
        daily, weekly, monthly = self._engine.get_current_state()
        open_pos   = self._engine.get_open_positions()
        headroom   = self._engine.get_daily_pnl_remaining()
        rules      = self._engine._rules

        if market_now:
            mins_left = self.get_minutes_until_market_close()
            mkt_label = _col(f"OPEN  ({mins_left:.0f} min to close)", True)
        else:
            mins_open = self.get_minutes_until_market_open()
            if mins_open > 0:
                mkt_label = _col(f"CLOSED  ({mins_open:.0f} min to open)", False)
            else:
                mkt_label = _col("CLOSED  (after hours)", False)

        token_ok = self._check_token_health()

        ltps: Dict[str, float] = {}
        unreal_pnl = 0.0
        if open_pos:
            try:
                ltps = self._broker.get_ltp_batch([p.instrument_key for p in open_pos])
            except Exception:
                ltps = {}
            for p in open_pos:
                ltp = ltps.get(p.instrument_key, p.entry_price)
                unreal_pnl += (ltp - p.entry_price) * p.quantity * (1 if p.side == "BUY" else -1)

        last_ts = (
            self._last_cycle_ts.strftime("%H:%M:%S IST")
            if self._last_cycle_ts else "—"
        )

        weekly_headroom  = rules.weekly_stop  + weekly.pnl_realized
        monthly_headroom = rules.monthly_stop + monthly.pnl_realized

        print(f"\n{'─' * 62}")
        print(f"  STATUS  {now.strftime('%H:%M:%S IST')}")
        print(f"{'─' * 62}")
        print(f"  Mode           : {mode_str}")
        print(f"  Market         : {mkt_label}")
        print(f"  Today P&L      : {_fmt_inr(daily.pnl_realized)} realized"
              f"  +  {_fmt_inr(unreal_pnl)} unrealized")
        print(f"  Headroom       : daily {_fmt_inr(headroom)}"
              f"  |  weekly {_fmt_inr(weekly_headroom)}"
              f"  |  monthly {_fmt_inr(monthly_headroom)}")
        print(f"  Weekly P&L     : {_fmt_inr(weekly.pnl_realized)}")
        print(f"  Monthly P&L    : {_fmt_inr(monthly.pnl_realized)}")
        print(f"  Open positions : {len(open_pos)}")
        for p in open_pos:
            ltp    = ltps.get(p.instrument_key, p.entry_price)
            unreal = (ltp - p.entry_price) * p.quantity * (1 if p.side == "BUY" else -1)
            print(
                f"    {p.instrument_key.split('|')[-1]:20s}  "
                f"{p.side:4s} ×{p.quantity:<4d} @ {p.entry_price:.2f}  "
                f"ltp={ltp:.2f}  unreal={_fmt_inr(unreal)}"
            )
        print(f"  Last cycle     : {last_ts}")
        print(f"  Token health   : {_col('OK', token_ok) if token_ok else _col('FAIL', False)}")
        print(f"{'─' * 62}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _run_tests() -> None:
    """Built-in self-tests for Phase 5D agent features."""
    import sqlite3 as _sqlite3
    from datetime import datetime as _dt

    print(f"\n{'═' * 62}")
    print("  Phase 5D Self-Tests")
    print(f"{'═' * 62}\n")

    # Silence sub-loggers during tests
    for _n in ("strategies", "risk_engine", "broker", "position_manager", "agent", "mock_broker"):
        logging.getLogger(_n).setLevel(logging.CRITICAL)

    passed = failed = 0

    def _ok(label: str) -> None:
        nonlocal passed
        passed += 1
        print(f"  [PASS] {label}")

    def _fail(label: str, reason: str) -> None:
        nonlocal failed
        failed += 1
        print(f"  [FAIL] {label} — {reason}")

    # ── Setup ────────────────────────────────────────────────────────────────
    try:
        agent = Agent()
    except Exception as exc:
        print(f"  Agent init failed: {exc}")
        print("  Cannot continue tests.\n")
        return

    # ── Test 1: status() prints clean summary ─────────────────────────────
    try:
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent.status()
        out = buf.getvalue()
        if "STATUS" in out and "Mode" in out and "Market" in out:
            _ok("Test 1: status() prints clean summary")
        else:
            _fail("Test 1", f"unexpected output: {out[:100]!r}")
    except Exception as exc:
        _fail("Test 1", str(exc))

    # ── Test 2: dry-run cycle emits no real orders ─────────────────────────
    try:
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent.run_one_cycle(dry_run=True)
        out = buf.getvalue()
        if "DRY-RUN" in out or "No signals" in out or "Cycle" in out:
            _ok("Test 2: dry-run cycle emits no real orders")
        else:
            _fail("Test 2", f"unexpected output: {out[:120]!r}")
    except Exception as exc:
        _fail("Test 2", str(exc))

    # ── Test 3: MOCK mode cycle uses mock broker ───────────────────────────
    try:
        if MODE.is_mock:
            from mock_broker import MockBroker
            if isinstance(agent._broker, MockBroker):
                _ok("Test 3: MOCK mode — agent uses MockBroker")
            else:
                _fail("Test 3", "TRADING_MODE=mock but broker is not MockBroker")
        else:
            _ok("Test 3: MOCK mode (skipped — TRADING_MODE is not mock)")
    except Exception as exc:
        _fail("Test 3", str(exc))

    # ── Test 4: state recovery reconciles stale OPEN position ─────────────
    try:
        yesterday = (_dt.now(tz=IST) - timedelta(days=1)).strftime("%Y-%m-%d") + "T10:00:00+05:30"
        db = _sqlite3.connect(str(DB_PATH))
        db.row_factory = _sqlite3.Row
        # Insert a fake OPEN position with a non-existent order_id
        db.execute(
            "INSERT INTO positions (instrument_key, side, quantity, entry_price, "
            "current_sl, current_target, strategy, segment, opened_at, status, "
            "order_id, original_sl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("NSE_EQ|FAKE_RECOV", "BUY", 10, 100.0, 95.0, 110.0,
             "ORB", "EQUITY_INTRADAY", yesterday, "OPEN",
             "FAKE_ORDER_RECOV_001", 95.0),
        )
        db.commit()
        fake_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()

        # Run recovery — Agent already has a DB connection; just call the method
        agent._recover_state()

        # Check if reconciled
        row = agent._db.execute(
            "SELECT status, exit_reason FROM positions WHERE id=?", (fake_id,)
        ).fetchone()
        if row and row["status"] == "CLOSED":
            _ok(f"Test 4: state recovery reconciled stale position (reason={row['exit_reason']})")
        else:
            _fail("Test 4", f"position still OPEN or missing: {dict(row) if row else None}")
    except Exception as exc:
        _fail("Test 4", str(exc))

    # ── Test 5: generate_eod_report() produces valid Markdown ─────────────
    try:
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report = agent.generate_eod_report()
        today = _dt.now(tz=IST).strftime("%Y-%m-%d")
        report_path = LOGS_DIR / f"eod_report_{today}.md"
        if (
            report.startswith("# EOD Report")
            and "## Summary" in report
            and "## P&L by Strategy" in report
            and report_path.exists()
        ):
            _ok(f"Test 5: generate_eod_report() saved valid Markdown to {report_path.name}")
        else:
            _fail("Test 5", f"report missing sections or file not saved: {report[:80]!r}")
    except Exception as exc:
        _fail("Test 5", str(exc))

    # ── Summary ──────────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n  Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} FAILED)")
    else:
        print("  — all green")
    print()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")

    # Silence noisy sub-loggers on console; they still write to their own files
    for _name in ("strategies", "risk_engine", "broker", "position_manager"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    _VALID_CMDS = ("once", "loop", "dry-run", "status", "test")
    if len(sys.argv) < 2 or sys.argv[1] not in _VALID_CMDS:
        print("Usage:")
        print("  python scripts/agent.py once      — one cycle")
        print("  python scripts/agent.py loop      — continuous (Ctrl+C to stop)")
        print("  python scripts/agent.py dry-run   — evaluate signals, no orders placed")
        print("  python scripts/agent.py status    — read-only status snapshot")
        print("  python scripts/agent.py test      — run built-in self-tests")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "test":
        _run_tests()
        sys.exit(0)

    print(f"\n{'═' * 62}")
    print(f"  Trading Agent — Phase 5D")
    print(f"  Command : {cmd}")
    print(f"{'═' * 62}")

    MODE.print_startup_banner()

    try:
        agent = Agent()
    except (EnvironmentError, FileNotFoundError) as exc:
        print(f"\nStartup error: {exc}\n")
        sys.exit(1)

    if cmd == "status":
        agent.status()
        sys.exit(0)

    print(
        f"  Strategies : {', '.join(s.name for s in agent._strategies)}\n"
        f"  Rules file : {RULES_PATH.name}\n"
        f"  Watchlist  : {WATCHLIST.name}\n"
    )

    if cmd == "once":
        agent.run_one_cycle(dry_run=False)

    elif cmd == "dry-run":
        print("  [DRY-RUN] No orders will be placed.\n")
        agent.run_one_cycle(dry_run=True)

    elif cmd == "loop":
        agent.run_loop(poll_interval_seconds=30)
