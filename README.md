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

Environment variables take priority over `config.json`. The panel never writes
`config.json`: the API key entered at **Settings** is stored in SQLite, and
admin credentials are stored as hashes in SQLite. `config.json` remains only as
a legacy fallback for the API key (see *Admin panel → Settings*).

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
- Settings  : http://localhost:5000/admin/settings
- Account   : http://localhost:5000/admin/account
- Agent     : http://localhost:5000/agent/login  (agent/owner accounts)
- Console   : http://localhost:5000/agent  (live agent console, after login)

## Admin panel

All owner pages share one responsive layout (`templates/admin_base.html`): a
dark sidebar with Appointments / Settings / Account plus links to the Agent
Console and Logout, a top bar showing who is signed in, and flash messages
rendered at the top of the content area. It is a single `{% block content %}`
per page — adding a page means one new template that extends the base and one
`nav="…"` value.

**Security on every `/admin` route**

- **Login + role.** All routes require a signed-in account with role `owner`;
  agent accounts get a hard `403` and anonymous visitors are redirected to
  `/admin/login`.
- **CSRF.** Every admin POST (login, logout, delete, settings, account) carries
  a per-session token as a hidden `csrf_token` field — or the `X-CSRF-Token`
  header for JSON — and the server rejects mismatches with `403`. Logout and
  appointment deletion are POST-only (`GET` → `405`).
- **Session cookies** are `HttpOnly` and `SameSite=Lax` always, and `Secure`
  when `COOKIE_SECURE=1` (set it behind HTTPS). Logging in clears the incoming
  session and mints a fresh CSRF token (session-fixation defence). Admin
  sessions idle out after `SESSION_IDLE_HOURS` (default 8, configurable).
- **Login throttle.** Five failed attempts per IP *or* per username within ten
  minutes blocks that key for five minutes with one generic message
  ("Too many failed attempts…"). Attempts are logged through the `logging`
  module as `clinic.admin` warnings — usernames and IPs only, never passwords.
- **Open redirect.** The `next` parameter after login is only honoured when it
  is a same-site relative path (`/admin/...`); absolute URLs, `//host`,
  backslashes and control characters fall back to `/admin`.
- **Password change on first login.** `admins.must_change_password` forces
  every page except **Account** and **Logout** to redirect there with an
  explanation until a new password is set. Seeded owners start with the flag,
  and owners still using the configured default password are flagged by an
  idempotent startup migration (`DEMO_SEED` demo agents are never affected).

**Account — `/admin/account`**

Change display name, username, and password. Password changes require the
current password, a minimum of 8 characters, and must differ from the username,
the original default password, and the current password; failures re-render
with a clear list of errors. Hashing is done with `werkzeug.security`.

**Settings — `/admin/settings`**

Grouped sections, all saved with one token-guarded POST:

- **Clinic information** — name, address, phone, opening hours (EN + BN),
  persona name, welcome messages, and extra info / FAQs as textareas, each with
  a per-field length limit enforced server-side (`maxlength` + validation).
- **AI** — `ai_model` text input with a `<datalist>` of suggestions
  (including `openrouter/auto`) plus a note that auto-routing is inconsistent
  and a fixed model is recommended for demos.
- **OpenRouter API key** — write-only. The page only ever shows `Set`/`Not set`
  and a masked tail (`••••abcd`, last four characters) together with the active
  source. The stored value is never rendered in HTML, JSON or logs. Leaving the
  field blank keeps the current key.
  Resolution order: **`OPENROUTER_API_KEY` environment variable → key saved in
  the `settings` table by this panel → `config.json` (legacy fallback)**.
  A **Test AI connection** button (POST + CSRF) makes a tiny request and reports
  success or a specific reason (invalid key, rate limit, timeout) without
  exposing the key.
- **Handoff timings** — `handoff_timeout_minutes`, `agent_online_window_seconds`,
  `idle_close_minutes`, `customer_away_seconds`, `customer_left_seconds`, each
  range-checked (`customer_left_seconds` now defaults to **300**; existing
  databases still on the old `120` are migrated automatically).
- **Preview system prompt** — BN and EN tabs showing the exact text
  `build_system_prompt(lang)` produces right now, escaped in a scrollable `<pre>`.

Settings are read from SQLite on every request, so changes reach the chatbot
immediately — no restart.

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

Timing settings (editable at `/admin/settings`): `handoff_timeout_minutes` (5),
`agent_online_window_seconds` (45), `idle_close_minutes` (30),
`customer_away_seconds` (45), `customer_left_seconds` (300). The console's
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
python test_admin.py      # 16 checks — admin panel security (unittest)
```
All three run against a throwaway database with a mocked AI (no network):

- **booking** — normal booking, taken slot, invalid phone, non-working day,
  past date, malformed marker, atomic double-booking, reset, rate limit;
- **handoff** — handoff state transitions, agent races, permissions/CSRF,
  timeouts, session isolation, callbacks, canned replies, console page;
- **admin panel** — CSRF on every admin POST, agent accounts locked out with a
  403, login throttling (per-IP and per-username) with password-free logging,
  the forced first-login password change, open-redirect protection on `next`,
  session cookie flags (`HttpOnly` / `SameSite=Lax` / `Secure` under
  `COOKIE_SECURE=1`), session regeneration on login, POST-only logout and
  delete, API-key non-exposure in HTML/JSON/prompt, password policy, settings
  validation, and the removal of `/admin/config`.

## File structure
```
dental_v4/
├── app.py              ← Flask routes / booking + handoff pipeline
├── db.py               ← All SQLite access + schema/seed/migration/state machine
├── prompt_builder.py   ← Builds the system prompt from the database
├── create_agent.py     ← Create a human-agent account
├── test_booking.py     ← Booking pipeline tests (mocked AI, temp DB)
├── test_handoff.py     ← Human-handoff tests (mocked AI, temp DB)
├── test_admin.py       ← Admin panel security tests (unittest, temp DB)
├── manual_test_widget.md ← Hands-on script for the browser widget
├── clinic.db           ← SQLite database (gitignored, auto-created)
├── config.example.json ← Template for config.json (safe to commit)
├── config.json         ← Your real secrets (gitignored)
├── requirements.txt    ← flask + requests + python-dotenv
└── templates/
    ├── index.html          ← Main dental website + chat popup
    ├── admin_base.html     ← Shared admin shell (sidebar, topbar, flash)
    ├── admin_login.html    ← Admin (owner) login
    ├── admin.html          ← Appointments table
    ├── admin_settings.html ← Settings (clinic info, AI, key, timings, preview)
    ├── admin_account.html  ← Account (profile + password change)
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

Admin (owner login + hidden `csrf_token` form field required on every POST):
| Method | Path | Notes |
| --- | --- | --- |
| GET  | `/admin/login` | Login form (issues the CSRF token) |
| POST | `/admin/login` | Sign in as owner; `next` only honours same-site paths |
| POST | `/admin/logout` | Sign out (`GET` → 405) |
| GET  | `/admin` | Appointments list |
| POST | `/admin/delete/<id>` | Delete an appointment (`GET` → 405) |
| GET/POST | `/admin/settings` | Clinic info, AI model, API key, handoff timings |
| POST | `/admin/settings/test_ai` | Tiny request to OpenRouter; reports ok/error, never the key |
| GET/POST | `/admin/account` | Display name, username, password change |

