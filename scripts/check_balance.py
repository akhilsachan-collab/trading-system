"""
Fetch Upstox user profile and funds/margin via API v2.
Credentials are loaded from .env in the project root.
"""

import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = "https://api.upstox.com/v2"
TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")

_FUNDS_FIELDS = [
    ("available_margin", "Available Margin"),
    ("used_margin",      "Used Margin     "),
    ("payin_amount",     "Payin Amount    "),
    ("span_margin",      "Span Margin     "),
    ("adhoc_margin",     "Adhoc Margin    "),
    ("notional_cash",    "Notional Cash   "),
    ("exposure_margin",  "Exposure Margin "),
]


def build_headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    }


def handle_error(response: requests.Response, context: str) -> None:
    code = response.status_code
    if code in (401, 403):
        print(f"\n[ERROR] {context}: token is expired or invalid (HTTP {code}).")
        print("  -> Log in at https://developer.upstox.com, generate a new")
        print("     access token, and update UPSTOX_ACCESS_TOKEN in your .env file.")
    else:
        print(f"\n[ERROR] {context}: HTTP {code} — {response.text[:200]}")
    sys.exit(1)


def fetch_profile() -> dict:
    r = requests.get(f"{BASE_URL}/user/profile", headers=build_headers(), timeout=10)
    if not r.ok:
        handle_error(r, "Fetching profile")
    return r.json().get("data", {})


def fetch_funds() -> dict | None:
    """Returns the funds data dict, or None if unavailable (e.g. outside market hours)."""
    r = requests.get(f"{BASE_URL}/user/get-funds-and-margin", headers=build_headers(), timeout=10)
    if not r.ok:
        if r.status_code in (401, 403):
            handle_error(r, "Fetching funds")
        # Non-auth failure — likely market-hours restriction
        return None
    body = r.json()
    # Upstox sometimes returns HTTP 200 with a message indicating data is unavailable
    msg = (body.get("message") or body.get("errors") or "").lower() if isinstance(body.get("message"), str) else ""
    if "not available" in msg or "before market" in msg:
        return None
    return body.get("data")


def fmt_list(values) -> str:
    if not values:
        return "N/A"
    return ", ".join(values)


def fmt_inr(value) -> str:
    """Format a number using Indian grouping (e.g. 1,23,456.78) with two decimal places."""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    integer_part = int(amount)
    decimal_part = round((amount - integer_part) * 100)
    s = str(integer_part)
    # Indian grouping: last 3 digits, then groups of 2
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while len(rest) > 2:
            groups.append(rest[-2:])
            rest = rest[:-2]
        groups.append(rest)
        grouped = ",".join(reversed(groups)) + "," + last3
    else:
        grouped = s
    return f"{sign}{grouped}.{decimal_part:02d}"


def print_profile(p: dict) -> None:
    print("=" * 50)
    print("  USER PROFILE")
    print("=" * 50)
    print(f"  Name           : {p.get('user_name', 'N/A')}")
    print(f"  User ID        : {p.get('user_id', 'N/A')}")
    print(f"  Email          : {p.get('email', 'N/A')}")
    print(f"  Broker         : {p.get('broker', 'N/A')}")
    print(f"  Exchanges      : {fmt_list(p.get('exchanges'))}")
    print(f"  Products       : {fmt_list(p.get('products'))}")
    print(f"  Order Types    : {fmt_list(p.get('order_types'))}")
    print("=" * 50)


def _buf(text: str) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def print_segment(title: str, seg: dict) -> str:
    lines = [f"  {title}\n"]
    for key, label in _FUNDS_FIELDS:
        lines.append(f"    {label} : ₹ {fmt_inr(seg.get(key, 0)):>12}\n")
    lines.append("\n")
    return "".join(lines)


def print_funds(data: dict) -> None:
    equity = data.get("equity") or {}
    commodity = data.get("commodity") or {}

    out = "\n" + "=" * 54 + "\n"
    out += "  ACCOUNT FUNDS & MARGIN\n"
    out += "=" * 54 + "\n"
    out += print_segment("EQUITY SEGMENT", equity)
    out += print_segment("COMMODITY SEGMENT", commodity)
    out += "=" * 54 + "\n"

    eq_avail = float(equity.get("available_margin") or 0)
    com_avail = float(commodity.get("available_margin") or 0)
    total = eq_avail + com_avail
    out += f"\n\U0001f4b0 Total Available Capital (Equity + Commodity): ₹ {fmt_inr(total)}\n\n"

    _buf(out)


def main() -> None:
    if not TOKEN:
        print("[ERROR] UPSTOX_ACCESS_TOKEN is not set in your .env file.")
        sys.exit(1)

    print("\nConnecting to Upstox API v2...\n")
    profile = fetch_profile()
    print_profile(profile)

    funds = fetch_funds()
    if funds is None:
        sys.stdout.buffer.write(
            "\n⚠️  Funds data not available right now. Upstox restricts this endpoint"
            " to market hours and post-EOD settlement.\n\n".encode("utf-8")
        )
    else:
        print_funds(funds)


if __name__ == "__main__":
    main()
