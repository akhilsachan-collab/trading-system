"""
run_strategies.py — One-cycle strategy evaluation runner.

Initialises the RiskEngine and all three strategy modules, runs a single
evaluate() cycle, and prints every proposal in a formatted table.

This script does NOT place orders. It is used for testing signal generation
and verifying that the full pipeline (data → signal → risk validation) works.

Usage:
    python scripts/run_strategies.py
"""

import sys
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make scripts/ importable from any working directory
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from risk_engine import RiskEngine
from strategies.orb           import OpeningRangeBreakout
from strategies.momentum      import MomentumBreakout
from strategies.mean_reversion import MeanReversion

try:
    from tabulate import tabulate
    _HAS_TABULATE = True
except ImportError:
    _HAS_TABULATE = False

IST = timezone(timedelta(hours=5, minutes=30))


def _fmt_inr(n: float) -> str:
    sign = "-" if n < 0 else ""
    integer_part, _, dec = f"{abs(n):.0f}".partition(".")
    if len(integer_part) <= 3:
        return f"{sign}₹{integer_part}"
    last3  = integer_part[-3:]
    head   = integer_part[:-3]
    groups = []
    while head:
        groups.append(head[-2:])
        head = head[:-2]
    groups.reverse()
    return f"{sign}₹{','.join(groups)},{last3}"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    # Silence strategy + engine loggers so the console stays clean
    logging.getLogger("strategies").setLevel(logging.WARNING)
    logging.getLogger("risk_engine").setLevel(logging.WARNING)

    now = datetime.now(tz=IST)
    print(f"\n{'═' * 62}")
    print(f"  Strategy Runner — {now.strftime('%a %d %b %Y  %H:%M:%S IST')}")
    print(f"{'═' * 62}\n")

    # ── Initialise engine and strategies ─────────────────────────────────────
    print("  Initialising RiskEngine …", end=" ", flush=True)
    try:
        engine = RiskEngine()
        print("OK")
    except Exception as exc:
        print(f"FAILED\n  {exc}")
        sys.exit(1)

    strategies = [
        OpeningRangeBreakout(engine),
        MomentumBreakout(engine),
        MeanReversion(engine),
    ]
    print(f"  Strategies loaded: {', '.join(s.name for s in strategies)}\n")

    # ── Quick pre-flight check ────────────────────────────────────────────────
    can_trade, reason = engine.can_open_new_position()
    remaining = engine.get_daily_pnl_remaining()
    print(f"  Can open new position : {'YES' if can_trade else 'NO — ' + reason}")
    print(f"  Daily P&L headroom    : {_fmt_inr(remaining)}")
    print()

    # ── Run evaluation cycle ──────────────────────────────────────────────────
    all_proposals = []
    for strat in strategies:
        print(f"  [{strat.name}] evaluating …", end=" ", flush=True)
        try:
            proposals = strat.evaluate()
            all_proposals.extend(proposals)
            tag = f"{len(proposals)} proposal(s)" if proposals else "no signals"
            print(tag)
        except Exception as exc:
            print(f"ERROR — {exc}")
            logging.getLogger("strategies").exception("run_strategies: %s failed", strat.name)

    print()

    # ── Print proposals ───────────────────────────────────────────────────────
    if not all_proposals:
        print("  No signals this cycle — all evaluate() calls returned empty.\n")
        print("  This is normal outside market hours or when no conditions are met.")
    else:
        rows = [
            [
                p.strategy,
                p.instrument_key.split("|")[-1],
                p.side,
                p.quantity,
                f"{p.entry_price:.2f}",
                f"{p.stop_loss:.2f}",
                f"{p.target:.2f}",
                p.segment,
                _fmt_inr(p.trade_risk),
            ]
            for p in all_proposals
        ]
        headers = ["Strategy", "Instrument", "Side", "Qty", "Entry", "SL", "Target", "Segment", "Risk"]
        if _HAS_TABULATE:
            print(tabulate(rows, headers=headers, tablefmt="rounded_outline"))
        else:
            col_w = [max(len(str(r[i])) for r in ([headers] + rows)) for i in range(len(headers))]
            sep = "  ".join("-" * w for w in col_w)
            fmt = "  ".join(f"{{:<{w}}}" for w in col_w)
            print("  " + fmt.format(*headers))
            print("  " + sep)
            for row in rows:
                print("  " + fmt.format(*row))

    # ── Open positions summary ────────────────────────────────────────────────
    open_pos = engine.get_open_positions()
    print(f"\n  Open positions in DB : {len(open_pos)} / {engine._rules.max_concurrent_positions}")
    print()


if __name__ == "__main__":
    main()
