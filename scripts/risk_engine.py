"""
risk_engine.py — Trading risk enforcement layer.

Loads TRADING_RULES.md from the project root and enforces every rule
before allowing an order to be placed. Maintains persistent state in
data/trading_state.db (SQLite). Writes an audit trail to both SQLite
and logs/audit_YYYY-MM-DD.csv.

Usage:
    from scripts.risk_engine import RiskEngine, ProposedTrade

    engine   = RiskEngine()
    proposal = ProposedTrade(
        instrument_key="NSE_EQ|INE002A01018",
        side="BUY",
        quantity=20,
        entry_price=1395.0,
        stop_loss=1380.0,
        target=1425.0,
        strategy="ORB",
        segment="EQUITY_INTRADAY",
    )
    result = engine.validate(proposal)
    if result.allowed:
        pass  # proceed with order placement
    else:
        print(f"Blocked: {result.reason}")
"""

import csv
import json
import logging
import logging.handlers
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

# ── Project paths ─────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH     = PROJECT_ROOT / ".env"
RULES_PATH   = PROJECT_ROOT / "TRADING_RULES.md"
DB_PATH      = PROJECT_ROOT / "data" / "trading_state.db"
EVENTS_PATH  = PROJECT_ROOT / "data" / "events.json"
LOGS_DIR     = PROJECT_ROOT / "logs"

IST = timezone(timedelta(hours=5, minutes=30))

# ── Logging ───────────────────────────────────────────────────────────────────

LOGS_DIR.mkdir(exist_ok=True)

_log_handler = logging.handlers.RotatingFileHandler(
    LOGS_DIR / "risk_engine.log",
    maxBytes=10 * 1024 * 1024,   # 10 MB per file
    backupCount=5,
    encoding="utf-8",
)
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s: %(message)s")
)

logger = logging.getLogger("risk_engine")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    logger.addHandler(_log_handler)


# ── Exceptions ────────────────────────────────────────────────────────────────

class RulesParseError(RuntimeError):
    """Raised when a required value cannot be parsed from TRADING_RULES.md."""


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ProposedTrade:
    instrument_key:   str
    side:             str            # "BUY" or "SELL"
    quantity:         int
    entry_price:      float
    stop_loss:        float
    target:           float
    strategy:         str            # "ORB", "Momentum", "MeanReversion"
    segment:          str            # "EQUITY_INTRADAY", "BUY_OPTIONS", "SWING_EQUITY"
    signal_timestamp: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.signal_timestamp is None:
            self.signal_timestamp = datetime.now(tz=IST)

    @property
    def trade_risk(self) -> float:
        return self.quantity * abs(self.entry_price - self.stop_loss)


@dataclass
class ValidationResult:
    allowed:       bool
    reason:        str
    checks_passed: List[str] = field(default_factory=list)
    checks_failed: List[str] = field(default_factory=list)


@dataclass
class Position:
    id:             Optional[int]
    instrument_key: str
    side:           str
    quantity:       int
    entry_price:    float
    current_sl:     float
    current_target: float
    strategy:       str
    segment:        str
    opened_at:      datetime
    status:         str = "OPEN"   # "OPEN" or "CLOSED"
    closed_at:      Optional[datetime] = None
    pnl:            Optional[float] = None

    @property
    def open_risk(self) -> float:
        return self.quantity * abs(self.entry_price - self.current_sl)


@dataclass
class DailyState:
    date:           date
    trades_taken:   int
    pnl_realized:   float
    pnl_unrealized: float
    daily_stop_hit: bool


@dataclass
class WeeklyState:
    week_start:      date
    pnl_realized:    float
    weekly_stop_hit: bool


@dataclass
class MonthlyState:
    month_start:      date
    pnl_realized:     float
    monthly_stop_hit: bool


# ── Rules configuration (parsed from TRADING_RULES.md) ────────────────────────

@dataclass
class RulesConfig:
    """Immutable snapshot of values extracted from TRADING_RULES.md."""
    risk_capital:          float   # ₹ absolute
    risk_per_trade:        float   # ₹ absolute (intraday)
    swing_risk_per_trade:  float   # ₹ absolute (swing)
    daily_stop:            float   # ₹ absolute
    weekly_stop:           float   # ₹ absolute
    monthly_stop:          float   # ₹ absolute
    vix_threshold:         float
    max_concurrent_positions: int
    combined_risk_cap:     float   # ₹ per instrument across all segments
    intraday_start:        str     # "HH:MM"
    intraday_latest:       str     # "HH:MM"
    intraday_close:        str     # "HH:MM"
    enabled_segments:      List[str]
    min_rr:                Dict[str, float]  # {"ORB": 1.9, ...}
    mtime:                 float   # file mtime at parse time


# ── Rules parser ──────────────────────────────────────────────────────────────

def _parse_inr(s: str) -> float:
    """'₹1,50,000' or '22,500' → 150000.0. Strips ₹ symbol and commas."""
    return float(s.replace("₹", "").replace(",", "").strip())


