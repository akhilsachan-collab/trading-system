# Trading Rules

**Version:** 1.0  
**Last Updated:** 2026-05-20  
**Status:** Active — all automated scripts must load and enforce this file before placing any order.

---

## Philosophy

This file is the single source of truth for every trading decision this system makes. It exists because good trading is not about skill in the moment — it is about having made the hard decisions in advance, when you were calm, and then mechanically enforcing them when you are not. Rules override impulses. Parameters override gut feeling. The system does not care if "this one feels different."

Every numeric value here was chosen deliberately. Risk limits exist not to constrain profit, but to prevent the kind of catastrophic drawdown that ends trading careers. Time windows exist because most intraday edge decays rapidly after the opening hour. Filters exist because trading into a storm — high VIX, binary events, gap instability — is not edge, it is noise. This is not a system for maximum exposure; it is a system for durable edge capture over hundreds of trades.

The hierarchy of rules is strict: **Safety > Risk > Filters > Strategy > Sizing**. A valid strategy signal is worthless if the daily stop has been hit. A good entry setup is irrelevant if VIX is above the threshold. The system enforces each layer in order, and a failure at any layer aborts the trade entirely. No exceptions in code; no exceptions in practice.

This document will be updated as backtesting in later phases reveals which parameters should be tightened or relaxed. Until explicitly versioned and committed, no parameter is considered changed. Informal notes belong in Section 11; production changes belong in Section 10.

---

## 1. Capital and Risk Limits

All monetary values are in Indian Rupees (₹). `RISK_CAPITAL` is a configurable placeholder — adjust it in `.env` as account size changes, but never trade above your stated capital without updating this value first.

```
RISK_CAPITAL          = ₹1,50,000    # Midpoint of ₹1L–₹2.5L target range; adjustable via .env

RISK_PER_TRADE        = 2.0%         # ₹3,000 at current capital
SWING_RISK_PER_TRADE  = 1.5%         # ₹2,250 — tighter due to overnight gap exposure

DAILY_STOP            = 6.0%         # ₹9,000  — halts ALL new entries for remainder of session
WEEKLY_STOP           = ₹22,500      # ~2.5× daily — halts ALL new entries for remainder of calendar week
MONTHLY_STOP          = ₹36,000      # ~4× daily  — halts ALL new entries for remainder of calendar month
```

**Enforcement logic:** Loss tracking is cumulative from midnight IST for the daily stop, from Monday open for the weekly stop, and from the 1st of the month for the monthly stop. Stops are checked before every order placement. Once a stop triggers, the flag persists in state until the next valid reset window opens — it is not reset by a subsequent profitable trade within the same period.

**Stop hierarchy:** If the monthly stop is active, it overrides the weekly and daily checks. If the weekly stop is active, it overrides the daily check. All three are independent accumulators.

| Limit | Value | % of Capital | Resets |
|---|---|---|---|
| Per-trade risk (intraday) | ₹3,000 | 2.0% | N/A |
| Per-trade risk (swing) | ₹2,250 | 1.5% | N/A |
| Per-instrument combined risk | ₹4,500 | 3.0% | N/A |
| Daily stop | ₹9,000 | 6.0% | Next trading day |
| Weekly stop | ₹22,500 | 15.0% | Next Monday open |
| Monthly stop | ₹36,000 | 24.0% | 1st of next month |

---

## 2. Time Windows

All times are **IST (UTC+5:30)**. The system must enforce these windows regardless of strategy signal quality.

```
INTRADAY_ENTRY_START   = 09:45    # No entries before this; ORB range builds 09:15–09:45
INTRADAY_ENTRY_LATEST  = 15:00    # No NEW position entries at or after this time
INTRADAY_FORCE_CLOSE   = 15:15    # All open intraday (MIS) positions must be flat by this time
```

**Pre-market (09:15–09:44):** Data collection only. Build ORB range, compute indicators. No orders placed.

**Entry window (09:45–14:59):** All three strategies may generate and execute entry signals.

**Late session (15:00–15:14):** No new entries. Manage existing positions only (trail stops, partial exits).

**Force-close (15:15):** Any MIS position still open is closed at market price. This is non-negotiable — holding MIS overnight creates margin calls. The system places market-sell/cover orders automatically at 15:14:55 as a safety margin.

