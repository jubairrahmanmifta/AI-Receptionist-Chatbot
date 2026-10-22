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

> ⚠️ **The API key saved through the Settings page is stored in plain text in
> `clinic.db`** (the `settings` table). It is never rendered back into a page,
> but anyone with the file has the key. For anything beyond a demo, set
> `OPENROUTER_API_KEY` in the environment instead and leave the panel field
> empty — that value is preferred automatically. **`clinic.db` must never be
> shared, emailed, or committed**; treat it like a password file.

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
- Doctors   : http://localhost:5000/admin/doctors
- Services  : http://localhost:5000/admin/services
- Team      : http://localhost:5000/admin/team
- Canned    : http://localhost:5000/admin/canned
- Audit log : http://localhost:5000/admin/audit
- Settings  : http://localhost:5000/admin/settings
- Account   : http://localhost:5000/admin/account
- Agent     : http://localhost:5000/agent/login  (agent/owner accounts)
- Console   : http://localhost:5000/agent  (live agent console, after login)
- Password  : http://localhost:5000/agent/password  (agent password change)

## Admin panel

All owner pages share one responsive layout (`templates/admin_base.html`): a
dark sidebar with Appointments / Doctors / Services / Team / Canned replies /
Audit log / Settings / Account plus links to the Agent Console and Logout, a top
bar showing who is signed in, and flash messages rendered at the top of the
content area. It is a single `{% block content %}` per page — adding a page means
one new template that extends the base and one `nav="…"` value.

**Security on every `/admin` route**

- **Login + role.** All routes require a signed-in account with role `owner`;
  agent accounts get a hard `403` and anonymous visitors are redirected to
  `/admin/login`.
- **CSRF.** Every admin POST (login, logout, delete, settings, account, team,
  canned replies, confirmation) carries a per-session token as a hidden
  `csrf_token` field — or the `X-CSRF-Token` header for JSON — and the server
  rejects mismatches with `403`. Logout and appointment deletion are POST-only
  (`GET` → `405`).
- **Session cookies** are `HttpOnly` and `SameSite=Lax` always, and `Secure`
  when `COOKIE_SECURE=1` (set it behind HTTPS). Logging in clears the incoming
  session and mints a fresh CSRF token (session-fixation defence). Admin
  sessions idle out after `SESSION_IDLE_HOURS` (default 8, configurable).
- **Session invalidation.** Every login stores the account's
  `admins.session_version`. On every `/admin/*` request (and every
  `/agent/api/*` request) the account is re-read: deleted, deactivated
  (`admins.active = 0`) or version-mismatched accounts are signed out
  immediately. A Team-page **Reset password** bumps the version, so every
  existing session for that account ends; deactivation also bumps it.
- **Audit trail.** Team changes, password changes, canned-reply saves and
  failed/blocked logins are written to `admin_audit_log` with the actor, target,
  IP and timestamp. Passwords, temporary passwords, hashes and API keys are
  never written to the audit log or the application log.
- **Login throttle.** Five failed attempts for the same **(IP, username) pair**
  within ten minutes blocks that pair for five minutes, and a separate looser
  cap of twenty failures per IP (across every username) protects against one
  address spraying many names. A block always shows the same generic message
  ("Too many failed attempts…"), and a username attacked from one address can
  still sign in from another. Attempts are logged through the `logging` module
  as `clinic.admin` warnings — usernames and IPs only, never passwords.
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

**Team — `/admin/team`**

Create owner and agent accounts without touching the database. When a user is
created, a random 16-character temporary password is generated with
`secrets.token_urlsafe` and shown **once** on the page; only its hash is stored,
and the new account must change it at first sign-in.

- **Edit name / change role.** Display names are 1–40 characters with no HTML,
  URLs or control characters. Owners can promote or demote accounts.
- **Reset password.** Generates a fresh one-time temporary password, forces a
  change at next sign-in and bumps `session_version`, ending any sessions the
  user already had.
- **Deactivate / reactivate.** Accounts are **never deleted**, so chat and audit
  history keeps a valid actor. A deactivated account cannot sign in, and any
  signed-in session is rejected on its next request.
