"""Database layer for the Smile Dental Care AI receptionist.

All dynamic data (settings, doctors, schedules, services, appointments,
chat messages, admin accounts) lives in a single SQLite database file.

Uses only the standard-library ``sqlite3`` module — no extra pip packages.
The password hashing helpers come from ``werkzeug.security`` which ships with
Flask.

Everything in this module uses parameterized SQL; never build queries by
string formatting.
"""

import os
import re
import json
import sqlite3
import datetime
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash, check_password_hash


DB_FILE = "clinic.db"
APPOINTMENTS_JSON = "appointments.json"
APPOINTMENTS_MIGRATED = "appointments.json.migrated"

TZ_DHAKA = ZoneInfo("Asia/Dhaka")


# ─── CONNECTION ───────────────────────────────────────────────────────────────

@contextmanager
def _connect():
    """Yield a SQLite connection, committing on success and closing always."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _connect_tx():
    """Connection inside an immediate write transaction (atomic booking)."""
    conn = sqlite3.connect(DB_FILE, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _now():
    """Return the current Asia/Dhaka timestamp as an ISO-8601 string."""
    return dhaka_now().isoformat()


def dhaka_now():
    """Return the current datetime in the Asia/Dhaka timezone."""
    return datetime.datetime.now(TZ_DHAKA)


def dhaka_today():
    """Return today's date in Asia/Dhaka as a ``datetime.date``."""
    return dhaka_now().date()


def _rows_to_dicts(rows):
    """Convert a list of ``sqlite3.Row`` objects into plain dicts."""
    return [dict(r) for r in rows]


def _to_minutes(hhmm):
    """Convert 'HH:MM' to minutes past midnight."""
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def _from_minutes(minutes):
    """Convert minutes past midnight to 'HH:MM'."""
    return "%02d:%02d" % divmod(minutes, 60)


# ─── SCHEMA ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS doctors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name_en        TEXT,
    name_bn        TEXT,
    specialty_en   TEXT,
    specialty_bn   TEXT,
    bio_en         TEXT,
    bio_bn         TEXT,
    slot_minutes   INTEGER DEFAULT 30,
    active         INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS doctor_schedule (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id  INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL,          -- 0 = Monday ... 6 = Sunday
    start_time TEXT NOT NULL,             -- 'HH:MM'
    end_time   TEXT NOT NULL              -- 'HH:MM'
);

CREATE TABLE IF NOT EXISTS doctor_time_off (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id  INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    date_from  TEXT NOT NULL,             -- 'YYYY-MM-DD' (inclusive)
    date_to    TEXT NOT NULL,             -- 'YYYY-MM-DD' (inclusive)
    reason     TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_doctor_time_off_doctor
    ON doctor_time_off(doctor_id);

CREATE TABLE IF NOT EXISTS services (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name_en          TEXT,
    name_bn          TEXT,
    price            REAL,
    price_max        REAL,
    duration_minutes INTEGER,
    active           INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS appointments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_name TEXT,
    phone        TEXT,
    doctor_id    INTEGER REFERENCES doctors(id) ON DELETE SET NULL,
    service_id   INTEGER REFERENCES services(id) ON DELETE SET NULL,
    date         TEXT,
    time         TEXT,
    status       TEXT DEFAULT 'pending',
    notes        TEXT,
    language     TEXT,
    session_id   TEXT,
    created_at   TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS admins (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT UNIQUE,
    password_hash        TEXT,
    display_name         TEXT,
    role                 TEXT DEFAULT 'owner',   -- 'owner' or 'agent'
    is_online            INTEGER DEFAULT 0,
    last_seen            TEXT,
    must_change_password INTEGER DEFAULT 0       -- 1 = forced to change it first
);

CREATE TABLE IF NOT EXISTS conversations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id         INTEGER DEFAULT 1,
    session_id          TEXT,
    status              TEXT DEFAULT 'bot',  -- bot / waiting / with_agent / closed
    agent_id            INTEGER REFERENCES admins(id) ON DELETE SET NULL,
    handoff_reason      TEXT,
    lang                TEXT,
    created_at          TEXT,
    handoff_requested_at TEXT,
    accepted_at         TEXT,
    closed_at           TEXT,
    last_message_at     TEXT,
    customer_last_seen  TEXT             -- last time the customer's widget spoke to us
);

CREATE INDEX IF NOT EXISTS idx_conversations_session
    ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_status
    ON conversations(status);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,
    role            TEXT,                 -- legacy: user / assistant / system
    content         TEXT,
    created_at      TEXT,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    sender_type     TEXT,                 -- customer / bot / agent / system
    agent_id        INTEGER REFERENCES admins(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages(session_id);

CREATE TABLE IF NOT EXISTS callback_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     INTEGER DEFAULT 1,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    name            TEXT,
    phone           TEXT,
    message         TEXT,
    status          TEXT DEFAULT 'new',   -- new / contacted / done
    created_at      TEXT
);
"""


def _create_tables():
    """Create every table and index if they do not already exist."""
    with _connect() as conn:
        conn.executescript(SCHEMA)


# ─── CANNED REPLIES (agent console quick answers) ─────────────────────────────
# Seeded into the ``settings`` table as JSON strings. They can be edited later
# from the admin panel without a code change.
CANNED_REPLIES_EN = [
    "Hello! Thanks for reaching out to Smile Dental Care. How can I help you today?",
    "Could you please share your phone number so we can get back to you?",
    "One moment while I check that for you.",
    "The doctor will confirm your appointment shortly.",
    "Thanks for chatting with us — take care!",
]
CANNED_REPLIES_BN = [
    "আসসালামু আলাইকুম! স্মাইল ডেন্টাল কেয়ারে যোগাযোগ করার জন্য ধন্যবাদ। কীভাবে সাহায্য করতে পারি?",
    "আপনার ফোন নম্বরটা একটু বলবেন? যাতে আমরা যোগাযোগ করতে পারি।",
    "একটু অপেক্ষা করুন, আমি দেখে নিচ্ছি।",
    "ডাক্তার আপনার অ্যাপয়েন্টমেন্ট শীঘ্রই নিশ্চিত করবেন।",
    "আমাদের সাথে কথা বলার জন্য ধন্যবাদ — ভালো থাকুন!",
]

# Valid callback request statuses (mirrors the ``callback_requests`` comment).
CALLBACK_STATUSES = ("new", "contacted", "done")


# ─── SEED DATA (used only to initialise a brand-new database) ─────────────────

# Prices are stored as a range: ``price`` is the minimum and ``price_max`` the
# maximum. A service with a single fixed price has ``price_max = None``.
SEED_DATA = {
    "settings": {
        "clinic_name": "Smile Dental Care",
        "clinic_name_bn": "স্মাইল ডেন্টাল কেয়ার",
        "address": "Dhanmondi 27, Dhaka",
        "address_bn": "ধানমন্ডি ২৭, ঢাকা",
        "phone": "",
        "opening_hours": "9 AM — 8 PM (Closed Sundays)",
        "opening_hours_bn": "সকাল ৯টা — রাত ৮টা (রবিবার বন্ধ)",
        "persona_name": "Mita",
        "persona_name_bn": "মিতা",
        "welcome_en": "",
        "welcome_bn": "",
        "extra_info_en": "",
        "extra_info_bn": "",
        "ai_model": "openrouter/auto",
        # Human-handoff timings.
        "handoff_timeout_minutes": "5",
        "agent_online_window_seconds": "45",
        "idle_close_minutes": "30",
        # How long a silent customer is considered "away" / "gone" while queued.
        "customer_away_seconds": "45",
        "customer_left_seconds": "300",
        # Agent console quick replies (JSON lists; editable later in /admin).
        "canned_replies_en": json.dumps(CANNED_REPLIES_EN, ensure_ascii=False),
        "canned_replies_bn": json.dumps(CANNED_REPLIES_BN, ensure_ascii=False),
    },
    "doctors": [
        {
            "name_en": "Dr. Rahela Khanam",
            "name_bn": "ডা. রাহেলা খানম",
            "specialty_en": "Dental Specialist",
            "specialty_bn": "দাঁতের বিশেষজ্ঞ",
            "bio_en": "",
            "bio_bn": "",
            "slot_minutes": 30,
            "active": 1,
            "schedule": [
                {"weekday": 0, "start_time": "10:00", "end_time": "14:00"},
                {"weekday": 2, "start_time": "10:00", "end_time": "14:00"},
                {"weekday": 4, "start_time": "10:00", "end_time": "14:00"},
            ],
        },
        {
            "name_en": "Dr. Karim",
            "name_bn": "ডা. করিম সাহেব",
            "specialty_en": "Pediatric Dentist",
            "specialty_bn": "শিশু দন্ত বিশেষজ্ঞ",
            "bio_en": "",
            "bio_bn": "",
            "slot_minutes": 30,
            "active": 1,
            "schedule": [
                {"weekday": 1, "start_time": "16:00", "end_time": "20:00"},
                {"weekday": 3, "start_time": "16:00", "end_time": "20:00"},
            ],
        },
    ],
    "services": [
        {"name_en": "Checkup", "name_bn": "চেকআপ", "price": 300.0,
         "price_max": 500.0, "duration_minutes": 30, "active": 1},
        {"name_en": "Scaling", "name_bn": "স্কেলিং", "price": 800.0,
         "price_max": 1200.0, "duration_minutes": 30, "active": 1},
        {"name_en": "Filling", "name_bn": "ফিলিং", "price": 500.0,
         "price_max": 1500.0, "duration_minutes": 30, "active": 1},
        {"name_en": "Root Canal", "name_bn": "রুট ক্যানেল", "price": 3000.0,
         "price_max": 6000.0, "duration_minutes": 30, "active": 1},
        {"name_en": "Extraction", "name_bn": "দাঁত তোলা", "price": 500.0,
         "price_max": 1000.0, "duration_minutes": 30, "active": 1},
        {"name_en": "Denture", "name_bn": "ডেনচার", "price": 5000.0,
         "price_max": 15000.0, "duration_minutes": 30, "active": 1},
    ],
}

# Original price ranges from the old hardcoded prompts. Used once to backfill
# ``price_max`` on databases created before that column existed.
OLD_PRICE_RANGES = {
    s["name_en"]: (s["price"], s["price_max"]) for s in SEED_DATA["services"]
}


# ─── SCHEMA MIGRATIONS (idempotent, check columns before altering) ────────────

def _table_columns(conn, table):
    """Return the set of column names for a table (SQLite, parameter-free)."""
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _migrate_columns():
    """Add columns introduced after the first release (safe to re-run)."""
    with _connect() as conn:
        service_cols = _table_columns(conn, "services")
        if "price_max" not in service_cols:
            conn.execute("ALTER TABLE services ADD COLUMN price_max REAL")
            print("[db] Added column services.price_max")
        appt_cols = _table_columns(conn, "appointments")
        if "session_id" not in appt_cols:
            conn.execute("ALTER TABLE appointments ADD COLUMN session_id TEXT")
            print("[db] Added column appointments.session_id")

        # Human-handoff columns + the forced-password-change flag.
        admin_cols = _table_columns(conn, "admins")
        for col, ddl in (
            ("display_name", "ALTER TABLE admins ADD COLUMN display_name TEXT"),
            ("role", "ALTER TABLE admins ADD COLUMN role TEXT DEFAULT 'owner'"),
            ("is_online", "ALTER TABLE admins ADD COLUMN is_online INTEGER DEFAULT 0"),
            ("last_seen", "ALTER TABLE admins ADD COLUMN last_seen TEXT"),
            ("must_change_password",
             "ALTER TABLE admins ADD COLUMN must_change_password INTEGER DEFAULT 0"),
        ):
            if col not in admin_cols:
                conn.execute(ddl)
                print("[db] Added column admins.%s" % col)

        msg_cols = _table_columns(conn, "chat_messages")
        for col, ddl in (
            ("conversation_id",
             "ALTER TABLE chat_messages ADD COLUMN conversation_id INTEGER"),
            ("sender_type", "ALTER TABLE chat_messages ADD COLUMN sender_type TEXT"),
            ("agent_id", "ALTER TABLE chat_messages ADD COLUMN agent_id INTEGER"),
        ):
            if col not in msg_cols:
                conn.execute(ddl)
                print("[db] Added column chat_messages.%s" % col)

        # Indexes that depend on columns added above (must run after the ALTERs).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation "
            "ON chat_messages(conversation_id)"
        )

        # Doctor days off (Prompt 4B). New databases get these from SCHEMA; this
        # keeps databases created earlier in step, and is safe to re-run.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS doctor_time_off (
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   doctor_id  INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
                   date_from  TEXT NOT NULL,
                   date_to    TEXT NOT NULL,
                   reason     TEXT,
                   created_at TEXT
               )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_doctor_time_off_doctor "
            "ON doctor_time_off(doctor_id)"
        )

        # Track the last time the customer's widget reached the server, so a
        # queued conversation can be detected as abandoned (customer left).
        conv_cols = _table_columns(conn, "conversations")
        if "customer_last_seen" not in conv_cols:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN customer_last_seen TEXT"
            )
            print("[db] Added column conversations.customer_last_seen")


def _seed_missing_settings():
    """Insert handoff settings that are absent, leaving existing values alone."""
    defaults = {
        "handoff_timeout_minutes": "5",
        "agent_online_window_seconds": "45",
        "idle_close_minutes": "30",
        "customer_away_seconds": "45",
        "customer_left_seconds": "300",
        "canned_replies_en": json.dumps(CANNED_REPLIES_EN, ensure_ascii=False),
        "canned_replies_bn": json.dumps(CANNED_REPLIES_BN, ensure_ascii=False),
    }
    with _connect() as conn:
        existing = {r["key"] for r in conn.execute("SELECT key FROM settings")}
        for key, value in defaults.items():
            if key not in existing:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)", (key, value)
                )


# The value ``customer_left_seconds`` shipped with before this release.
_OLD_CUSTOMER_LEFT_DEFAULT = "120"


def _migrate_settings():
    """One-off setting upgrades. Only touches values still on the old default."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'customer_left_seconds'"
        ).fetchone()
        if row and str(row["value"]).strip() == _OLD_CUSTOMER_LEFT_DEFAULT:
            conn.execute(
                "UPDATE settings SET value = '300' WHERE key = 'customer_left_seconds'"
            )
            print("[db] Migrated customer_left_seconds 120 -> 300")