**Swing positions (CNC):** Not subject to intraday time windows. GTT stop orders handle multi-day exits.

---

## 3. Market Condition Filters

These filters are evaluated at session start and re-evaluated before each new entry signal. They do not affect management of existing positions.

### 3.1 VIX Hard Filter

```
VIX_THRESHOLD         = 22.0
```

When India VIX > 22.0 at the time of entry evaluation: **skip all new entries for the session.** Log reason as `VIX_FILTER_TRIGGERED`. Resume normal operation the next trading session if VIX has normalized.

*Rationale: Above VIX 22, options premiums inflate, intraday ranges become erratic, and mean-reversion assumptions break down. Edge degrades faster than risk rises.*

### 3.2 VIX + Gap Soft Guard

```
GAP_SOFT_VIX          = 18.0     # Softer VIX threshold for gap overlap check
GAP_SOFT_THRESHOLD    = 1.0%     # Nifty 50 open vs previous close
```

If Nifty 50 opens with a gap exceeding ±1.0% **AND** VIX > 18.0: **pause all new entries for the full session.** This is distinct from the hard VIX filter — VIX can be below 22 but the combination of gap instability and elevated volatility still creates unfavorable conditions.

### 3.3 Event Day Soft Guard

```
EVENT_SIZE_MULTIPLIER = 0.5      # Position size halved on flagged event days
```

On flagged macro event days — **Union Budget, General/State Elections result day, RBI Monetary Policy announcement, US FOMC decision** — all new position sizes are automatically halved (round down). The system does not skip trading entirely on event days; it reduces exposure.

**Flagged event days must be manually maintained** in `data/event_calendar.json` (to be created in a future phase). Until that file exists, this guard is inert — scripts should log a warning at startup.

### 3.4 Disabled Filters

```
GAP_FILTER  = NONE    # No blanket gap filter beyond the VIX+gap overlap guard
EVENT_FILTER = SOFT   # See Section 3.3; hard skip not applied on event days
```

---

## 4. Position Sizing

### 4.1 Core Formula

```
quantity = floor(RISK_PER_TRADE / abs(entry_price - stop_loss_price))
```

- Always round **down** to the nearest whole share or lot.
- There is no minimum quantity floor — if the formula yields 0, the trade is skipped and logged as `SIZING_ZERO_SKIP`.
- There is no nominal position-size cap (e.g., no "max ₹X per trade" ceiling beyond what the risk formula naturally produces).
- No cool-down sizing: after a losing streak, risk stays at the full 2% (or 1.5% for swing). The math does not penalise consecutive losses.

### 4.2 Per-Instrument Combined Risk Cap

```
MAX_RISK_PER_INSTRUMENT = 3.0%    # ₹4,500 at current capital
```

Before placing a new order: sum the open risk (entry-to-SL × quantity) across ALL segments for the same instrument. If adding the new trade would bring combined instrument risk above ₹4,500, **block the entry**. Log as `INSTRUMENT_RISK_CAP_BLOCKED`.

*This applies across segments — e.g., a CNC swing position in RELIANCE and a new MIS intraday position in RELIANCE share the combined cap.*

### 4.3 Event Day Adjustment

When the event-day soft guard is active (Section 3.3), apply the 0.5× multiplier **after** the risk formula:

```
quantity = floor((RISK_PER_TRADE * EVENT_SIZE_MULTIPLIER) / abs(entry - sl))
```

---

## 5. Segments and Concurrency

### 5.1 Enabled Segments

| Segment | Exchange | Product Code | Notes |
|---|---|---|---|
| Equity Intraday | NSE | MIS | Mandatory force-close at 15:15 |
| Buy Options | NFO | NRML / MIS | Buying calls/puts only — no selling/writing |
| Swing Equity | NSE | CNC | Delivery; GTT orders for SL |

### 5.2 Disabled Segments

```
SELL_OPTIONS   = DISABLED    # No short options / writing positions
FUTURES        = DISABLED    # No equity or index futures
MCX_COMMODITIES = DISABLED   # MCX was removed from active trading; watchlist retained for reference only
```

*MCX commodity data is still ingested for informational purposes (see watchlist.json), but the system will not place MCX orders.*

