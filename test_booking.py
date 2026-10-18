"""End-to-end tests for the data-driven booking pipeline.

Runs against a throwaway SQLite database (the real ``clinic.db`` is untouched)
and mocks the AI so no network access is needed.

    python test_booking.py
"""

import os
import sys
import json
import datetime
import tempfile

# Skip app's startup DB init, then point the db layer at a temp database.
os.environ["SKIP_DB_INIT"] = "1"

import db  # noqa: E402

_TMPDIR = tempfile.mkdtemp(prefix="clinic_test_")
db.DB_FILE = os.path.join(_TMPDIR, "test_clinic.db")
db.init_db()

import prompt_builder  # noqa: E402
import app  # noqa: E402


client = app.app.test_client()
results = []


def check(label, condition):
    results.append((label, bool(condition)))
    print(("PASS" if condition else "FAIL"), "-", label)


# ─── Mock the AI ──────────────────────────────────────────────────────────────
state = {"reply": "", "corrective_reply": "Okay, let's find another time that works.",
         "calls": []}


def fake_chat(messages, lang="bn"):
    """Return the canned reply; use the corrective text for INTERNAL prompts."""
    system = messages[0]["content"]
    corrective = "INTERNAL" in system
    state["calls"].append("corrective" if corrective else "normal")
    return state["corrective_reply"] if corrective else state["reply"]


app._openrouter_chat = fake_chat


def marker(payload):
    return "Sure, let me book that for you!\nSAVE_APPOINTMENT:" + json.dumps(payload)


def post_chat(message, session_id="T1", lang="en", headers=None):
    return client.post("/chat", json={"session_id": session_id, "message": message,
                                      "lang": lang}, headers=headers or {})


def find_slot(doctor_id, *, weekdays=None, days=60):
    """Return (date, slot) for the first free slot (optionally a weekday set)."""
    today = db.dhaka_today()
    for offset in range(days):
        day = today + datetime.timedelta(days=offset)
        if weekdays is not None and day.weekday() not in weekdays:
            continue
        slots = db.get_available_slots(doctor_id, day.isoformat())
        if slots:
            return day.isoformat(), slots[0]
    return None, None


print("=" * 70)
print("Data-driven booking pipeline tests")
print("=" * 70)

# 1. Normal booking -----------------------------------------------------------
date, slot = find_slot(1)
assert date and slot, "expected a free slot for doctor 1"
state["reply"] = marker({"patient_name": "Test Patient", "phone": "01712345678",
                         "doctor_id": 1, "service": "Checkup",
                         "date": date, "time": slot, "notes": "toothache"})
state["calls"].clear()
resp = post_chat("I'd like to book", session_id="normal")
body = resp.get_json()
check("normal booking -> 200", resp.status_code == 200)
check("normal booking saved", body["appointment_saved"] is True)
check("normal booking returns id", isinstance(body["appointment_id"], int))
check("marker stripped from reply", "SAVE_APPOINTMENT" not in body["reply"])
saved = db.get_appointment(body["appointment_id"])
check("row has status pending", saved["status"] == "pending")
check("row stores session_id", saved["session_id"] == "normal")
check("row stores language", saved["language"] == "en")
check("service mapped to id", saved["service_id"] is not None)
check("normal booking no corrective call", state["calls"] == ["normal"])

# 2. Booking on a taken slot --------------------------------------------------
state["reply"] = marker({"patient_name": "Second Patient", "phone": "01812345678",
                         "doctor_id": 1, "date": date, "time": slot})
state["calls"].clear()
resp = post_chat("book the same time", session_id="taken")
body = resp.get_json()
check("taken slot not saved", body["appointment_saved"] is False)
check("taken slot marker stripped", "SAVE_APPOINTMENT" not in body["reply"])
check("taken slot triggered corrective AI call", state["calls"] == ["normal", "corrective"])
check("slot still booked exactly once",
      len([a for a in db.list_appointments()
           if a["doctor_id"] == 1 and a["date"] == date and a["time"] == slot]) == 1)

# 3. Invalid phone ------------------------------------------------------------
state["reply"] = marker({"patient_name": "Bad Phone", "phone": "12345",
                         "doctor_id": 1, "date": date, "time": slot})