SENDER_BY_ROLE = {"user": "customer", "assistant": "bot", "system": "system"}


def _migrate_conversations():
    """Backfill conversations + ``sender_type`` for pre-handoff chat logs.

    One conversation is created per contiguous run of messages for a session,
    splitting at legacy reset markers so the old "reset starts a fresh
    conversation" behaviour is preserved.
    """
    with _connect() as conn:
        # Anything with a message but no sender_type needs backfilling.
        pending = conn.execute(
            """SELECT COUNT(*) AS n FROM chat_messages
               WHERE sender_type IS NULL OR conversation_id IS NULL"""
        ).fetchone()["n"]
        if pending == 0:
            return

        sessions = [r["session_id"] for r in conn.execute(
            "SELECT DISTINCT session_id FROM chat_messages WHERE session_id IS NOT NULL"
        )]
        created = 0
        for session_id in sessions:
            rows = conn.execute(
                """SELECT * FROM chat_messages
                   WHERE session_id = ? ORDER BY id""",
                (session_id,),
            ).fetchall()
            conv_id = None
            for row in rows:
                is_reset = (row["role"] == "system"
                            and row["content"] == RESET_MARKER)
                if is_reset and conv_id is not None:
                    # Close the current conversation and start a fresh one.
                    conn.execute(
                        "UPDATE conversations SET status = 'closed', closed_at = ? "
                        "WHERE id = ?", (row["created_at"] or _now(), conv_id),
                    )
                    conv_id = None
                if conv_id is None:
                    cur = conn.execute(
                        """INSERT INTO conversations
                           (business_id, session_id, status, lang, created_at,
                            last_message_at)
                           VALUES (1, ?, 'bot', NULL, ?, ?)""",
                        (session_id, row["created_at"] or _now(),
                         row["created_at"] or _now()),
                    )
                    conv_id = cur.lastrowid
                    created += 1
                sender = row["sender_type"] or SENDER_BY_ROLE.get(row["role"], "system")
                conn.execute(
                    "UPDATE chat_messages SET conversation_id = ?, sender_type = ? "
                    "WHERE id = ?", (conv_id, sender, row["id"]),
                )
        if created:
            print("[db] Linked chat history into %d conversation(s)." % created)


def _seed_demo_agents():
    """Create two demo agent accounts when ``DEMO_SEED=1`` (idempotent)."""
    if os.environ.get("DEMO_SEED") != "1":
        return
    demos = [
        ("agent_riya", "Riya (demo)", "agentpass1"),
        ("agent_sam", "Sam (demo)", "agentpass2"),
    ]
    for username, display_name, password in demos:
        if not get_admin(username):
            create_admin(username, password, display_name=display_name, role="agent")
            print("[db] Seeded demo agent '%s'." % username)