### 5.3 Concurrency Limits

```
MAX_CONCURRENT_POSITIONS  = 3    # Across ALL segments combined
MAX_CONCURRENT_SWING      = 2    # Swing-specific sublimit (counts toward the 3 total)
```

Before placing any new entry order, count all open positions (MIS + CNC + options). If `open_positions >= 3`, block the entry. Log as `CONCURRENCY_LIMIT_BLOCKED`.

**Same-instrument overlap:** Allowed. A swing CNC position and an intraday MIS position in the same stock count as 2 separate positions against the concurrency limit, and their risks combine for the per-instrument cap check.

---

## 6. Strategies

### Strategy Quick Reference

| Parameter | ORB | Momentum | Mean Reversion |
|---|---|---|---|
| Signal | Price breaks 30-min range + 0.1% | Price breaks 20-day high/low | RSI(14) < 30 or > 70 |
| Volume filter | > 1.5× 20-day avg | > 1.5× 20-day avg | > 1.5× 20-day avg |
| Universe | 10 stocks | 10 stocks + Nifty 50 + Nifty Bank | 10 stocks + Nifty 50 + Nifty Bank |
| Direction | Long only (v1) | Long only (v1) | Both |
| Risk:Reward | 1:2 | 1:2.5 | 1:1 |
| Time stop | 60 min | 90 min | 30 min |
| Trailing stop | Yes (at break-even) | Yes (at initial target) | No |
| Limit order timeout | 5 min | 5 min | 5 min |

---

### 6.1 Opening Range Breakout (ORB)

**Concept:** The first 30 minutes of the session establish a range. A breakout beyond that range, confirmed by volume, signals directional momentum for the day.

#### Range Definition

```
ORB_RANGE_START   = 09:15    # Market open
ORB_RANGE_END     = 09:45    # End of range-building period
```

The ORB high is the highest traded price, and the ORB low is the lowest traded price, in the 09:15–09:44 window (inclusive).

#### Entry Conditions

```python
# Long entry (all conditions must be true):
price > orb_high * 1.001            # 0.1% breakout above range high
volume_now > avg_volume_20d * 1.5   # Volume confirmation
time >= 09:45
time < 15:00                        # Within entry window
direction == "LONG"                 # Long only in v1
```

#### Universe

```
ORB_UNIVERSE = [
    "RELIANCE",      # NSE_EQ|INE002A01018
    "HDFCBANK",      # NSE_EQ|INE040A01034
    "ICICIBANK",     # NSE_EQ|INE090A01021
    "SBIN",          # NSE_EQ|INE062A01020
    "TCS",           # NSE_EQ|INE467B01029
    "INFY",          # NSE_EQ|INE009A01021
    "BHARTIARTL",    # NSE_EQ|INE397D01024
    "LT",            # NSE_EQ|INE018A01030
    "ITC",           # NSE_EQ|INE154A01025
    "MARUTI",        # NSE_EQ|INE585B01010
]
# No indices; no commodities — volume filter requires liquid single-name stocks
```

#### Stop Loss

```
sl = min(
    entry - 1.5 * ATR(14),           # ATR-based stop
    orb_low                           # Opposite end of opening range
)
# Take the tighter (higher) of the two
```

#### Target and Trailing

```
target      = entry + 2.0 * (entry - sl)    # 2:1 reward:risk

# Trailing stop activates at break-even (i.e., when price >= target of 1:1):
trail_price = current_price * 0.99           # 1% trail below current price
```

#### Time and Order Management

```
TIME_STOP_MINUTES       = 60    # Close position 60 min after entry if neither target nor SL hit
LIMIT_ORDER_TIMEOUT_MIN = 5     # Cancel unfilled limit entry order after 5 minutes
```

---

### 6.2 Momentum Breakout

**Concept:** A break of the 20-day high (or low) with volume confirmation signals continuation momentum. Index signals route to options; stock signals route to equity intraday.

#### Entry Conditions

```python
# Long entry (all conditions must be true):
price > rolling_high_20d                     # 20-day high breakout
volume_now > avg_volume_20d * 1.5            # Volume confirmation
nifty50_close > nifty50_sma_20d              # Broader market long filter
nifty50_today_close > nifty50_yesterday_close  # Nifty closed up vs prior day
direction == "LONG"                          # Long only in v1 (shorts disabled)
```

