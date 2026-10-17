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

from werkzeug.security import generate_password_hash, check_password_hash


DB_FILE = "clinic.db"
APPOINTMENTS_JSON = "appointments.json"
APPOINTMENTS_MIGRATED = "appointments.json.migrated"


# ─── CONNECTION ───────────────────────────────────────────────────────────────

@contextmanager
def _connect():
    """Yield a SQLite connection, committing on success and closing always."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now():
    """Return the current local timestamp as an ISO-8601 string."""
    return datetime.datetime.now().isoformat()


def _rows_to_dicts(rows):
    """Convert a list of ``sqlite3.Row`` objects into plain dicts."""
    return [dict(r) for r in rows]


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


# ─── SEED EXTRACTION (from the hardcoded system prompts) ──────────────────────

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

_EN_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_BN_WEEKDAYS = {
    "সোম": 0, "মঙ্গল": 1, "বুধ": 2, "বৃহস্পতি": 3,
    "শুক্র": 4, "শনি": 5, "রবিবার": 6, "রবি": 6,
}


def _en_hour(hour, meridiem):
    """Convert a 12-hour English hour + AM/PM to a 24-hour integer."""
    hour = int(hour)
    meridiem = meridiem.upper()
    if meridiem == "PM" and hour != 12:
        hour += 12
    if meridiem == "AM" and hour == 12:
        hour = 0
    return hour


def _en_time(text):
    """Parse '10 AM' / '2 PM' into 'HH:MM'."""
    m = re.search(r"(\d{1,2})\s*(AM|PM)", text, re.IGNORECASE)
    if not m:
        return None
    return "%02d:00" % _en_hour(m.group(1), m.group(2))


def _bn_period_hour(period, hour):
    """Convert a Bengali period word + hour to a 24-hour integer."""
    hour = int(hour.translate(_BN_DIGITS))
    if period in ("সকাল",):            # morning
        return hour
    if period in ("দুপুর",):           # noon
        return 12 if hour == 12 else hour + 12
    if period in ("বিকাল", "বিকেল"):    # afternoon
        return 12 if hour == 12 else hour + 12
    if period in ("সন্ধ্যা", "রাত"):    # evening / night
        return 0 if hour == 12 else hour + 12
    return hour


def _bn_time(text):
    """Parse 'সকাল ১০টা' / 'বিকাল ৪টা' into 'HH:MM'."""
    m = re.search(r"(সকাল|দুপুর|বিকাল|বিকেল|সন্ধ্যা|রাত)\s*([০-৯]+)\s*টা", text)
    if not m:
        return None
    return "%02d:00" % _bn_period_hour(m.group(1), m.group(2))


def _en_weekdays(text):
    """Parse 'Mon, Wed, Fri' into [0, 2, 4]."""
    days = []
    for token in re.split(r"[,\s]+", text.strip()):
        key = token[:3].lower()
        if key in _EN_WEEKDAYS:
            days.append(_EN_WEEKDAYS[key])
    return days


def _bn_weekdays(text):
    """Parse 'সোম, বুধ, শুক্র' into [0, 2, 4]."""
    days = []
    for token in re.split(r"[,\s]+", text.strip()):
        for name, idx in _BN_WEEKDAYS.items():
            if token == name:
                days.append(idx)
                break
    return days


def _parse_doctors_en(prompt):
    """Extract doctor dicts (name/specialty/schedule) from the English prompt."""
    doctors = []
    row_re = re.compile(
        r"^-\s*(Dr\.\s*.+?)\s*\((.+?)\):\s*(.+?)\s*[—–-]\s*(.+)$",
        re.MULTILINE,
    )
    for m in row_re.finditer(prompt):
        days = _en_weekdays(m.group(3))
        start = _en_time(m.group(4))
        end = _en_time(m.group(4).split("to")[-1] if "to" in m.group(4) else m.group(4))
        doctors.append({
            "name": m.group(1).strip(),
            "specialty": m.group(2).strip(),
            "weekdays": days,
            "start": start,
            "end": end,
        })
    return doctors


def _parse_doctors_bn(prompt):
    """Extract doctor dicts (name/specialty/schedule) from the Bengali prompt."""
    doctors = []
    row_re = re.compile(
        r"^-\s*(ডা\.\s*.+?)\s*\((.+?)\):\s*(.+?)\s*[—–-]\s*(.+)$",
        re.MULTILINE,
    )
    for m in row_re.finditer(prompt):
        days = _bn_weekdays(m.group(3))
        parts = re.split(r"[—–]", m.group(4))
        start = _bn_time(parts[0]) if parts else None
        end = _bn_time(parts[-1]) if len(parts) > 1 else None
        doctors.append({
            "name": m.group(1).strip(),
            "specialty": m.group(2).strip(),
            "weekdays": days,
            "start": start,
            "end": end,
        })
    return doctors


def _parse_services_en(prompt):
    """Extract service names + starting prices from the English prompt."""
    services = []
    block = prompt.split("Services:")[-1]
    for m in re.finditer(r"([A-Za-z][A-Za-z ]*?)\s+([\d,]+)\s*[–-]\s*[\d,]+\s*BDT", block):
        services.append({"name": m.group(1).strip(), "price": float(m.group(2).replace(",", ""))})
    return services


def _parse_services_bn(prompt):
    """Extract service names + starting prices from the Bengali prompt."""
    services = []
    block = prompt.split("সেবা ও খরচ:")[-1]
    for m in re.finditer(r"([^|]+?)\s+([০-৯]+)\s*[–-]\s*[০-৯]+\s*৳", block):
        services.append({
            "name": m.group(1).strip(),
            "price": float(m.group(2).translate(_BN_DIGITS)),
        })
    return services


def extract_seed(prompt_bn, prompt_en, ai_model="openrouter/auto"):
    """Parse the hardcoded clinic data out of the two system prompts.

    Returns a dict with ``settings`` (key -> value), ``doctors`` (list, each
    with a ``schedule`` list) and ``services`` (list). Prices are stored as the
    *starting* value of the range shown in the prompt; durations default to 30
    minutes because the prompts do not specify them.
    """
    settings = {}

    m = re.search(r"-\s*Name:\s*(.+?)\s*\|\s*Address:\s*(.+)", prompt_en)
    if m:
        settings["clinic_name"] = m.group(1).strip()
        settings["address"] = m.group(2).strip()
    m = re.search(r"-\s*নাম:\s*(.+?)\s*\|\s*ঠিকানা:\s*(.+)", prompt_bn)
    if m:
        settings["clinic_name_bn"] = m.group(1).strip()
        settings["address_bn"] = m.group(2).strip()

    m = re.search(r"-\s*Hours:\s*(.+)", prompt_en)
    if m:
        settings["opening_hours"] = m.group(1).strip()
    m = re.search(r"-\s*সময়:\s*(.+)", prompt_bn)
    if m:
        settings["opening_hours_bn"] = m.group(1).strip()

    m = re.search(r"You are\s+(\w+)\s*[—–-]", prompt_en)
    if m:
        settings["persona_name"] = m.group(1).strip()
    m = re.search(r"তুমি\s+(\S+?)\s*[—–-]", prompt_bn)
    if m:
        settings["persona_name_bn"] = m.group(1).strip()

    settings["phone"] = ""
    settings["welcome_en"] = ""
    settings["welcome_bn"] = ""
    settings["extra_info_en"] = ""
    settings["extra_info_bn"] = ""
    settings["ai_model"] = ai_model

    doctors_en = _parse_doctors_en(prompt_en)
    doctors_bn = _parse_doctors_bn(prompt_bn)
    doctors = []
    for i, d_en in enumerate(doctors_en):
        d_bn = doctors_bn[i] if i < len(doctors_bn) else {}
        schedule = []
        for weekday in d_en["weekdays"] or d_bn.get("weekdays", []):
            if d_en.get("start") and d_en.get("end"):
                schedule.append({
                    "weekday": weekday,
                    "start_time": d_en["start"],
                    "end_time": d_en["end"],
                })
            elif d_bn.get("start") and d_bn.get("end"):
                schedule.append({
                    "weekday": weekday,
                    "start_time": d_bn["start"],
                    "end_time": d_bn["end"],
                })
        doctors.append({
            "name_en": d_en["name"],
            "name_bn": d_bn.get("name", ""),
            "specialty_en": d_en["specialty"],
            "specialty_bn": d_bn.get("specialty", ""),
            "bio_en": "",
            "bio_bn": "",
            "slot_minutes": 30,
            "active": 1,
            "schedule": schedule,
        })

    services_en = _parse_services_en(prompt_en)
    services_bn = _parse_services_bn(prompt_bn)
    services = []
    for i, s_en in enumerate(services_en):
        s_bn = services_bn[i] if i < len(services_bn) else {}
        services.append({
            "name_en": s_en["name"],
            "name_bn": s_bn.get("name", ""),
            "price": s_en["price"],
            "duration_minutes": 30,
            "active": 1,
        })

    return {"settings": settings, "doctors": doctors, "services": services}


def preview_seed(prompt_bn, prompt_en, ai_model="openrouter/auto"):
    """Return the extracted seed data as-is, for review before insertion."""
    return extract_seed(prompt_bn, prompt_en, ai_model=ai_model)


# ─── SEEDING ──────────────────────────────────────────────────────────────────

def _seed_if_empty(prompt_bn, prompt_en, ai_model):
    """Insert seed data when the settings table is empty."""
    with _connect() as conn:
        empty = conn.execute("SELECT COUNT(*) AS n FROM settings").fetchone()["n"] == 0
    if not empty:
        return

    data = extract_seed(prompt_bn, prompt_en, ai_model=ai_model)

    print("\n[db] Seeding initial data extracted from the system prompts:")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    with _connect() as conn:
        for key, value in data["settings"].items():
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
                   (name_en, name_bn, price, duration_minutes, active)
                   VALUES (?, ?, ?, ?, ?)""",
                (svc["name_en"], svc["name_bn"], svc["price"],
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

def init_db(prompt_bn=None, prompt_en=None, ai_model="openrouter/auto",
            admin_username=None, admin_password=None):
    """Create tables, seed initial data, seed an admin and migrate old data.

    Safe to call on every app startup: table creation uses ``IF NOT EXISTS``,
    seeding only happens when a table is empty, and the legacy JSON migration
    only runs while ``appointments.json`` still exists.
    """
    _create_tables()
    if prompt_bn and prompt_en:
        _seed_if_empty(prompt_bn, prompt_en, ai_model)
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


def create_service(name_en=None, name_bn=None, price=None, duration_minutes=30, active=1):
    """Create a service. Returns the new id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO services
               (name_en, name_bn, price, duration_minutes, active)
               VALUES (?, ?, ?, ?, ?)""",
            (name_en, name_bn, price, duration_minutes, active),
        )
    return cur.lastrowid