def preview_price_backfill():
    """Return the old range vs proposed price/price_max for every service.

    Used to review the backfill before it is applied.
    """
    out = []
    for s in list_services():
        rng = OLD_PRICE_RANGES.get(s["name_en"])
        proposed_max = s.get("price_max")
        if rng and proposed_max is None:
            proposed_max = rng[1]
            action = "set price_max"
        else:
            action = "no change"
        out.append({
            "service": s["name_en"],
            "old_range": ("%d-%d" % (int(rng[0]), int(rng[1]))) if rng else "—",
            "price": s["price"],
            "price_max": proposed_max,
            "action": action,
        })
    return out


def _backfill_price_max():
    """Fill ``price_max`` from the known ranges where it is still NULL."""
    pending = [
        (OLD_PRICE_RANGES[s["name_en"]], s)
        for s in list_services()
        if s["name_en"] in OLD_PRICE_RANGES and s.get("price_max") is None
    ]
    if not pending:
        return
    print("\n[db] Backfilling services.price_max from the old prompt ranges:")
    print("    %-12s %-12s %-10s %s" % ("service", "old range", "price", "price_max"))
    with _connect() as conn:
        for rng, s in pending:
            print("    %-12s %-12s %-10s %s" % (
                s["name_en"], "%d-%d" % (int(rng[0]), int(rng[1])),
                int(rng[0]), int(rng[1])))
            conn.execute(
                "UPDATE services SET price_max = ? WHERE id = ?",
                (rng[1], s["id"]),
            )
    print()


# ─── SEEDING ──────────────────────────────────────────────────────────────────

def _seed_if_empty(ai_model=None):
    """Insert the built-in clinic data when the settings table is empty."""
    with _connect() as conn:
        empty = conn.execute("SELECT COUNT(*) AS n FROM settings").fetchone()["n"] == 0
    if not empty:
        return

    print("[db] Seeding initial clinic data...")
    data = SEED_DATA
    with _connect() as conn:
        for key, value in data["settings"].items():
            if key == "ai_model" and ai_model:
                value = ai_model
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        for doc in data["doctors"]:
            cur = conn.execute(
                """INSERT INTO doctors
                   (name_en, name_bn, specialty_en, specialty_bn,
                    bio_en, bio_bn, slot_minutes, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc["name_en"], doc["name_bn"], doc["specialty_en"],
                 doc["specialty_bn"], doc["bio_en"], doc["bio_bn"],
                 doc["slot_minutes"], doc["active"]),
            )
            doctor_id = cur.lastrowid
            for slot in doc["schedule"]:
                conn.execute(
                    """INSERT INTO doctor_schedule
                       (doctor_id, weekday, start_time, end_time)
                       VALUES (?, ?, ?, ?)""",
                    (doctor_id, slot["weekday"], slot["start_time"], slot["end_time"]),
                )
        for svc in data["services"]:
            conn.execute(
                """INSERT INTO services
                   (name_en, name_bn, price, price_max, duration_minutes, active)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (svc["name_en"], svc["name_bn"], svc["price"], svc["price_max"],
                 svc["duration_minutes"], svc["active"]),
            )
    print("[db] Seed complete.\n")


# ─── MIGRATION FROM appointments.json ─────────────────────────────────────────

def _migrate_appointments_json():
    """Best-effort import of legacy appointments.json, then rename the file.

    - Maps a doctor by name when a ``doctor``/``doctor_name`` key is present.
    - Skips records that are not dicts or lack a name/phone and reports them.
    """
    if not os.path.exists(APPOINTMENTS_JSON):
        return

    try:
        with open(APPOINTMENTS_JSON, "r", encoding="utf-8") as f:
            records = json.load(f)
    except Exception as exc:  # unreadable / invalid JSON
        print(f"[db] Could not read {APPOINTMENTS_JSON}: {exc}")
        return

    if not isinstance(records, list):
        records = []

    migrated, skipped = 0, []
    with _connect() as conn:
        doctor_lookup = {
            (r["name_en"] or "").lower(): r["id"]
            for r in conn.execute("SELECT id, name_en FROM doctors")
        }
        for i, rec in enumerate(records):
            try:
                if not isinstance(rec, dict):
                    skipped.append((i, "not an object"))
                    continue
                name = (rec.get("name") or "").strip()
                phone = (rec.get("phone") or "").strip()
                if not name or not phone:
                    skipped.append((i, "missing name or phone"))
                    continue

                doctor_id = None
                doc_name = (rec.get("doctor") or rec.get("doctor_name") or "").strip().lower()
                if doc_name:
                    doctor_id = doctor_lookup.get(doc_name)

                created = rec.get("saved_at") or _now()
                conn.execute(
                    """INSERT INTO appointments
                       (patient_name, phone, doctor_id, service_id, date, time,
                        status, notes, language, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (name, phone, doctor_id, None,
                     rec.get("date"), rec.get("time"),
                     rec.get("status", "pending"),
                     rec.get("problem") or rec.get("notes"),
                     rec.get("language"),
                     created, created),
                )
                migrated += 1
            except Exception as exc:
                skipped.append((i, str(exc)))

    os.replace(APPOINTMENTS_JSON, APPOINTMENTS_MIGRATED)
    print(f"[db] Migrated {migrated} appointment(s) from {APPOINTMENTS_JSON}; "
          f"renamed it to {APPOINTMENTS_MIGRATED}.")
    if skipped:
        print(f"[db] Skipped {len(skipped)} unparseable record(s):")
        for idx, reason in skipped:
            print(f"     - record #{idx}: {reason}")


# ─── INIT ─────────────────────────────────────────────────────────────────────

def init_db(ai_model="openrouter/auto", admin_username=None, admin_password=None):
    """Create tables, migrate columns/data, seed data and seed admin.

    Safe to call on every app startup: table creation uses ``IF NOT EXISTS``,
    column migrations check for the column first, data migrations are guarded by
    completeness checks, seeding only happens when the settings table is empty,
    and the legacy JSON migration only runs while ``appointments.json`` exists.
    """
    _create_tables()
    _migrate_columns()
    _seed_if_empty(ai_model)
    _seed_missing_settings()
    _migrate_settings()
    _backfill_price_max()
    _migrate_conversations()
    if admin_username and admin_password:
        ensure_admin(admin_username, admin_password)
        _flag_default_password_owners(admin_username, admin_password)
    _seed_demo_agents()
    _migrate_appointments_json()


# ─── SETTINGS ─────────────────────────────────────────────────────────────────

def get_settings():
    """Return all settings as a ``{key: value}`` dict."""
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_setting(key, default=None):
    """Return a single setting value, or ``default`` when it is missing."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def get_setting_int(key, default=0):
    """Return a setting coerced to ``int``, falling back to ``default``."""
    try:
        return int(float(get_setting(key, default)))
    except (TypeError, ValueError):
        return default


def set_setting(key, value):
    """Insert or update a single setting by key."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_canned_replies(lang="en"):
    """Return the agent console's canned replies for ``lang`` as a list.

    Stored as a JSON array in the ``canned_replies_en`` / ``canned_replies_bn``
    settings. A missing or malformed value yields an empty list.
    """
    key = "canned_replies_bn" if str(lang).lower().startswith("bn") else "canned_replies_en"
    raw = get_setting(key)
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        data = []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def set_canned_replies(lang, replies):
    """Persist a list of canned replies for ``lang`` (used by tests/admin)."""
    if not isinstance(replies, (list, tuple)):
        raise ValueError("replies must be a list")
    key = "canned_replies_bn" if str(lang).lower().startswith("bn") else "canned_replies_en"
    set_setting(key, json.dumps([str(r) for r in replies], ensure_ascii=False))


# ─── PHONE VALIDATION (shared by booking and callbacks) ───────────────────────
# Accepts +8801XXXXXXXXX, 8801XXXXXXXXX and 01XXXXXXXXX (3rd digit 3–9).
PHONE_RE = re.compile(r"^(?:\+?880|0)1[3-9]\d{8}$")


def normalize_phone(raw):
    """Strip spaces/brackets/dashes from a phone number for validation."""
    return re.sub(r"[\s\-()]", "", str(raw or "")).strip()


def is_valid_phone(raw):
    """Return True when ``raw`` looks like a valid Bangladeshi mobile number."""
    return bool(PHONE_RE.match(normalize_phone(raw)))


def format_price(price, price_max=None):
    """Format a price as ``'300-500 BDT'`` (range) or ``'300 BDT'`` (fixed)."""
    if price is None:
        return ""
    def _fmt(value):
        return str(int(value)) if float(value).is_integer() else str(value)
    if price_max is not None and price_max != price:
        return "%s-%s BDT" % (_fmt(price), _fmt(price_max))
    return "%s BDT" % _fmt(price)


# ─── AVAILABILITY ─────────────────────────────────────────────────────────────

def get_available_slots(doctor_id, date):
    """Return the free ``'HH:MM'`` slots for a doctor on a date (Asia/Dhaka).

    Slots come from the doctor's weekly schedule, generated every
    ``slot_minutes``. Slots already booked (any status except ``cancelled``)
    are removed, and when ``date`` is today, slots already in the past are
    removed too. A disabled doctor, or a date inside one of the doctor's
    days-off ranges, has no slots at all.
    """
    doctor = get_doctor(doctor_id)
    if not doctor:
        return []
    if not doctor.get("active"):
        return []
    try:
        day = datetime.date.fromisoformat(date)
    except (ValueError, TypeError):
        return []
    if is_doctor_off(doctor_id, date):
        return []

    slot_minutes = doctor.get("slot_minutes") or 30
    minutes = []
    for row in doctor["schedule"]:
        if row["weekday"] != day.weekday():
            continue
        start, end = _to_minutes(row["start_time"]), _to_minutes(row["end_time"])
        t = start
        while t < end:
            minutes.append(t)
            t += slot_minutes

    with _connect() as conn:
        booked = {
            r["time"] for r in conn.execute(
                """SELECT time FROM appointments
                   WHERE doctor_id = ? AND date = ? AND status != 'cancelled'""",
                (doctor_id, date),
            )
        }
    minutes = [m for m in minutes if _from_minutes(m) not in booked]

    now = dhaka_now()
    if date == now.strftime("%Y-%m-%d"):
        now_minutes = now.hour * 60 + now.minute
        minutes = [m for m in minutes if m > now_minutes]

    return [_from_minutes(m) for m in sorted(set(minutes))]


def get_next_available_slots(doctor_id, days=7):
    """Return availability for the next ``days`` days as a list of dicts.

    Each item is ``{"date": 'YYYY-MM-DD', "weekday": 0-6, "slots": [...]}``
    and only days with at least one free slot are included.
    """
    out = []
    today = dhaka_today()
    for offset in range(days):
        day = today + datetime.timedelta(days=offset)
        date = day.isoformat()
        slots = get_available_slots(doctor_id, date)
        if slots:
            out.append({"date": date, "weekday": day.weekday(), "slots": slots})
    return out


def find_doctor_by_name(name):
    """Return an active doctor matching an English or Bengali name, else None."""
    name = (name or "").strip().lower()
    if not name:
        return None
    for doc in list_doctors(active_only=True):
        if (doc["name_en"] or "").lower() == name:
            return doc
        if (doc["name_bn"] or "").strip().lower() == name:
            return doc
    return None


def find_service_by_name(name):
    """Return an active service matching an English or Bengali name, else None."""
    name = (name or "").strip().lower()
    if not name:
        return None
    for svc in list_services(active_only=True):
        if (svc["name_en"] or "").lower() == name:
            return svc
        if (svc["name_bn"] or "").strip().lower() == name:
            return svc
    return None


# ─── DOCTORS ──────────────────────────────────────────────────────────────────

def list_doctors(active_only=False):
    """Return doctors as dicts; when ``active_only`` only active ones."""
    query = "SELECT * FROM doctors"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY id"
    with _connect() as conn:
        return _rows_to_dicts(conn.execute(query).fetchall())


def get_doctor(doctor_id):
    """Return one doctor (with a ``schedule`` list) or ``None``."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM doctors WHERE id = ?", (doctor_id,)).fetchone()
        if row is None:
            return None
        doctor = dict(row)
        doctor["schedule"] = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM doctor_schedule WHERE doctor_id = ? "
                "ORDER BY weekday, start_time",
                (doctor_id,),
            ).fetchall()
        )
    return doctor


