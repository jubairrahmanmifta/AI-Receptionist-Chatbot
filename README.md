# 🦷 Smile Dental Care — AI Receptionist v4

## Setup

### 1. Configure credentials (choose ONE)

**Option A — `.env` / environment variables (recommended):**
```bash
cp config.example.json config.json   # optional local fallback
export OPENROUTER_API_KEY="sk-or-v1-your-key-here"
export OPENROUTER_MODEL="openrouter/auto"
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="change-me"
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
# optional: export FLASK_DEBUG=1
```
If `python-dotenv` is installed (it is in `requirements.txt`), you can put the
same lines in a local `.env` file and they are loaded automatically on startup.

**Option B — local `config.json` (gitignored, never commit real keys):**
```bash
cp config.example.json config.json
```
```json
{
  "openrouter_api_key": "sk-or-v1-your-key-here",
  "openrouter_model": "openrouter/auto",
  "admin_username": "admin",
  "admin_password": "change-me"
}
```

Environment variables take priority over `config.json`. The `/admin/config`
page still reads/writes the local `config.json` for the API key/model. Admin
credentials are stored hashed in SQLite (see below).

> ⚠️ `config.json`, `appointments.json`, `clinic.db`, `venv/`, `__pycache__/`,
> `*.pyc`, and `.env` are gitignored. Never commit real secrets.

### 2. Install
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Run
```bash
python app.py
```

On first run the app creates `clinic.db`, seeds the clinic settings, doctors,
schedules and services, seeds one admin account from your env/config, and
migrates any legacy `appointments.json` (renaming it to
`appointments.json.migrated`). The system prompt is assembled from the database
at request time by `prompt_builder.py` — nothing about the clinic is hardcoded.

## URLs
- Website  : http://localhost:5000
- Admin    : http://localhost:5000/admin  (username/password from your config or env)
- Settings : http://localhost:5000/admin/config

## Tests
```bash
python test_booking.py
```
Runs the booking pipeline against a throwaway database with a mocked AI
(normal booking, taken slot, invalid phone, non-working day, past date,
malformed marker, atomic double-booking, reset, rate limit).

## File structure
```
dental_v4/
├── app.py              ← Flask routes / booking pipeline
├── db.py               ← All SQLite access + schema/seed/migration/availability
├── prompt_builder.py   ← Builds the system prompt from the database
├── test_booking.py     ← Booking pipeline tests (mocked AI, temp DB)
├── clinic.db           ← SQLite database (gitignored, auto-created)
├── config.example.json ← Template for config.json (safe to commit)
├── config.json         ← Your real secrets (gitignored)
├── requirements.txt    ← flask + requests + python-dotenv
└── templates/
    ├── index.html          ← Main dental website + chat popup
    ├── admin_login.html    ← Admin login
    ├── admin.html          ← Appointments table
    └── admin_config.html   ← Settings page
```