*Shorts are disabled in v1. Enable in v2 only after backtesting confirms short-side edge.*

#### Universe and Routing

```
MOMENTUM_UNIVERSE = [
    # Stocks → Equity Intraday (MIS)
    "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "TCS",
    "INFY", "BHARTIARTL", "LT", "ITC", "MARUTI",

    # Indices → Options (NFO) — long signal = buy call, short signal = buy put
    "NIFTY 50",    # NSE_INDEX|Nifty 50
    "NIFTY BANK",  # NSE_INDEX|Nifty Bank
]
```

#### Stop Loss

```
sl = min(
    entry - 1.5 * ATR(14),                         # ATR-based stop
    recent_swing_low_within_20_bars                # Recent structural low
)
# Take the tighter (higher) for longs
```

#### Target and Trailing

```
target = entry + 2.5 * (entry - sl)    # 2.5:1 reward:risk

# Trailing stop activates ONLY after initial 2.5:1 target is hit (let winners run):
trail_price = current_price * 0.99     # 1% trail below current price; no fixed upper exit
```

#### Time and Order Management

```
TIME_STOP_MINUTES       = 90
LIMIT_ORDER_TIMEOUT_MIN = 5
```

---

### 6.3 Mean Reversion

**Concept:** Oversold/overbought conditions, confirmed by Bollinger Band extremes and low ADX (non-trending market), produce short-term snap-backs. Fast time stop because mean reversion either works quickly or it doesn't.

#### Entry Conditions

```python
# Long entry (all conditions must be true):
rsi_14 < 30                                    # RSI oversold
price < bollinger_lower_band(20, 2)            # Price below lower BB
volume_now > avg_volume_20d * 1.5             # Volume confirmation
adx_14 < 25                                   # NOT in a strong trend
consecutive_same_direction_candles_daily < 3  # No 3-candle streak filter

# Short entry (mirror conditions):
rsi_14 > 70                                    # RSI overbought
price > bollinger_upper_band(20, 2)            # Price above upper BB
volume_now > avg_volume_20d * 1.5
adx_14 < 25
consecutive_same_direction_candles_daily < 3
```

#### Universe

```
MR_UNIVERSE = [
    # Same 10 stocks as ORB
    "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "TCS",
    "INFY", "BHARTIARTL", "LT", "ITC", "MARUTI",
    # Plus indices
    "NIFTY 50",
    "NIFTY BANK",
]
```

#### Stop Loss

```
sl = min(
    entry - 1.5 * ATR(14),                  # ATR-based stop (for longs)
    last_significant_low_on_chart            # Structural support
)
```

#### Target and Trailing

```
target = entry + 1.0 * (entry - sl)    # 1:1 reward:risk — mean reversion = frequent small wins

# NO trailing stop — exit at fixed target only
```

#### Time and Order Management

```
TIME_STOP_MINUTES       = 30    # Short window; if reversion hasn't started in 30 min, it's failing
LIMIT_ORDER_TIMEOUT_MIN = 5
```

---

## 7. Exits

This section consolidates exit methodology across all strategies. The tightest applicable exit rule always takes precedence.

### 7.1 Stop Loss Exits

All positions carry a hard stop loss calculated at entry time. Stop losses are:
- Set as exchange-side bracket/GTT orders immediately after fill confirmation, never held only in software.
- For intraday MIS: bracket order SL leg or equivalent real-time monitoring.
- For swing CNC: GTT (Good-Till-Triggered) order on NSE.

Stop loss is **never moved further from entry** (widened). It may only be moved in the direction of the trade (tightened / trailed).

### 7.2 Target Exits

Target orders are limit orders placed at entry time alongside the SL. On fill, the SL leg is cancelled.

| Strategy | Target R:R |
|---|---|
| ORB | 2:1 |
| Momentum | 2.5:1 |
| Mean Reversion | 1:1 |

### 7.3 Trailing Stops