def create_doctor(name_en=None, name_bn=None, specialty_en=None, specialty_bn=None,
                  bio_en=None, bio_bn=None, slot_minutes=30, active=1, schedule=None):
    """Create a doctor (and optional schedule slots). Returns the new id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO doctors
               (name_en, name_bn, specialty_en, specialty_bn,
                bio_en, bio_bn, slot_minutes, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name_en, name_bn, specialty_en, specialty_bn,
             bio_en, bio_bn, slot_minutes, active),
        )
        doctor_id = cur.lastrowid
        for slot in (schedule or []):
            conn.execute(
                """INSERT INTO doctor_schedule
                   (doctor_id, weekday, start_time, end_time)
                   VALUES (?, ?, ?, ?)""",
                (doctor_id, slot["weekday"], slot["start_time"], slot["end_time"]),
            )
    return doctor_id


def update_doctor(doctor_id, schedule=None, **fields):
    """Update allowed doctor fields; optionally replace the schedule.

    Pass ``schedule`` as a list of slot dicts to replace existing slots.
    """
    allowed = {"name_en", "name_bn", "specialty_en", "specialty_bn",
               "bio_en", "bio_bn", "slot_minutes", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    with _connect() as conn:
        if updates:
            assignments = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE doctors SET {assignments} WHERE id = ?",
                (*updates.values(), doctor_id),
            )
        if schedule is not None:
            conn.execute("DELETE FROM doctor_schedule WHERE doctor_id = ?", (doctor_id,))
            for slot in schedule:
                conn.execute(
                    """INSERT INTO doctor_schedule
                       (doctor_id, weekday, start_time, end_time)
                       VALUES (?, ?, ?, ?)""",
                    (doctor_id, slot["weekday"], slot["start_time"], slot["end_time"]),
                )
    return get_doctor(doctor_id)


def delete_doctor(doctor_id):
    """Delete a doctor and (via cascade) their schedule. Returns True if deleted."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM doctors WHERE id = ?", (doctor_id,))
        return cur.rowcount > 0


# ─── DOCTOR DAYS OFF ─────────────────────────────────────────────────────────

def list_doctor_time_off(doctor_id, upcoming_only=False, from_date=None):
    """Return a doctor's time-off ranges, soonest first.

    ``upcoming_only`` keeps ranges whose ``date_to`` is on/after today
    (Asia/Dhaka). ``from_date`` overrides "today" for the filter.
    """
    query = "SELECT * FROM doctor_time_off WHERE doctor_id = ?"
    params = [doctor_id]
    if upcoming_only:
        query += " AND date_to >= ?"
        params.append(from_date or dhaka_today().isoformat())
    query += " ORDER BY date_from, date_to, id"
    with _connect() as conn:
        return _rows_to_dicts(conn.execute(query, params).fetchall())


def is_doctor_off(doctor_id, date):
    """True when ``date`` ('YYYY-MM-DD') falls inside one of the doctor's ranges."""
    if not date:
        return False
    with _connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM doctor_time_off
               WHERE doctor_id = ? AND date_from <= ? AND date_to >= ?
               LIMIT 1""",
            (doctor_id, str(date), str(date)),
        ).fetchone()
    return row is not None


def time_off_reason(doctor_id, date):
    """Return the reason for the range covering ``date``, else ``None``."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT reason FROM doctor_time_off
               WHERE doctor_id = ? AND date_from <= ? AND date_to >= ?
               ORDER BY date_from LIMIT 1""",
            (doctor_id, str(date), str(date)),
        ).fetchone()
    return row["reason"] if row else None


def list_time_off_days(days=30, from_date=None):
    """Return upcoming time-off ranges within ``days``, grouped per doctor.

    Result: ``[{doctor_id, doctor_name_en, doctor_name_bn, date_from, date_to,
    reason}, ...]`` for use in the system prompt.
    """
    start = from_date or dhaka_today().isoformat()
    end = ((from_date and datetime.date.fromisoformat(from_date))
           or dhaka_today()) + datetime.timedelta(days=days)
    with _connect() as conn:
        rows = conn.execute(
            """SELECT t.*, d.name_en, d.name_bn
               FROM doctor_time_off t
               JOIN doctors d ON d.id = t.doctor_id
               WHERE t.date_to >= ? AND t.date_from <= ?
                 AND d.active = 1
               ORDER BY t.date_from, t.id""",
            (start, end.isoformat()),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["doctor_name_en"] = item.pop("name_en")
        item["doctor_name_bn"] = item.pop("name_bn")
        out.append(item)
    return out


def create_time_off(doctor_id, date_from, date_to, reason=None):
    """Add a days-off range. Returns the new id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO doctor_time_off
               (doctor_id, date_from, date_to, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (doctor_id, date_from, date_to, reason, _now()),
        )
    return cur.lastrowid