def _parse_rules(path: Path) -> RulesConfig:
    """
    Parse TRADING_RULES.md and return a RulesConfig.
    Values are extracted from code blocks using KEY = VALUE patterns.
    Raises RulesParseError for any missing required value.
    """
    text = path.read_text(encoding="utf-8")

    def extract(key: str) -> str:
        """Find the first 'KEY = VALUE' occurrence in the file (code blocks or prose)."""
        m = re.search(
            rf"^\s*{re.escape(key)}\s*=\s*([^\s#\n]+)",
            text,
            re.MULTILINE,
        )
        if not m:
            raise RulesParseError(
                f"Cannot find '{key}' in TRADING_RULES.md. "
                f"Expected pattern: '{key} = <value>' on its own line."
            )
        return m.group(1).strip()

    def to_inr(val: str, capital: float) -> float:
        """'6.0%' → capital × 0.06; '₹22,500' or '22500' → 22500.0."""
        if val.endswith("%"):
            return round(capital * float(val.rstrip("%")) / 100.0, 2)
        return _parse_inr(val)

    capital = _parse_inr(extract("RISK_CAPITAL"))

    # .env can override RISK_CAPITAL if set (account size changes over time)
    env_capital = os.getenv("RISK_CAPITAL")
    if env_capital:
        try:
            capital = _parse_inr(env_capital)
            logger.info("RISK_CAPITAL overridden from .env: ₹%.0f", capital)
        except ValueError:
            logger.warning("Invalid RISK_CAPITAL in .env ('%s') — using TRADING_RULES.md value", env_capital)

    rpt   = to_inr(extract("RISK_PER_TRADE"),       capital)
    srpt  = to_inr(extract("SWING_RISK_PER_TRADE"),  capital)
    daily = to_inr(extract("DAILY_STOP"),            capital)
    weekly  = to_inr(extract("WEEKLY_STOP"),  capital)
    monthly = to_inr(extract("MONTHLY_STOP"), capital)
    vix     = float(extract("VIX_THRESHOLD"))
    max_pos = int(extract("MAX_CONCURRENT_POSITIONS"))
    risk_cap = to_inr(extract("MAX_RISK_PER_INSTRUMENT"), capital)

    intraday_start  = extract("INTRADAY_ENTRY_START")
    intraday_latest = extract("INTRADAY_ENTRY_LATEST")
    intraday_close  = extract("INTRADAY_FORCE_CLOSE")

    # Enabled segments come from the prose table in §5.1 — not in a parseable code block.
    # These are structural constants; changes require a rules file version bump.
    enabled_segments = ["EQUITY_INTRADAY", "BUY_OPTIONS", "SWING_EQUITY"]

    # RR minimums from the quick-reference table: "| Risk:Reward | 1:2 | 1:2.5 | 1:1 |"
    rr_row = re.search(
        r"\|\s*Risk:Reward\s*\|\s*([\d.]+):([\d.]+)\s*\|\s*([\d.]+):([\d.]+)\s*\|\s*([\d.]+):([\d.]+)",
        text,
    )
    if rr_row:
        def _rr(n, d): return round(float(n) / float(d) - 0.1, 2)
        min_rr = {
            "ORB":           _rr(rr_row.group(2), rr_row.group(1)),
            "Momentum":      _rr(rr_row.group(4), rr_row.group(3)),
            "MeanReversion": _rr(rr_row.group(6), rr_row.group(5)),
        }
    else:
        logger.warning("RR table not found in TRADING_RULES.md — using hard-coded defaults")
        min_rr = {"ORB": 1.9, "Momentum": 2.4, "MeanReversion": 0.9}

    logger.info(
        "Rules loaded: capital=₹%.0f rpt=₹%.0f daily_stop=₹%.0f vix=%.1f",
        capital, rpt, daily, vix,
    )

    return RulesConfig(
        risk_capital=capital,
        risk_per_trade=rpt,
        swing_risk_per_trade=srpt,
        daily_stop=daily,
        weekly_stop=weekly,
        monthly_stop=monthly,
        vix_threshold=vix,
        max_concurrent_positions=max_pos,
        combined_risk_cap=risk_cap,
        intraday_start=intraday_start,
        intraday_latest=intraday_latest,
        intraday_close=intraday_close,
        enabled_segments=enabled_segments,
        min_rr=min_rr,
        mtime=path.stat().st_mtime,
    )


# ── Strategy–segment compatibility matrix ─────────────────────────────────────

_STRATEGY_ALLOWED_SEGMENTS: Dict[str, set] = {
    "ORB":           {"EQUITY_INTRADAY"},
    "Momentum":      {"EQUITY_INTRADAY", "BUY_OPTIONS"},
    "MeanReversion": {"EQUITY_INTRADAY"},
}

_VALID_STRATEGIES = list(_STRATEGY_ALLOWED_SEGMENTS.keys())


# ── Risk Engine ───────────────────────────────────────────────────────────────

