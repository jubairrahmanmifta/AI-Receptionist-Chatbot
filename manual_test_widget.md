# Manual test — customer widget + human handoff

A short, hands-on script for the browser widget. **No puppeteer / node test
runner** — you drive one browser window as the *customer* and use `curl` as the
*agent* (Postman works too).

Why manual? The behaviours we care about here (sessionStorage, refresh,
closing a tab, polling cadence, unread badge) only exist in a real browser, so
they are verified by hand rather than in `test_handoff.py`.

---

## 0. One-time setup

```bash
cd "/home/cyberninja/Projects/AI Receptionist Chatbot/AI-Receptionist-Chatbot"

# Demo agents (agent_riya / agentpass1, agent_sam / agentpass2) — only if you
# don't already have agent accounts.
DEMO_SEED=1 venv/bin/python3 -c "import db; db.init_db()"

# Fast timings so you don't wait 5 minutes mid-test.
venv/bin/python3 - <<'PY'
import db
db.init_db()
db.set_setting("customer_away_seconds", "45")     # flag "away" in the queue
db.set_setting("customer_left_seconds", "120")    # close a silent waiting chat
db.set_setting("handoff_timeout_minutes", "5")
db.set_setting("agent_online_window_seconds", "45")
print("timings set")
PY

# Run the app.
venv/bin/python3 app.py
```

Open <http://localhost:5000> in the **customer** window.

### Agent helpers (paste into another terminal)

`$TOKEN` is the agent's CSRF token; `cookies.txt` keeps the login session.

```bash
# Log in + grab the CSRF token
curl -s -c cookies.txt http://localhost:5000/agent/login -o /dev/null
curl -s -b cookies.txt -c cookies.txt -o /dev/null \
     -d "username=agent_riya&password=agentpass1" \
     http://localhost:5000/agent/login
TOKEN=$(curl -s -b cookies.txt http://localhost:5000/agent \
        | grep -o 'name="csrf-token" content="[^"]*"' | sed 's/.*content="//;s/"//')
echo "token: $TOKEN"

# Go online (heartbeat) — repeat every <45s to stay "online"
online() { curl -s -b cookies.txt -H "X-CSRF-Token: $TOKEN" \
           -H "Content-Type: application/json" -X POST \
           -d '{"is_online":true}' \
           http://localhost:5000/agent/api/heartbeat; echo; }

# The waiting queue
queue() { curl -s -b cookies.txt http://localhost:5000/agent/api/queue; echo; }
```

> The conversation id you need for the agent calls is the `id` shown in
> `queue()["waiting"][]` (and is printed in each step below).

---

## 1. Handoff when an agent is online

1. In the **customer** window: click **Chat & Book Now**, pick **English**, and
   send a message (e.g. *"I'd like an appointment"*).
2. In the terminal: `online` (make agent_riya available).
3. Customer window: click **Talk to a person**.
   - ✅ Banner turns amber: *"An agent will join shortly (you're number 1 …)"*.
   - ✅ The button is now disabled.
   - ✅ A line appears in the transcript that the request was queued.
4. Terminal: `queue` → note the `id` in `waiting`.
   ```bash
   CID=<the id from queue()>
   curl -s -b cookies.txt -H "X-CSRF-Token: $TOKEN" -X POST \
        http://localhost:5000/agent/api/conversation/$CID/accept; echo
   ```
5. Send an agent message:
   ```bash
   curl -s -b cookies.txt -H "X-CSRF-Token: $TOKEN" \
        -H "Content-Type: application/json" -X POST \
        -d '{"message":"Hi, this is Riya from the clinic."}' \
        http://localhost:5000/agent/api/conversation/$CID/message; echo
   ```
   - ✅ Within ~3 s the customer window shows the message on the **left**, in a
     teal bubble, labelled **Riya (demo)** + a green **Human** badge.
   - ✅ Banner turns teal: *"You're chatting with Riya (demo)"*.
   - ✅ The bot never shows its typing indicator in this state.

