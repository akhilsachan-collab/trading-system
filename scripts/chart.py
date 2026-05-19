"""
chart.py — Interactive candlestick chart viewer for historical OHLC CSV data.

Usage:
    python scripts/chart.py "NSE_INDEX|Nifty 50"
    python scripts/chart.py "NSE_INDEX|Nifty 50" --last 60 --indicator sma20 --indicator sma50
    python scripts/chart.py "NSE_EQ|INE002A01018" --interval 5minute --indicator vwap
"""

import argparse
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

DATA_DIR   = Path(__file__).resolve().parent.parent / "data" / "history"
CHARTS_DIR = Path(__file__).resolve().parent.parent / "data" / "charts"

INDICATOR_COLORS = {
    "SMA 20":    "#ffeb3b",
    "SMA 50":    "#ff9800",
    "SMA 200":   "#ef5350",
    "EMA 9":     "#64b5f6",
    "EMA 21":    "#ab47bc",
    "VWAP":      "#00e5ff",
    "BB Upper":  "#78909c",
    "BB Middle": "#546e7a",
    "BB Lower":  "#78909c",
}

VALID_INDICATORS = ["sma20", "sma50", "sma200", "ema9", "ema21", "vwap", "bollinger"]


def sanitize_key(instrument_key: str) -> str:
    return instrument_key.replace("|", "_").replace(" ", "_")


def csv_path(instrument_key: str, interval: str) -> Path:
    return DATA_DIR / f"{sanitize_key(instrument_key)}_{interval}.csv"


def load_data(instrument_key: str, interval: str, last: int | None) -> pd.DataFrame:
    path = csv_path(instrument_key, interval)
    if not path.exists():
        msg = f'[ERROR] No data found. Run: python scripts\\history.py "{instrument_key}" --interval {interval}\n'
        sys.stdout.buffer.write(msg.encode("utf-8"))
        sys.stdout.buffer.flush()
        sys.exit(1)

    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.sort_values("timestamp").reset_index(drop=True)

    if last is not None:
        if last > len(df):
            print(f"[NOTE] --last {last} exceeds available rows ({len(df)}); using all rows.")
        else:
            df = df.tail(last).reset_index(drop=True)

    return df


def is_index(df: pd.DataFrame) -> bool:
    return df["volume"].sum() == 0


def compute_indicators(
    df: pd.DataFrame, indicators: list[str], interval: str
) -> dict[str, pd.Series]:
    close  = df["close"]
    result: dict[str, pd.Series] = {}

    for ind in indicators:
        if ind == "sma20":
            result["SMA 20"] = close.rolling(20).mean()

        elif ind == "sma50":
            result["SMA 50"] = close.rolling(50).mean()

        elif ind == "sma200":
            result["SMA 200"] = close.rolling(200).mean()

        elif ind == "ema9":
            result["EMA 9"] = close.ewm(span=9, adjust=False).mean()

        elif ind == "ema21":
            result["EMA 21"] = close.ewm(span=21, adjust=False).mean()

        elif ind == "vwap":
            if is_index(df):
                print("[WARN] VWAP skipped: volume is 0 (index instrument).")
                continue
            typical = (df["high"] + df["low"] + df["close"]) / 3
            pv      = typical * df["volume"]
            if "minute" in interval or "hour" in interval:
                date_col = df["timestamp"].dt.date
                cum_pv   = pv.groupby(date_col).cumsum()
                cum_vol  = df["volume"].groupby(date_col).cumsum()
            else:
                cum_pv  = pv.cumsum()
                cum_vol = df["volume"].cumsum()
            result["VWAP"] = cum_pv / cum_vol

        elif ind == "bollinger":
            sma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            result["BB Upper"]  = sma20 + 2 * std20
            result["BB Middle"] = sma20
            result["BB Lower"]  = sma20 - 2 * std20

    return result