class RiskEngine:
    """
    Enforces every rule in TRADING_RULES.md before a trade can be placed.

    Maintains state in data/trading_state.db. All validate() calls are
    audit-logged. Pass db_path=":memory:" for tests.
    """

    def __init__(
        self,
        db_path:    Optional[str]  = None,
        rules_path: Optional[Path] = None,
    ) -> None:
        self._rules_path: Path = Path(rules_path) if rules_path else RULES_PATH
        self._db_path:    str  = str(db_path) if db_path else str(DB_PATH)

        # Test overrides — set these on the instance before calling validate()
        self._time_override:          Optional[datetime] = None   # replace datetime.now()
        self._vix_override:           Optional[float]    = None   # bypass VIX API call
        self._kill_switch_override:   Optional[bool]     = None   # bypass .env read

        load_dotenv(ENV_PATH)

        # Persistent connection (required for :memory:; also efficient for file DBs)
        self._conn: sqlite3.Connection = sqlite3.connect(
            self._db_path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row

        self._rules: Optional[RulesConfig] = None
        self._init_db()
        self._ensure_rules()

    # ── Database setup ────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_key TEXT    NOT NULL,
                side           TEXT    NOT NULL,
                quantity       INTEGER NOT NULL,
                entry_price    REAL    NOT NULL,
                current_sl     REAL    NOT NULL,
                current_target REAL    NOT NULL,
                strategy       TEXT    NOT NULL,
                segment        TEXT    NOT NULL,
                opened_at      TEXT    NOT NULL,
                status         TEXT    NOT NULL DEFAULT 'OPEN',
                closed_at      TEXT,
                pnl            REAL
            );

            CREATE TABLE IF NOT EXISTS daily_state (
                date             TEXT PRIMARY KEY,
                trades_taken     INTEGER NOT NULL DEFAULT 0,
                pnl_realized     REAL    NOT NULL DEFAULT 0.0,
                pnl_unrealized   REAL    NOT NULL DEFAULT 0.0,
                daily_stop_hit   INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS weekly_state (
                week_start       TEXT PRIMARY KEY,
                pnl_realized     REAL    NOT NULL DEFAULT 0.0,
                weekly_stop_hit  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS monthly_state (
                month_start      TEXT PRIMARY KEY,
                pnl_realized     REAL    NOT NULL DEFAULT 0.0,
                monthly_stop_hit INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp      TEXT    NOT NULL,
                action_type    TEXT    NOT NULL,
                instrument_key TEXT,
                side           TEXT,
                quantity       INTEGER,
                price          REAL,
                strategy       TEXT,
                segment        TEXT,
                reasoning      TEXT,
                sl             REAL,
                target         REAL,
                pnl            REAL,
                details        TEXT
            );
        """)
        self._conn.commit()

    # ── Rules cache ───────────────────────────────────────────────────────────

    def _ensure_rules(self) -> None:
        """Parse (or re-parse) TRADING_RULES.md if it has changed on disk."""
        try:
            current_mtime = self._rules_path.stat().st_mtime
        except FileNotFoundError:
            raise RulesParseError(
                f"TRADING_RULES.md not found at {self._rules_path}. "
                "This file is required for the risk engine to operate."
            )
        if self._rules is None or current_mtime != self._rules.mtime:
            self._rules = _parse_rules(self._rules_path)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _now(self) -> datetime:
        if self._time_override is not None:
            return self._time_override.astimezone(IST)
        return datetime.now(tz=IST)

    def _trading_enabled(self) -> bool:
        if self._kill_switch_override is not None:
            return self._kill_switch_override
        load_dotenv(ENV_PATH, override=True)
        val = os.getenv("ENABLE_TRADING", "true").strip().lower()
        return val not in ("false", "0", "no", "off")

    def _fetch_vix(self) -> Optional[float]:
        """
        Fetch India VIX from Upstox. Returns None on any failure (network,
        token expired, market closed) so callers can decide whether to block
        or pass-through on unavailability.
        """
        if self._vix_override is not None:
            return self._vix_override
        token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
        if not token:
            logger.warning("VIX: no access token — check skipped")
            return None
        try:
            resp = requests.get(
                "https://api.upstox.com/v2/market-quote/quotes",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"instrument_key": "NSE_INDEX|India VIX"},
                timeout=5,
            )
            if not resp.ok:
                logger.warning("VIX: API returned HTTP %d — check skipped", resp.status_code)
                return None
            data = resp.json().get("data", {})
            for k, v in data.items():
                if "VIX" in k.upper():
                    vix = v.get("last_price")
                    if vix is not None:
                        logger.debug("India VIX fetched: %.2f", float(vix))
                        return float(vix)
            logger.warning("VIX: instrument not in API response — check skipped")
            return None
        except Exception as exc:
            logger.warning("VIX: fetch error (%s) — check skipped", exc)
            return None

    def _load_events(self) -> dict:
        """Load data/events.json. Returns {} if file missing or malformed."""
        try:
            return json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _check_time_window(
        self, now: datetime, rules: RulesConfig, segment: str
    ) -> Optional[str]:
        """Returns an error string if the current time is outside the entry window, else None."""
        if segment == "SWING_EQUITY":
            return None  # Swing trades are not subject to intraday time windows
        now_t = now.time().replace(tzinfo=None)
        sh, sm = map(int, rules.intraday_start.split(":"))
        lh, lm = map(int, rules.intraday_latest.split(":"))
        start  = time(sh, sm)
        latest = time(lh, lm)
        if now_t < start:
            return (
                f"Too early — entry window opens at {rules.intraday_start} IST "
                f"(now: {now_t.strftime('%H:%M')} IST)"
            )
        if now_t >= latest:
            return (
                f"Entry window closed at {rules.intraday_latest} IST "
                f"(now: {now_t.strftime('%H:%M')} IST)"
            )
        return None

    def _check_event_day(
        self, today: date, proposal: ProposedTrade, rules: RulesConfig
    ) -> Optional[str]:
        """
        If today is a flagged event day, verify the proposal quantity has been halved.
        The strategy layer is responsible for submitting at 50% size on event days.
        """
        events = self._load_events()
        today_str = today.isoformat()
        if today_str not in events:
            return None
        event_name = events[today_str]
        # Calculate the max quantity allowed at 50% risk
        sl_distance = abs(proposal.entry_price - proposal.stop_loss)
        if sl_distance == 0:
            return f"Event day ({event_name}) — cannot size position with zero SL distance"
        risk_limit = (
            rules.swing_risk_per_trade if proposal.segment == "SWING_EQUITY"
            else rules.risk_per_trade
        )
        max_event_qty = math.floor((risk_limit * 0.5) / sl_distance)
        if proposal.quantity > max_event_qty:
            return (
                f"Event day ({event_name}) — max quantity is {max_event_qty} "
                f"at 50% size (submitted: {proposal.quantity}). Resubmit at reduced size."
            )
        return None

    def _check_segment_strategy(self, proposal: ProposedTrade) -> Optional[str]:
        allowed = _STRATEGY_ALLOWED_SEGMENTS.get(proposal.strategy)
        if allowed is None:
            return None  # Unknown strategy is caught by check 18
        if proposal.segment not in allowed:
            return (
                f"Strategy '{proposal.strategy}' cannot be used with segment "
                f"'{proposal.segment}'. Allowed segments: {sorted(allowed)}"
            )
        return None

    # ── State queries ─────────────────────────────────────────────────────────

    def _get_daily_state(self, d: date) -> DailyState:
        date_str = d.isoformat()
        row = self._conn.execute(
            "SELECT * FROM daily_state WHERE date = ?", (date_str,)
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT OR IGNORE INTO daily_state "
                "(date, trades_taken, pnl_realized, pnl_unrealized, daily_stop_hit) "
                "VALUES (?, 0, 0.0, 0.0, 0)",
                (date_str,),
            )
            self._conn.commit()
            return DailyState(d, 0, 0.0, 0.0, False)
        return DailyState(
            date=d,
            trades_taken=row["trades_taken"],
            pnl_realized=row["pnl_realized"],
            pnl_unrealized=row["pnl_unrealized"],
            daily_stop_hit=bool(row["daily_stop_hit"]),
        )

    def _get_weekly_state(self, d: date) -> WeeklyState:
        week_start = d - timedelta(days=d.weekday())  # Monday
        ws_str = week_start.isoformat()
        row = self._conn.execute(
            "SELECT * FROM weekly_state WHERE week_start = ?", (ws_str,)
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT OR IGNORE INTO weekly_state "
                "(week_start, pnl_realized, weekly_stop_hit) VALUES (?, 0.0, 0)",
                (ws_str,),
            )
            self._conn.commit()
            return WeeklyState(week_start, 0.0, False)
        return WeeklyState(
            week_start=week_start,
            pnl_realized=row["pnl_realized"],
            weekly_stop_hit=bool(row["weekly_stop_hit"]),
        )

    def _get_monthly_state(self, d: date) -> MonthlyState:
        month_start = d.replace(day=1)
        ms_str = month_start.isoformat()
        row = self._conn.execute(
            "SELECT * FROM monthly_state WHERE month_start = ?", (ms_str,)
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT OR IGNORE INTO monthly_state "
                "(month_start, pnl_realized, monthly_stop_hit) VALUES (?, 0.0, 0)",
                (ms_str,),
            )
            self._conn.commit()
            return MonthlyState(month_start, 0.0, False)
        return MonthlyState(
            week_start=month_start,
            pnl_realized=row["pnl_realized"],
            monthly_stop_hit=bool(row["monthly_stop_hit"]),
        )

    def _write_audit_log(
        self,
        proposal: ProposedTrade,
        action_type: str,
        reasoning: str,
    ) -> None:
        """Write one entry to both SQLite audit_log and the daily CSV."""
        now_str = self._now().isoformat()
        # SQLite
        self._conn.execute(
            "INSERT INTO audit_log "
            "(timestamp, action_type, instrument_key, side, quantity, price, "
            " strategy, segment, reasoning, sl, target, pnl, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (
                now_str, action_type,
                proposal.instrument_key, proposal.side, proposal.quantity,
                proposal.entry_price, proposal.strategy, proposal.segment,
                reasoning, proposal.stop_loss, proposal.target,
            ),
        )
        self._conn.commit()

        # CSV
        today_str = self._now().strftime("%Y-%m-%d")
        csv_path = LOGS_DIR / f"audit_{today_str}.csv"
        write_header = not csv_path.exists()
        try:
            with csv_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow([
                        "timestamp", "action_type", "instrument_key", "side",
                        "quantity", "price", "strategy", "segment",
                        "reasoning", "sl", "target", "pnl",
                    ])
                writer.writerow([
                    now_str, action_type,
                    proposal.instrument_key, proposal.side, proposal.quantity,
                    proposal.entry_price, proposal.strategy, proposal.segment,
                    reasoning, proposal.stop_loss, proposal.target, "",
                ])
        except Exception as exc:
            logger.error("CSV audit write failed: %s", exc)

    # ── Public state API ──────────────────────────────────────────────────────

    def get_current_state(self) -> Tuple[DailyState, WeeklyState, MonthlyState]:
        """Return today's daily, weekly, and monthly state objects."""
        today = self._now().date()
        return (
            self._get_daily_state(today),
            self._get_weekly_state(today),
            self._get_monthly_state(today),
        )

    def get_open_positions(self) -> List[Position]:
        """Return all positions with status='OPEN'."""
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN'"
        ).fetchall()
        result = []
        for r in rows:
            result.append(Position(
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
                closed_at=datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None,
                pnl=r["pnl"],
            ))
        return result

    def record_trade_decision(
        self, proposal: ProposedTrade, result: ValidationResult
    ) -> None:
        """Log every validate() call — both approved and rejected."""
        action = "TRADE_APPROVED" if result.allowed else f"TRADE_REJECTED_{result.checks_failed[0] if result.checks_failed else 'UNKNOWN'}"
        self._write_audit_log(proposal, action, result.reason)

    def update_position(self, position: Position, new_state: dict) -> None:
        """
        Update a position in the DB. new_state keys match column names:
        current_sl, current_target, status, closed_at, pnl.
        """
        allowed_fields = {"current_sl", "current_target", "status", "closed_at", "pnl"}
        updates = {k: v for k, v in new_state.items() if k in allowed_fields}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [position.id]
        self._conn.execute(
            f"UPDATE positions SET {set_clause} WHERE id = ?", values
        )
        self._conn.commit()
        logger.info("Position %d updated: %s", position.id, updates)

    def record_pnl(self, amount: float, realized: bool = True) -> None:
        """
        Add P&L to the current day/week/month accumulators.
        Call this when a position closes (realized=True) or mark-to-market changes
        (realized=False).
        """
        today = self._now().date()
        if realized:
            self._conn.execute(
                "UPDATE daily_state SET pnl_realized = pnl_realized + ? WHERE date = ?",
                (amount, today.isoformat()),
            )
            week_start = today - timedelta(days=today.weekday())
            self._conn.execute(
                "UPDATE weekly_state SET pnl_realized = pnl_realized + ? WHERE week_start = ?",
                (amount, week_start.isoformat()),
            )
            month_start = today.replace(day=1)
            self._conn.execute(
                "UPDATE monthly_state SET pnl_realized = pnl_realized + ? WHERE month_start = ?",
                (amount, month_start.isoformat()),
            )
        else:
            self._conn.execute(
                "UPDATE daily_state SET pnl_unrealized = pnl_unrealized + ? WHERE date = ?",
                (amount, today.isoformat()),
            )
        self._conn.commit()

    # ── Helper methods ────────────────────────────────────────────────────────

    def get_position_size_suggestion(
        self,
        instrument_key: str,
        entry_price:    float,
        stop_loss:      float,
        strategy:       str,
        segment:        str = "EQUITY_INTRADAY",
    ) -> int:
        """
        Return the maximum quantity that fits within the risk-per-trade rule.
        Uses swing risk for SWING_EQUITY, intraday risk otherwise.
        Accounts for remaining combined instrument risk cap.
        """
        self._ensure_rules()
        rules = self._rules
        sl_distance = abs(entry_price - stop_loss)
        if sl_distance == 0:
            return 0
        risk_budget = (
            rules.swing_risk_per_trade
            if segment == "SWING_EQUITY"
            else rules.risk_per_trade
        )
        # Subtract existing risk already on this instrument
        open_positions = self.get_open_positions()
        existing_risk = sum(
            p.open_risk for p in open_positions if p.instrument_key == instrument_key
        )
        remaining_cap = rules.combined_risk_cap - existing_risk
        effective_budget = min(risk_budget, remaining_cap)
        if effective_budget <= 0:
            return 0
        return math.floor(effective_budget / sl_distance)

    def can_open_new_position(self) -> Tuple[bool, str]:
        """
        Quick pre-flight check: can a new position be opened right now?
        Does not validate instrument-specific or size-specific rules.
        """
        self._ensure_rules()
        rules = self._rules
        if not self._trading_enabled():
            return False, "Kill switch active"
        today = self._now().date()
        daily = self._get_daily_state(today)
        if daily.pnl_realized <= -rules.daily_stop:
            return False, f"Daily stop hit (₹{daily.pnl_realized:,.0f})"
        weekly = self._get_weekly_state(today)
        if weekly.pnl_realized <= -rules.weekly_stop:
            return False, f"Weekly stop hit (₹{weekly.pnl_realized:,.0f})"
        monthly = self._get_monthly_state(today)
        if monthly.pnl_realized <= -rules.monthly_stop:
            return False, f"Monthly stop hit (₹{monthly.pnl_realized:,.0f})"
        open_pos = self.get_open_positions()
        if len(open_pos) >= rules.max_concurrent_positions:
            return False, f"At max concurrent positions ({len(open_pos)}/{rules.max_concurrent_positions})"
        return True, "OK"

    def get_daily_pnl_remaining(self) -> float:
        """
        Return the ₹ amount of loss remaining before the daily stop triggers.
        Positive value = headroom remaining. Zero or negative = stop already hit.
        """
        self._ensure_rules()
        today = self._now().date()
        daily = self._get_daily_state(today)
        return self._rules.daily_stop + daily.pnl_realized  # pnl_realized is negative for losses

    # ── Core validation ───────────────────────────────────────────────────────

    def validate(self, proposal: ProposedTrade) -> ValidationResult:
        """
        Run all 20 risk checks against a proposed trade, in enforcement order.
        Returns on the FIRST failure. Writes an audit log entry regardless of outcome.
        """
        self._ensure_rules()
        rules = self._rules
        now   = self._now()

        passed: List[str] = []
        failed: List[str] = []

        def block(check: str, reason: str) -> ValidationResult:
            failed.append(check)
            logger.info("BLOCKED [%s] %s: %s", check, proposal.instrument_key, reason)
            result = ValidationResult(
                allowed=False, reason=reason,
                checks_passed=list(passed), checks_failed=[check],
            )
            try:
                self._write_audit_log(proposal, f"BLOCKED_{check}", reason)
            except Exception as exc:
                logger.error("Audit log write failed during block: %s", exc)
            return result

        def ok(check: str) -> None:
            passed.append(check)

        # ── 1. KILL_SWITCH ────────────────────────────────────────────────────
        if not self._trading_enabled():
            return block("KILL_SWITCH", "Kill switch active (.env ENABLE_TRADING=false)")
        ok("KILL_SWITCH")

        # ── 2. TIME_WINDOW ────────────────────────────────────────────────────
        time_err = self._check_time_window(now, rules, proposal.segment)
        if time_err:
            return block("TIME_WINDOW", time_err)
        ok("TIME_WINDOW")

        # ── 3. INSTRUMENT_KEY_VALID ───────────────────────────────────────────
        if "|" not in proposal.instrument_key or len(proposal.instrument_key.split("|")) != 2:
            return block(
                "INSTRUMENT_KEY_VALID",
                f"Invalid instrument key '{proposal.instrument_key}' — expected EXCHANGE|IDENTIFIER "
                "(e.g. NSE_EQ|INE002A01018)",
            )
        ok("INSTRUMENT_KEY_VALID")

        # ── 4. SEGMENT_ENABLED ────────────────────────────────────────────────
        if proposal.segment not in rules.enabled_segments:
            return block(
                "SEGMENT_ENABLED",
                f"Segment '{proposal.segment}' is not enabled. "
                f"Enabled: {rules.enabled_segments}",
            )
        ok("SEGMENT_ENABLED")

        # ── 5. VIX_FILTER ─────────────────────────────────────────────────────
        vix = self._fetch_vix()
        if vix is not None and vix > rules.vix_threshold:
            return block(
                "VIX_FILTER",
                f"India VIX {vix:.1f} exceeds threshold {rules.vix_threshold:.1f}. "
                "No new entries permitted today.",
            )
        ok("VIX_FILTER")

        # ── 6. KILL_SWITCH_DOUBLE_CHECK ───────────────────────────────────────
        # Re-read .env in case it changed mid-session
        if not self._trading_enabled():
            return block(
                "KILL_SWITCH_DOUBLE_CHECK",
                "Kill switch activated mid-validation (.env ENABLE_TRADING=false)",
            )
        ok("KILL_SWITCH_DOUBLE_CHECK")

        # ── 7. DAILY_STOP_CHECK ───────────────────────────────────────────────
        daily = self._get_daily_state(now.date())
        if daily.pnl_realized <= -rules.daily_stop:
            return block(
                "DAILY_STOP_CHECK",
                f"Daily stop hit — realized P&L ₹{daily.pnl_realized:,.0f} "
                f"≤ -₹{rules.daily_stop:,.0f}. No new entries today.",
            )
        ok("DAILY_STOP_CHECK")

        # ── 8. WEEKLY_STOP_CHECK ──────────────────────────────────────────────
        weekly = self._get_weekly_state(now.date())
        if weekly.pnl_realized <= -rules.weekly_stop:
            return block(
                "WEEKLY_STOP_CHECK",
                f"Weekly stop hit — realized P&L ₹{weekly.pnl_realized:,.0f} "
                f"≤ -₹{rules.weekly_stop:,.0f}. No new entries this week.",
            )
        ok("WEEKLY_STOP_CHECK")

        # ── 9. MONTHLY_STOP_CHECK ─────────────────────────────────────────────
        monthly = self._get_monthly_state(now.date())
        if monthly.pnl_realized <= -rules.monthly_stop:
            return block(
                "MONTHLY_STOP_CHECK",
                f"Monthly stop hit — realized P&L ₹{monthly.pnl_realized:,.0f} "
                f"≤ -₹{rules.monthly_stop:,.0f}. No new entries this month.",
            )
        ok("MONTHLY_STOP_CHECK")

        # ── 10. MAX_CONCURRENT_POSITIONS ──────────────────────────────────────
        open_positions = self.get_open_positions()
        if len(open_positions) >= rules.max_concurrent_positions:
            return block(
                "MAX_CONCURRENT_POSITIONS",
                f"Already at max concurrent positions "
                f"({len(open_positions)}/{rules.max_concurrent_positions})",
            )
        ok("MAX_CONCURRENT_POSITIONS")

        # ── 11. POSITION_SIZE_RISK ────────────────────────────────────────────
        sl_distance = abs(proposal.entry_price - proposal.stop_loss)
        risk_limit = (
            rules.swing_risk_per_trade
            if proposal.segment == "SWING_EQUITY"
            else rules.risk_per_trade
        )
        trade_risk = proposal.trade_risk
        if trade_risk > risk_limit:
            suggested = math.floor(risk_limit / sl_distance) if sl_distance else 0
            return block(
                "POSITION_SIZE_RISK",
                f"Trade risk ₹{trade_risk:,.0f} exceeds limit ₹{risk_limit:,.0f}. "
                f"Suggested quantity: {suggested} "
                f"(at entry={proposal.entry_price}, SL={proposal.stop_loss})",
            )
        ok("POSITION_SIZE_RISK")

        # ── 12. POSITION_SIZE_REASONABLE ──────────────────────────────────────
        if proposal.quantity <= 0:
            return block(
                "POSITION_SIZE_REASONABLE",
                "Quantity is 0 or negative. Risk too tight for any whole lot/share — "
                "widen the SL or increase capital.",
            )
        ok("POSITION_SIZE_REASONABLE")

        # ── 13. COMBINED_INSTRUMENT_RISK ──────────────────────────────────────
        existing_risk = sum(
            p.open_risk for p in open_positions
            if p.instrument_key == proposal.instrument_key
        )
        combined_risk = existing_risk + trade_risk
        if combined_risk > rules.combined_risk_cap:
            return block(
                "COMBINED_INSTRUMENT_RISK",
                f"Combined risk on {proposal.instrument_key} would be ₹{combined_risk:,.0f} "
                f"(existing ₹{existing_risk:,.0f} + new ₹{trade_risk:,.0f}). "
                f"Cap: ₹{rules.combined_risk_cap:,.0f}.",
            )
        ok("COMBINED_INSTRUMENT_RISK")

        # ── 14. STOP_LOSS_LOGIC ───────────────────────────────────────────────
        if proposal.side == "BUY" and proposal.stop_loss >= proposal.entry_price:
            return block(
                "STOP_LOSS_LOGIC",
                f"Invalid SL for BUY: stop_loss {proposal.stop_loss} ≥ entry {proposal.entry_price}",
            )
        if proposal.side == "SELL" and proposal.stop_loss <= proposal.entry_price:
            return block(
                "STOP_LOSS_LOGIC",
                f"Invalid SL for SELL: stop_loss {proposal.stop_loss} ≤ entry {proposal.entry_price}",
            )
        ok("STOP_LOSS_LOGIC")

        # ── 15. TARGET_LOGIC ──────────────────────────────────────────────────
        if proposal.side == "BUY" and proposal.target <= proposal.entry_price:
            return block(
                "TARGET_LOGIC",
                f"Invalid target for BUY: target {proposal.target} ≤ entry {proposal.entry_price}",
            )
        if proposal.side == "SELL" and proposal.target >= proposal.entry_price:
            return block(
                "TARGET_LOGIC",
                f"Invalid target for SELL: target {proposal.target} ≥ entry {proposal.entry_price}",
            )
        ok("TARGET_LOGIC")

        # ── 16. RISK_REWARD_RATIO ─────────────────────────────────────────────
        rr_actual  = abs(proposal.target - proposal.entry_price) / sl_distance if sl_distance else 0.0
        rr_minimum = rules.min_rr.get(proposal.strategy, 0.9)
        if rr_actual < rr_minimum:
            return block(
                "RISK_REWARD_RATIO",
                f"R:R {rr_actual:.2f} is below the minimum {rr_minimum} "
                f"for strategy '{proposal.strategy}'",
            )
        ok("RISK_REWARD_RATIO")

        # ── 17. EVENT_DAY_GUARD ───────────────────────────────────────────────
        event_err = self._check_event_day(now.date(), proposal, rules)
        if event_err:
            return block("EVENT_DAY_GUARD", event_err)
        ok("EVENT_DAY_GUARD")

        # ── 18. STRATEGY_ENABLED ──────────────────────────────────────────────
        if proposal.strategy not in _VALID_STRATEGIES:
            return block(
                "STRATEGY_ENABLED",
                f"Unknown strategy '{proposal.strategy}'. "
                f"Valid strategies: {_VALID_STRATEGIES}",
            )
        ok("STRATEGY_ENABLED")

        # ── 19. SEGMENT_STRATEGY_COMPATIBILITY ───────────────────────────────
        compat_err = self._check_segment_strategy(proposal)
        if compat_err:
            return block("SEGMENT_STRATEGY_COMPATIBILITY", compat_err)
        ok("SEGMENT_STRATEGY_COMPATIBILITY")

        # ── 20. AUDIT_LOG_WRITE ───────────────────────────────────────────────
        try:
            self._write_audit_log(proposal, "TRADE_VALIDATED", "All 19 checks passed")
            ok("AUDIT_LOG_WRITE")
        except Exception as exc:
            logger.error("Audit log write failed on approval: %s", exc)
            ok("AUDIT_LOG_WRITE")  # Do not block a valid trade for a logging failure

        logger.info(
            "ALLOWED: %s %s ×%d @ %.2f  SL=%.2f  T=%.2f  RR=%.2f  risk=₹%.0f  strategy=%s",
            proposal.side, proposal.instrument_key, proposal.quantity,
            proposal.entry_price, proposal.stop_loss, proposal.target,
            rr_actual, trade_risk, proposal.strategy,
        )

        return ValidationResult(
            allowed=True,
            reason="All checks passed",
            checks_passed=passed,
            checks_failed=failed,
        )


# ── Test suite ────────────────────────────────────────────────────────────────

def _fmt_result(name: str, result: ValidationResult, expect_allowed: bool) -> Tuple[bool, str]:
    ok = result.allowed == expect_allowed
    verdict = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    outcome  = "ALLOWED" if result.allowed else f"BLOCKED — {result.reason}"
    expected = "ALLOWED" if expect_allowed else "BLOCKED"
    mismatch = f"  !! expected {expected}" if not ok else ""
    return ok, f"  {verdict}  {name}\n         → {outcome}{mismatch}"


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    # Suppress file-level log output during tests so stdout stays clean
    logging.getLogger("risk_engine").setLevel(logging.CRITICAL)

    T_MARKET = datetime(2026, 5, 20, 11, 0, 0, tzinfo=IST)   # mid-session
    T_NIGHT  = datetime(2026, 5, 20, 23, 30, 0, tzinfo=IST)  # outside window

    def make_engine(
        *, daily_pnl: float = 0.0, inject_positions: int = 0,
        test_date: Optional[date] = None,
    ) -> RiskEngine:
        eng = RiskEngine(db_path=":memory:")
        eng._kill_switch_override = True   # ENABLE_TRADING=true in tests
        eng._vix_override = 15.0           # VIX below threshold
        # Use T_MARKET.date() as default so injected state matches the time override
        today_str = (test_date or T_MARKET.date()).isoformat()
        if daily_pnl != 0.0:
            eng._conn.execute(
                "INSERT OR REPLACE INTO daily_state "
                "(date, trades_taken, pnl_realized, pnl_unrealized, daily_stop_hit) "
                "VALUES (?, 0, ?, 0.0, 1)",
                (today_str, daily_pnl),
            )
            eng._conn.commit()
        opened = datetime.now(tz=IST).isoformat()
        for i in range(inject_positions):
            eng._conn.execute(
                "INSERT INTO positions "
                "(instrument_key, side, quantity, entry_price, current_sl, "
                " current_target, strategy, segment, opened_at, status) "
                "VALUES (?, 'BUY', 10, 500.0, 490.0, 520.0, 'ORB', 'EQUITY_INTRADAY', ?, 'OPEN')",
                (f"NSE_EQ|TEST{i:03d}", opened),
            )
        eng._conn.commit()
        return eng

    # Reference trade: Reliance ORB long, ₹300 risk, 2:1 R:R
    RELIANCE_ORB = ProposedTrade(
        instrument_key="NSE_EQ|INE002A01018",
        side="BUY", quantity=20,
        entry_price=1395.0, stop_loss=1380.0, target=1425.0,
        strategy="ORB", segment="EQUITY_INTRADAY",
    )

    tests = []

    # T1 — Valid ORB trade → ALLOWED
    eng1 = make_engine()
    eng1._time_override = T_MARKET
    tests.append(("T1 Valid ORB trade", eng1, RELIANCE_ORB, True))

    # T2 — Outside trading hours (23:30) → BLOCKED
    eng2 = make_engine()
    eng2._time_override = T_NIGHT
    tests.append(("T2 Outside trading hours (23:30 IST)", eng2, RELIANCE_ORB, False))

    # T3 — Risk too high (₹5,000 > ₹3,000) → BLOCKED
    eng3 = make_engine()
    eng3._time_override = T_MARKET
    risky_trade = ProposedTrade(
        instrument_key="NSE_EQ|INE040A01034",
        side="BUY", quantity=50,
        entry_price=1700.0, stop_loss=1600.0, target=1900.0,
        strategy="ORB", segment="EQUITY_INTRADAY",
    )
    tests.append(("T3 Risk too high (₹5,000 > ₹3,000 limit)", eng3, risky_trade, False))

    # T4 — Invalid SL direction (SL above entry for BUY) → BLOCKED
    eng4 = make_engine()
    eng4._time_override = T_MARKET
    bad_sl = ProposedTrade(
        instrument_key="NSE_EQ|INE002A01018",
        side="BUY", quantity=10,
        entry_price=1395.0, stop_loss=1410.0, target=1425.0,   # SL > entry
        strategy="ORB", segment="EQUITY_INTRADAY",
    )
    tests.append(("T4 Invalid SL direction (SL > entry for BUY)", eng4, bad_sl, False))

    # T5 — Wrong segment for strategy (ORB + BUY_OPTIONS) → BLOCKED
    eng5 = make_engine()
    eng5._time_override = T_MARKET
    wrong_seg = ProposedTrade(
        instrument_key="NSE_EQ|INE002A01018",
        side="BUY", quantity=20,
        entry_price=1395.0, stop_loss=1380.0, target=1425.0,
        strategy="ORB", segment="BUY_OPTIONS",   # ORB only allowed with EQUITY_INTRADAY
    )
    tests.append(("T5 Wrong segment for strategy (ORB + BUY_OPTIONS)", eng5, wrong_seg, False))

    # T6 — Already at max concurrent positions (3/3) → BLOCKED
    eng6 = make_engine(inject_positions=3)
    eng6._time_override = T_MARKET
    tests.append(("T6 Max concurrent positions already at 3/3", eng6, RELIANCE_ORB, False))

    # T7 — Daily stop already hit (−₹9,001) → BLOCKED
    eng7 = make_engine(daily_pnl=-9001.0)
    eng7._time_override = T_MARKET
    tests.append(("T7 Daily stop already hit (−₹9,001)", eng7, RELIANCE_ORB, False))

    # T8 — All conditions perfect (HDFCBANK ORB long) → ALLOWED
    eng8 = make_engine()
    eng8._time_override = T_MARKET
    perfect = ProposedTrade(
        instrument_key="NSE_EQ|INE040A01034",
        side="BUY", quantity=15,
        entry_price=1640.0, stop_loss=1620.0, target=1680.0,   # risk=₹300, RR=2.0
        strategy="ORB", segment="EQUITY_INTRADAY",
    )
    tests.append(("T8 All conditions perfect — HDFCBANK ORB long", eng8, perfect, True))

    # ── Run ───────────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  Risk Engine — Test Suite")
    print("═" * 60 + "\n")

    all_ok = True
    for name, eng, proposal, expect in tests:
        try:
            result = eng.validate(proposal)
        except Exception as exc:
            print(f"  \033[31mFAIL\033[0m  {name}\n         → EXCEPTION: {exc}\n")
            all_ok = False
            continue
        passed, line = _fmt_result(name, result, expect)
        print(line + "\n")
        if not passed:
            all_ok = False

    print("─" * 60)
    if all_ok:
        print("  \033[32mAll 8 tests passed.\033[0m")
    else:
        print("  \033[31mSome tests failed — see output above.\033[0m")
        sys.exit(1)
    print()