## 2. Agent hand-back → the bot replies again

1. Customer window: send *"Can I come Tuesday?"* → no bot reply (the agent
   owns the chat); it reaches the agent.
2. Terminal:
   ```bash
   curl -s -b cookies.txt -H "X-CSRF-Token: $TOKEN" -X POST \
        http://localhost:5000/agent/api/conversation/$CID/handback; echo
   ```
   - ✅ The customer sees the *"assistant is back"* system line (centred, muted).
   - ✅ Customer window: send *"hello again"* → Mita answers as the bot again.

## 3. Nobody online → the callback form

1. Terminal: take the agent offline.
   ```bash
   curl -s -b cookies.txt -H "X-CSRF-Token: $TOKEN" \
        -H "Content-Type: application/json" -X POST \
        -d '{"is_online":false}' \
        http://localhost:5000/agent/api/heartbeat; echo
   ```
2. Customer window: start a **new chat** (or use a fresh incognito window), pick
   English, then click **Talk to a person**.
   - ✅ No queue is created; the inline **callback form** appears
     (name, phone, optional message).
3. Submit with a bad phone (`123`) → ✅ a validation error shows inline.
4. Submit with name + `01712345678` → ✅ the form closes and a confirmation
   line appears; the clinic can see it (owner `/admin` or
   `db.list_callback_requests()`).

## 4. Refresh mid-conversation restores state

1. Get into a known state — e.g. repeat step 1 so the chat is `with_agent`
   (agent online + accepted) and has a few messages.
2. In the customer window press **F5** (reload).
   - ✅ The chat reopens automatically (a session exists).
   - ✅ The whole transcript is back, in order, with the same sender styling.
   - ✅ The status banner is still *"You're chatting with …"*.
   - ✅ `sessionStorage` still has `sdc_session_id` (DevTools → Application).

## 5. Closing a waiting customer's tab → it leaves the queue

1. Make agent_riya `online`.
2. Customer window: fresh chat → **Talk to a person** → confirm the amber
   "waiting" banner and `queue` shows the conversation.
3. **Close the customer tab** (don't press any "close" button — just close it),
   then poll the queue from the terminal every ~15 s:
   ```bash
   for i in $(seq 1 12); do online >/dev/null; date +%T; queue; sleep 15; done
   ```
   - ✅ After ~45 s the item's `customer_away` becomes `true`.
   - ✅ After ~120 s (`customer_left_seconds`) the conversation is gone from
     `waiting` — housekeeping closed it with a *"Customer left the chat"* system
     message. (The agent queue also runs housekeeping, so this fires even if the
     customer page is gone.)

## 6. Widget niceties (quick checks)

- **Polling cadence:** DevTools → Network. `chat/poll` every ~3 s while waiting /
  with an agent; ~10 s when the tab is hidden (switch tabs); it stops once the
  chat is `closed`.
- **Unread badge:** while waiting / with an agent, close the popup (✕), then have
  the agent send a message → the floating launcher gets a red badge and pulses.
  Reopen → badge clears.
- **Offline resilience:** stop the server. Within a few seconds a muted
  *"reconnecting…"* note appears and requests back off. Start the server again →
  the note disappears and polling resumes on its own.
- **Accessibility / keyboard:** the message list is `aria-live="polite"`;
  **Enter** sends, **Shift+Enter** adds a newline; buttons have labels; on a
  narrow window (<560 px) the chat is full-screen.
- **Text safety:** send a message containing `<b>hello</b>` — it must render as
  literal text (we only ever use `textContent`).

---

## Clean-up

```bash
# optional: put the default (slower) timings back
venv/bin/python3 - <<'PY'
import db
db.init_db()
db.set_setting("customer_away_seconds", "45")
db.set_setting("customer_left_seconds", "120")
print("timings reset")
PY
```
