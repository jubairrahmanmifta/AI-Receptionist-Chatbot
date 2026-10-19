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
- Website   : http://localhost:5000
- Admin     : http://localhost:5000/admin  (owner accounts only)
- Settings  : http://localhost:5000/admin/config
- Agent     : http://localhost:5000/agent/login  (agent/owner accounts)

## Human handoff (agents)

Customers can move from the AI chatbot to a live human agent. A conversation
moves through `bot → waiting → with_agent → closed`:

- The widget asks the server for a session id (`POST /session`) instead of
  inventing one; ids are unguessable and validated on every request.
- When a customer asks for a human (button or the AI's `HANDOFF_REQUEST`
  marker), the conversation is queued **only if** an agent has a fresh
  heartbeat. Otherwise the customer is offered a callback instead.
- Agents log in at `/agent/login` (same `admins` table) and use the JSON API
  under `/agent/api` (heartbeat, queue, conversation, accept/message/handback/
  close). Every agent POST needs the `X-CSRF-Token` header.
- If nobody accepts within `handoff_timeout_minutes`, the bot takes over again;
  idle `with_agent` chats close after `idle_close_minutes`. These run lazily
  from the polling endpoints — no background threads.

Create agent accounts with:
```bash
python create_agent.py            # prompts for username / display name / password
```
Set `DEMO_SEED=1` in the environment to seed two demo agents (never done
otherwise).

Timing settings (editable in `settings`): `handoff_timeout_minutes` (5),
`agent_online_window_seconds` (45), `idle_close_minutes` (30).

## Tests
```bash
python test_booking.py    # 30 checks — booking pipeline
python test_handoff.py    # 52 checks — sessions, state machine, agent API
```
Both run against a throwaway database with a mocked AI (normal booking, taken
slot, invalid phone, non-working day, past date, malformed marker, atomic
double-booking, reset, rate limit; plus handoff state transitions, agent races,
permissions/CSRF, timeouts and session isolation).

## File structure
```
dental_v4/
├── app.py              ← Flask routes / booking + handoff pipeline
├── db.py               ← All SQLite access + schema/seed/migration/state machine
├── prompt_builder.py   ← Builds the system prompt from the database
├── create_agent.py     ← Create a human-agent account
├── test_booking.py     ← Booking pipeline tests (mocked AI, temp DB)
├── test_handoff.py     ← Human-handoff tests (mocked AI, temp DB)
├── clinic.db           ← SQLite database (gitignored, auto-created)
├── config.example.json ← Template for config.json (safe to commit)
├── config.json         ← Your real secrets (gitignored)
├── requirements.txt    ← flask + requests + python-dotenv
└── templates/
    ├── index.html          ← Main dental website + chat popup
    ├── admin_login.html    ← Admin (owner) login
    ├── admin.html          ← Appointments table
    ├── admin_config.html   ← Settings page
    └── agent_login.html    ← Agent login / console placeholder
```

## API reference

Public (customer widget):
| Method | Path | Notes |
| --- | --- | --- |
| POST | `/session` | Issue a new session id + conversation |
| POST | `/chat` | Send a message; returns bot reply, booking/handoff flags |
| GET  | `/chat/poll?session_id=&after_id=` | New visible messages + status |
| POST | `/handoff` | "Talk to a human" button |
| POST | `/callback` | Leave name + phone for a callback |
| POST | `/reset` | Close the current conversation, issue a new id |

Agent (login + CSRF header required):
| Method | Path | Notes |
| --- | --- | --- |
| POST | `/agent/api/heartbeat` | `{is_online: true/false}` |
| GET  | `/agent/api/queue` | Waiting list + my active chats |
| GET  | `/agent/api/conversation/<id>?after_id=` | Full transcript |
| POST | `/agent/api/conversation/<id>/accept` | Claim a waiting chat |
| POST | `/agent/api/conversation/<id>/message` | Send a message |
| POST | `/agent/api/conversation/<id>/handback` | Return to the bot |
| POST | `/agent/api/conversation/<id>/close` | Close the conversation |