def build_figure(
    df: pd.DataFrame,
    instrument_key: str,
    interval: str,
    indicator_data: dict[str, pd.Series],
) -> go.Figure:
    index_instrument = is_index(df)
    bar_colors = [
        "#26a69a" if c >= o else "#ef5350"
        for o, c in zip(df["open"], df["close"])
    ]

    if index_instrument:
        fig        = go.Figure()
        add_kwargs: dict = {}
    else:
        fig = make_subplots(
            rows=2, cols=1,
            row_heights=[0.7, 0.3],
            shared_xaxes=True,
            vertical_spacing=0.02,
        )
        add_kwargs = {"row": 1, "col": 1}

    fig.add_trace(
        go.Candlestick(
            x=df["timestamp"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
            name="OHLC",
        ),
        **add_kwargs,
    )

    for name, series in indicator_data.items():
        color = INDICATOR_COLORS.get(name, "#ffffff")
        dash  = "dash"  if name in ("BB Upper", "BB Lower") else "solid"
        width = 1.0     if name in ("BB Upper", "BB Lower") else 1.5

        fig.add_trace(
            go.Scatter(
                x=df["timestamp"],
                y=series,
                name=name,
                line=dict(color=color, width=width, dash=dash),
                mode="lines",
            ),
            **add_kwargs,
        )

    if not index_instrument:
        fig.add_trace(
            go.Bar(
                x=df["timestamp"],
                y=df["volume"],
                marker_color=bar_colors,
                name="Volume",
                showlegend=False,
            ),
            row=2, col=1,
        )

    label     = instrument_key.split("|")[-1] if "|" in instrument_key else instrument_key
    date_min  = df["timestamp"].min().strftime("%Y-%m-%d")
    date_max  = df["timestamp"].max().strftime("%Y-%m-%d")
    title_str = f"{label} — {interval} — {date_min} to {date_max}"

    fig.update_layout(
        title=title_str,
        template="plotly_dark",
        hovermode="x unified",
        height=800,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    # Crosshair spikes on all axes
    fig.update_xaxes(
        showspikes=True,
        spikemode="across",
        spikedash="solid",
        spikethickness=1,
        spikesnap="cursor",
    )
    fig.update_yaxes(
        showspikes=True,
        spikemode="across",
        spikedash="solid",
        spikethickness=1,
    )

    # Range slider at the bottom; disable the one auto-attached to the candlestick
    if index_instrument:
        fig.update_layout(xaxis_rangeslider_visible=True)
    else:
        fig.update_layout(
            xaxis_rangeslider_visible=False,
            xaxis2_rangeslider_visible=True,
            xaxis2_rangeslider_thickness=0.05,
        )

    return fig


def print_summary(
    df: pd.DataFrame,
    instrument_key: str,
    interval: str,
    indicator_data: dict[str, pd.Series],
) -> None:
    n       = len(df)
    d_min   = df["timestamp"].min().strftime("%Y-%m-%d")
    d_max   = df["timestamp"].max().strftime("%Y-%m-%d")
    c_first = df["close"].iloc[0]
    c_last  = df["close"].iloc[-1]
    ret_pct = (c_last - c_first) / c_first * 100
    arrow   = "\U0001f4c8" if ret_pct >= 0 else "\U0001f4c9"  # 📈 / 📉
    sign    = "+" if ret_pct >= 0 else ""
    ind_str = ", ".join(indicator_data.keys()) if indicator_data else "none"

    lines = (
        "\n"
        f"  Instrument : {instrument_key}\n"
        f"  Interval   : {interval}\n"
        f"  Date range : {d_min} → {d_max}\n"
        f"  Candles    : {n:,}\n"
        f"  First close: {c_first:,.2f}\n"
        f"  Last close : {c_last:,.2f}\n"
        f"  Return     : {arrow} {sign}{ret_pct:.2f}%\n"
        f"  Indicators : {ind_str}\n"
    )
    sys.stdout.buffer.write(lines.encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="View historical OHLC data as an interactive candlestick chart.",
        epilog=(
            'Examples:\n'
            '  python scripts/chart.py "NSE_INDEX|Nifty 50"\n'
            '  python scripts/chart.py "NSE_INDEX|Nifty 50" --last 60 --indicator sma20 --indicator sma50\n'
            '  python scripts/chart.py "NSE_EQ|INE002A01018" --interval 5minute --indicator vwap'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("instrument_key", help='e.g. "NSE_INDEX|Nifty 50"')
    parser.add_argument(
        "--interval",
        default="day",
        metavar="INTERVAL",
        help="Candle interval (default: day)",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=None,
        metavar="N",
        help="Show only the last N candles",
    )
    parser.add_argument(
        "--indicator",
        action="append",
        dest="indicators",
        default=[],
        choices=VALID_INDICATORS,
        metavar="INDICATOR",
        help=(
            f"Overlay indicator (repeatable). Choices: {', '.join(VALID_INDICATORS)}"
        ),
    )
    args = parser.parse_args()

    df             = load_data(args.instrument_key, args.interval, args.last)
    indicator_data = compute_indicators(df, args.indicators, args.interval)
    fig            = build_figure(df, args.instrument_key, args.interval, indicator_data)

    print_summary(df, args.instrument_key, args.interval, indicator_data)

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    key_slug = sanitize_key(args.instrument_key)
    out_path = CHARTS_DIR / f"{key_slug}_{args.interval}_{ts}.html"
    fig.write_html(str(out_path))

    out_msg = f"\n  Saved  : {out_path}\n  Opening in browser...\n"
    sys.stdout.buffer.write(out_msg.encode("utf-8"))
    sys.stdout.buffer.flush()

    webbrowser.open(out_path.as_uri())


if __name__ == "__main__":
    main()
