# EDL Trial Slot Notifier

Monitors the Nepal EDL (Electronic Driving License) portal for available trial
slots and sends push notifications via [ntfy.sh](https://ntfy.sh) when a slot
opens. Supports multiple applicants. Runs free on GitHub Actions (every 5 min).

---

## How it works

1. Every 5 minutes, GitHub Actions calls the EDL availability API for each user.
2. If a bookable slot appears, a push notification is sent to that user's ntfy
   topic with a tap link directly to their booking page.
3. A warning push is sent 2 days before a bearer token expires.
4. If a token has expired, a STOPPED push is sent and that user is skipped
   (other users keep being monitored).

---

## Repository layout

```
edl_trial_notifier/
├── trial_slot_notifier.py       # main monitoring script
├── refresh_token.py             # interactive token renewal helper
├── users.json                   # per-user config  ← gitignored (contains tokens)
├── users.json.example           # template showing all supported fields
├── .env                         # global settings  ← gitignored
├── .env.example                 # template
├── requirements.txt
└── .github/
    └── workflows/
        └── check_slots.yml      # GitHub Actions cron workflow
```

---

## Initial setup (one-time)

### 1. Python environment

```bash
cd edl_trial_notifier
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create config files

```bash
cp .env.example .env
cp users.json.example users.json
```

Edit `.env` — the `NTFY_TOPIC` here is only used as a fallback when a user
has no per-user `ntfy_topic`. If every user has their own topic, this can be
left as the example value.

### 3. Fill in users.json

Each applicant needs four fields:

```json
[
  {
    "name": "Shiva",
    "booking_url": "https://edlvrs.lumbini.gov.np/edl/<UUID>",
    "bearer_token": "eyJ...",
    "application_id": "N562101",
    "ntfy_topic": "edl-N562101"
  }
]
```

| Field | How to get it |
|---|---|
| `booking_url` | The URL you get after registering on the EDL portal |
| `bearer_token` | Obtained via `python refresh_token.py` (see below) |
| `application_id` | Your EDL application number (e.g. `N562101`) |
| `ntfy_topic` | Choose any private string — recommended: `edl-{application_id}` |

### 4. Get the initial bearer token

```bash
python refresh_token.py --all
```

Enter the application ID when prompted. An OTP is sent to the registered phone.
Enter the OTP → token is written to `users.json` automatically.

### 5. Test everything locally

```bash
python trial_slot_notifier.py --test
```

Expected output per user:
```
API OK   : 61 date entries
No bookable slots right now (expected).
Push OK  : check ntfy app.
All users passed. Ready to deploy.
```

You should also receive a test push on your phone.

### 6. Set up ntfy on your phone

1. Install the **ntfy** app ([iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
2. Tap **+** → subscribe to your topic (e.g. `edl-N562101`)
3. Allow notifications

---

## GitHub Actions setup (free 24/7 hosting)

### Push the repo

```bash
git remote add origin https://github.com/YOUR_USERNAME/edl-trial-notifier.git
git push -u origin main
```

Or create a new private repo and push in one step:

```bash
gh repo create edl-trial-notifier --private --source=. --remote=origin --push
```

### Add GitHub Secrets

```bash
# Paste entire contents of users.json as a secret
gh secret set USERS_CONFIG < users.json

# Global fallback ntfy topic (used only if a user has no ntfy_topic)
gh secret set NTFY_TOPIC --body "your-fallback-topic"
```

Or via the GitHub web UI: **Settings → Secrets and variables → Actions → New repository secret**

The workflow runs automatically every 5 minutes. No further action needed.

---

## Maintenance

### Refresh a bearer token (every ~15 days)

Tokens expire after 15 days. You will receive a push notification 2 days before
expiry as a reminder.

**Refresh one user:**
```bash
source venv/bin/activate
python refresh_token.py --user "Shiva"
```

**Refresh all users that are expired or expiring soon:**
```bash
python refresh_token.py
```

**Refresh all users regardless of status (useful for testing):**
```bash
python refresh_token.py --all
```

The script will:
1. Show token status for all users
2. Trigger OTP to the applicant's registered phone
3. Prompt you to enter the OTP
4. Update `users.json` automatically

**After refreshing, sync to GitHub Actions:**
```bash
gh secret set USERS_CONFIG < users.json
```

> **For a friend's token:** They receive the OTP on their phone.
> They tell you the 6-digit code over WhatsApp/call, then you run
> `python refresh_token.py --user "Friend Name"` and enter it.

---

### Add a new user (friend)

1. **Get their details:**
   - `booking_url` — their EDL portal URL (they can copy it from their browser)
   - `application_id` — their EDL application number (e.g. `N562101`)

2. **Add them to `users.json`:**
   ```json
   {
     "name": "Friend Name",
     "booking_url": "https://edlvrs.lumbini.gov.np/edl/<THEIR-UUID>",
     "bearer_token": "",
     "application_id": "N999999",
     "ntfy_topic": "edl-N999999"
   }
   ```

3. **Get their initial token** (they must be near their phone for the OTP):
   ```bash
   python refresh_token.py --user "Friend Name"
   ```

4. **Test their setup:**
   ```bash
   python trial_slot_notifier.py --test
   ```

5. **Sync to GitHub Actions:**
   ```bash
   gh secret set USERS_CONFIG < users.json
   ```

6. **Tell them:** Install ntfy app → subscribe to `edl-N999999`

---

### Update USERS_CONFIG after any change to users.json

Any time `users.json` changes (new user, refreshed token, removed user), run:

```bash
gh secret set USERS_CONFIG < users.json
```

---

### Remove a user

1. Delete their entry from `users.json`
2. Sync to GitHub Actions:
   ```bash
   gh secret set USERS_CONFIG < users.json
   ```

---

### Run a test push from GitHub Actions

Verifies the hosted workflow can reach the API and send push notifications:

```bash
gh workflow run check_slots.yml --repo YOUR_USERNAME/edl-trial-notifier -f mode=test
```

Or from the GitHub web UI: **Actions → EDL Trial Slot Check → Run workflow → mode: test → Run workflow**

---

### Check if slots are available right now

```bash
source venv/bin/activate
python trial_slot_notifier.py --once
```

Prints availability for all users and exits. No loop, no sleep.

---

### Run locally in continuous mode (alternative to GitHub Actions)

```bash
python trial_slot_notifier.py
```

Polls every 5 minutes (configurable via `POLL_INTERVAL_SECONDS` in `.env`).
Keep the terminal open or run it in a background process / screen session.

---

## Environment variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `NTFY_TOPIC` | — | Fallback ntfy topic for users without a per-user `ntfy_topic` |
| `POLL_INTERVAL_SECONDS` | `300` | Seconds between checks in continuous mode |
| `SCAN_DAYS_AHEAD` | `60` | How many days ahead to scan for available slots |

---

## Troubleshooting

**401 Unauthorized from the API**
: Bearer token has expired. Run `python refresh_token.py` to renew.

**No push notification received**
: Run `--test` to check. Make sure you subscribed to the correct topic in the ntfy app.

**`users.json not found`**
: Run from inside the `edl_trial_notifier/` directory, or set `USERS_FILE` env var.

**GitHub Actions not triggering**
: Check the Actions tab is enabled. The cron may be delayed up to a few minutes
  during GitHub high-load periods. Manual trigger via `workflow_dispatch` always works.

**Token refresh: OTP not received**
: The OTP is sent to the phone number registered at the time of EDL application.
  Make sure you're entering the correct `application_id`.
