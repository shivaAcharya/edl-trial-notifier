#!/usr/bin/env python3
# refresh_token.py - Interactive token renewal helper for EDL Trial Notifier
#
# Walks through the OTP login flow and updates bearer_token in users.json
# automatically — no browser DevTools required after first setup.
#
# Usage:
#   python refresh_token.py              # refresh expired / expiring users
#   python refresh_token.py --all        # refresh every user regardless
#   python refresh_token.py --user Shiva # refresh one user by name
#
# The applicant's application_id (e.g. "N562101") is needed once.
# If you add it to users.json under "application_id", it will be remembered.
#
# ── How to find the correct endpoint URLs (one-time step) ────────────────────
# If the OTP request fails, the endpoint paths below may differ slightly.
# To confirm them:
#   1. Open https://edlvrs.lumbini.gov.np in Chrome
#   2. F12 → Network tab → check "Preserve log"
#   3. Enter your application_id and click "Send OTP"
#   4. Find the POST request in the Network tab
#   5. Copy the full Request URL and update SEND_OTP_URL below
#   6. Complete the OTP flow and similarly copy the verify-otp Request URL
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import base64
import json
import os
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

USERS_FILE = os.getenv("USERS_FILE", "users.json")

# ── Endpoint URLs ─────────────────────────────────────────────────────────────
# These are based on the known API base pattern. Confirm via DevTools if needed.
_API_BASE = "https://edl-public-api.lumbini.gov.np/api/v1/web"
SEND_OTP_URL   = f"{_API_BASE}/applications/track"             # POST: {"application_id": "...", "recaptcha_token": ""}
VERIFY_OTP_URL = f"{_API_BASE}/applications/track/verify-otp"  # POST: {"application_id": "...", "token": "123456"}

_HEADERS = {
    "Origin":       "https://edlvrs.lumbini.gov.np",
    "Referer":      "https://edlvrs.lumbini.gov.np/",
    "Content-Type": "application/json",
}

# Offer to refresh tokens within this many days of expiry (even if not yet expired)
REFRESH_WITHIN_DAYS = 5


# ── Token helpers ─────────────────────────────────────────────────────────────

def decode_expiry(token: str) -> datetime | None:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return datetime.fromtimestamp(payload["exp"])
    except Exception:
        return None


def days_remaining(token: str) -> int | None:
    exp = decode_expiry(token)
    if exp is None:
        return None
    return (exp - datetime.now()).days


# ── File I/O ──────────────────────────────────────────────────────────────────

def load_users() -> list:
    if not os.path.exists(USERS_FILE):
        raise SystemExit(f"'{USERS_FILE}' not found. Run from the edl_trial_notifier/ directory.")
    with open(USERS_FILE) as f:
        return json.load(f)


def save_users(users: list) -> None:
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)
        f.write("\n")
    print(f"  Saved updated token(s) to {USERS_FILE}.")


# ── OTP flow ──────────────────────────────────────────────────────────────────