def delete_time_off(time_off_id):
    """Remove a days-off range. Returns True when a row was deleted."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM doctor_time_off WHERE id = ?",
                           (time_off_id,))
        return cur.rowcount > 0


def get_time_off(time_off_id):
    """Return one days-off range as a dict or ``None``."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM doctor_time_off WHERE id = ?",
                           (time_off_id,)).fetchone()
    return dict(row) if row else None


# ─── CATALOG HELPERS (admin panel) ───────────────────────────────────────────

def count_appointments_for_doctor(doctor_id, upcoming_only=False, from_date=None):
    """Number of non-cancelled appointments for a doctor."""
    query = ("SELECT COUNT(*) AS n FROM appointments "
             "WHERE doctor_id = ? AND status != 'cancelled'")
    params = [doctor_id]
    if upcoming_only:
        query += " AND date >= ?"
        params.append(from_date or dhaka_today().isoformat())
    with _connect() as conn:
        return conn.execute(query, params).fetchone()["n"]


def count_appointments_for_service(service_id):
    """Number of non-cancelled appointments using a service."""
    with _connect() as conn:
        return conn.execute(
            """SELECT COUNT(*) AS n FROM appointments
               WHERE service_id = ? AND status != 'cancelled'""",
            (service_id,),
        ).fetchone()["n"]


def count_all_appointments_for_doctor(doctor_id):
    """Every appointment row for a doctor, cancelled ones included.

    Used to decide whether a doctor may be deleted: a doctor that was ever
    booked is kept so the history still makes sense.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM appointments WHERE doctor_id = ?",
            (doctor_id,),
        ).fetchone()["n"]


def count_all_appointments_for_service(service_id):
    """Every appointment row for a service, cancelled ones included."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM appointments WHERE service_id = ?",
            (service_id,),
        ).fetchone()["n"]


def list_upcoming_appointments(doctor_id=None, service_id=None, from_date=None):
    """Non-cancelled appointments today or later, soonest first."""
    query = ("SELECT * FROM appointments WHERE status != 'cancelled' "
             "AND date >= ?")
    params = [from_date or dhaka_today().isoformat()]
    if doctor_id is not None:
        query += " AND doctor_id = ?"
        params.append(doctor_id)
    if service_id is not None:
        query += " AND service_id = ?"
        params.append(service_id)
    query += " ORDER BY date, time, id"
    with _connect() as conn:
        return _rows_to_dicts(conn.execute(query, params).fetchall())


def set_schedule_atomic(doctor_id, schedule):
    """Replace a doctor's weekly schedule in one transaction.

    Either every new range is written or none are: a failure rolls back to the
    previous schedule. Returns the doctor after the swap.
    """
    slots = list(schedule or [])
    with _connect_tx() as conn:
        conn.execute("DELETE FROM doctor_schedule WHERE doctor_id = ?",
                     (doctor_id,))
        for slot in slots:
            conn.execute(
                """INSERT INTO doctor_schedule
                   (doctor_id, weekday, start_time, end_time)
                   VALUES (?, ?, ?, ?)""",
                (doctor_id, slot["weekday"], slot["start_time"],
                 slot["end_time"]),
            )
    return get_doctor(doctor_id)


def save_doctor_with_schedule(doctor_id, schedule=None, **fields):
    """Update a doctor's fields and (optionally) schedule in ONE transaction.

    Used by the admin panel so a bad schedule can never half-apply: the field
    update and the schedule swap commit together or not at all.
    """
    allowed = {"name_en", "name_bn", "specialty_en", "specialty_bn",
               "bio_en", "bio_bn", "slot_minutes", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    slots = None if schedule is None else list(schedule)
    with _connect_tx() as conn:
        if updates:
            assignments = ", ".join("%s = ?" % k for k in updates)
            conn.execute(
                "UPDATE doctors SET %s WHERE id = ?" % assignments,
                (*updates.values(), doctor_id),
            )
        if slots is not None:
            conn.execute("DELETE FROM doctor_schedule WHERE doctor_id = ?",
                         (doctor_id,))
            for slot in slots:
                conn.execute(
                    """INSERT INTO doctor_schedule
                       (doctor_id, weekday, start_time, end_time)
                       VALUES (?, ?, ?, ?)""",
                    (doctor_id, slot["weekday"], slot["start_time"],
                     slot["end_time"]),
                )
    return get_doctor(doctor_id)


# ─── SERVICES ─────────────────────────────────────────────────────────────────

def list_services(active_only=False):
    """Return services as dicts; when ``active_only`` only active ones."""
    query = "SELECT * FROM services"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY id"
    with _connect() as conn:
        return _rows_to_dicts(conn.execute(query).fetchall())


def get_service(service_id):
    """Return one service as a dict or ``None``."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    return dict(row) if row else None


def create_service(name_en=None, name_bn=None, price=None, price_max=None,
                   duration_minutes=30, active=1):
    """Create a service. Returns the new id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO services
               (name_en, name_bn, price, price_max, duration_minutes, active)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name_en, name_bn, price, price_max, duration_minutes, active),
        )
    return cur.lastrowid