state["calls"].clear()
resp = post_chat("my number is 12345", session_id="badphone")
body = resp.get_json()
check("invalid phone not saved", body["appointment_saved"] is False)
check("invalid phone corrective call", "corrective" in state["calls"])

# 4. Doctor does not work that day -------------------------------------------
# Doctor 2 works Tue/Thu only; pick a day they are closed.
bad_date, _ = find_slot(2, weekdays={0, 2, 4, 5, 6}, days=60)  # Mon/Wed/Fri/Sat/Sun
state["reply"] = marker({"patient_name": "Wrong Day", "phone": "01912345678",
                         "doctor_id": 2, "date": bad_date, "time": "10:00"})
state["calls"].clear()
resp = post_chat("can I come that day?", session_id="wrongday")
body = resp.get_json()
check("non-working day not saved", body["appointment_saved"] is False)
check("non-working day corrective call", "corrective" in state["calls"])

# 5. Booking in the past ------------------------------------------------------
past = (db.dhaka_today() - datetime.timedelta(days=1)).isoformat()
state["reply"] = marker({"patient_name": "Past Patient", "phone": "01712345678",
                         "doctor_id": 1, "date": past, "time": "10:00"})
state["calls"].clear()
resp = post_chat("book me yesterday", session_id="past")
body = resp.get_json()
check("past date not saved", body["appointment_saved"] is False)
check("past date corrective call", "corrective" in state["calls"])

# 6. Malformed marker is still stripped --------------------------------------
state["reply"] = "Hello there!\nSAVE_APPOINTMENT:{this is not valid json"
state["calls"].clear()
resp = post_chat("ok", session_id="malformed")
body = resp.get_json()
check("malformed marker stripped", "SAVE_APPOINTMENT" not in body["reply"])
check("malformed marker not saved", body["appointment_saved"] is False)

# 7. Atomic double-booking at the db layer ------------------------------------
date7, slot7 = find_slot(2)
first = db.book_appointment("A", "01712345678", 2, None, date7, slot7, session_id="x")
second = db.book_appointment("B", "01712345678", 2, None, date7, slot7, session_id="x")
check("first atomic booking succeeds", isinstance(first, int))
check("second atomic booking rejected", second is None)

# 8. Reset starts a fresh conversation without deleting history ---------------
db.reset_conversation("reset-test")  # ensure clean baseline marker
db.add_chat_message("reset-test", "user", "old message")
db.add_chat_message("reset-test", "assistant", "old reply")
db.reset_conversation("reset-test")
db.add_chat_message("reset-test", "user", "new message")
recent = db.get_recent_messages("reset-test", limit=12)
check("reset hides pre-reset messages", [m["content"] for m in recent] == ["new message"])
with db._connect() as conn:
    total = conn.execute("SELECT COUNT(*) n FROM chat_messages WHERE session_id = 'reset-test'"
                         ).fetchone()["n"]
check("reset keeps the full log", total >= 4)

# 9. History limit: only last 12 messages are sent ---------------------------
for i in range(20):
    db.add_chat_message("hist", "user", f"m{i}")
check("history capped at 12", len(db.get_recent_messages("hist", limit=12)) == 12)

# 10. Message length guard ----------------------------------------------------
long_msg = "x" * (app.MAX_MESSAGE_LEN + 1)
resp = post_chat(long_msg, session_id="long")
check("over-long message rejected (400)", resp.status_code == 400)
check("over-long message has bilingual reply", bool(resp.get_json().get("reply")))

# 11. Per-IP rate limit -------------------------------------------------------
app._rate_buckets.clear()
app.RATE_LIMIT = 3
state["reply"] = "just chatting"
codes = [post_chat("hi", session_id="rl", headers={"X-Forwarded-For": "9.9.9.9"}).status_code
         for _ in range(4)]
check("rate limit kicks in (last = 429)", codes[-1] == 429)
check("earlier requests allowed", all(c == 200 for c in codes[:-1]))
app.RATE_LIMIT = 20  # restore

# ─── Summary ──────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok in results if ok)
print()
print(f"TOTAL: {passed}/{len(results)} passed")

print("\n" + "=" * 70)
print("Generated BN system prompt")
print("=" * 70)
print(prompt_builder.build_system_prompt("bn"))

sys.exit(0 if passed == len(results) else 1)