def send_otp(application_id: str) -> bool:
    """POST to the login endpoint to trigger OTP to the applicant's phone."""
    print(f"  Requesting OTP for application ID: {application_id} ...")
    try:
        r = requests.post(
            SEND_OTP_URL,
            json={"application_id": application_id, "recaptcha_token": ""},
            headers=_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        print("  OTP sent — check the phone number registered with this application.")
        return True
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body   = exc.response.text[:300] if exc.response is not None else ""
        print(f"  ERROR: server returned HTTP {status}.")
        if body:
            print(f"  Body: {body}")
        _maybe_hint_endpoint(status)
        return False
    except requests.RequestException as exc:
        print(f"  ERROR: {exc}")
        return False


def verify_otp(application_id: str, otp: str) -> str | None:
    """
    POST to the verify-otp endpoint.
    Returns the new access_token string, or None on failure.
    """
    print("  Verifying OTP ...")
    try:
        r = requests.post(
            VERIFY_OTP_URL,
            json={"application_id": application_id, "token": otp},
            headers=_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        # The token may be at the top level or nested under "data"
        token = (
            data.get("access_token")
            or data.get("data", {}).get("access_token")
            or data.get("token")
        )
        if not token:
            print("  ERROR: Response did not contain an access_token.")
            print(f"  Keys received: {list(data.keys())}")
            print("  Full response (truncated):", json.dumps(data)[:400])
            return None
        return token
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body   = exc.response.text[:300] if exc.response is not None else ""
        print(f"  ERROR: server returned HTTP {status}.")
        if body:
            print(f"  Body: {body}")
        _maybe_hint_endpoint(status)
        return None
    except requests.RequestException as exc:
        print(f"  ERROR: {exc}")
        return None


def _maybe_hint_endpoint(status: int | str) -> None:
    if status in (404, "404"):
        print()
        print("  HINT: 404 usually means the endpoint URL is wrong.")
        print("  Confirm the correct URL via Chrome DevTools (see instructions at top of this file).")
        print(f"  Current SEND_OTP_URL   = {SEND_OTP_URL}")
        print(f"  Current VERIFY_OTP_URL = {VERIFY_OTP_URL}")


# ── Per-user refresh flow ─────────────────────────────────────────────────────

def refresh_one(user: dict, users: list, idx: int) -> bool:
    """
    Guide the user through OTP authentication for a single applicant.
    Mutates users[idx] with the new bearer_token if successful.
    Returns True on success.
    """
    name = user.get("name", f"user[{idx}]")
    print(f"\n{'─' * 55}")
    print(f"  User: {name}")

    # Resolve application_id
    app_id = user.get("application_id", "").strip()
    if not app_id:
        print("  This user's application_id is not stored in users.json.")
        app_id = input("  Enter application ID (e.g. N562101): ").strip()
        if not app_id:
            print("  Skipped — no application ID entered.")
            return False

    # Step 1 — trigger OTP
    if not send_otp(app_id):
        return False

    # Step 2 — get OTP from keyboard
    otp = input("  Enter the OTP from the phone: ").strip()
    if not otp:
        print("  Skipped — no OTP entered.")
        return False

    # Step 3 — verify
    new_token = verify_otp(app_id, otp)
    if not new_token:
        return False

    # Step 4 — update in-memory record
    users[idx]["bearer_token"] = new_token

    # Offer to save the application_id for future runs
    if not user.get("application_id"):
        ans = input(f"  Remember application_id '{app_id}' in users.json? [Y/n]: ").strip().lower()
        if ans != "n":
            users[idx]["application_id"] = app_id

    exp = decode_expiry(new_token)
    if exp:
        days = (exp - datetime.now()).days
        print(f"  New token valid until: {exp.strftime('%Y-%m-%d %H:%M')} ({days} days)")

    return True


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="EDL Token Refresh Helper — renew bearer_token entries in users.json"
    )
    p.add_argument("--user", "-u", metavar="NAME",
                   help="Refresh only this user (matched by name, case-insensitive)")
    p.add_argument("--all", "-a", action="store_true",
                   help="Refresh all users regardless of token status")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    users = load_users()

    print("=" * 55)
    print("  EDL Token Refresh Helper")
    print("=" * 55)

    # ── Summarise token status ────────────────────────────────────────────────
    for user in users:
        name  = user.get("name", "?")
        dr    = days_remaining(user.get("bearer_token", ""))
        exp   = decode_expiry(user.get("bearer_token", ""))
        exp_s = exp.strftime("%Y-%m-%d") if exp else "unknown"

        if dr is None:
            status = "[?? unknown]"
        elif dr <= 0:
            status = "[EXPIRED]"
        elif dr < REFRESH_WITHIN_DAYS:
            status = f"[expires in {dr}d — refresh recommended]"
        else:
            status = f"[OK, {dr}d remaining]"

        print(f"  {name:<20s}  expires: {exp_s}  {status}")

    print()

    # ── Decide which users to refresh ────────────────────────────────────────
    targets: list[int] = []
    for i, user in enumerate(users):
        name = user.get("name", f"user[{i}]")
        if args.user:
            if name.lower() == args.user.lower():
                targets.append(i)
        elif args.all:
            targets.append(i)
        else:
            dr = days_remaining(user.get("bearer_token", ""))
            if dr is None or dr < REFRESH_WITHIN_DAYS:
                targets.append(i)

    if not targets:
        print("All tokens are healthy — nothing to refresh.")
        print("Use --all to force-refresh anyway.")
        return

    print(f"Refreshing {len(targets)} user(s). You will need access to their registered phone(s).")

    # ── Run OTP flow for each target ─────────────────────────────────────────
    updated: list[str] = []
    for i in targets:
        if refresh_one(users[i], users, i):
            updated.append(users[i].get("name", f"user[{i}]"))

    # ── Persist changes ───────────────────────────────────────────────────────
    print()
    if updated:
        save_users(users)
        print(f"Updated: {', '.join(updated)}")
        print()
        print("Next steps:")
        print("  Local run  : restart trial_slot_notifier.py — it will pick up the new token.")
        print("  GitHub Actions: go to Settings → Secrets → USERS_CONFIG")
        print("                  and paste the new contents of users.json.")
    else:
        print("No tokens were updated.")


if __name__ == "__main__":
    main()
