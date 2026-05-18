"""
Fetch Upstox user profile via API v2.
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


def fmt_list(values) -> str:
    if not values:
        return "N/A"
    return ", ".join(values)


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


def main() -> None:
    if not TOKEN:
        print("[ERROR] UPSTOX_ACCESS_TOKEN is not set in your .env file.")
        sys.exit(1)

    print("\nConnecting to Upstox API v2...\n")
    profile = fetch_profile()
    print_profile(profile)


if __name__ == "__main__":
    main()