- **Force offline.** Clears the agent's online flag without ending their login.
- **Guards.** You cannot deactivate, demote or force-offline your own account,
  and the last active owner can never be deactivated or demoted.
- **Active chats.** Deactivating an agent who is currently chatting shows a
  confirmation page first. On confirm, one transaction hands every active chat
  back to the bot (status `bot`, `agent_id` cleared, a “The assistant is back…”
  system message), clears the online flag, bumps `session_version` and sets
  `active = 0`. Nothing changes if you go back.

**Canned replies — `/admin/canned`**

The quick replies offered in the agent console's “Canned replies…” menu. Add,
edit, delete and reorder (up/down) rows for English and Bengali in vanilla JS,
then save with one token-guarded POST. Limits: at most **20 per language**,
each reply **1–300 characters after trimming**, no case-insensitive duplicates,
valid JSON, and control characters are stripped. Each row shows a live preview
with the `{agent_name}` and `{clinic_name}` placeholders filled in for the
signed-in owner and the active language. Stored text is only rendered with
`textContent`/input values — never as HTML.

**Audit log — `/admin/audit`**

A read-only, newest-first view of the latest 200 entries from
`admin_audit_log`. Logged actions include team creation, name/role changes,
deactivations and reactivations, password resets, force-offline, password
changes, canned-reply saves, and failed/blocked logins (username only). Each row
shows the time, actor, action, target, detail and IP. Passwords, temporary
passwords, password hashes and API keys are never logged.

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
immediately — no restart. The **Doctors** and **Services** pages link back to
this preview ("View what the bot sees").

**Doctors — `/admin/doctors`**


Each doctor card lists the EN/BN name, specialty, an active badge, slot length,
a compact weekly-hours summary, upcoming appointments, and how many days off are
scheduled. Actions are Edit, Enable/Disable and Delete.

- **Add/Edit** — name EN/BN (at least one required, max 100), specialty EN/BN
  (max 120), bio EN/BN (max 600), `slot_minutes` (10–120, multiple of 5), and an
  active toggle. An active doctor must have at least one weekly range.
- **Weekly schedule editor** — one row per weekday (Mon–Sun) with small vanilla
  JS **Add range** / **Remove** buttons. The server checks `HH:MM` formatting,
  `start < end`, no overlap within a weekday, and that each range fits at least
  one slot. Saving replaces the schedule **atomically** — a rejected save leaves
  the previous schedule untouched.
- **Impact warning** — if a save would change the schedule, slot length or
  active flag in a way that affects **upcoming, non-cancelled** appointments
  (time no longer inside the schedule or off a slot boundary, or the doctor is
  being disabled), nothing is applied. A confirmation page lists the affected
  appointments (patient, phone, date, time, reason); the owner either confirms
  (appointments stay exactly as they are) or goes back. **Nothing is ever
  auto-cancelled.**
- **Delete** — only allowed when the doctor has **no appointments at all**;
  otherwise it is blocked with a message suggesting **Disable**. Disabled doctors
  keep their history everywhere but vanish from the chatbot prompt and from slot
  generation.

**Doctor days off**

On each doctor's edit page you can list upcoming time off, add a range (each
range: `date_from <= date_to`, not in the past, at most 90 days long, reason
optional max 200), and remove a range. Adding a range that overlaps an upcoming
appointment shows the same confirmation page. Days off:

