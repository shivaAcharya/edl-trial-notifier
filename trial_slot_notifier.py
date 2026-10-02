#!/usr/bin/env python3
# trial_slot_notifier.py - Monitors Nepal EDL portal for available trial slots
# and sends an SMS via Twilio when a slot opens up.
#
# Reusable: change BOOKING_URL and TO_PHONE_NUMBER in .env for any applicant.
#
# Usage:
#   pip install -r requirements.txt
#   cp .env.example .env        # fill in your credentials
#
#   python trial_slot_notifier.py           # continuous polling (every 5 min)
#   python trial_slot_notifier.py --test    # verify API + send a test SMS, then exit
#   python trial_slot_notifier.py --once    # check once and exit (used by GitHub Actions)

import argparse
import os
import time
import logging
from datetime import date, timedelta
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

# ── Config (all values come from .env) ──────────────────────────────────────
BOOKING_URL     = os.getenv("BOOKING_URL", "").strip()
TO_PHONE        = os.getenv("TO_PHONE_NUMBER", "").strip()
TWILIO_SID      = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM     = os.getenv("TWILIO_FROM_NUMBER", "").strip()
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))   # default: 5 min
SCAN_DAYS_AHEAD = int(os.getenv("SCAN_DAYS_AHEAD", "60"))          # default: 60 days

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
    response = requests.get(API_URL, params=params, timeout=15)
    response.raise_for_status()
    return response.json().get("data", [])


def send_sms(body: str) -> None:
    """Send an SMS via Twilio."""
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    client.messages.create(body=body, from_=TWILIO_FROM, to=TO_PHONE)
    log.info("SMS sent to %s", TO_PHONE)


# ── Core logic ────────────────────────────────────────────────────────────────

def check_and_notify(uuid: str, already_notified: set) -> set:
    """
    Fetch availability, compare against already-notified dates, and send SMS
    for any newly available slots.

    Returns the updated set of notified dates.
    """
    try:
        slots = fetch_availability(uuid)
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
            # Don't add to notified so we retry next cycle
            return already_notified

        already_notified.update(s["date"] for s in new_slots)
    else:
        log.info(
            "No new slots. Next check in %ds. (Bookable now: %s)",
            POLL_INTERVAL,
            ", ".join(sorted(bookable_dates)) or "none",
        )

    # Prune dates that are no longer bookable so we re-notify if they reopen
    already_notified &= bookable_dates

    return already_notified


def run_test(uuid: str) -> None:
    """
    Test mode: make one API call, print full availability, send a test SMS.
    Use this to confirm credentials and API access are working before deploying.
    """
    log.info("=" * 55)
    log.info("TEST MODE — checking API and SMS connectivity")
    log.info("=" * 55)

    log.info("Fetching availability for UUID: %s", uuid)
    log.info("Scan window: today → +%d days", SCAN_DAYS_AHEAD)

    try:
        slots = fetch_availability(uuid)
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
            "No bookable slots right now (this is expected if all are full). "
            "The notifier will alert you as soon as one opens."
        )

    # Show a sample of dates to confirm the API is returning data
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

    if args.once:
        # Single-shot mode for GitHub Actions scheduled runs
        check_and_notify(uuid, notified)
    else:
        # Continuous polling mode for running on a server/local machine
        while True:
            notified = check_and_notify(uuid, notified)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
