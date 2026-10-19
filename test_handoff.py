"""End-to-end tests for the human-handoff pipeline (Prompt 3A).

Runs against a throwaway SQLite database (the real ``clinic.db`` is untouched)
and mocks the AI so no network access is needed.

    python test_handoff.py
"""

import os
import sys
import json
import datetime
import tempfile

# Skip app's startup DB init, then point the db layer at a temp database.
os.environ["SKIP_DB_INIT"] = "1"

import db  # noqa: E402

_TMPDIR = tempfile.mkdtemp(prefix="clinic_handoff_")
db.DB_FILE = os.path.join(_TMPDIR, "test_clinic.db")
db.init_db()

import app  # noqa: E402

results = []


def check(label, condition):
    results.append((label, bool(condition)))
    print(("PASS" if condition else "FAIL"), "-", label)


# ─── Mock the AI ──────────────────────────────────────────────────────────────
state = {"reply": "Hello! How can I help?", "calls": 0}


def fake_chat(messages, lang="bn"):
    state["calls"] += 1
    return state["reply"]


app._openrouter_chat = fake_chat

# Keep the online window generous so agents stay "online" during the test run.
db.set_setting("agent_online_window_seconds", "600")
db.set_setting("handoff_timeout_minutes", "5")
db.set_setting("idle_close_minutes", "30")

client = app.app.test_client()


# ─── Helpers ──────────────────────────────────────────────────────────────────
def new_session(cli=None):
    cli = cli or client
    resp = cli.post("/session", json={"lang": "en"})
    return resp.get_json()["session_id"]


def post_chat(message, sid, cli=None, lang="en"):
    cli = cli or client
    return cli.post("/chat", json={"session_id": sid, "message": message, "lang": lang})


def ensure_agent(username, display="Agent"):
    existing = db.get_admin(username)
    if existing:
        return existing["id"]
    return db.create_admin(username, "agentpw", display_name=display, role="agent")


def go_online(agent_id, online=True):
    db.heartbeat(agent_id, online)


def agent_client(username, password="agentpw"):
    """Log a fresh client in as an agent and return (client, csrf_token)."""
    cli = app.app.test_client()
    cli.post("/agent/login", data={"username": username, "password": password})
    with cli.session_transaction() as sess:
        token = sess.get("csrf_token")
    return cli, token


def make_waiting(agent_id):
    """Make ``agent_id`` online and queue a fresh conversation. Returns (sid, cid)."""
    go_online(agent_id, True)
    sid = new_session()
    conv = db.get_or_create_conversation(sid, "en")
    db.request_handoff(conv["id"], "test")
    return sid, conv["id"]


def set_times(cid, **fields):
    """Directly override timestamps on a conversation (for time-based tests)."""
    assignments = ", ".join("%s = ?" % k for k in fields)
    with db._connect() as conn:
        conn.execute("UPDATE conversations SET %s WHERE id = ?" % assignments,
                     (*fields.values(), cid))


def conv_status(cid):
    return db.get_conversation(cid)["status"]


print("=" * 70)
print("Human-handoff pipeline tests")
print("=" * 70)

# 1. Session issuing + strict validation --------------------------------------
sid = new_session()
check("POST /session returns a valid id", app._valid_session_id(sid))
check("session id is long/unguessable", len(sid) >= 22)
check("issued session exists in db", db.session_exists(sid))

r = client.post("/chat", json={"session_id": "default", "message": "hi", "lang": "en"})
check("malformed/legacy session rejected (400)", r.status_code == 400)
r = client.post("/chat", json={"session_id": "A" * 30, "message": "hi", "lang": "en"})
check("unknown session rejected (400)", r.status_code == 400)

# 2. Nobody online -> not queued + callback offered ---------------------------
agent_a = ensure_agent("agent_a", "Aisha")
agent_b = ensure_agent("agent_b", "Bilal")
go_online(agent_a, False)
go_online(agent_b, False)

