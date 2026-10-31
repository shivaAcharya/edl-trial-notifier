#!/usr/bin/env python3
# trial_slot_notifier.py - Monitors Nepal EDL portal for available trial slots
# and sends push notifications via ntfy.sh when slots open up.
#
# Supports multiple applicants in a single run via users.json.
#
# Usage:
#   pip install -r requirements.txt
#   cp users.json.example users.json   # fill in each applicant's details
#
#   python trial_slot_notifier.py           # continuous polling (every 5 min)
#   python trial_slot_notifier.py --test    # verify API + push for all users, then exit
#   python trial_slot_notifier.py --once    # check all users once and exit (GitHub Actions)
#
# users.json format:
#   [
#     {
#       "name": "Shiva",
#       "booking_url": "https://edlvrs.lumbini.gov.np/edl/YOUR-UUID",
#       "bearer_token": "eyJ..."
#       // ntfy_topic is optional — omit to use the global NTFY_TOPIC from .env
#     },
#     { ... next applicant ... }
#   ]
#
# Bearer token renewal (every 15 days per user):
#   Run:  python refresh_token.py
#   It walks through the OTP login flow and updates users.json automatically.
#   (No browser DevTools needed once application_id is stored in users.json.)

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

load_dotenv()

# ── Global config (from .env — applies to all users) ─────────────────────────
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))   # default: 5 min
SCAN_DAYS_AHEAD = int(os.getenv("SCAN_DAYS_AHEAD", "60"))          # default: 60 days
USERS_FILE      = os.getenv("USERS_FILE", "users.json")
DEFAULT_NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()           # shared fallback topic

TOKEN_WARN_DAYS = 2

STATE_FILE  = os.getenv("EDL_STATE_FILE", "edl_state.json")

NTFY_URL    = "https://ntfy.sh"
EDL_API_BASE = "https://edl-public-api.lumbini.gov.np/api/v1/web/applicant/application"
EDL_API_URL  = f"{EDL_API_BASE}/date-availability"
GET_DATE_SLOTS_URL = f"{EDL_API_BASE}/get-date-slots"
TRIAL_BOOK_URL     = f"{EDL_API_BASE}/trial/book"

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── User config ───────────────────────────────────────────────────────────────

def load_users() -> list:
    """Load and validate user configurations from users.json."""
    if not os.path.exists(USERS_FILE):
        raise SystemExit(
            f"'{USERS_FILE}' not found.\n"
            "Copy users.json.example to users.json and fill in the values."
        )
    with open(USERS_FILE) as f:
        users = json.load(f)

    if not isinstance(users, list) or not users:
        raise SystemExit(f"'{USERS_FILE}' must be a non-empty JSON array of user objects.")

    required = {"booking_url", "bearer_token"}
    for i, user in enumerate(users):
        missing = [k for k in required if not user.get(k, "").strip()]
        if missing:
            label = user.get("name", f"entry [{i}]")
            raise SystemExit(f"User '{label}' is missing required fields: {', '.join(missing)}")
        # ntfy_topic is optional per-user; falls back to global NTFY_TOPIC from .env
        if not user.get("ntfy_topic", "").strip() and not DEFAULT_NTFY_TOPIC:
            label = user.get("name", f"entry [{i}]")
            raise SystemExit(
                f"User '{label}' has no ntfy_topic, and NTFY_TOPIC is not set in .env.\n"
                "Set NTFY_TOPIC in .env or add ntfy_topic to each user entry."
            )

    return users


# ── State persistence (--once / GitHub Actions) ───────────────────────────────

def _load_state() -> dict:
    """Return persisted run state, or {} if the file is missing or unreadable."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Could not read state file '%s': %s", STATE_FILE, exc)
        return {}


def _save_state(data: dict) -> None:
    """Write run state so --once invocations share memory across cron runs."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("Could not write state file '%s': %s", STATE_FILE, exc)


# ── Token helpers ─────────────────────────────────────────────────────────────