def update_service(service_id, **fields):
    """Update allowed service fields. Returns the updated dict or None."""
    allowed = {"name_en", "name_bn", "price", "price_max", "duration_minutes", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        with _connect() as conn:
            conn.execute(
                f"UPDATE services SET {assignments} WHERE id = ?",
                (*updates.values(), service_id),
            )
    return get_service(service_id)


def delete_service(service_id):
    """Delete a service. Returns True if a row was deleted."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM services WHERE id = ?", (service_id,))
        return cur.rowcount > 0


# ─── APPOINTMENTS ─────────────────────────────────────────────────────────────

def list_appointments(status=None, date=None, doctor_id=None):
    """Return appointments (newest first), optionally filtered."""
    query = "SELECT * FROM appointments WHERE 1 = 1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if date:
        query += " AND date = ?"
        params.append(date)
    if doctor_id:
        query += " AND doctor_id = ?"
        params.append(doctor_id)
    query += " ORDER BY id DESC"
    with _connect() as conn:
        return _rows_to_dicts(conn.execute(query, params).fetchall())


def get_appointment(appointment_id):
    """Return one appointment as a dict or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
    return dict(row) if row else None


def create_appointment(patient_name=None, phone=None, doctor_id=None, service_id=None,
                       date=None, time=None, status="pending", notes=None,
                       language=None, session_id=None):
    """Create an appointment. Returns the new id."""
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO appointments
               (patient_name, phone, doctor_id, service_id, date, time,
                status, notes, language, session_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (patient_name, phone, doctor_id, service_id, date, time,
             status, notes, language, session_id, now, now),
        )
    return cur.lastrowid


def book_appointment(patient_name, phone, doctor_id, service_id, date, time,
                     notes=None, language=None, session_id=None):
    """Atomically book a slot, returning the new id or ``None`` if taken.

    Runs inside a ``BEGIN IMMEDIATE`` transaction and re-checks for an
    existing booking for the same doctor/date/time, so two simultaneous
    requests cannot double-book the same slot.
    """
    now = _now()
    with _connect_tx() as conn:
        clash = conn.execute(
            """SELECT 1 FROM appointments
               WHERE doctor_id = ? AND date = ? AND time = ? AND status != 'cancelled'""",
            (doctor_id, date, time),
        ).fetchone()
        if clash:
            return None
        cur = conn.execute(
            """INSERT INTO appointments
               (patient_name, phone, doctor_id, service_id, date, time,
                status, notes, language, session_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (patient_name, phone, doctor_id, service_id, date, time,
             "pending", notes, language, session_id, now, now),
        )
        return cur.lastrowid


def update_appointment(appointment_id, **fields):
    """Update allowed appointment fields, refreshing updated_at.

    Returns the updated dict or ``None``.
    """
    allowed = {"patient_name", "phone", "doctor_id", "service_id", "date",
               "time", "status", "notes", "language", "session_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with _connect() as conn:
        conn.execute(
            f"UPDATE appointments SET {assignments} WHERE id = ?",
            (*updates.values(), appointment_id),
        )
    return get_appointment(appointment_id)


def delete_appointment(appointment_id):
    """Delete an appointment. Returns True if a row was deleted."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
        return cur.rowcount > 0


# ─── CONVERSATIONS ────────────────────────────────────────────────────────────
#
# A conversation is the unit of human handoff. Its ``status`` is one of:
#   bot        — the AI is answering
#   waiting    — the customer asked for a human and is queued
#   with_agent — a human agent has accepted and is chatting
#   closed     — finished; the next customer message starts a new conversation
#
# All transitions run inside ``BEGIN IMMEDIATE`` transactions so concurrent
# agents/customers cannot corrupt the state.

RESET_MARKER = "__RESET__"

# Map legacy ``role`` values to the new ``sender_type`` values (and back).
ROLE_BY_SENDER = {"customer": "user", "bot": "assistant",
                  "agent": "assistant", "system": "system"}

HANDOFF_TIMEOUT_MESSAGE = (
    "Sorry, no agent is free right now. You can leave your name and number for "
    "a callback and we'll call you back.",
    "দুঃখিত, এখন কোনো এজেন্ট ফ্রি নেই। আপনার নাম আর নম্বর দিলে আমরা ফোন করে জানাব।",
)
IDLE_CLOSE_MESSAGE = (
    "This chat was closed due to inactivity.",
    "অনেকক্ষণ কোনো বার্তা না আসায় কথোপকথনটি বন্ধ করা হলো।",
)
AGENT_BACK_MESSAGE = (
    "The assistant is back and can help you again.",
    "সহকারী আবার ফিরে এসেছে এবং আপনাকে সাহায্য করতে পারবে।",
)
CONVERSATION_CLOSED_MESSAGE = (
    "This conversation has been closed.",
    "এই কথোপকথনটি বন্ধ করা হয়েছে।",
)
CUSTOMER_LEFT_MESSAGE = (
    "Customer left the chat — closed after inactivity.",
    "গ্রাহক চ্যাট ছেড়ে চলে গেছেন — নিষ্ক্রিয়তার কারণে বন্ধ করা হলো।",
)


def _msg(conversation, en, bn):
    """Pick the Bengali or English variant based on the conversation language."""
    lang = conversation["lang"] if conversation is not None else None
    return bn if lang == "bn" else en


def _agent_display_name(conn, agent_id):
    """Return an agent's display name (falling back to username)."""
    if agent_id is None:
        return "Agent"
    row = conn.execute(
        "SELECT username, display_name FROM admins WHERE id = ?", (agent_id,)
    ).fetchone()
    if not row:
        return "Agent"
    return row["display_name"] or row["username"]


def _insert_message(conn, conversation_id, session_id, sender_type, content,
                    agent_id=None, created_at=None):
    """Insert a chat message and bump the conversation's last_message_at."""
    created = created_at or _now()
    role = ROLE_BY_SENDER.get(sender_type, sender_type)
    cur = conn.execute(
        """INSERT INTO chat_messages
           (session_id, role, content, created_at, conversation_id, sender_type, agent_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, role, content, created, conversation_id, sender_type, agent_id),
    )
    conn.execute(
        "UPDATE conversations SET last_message_at = ? WHERE id = ?",
        (created, conversation_id),
    )
    return cur.lastrowid


def get_conversation(conversation_id):
    """Return one conversation as a dict, or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    return dict(row) if row else None


def get_current_conversation(session_id):
    """Return the newest non-closed conversation for a session, or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM conversations
               WHERE session_id = ? AND status != 'closed'
               ORDER BY id DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def get_latest_conversation(session_id):
    """Return the newest conversation for a session (closed or not), or ``None``.

    Used to restore the widget after a page refresh: unlike
    :func:`get_current_conversation` this also returns a closed conversation, so
    the widget can show its final state.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM conversations
               WHERE session_id = ? ORDER BY id DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def touch_customer_seen(session_id):
    """Record that the customer's widget just reached the server.

    Called on every ``/chat``, ``/chat/poll``, ``/chat/history`` and ``/handoff``
    request for a session. Returns the number of open conversations updated.
    """
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE conversations SET customer_last_seen = ?
               WHERE session_id = ? AND status != 'closed'""",
            (now, session_id),
        )
        return cur.rowcount


def session_exists(session_id):
    """Return True when a session has ever had a conversation (i.e. was issued)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM conversations WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone() is not None


def get_or_create_conversation(session_id, lang=None):
    """Return the open conversation for ``session_id``, creating one if needed.

    A closed conversation is never returned; the next message starts a fresh
    one, which is how reset/close is implemented.
    """
    now = _now()
    with _connect_tx() as conn:
        row = conn.execute(
            """SELECT * FROM conversations
               WHERE session_id = ? AND status != 'closed'
               ORDER BY id DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row:
            if lang and not row["lang"]:
                conn.execute(
                    "UPDATE conversations SET lang = ? WHERE id = ?", (lang, row["id"])
                )
                row = conn.execute(
                    "SELECT * FROM conversations WHERE id = ?", (row["id"],)
                ).fetchone()
            return dict(row)
        cur = conn.execute(
            """INSERT INTO conversations
               (business_id, session_id, status, lang, created_at, last_message_at)
               VALUES (1, ?, 'bot', ?, ?, ?)""",
            (session_id, lang, now, now),
        )
        created_id = cur.lastrowid
        return dict(conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (created_id,)
        ).fetchone())


def add_message(conversation_id, sender_type, content, agent_id=None):
    """Append a message of any sender type to a conversation. Returns its id."""
    if not content:
        return None
    with _connect() as conn:
        conv = conn.execute(
            "SELECT session_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not conv:
            return None
        return _insert_message(
            conn, conversation_id, conv["session_id"], sender_type, content, agent_id
        )


def customer_send_message(conversation_id, content):
    """Store a customer message while the conversation is ``with_agent``."""
    if not content:
        return None
    with _connect_tx() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not conv or conv["status"] != "with_agent":
            return None
        return _insert_message(
            conn, conversation_id, conv["session_id"], "customer", content
        )


def agent_send_message(conversation_id, agent_id, content):
    """Store an agent message when the conversation is assigned to them."""
    if not content:
        return None
    with _connect_tx() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not conv or conv["status"] != "with_agent" or conv["agent_id"] != agent_id:
            return None
        return _insert_message(
            conn, conversation_id, conv["session_id"], "agent", content, agent_id
        )


def _connection_setting(conn, key, default):
    """Read a setting value using an existing connection."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _online_agents(conn):
    """Count agents/owners whose heartbeat is fresh enough to be 'online'."""
    try:
        window = int(float(_connection_setting(conn, "agent_online_window_seconds", 45)))
    except (TypeError, ValueError):
        window = 45
    cutoff = (dhaka_now() - datetime.timedelta(seconds=window)).isoformat()
    return conn.execute(
        """SELECT COUNT(*) AS n FROM admins
           WHERE is_online = 1 AND role IN ('agent', 'owner')
             AND last_seen IS NOT NULL AND last_seen >= ?""",
        (cutoff,),
    ).fetchone()["n"]


def _queue_position(conn, conversation):
    """Return this conversation's 1-based position in the waiting queue."""
    return conn.execute(
        """SELECT COUNT(*) AS n FROM conversations
           WHERE status = 'waiting'
             AND handoff_requested_at <= ?""",
        (conversation["handoff_requested_at"],),
    ).fetchone()["n"]


def request_handoff(conversation_id, reason=None):
    """Queue a conversation for a human agent.

    Returns ``{"queued": bool, "position": int|None, "status": str}``. If at
    least one agent is online the conversation becomes ``waiting`` (never
    creating a duplicate queue entry); otherwise the status stays ``bot``.
    """
    now = _now()
    with _connect_tx() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not conv:
            return {"queued": False, "position": None, "status": None}
        if conv["status"] == "closed":
            return {"queued": False, "position": None, "status": "closed"}
        if conv["status"] == "with_agent":
            return {"queued": True, "position": 0, "status": "with_agent",
                    "already": True}
        if conv["status"] == "waiting":
            return {"queued": True, "position": _queue_position(conn, conv),
                    "status": "waiting", "already": True}

        if _online_agents(conn):
            conn.execute(
                """UPDATE conversations
                   SET status = 'waiting', handoff_reason = ?,
                       handoff_requested_at = ?, last_message_at = ?
                   WHERE id = ?""",
                (reason, now, now, conversation_id),
            )
            conv = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            return {"queued": True, "position": _queue_position(conn, conv),
                    "status": "waiting"}
        # Nobody online: remember the reason but stay with the bot.
        conn.execute(
            "UPDATE conversations SET handoff_reason = ? WHERE id = ?",
            (reason, conversation_id),
        )
        return {"queued": False, "position": None, "status": "bot"}


def accept_conversation(conversation_id, agent_id):
    """Claim a waiting conversation for an agent. Returns True only once.

    The conditional ``UPDATE`` guarantees two racing agents cannot both win.
    On success a "<name> joined the chat" system message is stored.
    """
    now = _now()
    with _connect_tx() as conn:
        cur = conn.execute(
            """UPDATE conversations
               SET status = 'with_agent', agent_id = ?, accepted_at = ?
               WHERE id = ? AND status = 'waiting' AND agent_id IS NULL""",
            (agent_id, now, conversation_id),
        )
        if cur.rowcount != 1:
            return False
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        name = _agent_display_name(conn, agent_id)
        _insert_message(
            conn, conversation_id, conv["session_id"], "system",
            "%s joined the chat." % name, created_at=now,
        )
        return True


def hand_back_to_bot(conversation_id, agent_id):
    """Return a ``with_agent`` conversation to the bot. Returns True on success."""
    now = _now()
    with _connect_tx() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not conv or conv["status"] != "with_agent":
            return False
        cur = conn.execute(
            """UPDATE conversations
               SET status = 'bot', agent_id = NULL, last_message_at = ?
               WHERE id = ? AND status = 'with_agent'""",
            (now, conversation_id),
        )
        if cur.rowcount != 1:
            return False
        _insert_message(
            conn, conversation_id, conv["session_id"], "system",
            _msg(conv, *AGENT_BACK_MESSAGE), created_at=now,
        )
        return True


def close_conversation(conversation_id, by="system"):
    """Close a conversation and record a system message. Returns True on success."""
    now = _now()
    with _connect_tx() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not conv or conv["status"] == "closed":
            return False
        cur = conn.execute(
            """UPDATE conversations
               SET status = 'closed', closed_at = ?, last_message_at = ?
               WHERE id = ? AND status != 'closed'""",
            (now, now, conversation_id),
        )
        if cur.rowcount != 1:
            return False
        _insert_message(
            conn, conversation_id, conv["session_id"], "system",
            _msg(conv, *CONVERSATION_CLOSED_MESSAGE), created_at=now,
        )
        return True


def run_housekeeping():
    """Apply time-based transitions lazily (no background threads).

    - ``waiting`` whose customer has not been seen for more than
      ``customer_left_seconds`` -> ``closed`` (customer left).
    - ``waiting`` longer than ``handoff_timeout_minutes`` -> back to ``bot``
      with a system message offering a callback.
    - ``with_agent`` idle longer than ``idle_close_minutes`` -> ``closed``.

    Safe to call repeatedly; returns a small summary dict.
    """
    now = dhaka_now()
    now_iso = now.isoformat()
    timed_out = closed = left = 0
    with _connect_tx() as conn:
        # A waiting customer who stopped reaching the server (closed the tab) is
        # treated as gone and the conversation is closed.
        try:
            gone = int(float(_connection_setting(conn, "customer_left_seconds", 300)))
        except (TypeError, ValueError):
            gone = 120
        gone_cutoff = (now - datetime.timedelta(seconds=gone)).isoformat()
        gone_rows = conn.execute(
            """SELECT * FROM conversations
               WHERE status = 'waiting'
                 AND customer_last_seen IS NOT NULL
                 AND customer_last_seen <= ?""",
            (gone_cutoff,),
        ).fetchall()
        for conv in gone_rows:
            conn.execute(
                "UPDATE conversations SET status = 'closed', agent_id = NULL, "
                "closed_at = ?, last_message_at = ? WHERE id = ?",
                (now_iso, now_iso, conv["id"]),
            )
            _insert_message(
                conn, conv["id"], conv["session_id"], "system",
                _msg(conv, *CUSTOMER_LEFT_MESSAGE), created_at=now_iso,
            )
            left += 1

        try:
            timeout = int(float(_connection_setting(conn, "handoff_timeout_minutes", 5)))
        except (TypeError, ValueError):
            timeout = 5
        cutoff = (now - datetime.timedelta(minutes=timeout)).isoformat()
        waiting = conn.execute(
            """SELECT * FROM conversations
               WHERE status = 'waiting'
                 AND handoff_requested_at IS NOT NULL
                 AND handoff_requested_at <= ?""",
            (cutoff,),
        ).fetchall()
        for conv in waiting:
            conn.execute(
                "UPDATE conversations SET status = 'bot', agent_id = NULL, "
                "last_message_at = ? WHERE id = ?", (now_iso, conv["id"]),
            )
            _insert_message(
                conn, conv["id"], conv["session_id"], "system",
                _msg(conv, *HANDOFF_TIMEOUT_MESSAGE), created_at=now_iso,
            )
            timed_out += 1

        try:
            idle = int(float(_connection_setting(conn, "idle_close_minutes", 30)))
        except (TypeError, ValueError):
            idle = 30
        cutoff2 = (now - datetime.timedelta(minutes=idle)).isoformat()
        idle_rows = conn.execute(
            """SELECT * FROM conversations
               WHERE status = 'with_agent'
                 AND COALESCE(last_message_at, accepted_at) <= ?""",
            (cutoff2,),
        ).fetchall()
        for conv in idle_rows:
            conn.execute(
                "UPDATE conversations SET status = 'closed', closed_at = ?, "
                "last_message_at = ? WHERE id = ?", (now_iso, now_iso, conv["id"]),
            )
            _insert_message(
                conn, conv["id"], conv["session_id"], "system",
                _msg(conv, *IDLE_CLOSE_MESSAGE), created_at=now_iso,
            )
            closed += 1
    return {"timed_out": timed_out, "idle_closed": closed, "customer_left": left}


def create_callback_request(conversation_id, name, phone, message):
    """Create a callback request (validates the phone). Returns the new id.

    Raises ``ValueError`` when the phone number is not a valid BD mobile.
    """
    if not is_valid_phone(phone):
        raise ValueError("invalid phone")
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO callback_requests
               (business_id, conversation_id, name, phone, message, status, created_at)
               VALUES (1, ?, ?, ?, ?, 'new', ?)""",
            (conversation_id, name, normalize_phone(phone), message, _now()),
        )
    return cur.lastrowid


def list_callback_requests(status=None):
    """Return callback requests (newest first), optionally filtered."""
    query = "SELECT * FROM callback_requests"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC"
    with _connect() as conn:
        return _rows_to_dicts(conn.execute(query, params).fetchall())


def get_callback_request(callback_id):
    """Return a single callback request, or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM callback_requests WHERE id = ?", (callback_id,)
        ).fetchone()
    return dict(row) if row else None


def set_callback_status(callback_id, status):
    """Update a callback request's status. Returns True when a row changed.

    ``status`` must be one of :data:`CALLBACK_STATUSES`.
    """
    if status not in CALLBACK_STATUSES:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE callback_requests SET status = ? WHERE id = ?",
            (status, callback_id),
        )
        return cur.rowcount > 0


def list_callback_requests_for_conversation(conversation_id):
    """Return the callback requests left against a conversation."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM callback_requests
               WHERE conversation_id = ? ORDER BY id DESC""",
            (conversation_id,),
        ).fetchall()
    return _rows_to_dicts(rows)


def list_appointments_by_session(session_id):
    """Return appointments booked through a customer session (newest first)."""
    if not session_id:
        return []
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE session_id = ? ORDER BY id DESC""",
            (session_id,),
        ).fetchall()
    return _rows_to_dicts(rows)


def _wait_seconds(since_iso, now):
    """Return whole seconds between an ISO timestamp and ``now`` (>= 0)."""
    if not since_iso:
        return 0
    try:
        since = datetime.datetime.fromisoformat(since_iso)
    except (TypeError, ValueError):
        return 0
    return max(0, int((now - since).total_seconds()))


def _is_away(last_seen_iso, now, threshold_seconds):
    """Return True when ``last_seen_iso`` is older than ``threshold_seconds``.

    A missing timestamp counts as "not away" (unknown), so legacy rows are not
    flagged.
    """
    if not last_seen_iso:
        return False
    return _wait_seconds(last_seen_iso, now) > threshold_seconds


def _conversation_summaries(rows, now):
    """Attach last-message preview, wait time and customer-away flag to rows."""
    try:
        away_seconds = int(float(get_setting("customer_away_seconds", 45)))
    except (TypeError, ValueError):
        away_seconds = 45
    out = _rows_to_dicts(rows)
    for conv in out:
        conv["wait_seconds"] = _wait_seconds(conv.get("handoff_requested_at"), now)
        conv["customer_away"] = _is_away(conv.get("customer_last_seen"),
                                         now, away_seconds)
    return out


_SUMMARY_SELECT = """
    SELECT c.*,
           (SELECT content FROM chat_messages m
             WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_message,
           (SELECT sender_type FROM chat_messages m
             WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_sender
      FROM conversations c
"""


def list_waiting():
    """Return waiting conversations (FIFO) with preview and wait time."""
    now = dhaka_now()
    with _connect() as conn:
        rows = conn.execute(
            _SUMMARY_SELECT + " WHERE c.status = 'waiting' "
            "ORDER BY c.handoff_requested_at ASC"
        ).fetchall()
    return _conversation_summaries(rows, now)


def list_my_conversations(agent_id):
    """Return the conversations currently assigned to an agent."""
    now = dhaka_now()
    with _connect() as conn:
        rows = conn.execute(
            _SUMMARY_SELECT + " WHERE c.status = 'with_agent' AND c.agent_id = ? "
            "ORDER BY c.last_message_at DESC",
            (agent_id,),
        ).fetchall()
    return _conversation_summaries(rows, now)


def queue_position(conversation_id):
    """Return the 1-based waiting-queue position, or ``None`` if not waiting."""
    conv = get_conversation(conversation_id)
    if not conv or conv["status"] != "waiting":
        return None
    with _connect() as conn:
        return _queue_position(conn, conv)


def get_conversation_with_messages(conversation_id, after_id=None):
    """Return a conversation plus its transcript (optionally after ``after_id``).

    Each message carries ``sender_type`` and, for agent messages, the agent's
    display name. Returns ``None`` when the conversation does not exist.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not row:
            return None
        conversation = dict(row)
        conversation["agent_display_name"] = (
            _agent_display_name(conn, conversation["agent_id"])
            if conversation["agent_id"] else None
        )
        query = (
            """SELECT m.*, a.username AS agent_username,
                      a.display_name AS agent_display_name
                 FROM chat_messages m
                 LEFT JOIN admins a ON a.id = m.agent_id
                WHERE m.conversation_id = ?"""
        )
        params = [conversation_id]
        if after_id:
            query += " AND m.id > ?"
            params.append(after_id)
        query += " ORDER BY m.id ASC"
        messages = _rows_to_dicts(conn.execute(query, params).fetchall())
    return {"conversation": conversation, "messages": messages}


def get_conversation_history(conversation_id, limit=12):
    """Return recent messages for the AI as ``{"role", "content"}`` dicts.

    Customer -> user, bot/agent -> assistant; system messages are skipped.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM (
                   SELECT * FROM chat_messages
                   WHERE conversation_id = ?
                     AND sender_type IN ('customer', 'bot', 'agent')
                   ORDER BY id DESC LIMIT ?
               ) ORDER BY id ASC""",
            (conversation_id, limit),
        ).fetchall()
    return [
        {"role": "user" if r["sender_type"] == "customer" else "assistant",
         "content": r["content"]}
        for r in rows
    ]


def add_chat_message(session_id, role, content):
    """Append a message to the session's current conversation. Returns its id."""
    sender = SENDER_BY_ROLE.get(role, role)
    conv = get_or_create_conversation(session_id)
    return add_message(conv["id"], sender, content)


def reset_conversation(session_id):
    """Close the session's current conversation; the next message starts fresh.

    The message log is never deleted. Returns the closed conversation id, or
    ``None`` when there was nothing to close.
    """
    conv = get_current_conversation(session_id)
    if not conv:
        return None
    close_conversation(conv["id"], by="customer")
    return conv["id"]


def get_recent_messages(session_id, limit=12):
    """Return the last ``limit`` customer/bot/agent messages, oldest first.

    Uses the session's current (non-closed) conversation, so a reset — which
    closes the current conversation — hides earlier messages.
    """
    conv = get_current_conversation(session_id)
    if not conv:
        return []
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM (
                   SELECT * FROM chat_messages
                   WHERE conversation_id = ?
                     AND sender_type IN ('customer', 'bot', 'agent')
                   ORDER BY id DESC LIMIT ?
               ) ORDER BY id ASC""",
            (conv["id"], limit),
        ).fetchall()
    return _rows_to_dicts(rows)


# ─── ADMINS / AGENTS ──────────────────────────────────────────────────────────

def list_admins():
    """Return admin accounts (id + username) as dicts."""
    with _connect() as conn:
        rows = conn.execute("SELECT id, username FROM admins ORDER BY id").fetchall()
    return _rows_to_dicts(rows)


def list_agents():
    """Return agent/owner accounts (without password hashes)."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, username, display_name, role, is_online, last_seen
               FROM admins ORDER BY id"""
        ).fetchall()
    return _rows_to_dicts(rows)


def get_admin(username):
    """Return an admin row (including password_hash) or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM admins WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_admin_by_id(admin_id):
    """Return an admin row (without the hash) or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT id, username, display_name, role, is_online, last_seen,
                      must_change_password
               FROM admins WHERE id = ?""",
            (admin_id,),
        ).fetchone()
    return dict(row) if row else None


def create_admin(username, password, display_name=None, role="owner",
                 must_change_password=0):
    """Create an admin/agent with a hashed password. Returns the new id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO admins(username, password_hash, display_name, role,
                                  must_change_password)
               VALUES (?, ?, ?, ?, ?)""",
            (username, generate_password_hash(password), display_name, role,
             1 if must_change_password else 0),
        )
    return cur.lastrowid