sid = new_session()
r = client.post("/handoff", json={"session_id": sid, "lang": "en"})
body = r.get_json()
check("no agent online -> not queued", body["queued"] is False)
check("no agent online -> callback offered", body["callback_offered"] is True)

# 3. Online agent -> queued with a position -----------------------------------
sid, cid = make_waiting(agent_a)
check("online agent -> conversation is waiting", conv_status(cid) == "waiting")
r = client.post("/handoff", json={"session_id": sid, "lang": "en"})
check("second handoff request stays queued (no duplicate)", r.get_json()["queued"] is True)
with db._connect() as conn:
    waiting = conn.execute("SELECT COUNT(*) n FROM conversations WHERE status='waiting'"
                           ).fetchone()["n"]
check("exactly one waiting entry for the session", waiting == 1)

# 4. Queue positions are ordered ----------------------------------------------
positions = []
for i in range(3):
    s, c = make_waiting(agent_a)
    set_times(c, handoff_requested_at=(db.dhaka_now()
              + datetime.timedelta(seconds=i)).isoformat())
    positions.append(db.queue_position(c))
check("queue positions increase", positions == sorted(positions)
      and len(set(positions)) == 3)

# 5. /chat does NOT call the AI while with_agent ------------------------------
sid, cid = make_waiting(agent_a)
check("accept succeeded", db.accept_conversation(cid, agent_a) is True)
check("conversation now with_agent", conv_status(cid) == "with_agent")
state["calls"] = 0
r = post_chat("Are you there?", sid)
body = r.get_json()
check("with_agent /chat returns agent mode", body.get("mode") == "agent")
check("with_agent /chat does NOT call the AI", state["calls"] == 0)

# 6. The bot keeps answering while waiting ------------------------------------
sid, cid = make_waiting(agent_a)  # waiting (no accept)
state["reply"] = "Sure, I can still help."
state["calls"] = 0
r = post_chat("still there?", sid)
body = r.get_json()
check("waiting /chat keeps answering (bot mode)", body.get("mode") == "bot")
check("waiting /chat calls the AI", state["calls"] >= 1)

# 7. HANDOFF_REQUEST is always stripped ---------------------------------------
state["reply"] = ('Of course, connecting you now.\n'
                  'HANDOFF_REQUEST:{"reason":"asked for a human"}')
r = post_chat("I want a human", sid)
body = r.get_json()
check("valid HANDOFF_REQUEST stripped", "HANDOFF_REQUEST" not in body["reply"])

state["reply"] = "Hello!\nHANDOFF_REQUEST:{this is not valid json"
r = post_chat("hello", sid)
body = r.get_json()
check("malformed HANDOFF_REQUEST stripped", "HANDOFF_REQUEST" not in body["reply"])
check("malformed HANDOFF_REQUEST does not crash (200)", r.status_code == 200)

# 8. Two agents racing to accept -> exactly one wins (db layer) ---------------
sid, cid = make_waiting(agent_a)
first = db.accept_conversation(cid, agent_a)
second = db.accept_conversation(cid, agent_b)
check("db race: exactly one accept wins", (first, second) == (True, False))
check("db race: winner is stored", db.get_conversation(cid)["agent_id"] == agent_a)

# 9. Two agents racing to accept -> exactly one wins (API layer) --------------
sid, cid = make_waiting(agent_a)
cli_a, tok_a = agent_client("agent_a")
cli_b, tok_b = agent_client("agent_b")
r1 = cli_a.post("/agent/api/conversation/%d/accept" % cid,
                headers={"X-CSRF-Token": tok_a})
r2 = cli_b.post("/agent/api/conversation/%d/accept" % cid,
                headers={"X-CSRF-Token": tok_b})
check("api race: first accept 200", r1.status_code == 200)
check("api race: second accept 409", r2.status_code == 409)

