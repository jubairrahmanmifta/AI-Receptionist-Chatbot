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
- Console   : http://localhost:5000/agent  (live agent console, after login)

## Human handoff (agents)

Customers can move from the AI chatbot to a live human agent. A conversation
moves through `bot → waiting → with_agent → closed`:

- The widget asks the server for a session id (`POST /session`) instead of
  inventing one; ids are unguessable and validated on every request. The id and
  language are kept in `sessionStorage`, so **a refresh restores the transcript**
  via `GET /chat/history`.
- When a customer asks for a human (button or the AI's `HANDOFF_REQUEST`
  marker), the conversation is queued **only if** an agent has a fresh
  heartbeat. Otherwise the customer is offered a callback instead.
- Agents log in at `/agent/login` (same `admins` table) and work from the
  **console at `/agent`**: an Online/Offline switch (with a 15 s heartbeat),
  Waiting / My chats / Callbacks tabs, the live transcript, canned replies and
  Hand-back / Close actions. It uses the JSON API under `/agent/api`; every
  agent POST needs the `X-CSRF-Token` header (read from a `<meta>` tag).
- Every customer request stamps `conversations.customer_last_seen`, so the queue
  reports `customer_away` (silent for `customer_away_seconds`); a `waiting`
  conversation whose customer disappears is closed with a "customer left"
  system message after `customer_left_seconds`.
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
`agent_online_window_seconds` (45), `idle_close_minutes` (30),
`customer_away_seconds` (45), `customer_left_seconds` (120). The console's
quick answers live in `canned_replies_en` / `canned_replies_bn` (JSON lists of
five clinic-style replies each, served by `GET /agent/api/canned`).

## Customer widget

`templates/index.html` is a single page with inline CSS/JS (no build tools).
The chat popup:

- stores `session_id` + `lang` in `sessionStorage` and restores the transcript
  from `/chat/history` on load;
- polls `/chat/poll` every ~3 s while `waiting`/`with_agent` (10 s when the tab
  is hidden), backs off on errors and shows a small "reconnecting…" note;
- shows a status banner per state and a persistent **Talk to a person** button;
- renders messages by `sender_type` (customer right; bot, agent, system distinct)
  and only ever writes text with `textContent`.

See `manual_test_widget.md` for a hands-on script (one browser + `curl` as the
agent).

## Demo walkthrough

A five-minute tour for a business owner. You need **two browser windows** (or
one normal + one private window, so the sessions stay separate):

- **Window A — customer:** http://localhost:5000
- **Window B — agent:** http://localhost:5000/agent/login

Start with demo data:

```bash
DEMO_SEED=1 python app.py     # seeds agent_riya/agentpass1 and agent_sam/agentpass2
```

Then:

1. **Bot answers a question.** In Window A, open the chat and ask
   *"What are your opening hours?"* (or *"কখন খোলা?"*). The persona answers,
   grounded in the database.
2. **Bot books an appointment.** Ask *"Book me a cleaning tomorrow at 5pm"*.
   The bot collects name + phone and confirms. Show the row in `/admin`.
3. **Customer asks for a person.** Click **Talk to a person** (or type *"I want
   to talk to a human"*). The widget shows the "waiting for an agent" banner.
4. **Agent accepts and replies.** In Window B, log in as `agent_riya` /
   `agentpass1`, flip the switch to **Online**, and the chat appears in
   **Waiting** with its wait time and language. Click it, **Accept**, and send a
   reply — or pick a **canned reply** from the dropdown. Window A updates within
   a couple of seconds.
5. **Agent hands back.** Click **Hand back to bot**; the customer is talking to
   the persona again (the transcript shows a system note).
6. **Nobody online → callback.** In Window B flip the agent **Offline**. In
   Window A ask for a person again: instead of queueing, the widget offers a
   **callback form**. Submit name + phone; then in Window B open the
   **Callbacks** tab and move it `New → Contacted → Done`.

Optional extras to show: the **unread dot** on *My chats*, the sound toggle
(off by default — it beeps on a new waiting chat), the `(N)` counter in the
browser-tab title, and the responsive layout (narrow the window: the list and
the chat become separate views with a back button).

## Tests
```bash
python test_booking.py    # 30 checks — booking pipeline
python test_handoff.py    # 90 checks — sessions, state machine, agent API, widget API
```
Both run against a throwaway database with a mocked AI (normal booking, taken
slot, invalid phone, non-working day, past date, malformed marker, atomic
double-booking, reset, rate limit; plus handoff state transitions, agent races,
permissions/CSRF, timeouts, session isolation, callbacks, canned replies and the
console page).

## File structure
```
dental_v4/
├── app.py              ← Flask routes / booking + handoff pipeline
├── db.py               ← All SQLite access + schema/seed/migration/state machine
├── prompt_builder.py   ← Builds the system prompt from the database
├── create_agent.py     ← Create a human-agent account
├── test_booking.py     ← Booking pipeline tests (mocked AI, temp DB)
├── test_handoff.py     ← Human-handoff tests (mocked AI, temp DB)
├── manual_test_widget.md ← Hands-on script for the browser widget
├── clinic.db           ← SQLite database (gitignored, auto-created)
├── config.example.json ← Template for config.json (safe to commit)
├── config.json         ← Your real secrets (gitignored)
├── requirements.txt    ← flask + requests + python-dotenv
└── templates/
    ├── index.html          ← Main dental website + chat popup
    ├── admin_login.html    ← Admin (owner) login
    ├── admin.html          ← Appointments table
    ├── admin_config.html   ← Settings page
    ├── agent_login.html    ← Agent login
    └── agent_console.html  ← Agent console (queue, transcript, callbacks)
```

## API reference

Public (customer widget):
| Method | Path | Notes |
| --- | --- | --- |
| POST | `/session` | Issue a new session id + conversation |
| POST | `/chat` | Send a message; returns bot reply, booking/handoff flags |
| GET  | `/chat/poll?session_id=&after_id=` | New visible messages + status |
| GET  | `/chat/history?session_id=` | Full transcript + status (restore after refresh) |
| POST | `/handoff` | "Talk to a human" button |
| POST | `/callback` | Leave name + phone for a callback |
| POST | `/reset` | Close the current conversation, issue a new id |

Agent (login + CSRF header required):
| Method | Path | Notes |
| --- | --- | --- |
| POST | `/agent/api/heartbeat` | `{is_online: true/false}` |
| GET  | `/agent/api/queue` | Waiting list + my active chats (`customer_away`) |
| GET  | `/agent/api/conversation/<id>?after_id=` | Transcript + linked appointments/callbacks |
| POST | `/agent/api/conversation/<id>/accept` | Claim a waiting chat |
| POST | `/agent/api/conversation/<id>/message` | Send a message |
| POST | `/agent/api/conversation/<id>/handback` | Return to the bot |
| POST | `/agent/api/conversation/<id>/close` | Close the conversation |
| GET  | `/agent/api/callbacks` | Callback requests, newest first |
| POST | `/agent/api/callbacks/<id>/status` | `{status: new\|contacted\|done}` |
| GET  | `/agent/api/canned` | Canned replies `{en: [...], bn: [...]}` |

