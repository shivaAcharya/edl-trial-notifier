#!/usr/bin/env python3
# trial_slot_notifier.py - Monitors Nepal EDL portal for available trial slots
# and sends an SMS via Twilio when a slot opens up.
#
# Reusable: change BOOKING_URL, TO_PHONE_NUMBER, and BEARER_TOKEN in .env.
#
# Usage:
#   pip install -r requirements.txt
#   cp .env.example .env        # fill in your credentials
#
#   python trial_slot_notifier.py           # continuous polling (every 5 min)
#   python trial_slot_notifier.py --test    # verify API + send a test SMS, then exit
#   python trial_slot_notifier.py --once    # check once and exit (used by GitHub Actions)
#
# IMPORTANT — Bearer token:
#   The API requires a JWT from the EDL portal. It lasts 15 days.
#   To renew: open your booking page in Chrome → F12 → Network tab → reload →
#   click the date-availability request → copy the Authorization header value
#   (everything after "Bearer ") → paste into BEARER_TOKEN in .env.

import argparse
import base64
import json
import os
import time
import logging
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

# ── Config (all values come from .env) ──────────────────────────────────────
BOOKING_URL     = os.getenv("BOOKING_URL", "").strip()
BEARER_TOKEN    = os.getenv("BEARER_TOKEN", "").strip()
TO_PHONE        = os.getenv("TO_PHONE_NUMBER", "").strip()
TWILIO_SID      = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM     = os.getenv("TWILIO_FROM_NUMBER", "").strip()
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))   # default: 5 min
SCAN_DAYS_AHEAD = int(os.getenv("SCAN_DAYS_AHEAD", "60"))          # default: 60 days

# Warn by SMS when token expires within this many days
TOKEN_WARN_DAYS = 2

API_URL = (
    "https://edl-public-api.lumbini.gov.np"
    "/api/v1/web/applicant/application/date-availability"
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Token helpers ─────────────────────────────────────────────────────────────

def decode_token_expiry() -> datetime | None:
    """Return the expiry datetime from the JWT, or None if it can't be parsed."""
    try:
        payload_b64 = BEARER_TOKEN.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return datetime.fromtimestamp(payload["exp"])
    except Exception:
        return None


def check_token_validity() -> None:
    """
    Log the token expiry. Raise SystemExit if already expired.
    Returns without error if token looks fine.
    """
    exp = decode_token_expiry()
    if exp is None:
        log.warning("Could not decode token expiry — proceeding anyway.")
        return

    remaining = exp - datetime.now()

    if remaining.total_seconds() <= 0:
        raise SystemExit(
            f"BEARER_TOKEN expired at {exp.strftime('%Y-%m-%d %H:%M')}.\n"
            "Renew it:\n"
            "  1. Open your booking page in Chrome\n"
            "  2. F12 → Network tab → reload the page\n"
            "  3. Click the 'date-availability' request\n"
            "  4. Copy the Authorization header value (after 'Bearer ')\n"
            "  5. Paste it as BEARER_TOKEN in .env (or GitHub Secret)"
        )

    days_left = remaining.days
    if days_left < TOKEN_WARN_DAYS:
        log.warning("Token expires in %d day(s) on %s — renew soon!",
                    days_left, exp.strftime("%Y-%m-%d %H:%M"))
    else:
        log.info("Token expires  : %s (%d days remaining)",
                 exp.strftime("%Y-%m-%d %H:%M"), days_left)


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EDL Trial Slot Notifier — monitors and texts when slots open"
    )
    parser.add_argument(
        "--test", "-t",
        action="store_true",
        help="Check the API once, print results, send a test SMS, then exit",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once and exit without looping (used by GitHub Actions cron)",
    )
    return parser.parse_args()