# 10. Agent API requires login + CSRF -----------------------------------------
anon = app.app.test_client()
r = anon.get("/agent/api/queue")
check("agent API without login -> 401", r.status_code == 401)
r = anon.post("/agent/api/conversation/%d/accept" % cid)
check("agent POST without login -> 401", r.status_code == 401)
r = cli_a.post("/agent/api/conversation/%d/handback" % cid)  # logged in, no CSRF
check("agent POST without CSRF -> 403", r.status_code == 403)

# 11. Agent cannot read another agent's active conversation -------------------
sid, cid = make_waiting(agent_a)
db.accept_conversation(cid, agent_a)
r = cli_b.get("/agent/api/conversation/%d" % cid)
check("agent B cannot read agent A's active chat -> 403", r.status_code == 403)
r = cli_a.get("/agent/api/conversation/%d" % cid)
check("assigned agent can read own chat -> 200", r.status_code == 200)

# 12. Agents get 403 on /admin -----------------------------------------------
r = cli_a.get("/admin")
check("agent account gets 403 on /admin", r.status_code == 403)

# 13. Owner can use /admin, agent cannot log into /admin ----------------------
if not db.get_admin("owner"):
    db.create_admin("owner", "ownerpw", display_name="Owner", role="owner")
owner_cli = app.app.test_client()
r = owner_cli.post("/admin/login", data={"username": "owner", "password": "ownerpw"})
check("owner login redirects", r.status_code in (301, 302))
r = owner_cli.get("/admin")
check("owner can open /admin", r.status_code == 200)
bad = app.app.test_client().post("/admin/login",
                                 data={"username": "agent_a", "password": "agentpw"})
check("agent credentials rejected at /admin/login",
      bad.status_code == 200 and b"agent account" in bad.data.lower())

# 14. Agent messaging + handback, then the bot answers again ------------------
sid, cid = make_waiting(agent_a)
cli_a, tok_a = agent_client("agent_a")
cli_a.post("/agent/api/conversation/%d/accept" % cid, headers={"X-CSRF-Token": tok_a})
r = cli_a.post("/agent/api/conversation/%d/message" % cid,
               json={"message": "Hi, this is Aisha from the clinic."},
               headers={"X-CSRF-Token": tok_a})
check("agent can send a message", r.status_code == 200)
r = client.get("/chat/poll", query_string={"session_id": sid, "after_id": 0})
msgs = r.get_json()["messages"]
check("customer sees the agent message via poll",
      any(m["sender_type"] == "agent" and "Aisha" in m["content"] for m in msgs))
check("poll reports with_agent + agent name",
      r.get_json()["status"] == "with_agent" and r.get_json()["agent_name"] == "Aisha")

r = cli_a.post("/agent/api/conversation/%d/handback" % cid, headers={"X-CSRF-Token": tok_a})
check("agent can hand back to the bot", r.status_code == 200 and r.get_json()["ok"])
check("conversation back to bot", conv_status(cid) == "bot")
state["calls"] = 0
r = post_chat("back to you", sid)
check("bot answers again after handback", r.get_json().get("mode") == "bot" and state["calls"] >= 1)

# 15. Waiting timeout via lazy housekeeping -----------------------------------
sid, cid = make_waiting(agent_a)
set_times(cid, handoff_requested_at=(db.dhaka_now()
          - datetime.timedelta(minutes=30)).isoformat())