def update_service(service_id, **fields):
    """Update allowed service fields. Returns the updated dict or None."""
    allowed = {"name_en", "name_bn", "price", "duration_minutes", "active"}
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
                       language=None):
    """Create an appointment. Returns the new id."""
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO appointments
               (patient_name, phone, doctor_id, service_id, date, time,
                status, notes, language, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (patient_name, phone, doctor_id, service_id, date, time,
             status, notes, language, now, now),
        )
    return cur.lastrowid


def update_appointment(appointment_id, **fields):
    """Update allowed appointment fields, refreshing updated_at.

    Returns the updated dict or ``None``.
    """
    allowed = {"patient_name", "phone", "doctor_id", "service_id", "date",
               "time", "status", "notes", "language"}
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

def add_chat_message(session_id, role, content):
    """Append a chat message to the log. Returns the new id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (session_id, role, content, created_at)
               VALUES (?, ?, ?, ?)""",
            (session_id, role, content, _now()),
        )
    return cur.lastrowid


def get_recent_messages(session_id, limit=20):
    """Return the most recent messages for a session, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM (
                   SELECT * FROM chat_messages
                   WHERE session_id = ?
                   ORDER BY id DESC
                   LIMIT ?
               ) ORDER BY id ASC""",
            (session_id, limit),
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
