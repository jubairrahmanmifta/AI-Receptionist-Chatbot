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

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role       TEXT,
    content    TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages(session_id);

CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE,
    password_hash TEXT
);
"""


def _create_tables():
    """Create every table and index if they do not already exist."""
    with _connect() as conn:
        conn.executescript(SCHEMA)


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

def _migrate_columns():
    """Add columns introduced after the first release (safe to re-run)."""
    with _connect() as conn:
        service_cols = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
        if "price_max" not in service_cols:
            conn.execute("ALTER TABLE services ADD COLUMN price_max REAL")
            print("[db] Added column services.price_max")
        appt_cols = {r["name"] for r in conn.execute("PRAGMA table_info(appointments)")}
        if "session_id" not in appt_cols:
            conn.execute("ALTER TABLE appointments ADD COLUMN session_id TEXT")
            print("[db] Added column appointments.session_id")


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
    """Create tables, migrate columns, seed data, seed admin, migrate JSON.

    Safe to call on every app startup: table creation uses ``IF NOT EXISTS``,
    column migrations check for the column first, seeding only happens when the
    settings table is empty, and the legacy JSON migration only runs while
    ``appointments.json`` still exists.
    """
    _create_tables()
    _seed_if_empty(ai_model)
    _migrate_columns()
    _backfill_price_max()
    if admin_username and admin_password:
        ensure_admin(admin_username, admin_password)
    _migrate_appointments_json()


# ─── SETTINGS ─────────────────────────────────────────────────────────────────

def get_settings():
    """Return all settings as a ``{key: value}`` dict."""
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_setting(key, value):
    """Insert or update a single setting by key."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


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
    removed too.
    """
    doctor = get_doctor(doctor_id)
    if not doctor:
        return []
    try:
        day = datetime.date.fromisoformat(date)
    except (ValueError, TypeError):
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


# ─── CHAT MESSAGES ────────────────────────────────────────────────────────────

RESET_MARKER = "__RESET__"


def add_chat_message(session_id, role, content):
    """Append a chat message to the log. Returns the new id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (session_id, role, content, created_at)
               VALUES (?, ?, ?, ?)""",
            (session_id, role, content, _now()),
        )
    return cur.lastrowid


def reset_conversation(session_id):
    """Start a fresh conversation without deleting the message log.

    Inserts a reset marker; ``get_recent_messages`` ignores everything before
    the most recent marker.
    """
    return add_chat_message(session_id, "system", RESET_MARKER)


def get_recent_messages(session_id, limit=12):
    """Return the last ``limit`` user/assistant messages, oldest first.

    Messages before the most recent reset marker are ignored, so a reset
    starts a fresh conversation without destroying history.
    """
    with _connect() as conn:
        marker = conn.execute(
            """SELECT MAX(id) AS m FROM chat_messages
               WHERE session_id = ? AND role = 'system' AND content = ?""",
            (session_id, RESET_MARKER),
        ).fetchone()["m"] or 0
        rows = conn.execute(
            """SELECT * FROM (
                   SELECT * FROM chat_messages
                   WHERE session_id = ? AND id > ?
                     AND role IN ('user', 'assistant')
                   ORDER BY id DESC
                   LIMIT ?
               ) ORDER BY id ASC""",
            (session_id, marker, limit),
        ).fetchall()
    return _rows_to_dicts(rows)


# ─── ADMINS ───────────────────────────────────────────────────────────────────

def list_admins():
    """Return admin accounts (id + username) as dicts."""
    with _connect() as conn:
        rows = conn.execute("SELECT id, username FROM admins ORDER BY id").fetchall()
    return _rows_to_dicts(rows)


def get_admin(username):
    """Return an admin row (including password_hash) or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM admins WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def create_admin(username, password):
    """Create an admin with a hashed password. Returns the new id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO admins(username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
    return cur.lastrowid


def update_admin(admin_id, username=None, password=None):
    """Update an admin's username and/or password (re-hashed). Returns True."""
    updates = {}
    if username:
        updates["username"] = username
    if password:
        updates["password_hash"] = generate_password_hash(password)
    if not updates:
        return False
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE admins SET {assignments} WHERE id = ?",
            (*updates.values(), admin_id),
        )
        return cur.rowcount > 0


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
    """Seed a single admin account when the admins table is empty."""
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]
    if count == 0:
        create_admin(username, password)
        print(f"[db] Seeded admin account '{username}' (password hashed).")