def update_admin(admin_id, username=None, password=None, display_name=None,
                 role=None, must_change_password=None):
    """Update an admin's username/password/display_name/role/flag. Returns True."""
    updates = {}
    if username:
        updates["username"] = username
    if password:
        updates["password_hash"] = generate_password_hash(password)
    if display_name is not None:
        updates["display_name"] = display_name
    if role:
        updates["role"] = role
    if must_change_password is not None:
        updates["must_change_password"] = 1 if must_change_password else 0
    if not updates:
        return False
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE admins SET {assignments} WHERE id = ?",
            (*updates.values(), admin_id),
        )
        return cur.rowcount > 0


def set_must_change_password(admin_id, flag=True):
    """Turn the forced-password-change flag on or off. Returns True."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE admins SET must_change_password = ? WHERE id = ?",
            (1 if flag else 0, admin_id),
        )
        return cur.rowcount > 0


def set_agent_status(agent_id, is_online, touch=True):
    """Record an agent's online flag and heartbeat timestamp."""
    now = _now()
    with _connect() as conn:
        if touch:
            conn.execute(
                "UPDATE admins SET is_online = ?, last_seen = ? WHERE id = ?",
                (1 if is_online else 0, now, agent_id),
            )
        else:
            conn.execute(
                "UPDATE admins SET is_online = ? WHERE id = ?",
                (1 if is_online else 0, agent_id),
            )
    return get_admin_by_id(agent_id)


