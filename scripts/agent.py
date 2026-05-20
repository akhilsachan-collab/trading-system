"""
agent.py — Main trading agent orchestrator (Phase 5C).

Initialises RiskEngine, Broker, PositionManager, and all three strategies,
then runs one-cycle or continuous-loop evaluation.

Market hours: 09:15 – 15:30 IST, Monday – Friday.
Force-close intraday at 15:15. EOD report at 15:30.

CLI:
    python scripts/agent.py once      # single cycle
    python scripts/agent.py loop      # continuous, polls every 30 s
    python scripts/agent.py dry-run   # evaluate signals, no orders placed
"""

import logging
import logging.handlers
import os
import sys
import time as _time
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────

_SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _SCRIPTS_DIR.parent
ENV_PATH     = PROJECT_ROOT / ".env"
RULES_PATH   = PROJECT_ROOT / "TRADING_RULES.md"
WATCHLIST    = PROJECT_ROOT / "watchlist.json"
LOGS_DIR     = PROJECT_ROOT / "logs"

load_dotenv(ENV_PATH)

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from broker import Broker, MODE                                   # noqa: E402
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

        self._engine     = RiskEngine()
        self._broker     = Broker(access_token=token)
        self._pm         = PositionManager(self._engine, self._broker)
        self._strategies = [
            OpeningRangeBreakout(self._engine),
            MomentumBreakout(self._engine),
            MeanReversion(self._engine),
        ]
        self._cycles_run  = 0
        self._force_closed = False

        logger.info(
            "Agent ready — mode=%s  strategies=%s",
            "LIVE" if MODE.is_live else "SANDBOX",
            [s.name for s in self._strategies],
        )

    # ── Market hours ──────────────────────────────────────────────────────────

    def _is_market_open(self) -> bool:
        now = datetime.now(tz=IST)
        if now.weekday() >= 5:      # Saturday / Sunday
            return False
        t = now.time()
        return self._MARKET_OPEN <= t <= self._MARKET_CLOSE

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

        # ── Step 3: print cycle summary ───────────────────────────────────────
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
        mode_tag               = "SANDBOX" if MODE.is_sandbox else "LIVE"

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
        - 15:15 IST: force-close all intraday positions (once)
        - 15:30 IST: print EOD report and exit
        - Ctrl+C: prompt before force-closing open positions
        """
        print(f"\n  Agent loop started — polling every {poll_interval_seconds}s")
        print(f"  Market: 09:15 – 15:30 IST  |  Force-close: 15:15 IST")
        print(f"  Press Ctrl+C to stop.\n")

        try:
            while True:
                if self._is_after_market():
                    self._print_eod_report()
                    break

                if not self._is_market_open():
                    now = datetime.now(tz=IST)
                    wait_msg = (
                        "before market open — waiting"
                        if now.time() < self._MARKET_OPEN
                        else "market closed today (weekend)"
                    )
                    print(f"  [{now.strftime('%H:%M:%S IST')}] {wait_msg}")
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
        print(f"\n\n  Stop signal received. Open positions: {n}")
        if n == 0:
            print("  No open positions. Exiting cleanly.\n")
            return

        print(f"  Press y to FORCE-CLOSE all {n} position(s) now,")
        print("  Press n to leave them open (e.g. swing positions).")
        print("  Your choice [y/n]: ", end="", flush=True)
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
            print(f"  {len(closed)} position(s) closed. Exiting.\n")
        else:
            print("  Leaving positions open. Exiting without closing.\n")

    def _print_eod_report(self) -> None:
        daily, weekly, monthly = self._engine.get_current_state()
        open_pos               = self._engine.get_open_positions()
        now                    = datetime.now(tz=IST)

        print(f"\n{'═' * 62}")
        print(f"  EOD Report — {now.strftime('%a %d %b %Y')}")
        print(f"{'═' * 62}")
        print(f"  Cycles run     : {self._cycles_run}")
        print(f"  Trades taken   : {daily.trades_taken}")
        print(f"  Daily P&L      : {_fmt_inr(daily.pnl_realized)}")
        print(f"  Weekly P&L     : {_fmt_inr(weekly.pnl_realized)}")
        print(f"  Monthly P&L    : {_fmt_inr(monthly.pnl_realized)}")
        print(f"  Positions open : {len(open_pos)}  (swing positions carry over)")
        print(f"{'═' * 62}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")

    # Silence noisy sub-loggers on console; they still write to their own files
    for _name in ("strategies", "risk_engine", "broker", "position_manager"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    if len(sys.argv) < 2 or sys.argv[1] not in ("once", "loop", "dry-run"):
        print("Usage:")
        print("  python scripts/agent.py once      — one cycle")
        print("  python scripts/agent.py loop      — continuous (Ctrl+C to stop)")
        print("  python scripts/agent.py dry-run   — evaluate signals, no orders placed")
        sys.exit(1)

    cmd = sys.argv[1]

    print(f"\n{'═' * 62}")
    print(f"  Trading Agent — Phase 5C")
    print(f"  Command : {cmd}")
    print(f"{'═' * 62}")

    MODE.print_startup_banner()

    try:
        agent = Agent()
    except (EnvironmentError, FileNotFoundError) as exc:
        print(f"\nStartup error: {exc}\n")
        sys.exit(1)

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