def decode_token_expiry(token: str) -> datetime | None:
    """Return the JWT expiry as a datetime, or None if the token can't be parsed."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return datetime.fromtimestamp(payload["exp"])
    except Exception:
        return None


def check_token_validity(user: dict) -> None:
    """
    Log token expiry for a user at startup.
    Raises SystemExit if the token is already expired.
    """
    name = user.get("name", "user")
    exp = decode_token_expiry(user["bearer_token"])

    if exp is None:
        log.warning("[%s] Could not decode token expiry — proceeding anyway.", name)
        return

    remaining = exp - datetime.now()

    if remaining.total_seconds() <= 0:
        raise SystemExit(
            f"[{name}] BEARER_TOKEN expired at {exp.strftime('%Y-%m-%d %H:%M')}.\n"
            "Renew it in users.json:\n"
            "  1. Open the booking page in Chrome\n"
            "  2. F12 → Network tab → reload\n"
            "  3. Click 'date-availability' → Headers tab\n"
            "  4. Copy Authorization value (after 'Bearer ')\n"
            "  5. Paste it as bearer_token in users.json"
        )

    days_left = remaining.days
    if days_left < TOKEN_WARN_DAYS:
        log.warning("[%s] Token expires in %d day(s) on %s — renew soon!",
                    name, days_left, exp.strftime("%Y-%m-%d %H:%M"))
    else:
        log.info("[%s] Token expires: %s (%d days remaining)",
                 name, exp.strftime("%Y-%m-%d %H:%M"), days_left)


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EDL Trial Slot Notifier — monitors and pushes when slots open"
    )
    parser.add_argument(
        "--test", "-t",
        action="store_true",
        help="Check API and send a test push for every user in users.json, then exit",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check all users once and exit without looping (used by GitHub Actions)",
    )
    return parser.parse_args()


def extract_uuid(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1]


def fetch_availability(user: dict) -> list:
    """Call the EDL API for a specific user and return the list of date objects."""
    uuid = extract_uuid(user["booking_url"])
    today = date.today()
    params = {
        "type": "TRIAL",
        "from": today.isoformat(),
        "to": (today + timedelta(days=SCAN_DAYS_AHEAD)).isoformat(),
        "application_uuid": uuid,
    }
    headers = {
        "Authorization": f"Bearer {user['bearer_token']}",
        "Origin": "https://edlvrs.lumbini.gov.np",
        "Referer": "https://edlvrs.lumbini.gov.np/",
    }
    response = requests.get(EDL_API_URL, params=params, headers=headers, timeout=15)

    if response.status_code == 401:
        raise PermissionError(
            f"[{user.get('name', 'user')}] API returned 401 — Bearer token has expired."
        )

    response.raise_for_status()
    return response.json().get("data", [])


def push(user: dict, title: str, body: str,
         priority: str = "default", tags: str = "bell",
         actions: str | None = None) -> None:
    """
    Send a push notification to this user's ntfy topic.
    Uses the user's own ntfy_topic if set, otherwise the global NTFY_TOPIC from .env.
    Tapping the notification opens their booking page directly.
    """
    topic = user.get("ntfy_topic", "").strip() or DEFAULT_NTFY_TOPIC
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
        "Click": user["booking_url"],
    }
    if actions:
        headers["Actions"] = actions
    resp = requests.post(
        f"{NTFY_URL}/{topic}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    log.info("[%s] Push sent → ntfy.sh/%s", user.get("name", "user"), topic)


def book_slot(user: dict, slot: dict) -> bool:
    """
    Attempt to book a single slot for the given user via the EDL booking API.
    Returns True on success (HTTP 200/201), False on any failure.

    Booking is a two-step flow (reverse-engineered from the portal JS):
      1. POST get-date-slots → fetch available time slots for the chosen date.
      2. POST trial/book     → book with the first available time slot.
    """
    uuid = extract_uuid(user["booking_url"])
    name = user.get("name", "user")
    headers = {
        "Authorization": f"Bearer {user['bearer_token']}",
        "Origin": "https://edlvrs.lumbini.gov.np",
        "Referer": "https://edlvrs.lumbini.gov.np/",
        "Content-Type": "application/json",
    }
    params = {"application_uuid": uuid}

    # Step 1: fetch available time slots for the chosen date
    try:
        slots_resp = requests.post(
            GET_DATE_SLOTS_URL,
            json={"date": slot["date"], "type": "TRIAL"},
            headers=headers,
            params=params,
            timeout=15,
        )
        slots_resp.raise_for_status()
        time_slots = slots_resp.json().get("data", [])
    except requests.RequestException as exc:
        log.error("[%s] Failed to fetch time slots for %s: %s", name, slot["date"], exc)
        return False

    if not time_slots:
        log.warning("[%s] No time slots returned for %s — skipping.", name, slot["date"])
        return False

    # Step 2: book with the first available time slot
    time_slot = time_slots[0]
    log.info("[%s] Booking %s — time slot: %s", name, slot["date"], time_slot)
    try:
        resp = requests.post(
            TRIAL_BOOK_URL,
            json={"date": slot["date"], "time": time_slot},
            headers=headers,
            params=params,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log.info("[%s] Booking SUCCESS for %s", name, slot["date"])
            return True
        log.warning("[%s] Booking failed (HTTP %d): %s",
                    name, resp.status_code, resp.text[:300])
        return False
    except requests.RequestException as exc:
        log.error("[%s] Booking request error: %s", name, exc)
        return False


# ── Core logic ────────────────────────────────────────────────────────────────

def maybe_warn_token_expiry(user: dict, token_warned: list) -> None:
    """Send a one-time push if this user's token is about to expire."""
    if token_warned:
        return
    exp = decode_token_expiry(user["bearer_token"])
    if exp is None:
        return
    remaining = exp - datetime.now()
    if 0 < remaining.total_seconds() < TOKEN_WARN_DAYS * 86400:
        try:
            push(
                user,
                title="EDL Notifier - Token expiring soon",
                body=(
                    f"Bearer token expires {exp.strftime('%Y-%m-%d')} "
                    f"({remaining.days}d left).\n"
                    "Update bearer_token in users.json to keep monitoring."
                ),
                priority="high",
                tags="warning",
            )
            token_warned.append(True)
            log.warning("[%s] Token expiry warning push sent.", user.get("name"))
        except Exception as exc:
            log.error("[%s] Could not send token warning: %s", user.get("name"), exc)