def heartbeat(agent_id, is_online=True):
    """Update an agent's heartbeat (called by the agent UI periodically)."""
    return set_agent_status(agent_id, is_online, touch=True)


def delete_admin(admin_id):
    """Delete an admin account. Returns True if a row was deleted."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
        return cur.rowcount > 0


def verify_admin(username, password):
    """Return the admin dict when credentials match, else ``None``."""
    admin = get_admin(username)
    if admin and check_password_hash(admin["password_hash"], password):
        return admin
    return None


def ensure_admin(username, password):
    """Seed a single owner admin account when the admins table is empty.

    The seeded owner starts with ``must_change_password = 1`` — the very first
    login forces a password change.
    """
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]
    if count == 0:
        create_admin(username, password, display_name=username, role="owner",
                     must_change_password=1)
        print(f"[db] Seeded admin account '{username}' (password hashed).")


def _flag_default_password_owners(username, password):
    """Force a password change for owners still using the configured default.

    Newly seeded owners already get the flag from :func:`ensure_admin`; this
    catches accounts created before this release whose password is still the
    configured default. Owners who changed their password are left alone.
    """
    if not username or not password:
        return
    flagged = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, username, password_hash FROM admins WHERE role = 'owner'"
        ).fetchall()
        for row in rows:
            try:
                still_default = check_password_hash(row["password_hash"], password)
            except (ValueError, TypeError):
                continue
            if not still_default:
                continue
            conn.execute(
                "UPDATE admins SET must_change_password = 1 WHERE id = ?",
                (row["id"],),
            )
            flagged.append(row["username"])
    if flagged:
        print("[db] must_change_password=1 for owner(s) still using the default "
              "password: %s" % ", ".join(flagged))