def validate_config() -> None:
    """Raise early if any required .env variable is missing."""
    required = {
        "BOOKING_URL": BOOKING_URL,
        "BEARER_TOKEN": BEARER_TOKEN,
        "TO_PHONE_NUMBER": TO_PHONE,
        "TWILIO_ACCOUNT_SID": TWILIO_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
        "TWILIO_FROM_NUMBER": TWILIO_FROM,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise SystemExit(
            f"Missing required .env variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )


def extract_uuid(url: str) -> str:
    """Pull the application UUID from the last segment of the booking URL."""
    return urlparse(url).path.rstrip("/").split("/")[-1]


def fetch_availability(uuid: str) -> list:
    """Call the API and return the full list of date objects for the scan window."""
    today = date.today()
    params = {
        "type": "TRIAL",
        "from": today.isoformat(),
        "to": (today + timedelta(days=SCAN_DAYS_AHEAD)).isoformat(),
        "application_uuid": uuid,
    }
    headers = {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Origin": "https://edlvrs.lumbini.gov.np",
        "Referer": "https://edlvrs.lumbini.gov.np/",
    }
    response = requests.get(API_URL, params=params, headers=headers, timeout=15)

    if response.status_code == 401:
        raise PermissionError(
            "API returned 401 Unauthorized — Bearer token has expired.\n"
            "Update BEARER_TOKEN in .env (or GitHub Secret) with a fresh token."
        )

    response.raise_for_status()
    return response.json().get("data", [])


def send_sms(body: str) -> None:
    """Send an SMS via Twilio."""
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    client.messages.create(body=body, from_=TWILIO_FROM, to=TO_PHONE)
    log.info("SMS sent to %s", TO_PHONE)


# ── Core logic ────────────────────────────────────────────────────────────────

def maybe_warn_token_expiry_by_sms() -> None:
    """Send a one-time SMS warning if the token is about to expire."""
    exp = decode_token_expiry()
    if exp is None:
        return
    remaining = exp - datetime.now()
    if 0 < remaining.total_seconds() < TOKEN_WARN_DAYS * 86400:
        try:
            send_sms(
                f"[EDL Notifier] Token expires {exp.strftime('%Y-%m-%d')} "
                f"({remaining.days}d left). Renew BEARER_TOKEN to keep monitoring."
            )
            log.warning("Token expiry warning SMS sent.")
        except Exception as exc:
            log.error("Could not send token warning SMS: %s", exc)


def check_and_notify(uuid: str, already_notified: set, token_warned: list) -> set:
    """
    Fetch availability, compare against already-notified dates, and send SMS
    for any newly available slots.

    Returns the updated set of notified dates.
    token_warned is a mutable list used as a flag to avoid repeated warning SMS.
    """
    # Send token expiry warning SMS once per session if expiry is close
    if not token_warned:
        exp = decode_token_expiry()
        if exp and 0 < (exp - datetime.now()).total_seconds() < TOKEN_WARN_DAYS * 86400:
            maybe_warn_token_expiry_by_sms()
            token_warned.append(True)

    try:
        slots = fetch_availability(uuid)
    except PermissionError as exc:
        log.critical(str(exc))
        try:
            send_sms("[EDL Notifier] STOPPED — Bearer token expired. Renew it to resume.")
        except Exception:
            pass
        raise SystemExit(1)
    except requests.RequestException as exc:
        log.error("API request failed: %s", exc)
        return already_notified

    # Bookable: is_bookable flag is True AND at least one seat is open
    bookable = [s for s in slots if s.get("is_bookable") and s.get("available", 0) > 0]
    bookable_dates = {s["date"] for s in bookable}

    # New = bookable now but not yet texted about
    new_slots = [s for s in bookable if s["date"] not in already_notified]

    if new_slots:
        summary = ", ".join(
            f"{s['date']} ({s['available']} slot{'s' if s['available'] != 1 else ''})"
            for s in new_slots
        )
        message = (
            f"[EDL Trial Slot Alert]\n"
            f"Available dates: {summary}\n"
            f"Book now: {BOOKING_URL}"
        )
        log.info("New slot(s) found: %s", summary)
        try:
            send_sms(message)
        except Exception as exc:
            log.error("Failed to send SMS: %s", exc)
            return already_notified

        already_notified.update(s["date"] for s in new_slots)
    else:
        log.info(
            "No new slots. Next check in %ds. (Bookable now: %s)",
            POLL_INTERVAL,
            ", ".join(sorted(bookable_dates)) or "none",
        )

    # Prune dates no longer bookable so we re-notify if they reopen
    already_notified &= bookable_dates

    return already_notified


# ── Test mode ─────────────────────────────────────────────────────────────────

def run_test(uuid: str) -> None:
    """
    Test mode: verify token, hit the API, print results, send a test SMS.
    Run this before deploying to confirm everything is wired up correctly.
    """
    log.info("=" * 55)
    log.info("TEST MODE — checking API and SMS connectivity")
    log.info("=" * 55)

    log.info("Fetching availability for UUID: %s", uuid)
    log.info("Scan window: today → +%d days", SCAN_DAYS_AHEAD)

    try:
        slots = fetch_availability(uuid)
    except PermissionError as exc:
        log.error(str(exc))
        raise SystemExit(1)
    except requests.RequestException as exc:
        log.error("API request FAILED: %s", exc)
        raise SystemExit(1)

    log.info("API OK — received %d date entries", len(slots))

    bookable = [s for s in slots if s.get("is_bookable") and s.get("available", 0) > 0]

    if bookable:
        log.info("Bookable slots found:")
        for s in bookable:
            log.info("  %s — %d available", s["date"], s["available"])
    else:
        log.info(
            "No bookable slots right now (expected if all full). "
            "Will alert as soon as one opens."
        )

    log.info("Sample of returned dates (first 5):")
    for s in slots[:5]:
        log.info(
            "  %s | total=%s booked=%s available=%s bookable=%s",
            s["date"], s["total"], s["booked"], s["available"], s["is_bookable"],
        )

    log.info("Sending test SMS to %s ...", TO_PHONE)
    try:
        send_sms(
            f"[EDL Notifier — Test]\n"
            f"Setup is working correctly.\n"
            f"Monitoring: {BOOKING_URL}"
        )
        log.info("Test SMS sent successfully!")
    except Exception as exc:
        log.error("SMS FAILED: %s", exc)
        raise SystemExit(1)

    log.info("=" * 55)
    log.info("All checks passed. Ready to deploy.")
    log.info("=" * 55)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    validate_config()
    check_token_validity()

    uuid = extract_uuid(BOOKING_URL)

    log.info("━" * 55)
    log.info("EDL Trial Slot Notifier")
    log.info("Application UUID : %s", uuid)
    log.info("Notify           : %s", TO_PHONE)
    log.info("Poll interval    : %ds", POLL_INTERVAL)
    log.info("Scan window      : %d days ahead", SCAN_DAYS_AHEAD)
    log.info("━" * 55)

    if args.test:
        run_test(uuid)
        return

    notified: set = set()
    token_warned: list = []

    if args.once:
        check_and_notify(uuid, notified, token_warned)
    else:
        while True:
            notified = check_and_notify(uuid, notified, token_warned)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