def check_and_notify(user: dict, already_notified: set,
                     token_warned: list, booked: list) -> set:
    """
    Fetch availability for one user, compare against already-notified dates,
    and push for any newly available slots.

    If the user has auto_book: true, attempts to book the earliest available
    slot before falling back to notification-only behaviour.

    On a 401, sends a STOPPED notification to that user but does NOT exit —
    other users in the list continue to be monitored.

    Returns the updated set of notified dates for this user.
    """
    name = user.get("name", "user")
    maybe_warn_token_expiry(user, token_warned)

    try:
        slots = fetch_availability(user)
    except PermissionError as exc:
        log.critical(str(exc))
        try:
            push(user,
                 title="EDL Notifier - STOPPED",
                 body="Bearer token expired. Update bearer_token in users.json to resume.",
                 priority="max", tags="no_entry")
        except Exception:
            pass
        # Skip this user but keep running for others
        return already_notified
    except requests.RequestException as exc:
        log.error("[%s] API request failed: %s", name, exc)
        return already_notified

    bookable = [s for s in slots if s.get("is_bookable") and s.get("available", 0) > 0]
    bookable_dates = {s["date"] for s in bookable}
    new_slots = [s for s in bookable if s["date"] not in already_notified]

    # ── Auto-booking branch ───────────────────────────────────────────────────
    if new_slots and user.get("auto_book") and not booked:
        log.info("[%s] Auto-booking enabled — attempting to book earliest slot.", name)
        for slot in sorted(new_slots, key=lambda s: s["date"]):
            if book_slot(user, slot):
                booked.append(slot["date"])
                already_notified.update(s["date"] for s in new_slots)
                already_notified &= bookable_dates
                try:
                    push(user,
                         title="EDL Trial Slot BOOKED!",
                         body=(
                             f"Successfully booked: {slot['date']} "
                             f"({slot['available']} slot(s) were available)"
                         ),
                         priority="max", tags="white_check_mark")
                except Exception as exc:
                    log.error("[%s] Booking confirmation push failed: %s", name, exc)
                return already_notified
        # All booking attempts failed — slots were taken before we could confirm
        log.warning("[%s] All booking attempts failed — slots were taken.", name)
        try:
            push(user,
                 title="EDL Slot appeared but was TAKEN",
                 body=(
                     f"Tried to book: {', '.join(s['date'] for s in new_slots)}\n"
                     "All slots were taken before booking could complete. Monitoring continues."
                 ),
                 priority="high", tags="warning")
        except Exception as exc:
            log.error("[%s] Slot-taken push failed: %s", name, exc)
        # Mark these dates as notified so the alert isn't re-sent every poll cycle.
        # Pruning below removes them if they drop out of bookable_dates entirely,
        # so they'll be retried if they genuinely reopen later.
        already_notified.update(s["date"] for s in new_slots)
        already_notified &= bookable_dates
        return already_notified

    if new_slots:
        summary = ", ".join(
            f"{s['date']} ({s['available']} slot{'s' if s['available'] != 1 else ''})"
            for s in new_slots
        )
        log.info("[%s] New slot(s) found: %s", name, summary)
        try:
            push(user,
                 title="EDL Trial Slot Available!",
                 body=f"Available dates: {summary}\nAlert repeats every 5 min until slot is booked.",
                 priority="max", tags="rotating_light",
                 actions=f"view, Book Now, {user['booking_url']}, clear=true")
        except Exception as exc:
            log.error("[%s] Push failed: %s", name, exc)
            return already_notified  # retry next cycle

        already_notified.update(s["date"] for s in new_slots)
    else:
        log.info("[%s] No new slots. (Bookable now: %s)",
                 name, ", ".join(sorted(bookable_dates)) or "none")

    # Prune dates no longer bookable so we re-notify if they reopen
    already_notified &= bookable_dates
    return already_notified