db.run_housekeeping()
check("timed-out waiting -> bot", conv_status(cid) == "bot")
with db._connect() as conn:
    sysmsg = conn.execute(
        "SELECT content FROM chat_messages WHERE conversation_id=? AND sender_type='system' "
        "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
check("timeout stores a system message offering a callback",
      sysmsg is not None and "callback" in sysmsg["content"].lower())

# 16. Idle close via lazy housekeeping ----------------------------------------
sid, cid = make_waiting(agent_a)
db.accept_conversation(cid, agent_a)
set_times(cid, last_message_at=(db.dhaka_now()
          - datetime.timedelta(minutes=60)).isoformat())
db.run_housekeeping()
check("idle with_agent -> closed", conv_status(cid) == "closed")

# 17. /reset issues a new id and closes the old conversation ------------------
sid = new_session()
conv = db.get_or_create_conversation(sid, "en")
db.add_message(conv["id"], "customer", "hi")
r = client.post("/reset", json={"session_id": sid, "lang": "en"})
new_sid = r.get_json()["session_id"]
check("/reset returns a new id", app._valid_session_id(new_sid) and new_sid != sid)
check("/reset closes the previous conversation",
      db.get_conversation(conv["id"])["status"] == "closed")
check("/reset leaves the old session with no open conversation",
      db.get_current_conversation(sid) is None)

# 18. Customer isolation: cannot poll another customer's session --------------
sid_a = new_session()
sid_b = new_session()
state["reply"] = "SECRET-A"
post_chat("hello", sid_a)
r = client.get("/chat/poll", query_string={"session_id": sid_b, "after_id": 0})
contents = [m["content"] for m in r.get_json()["messages"]]
check("session B does not see session A's messages", "SECRET-A" not in contents)
r = client.get("/chat/poll", query_string={"session_id": "Z" * 30, "after_id": 0})
check("poll with unknown session -> 400", r.status_code == 400)

# 19. Callback endpoint validation --------------------------------------------
sid = new_session()
r = client.post("/callback", json={"session_id": sid, "name": "Rahim",
                                   "phone": "123", "message": "call me"})
check("callback rejects a bad phone (400)", r.status_code == 400)
r = client.post("/callback", json={"session_id": sid, "name": "Rahim",
                                   "phone": "01712345678", "message": "call me"})
check("callback accepts a valid phone (200)", r.status_code == 200)
check("callback row stored", len(db.list_callback_requests()) >= 1)

# 20. Conversation history for the AI excludes system messages ----------------
sid, cid = make_waiting(agent_a)
db.accept_conversation(cid, agent_a)          # stores a "<name> joined" system msg
with db._connect() as conn:
    has_system = conn.execute(
        "SELECT COUNT(*) n FROM chat_messages WHERE conversation_id=? AND sender_type='system'",
        (cid,)).fetchone()["n"] >= 1
history = db.get_conversation_history(cid, limit=50)
check("transcript contains a system message", has_system)
check("AI history excludes system messages",
      all(m["role"] in ("user", "assistant") for m in history))

# 21. customer_last_seen is stamped on /chat and /chat/poll (A1) ---------------
sid = new_session()
conv = db.get_or_create_conversation(sid, "en")
set_times(conv["id"], customer_last_seen=None)
post_chat("hello", sid)
check("A1: /chat stamps customer_last_seen",
      bool(db.get_conversation(conv["id"])["customer_last_seen"]))
set_times(conv["id"], customer_last_seen=None)
client.get("/chat/poll", query_string={"session_id": sid, "after_id": 0})
check("A1: /chat/poll stamps customer_last_seen",
      bool(db.get_conversation(conv["id"])["customer_last_seen"]))

# 22. A silent waiting customer is closed as "customer left" (A2) -------------
db.set_setting("customer_left_seconds", "120")
sid, cid = make_waiting(agent_a)
now = db.dhaka_now()
set_times(cid, handoff_requested_at=now.isoformat(),
          customer_last_seen=(now - datetime.timedelta(seconds=200)).isoformat())
db.run_housekeeping()
check("A2: silent waiting customer -> closed", conv_status(cid) == "closed")
with db._connect() as conn:
    left_msg = conn.execute(
        "SELECT content FROM chat_messages WHERE conversation_id=? "
        "AND sender_type='system' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
check("A2: stores a 'customer left' system message",
      left_msg is not None and "customer left" in left_msg["content"].lower())

# A waiting customer who is still present is left alone.
sid, cid = make_waiting(agent_a)
set_times(cid, handoff_requested_at=db.dhaka_now().isoformat(),
          customer_last_seen=db.dhaka_now().isoformat())
db.run_housekeeping()
check("A2: present waiting customer stays waiting", conv_status(cid) == "waiting")

# 23. Queue reports customer_away for waiting and with_agent chats (A2) -------
db.set_setting("customer_away_seconds", "45")
cli_q, _tok = agent_client("agent_a")

sid_away, cid_away = make_waiting(agent_a)
now = db.dhaka_now()
set_times(cid_away, handoff_requested_at=now.isoformat(),
          customer_last_seen=(now - datetime.timedelta(seconds=90)).isoformat())
q = cli_q.get("/agent/api/queue").get_json()
item = next((w for w in q["waiting"] if w["id"] == cid_away), None)
check("A2: stale waiting customer flagged customer_away",
      item is not None and item.get("customer_away") is True)

sid_here, cid_here = make_waiting(agent_a)
set_times(cid_here, handoff_requested_at=db.dhaka_now().isoformat(),
          customer_last_seen=db.dhaka_now().isoformat())
q = cli_q.get("/agent/api/queue").get_json()
here = next((w for w in q["waiting"] if w["id"] == cid_here), None)
check("A2: present waiting customer not flagged away",
      here is not None and here.get("customer_away") is False)

# A with_agent chat also carries the flag.
sid_mine, cid_mine = make_waiting(agent_a)
db.accept_conversation(cid_mine, agent_a)
now = db.dhaka_now()
set_times(cid_mine, last_message_at=now.isoformat(),
          customer_last_seen=(now - datetime.timedelta(seconds=90)).isoformat())
q = cli_q.get("/agent/api/queue").get_json()
mine = next((c for c in q["mine"] if c["id"] == cid_mine), None)
check("A2: with_agent chat also reports customer_away",
      mine is not None and mine.get("customer_away") is True)

# 24. GET /agent/api/queue runs housekeeping (A2) -----------------------------
sid, cid = make_waiting(agent_a)
now = db.dhaka_now()
set_times(cid, handoff_requested_at=now.isoformat(),
          customer_last_seen=(now - datetime.timedelta(seconds=200)).isoformat())
cli_q.get("/agent/api/queue")
check("A2: queue endpoint triggers housekeeping", conv_status(cid) == "closed")

# 25. /chat/history restores the customer-visible transcript (A3) -------------
sid = new_session()
state["reply"] = "Sure, happy to help."
post_chat("Hi there", sid)
conv = db.get_or_create_conversation(sid, "en")
db.add_message(conv["id"], "agent", "Hello, this is Aisha.", agent_id=agent_a)
r = client.get("/chat/history", query_string={"session_id": sid})
body = r.get_json()
check("A3: /chat/history returns 200", r.status_code == 200)
types = {m["sender_type"] for m in body.get("messages", [])}
check("A3: history includes customer + bot + agent messages",
      {"customer", "bot", "agent"} <= types)
check("A3: history carries the agent display name",
      any(m["sender_type"] == "agent" and m["agent_name"] == "Aisha"
          for m in body["messages"]))
check("A3: history reports the current status", body.get("status") == "bot")
check("A3: history has a last_id", isinstance(body.get("last_id"), int))
r = client.get("/chat/history", query_string={"session_id": "Z" * 30})
check("A3: history rejects an unknown session (400)", r.status_code == 400)

# 26. Agent conversation detail links appointments + callbacks (A4) -----------
sid, cid = make_waiting(agent_a)
doctor = db.list_doctors(active_only=True)[0]
days = db.get_next_available_slots(doctor["id"], days=21)
slot_date, slot_time = days[0]["date"], days[0]["slots"][0]
appt_id = db.book_appointment("Linked Patient", "01712345678", doctor["id"],
                              None, slot_date, slot_time, session_id=sid)
db.create_callback_request(cid, "Linked Patient", "01712345678", "please call")
cli_d, _tok = agent_client("agent_a")
body = cli_d.get("/agent/api/conversation/%d" % cid).get_json()
check("A4: detail includes the session's appointment",
      any(a["id"] == appt_id for a in body.get("appointments", [])))
check("A4: detail includes the conversation's callbacks",
      len(body.get("callbacks", [])) >= 1)

# 27. Agent callback API (A1) -------------------------------------------------
cb_id = db.create_callback_request(cid, "Caller One", "01712345678", "call me back")
cli_c, tok_c = agent_client("agent_a")

r = app.app.test_client().get("/agent/api/callbacks")
check("A1: callbacks require login (401)", r.status_code == 401)

r = cli_c.get("/agent/api/callbacks")
body = r.get_json()
check("A1: callbacks list returns 200", r.status_code == 200)
found = [c for c in body.get("callbacks", []) if c["id"] == cb_id]
check("A1: list includes the new callback with status 'new'",
      bool(found) and found[0]["status"] == "new")
ids = [c["id"] for c in body.get("callbacks", [])]
check("A1: callbacks are newest first", ids == sorted(ids, reverse=True))

r = cli_c.post("/agent/api/callbacks/%d/status" % cb_id, json={"status": "contacted"})
check("A1: status update needs CSRF (403)", r.status_code == 403)

r = cli_c.post("/agent/api/callbacks/%d/status" % cb_id,
               json={"status": "bogus"}, headers={"X-CSRF-Token": tok_c})
check("A1: bad status rejected (400)", r.status_code == 400)

r = cli_c.post("/agent/api/callbacks/%d/status" % cb_id,
               json={"status": "contacted"}, headers={"X-CSRF-Token": tok_c})
check("A1: valid status update ok", r.status_code == 200
      and r.get_json()["callback"]["status"] == "contacted")
check("A1: status persisted to db",
      db.get_callback_request(cb_id)["status"] == "contacted")

r = cli_c.post("/agent/api/callbacks/999999/status",
               json={"status": "done"}, headers={"X-CSRF-Token": tok_c})
check("A1: unknown callback -> 404", r.status_code == 404)

# 28. Canned replies (A2) -----------------------------------------------------
en = db.get_canned_replies("en")
bn = db.get_canned_replies("bn")
check("A2: 5 EN canned replies seeded", len(en) == 5)
check("A2: 5 BN canned replies seeded", len(bn) == 5)
check("A2: canned replies are non-empty strings",
      all(isinstance(x, str) and x.strip() for x in en + bn))

r = app.app.test_client().get("/agent/api/canned")
check("A2: canned endpoint requires login (401)", r.status_code == 401)

r = cli_c.get("/agent/api/canned")
body = r.get_json()
check("A2: /agent/api/canned returns en+bn lists", r.status_code == 200
      and isinstance(body.get("en"), list) and isinstance(body.get("bn"), list))
check("A2: canned endpoint returns the seeded replies",
      body["en"] == en and body["bn"] == bn)

db.set_setting("canned_replies_en", "{not json")
check("A2: malformed canned setting -> empty list", db.get_canned_replies("en") == [])
db.set_canned_replies("en", ["Hello", "Bye"])
check("A2: set_canned_replies round-trips",
      db.get_canned_replies("en") == ["Hello", "Bye"])
db.set_canned_replies("en", en)

# 29. Agent console page renders ----------------------------------------------
r = cli_c.get("/agent")
html = r.get_data(as_text=True)
check("console: /agent renders for a logged-in agent (200)", r.status_code == 200)
check("console: page includes the CSRF meta tag", 'name="csrf-token"' in html)
check("console: page includes the composer + callback tab",
      'id="composer-input"' in html and 'data-tab="callbacks"' in html)
anon = app.app.test_client().get("/agent")
check("console: /agent redirects anonymous visitors", anon.status_code == 302
      and "/agent/login" in anon.headers.get("Location", ""))

# ─── Summary ──────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok in results if ok)
print()
print("TOTAL: %d/%d passed" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