Trailing stop logic is strategy-specific (see Section 6). Common rules:
- Trailing is activated by a price event (reaching break-even for ORB; reaching initial target for Momentum).
- Trail is calculated as `current_price × 0.99` — a 1% fixed-percentage trail.
- Trail only moves in the winning direction; it never retreats.
- Mean Reversion has **no trailing stop** — it exits at fixed target.

### 7.4 Time Stops

| Strategy | Time Stop |
|---|---|
| ORB | 60 min from entry fill |
| Momentum | 90 min from entry fill |
| Mean Reversion | 30 min from entry fill |

On time stop trigger: close position at market price. Log action as `TIME_STOP_EXIT`.

### 7.5 Stale Signal Exit

When a pending limit order fills, **re-validate all entry conditions** for that strategy at the moment of fill. If conditions no longer hold (e.g., price has already retreated below the breakout level, VIX has spiked, volume has collapsed):

1. Immediately close position at market price.
2. Log action as `STALE_SIGNAL_EXIT` with full re-validation state.

*This protects against partial fills that arrive late, or fills that occur on a delayed order queue.*

### 7.6 Intraday Force Close

At **15:14:55 IST**, the system scans all open MIS positions. Any remaining open position is closed at market. This is a safety net for all other exit mechanisms. Log as `FORCE_CLOSE_15_15`.

### 7.7 Limit Order Timeout

All entry limit orders that remain **unfilled for 5 minutes** are cancelled. Log as `ENTRY_ORDER_TIMEOUT`. The signal is considered void — no re-entry is attempted for the same signal instance.

---

## 8. Swing-Specific Rules

Swing trades (CNC delivery) carry overnight gap risk. These rules supplement all general rules and override them where they conflict.

```
SWING_RISK_PER_TRADE          = 1.5%    # ₹2,250 — tighter than intraday 2%
MAX_CONCURRENT_SWING           = 2      # Sublimit within the global 3-position cap
NO_SWING_ENTRY_DAY             = Friday # No new swing entries on Fridays
SWING_MAX_HOLDING_DAYS         = 30     # Close position at market on day 30 if still open
SWING_SL_ORDER_TYPE            = GTT    # Stop-loss must be a GTT order for multi-day persistence
```

**Friday rule:** No new CNC swing entries on Fridays. The weekend gap risk is asymmetric — the market can open significantly higher or lower on Monday, bypassing the stop. Existing swing positions held over a weekend are fine; new ones are not initiated.

**GTT orders:** Every swing position must have a live GTT order in place on the exchange for the stop loss price. The system verifies GTT existence after order fill. If GTT placement fails, the position is flagged for manual review and no additional swing entries are permitted until resolved.

**30-day holding cap:** If a swing position is still open on day 30 (calendar days from entry), it is closed at market on the next trading session open, regardless of P&L. Log as `SWING_MAX_HOLDING_EXIT`.

---

## 9. Compliance and Emergency Procedures

### 9.1 SEBI Algo Tagging

```
SEBI_ALGO_TAG = ENABLED
```

All orders placed via the Upstox API must include the `tag: "algo"` field per SEBI's 2024 algorithmic trading regulation. The system enforces this at the order-placement layer — no order is submitted without this tag.

### 9.2 Kill Switch

```
ENABLE_TRADING = true    # Set in .env file
```

When `ENABLE_TRADING=false` in `.env`, the system performs zero order placement. It may still receive data, compute signals, and log hypothetical signals — but no order is ever submitted. This is the master off switch.

**Default:** `true`. Set to `false` immediately if unexpected behaviour is observed and the cause is unknown.

### 9.3 API Failure Handling

```
API_MAX_RETRIES = 3
API_RETRY_DELAY = 2s     # Between retries
```

On 3 consecutive failed Upstox API calls (any order or data endpoint):

1. **Halt all new entries** for the session.
2. **Alert user via Telegram** (channel/bot to be configured in `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`).
3. **Do NOT auto-close existing positions.** Open positions require manual intervention — the system does not know if the API failure is transient or reflects a broader exchange/connectivity issue.

Log all API failures as `API_ERROR` entries in the audit trail.

### 9.4 Audit Trail

Every system action is logged to two targets:

**CSV (human-readable, date-partitioned):**
```
logs/audit_YYYY-MM-DD.csv
```

**SQLite (queryable history):**
```
logs/audit.db
```