# ── Test mode ─────────────────────────────────────────────────────────────────

def run_test(users: list) -> None:
    """Verify API access and push connectivity for every user in users.json."""
    log.info("=" * 55)
    log.info("TEST MODE — %d user(s)", len(users))
    log.info("=" * 55)

    all_passed = True
    for user in users:
        name = user.get("name", "user")
        log.info("--- %s ---", name)
        log.info("  UUID     : %s", extract_uuid(user["booking_url"]))
        log.info("  ntfy     : ntfy.sh/%s", user.get("ntfy_topic", "").strip() or DEFAULT_NTFY_TOPIC)

        try:
            slots = fetch_availability(user)
            log.info("  API OK   : %d date entries", len(slots))

            bookable = [s for s in slots if s.get("is_bookable") and s.get("available", 0) > 0]
            if bookable:
                log.info("  Bookable slots:")
                for s in bookable:
                    log.info("    %s — %d available", s["date"], s["available"])
            else:
                log.info("  No bookable slots right now (expected).")

        except PermissionError as exc:
            log.error("  %s", exc)
            all_passed = False
            continue
        except requests.RequestException as exc:
            log.error("  API FAILED: %s", exc)
            all_passed = False
            continue

        try:
            push(user,
                 title="EDL Notifier - Test",
                 body=f"[{name}] Setup is working. You will be notified here when a slot opens.",
                 priority="max", tags="white_check_mark")
            log.info("  Push OK  : check ntfy app.")
        except Exception as exc:
            log.error("  Push FAILED: %s", exc)
            all_passed = False

    log.info("=" * 55)
    if all_passed:
        log.info("All users passed. Ready to deploy.")
    else:
        log.warning("Some users failed — fix the errors above before deploying.")
        raise SystemExit(1)
    log.info("=" * 55)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    users = load_users()

    # Validate all tokens at startup — fail fast before polling begins
    for user in users:
        check_token_validity(user)

    log.info("━" * 55)
    log.info("EDL Trial Slot Notifier — %d user(s)", len(users))
    for user in users:
        topic = user.get("ntfy_topic", "").strip() or DEFAULT_NTFY_TOPIC
        log.info("  %-20s ntfy.sh/%s", user.get("name", "?"), topic)
    log.info("Poll interval    : %ds", POLL_INTERVAL)
    log.info("Scan window      : %d days ahead", SCAN_DAYS_AHEAD)
    log.info("━" * 55)

    if args.test:
        run_test(users)
        return

    # Per-user state: set of already-notified dates, token-warned flag, booked flag
    state = [{"notified": set(), "token_warned": [], "booked": []} for _ in users]

    if args.once:
        persistent = _load_state()

        for i, user in enumerate(users):
            name = user.get("name", "user")
            uuid = extract_uuid(user["booking_url"])
            saved = persistent.get(uuid, {})

            # Skip users already booked in a previous run
            if saved.get("booked"):
                log.info("[%s] Already booked %s (from previous run) — skipping.",
                         name, saved["booked"])
                continue

            # For auto_book users, restore notified dates so the TAKEN alert
            # isn't re-sent on every GitHub Actions invocation.
            # Non-auto users intentionally re-notify each run, so no restore.
            if user.get("auto_book"):
                state[i]["notified"] = set(saved.get("notified", []))

            state[i]["notified"] = check_and_notify(
                user, state[i]["notified"], state[i]["token_warned"], state[i]["booked"]
            )

            persistent[uuid] = {
                "notified": sorted(state[i]["notified"]) if user.get("auto_book") else [],
                "booked":   state[i]["booked"][0] if state[i]["booked"] else None,
            }

        _save_state(persistent)
    else:
        while True:
            for i, user in enumerate(users):
                if state[i]["booked"]:
                    log.info("[%s] Already booked %s — skipping.",
                             user.get("name"), state[i]["booked"][0])
                    continue
                state[i]["notified"] = check_and_notify(
                    user, state[i]["notified"], state[i]["token_warned"], state[i]["booked"]
                )
            log.info("Next check in %ds.", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