- remove those dates from `get_available_slots()` and `get_next_available_slots()`,
- stop the availability section of the prompt from offering them,
- make the booking validator reject them with a clear reason ("the doctor is on
  leave") so the bot can offer alternatives,
- and appear in a short "Upcoming days off (next 30 days)" line in the prompt
  (e.g. *"Dr. X is on leave 25 Oct–27 Oct"*) so the bot can explain why.

**Services — `/admin/services`**

Each service card shows the EN/BN name, the price range via `format_price`, the
duration, an active badge, and how many appointments use it. Actions are Edit,
Enable/Disable and Delete. Validation: at least one name (max 100), `price >= 0`,
`price_max` empty or `>= price`, duration 5–480 (multiple of 5), and sane numeric
bounds. Delete is blocked while any appointment references the service (use
Disable); disabled services disappear from the prompt.

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
- **Forced password change.** Accounts created from the Team page (or
  `create_agent.py`) start with `must_change_password = 1`. Until the agent
  chooses a new password at `/agent/password`, `/agent` redirects there and
  every `/agent/api/*` call returns `403 {"error": "must_change_password"}`.
  The page requires the current password, a new password of at least 8
  characters that differs from the username (and the current password and the
  configured default), plus the session CSRF token. A “Change password” link is
  always available in the console top bar.
- **Session invalidation.** Agent sessions store `session_version` at login.
  A Team-page password reset, deactivation or deletion ends the session
  immediately: `/agent` redirects to the login page and `/agent/api/*` returns
  `401 {"error": "unauthorized"}`.
- Every customer request stamps `conversations.customer_last_seen`, so the queue
  reports `customer_away` (silent for `customer_away_seconds`); a `waiting`
  conversation whose customer disappears is closed with a "customer left"
  system message after `customer_left_seconds`.
- If nobody accepts within `handoff_timeout_minutes`, the bot takes over again;
  idle `with_agent` chats close after `idle_close_minutes`. These run lazily
  from the polling endpoints — no background threads.

Create agent accounts at **`/admin/team`** (recommended: one-time temporary
password, forced change, reset/deactivate controls), or from a terminal with:
```bash
python create_agent.py            # prompts for username / display name / password
```
Set `DEMO_SEED=1` in the environment to seed two demo agents (never done
otherwise).

Timing settings (editable at `/admin/settings`): `handoff_timeout_minutes` (5),
`agent_online_window_seconds` (45), `idle_close_minutes` (30),
`customer_away_seconds` (45), `customer_left_seconds` (300). The console's
quick answers are managed at `/admin/canned` (stored as `canned_replies_en` /
`canned_replies_bn` JSON lists) and served by `GET /agent/api/canned`, which
substitutes `{agent_name}` and `{clinic_name}` per language and leaves any other
braces untouched.

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

### Demo: doctors, days off and immediate effect

1. **Disable Dr. Karim.** In `/admin/doctors`, click **Disable** next to
   *Dr. Karim*. Ask the chatbot to book him — it must **refuse** and offer
   *Dr. Rahela Khanam* instead. Then click **Enable** to bring him back.
2. **Add a day off for Dr. Rahela.** Open her edit page, add a days-off range
   covering one of her working days, and ask the chatbot for that day — it must
   explain she is **on leave** and offer other days.
3. **Nothing needs a restart.** Every one of these changes is read from SQLite
   on the next message; use *View what the bot sees* on the doctors/services
   page to read the exact prompt the bot is using.

## Tests
```bash
python test_booking.py        # 30 checks — booking pipeline
python test_handoff.py        # 90 checks — sessions, state machine, agent API, widget API
python test_admin.py          # 18 tests  — admin panel security (unittest)
python test_admin_catalog.py  # 23 tests  — doctors, days off, services (unittest)
python test_admin_team.py     # 29 tests  — team, audit, canned replies, passwords (unittest)
```
All five run against a throwaway database with a mocked AI (no network):

- **booking** — normal booking, taken slot, invalid phone, non-working day,
  past date, malformed marker, atomic double-booking, reset, rate limit;
- **handoff** — handoff state transitions, agent races, permissions/CSRF,
  timeouts, session isolation, callbacks, canned replies, console page;
- **admin panel** — CSRF on every admin POST, agent accounts locked out with a
  403, login throttling keyed on the **(IP, username) pair** plus the per-IP cap
  with password-free logging, the forced first-login password change,
  open-redirect protection on `next`, session cookie flags (`HttpOnly` /
  `SameSite=Lax` / `Secure` under `COOKIE_SECURE=1`), session regeneration on
  login, POST-only logout and delete, API-key non-exposure in HTML/JSON/prompt,
  password policy, settings validation, and the removal of `/admin/config`;
- **admin catalog** — CSRF on every new POST, agent 403 wall, doctor/services
  field validation, inverted/overlapping/too-short schedule ranges, **atomic**
  schedule saves (a rejected save keeps the old schedule), the impact-warning
  flow (nothing changes until confirmed), disabling a doctor removing them from
  the prompt and from slot generation, days off removing dates from slots and
  making the booking validator explain why, delete blocked while appointments
  exist, time-off and price/duration validation, and multi-range slot
  generation;
- **admin team** — CSRF on every new POST and the agent 403 wall on every admin
  route; account creation with a one-time temporary password; display-name,
  role, reset-password, deactivate/reactivate and force-offline flows; the
  self-service and last-owner guards; no hard-delete route; the confirmation
  flow that returns active chats to the bot in one transaction; session
  invalidation on version bump, deactivation and deletion for both `/admin` and
  `/agent/api`; the forced agent password change and its API gate; canned-reply
  limits, trimming, duplicate/control-character validation and escaping; the
  `{agent_name}`/`{clinic_name}` substitutions per language; and the audit log
  (latest 200, secret-free details, failed/blocked logins).

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
├── test_admin_catalog.py ← Doctors/services/days-off tests (unittest, temp DB)
├── test_admin_team.py  ← Team/audit/canned/password tests (unittest, temp DB)
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
    ├── admin_doctors.html  ← Doctors list (schedule summary, badges)
    ├── admin_doctor_edit.html ← Doctor form, schedule editor, days off
    ├── admin_services.html ← Services list (price, duration, badges)
    ├── admin_service_edit.html ← Service form
    ├── admin_team.html     ← Team list, create form, one-time temp password
    ├── admin_confirm_team.html ← Confirm deactivation while chats are active
    ├── admin_canned.html   ← Canned-reply editor (EN/BN, inline JS)
    ├── admin_audit.html    ← Read-only audit log (latest 200)
    ├── admin_confirm_impact.html ← Warning page for affected appointments
    ├── admin_settings.html ← Settings (clinic info, AI, key, timings, preview)
    ├── admin_account.html  ← Account (profile + password change)
    ├── agent_login.html    ← Agent login
    ├── agent_password.html ← Agent password change (also clears the forced flag)
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
| GET  | `/agent/api/canned` | Canned replies with `{agent_name}`/`{clinic_name}` substituted |

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
| GET  | `/admin/team` | Team list, create form, status/actions |
| POST | `/admin/team/new` | Create an account; returns a one-time temporary password |
| POST | `/admin/team/<id>/name` | Change a display name |
| POST | `/admin/team/<id>/role` | Change role (`agent`/`owner`) |
| POST | `/admin/team/<id>/toggle` | Deactivate/reactivate (confirmation if chats are active) |
| POST | `/admin/team/<id>/reset-password` | New temporary password + end all sessions |
| POST | `/admin/team/<id>/force-offline` | Clear the agent's online flag |
| GET  | `/admin/canned` | Canned-reply editor (EN/BN) |
| POST | `/admin/canned` | Save both language lists as one JSON POST |
| GET  | `/admin/audit` | Latest 200 audit entries (read-only) |
| GET  | `/admin/doctors` | Doctors list (schedule summary, upcoming counts, days off) |
| GET/POST | `/admin/doctors/new` | Create a doctor (+ weekly schedule) |
| GET/POST | `/admin/doctors/<id>` | Edit a doctor, schedule, and add days off |
| POST | `/admin/doctors/<id>/toggle` | Enable/disable (impact-confirmed if it affects bookings) |
| POST | `/admin/doctors/<id>/delete` | Delete (blocked while appointments exist) |
| POST | `/admin/doctors/<id>/time-off/<tid>/delete` | Remove a days-off range |
| GET  | `/admin/services` | Services list (price, duration, usage) |
| GET/POST | `/admin/services/new` | Create a service |
| GET/POST | `/admin/services/<id>` | Edit a service |
| POST | `/admin/services/<id>/toggle` | Enable/disable |
| POST | `/admin/services/<id>/delete` | Delete (blocked while appointments use it) |
| POST | `/admin/confirm-impact` | Apply a doctor/schedule/days-off/deactivation change the owner confirmed |

