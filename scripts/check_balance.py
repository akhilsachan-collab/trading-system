"""
Fetch Upstox account profile and fund balances via API v2.
Credentials are loaded from .env in the project root.
"""

import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

# .env is one level up from this script (project root)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = "https://api.upstox.com/v2"
TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")


def build_headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    }


def handle_error(response: requests.Response, context: str) -> None:
    """Print a friendly message for common API errors and exit."""
    code = response.status_code
    if code in (401, 403):
        print(f"\n[ERROR] {context}: token is expired or invalid (HTTP {code}).")
        print("  -> Log in at https://developer.upstox.com, generate a new")
        print("     access token, and update UPSTOX_ACCESS_TOKEN in your .env file.")
    elif code == 423:
        print(f"\n[ERROR] {context}: service unavailable right now (HTTP 423).")
        print("  -> Upstox Funds API is only accessible 5:30 AM – 12:00 AM IST.")
        print("     Try again during those hours.")
    else:
        print(f"\n[ERROR] {context}: HTTP {code} — {response.text[:200]}")
    sys.exit(1)


def fetch_profile() -> dict:
    r = requests.get(f"{BASE_URL}/user/profile", headers=build_headers(), timeout=10)
    if not r.ok:
        handle_error(r, "Fetching profile")
    return r.json().get("data", {})


def fetch_funds() -> dict:
    r = requests.get(f"{BASE_URL}/user/get-funds-and-margin", headers=build_headers(), timeout=10)
    if not r.ok:
        handle_error(r, "Fetching funds")
    return r.json().get("data", {})


def fmt_inr(value) -> str:
    """Format a number as Indian rupees."""
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "N/A"


def print_profile(profile: dict) -> None:
    print("=" * 45)
    print("  USER PROFILE")
    print("=" * 45)
    print(f"  Name     : {profile.get('user_name', 'N/A')}")
    print(f"  User ID  : {profile.get('user_id', 'N/A')}")
    print(f"  Email    : {profile.get('email', 'N/A')}")
    print(f"  Broker   : {profile.get('broker', 'N/A')}")


def print_segment(label: str, seg: dict) -> None:
    print(f"\n  {label}")
    print(f"  {'─' * 40}")
    print(f"  Available Cash   : {fmt_inr(seg.get('available_margin'))}")
    print(f"  Used Margin      : {fmt_inr(seg.get('used_margin'))}")
    print(f"  Total Balance    : {fmt_inr(seg.get('net'))}")


def print_funds(funds: dict) -> None:
    print("\n" + "=" * 45)
    print("  ACCOUNT FUNDS")
    print("=" * 45)
    equity = funds.get("equity", {})
    commodity = funds.get("commodity", {})
    if equity:
        print_segment("EQUITY", equity)
    if commodity:
        print_segment("COMMODITY", commodity)
    if not equity and not commodity:
        print("  (No segment data returned)")


def main() -> None:
    if not TOKEN:
        print("[ERROR] UPSTOX_ACCESS_TOKEN is not set in your .env file.")
        sys.exit(1)

    print("\nConnecting to Upstox API v2...\n")
    profile = fetch_profile()
    print_profile(profile)

    funds = fetch_funds()
    print_funds(funds)
    print("\n" + "=" * 45)


if __name__ == "__main__":
    main()