**Required columns for every log entry:**

| Column | Type | Description |
|---|---|---|
| `timestamp` | ISO8601 | UTC timestamp of action |
| `action_type` | string | e.g., `ENTRY_ORDER`, `SL_HIT`, `TIME_STOP_EXIT`, `VIX_FILTER_TRIGGERED` |
| `instrument` | string | e.g., `NSE_EQ|RELIANCE` |
| `side` | string | `BUY` / `SELL` / `N/A` |
| `quantity` | int | Shares/lots |
| `price` | float | Execution or trigger price |
| `strategy_name` | string | `ORB` / `MOMENTUM` / `MEAN_REVERSION` / `SYSTEM` |
| `reasoning` | string | Human-readable explanation of why this action occurred |
| `sl` | float | Stop-loss price at time of action (null if not applicable) |
| `target` | float | Target price (null if not applicable) |
| `pnl` | float | Realised P&L for closing actions; null for entries |

Both targets receive every log entry. If SQLite write fails, continue to CSV only and log the SQLite failure.

### 9.5 Emergency Checklist

If unexpected positions appear, orders are missing, or the system behaves unexpectedly:

1. Set `ENABLE_TRADING=false` in `.env` immediately.
2. Log into Upstox web/app and verify open positions manually.
3. Close any positions that should not exist.
4. Check `logs/audit_YYYY-MM-DD.csv` for the last 50 entries to identify root cause.
5. Do not re-enable trading until the cause is understood and resolved.

---

## 10. Change Log

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-05-20 | Akhil Sachan | Initial constitution — Phase 4 trading rules. All parameters are v1 defaults; full backtesting and tuning deferred to Phase 6. |

---

## 11. Notes for Future Refinement

These notes distinguish **locked-in structural decisions** from **starting-point parameters** that will be revisited in Phase 6 backtesting.

### Locked-in (structural, not tunable in Phase 6)

- Daily/weekly/monthly stop loss hierarchy — these are risk-of-ruin guards, not performance parameters.
- SEBI algo tagging requirement — regulatory, non-negotiable.
- Intraday force-close at 15:15 — exchange rule / margin safety.
- GTT orders for swing SL — operational safety, not a style choice.
- Kill switch (`ENABLE_TRADING`) — safety infrastructure.
- Stale signal re-validation — correctness, not tuning.
- Friday no-swing rule — structural position on weekend gap risk.

### Starting defaults (will be tuned in Phase 6 backtesting)

- **Risk:Reward ratios** (2:1 ORB, 2.5:1 Momentum, 1:1 MR) — set from convention; backtesting will determine optimal ratios per strategy.
- **ATR multiplier for SL** (1.5×) — reasonable starting point; may need widening for lower-volatility stocks or tightening for high-beta names.
- **VIX threshold** (22.0) and gap+VIX composite (18.0 / 1.0%) — calibrate against historical VIX distribution and win-rate conditional on VIX bucket.
- **Time stops** (30/60/90 min) — based on typical intraday pattern duration; backtest will show where exits cluster.
- **Mean Reversion ADX filter** (< 25) and streak filter (< 3 candles) — conservative starting values; likely the most sensitive parameters in that strategy.
- **Bollinger Band settings** (20-period, 2 std dev) — standard default; may explore 1.5 or 2.5 std dev for MR signal frequency vs quality trade-off.
- **Volume threshold** (1.5× 20-day avg) — uniform across strategies for simplicity; may stratify by instrument or time-of-day in later versions.
- **Trail percentage** (1%) — arbitrary starting point; test 0.5%, 1.5%, 2% to find what retains most of Momentum winners without early exit.

### Planned Phase 6 tasks

- [ ] Backtest all three strategies on 3 years of OHLCV data (use `scripts/history.py` as data source)
- [ ] Validate per-instrument ATR-based SL vs fixed-% SL
- [ ] Quantify event-day edge degradation to confirm 0.5× size multiplier is calibrated correctly
- [ ] Test Momentum shorts (v2 gate: positive expectancy confirmed)
- [ ] Build `data/event_calendar.json` for the event-day soft guard
- [ ] Calibrate `RISK_CAPITAL` against live account equity once paper trading phase completes
