import os
import re
import io
import csv
import json
import time
import hmac
import base64
import hashlib
import logging
import datetime
import functools
import secrets
import threading
from urllib.parse import urlparse
from flask import (
    Flask, render_template, request,
    jsonify, session, redirect, url_for, flash
)
import requests

import db
import prompt_builder

# Load variables from a local .env file if python-dotenv is available.
# This is optional — you can also just export the variables in your shell.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
# Prefer the FLASK_SECRET_KEY env var; fall back to a random per-process key.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)


def _env_flag(name):
    """Return True when an env var is a common truthy string."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# ─── SESSION COOKIE SECURITY ──────────────────────────────────────────────────
# The session cookie is HttpOnly + SameSite=Lax always; COOKIE_SECURE=1 adds
# Secure (use it behind HTTPS). SESSION_IDLE_HOURS caps how long an idle admin
# session stays usable (checked server-side in _admin_gate()).
SESSION_IDLE_HOURS = max(1, int(os.environ.get("SESSION_IDLE_HOURS", "8") or 8))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_env_flag("COOKIE_SECURE"),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=SESSION_IDLE_HOURS),
)


@app.before_request
def _apply_cookie_flags():
    """Re-read COOKIE_SECURE per request so it needs no restart (and is testable)."""
    app.config["SESSION_COOKIE_SECURE"] = _env_flag("COOKIE_SECURE")


@app.after_request
def _no_store_private_pages(response):
    """Never cache admin or agent pages — they show customer data and secrets."""
    path = request.path or ""
    if path == "/admin" or path.startswith("/admin/") or \
            path == "/agent" or path.startswith("/agent/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


# Failed admin logins: 5 per (IP, username) pair *or* 20 per IP across all
# usernames, in a 10-minute window, blocks that key for 5 more minutes.
# In-memory on purpose — no background thread, no new deps.
LOGIN_FAIL_LIMIT    = 5
LOGIN_IP_FAIL_LIMIT = 20      # looser, across ALL usernames from one address
LOGIN_FAIL_WINDOW  = 600      # 10 minutes
LOGIN_BLOCK_SECONDS = 300     # 5 minutes
_login_failures = {}          # key -> [failure timestamps]
_login_blocks   = {}          # key -> epoch until blocked
_login_lock     = threading.Lock()

ADMIN_LOGGER = logging.getLogger("clinic.admin")

# ─── FILES ─────────────────────────────────────────────────────────────────────
# Only secrets/API config stay in a JSON file (gitignored). Everything dynamic
# — settings, doctors, services, appointments, admins — lives in SQLite (db.py).
CONFIG_FILE = "config.json"

# ─── LOAD CONFIG ───────────────────────────────────────────────────────────────
# Environment variables take priority; config.json is the gitignored legacy
# fallback. The panel never writes to this file — API keys entered in
# /admin/settings are stored in SQLite instead.
ENV_OVERRIDES = {
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "admin_username":     "ADMIN_USERNAME",
    "admin_password":     "ADMIN_PASSWORD",
}

def load_config():
    """Read config.json if present, then let env vars override values.

    Change the file or the env vars, then restart the server.
    """
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    for key, env_var in ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value:
            cfg[key] = value
    return cfg

def get_cfg(key):
    return load_config().get(key, "")

# ─── DATABASE INIT ─────────────────────────────────────────────────────────────
# Creates clinic.db (if needed), migrates columns, seeds clinic data from db.py,
# seeds an admin account, and migrates any legacy appointments.json.
# Set SKIP_DB_INIT=1 to skip this (used for tooling/testing).
if os.environ.get("SKIP_DB_INIT") != "1":
    _cfg = load_config()
    db.init_db(
        ai_model=_cfg.get("openrouter_model") or "openrouter/auto",
        admin_username=_cfg.get("admin_username"),
        admin_password=_cfg.get("admin_password"),
    )

# ─── HELPERS ──────────────────────────────────────────────────────────────────
BOOKING_MARKER  = "SAVE_APPOINTMENT:"
HANDOFF_MARKER  = "HANDOFF_REQUEST:"
# Share the exact phone rule with db.py (callbacks use the same validation).
PHONE_RE        = db.PHONE_RE
MAX_NAME_LEN    = 100
MAX_DAYS_AHEAD  = 60
HISTORY_LIMIT   = 12
MAX_MESSAGE_LEN = 1000

# Simple in-memory per-IP rate limit for /chat (kept as globals so tests can
# adjust them). Other endpoints use their own buckets.
RATE_LIMIT    = 20     # requests ...
RATE_WINDOW   = 60     # ... per this many seconds
_rate_buckets = {}
_rate_lock    = threading.Lock()

_poll_buckets    = {}   # GET /chat/poll (keyed by session)
_callback_buckets = {}  # POST /callback (keyed by IP)
_agent_buckets   = {}   # agent sends (keyed by agent id)
_session_buckets = {}   # POST /session (keyed by IP)

POLL_RATE_LIMIT     = 60
CALLBACK_RATE_LIMIT = 5
AGENT_RATE_LIMIT    = 30
SESSION_RATE_LIMIT  = 60


def _rate_check(buckets, key, limit, window=RATE_WINDOW):
    """Return True when ``key`` has exceeded ``limit`` requests per ``window``."""
    now = time.time()
    with _rate_lock:
        stamps = [t for t in buckets.get(key, []) if now - t < window]
        if len(stamps) >= limit:
            buckets[key] = stamps
            return True
        stamps.append(now)
        buckets[key] = stamps
    return False


def _rate_limited(ip):
    """Return True when ``ip`` has exceeded the per-minute /chat limit."""
    return _rate_check(_rate_buckets, ip, RATE_LIMIT, RATE_WINDOW)


def _client_ip():
    """Best-effort client IP (honours one X-Forwarded-For hop)."""
    return (request.headers.get("X-Forwarded-For", "")
            or request.remote_addr or "unknown").split(",")[0].strip()


# ─── SESSION SECURITY ─────────────────────────────────────────────────────────
# Customer session ids are server-issued ``secrets.token_urlsafe(24)`` values
# (~32 chars of [A-Za-z0-9_-]). Anything else is rejected on public endpoints.
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")


def _valid_session_id(sid):
    """Return True when ``sid`` has the expected server-issued shape."""
    return bool(sid) and bool(SESSION_RE.match(sid))


def new_session_id():
    """Generate a fresh, unguessable customer session id."""
    return secrets.token_urlsafe(24)


# ─── CSRF (per session) ───────────────────────────────────────────────────────
def _csrf_token():
    """Return this browser session's CSRF token, creating it on first use."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_ok():
    """Verify the CSRF token against this session's token.

    Accepts the ``X-CSRF-Token`` header (JSON APIs) or the hidden
    ``csrf_token`` form field (HTML forms).
    """
    token = session.get("csrf_token")
    sent = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")
    if not token or not sent:
        return False
    return hmac.compare_digest(str(token), str(sent))


@app.context_processor
def _inject_csrf():
    """Make ``csrf_token`` available to templates (admin forms reuse it later)."""
    return {"csrf_token": _csrf_token()}


# ─── HANDOFF MESSAGE LINES ────────────────────────────────────────────────────
def _handoff_queued_line(position, lang):
    if lang == "bn":
        return ("আপনার অনুরোধ পেয়েছি — একজন এজেন্ট শীঘ্রই যোগ দেবেন "
                "(আপনি %s নম্বরে আছেন)।" % position)
    return ("Got it — I've asked a human agent to join. "
            "You're number %s in the queue." % position)


def _handoff_callback_line(lang):
    if lang == "bn":
        return ("এখন কোনো এজেন্ট অনলাইনে নেই। চাইলে আপনার নাম আর নম্বর দিলে "
                "আমরা ফোন করে জানাব।")
    return ("No agent is online right now. Leave your name and number "
            "and we'll call you back.")


def _append_line(reply, line):
    """Append a server-authored line to the model's visible reply."""
    reply = (reply or "").strip()
    return ("%s\n\n%s" % (reply, line)).strip() if reply else line


def _public_appt(appt):
    """Shape an appointment dict for the /chat response."""
    if not appt:
        return None
    return {
        "id":       appt.get("id"),
        "name":     appt.get("patient_name"),
        "phone":    appt.get("phone"),
        "problem":  appt.get("notes"),
        "doctor_id": appt.get("doctor_id"),
        "service_id": appt.get("service_id"),
        "date":     appt.get("date"),
        "time":     appt.get("time"),
        "status":   appt.get("status"),
        "saved_at": appt.get("created_at"),
    }


def _shape_message(m):
    """Shape one chat row for the customer widget (poll + history)."""
    return {
        "id":          m["id"],
        "sender_type": m["sender_type"],
        "content":     m["content"],
        "created_at":  m["created_at"],
        "agent_name":  m.get("agent_display_name"),
    }


# ─── AI CHAT ──────────────────────────────────────────────────────────────────
# ─── OPENROUTER API KEY ────────────────────────────────────────────────────────
# Resolution order: env OPENROUTER_API_KEY  ->  settings table (admin panel)
# ->  config.json (legacy fallback). The value itself is never rendered.
def _usable_key(value):
    """True when ``value`` looks like a real key (not a placeholder)."""
    value = (value or "").strip()
    if not value or value.startswith("YOUR_") or "বসান" in value:
        return False
    return True


def resolve_api_key():
    """Return ``(key, source)`` where source is 'env', 'panel' or 'config'."""
    env_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if _usable_key(env_key):
        return env_key, "env"
    panel_key = (db.get_setting("openrouter_api_key") or "").strip()
    if _usable_key(panel_key):
        return panel_key, "panel"
    legacy_key = (load_config().get("openrouter_api_key") or "").strip()
    if _usable_key(legacy_key):
        return legacy_key, "config"
    return "", ""


def api_key_status():
    """Return display info for the stored key: set flag, source, masked tail."""
    key, source = resolve_api_key()
    if not key:
        return {"set": False, "source": None, "source_label": "Not set",
                "masked": None}
    labels = {
        "env":    "Environment variable OPENROUTER_API_KEY",
        "panel":  "Admin panel (saved in this database)",
        "config": "config.json (legacy file)",
    }
    tail = key[-4:] if len(key) >= 4 else ""
    return {"set": True, "source": source,
            "source_label": labels.get(source, source),
            "masked": "••••" + tail}


def _openrouter_chat(messages, lang="bn"):
    """Send ``messages`` to OpenRouter and return the reply text.

    The model comes from the ``ai_model`` setting (fallback ``openrouter/auto``)
    and the API key from :func:`resolve_api_key` (env -> panel -> config.json).
    Errors are returned as friendly bilingual strings and never include the key.
    """
    api_key, _source = resolve_api_key()
    settings = db.get_settings()
    model   = (settings.get("ai_model") or "").strip() or "openrouter/auto"
    title   = (settings.get("clinic_name") or settings.get("clinic_name_bn")
               or "AI Receptionist")

    if not api_key:
        return ("OPENROUTER_API_KEY env var অথবা config.json ফাইলে API key বসান।" if lang == "bn"
                else "Please set OPENROUTER_API_KEY or your API key in config.json.")

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:5000",
                "X-Title": title,
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 400,
                "temperature": 0.7,
            },
            timeout=30,
        )
        print(f"[OpenRouter] status={resp.status_code}")
        print(f"[OpenRouter] response={resp.text[:500]}")
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        if code == 401:
            return ("API key ভুল। config.json চেক করুন।" if lang == "bn"
                    else "Invalid API key. Please check config.json.")
        if code == 404:
            return (f"Model পাওয়া যাচ্ছে না ({model})।" if lang == "bn"
                    else f"Model not found ({model}).")
        if code == 429:
            return ("অনেক বেশি request হয়েছে। একটু পরে আবার চেষ্টা করুন।" if lang == "bn"
                    else "Too many requests. Please wait a moment and try again.")
        return (f"সার্ভার error ({code})। আবার চেষ্টা করুন।" if lang == "bn"
                else f"Server error ({code}). Please try again.")

    except requests.exceptions.ConnectionError:
        return ("ইন্টারনেট সংযোগ নেই।" if lang == "bn" else "No internet connection.")

    except requests.exceptions.Timeout:
        return ("সময় শেষ হয়ে গেছে। আবার চেষ্টা করুন।" if lang == "bn"
                else "Request timed out. Please try again.")

    except Exception as e:
        return (f"সমস্যা হয়েছে: {str(e)}" if lang == "bn"
                else f"Something went wrong: {str(e)}")


def test_ai_connection():
    """Make a tiny OpenRouter request. Returns ``(ok, message)``.

    The message is safe to show in the UI and in JSON: it never contains the
    API key — only the HTTP outcome and which source supplied the key.
    """
    api_key, source = resolve_api_key()
    if not api_key:
        return False, ("No API key is configured. Set one below, export "
                       "OPENROUTER_API_KEY, or add it to config.json.")
    settings = db.get_settings()
    model = (settings.get("ai_model") or "").strip() or "openrouter/auto"
    source_label = {"env": "environment", "panel": "admin panel",
                    "config": "config.json"}.get(source, source)
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": "PING"}],
                  "max_tokens": 5},
            timeout=20,
        )
    except requests.exceptions.Timeout:
        return False, ("Connection timed out after 20s. Check your network and "
                       "try again.")
    except requests.exceptions.ConnectionError:
        return False, "Could not reach openrouter.ai. Check your internet connection."
    except Exception:
        return False, "The connection test failed unexpectedly."

    code = resp.status_code
    if code == 401:
        return False, ("OpenRouter rejected the key (HTTP 401) — it is invalid "
                       "or has been revoked. Source: %s." % source_label)
    if code == 403:
        return False, ("Access denied (HTTP 403). The key may lack credit or "
                       "permission. Source: %s." % source_label)
    if code == 404:
        return False, ("Model not found (HTTP 404): %s. Pick another model "
                       "below." % model)
    if code == 429:
        return False, ("Rate limited (HTTP 429) by OpenRouter. Wait a minute "
                       "and try again.")
    if code >= 500:
        return False, ("OpenRouter server error (HTTP %d). Try again later." % code)
    if code >= 400:
        return False, ("Request rejected (HTTP %d). Check the model name." % code)
    return True, ("Connected to OpenRouter (HTTP 200) using model '%s'. "
                  "Key source: %s." % (model, source_label))


def _history_messages(conversation_id):
    """Return the last HISTORY_LIMIT messages formatted for the API.

    Agent messages are treated as assistant messages and system messages are
    skipped (see :func:`db.get_conversation_history`).
    """
    return db.get_conversation_history(conversation_id, limit=HISTORY_LIMIT)


def generate_reply(conversation_id, lang="bn"):
    """Return the model's reply using the conversation's stored history.

    The caller stores the customer message before calling this, so the history
    already includes it.
    """
    system = prompt_builder.build_system_prompt(lang)
    messages = [{"role": "system", "content": system}] + _history_messages(conversation_id)
    return _openrouter_chat(messages, lang)


def corrective_reply(conversation_id, lang, instruction):
    """Second, hidden call telling the model what was wrong with a booking."""
    system = prompt_builder.build_system_prompt(lang) + "\n\n" + instruction
    messages = [{"role": "system", "content": system}] + _history_messages(conversation_id)
    return _openrouter_chat(messages, lang)


def _parse_marker_json(raw):
    """Parse a marker's JSON payload into a dict, or ``None`` when unusable."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


def _strip_marker(reply, marker):
    """Split ``reply`` at ``marker``.

    Returns ``(text_before_marker, raw_payload_or_None, marker_present)``. The
    marker (and anything after it) is always removed from the visible text,
    even when the JSON is missing or malformed.
    """
    if marker not in reply:
        return reply, None, False
    before, after = reply.split(marker, 1)
    raw = after.strip().splitlines()[0].strip() if after.strip() else ""
    return before, raw, True


def _extract_markers(reply):
    """Strip the booking and handoff markers from a model reply.

    Returns ``(visible, booking_payload, booking_present, handoff_payload,
    handoff_present)``. Both markers are always stripped, even when malformed.
    """
    before_booking, booking_raw, booking_present = _strip_marker(reply, BOOKING_MARKER)
    before_handoff, handoff_raw, handoff_present = _strip_marker(reply, HANDOFF_MARKER)

    if booking_present and handoff_present:
        first = min(reply.find(BOOKING_MARKER), reply.find(HANDOFF_MARKER))
        visible = reply[:first].strip()
    elif booking_present:
        visible = before_booking.strip()
    elif handoff_present:
        visible = before_handoff.strip()
    else:
        visible = reply.strip()

    booking = _parse_marker_json(booking_raw) if booking_present else None
    handoff = _parse_marker_json(handoff_raw) if handoff_present else None
    return visible, booking, booking_present, handoff, handoff_present


def _validate_booking(payload, lang, exclude_appointment_id=None):
    """Validate a parsed booking payload.

    Returns ``(booking_dict_or_None, error_message_or_None)``.
    ``exclude_appointment_id`` lets the admin editor re-validate an existing
    appointment without it counting as its own double booking.
    """
    def err(en, bn):
        return bn if lang == "bn" else en

    name = str(payload.get("patient_name") or "").strip()
    if not name:
        return None, err("I still need the patient's name to book.",
                         "বুকিং করার জন্য রোগীর নামটা দরকার।")
    if len(name) > MAX_NAME_LEN:
        return None, err("That name is too long.",
                         "নামটা একটু ছোট করে বলুন।")

    phone = re.sub(r"[\s\-()]", "", str(payload.get("phone") or ""))
    if not PHONE_RE.match(phone):
        return None, err("That phone number doesn't look like a valid Bangladeshi number.",
                         "ফোন নম্বরটা সঠিক বাংলাদেশি নম্বর মনে হচ্ছে না।")

    doctor = None
    raw_id = payload.get("doctor_id")
    if raw_id not in (None, "", 0):
        try:
            doctor = db.get_doctor(int(raw_id))
        except (ValueError, TypeError):
            doctor = None
    if doctor is None:
        doctor = db.find_doctor_by_name(payload.get("doctor_name") or payload.get("doctor"))
    if doctor is None or not doctor.get("active"):
        return None, err("We couldn't match that doctor. Please pick one of our doctors.",
                         "ডাক্তারটা মেলাতে পারলাম না। আমাদের ডাক্তারদের একজন বেছে নিন।")
    doctor_id = doctor["id"]

    date = str(payload.get("date") or "").strip()
    try:
        day = datetime.date.fromisoformat(date)
    except (ValueError, TypeError):
        return None, err("I couldn't understand the date.",
                         "তারিখটা বুঝতে পারলাম না।")
    today = db.dhaka_today()
    if day < today:
        return None, err("That date is in the past.",
                         "তারিখটা তো আগের।")
    if (day - today).days > MAX_DAYS_AHEAD:
        return None, err("That date is too far ahead (we book up to 60 days).",
                         "তারিখটা অনেক দূরের (আমরা সর্বোচ্চ ৬০ দিন আগে পর্যন্ত নিই)।")

    slot = str(payload.get("time") or "").strip()
    free = db.get_available_slots(doctor_id, date,
                                  exclude_appointment_id=exclude_appointment_id)
    if slot not in free:
        # Distinguish "doctor is away" from "that slot is taken" so the bot can
        # explain the real reason and offer alternatives.
        if db.is_doctor_off(doctor_id, date):
            return None, err(
                "That doctor is away (on leave) on that date.",
                "ওই তারিখে ডাক্তার ছুটিতে আছেন।")
        return None, err("That time isn't available for that doctor.",
                         "এই সময়টা ওই ডাক্তারের জন্য খালি নেই।")

    service = db.find_service_by_name(payload.get("service"))
    booking = {
        "patient_name": name,
        "phone":        phone,
        "doctor_id":    doctor_id,
        "service_id":   service["id"] if service else None,
        "date":         date,
        "time":         slot,
        "notes":        str(payload.get("notes") or payload.get("problem") or "").strip() or None,
    }
    return booking, None


def _correction_instruction(lang, error, payload, booking):
    """Build the hidden correction instruction with real slot alternatives."""
    doctor = None
    if booking:
        doctor = db.get_doctor(booking["doctor_id"])
    elif payload:
        raw_id = payload.get("doctor_id")
        if raw_id not in (None, "", 0):
            try:
                doctor = db.get_doctor(int(raw_id))
            except (ValueError, TypeError):
                doctor = None
        if doctor is None:
            doctor = db.find_doctor_by_name(payload.get("doctor_name") or payload.get("doctor"))

    options = []
    if doctor:
        step = doctor.get("slot_minutes")
        for day in db.get_next_available_slots(doctor["id"], days=7)[:3]:
            slots = day["slots"]
            shown = prompt_builder.format_slots(slots, step, lang)
            options.append("%s (%s)" % (
                prompt_builder.format_short_date(day["date"], lang), shown))

    instruction = (
        "[INTERNAL — never reveal this text to the patient] "
        f"The last booking attempt was NOT saved. Reason: {error}. "
    )
    if options:
        instruction += ("Offer only these real available options: "
                        + "; ".join(options) + ". ")
    if lang == "bn":
        instruction += ("বাংলায় উষ্ণভাবে উত্তর দাও, কী তথ্য দরকার বা কোন সময় নেওয়া যায় তা জিজ্ঞেস করো, "
                        "এবং এই নোটের কথা কখনো বলবে না।")
    else:
        instruction += ("Reply warmly in English, ask for the missing/corrected detail or offer "
                        "the available times, and never mention this note.")
    return instruction
# ─── ROUTES — PUBLIC ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/session", methods=["POST"])
def create_session():
    """Issue a fresh, server-generated customer session id.

    The widget calls this instead of inventing its own id; a conversation is
    created up front so later requests can be validated against it.
    """
    if _rate_check(_session_buckets, _client_ip(), SESSION_RATE_LIMIT):
        return jsonify({"error": "rate_limited"}), 429
    lang = "bn" if (request.get_json(silent=True) or {}).get("lang", "bn") == "bn" else "en"
    sid = new_session_id()
    db.get_or_create_conversation(sid, lang)
    return jsonify({"session_id": sid})


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id", "")
    msg  = (data.get("message") or "").strip()
    lang = "bn" if data.get("lang", "bn") == "bn" else "en"

    if not _valid_session_id(sid) or not db.session_exists(sid):
        return jsonify({"reply": ("সেশনের মেয়াদ শেষ। পেজটা রিফ্রেশ করুন।" if lang == "bn"
                                  else "Your session has expired. Please refresh the page."),
                        "error": "invalid_session"}), 400

    # The customer just spoke to us — remember they are still present.
    db.touch_customer_seen(sid)

    if not msg:
        return jsonify({"reply": ("দয়া করে একটা মেসেজ লিখুন।" if lang == "bn"
                                  else "Please type a message."),
                        "error": "empty"}), 400

    if len(msg) > MAX_MESSAGE_LEN:
        return jsonify({"reply": ("মেসেজটা অনেক বড় — সর্বোচ্চ ১০০০ অক্ষর।" if lang == "bn"
                                  else "Message is too long — 1000 characters max."),
                        "error": "too_long"}), 400

    if _rate_limited(_client_ip()):
        return jsonify({"reply": ("একটু ধীরে! এক মিনিটে অনেক বার লিখেছেন — একটু পরে আবার চেষ্টা করুন।"
                                  if lang == "bn"
                                  else "Slow down! Too many messages in a minute — please try again shortly."),
                        "error": "rate_limited"}), 429

    conv = db.get_or_create_conversation(sid, lang)

    # A human agent owns the conversation: store the message, do NOT call the AI.
    if conv["status"] == "with_agent":
        db.customer_send_message(conv["id"], msg)
        return jsonify({"mode": "agent", "status": "with_agent",
                        "conversation_id": conv["id"], "reply": None})

    # Bot (or waiting-for-agent): the AI keeps answering.
    db.add_message(conv["id"], "customer", msg)
    raw = generate_reply(conv["id"], lang)
    visible, payload, marker_present, handoff, handoff_present = _extract_markers(raw)

    saved = False
    appt  = None
    if marker_present:
        if payload is None:
            booking, error = None, ("বুকিং তথ্যটা ঠিকভাবে পড়া গেল না।" if lang == "bn"
                                    else "I couldn't read the booking details.")
        else:
            booking, error = _validate_booking(payload, lang)

        if booking:
            appt_id = db.book_appointment(
                patient_name=booking["patient_name"],
                phone=booking["phone"],
                doctor_id=booking["doctor_id"],
                service_id=booking["service_id"],
                date=booking["date"],
                time=booking["time"],
                notes=booking["notes"],
                language=lang,
                session_id=sid,
            )
            if appt_id:
                appt  = db.get_appointment(appt_id)
                saved = True
            else:
                error = ("দুঃখিত, এই সময়টা এইমাত্র বুক হয়ে গেছে।" if lang == "bn"
                         else "Sorry, that slot was just taken.")

        if not saved:
            instruction = _correction_instruction(lang, error, payload, booking)
            visible = corrective_reply(conv["id"], lang, instruction)

    # Handoff request: the server (not the model) decides the truth.
    handoff_queued = False
    callback_offered = False
    if handoff_present:
        reason = (handoff or {}).get("reason") or "customer_request"
        result = db.request_handoff(conv["id"], reason)
        handoff_queued = bool(result.get("queued"))
        if handoff_queued:
            visible = _append_line(
                visible, _handoff_queued_line(result.get("position") or 1, lang))
        else:
            visible = _append_line(visible, _handoff_callback_line(lang))
            callback_offered = True

    db.add_message(conv["id"], "bot", visible)
    return jsonify({
        "reply": visible,
        "mode": "bot",
        "appointment_saved": saved,
        "appointment_id": appt["id"] if appt else None,
        "appointment_data": _public_appt(appt),
        "handoff_queued": handoff_queued,
        "callback_offered": callback_offered,
    })


@app.route("/handoff", methods=["POST"])
def handoff():
    """Customer pressed the 'talk to a human' button."""
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id", "")
    lang = "bn" if data.get("lang", "bn") == "bn" else "en"

    if not _valid_session_id(sid) or not db.session_exists(sid):
        return jsonify({"error": "invalid_session"}), 400
    if _rate_limited(_client_ip()):
        return jsonify({"error": "rate_limited"}), 429

    db.touch_customer_seen(sid)
    conv = db.get_or_create_conversation(sid, lang)
    if conv["status"] == "with_agent":
        return jsonify({"queued": True, "position": 0, "status": "with_agent",
                        "callback_offered": False, "reply": None})

    reason = str(data.get("reason") or "customer_button").strip() or "customer_button"
    result = db.request_handoff(conv["id"], reason)
    if result.get("queued"):
        line = _handoff_queued_line(result.get("position") or 1, lang)
    else:
        line = _handoff_callback_line(lang)
    # Store as a system message so /chat/poll surfaces it to the customer.
    db.add_message(conv["id"], "system", line)
    return jsonify({
        "queued": bool(result.get("queued")),
        "position": result.get("position"),
        "status": result.get("status"),
        "callback_offered": not result.get("queued"),
        "reply": line,
    })


@app.route("/callback", methods=["POST"])
def callback():
    """Leave a name + phone for the clinic to call back."""
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id", "")
    lang = "bn" if data.get("lang", "bn") == "bn" else "en"

    if not _valid_session_id(sid) or not db.session_exists(sid):
        return jsonify({"error": "invalid_session"}), 400
    if _rate_check(_callback_buckets, _client_ip(), CALLBACK_RATE_LIMIT):
        return jsonify({"error": "rate_limited"}), 429

    db.touch_customer_seen(sid)
    name    = (data.get("name") or "").strip()
    phone   = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()

    if not name:
        return jsonify({"error": "name",
                        "reply": ("নামটা লিখুন।" if lang == "bn"
                                  else "Please include your name.")}), 400
    if not db.is_valid_phone(phone):
        return jsonify({"error": "phone",
                        "reply": ("ফোন নম্বরটা সঠিক মনে হচ্ছে না।" if lang == "bn"
                                  else "That phone number doesn't look right.")}), 400

    conv = db.get_or_create_conversation(sid, lang)
    callback_id = db.create_callback_request(conv["id"], name, phone, message)
    return jsonify({"ok": True, "callback_id": callback_id})


@app.route("/chat/poll")
def chat_poll():
    """Return new customer-visible messages and the conversation status."""
    sid = request.args.get("session_id", "")
    after_id = request.args.get("after_id", type=int)

    if not _valid_session_id(sid) or not db.session_exists(sid):
        return jsonify({"error": "invalid_session"}), 400
    if _rate_check(_poll_buckets, sid, POLL_RATE_LIMIT, 60):
        return jsonify({"error": "rate_limited"}), 429

    # Touch before housekeeping: a polling widget proves the customer is present.
    db.touch_customer_seen(sid)
    db.run_housekeeping()
    conv = db.get_or_create_conversation(sid)
    data = db.get_conversation_with_messages(conv["id"], after_id=after_id)
    if not data:
        return jsonify({"error": "not_found"}), 404

    all_msgs = data["messages"]
    visible = [m for m in all_msgs if m["sender_type"] in ("bot", "agent", "system")]
    last_id = max([m["id"] for m in all_msgs] + [after_id or 0])
    status = data["conversation"]["status"]

    payload = {
        "status": status,
        "messages": [_shape_message(m) for m in visible],
        "last_id": last_id,
        "agent_name": (data["conversation"].get("agent_display_name")
                       if status == "with_agent" else None),
        "position": None,
    }
    if status == "waiting":
        payload["position"] = db.queue_position(conv["id"])
    return jsonify(payload)


@app.route("/chat/history")
def chat_history():
    """Return the customer-visible transcript so the widget can restore itself.

    Used on page load after a refresh: it returns the latest conversation for
    the session (including a closed one) with every message the customer saw —
    customer, bot, agent and system — plus the current status and queue position.
    Same validation and rate limiting as ``/chat/poll``.
    """
    sid = request.args.get("session_id", "")

    if not _valid_session_id(sid) or not db.session_exists(sid):
        return jsonify({"error": "invalid_session"}), 400
    if _rate_check(_poll_buckets, sid, POLL_RATE_LIMIT, 60):
        return jsonify({"error": "rate_limited"}), 429

    db.touch_customer_seen(sid)
    db.run_housekeeping()

    conv = db.get_latest_conversation(sid)
    if not conv:
        return jsonify({"status": "none", "messages": [], "last_id": 0,
                        "conversation_id": None, "agent_name": None,
                        "position": None, "lang": None})

    data = db.get_conversation_with_messages(conv["id"])
    messages = data["messages"] if data else []
    status = conv["status"]
    last_id = max([m["id"] for m in messages] + [0])
    return jsonify({
        "status": status,
        "messages": [_shape_message(m) for m in messages],
        "last_id": last_id,
        "conversation_id": conv["id"],
        "lang": conv.get("lang"),
        "agent_name": (data["conversation"].get("agent_display_name")
                       if data and status == "with_agent" else None),
        "position": db.queue_position(conv["id"]) if status == "waiting" else None,
    })


@app.route("/reset", methods=["POST"])
def reset():
    """Close the current conversation and hand back a brand-new session id."""
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id", "")
    lang = "bn" if data.get("lang", "bn") == "bn" else "en"

    if _valid_session_id(sid):
        db.reset_conversation(sid)
    new_sid = new_session_id()
    db.get_or_create_conversation(new_sid, lang)
    return jsonify({"ok": True, "session_id": new_sid})

# ─── ROUTES — ADMIN (owner only) ──────────────────────────────────────────────
# Endpoints that stay reachable while a forced password change is pending.
PASSWORD_GATE_EXEMPT = ("admin_account", "admin_logout")


def _safe_next(target, fallback):
    """Honour only same-site relative paths — blocks open redirects."""
    if not target:
        return fallback
    target = str(target).strip()
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return fallback
    if any(ch in target for ch in "\r\n\t"):
        return fallback
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return fallback
    return target


# ─── LOGIN THROTTLE (in-memory) ───────────────────────────────────────────────
def _login_keys(ip, username):
    """Throttle keys for an attempt: the (IP, username) pair, plus a per-IP cap.

    The pair key means one username being attacked from a single address
    cannot lock that username out from anywhere else. The separate ``ip:`` key
    is a looser safety net against one address spraying many usernames.
    """
    ip = ip or "unknown"
    keys = [("pair", "pair:%s|%s" % (ip, str(username or "").lower()), LOGIN_FAIL_LIMIT)]
    keys.append(("ip", "ip:%s" % ip, LOGIN_IP_FAIL_LIMIT))
    return keys


def _login_blocked(ip, username):
    """True while this (IP, username) pair or the whole IP is locked out."""
    now = time.time()
    with _login_lock:
        for _kind, key, _limit in _login_keys(ip, username):
            until = _login_blocks.get(key)
            if until is None:
                continue
            if now < until:
                return True
            del _login_blocks[key]
    return False


def _login_failure(ip, username):
    """Record a failed attempt; lock a key once its own limit is reached."""
    now = time.time()
    with _login_lock:
        for _kind, key, limit in _login_keys(ip, username):
            recent = [t for t in _login_failures.get(key, [])
                      if now - t < LOGIN_FAIL_WINDOW]
            recent.append(now)
            _login_failures[key] = recent
            if len(recent) >= limit:
                _login_blocks[key] = now + LOGIN_BLOCK_SECONDS
                _login_failures.pop(key, None)


def _login_success(ip, username):
    """Clear counters for a key once it authenticates successfully."""
    with _login_lock:
        for _kind, key, _limit in _login_keys(ip, username):
            _login_failures.pop(key, None)
            _login_blocks.pop(key, None)


def _session_idle_expired():
    """True when the admin session has been idle longer than the limit."""
    last = session.get("last_active")
    if last is None:
        return False
    try:
        last = float(last)
    except (TypeError, ValueError):
        return True
    return (time.time() - last) > (SESSION_IDLE_HOURS * 3600)


def _start_admin_session(admin):
    """Clear the incoming session (fixation) and start a fresh owner session."""
    session.clear()
    session["admin_logged_in"] = True
    session["admin_id"] = admin["id"]
    session["admin_role"] = admin.get("role") or "owner"
    session["admin_username"] = admin["username"]
    session["admin_display_name"] = (admin.get("display_name")
                                     or admin["username"])
    session["admin_session_version"] = int(admin.get("session_version") or 0)
    session["last_active"] = time.time()
    session.permanent = True
    _csrf_token()   # a brand-new token after clearing the session


def _session_version_matches(admin, session_key):
    """True when the session's stored version matches the database value."""
    stored = session.get(session_key)
    return str(stored if stored is not None else "") == str(
        int(admin.get("session_version") or 0))


def _admin_gate():
    """Shared guard for /admin. Returns a response to send, or ``None``."""
    if not session.get("admin_logged_in"):
        # Logged in as an agent (but not an owner) — refuse the admin area.
        if session.get("agent_id"):
            return ("Forbidden — agent accounts cannot open /admin.", 403)
        return redirect(url_for("admin_login"))
    if session.get("admin_role") != "owner":
        return ("Forbidden — agent accounts cannot open /admin.", 403)

    admin = db.get_admin_by_id(session.get("admin_id"))
    if (not admin or admin.get("role") != "owner"
            or not int(admin.get("active", 1) or 0)
            or not _session_version_matches(admin, "admin_session_version")):
        session.clear()
        return redirect(url_for("admin_login"))
    if _session_idle_expired():
        session.clear()
        flash("Your session expired after %d hours of inactivity — "
              "please sign in again." % SESSION_IDLE_HOURS, "notice")
        return redirect(url_for("admin_login"))
    session["last_active"] = time.time()

    if admin.get("must_change_password") and request.endpoint not in PASSWORD_GATE_EXEMPT:
        flash("For your security, please choose a new password before using "
              "the admin panel.", "notice")
        return redirect(url_for("admin_account"))
    return None


def owner_required(f):
    """Allow only a logged-in owner; agent accounts get a hard 403.

    POST requests additionally need a valid CSRF token — either the hidden
    ``csrf_token`` form field or the ``X-CSRF-Token`` header.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        blocked = _admin_gate()
        if blocked is not None:
            return blocked
        if request.method == "POST" and not _csrf_ok():
            return ("Missing or invalid security token (CSRF). "
                    "Go back, reload the page and try again.", 403)
        return f(*args, **kwargs)
    return decorated


# POST-only admin routes share the exact same guard.
owner_post_required = owner_required


@app.context_processor
def _inject_admin_context():
    """Expose the signed-in owner's name to the shared admin layout."""
    name = session.get("admin_display_name")
    if not name and session.get("admin_logged_in"):
        row = db.get_admin_by_id(session.get("admin_id")) or {}
        name = row.get("display_name") or row.get("username") or ""
    return {"admin_name": name or "",
            "format_price": db.format_price,
            "weekday_names": WEEKDAY_NAMES}


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    nxt = (request.form.get("next") if request.method == "POST"
           else request.args.get("next"))
    if request.method == "POST":
        if not _csrf_ok():
            return ("Missing or invalid security token (CSRF). "
                    "Reload the page and try again.", 403)
        ip = _client_ip()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if _login_blocked(ip, username):
            ADMIN_LOGGER.warning("Admin login blocked: username=%r ip=%s",
                                 username, ip)
            db.audit("login_blocked", target=username,
                     detail="Admin login blocked after repeated failures.")
            error = ("Too many failed attempts. Please wait a few minutes "
                     "and try again.")
        else:
            admin = db.verify_admin(username, password) if username else None
            if admin and admin.get("role") == "owner":
                _login_success(ip, username)
                _start_admin_session(admin)
                if admin.get("must_change_password"):
                    flash("Please choose a new password to continue.", "notice")
                    return redirect(url_for("admin_account"))
                return redirect(_safe_next(nxt, url_for("admin_panel")))
            if admin and admin.get("role") == "agent":
                # Right password, wrong portal — don't count it as a failure.
                _login_success(ip, username)
                error = "This is an agent account — please use the agent login."
            else:
                _login_failure(ip, username)
                ADMIN_LOGGER.warning("Admin login failed: username=%r ip=%s",
                                     username, ip)
                db.audit("login_failed", target=username,
                         detail="Admin login failed (bad credentials or inactive account).")
                error = "Invalid username or password."
    return render_template("admin_login.html", error=error,
                           next_value=_safe_next(nxt, ""))


@app.route("/admin/logout", methods=["POST"])
@owner_post_required
def admin_logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("admin_login"))


@app.route("/admin")
@owner_required
def admin_panel():
    """Alias for the appointments page (the dashboard replaces it later)."""
    return _render_appointments_page()


@app.route("/admin/appointments")
@owner_required
def admin_appointments():
    return _render_appointments_page()


@app.route("/admin/delete/<int:appt_id>", methods=["POST"])
@owner_post_required
def admin_delete(appt_id):
    if db.delete_appointment(appt_id):
        db.audit("appointment_deleted", target="#%d" % appt_id,
                 detail="Appointment deleted from the legacy delete route.")
        flash("Appointment deleted.", "success")
    else:
        flash("That appointment no longer exists.", "error")
    return redirect(url_for("admin_panel"))


# ─── ADMIN: APPOINTMENTS ──────────────────────────────────────────────────────
APPT_PER_PAGE = 20
APPT_NOTES_MAX = 500
APPT_QUERY_MAX = 100
APPT_STATUS_LABELS = {
    "pending":   "Pending",
    "confirmed": "Confirmed",
    "completed": "Completed",
    "cancelled": "Cancelled",
    "no_show":   "No show",
}
APPT_STATUS_CLASS = {
    "pending":   "pending",
    "confirmed": "confirmed",
    "completed": "completed",
    "cancelled": "cancelled",
    "no_show":   "no-show",
}
APPT_SOURCE_LABELS = {"chat": "Chat", "manual": "Manual"}


def _valid_iso_date(value):
    try:
        return datetime.date.fromisoformat(str(value or "").strip()).isoformat()
    except (ValueError, TypeError):
        return ""


def _appointment_filters():
    """Read + validate the list filters from the query string.

    Unknown / malformed values are ignored rather than trusted, so a crafted
    URL cannot break the page or the CSV export.
    """
    args = request.args
    date_from = _valid_iso_date(args.get("date_from"))
    date_to = _valid_iso_date(args.get("date_to"))
    doctor_id = ""
    raw_doctor = (args.get("doctor") or "").strip()
    if raw_doctor.isdigit():
        doctor = db.get_doctor(int(raw_doctor))
        if doctor:
            doctor_id = str(doctor["id"])
    status = (args.get("status") or "").strip()
    if status not in db.APPOINTMENT_STATUSES:
        status = ""
    source = (args.get("source") or "").strip()
    if source not in db.APPOINTMENT_SOURCES:
        source = ""
    q = (args.get("q") or "").strip()[:APPT_QUERY_MAX]
    attention = "1" if (args.get("attention") or "").strip() == "1" else ""
    sort = "created" if (args.get("sort") or "").strip() == "created" else "date"
    page = args.get("page", type=int) or 1
    if page < 1:
        page = 1
    return {
        "date_from": date_from,
        "date_to": date_to,
        "doctor": doctor_id,
        "status": status,
        "source": source,
        "q": q,
        "attention": attention,
        "sort": sort,
        "page": page,
    }


def _appointment_query_args(filters, **overrides):
    """Merge filter values into a query-string dict, dropping empty ones."""
    data = dict(filters)
    data.update(overrides)
    clean = {}
    for key, value in data.items():
        if value in (None, "", 0):
            continue
        if key == "page" and int(value) == 1:
            continue
        if key == "sort" and value == "date":
            continue
        clean[key] = value
    return clean


def _appointment_filter_summary(filters):
    """Short, value-free summary for the export audit entry."""
    bits = []
    for key in ("date_from", "date_to", "doctor", "status", "source", "q",
                "attention", "sort"):
        value = filters.get(key)
        if value:
            bits.append("%s=%s" % (key, value))
    return ", ".join(bits) if bits else "no filters"


def _appointment_attention(appt, doctor_cache, today):
    """Reasons an upcoming non-cancelled appointment no longer fits.

    A change the owner confirmed earlier can leave a booking outside the
    doctor's current schedule, off the slot grid, on a day off, or with a
    disabled doctor. Nothing is auto-cancelled; the list flags it instead.
    """
    if (appt.get("status") or "pending") == "cancelled":
        return []
    date = str(appt.get("date") or "")
    if not date or date < today:
        return []
    doctor_id = appt.get("doctor_id")
    if not doctor_id:
        return []
    if doctor_id not in doctor_cache:
        doctor_cache[doctor_id] = db.get_doctor(doctor_id)
    doctor = doctor_cache[doctor_id]
    if not doctor:
        return []
    reasons = []
    if not int(doctor.get("active", 1) or 0):
        reasons.append("doctor is disabled")
    if db.is_doctor_off(doctor_id, date):
        reasons.append("doctor is on leave")
    if not _slot_boundary_ok(doctor_id, doctor, date, appt.get("time")):
        reasons.append("outside the current schedule")
    return reasons


def _appointment_rows(filters):
    """All matching rows with the needs-attention flag, sorted for display."""
    rows = db.list_appointments_filtered(
        date_from=filters["date_from"] or None,
        date_to=filters["date_to"] or None,
        doctor_id=int(filters["doctor"]) if filters["doctor"] else None,
        status=filters["status"] or None,
        source=filters["source"] or None,
        q=filters["q"] or None,
    )
    today = db.dhaka_today().isoformat()
    doctor_cache = {}
    for row in rows:
        reasons = _appointment_attention(row, doctor_cache, today)
        row["attention_reasons"] = reasons
        row["needs_attention"] = bool(reasons)
        row["status"] = row.get("status") or "pending"
    if filters["attention"]:
        rows = [row for row in rows if row["needs_attention"]]
    if filters["sort"] == "created":
        rows.sort(key=lambda r: (r.get("created_at") or "", r["id"]),
                  reverse=True)
    else:
        upcoming = [r for r in rows if (r.get("date") or "") >= today]
        past = [r for r in rows if (r.get("date") or "") < today]
        upcoming.sort(key=lambda r: (r.get("date") or "",
                                     r.get("time") or "", r["id"]))
        past.sort(key=lambda r: (r.get("date") or "",
                                 r.get("time") or "", r["id"]), reverse=True)
        rows = upcoming + past
    return rows


def _render_appointments_page():
    filters = _appointment_filters()
    rows = _appointment_rows(filters)
    total = len(rows)
    pages = max(1, (total + APPT_PER_PAGE - 1) // APPT_PER_PAGE)
    page = min(filters["page"], pages)
    start = (page - 1) * APPT_PER_PAGE
    page_rows = rows[start:start + APPT_PER_PAGE]
    filters["page"] = page

    def query_url(**overrides):
        return url_for("admin_appointments",
                       **_appointment_query_args(filters, **overrides))

    def export_query_url():
        return url_for("admin_appointments_export",
                       **_appointment_query_args(filters))

    return render_template(
        "admin_appointments.html", nav="appointments",
        rows=page_rows, total=total, page=page, pages=pages,
        per_page=APPT_PER_PAGE, filters=filters,
        query_url=query_url, export_url=export_query_url(),
        doctors=db.list_doctors(), statuses=db.APPOINTMENT_STATUSES,
        status_labels=APPT_STATUS_LABELS, status_class=APPT_STATUS_CLASS,
        source_labels=APPT_SOURCE_LABELS,
        transcription_count=sum(1 for r in rows if r["needs_attention"]),
    )


def _appointment_form_values(appt=None):
    appt = appt or {}
    return {
        "patient_name": appt.get("patient_name") or "",
        "phone": appt.get("phone") or "",
        "doctor_id": str(appt.get("doctor_id") or ""),
        "service_id": str(appt.get("service_id") or ""),
        "date": appt.get("date") or "",
        "time": appt.get("time") or "",
        "notes": appt.get("notes") or "",
        "status": appt.get("status") or "pending",
    }


def _appointment_prefill():
    """Validated query-string prefill for the manual booking form.

    Only an existing active doctor, a sensible date and a slot that is really
    free are copied into the form; everything else is ignored.
    """
    values = _appointment_form_values({"status": "confirmed"})
    raw_doctor = (request.args.get("doctor") or "").strip()
    doctor = db.get_doctor(int(raw_doctor)) if raw_doctor.isdigit() else None
    if not doctor or not int(doctor.get("active", 1) or 0):
        return values
    date = _valid_iso_date(request.args.get("date"))
    today = db.dhaka_today()
    if not date:
        return values
    try:
        day = datetime.date.fromisoformat(date)
    except ValueError:
        return values
    if day < today or (day - today).days > MAX_DAYS_AHEAD:
        return values
    values["doctor_id"] = str(doctor["id"])
    values["date"] = date
    raw_time = (request.args.get("time") or "").strip()
    if TIME_RE.match(raw_time) and raw_time in db.get_available_slots(
            doctor["id"], date):
        values["time"] = raw_time
    return values


def _appointment_status_options(current=None):
    """The current status plus the transitions allowed from it."""
    current = current or "pending"
    if current not in db.APPOINTMENT_STATUSES:
        current = "pending"
    allowed = db.APPOINTMENT_TRANSITIONS.get(current, set())
    return [current] + [s for s in db.APPOINTMENT_STATUSES if s in allowed]


def _validate_admin_appointment_form(form, exclude_id=None, override=False):
    """Validate the shared new/edit appointment form.

    Normal saves go through the chatbot's own :func:`_validate_booking` for the
    doctor, schedule, slot-grid, days-off and double-booking checks. The
    emergency override skips only the schedule/slot/days-off checks; an exact
    duplicate is still refused later inside the BEGIN IMMEDIATE transaction.
    """
    values = _appointment_form_values()
    for key in ("patient_name", "phone", "doctor_id", "service_id", "date",
                "time", "notes", "status"):
        if key in form:
            values[key] = (form.get(key) or "").strip()
    errors = []

    name = values["patient_name"]
    if not name:
        errors.append("Patient name is required.")
    elif len(name) > MAX_NAME_LEN:
        errors.append("Patient name must be %d characters or fewer." % MAX_NAME_LEN)

    phone = db.normalize_phone(values["phone"])
    if not phone:
        errors.append("Phone number is required.")
    elif not db.is_valid_phone(phone):
        errors.append("Phone must be a valid Bangladeshi mobile number "
                      "(e.g. 01712345678).")
    values["phone"] = phone

    raw_doctor = values["doctor_id"]
    doctor = None
    if not raw_doctor.isdigit():
        errors.append("Please choose a doctor.")
    else:
        doctor = db.get_doctor(int(raw_doctor))
        if not doctor:
            errors.append("That doctor no longer exists.")
        elif not int(doctor.get("active", 1) or 0):
            errors.append("That doctor is disabled — choose an active doctor.")

    service_id = None
    if values["service_id"]:
        if not values["service_id"].isdigit():
            errors.append("Please choose a valid service.")
        else:
            service = db.get_service(int(values["service_id"]))
            if not service:
                errors.append("That service no longer exists.")
            else:
                service_id = service["id"]

    date = _valid_iso_date(values["date"])
    day = None
    if not date:
        errors.append("Please choose a valid date.")
    else:
        day = datetime.date.fromisoformat(date)
        today = db.dhaka_today()
        if day < today:
            errors.append("The date is in the past.")
        elif (day - today).days > MAX_DAYS_AHEAD:
            errors.append("Appointments can be booked at most %d days ahead."
                          % MAX_DAYS_AHEAD)
    values["date"] = date

    time_value = values["time"]
    if not TIME_RE.match(time_value):
        errors.append("Please choose a valid time (HH:MM).")

    notes = values["notes"]
    if len(notes) > APPT_NOTES_MAX:
        errors.append("Notes must be %d characters or fewer." % APPT_NOTES_MAX)

    status = values["status"] or "pending"
    if status not in db.APPOINTMENT_STATUSES:
        errors.append("Please choose a valid status.")
    elif status in ("completed", "no_show"):
        candidate = {"date": date, "time": time_value}
        if db.appointment_is_upcoming(candidate):
            errors.append("Completed and no-show can only be set once the "
                          "appointment time has passed.")

    if not errors and not override:
        payload = {
            "patient_name": name,
            "phone": phone,
            "doctor_id": doctor["id"],
            "date": date,
            "time": time_value,
            "notes": notes,
        }
        booking, error = _validate_booking(
            payload, "en", exclude_appointment_id=exclude_id)
        if error:
            errors.append(error)
        else:
            values["patient_name"] = booking["patient_name"]
            values["phone"] = booking["phone"]
            values["date"] = booking["date"]
            values["time"] = booking["time"]

    values["doctor_id"] = str(doctor["id"]) if doctor else values["doctor_id"]
    values["service_id"] = str(service_id) if service_id else ""
    values["notes"] = notes
    values["status"] = status if status in db.APPOINTMENT_STATUSES else "pending"
    return values, errors


def _appointment_form_context(values, errors, mode, appointment=None,
                              override=False):
    current = values.get("status") if mode == "edit" else None
    return {
        "nav": "appointments",
        "mode": mode,
        "appointment": appointment,
        "values": values,
        "errors": errors,
        "doctors": db.list_doctors(),
        "services": db.list_services(),
        "status_choices": (_appointment_status_options(current)
                           if mode == "edit"
                           else list(db.APPOINTMENT_STATUSES)),
        "status_labels": APPT_STATUS_LABELS,
        "notes_max": APPT_NOTES_MAX,
        "override": override,
    }


def _appointment_return_url():
    return _safe_next(request.form.get("next"),
                      url_for("admin_appointments"))


@app.route("/admin/appointments/new", methods=["GET", "POST"])
@owner_required
def admin_appointment_new():
    if request.method == "POST":
        values, errors = _validate_admin_appointment_form(request.form)
        if not errors:
            appt_id, error = db.create_admin_appointment(
                patient_name=values["patient_name"], phone=values["phone"],
                doctor_id=int(values["doctor_id"]),
                service_id=int(values["service_id"]) if values["service_id"] else None,
                date=values["date"], time=values["time"],
                status=values["status"], notes=values["notes"] or None,
                language=None, source="manual")
            if error == "slot_taken":
                errors.append("That doctor already has an active appointment "
                              "at that date and time.")
            elif error:
                errors.append("The appointment could not be saved.")
            else:
                db.audit("appointment_created", target="#%d" % appt_id,
                         detail="Manual booking: %s, %s %s, status %s."
                                % (values["patient_name"], values["date"],
                                   values["time"], values["status"]))
                flash("Appointment created.", "success")
                return redirect(url_for("admin_appointments"))
        return render_template(
            "admin_appointment_edit.html",
            **_appointment_form_context(values, errors, "new"))
    values = _appointment_prefill()
    return render_template(
        "admin_appointment_edit.html",
        **_appointment_form_context(values, [], "new"))


@app.route("/admin/appointments/<int:appt_id>", methods=["GET", "POST"])
@owner_required
def admin_appointment_edit(appt_id):
    appointment = db.get_appointment(appt_id)
    if appointment is None:
        flash("That appointment no longer exists.", "error")
        return redirect(url_for("admin_appointments"))

    if request.method == "POST":
        override = bool(request.form.get("override_schedule"))
        values, errors = _validate_admin_appointment_form(
            request.form, exclude_id=appt_id, override=override)
        if not errors:
            updated, error = db.update_admin_appointment(
                appt_id,
                patient_name=values["patient_name"], phone=values["phone"],
                doctor_id=int(values["doctor_id"]),
                service_id=(int(values["service_id"])
                            if values["service_id"] else None),
                date=values["date"], time=values["time"],
                notes=values["notes"] or None, status=values["status"])
            if error == "slot_taken":
                errors.append("That doctor already has another active "
                              "appointment at that exact date and time.")
            elif error == "invalid_transition":
                errors.append("That status change is not allowed from '%s'."
                              % (appointment.get("status") or "pending"))
            elif error == "future":
                errors.append("Completed and no-show can only be set once the "
                              "appointment time has passed.")
            elif error:
                errors.append("That appointment no longer exists.")
            else:
                db.audit(
                    "appointment_edited", target="#%d" % appt_id,
                    detail="Edited: %s, %s %s, status %s%s."
                           % (values["patient_name"], values["date"],
                              values["time"], values["status"],
                              "; schedule override used" if override else ""))
                flash("Appointment updated.", "success")
                return redirect(url_for("admin_appointments"))
        return render_template(
            "admin_appointment_edit.html",
            **_appointment_form_context(values, errors, "edit",
                                        appointment=appointment,
                                        override=override))

    values = _appointment_form_values(appointment)
    return render_template(
        "admin_appointment_edit.html",
        **_appointment_form_context(values, [], "edit",
                                    appointment=appointment))


@app.route("/admin/appointments/<int:appt_id>/status", methods=["POST"])
@owner_required
def admin_appointment_status(appt_id):
    appointment = db.get_appointment(appt_id)
    if appointment is None:
        flash("That appointment no longer exists.", "error")
        return redirect(url_for("admin_appointments"))
    new_status = (request.form.get("status") or "").strip()
    updated, error = db.transition_appointment(appt_id, new_status)
    if error == "invalid_transition":
        flash("Cannot change a %s appointment to %s."
              % (appointment.get("status") or "pending",
                 APPT_STATUS_LABELS.get(new_status, new_status)), "error")
    elif error == "future":
        flash("Completed and no-show can only be set once the appointment "
              "time has passed.", "error")
    elif error == "slot_taken":
        flash("That slot is no longer free, so the appointment cannot be "
              "reopened.", "error")
    elif error:
        flash("That status change is not allowed.", "error")
    else:
        db.audit("appointment_status", target="#%d" % appt_id,
                 detail="Status %s -> %s."
                        % (appointment.get("status") or "pending", new_status))
        flash("Appointment is now %s."
              % APPT_STATUS_LABELS.get(new_status, new_status).lower(), "success")
    return redirect(_appointment_return_url())


@app.route("/admin/appointments/<int:appt_id>/cancel", methods=["POST"])
@owner_required
def admin_appointment_cancel(appt_id):
    appointment = db.get_appointment(appt_id)
    if appointment is None:
        flash("That appointment no longer exists.", "error")
        return redirect(url_for("admin_appointments"))
    if (appointment.get("status") or "pending") == "cancelled":
        flash("That appointment is already cancelled.", "notice")
        return redirect(_appointment_return_url())
    updated, error = db.transition_appointment(appt_id, "cancelled")
    if error:
        flash("That appointment cannot be cancelled from its current status.",
              "error")
    else:
        db.audit("appointment_cancelled", target="#%d" % appt_id,
                 detail="Cancelled; the slot is free again.")
        flash("Appointment cancelled — the slot is free again.", "success")
    return redirect(_appointment_return_url())


@app.route("/admin/appointments/<int:appt_id>/delete", methods=["POST"])
@owner_required
def admin_appointment_delete(appt_id):
    appointment = db.get_appointment(appt_id)
    if not appointment:
        flash("That appointment no longer exists.", "error")
        return redirect(_appointment_return_url())
    db.delete_appointment(appt_id)
    db.audit("appointment_deleted", target="#%d" % appt_id,
             detail="Permanently deleted: %s, %s %s."
                    % (appointment.get("patient_name"),
                       appointment.get("date"), appointment.get("time")))
    flash("Appointment permanently deleted.", "success")
    return redirect(_appointment_return_url())


def _csv_safe(value):
    """Neutralise spreadsheet formulas in exported cells."""
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


@app.route("/admin/appointments/export.csv")
@owner_required
def admin_appointments_export():
    """Stream the current filtered/sorted list as a BOM-prefixed CSV."""
    filters = _appointment_filters()
    rows = _appointment_rows(filters)
    header = ["id", "patient", "phone", "doctor", "service", "date", "time",
              "status", "source", "language", "created_at", "notes"]

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        yield "\ufeff" + buffer.getvalue()
        for row in rows:
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([
                _csv_safe(row.get("id")),
                _csv_safe(row.get("patient_name")),
                _csv_safe(row.get("phone")),
                _csv_safe(row.get("doctor_name_en")
                          or row.get("doctor_name_bn")),
                _csv_safe(row.get("service_name_en")
                          or row.get("service_name_bn")),
                _csv_safe(row.get("date")),
                _csv_safe(row.get("time")),
                _csv_safe(row.get("status")),
                _csv_safe(row.get("source")),
                _csv_safe(row.get("language")),
                _csv_safe(row.get("created_at")),
                _csv_safe(row.get("notes")),
            ])
            yield buffer.getvalue()

    db.audit("appointments_exported", target="appointments.csv",
             detail="CSV export (%s); %d row(s)."
                    % (_appointment_filter_summary(filters), len(rows)))
    response = app.response_class(generate(), mimetype="text/csv")
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        'attachment; filename="appointments.csv"')
    return response



# ─── ADMIN: SETTINGS FIELD SPEC ───────────────────────────────────────────────
# Text fields: (key, label, max length).
ADMIN_TEXT_FIELDS = [
    ("clinic_name",      "Clinic name (English)", 120),
    ("clinic_name_bn",   "ক্লিনিকের নাম (বাংলা)", 120),
    ("address",          "Address (English)", 200),
    ("address_bn",       "ঠিকানা (বাংলা)", 200),
    ("phone",            "Phone number", 40),
    ("opening_hours",    "Opening hours (English)", 200),
    ("opening_hours_bn", "খোলার সময় (বাংলা)", 200),
    ("persona_name",     "Receptionist name (English)", 60),
    ("persona_name_bn",  "রিসেপশনিস্টের নাম (বাংলা)", 60),
    ("welcome_en",       "Welcome message (English)", 300),
    ("welcome_bn",       "স্বাগত বার্তা (বাংলা)", 300),
    ("ai_model",         "AI model", 120),
]
# Text areas: (key, label, max length).
ADMIN_TEXTAREA_FIELDS = [
    ("extra_info_en", "Extra info (FAQs / policies) — English", 4000),
    ("extra_info_bn", "অতিরিক্ত তথ্য (FAQ / নীতিমালা) — বাংলা", 4000),
]
# Numeric fields: (key, label, minimum, maximum, unit).
ADMIN_INT_FIELDS = [
    ("handoff_timeout_minutes",     "Handoff timeout",       1,   60,   "minutes"),
    ("agent_online_window_seconds", "Agent online window",   10,  600,   "seconds"),
    ("idle_close_minutes",          "Idle chat auto-close",  1,   1440,  "minutes"),
    ("customer_away_seconds",       "Customer marked 'away'", 5,   600,  "seconds"),
    ("customer_left_seconds",       "Customer marked 'left'", 10,  3600,  "seconds"),
]
# Shown in the datalist under the AI model input.
AI_MODEL_SUGGESTIONS = [
    "openrouter/auto",
    "openai/gpt-4o-mini",
    "anthropic/claude-3.5-sonnet",
    "google/gemini-flash-1.5",
    "meta-llama/llama-3.1-70b-instruct",
]
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._\-/:]+$")

# Field groups for the Settings page, selected from ADMIN_TEXT_FIELDS by key so
# reordering the spec above can't silently scramble the sections.
_FIELD_SPEC_BY_KEY = {key: (key, label, maxlen)
                      for key, label, maxlen in ADMIN_TEXT_FIELDS}
CLINIC_FIELDS = [_FIELD_SPEC_BY_KEY[k] for k in (
    "clinic_name", "clinic_name_bn", "address", "address_bn", "phone",
    "opening_hours", "opening_hours_bn")]
PERSONA_FIELDS = [_FIELD_SPEC_BY_KEY[k] for k in (
    "persona_name", "persona_name_bn", "welcome_en", "welcome_bn")]
# ADMIN_TEXT_FIELDS also holds "ai_model", which gets its own AI section.


@app.route("/admin/settings", methods=["GET", "POST"])
@owner_required
def admin_settings():
    settings = db.get_settings()
    errors = []
    values = {key: (settings.get(key) or "")
              for key, _label, _max in ADMIN_TEXT_FIELDS}
    values.update({key: (settings.get(key) or "")
                   for key, _label, _max in ADMIN_TEXTAREA_FIELDS})
    values.update({key: (settings.get(key) or "")
                   for key, _label, _lo, _hi, _unit in ADMIN_INT_FIELDS})

    if request.method == "POST":
        typed = dict(values)
        for key, label, maxlen in ADMIN_TEXT_FIELDS:
            if key not in request.form:
                continue
            value = request.form.get(key, "").strip()
            typed[key] = value
            if len(value) > maxlen:
                errors.append("%s must be %d characters or fewer."
                              % (label, maxlen))
            elif key == "ai_model" and value and not MODEL_NAME_RE.match(value):
                errors.append("AI model may only contain letters, numbers and "
                              "the characters . _ - / :")
        for key, label, maxlen in ADMIN_TEXTAREA_FIELDS:
            if key not in request.form:
                continue
            value = request.form.get(key, "")
            typed[key] = value
            if len(value) > maxlen:
                errors.append("%s must be %d characters or fewer."
                              % (label, maxlen))
        for key, label, lo, hi, unit in ADMIN_INT_FIELDS:
            if key not in request.form:
                continue
            raw = (request.form.get(key) or "").strip()
            typed[key] = raw
            try:
                amount = int(raw)
            except (TypeError, ValueError):
                errors.append("%s must be a whole number of %s."
                              % (label, unit))
                continue
            if amount < lo or amount > hi:
                errors.append("%s must be between %d and %d %s."
                              % (label, lo, hi, unit))
        new_key = (request.form.get("openrouter_api_key") or "").strip()
        if new_key:
            if len(new_key) > 300:
                errors.append("That API key looks too long.")
            elif not _usable_key(new_key):
                errors.append("That looks like a placeholder, not a real key. "
                              "Paste the key from openrouter.ai.")

        if errors:
            # Re-render exactly what was typed so nothing is lost.
            return render_template("admin_settings.html", nav="settings",
                                   values=typed, errors=errors,
                                   clinic_fields=CLINIC_FIELDS,
                                   persona_fields=PERSONA_FIELDS,
                                   textarea_fields=ADMIN_TEXTAREA_FIELDS,
                                   int_fields=ADMIN_INT_FIELDS,
                                   key_status=api_key_status(),
                                   models=AI_MODEL_SUGGESTIONS,
                                   prompt_bn=prompt_builder.build_system_prompt("bn"),
                                   prompt_en=prompt_builder.build_system_prompt("en"))

        before = dict(settings)
        changed_keys = []
        for key, _label, _max in ADMIN_TEXT_FIELDS:
            if key in request.form:
                new_value = request.form.get(key, "").strip()
                if before.get(key) != new_value:
                    changed_keys.append(key)
                db.set_setting(key, new_value)
        for key, _label, _max in ADMIN_TEXTAREA_FIELDS:
            if key in request.form:
                new_value = request.form.get(key, "")
                if before.get(key) != new_value:
                    changed_keys.append(key)
                db.set_setting(key, new_value)
        for key, _label, _lo, _hi, _unit in ADMIN_INT_FIELDS:
            if key in request.form:
                new_value = typed.get(key, "").strip()
                if before.get(key) != new_value:
                    changed_keys.append(key)
                db.set_setting(key, new_value)
        api_key_changed = bool(new_key and before.get("openrouter_api_key") != new_key)
        if new_key:
            db.set_setting("openrouter_api_key", new_key)

        # Audit which keys changed — never their values (the API key is only
        # ever reported as "api key changed").
        visible_keys = [k for k in changed_keys if k != "openrouter_api_key"]
        details = []
        if visible_keys:
            details.append("Changed keys: %s." % ", ".join(visible_keys))
        else:
            details.append("No clinic/AI/timing values changed.")
        if api_key_changed:
            details.append("api key changed")
        db.audit("settings_saved", target="settings",
                 detail=" ".join(details))
        flash("Settings saved — the chatbot uses them immediately, "
              "no restart needed.", "success")
        return redirect(url_for("admin_settings"))

    return render_template("admin_settings.html", nav="settings", values=values,
                           errors=errors, clinic_fields=CLINIC_FIELDS,
                           persona_fields=PERSONA_FIELDS,
                           textarea_fields=ADMIN_TEXTAREA_FIELDS,
                           int_fields=ADMIN_INT_FIELDS,
                           key_status=api_key_status(),
                           models=AI_MODEL_SUGGESTIONS,
                           prompt_bn=prompt_builder.build_system_prompt("bn"),
                           prompt_en=prompt_builder.build_system_prompt("en"))


@app.route("/admin/settings/test_ai", methods=["POST"])
@owner_post_required
def admin_settings_test_ai():
    """Tiny OpenRouter request; reports success/failure without the key."""
    ok, message = test_ai_connection()
    return jsonify({"ok": ok, "message": message})


@app.route("/admin/account", methods=["GET", "POST"])
@owner_required
def admin_account():
    admin = db.get_admin_by_id(session.get("admin_id")) or {}
    errors = []
    form = {"display_name": session.get("admin_display_name") or admin.get("display_name") or "",
            "username": session.get("admin_username") or admin.get("username") or ""}

    if request.method == "POST":
        if request.form.get("action") == "password":
            current = request.form.get("current_password") or ""
            new = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_password") or ""
            if not db.verify_admin(admin.get("username"), current):
                errors.append("Your current password is incorrect.")
            if new != confirm:
                errors.append("The two new passwords don't match.")
            errors.extend(_password_policy_errors(form["username"], new, current))
            if not errors:
                db.update_admin(admin["id"], password=new,
                                must_change_password=False)
                db.audit("password_changed", target=admin.get("username"),
                         detail="Owner changed their own password.")
                flash("Password updated.", "success")
                return redirect(url_for("admin_account"))
        else:
            display = (request.form.get("display_name") or "").strip()
            username = (request.form.get("username") or "").strip()
            form["display_name"] = display
            form["username"] = username
            if not display:
                errors.append("Display name is required.")
            elif len(display) > 80:
                errors.append("Display name must be 80 characters or fewer.")
            if not username:
                errors.append("Username is required.")
            elif not re.match(r"^[A-Za-z0-9_.\-]{3,40}$", username):
                errors.append("Username must be 3–40 characters using only "
                              "letters, numbers, dots, dashes or underscores.")
            else:
                other = db.get_admin(username)
                if other and other["id"] != admin.get("id"):
                    errors.append("That username is already taken.")
            if not errors:
                db.update_admin(admin["id"], username=username,
                                display_name=display)
                session["admin_username"] = username
                session["admin_display_name"] = display
                flash("Profile updated.", "success")
                return redirect(url_for("admin_account"))

    return render_template("admin_account.html", nav="account",
                           errors=errors, form=form,
                           must_change=bool(admin.get("must_change_password")))


def _password_policy_errors(username, new_password, current_password=None):
    """Validate a proposed password. Returns a list of friendly errors."""
    errors = []
    if len(new_password) < 8:
        errors.append("The new password must be at least 8 characters.")
    if new_password and new_password.lower() == (username or "").lower():
        errors.append("The password can't be your username.")
    default_password = (os.environ.get("ADMIN_PASSWORD")
                        or load_config().get("admin_password") or "")
    if default_password and new_password == default_password:
        errors.append("That's the original default password — please choose "
                      "a different one.")
    if current_password is not None and new_password == current_password:
        errors.append("The new password must be different from the current one.")
    return errors


# ─── ADMIN: TEAM ──────────────────────────────────────────────────────────────
TEAM_USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,30}$")
TEAM_DISPLAY_MAX = 40
TEAM_HTML_RE = re.compile(r"[<>]")
TEAM_URL_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _temp_password():
    """A random temporary password (16 chars) from the secrets module."""
    return secrets.token_urlsafe(12)


def _validate_team_display(value):
    """Validate a display name; returns an error string or ``None``."""
    display = (value or "").strip()
    if not display:
        return "Display name is required."
    if len(display) > TEAM_DISPLAY_MAX:
        return ("Display name must be %d characters or fewer."
                % TEAM_DISPLAY_MAX)
    if CONTROL_CHARS_RE.search(display):
        return "Display name can't contain control characters."
    if TEAM_HTML_RE.search(display):
        return "Display name can't contain HTML."
    if TEAM_URL_RE.search(display):
        return "Display name can't contain a URL."
    return None


def _validate_team_form(form):
    """Validate the add-user form. Returns ``(values, errors)``."""
    values = {
        "username": (form.get("username") or "").strip(),
        "display_name": (form.get("display_name") or "").strip(),
        "role": (form.get("role") or "agent").strip(),
    }
    errors = []
    if not TEAM_USERNAME_RE.match(values["username"]):
        errors.append("Username must be 3–30 characters using only lowercase "
                      "letters, digits, dots, dashes or underscores.")
    elif db.get_admin_case_insensitive(values["username"]):
        errors.append("That username is already taken.")
    display_error = _validate_team_display(values["display_name"])
    if display_error:
        errors.append(display_error)
    if values["role"] not in ("agent", "owner"):
        errors.append("Role must be agent or owner.")
    return values, errors


def _team_rows():
    """Team rows with a computed online indicator (fresh heartbeat only)."""
    rows = db.list_team()
    window = db.get_setting_int("agent_online_window_seconds", 45)
    now = db.dhaka_now()
    for row in rows:
        online = False
        if row.get("is_online") and row.get("last_seen"):
            try:
                last = datetime.datetime.fromisoformat(str(row["last_seen"]))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=db.TZ_DHAKA)
                online = (now - last).total_seconds() <= window
            except (TypeError, ValueError):
                online = False
        row["online"] = online
        row["active"] = int(row.get("active", 1) or 0)
        row["active_chats"] = int(row.get("active_chats") or 0)
    return rows


def _render_team(**overrides):
    context = {
        "nav": "team",
        "rows": _team_rows(),
        "errors": [],
        "form": {"username": "", "display_name": "", "role": "agent"},
        "new_credentials": None,
    }
    context.update(overrides)
    return render_template("admin_team.html", **context)


@app.route("/admin/team")
@owner_required
def admin_team():
    return _render_team()


@app.route("/admin/team/new", methods=["POST"])
@owner_required
def admin_team_new():
    values, errors = _validate_team_form(request.form)
    if errors:
        return _render_team(errors=errors, form=values), 200

    temp_password = _temp_password()
    db.create_admin(values["username"], temp_password,
                    display_name=values["display_name"], role=values["role"],
                    must_change_password=1)
    db.audit("team_added", target=values["username"],
             detail="Added %s (%s); temporary password issued."
                    % (values["display_name"], values["role"]))
    return _render_team(new_credentials={
        "username": values["username"],
        "display_name": values["display_name"],
        "role": values["role"],
        "password": temp_password,
    })


@app.route("/admin/team/<int:admin_id>/name", methods=["POST"])
@owner_required
def admin_team_name(admin_id):
    target = db.get_admin_by_id(admin_id)
    if not target:
        flash("That user no longer exists.", "error")
        return redirect(url_for("admin_team"))
    display = (request.form.get("display_name") or "").strip()
    error = _validate_team_display(display)
    if error:
        flash(error, "error")
        return redirect(url_for("admin_team"))
    db.update_admin(admin_id, display_name=display)
    if admin_id == session.get("admin_id"):
        session["admin_display_name"] = display
    db.audit("team_display_name", target=target["username"],
             detail="Display name changed to %s." % display)
    flash("Display name updated for %s." % target["username"], "success")
    return redirect(url_for("admin_team"))


@app.route("/admin/team/<int:admin_id>/role", methods=["POST"])
@owner_required
def admin_team_role(admin_id):
    target = db.get_admin_by_id(admin_id)
    if not target:
        flash("That user no longer exists.", "error")
        return redirect(url_for("admin_team"))
    role = (request.form.get("role") or "").strip()
    if role not in ("agent", "owner"):
        flash("Role must be agent or owner.", "error")
        return redirect(url_for("admin_team"))
    if admin_id == session.get("admin_id") and role != "owner":
        flash("You can't change your own owner role — ask another owner.", "error")
        return redirect(url_for("admin_team"))
    if (target.get("role") == "owner" and int(target.get("active", 1) or 0)
            and role != "owner"
            and db.count_active_owners(exclude_id=admin_id) == 0):
        flash("The last active owner can't be demoted.", "error")
        return redirect(url_for("admin_team"))
    if target.get("role") == role:
        flash("%s already has the %s role." % (target["username"], role),
              "notice")
        return redirect(url_for("admin_team"))
    db.set_admin_role(admin_id, role)
    db.audit("team_role", target=target["username"],
             detail="Role changed from %s to %s."
                    % (target.get("role"), role))
    flash("%s is now %s." % (target["username"], role), "success")
    return redirect(url_for("admin_team"))


@app.route("/admin/team/<int:admin_id>/toggle", methods=["POST"])
@owner_required
def admin_team_toggle(admin_id):
    target = db.get_admin_by_id(admin_id)
    if not target:
        flash("That user no longer exists.", "error")
        return redirect(url_for("admin_team"))

    is_active = bool(int(target.get("active", 1) or 0))
    if not is_active:
        db.reactivate_admin(admin_id)
        db.audit("team_reactivated", target=target["username"],
                 detail="Account reactivated.")
        flash("%s can sign in again." % target["username"], "success")
        return redirect(url_for("admin_team"))

    if admin_id == session.get("admin_id"):
        flash("You can't deactivate your own account.", "error")
        return redirect(url_for("admin_team"))
    if (target.get("role") == "owner"
            and db.count_active_owners(exclude_id=admin_id) == 0):
        flash("The last active owner can't be deactivated.", "error")
        return redirect(url_for("admin_team"))

    chats = db.list_active_chats_for_agent(admin_id)
    if chats:
        return render_template(
            "admin_confirm_team.html", nav="team", target=target,
            chats=chats, back_url=url_for("admin_team"),
            token=_impact_token({"kind": "deactivate_admin",
                                 "admin_id": admin_id}),
            summary="%s has %d active chat%s."
                    % (target["display_name"] or target["username"],
                       len(chats), "s" if len(chats) != 1 else ""))

    handed_back = db.deactivate_admin(admin_id)
    db.audit("team_deactivated", target=target["username"],
             detail="Account deactivated; %d active chat(s) handed back to the bot."
                    % len(handed_back))
    flash("%s was deactivated. All their sessions have ended." % target["username"],
          "success")
    return redirect(url_for("admin_team"))


@app.route("/admin/team/<int:admin_id>/reset-password", methods=["POST"])
@owner_required
def admin_team_reset_password(admin_id):
    target = db.get_admin_by_id(admin_id)
    if not target:
        flash("That user no longer exists.", "error")
        return redirect(url_for("admin_team"))
    temp_password = _temp_password()
    db.reset_admin_password(admin_id, temp_password)
    db.audit("team_password_reset", target=target["username"],
             detail="Temporary password issued; all sessions ended.")
    return _render_team(new_credentials={
        "username": target["username"],
        "display_name": target.get("display_name") or target["username"],
        "role": target.get("role"),
        "password": temp_password,
    })


@app.route("/admin/team/<int:admin_id>/force-offline", methods=["POST"])
@owner_required
def admin_team_force_offline(admin_id):
    target = db.get_admin_by_id(admin_id)
    if not target:
        flash("That user no longer exists.", "error")
        return redirect(url_for("admin_team"))
    if admin_id == session.get("admin_id"):
        flash("You can't force-offline your own account.", "error")
        return redirect(url_for("admin_team"))
    db.force_offline_admin(admin_id)
    db.audit("team_force_offline", target=target["username"],
             detail="Signed out of the agent queue (forced offline).")
    flash("%s is now offline." % target["username"], "success")
    return redirect(url_for("admin_team"))


# ─── ADMIN: AUDIT LOG ─────────────────────────────────────────────────────────
@app.route("/admin/audit")
@owner_required
def admin_audit():
    return render_template("admin_audit.html", nav="audit",
                           entries=db.list_audit_log(200))


# ─── ADMIN: CANNED REPLIES ────────────────────────────────────────────────────
CANNED_MAX_REPLIES = 20
CANNED_MAX_LEN = 300


def _parse_canned_json(raw, label):
    """Parse + validate one language's JSON list. Returns ``(list, errors)``."""
    errors = []
    if raw is None or not str(raw).strip():
        return [], errors
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return [], ["%s replies must be valid JSON." % label]
    if not isinstance(data, list):
        return [], ["%s replies must be a JSON list." % label]
    if len(data) > CANNED_MAX_REPLIES:
        errors.append("%s can have at most %d replies."
                      % (label, CANNED_MAX_REPLIES))
    cleaned = []
    seen = set()
    for index, item in enumerate(data, start=1):
        if not isinstance(item, str):
            errors.append("%s reply %d must be text." % (label, index))
            continue
        text = CONTROL_CHARS_RE.sub("", item).strip()
        if not text:
            errors.append("%s reply %d can't be empty." % (label, index))
            continue
        if len(text) > CANNED_MAX_LEN:
            errors.append("%s reply %d is longer than %d characters."
                          % (label, index, CANNED_MAX_LEN))
            continue
        key = text.casefold()
        if key in seen:
            errors.append("%s reply %d is a duplicate." % (label, index))
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned, errors


def _render_canned(values=None, errors=None):
    settings = db.get_settings()
    if values is None:
        values = {
            "en": db.get_canned_replies("en"),
            "bn": db.get_canned_replies("bn"),
        }
    return render_template(
        "admin_canned.html", nav="canned", values=values, errors=errors or [],
        max_replies=CANNED_MAX_REPLIES, max_len=CANNED_MAX_LEN,
        clinic_en=(settings.get("clinic_name")
                   or settings.get("clinic_name_bn") or "the clinic"),
        clinic_bn=(settings.get("clinic_name_bn")
                   or settings.get("clinic_name") or "ক্লিনিক"),
        preview_agent=session.get("admin_display_name")
                       or session.get("admin_username") or "Riya")


@app.route("/admin/canned", methods=["GET", "POST"])
@owner_required
def admin_canned():
    if request.method == "POST":
        raw_en = request.form.get("canned_en", "[]")
        raw_bn = request.form.get("canned_bn", "[]")
        en, en_errors = _parse_canned_json(raw_en, "English")
        bn, bn_errors = _parse_canned_json(raw_bn, "Bengali")
        errors = en_errors + bn_errors
        if errors:
            return _render_canned(values={"en": en, "bn": bn}, errors=errors)
        db.set_canned_replies("en", en)
        db.set_canned_replies("bn", bn)
        db.audit("canned_replies_saved", target="en, bn",
                 detail="Saved %d English and %d Bengali replies."
                        % (len(en), len(bn)))
        flash("Canned replies saved — the agent console uses them immediately.",
              "success")
        return redirect(url_for("admin_canned"))
    return _render_canned()


# ─── ADMIN: CATALOG (doctors, days off, services) ──────────────────────────────
# Validation limits shared by the routes and the templates.
DOCTOR_NAME_MAX      = 100
DOCTOR_SPECIALTY_MAX = 120
DOCTOR_BIO_MAX       = 600
SLOT_MINUTES_MIN     = 10
SLOT_MINUTES_MAX     = 120
SERVICE_NAME_MAX     = 100
SERVICE_DURATION_MIN = 5
SERVICE_DURATION_MAX = 480
SERVICE_PRICE_MAX    = 1_000_000
TIME_OFF_MAX_DAYS    = 90
TIME_OFF_REASON_MAX  = 200
TIME_OFF_MAX_AHEAD   = 365

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]
WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _hhmm(value):
    """Return minutes for a valid 'HH:MM', else ``None``."""
    match = TIME_RE.match(str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _is_multiple(value, step):
    return value % step == 0


def _parse_schedule_form(form, slot_minutes):
    """Parse ``weekday``/``start``/``end`` arrays into validated range rows.

    Returns ``(schedule, errors)``. ``schedule`` is a list of dicts ready for
    the database; ``errors`` explains anything rejected. Overlapping ranges on
    the same weekday and ranges shorter than one slot are refused here.
    """
    schedule = []
    errors = []
    for weekday in range(7):
        starts = form.getlist("start_%d" % weekday)
        ends = form.getlist("end_%d" % weekday)
        if len(starts) != len(ends):
            errors.append("%s: each range needs both a start and an end time."
                          % WEEKDAY_NAMES[weekday])
            continue
        ranges = []
        for index, (raw_start, raw_end) in enumerate(zip(starts, ends), start=1):
            raw_start = (raw_start or "").strip()
            raw_end = (raw_end or "").strip()
            if not raw_start and not raw_end:
                continue
            label = "%s range %d" % (WEEKDAY_NAMES[weekday], index)
            start = _hhmm(raw_start)
            end = _hhmm(raw_end)
            if start is None:
                errors.append("%s: '%s' is not a valid time (use HH:MM, "
                              "e.g. 09:30)." % (label, raw_start))
                continue
            if end is None:
                errors.append("%s: '%s' is not a valid time (use HH:MM, "
                              "e.g. 17:00)." % (label, raw_end))
                continue
            if start >= end:
                errors.append("%s: the start time must be before the end time."
                              % label)
                continue
            if (end - start) < slot_minutes:
                errors.append("%s: the range is shorter than one %d-minute "
                              "slot." % (label, slot_minutes))
                continue
            ranges.append((start, end, label))
        ranges.sort()
        for (start_a, end_a, label_a), (start_b, _end_b, label_b) in zip(
                ranges, ranges[1:]):
            if start_b < end_a:
                errors.append("%s and %s overlap on %s."
                              % (label_a, label_b, WEEKDAY_NAMES[weekday]))
        for start, end, _label in ranges:
            schedule.append({"weekday": weekday,
                             "start_time": db._from_minutes(start),
                             "end_time": db._from_minutes(end)})
    return schedule, errors


def _schedule_summary(schedule, max_ranges=3):
    """Compact 'Mon 10:00–14:00, Wed 10:00–14:00 (+1 more)' summary."""
    by_day = {}
    for row in schedule or []:
        by_day.setdefault(row["weekday"], []).append(
            "%s–%s" % (row["start_time"], row["end_time"]))
    parts = ["%s %s" % (WEEKDAY_SHORT[wd], ", ".join(by_day[wd]))
             for wd in sorted(by_day)]
    if not parts:
        return "No set hours"
    if len(parts) > max_ranges:
        return "%s (+%d more)" % (", ".join(parts[:max_ranges]),
                                  len(parts) - max_ranges)
    return ", ".join(parts)


def _slot_boundary_ok(doctor_id, doctor, date, time_str):
    """True when ``time_str`` sits on a valid slot boundary of the schedule."""
    try:
        day = datetime.date.fromisoformat(date)
    except (ValueError, TypeError):
        return False
    minutes = _hhmm(time_str)
    if minutes is None:
        return False
    slot_minutes = doctor.get("slot_minutes") or 30
    for row in doctor.get("schedule", []):
        if row["weekday"] != day.weekday():
            continue
        start = _hhmm(row["start_time"])
        end = _hhmm(row["end_time"])
        if start is None or end is None or not (start <= minutes < end):
            continue
        if _is_multiple(minutes - start, slot_minutes):
            return True
    return False


def _schedule_impact(doctor, new_schedule, new_slot_minutes, new_active):
    """Upcoming appointments broken by a proposed doctor change.

    An appointment is affected when the doctor is being disabled, or when its
    time no longer falls inside the new schedule on a slot boundary, or when
    it falls inside one of the doctor's days-off ranges. Nothing is cancelled
    or moved — the owner decides.
    """
    doctor_id = doctor["id"]
    candidate = {
        "id": doctor_id,
        "slot_minutes": new_slot_minutes,
        "active": new_active,
        "schedule": new_schedule,
    }
    affected = []
    for appt in db.list_upcoming_appointments(doctor_id=doctor_id):
        reasons = []
        if not new_active:
            reasons.append("doctor is disabled")
        if not _slot_boundary_ok(doctor_id, candidate, appt["date"], appt["time"]):
            reasons.append("outside the new schedule")
        if db.is_doctor_off(doctor_id, appt["date"]):
            reasons.append("doctor is on leave")
        if reasons:
            affected.append({
                "id": appt["id"],
                "patient_name": appt.get("patient_name"),
                "phone": appt.get("phone"),
                "date": appt.get("date"),
                "time": appt.get("time"),
                "reasons": reasons,
            })
    return affected


def _time_off_impact(doctor_id, date_from, date_to):
    """Upcoming appointments that fall inside a proposed days-off range."""
    affected = []
    for appt in db.list_upcoming_appointments(doctor_id=doctor_id):
        date = appt.get("date") or ""
        if date_from <= date <= date_to:
            affected.append({
                "id": appt["id"],
                "patient_name": appt.get("patient_name"),
                "phone": appt.get("phone"),
                "date": date,
                "time": appt.get("time"),
                "reasons": ["inside the days-off range"],
            })
    return affected


def _impact_token(payload):
    """Sign a pending change so the confirmation page can't be tampered with.

    The result is base64url so it survives being placed in an HTML attribute
    without any characters (quotes, braces, spaces) that would be escaped and
    change the value the browser submits.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(app.secret_key.encode("utf-8"), raw.encode("utf-8"),
                   hashlib.sha256).digest()
    body = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    tag = base64.urlsafe_b64encode(sig).decode("ascii")
    return body + "." + tag


def _impact_payload(token):
    """Verify and decode a token from :func:`_impact_token`; None when bad."""
    body, _sep, tag = str(token or "").partition(".")
    if not body or not tag:
        return None
    try:
        raw = base64.urlsafe_b64decode(body.encode("ascii"))
        sig = base64.urlsafe_b64decode(tag.encode("ascii"))
    except (ValueError, TypeError):
        return None
    expected = hmac.new(app.secret_key.encode("utf-8"), raw,
                        hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _schedule_view_rows(doctor=None, form=None):
    """Build the weekday-grouped rows the schedule editor renders.

    On a failed POST the typed values are kept (and flagged), otherwise the
    saved schedule is shown.
    """
    grouped = {wd: [] for wd in range(7)}
    if form is not None:
        slot = 30
        try:
            slot = int(form.get("slot_minutes") or 30)
        except (TypeError, ValueError):
            slot = 30
        for weekday in range(7):
            starts = form.getlist("start_%d" % weekday)
            ends = form.getlist("end_%d" % weekday)
            for start, end in zip(starts, ends):
                grouped[weekday].append({"start_time": (start or "").strip(),
                                         "end_time": (end or "").strip(),
                                         "bad": _hhmm(start) is None
                                                or _hhmm(end) is None
                                                or (_hhmm(start) or 0) >= (_hhmm(end) or 0)})
    else:
        for row in (doctor or {}).get("schedule", []):
            grouped[row["weekday"]].append({"start_time": row["start_time"],
                                            "end_time": row["end_time"],
                                            "bad": False})
    return grouped


def _doctor_form_values(doctor=None):
    doctor = doctor or {}
    return {
        "name_en": doctor.get("name_en") or "",
        "name_bn": doctor.get("name_bn") or "",
        "specialty_en": doctor.get("specialty_en") or "",
        "specialty_bn": doctor.get("specialty_bn") or "",
        "bio_en": doctor.get("bio_en") or "",
        "bio_bn": doctor.get("bio_bn") or "",
        "slot_minutes": doctor.get("slot_minutes") or 30,
        "active": 1 if doctor.get("active", 1) else 0,
    }


def _validate_doctor_fields(form):
    """Validate the doctor's plain fields. Returns ``(values, errors)``."""
    values = {
        "name_en": (form.get("name_en") or "").strip(),
        "name_bn": (form.get("name_bn") or "").strip(),
        "specialty_en": (form.get("specialty_en") or "").strip(),
        "specialty_bn": (form.get("specialty_bn") or "").strip(),
        "bio_en": (form.get("bio_en") or "").strip(),
        "bio_bn": (form.get("bio_bn") or "").strip(),
        "active": 1 if form.get("active") else 0,
    }
    errors = []
    if not values["name_en"] and not values["name_bn"]:
        errors.append("A doctor needs a name in English or Bangla.")
    if len(values["name_en"]) > DOCTOR_NAME_MAX:
        errors.append("The English name must be %d characters or fewer."
                      % DOCTOR_NAME_MAX)
    if len(values["name_bn"]) > DOCTOR_NAME_MAX:
        errors.append("The Bangla name must be %d characters or fewer."
                      % DOCTOR_NAME_MAX)
    if len(values["specialty_en"]) > DOCTOR_SPECIALTY_MAX:
        errors.append("The English specialty must be %d characters or fewer."
                      % DOCTOR_SPECIALTY_MAX)
    if len(values["specialty_bn"]) > DOCTOR_SPECIALTY_MAX:
        errors.append("The Bangla specialty must be %d characters or fewer."
                      % DOCTOR_SPECIALTY_MAX)
    if len(values["bio_en"]) > DOCTOR_BIO_MAX:
        errors.append("The English bio must be %d characters or fewer."
                      % DOCTOR_BIO_MAX)
    if len(values["bio_bn"]) > DOCTOR_BIO_MAX:
        errors.append("The Bangla bio must be %d characters or fewer."
                      % DOCTOR_BIO_MAX)

    raw_slot = (form.get("slot_minutes") or "").strip()
    try:
        slot_minutes = int(raw_slot)
    except (TypeError, ValueError):
        slot_minutes = None
        errors.append("Slot length must be a whole number of minutes.")
    if slot_minutes is not None:
        if not (SLOT_MINUTES_MIN <= slot_minutes <= SLOT_MINUTES_MAX):
            errors.append("Slot length must be between %d and %d minutes."
                          % (SLOT_MINUTES_MIN, SLOT_MINUTES_MAX))
        elif not _is_multiple(slot_minutes, 5):
            errors.append("Slot length must be a multiple of 5 minutes.")
        else:
            values["slot_minutes"] = slot_minutes
    if "slot_minutes" not in values:
        values["slot_minutes"] = slot_minutes if slot_minutes else 30
    return values, errors


def _validate_time_off_form(form, doctor_id):
    """Validate a days-off range. Returns ``(payload, errors)``."""
    errors = []
    date_from = (form.get("date_from") or "").strip()
    date_to = (form.get("date_to") or "").strip()
    reason = (form.get("reason") or "").strip()
    start = end = None
    try:
        start = datetime.date.fromisoformat(date_from)
    except (ValueError, TypeError):
        errors.append("Please choose a valid start date.")
    try:
        end = datetime.date.fromisoformat(date_to)
    except (ValueError, TypeError):
        errors.append("Please choose a valid end date.")
    today = db.dhaka_today()
    if start and end:
        if end < start:
            errors.append("The end date must be on or after the start date.")
        elif start < today:
            errors.append("Days off can't start in the past.")
        elif (end - start).days + 1 > TIME_OFF_MAX_DAYS:
            errors.append("A days-off range can be at most %d days long."
                          % TIME_OFF_MAX_DAYS)
        elif (end - today).days > TIME_OFF_MAX_AHEAD:
            errors.append("Please add days off no more than %d days ahead."
                          % TIME_OFF_MAX_AHEAD)
    if len(reason) > TIME_OFF_REASON_MAX:
        errors.append("The reason must be %d characters or fewer."
                      % TIME_OFF_REASON_MAX)
    payload = None
    if not errors:
        payload = {"doctor_id": doctor_id, "date_from": date_from,
                   "date_to": date_to, "reason": reason or None}
    return payload, errors


def _validate_service_form(form):
    """Validate a service. Returns ``(values, errors)``."""
    values = {
        "name_en": (form.get("name_en") or "").strip(),
        "name_bn": (form.get("name_bn") or "").strip(),
        "active": 1 if form.get("active") else 0,
    }
    errors = []
    if not values["name_en"] and not values["name_bn"]:
        errors.append("A service needs a name in English or Bangla.")
    if len(values["name_en"]) > SERVICE_NAME_MAX:
        errors.append("The English name must be %d characters or fewer."
                      % SERVICE_NAME_MAX)
    if len(values["name_bn"]) > SERVICE_NAME_MAX:
        errors.append("The Bangla name must be %d characters or fewer."
                      % SERVICE_NAME_MAX)

    price, raw_price = _parse_price(form.get("price"))
    if raw_price and price is None:
        errors.append("The price must be a number of taka (e.g. 500).")
    elif price is not None and price < 0:
        errors.append("The price can't be negative.")
    elif price is not None and price > SERVICE_PRICE_MAX:
        errors.append("That price looks unrealistic — please check it.")
    values["price"] = price

    price_max, raw_max = _parse_price(form.get("price_max"))
    if raw_max and price_max is None:
        errors.append("The maximum price must be a number of taka.")
    elif price_max is not None:
        if price_max < 0:
            errors.append("The maximum price can't be negative.")
        elif price_max > SERVICE_PRICE_MAX:
            errors.append("That price looks unrealistic — please check it.")
        elif price is not None and price_max < price:
            errors.append("The maximum price must be greater than or equal to "
                          "the minimum price.")
    values["price_max"] = price_max

    raw_duration = (form.get("duration_minutes") or "").strip()
    try:
        duration = int(raw_duration)
    except (TypeError, ValueError):
        duration = None
        errors.append("Duration must be a whole number of minutes.")
    if duration is not None:
        if not (SERVICE_DURATION_MIN <= duration <= SERVICE_DURATION_MAX):
            errors.append("Duration must be between %d and %d minutes."
                          % (SERVICE_DURATION_MIN, SERVICE_DURATION_MAX))
        elif not _is_multiple(duration, 5):
            errors.append("Duration must be a multiple of 5 minutes.")
        else:
            values["duration_minutes"] = duration
    if "duration_minutes" not in values:
        values["duration_minutes"] = duration or 30
    return values, errors


def _parse_price(raw):
    """Return ``(value_or_None, raw_string)`` — empty input means ``None``."""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    try:
        return float(raw), raw
    except (TypeError, ValueError):
        return None, raw


def _doctor_list_row(doctor):
    """Extra display data for the doctors list."""
    schedule = doctor.get("schedule") or []
    today = db.dhaka_today().isoformat()
    return {
        "doctor": doctor,
        "summary": _schedule_summary(schedule),
        "upcoming": db.count_appointments_for_doctor(
            doctor["id"], upcoming_only=True, from_date=today),
    }


def _service_list_row(service):
    return {
        "service": service,
        "in_use": db.count_appointments_for_service(service["id"]),
        "total": db.count_all_appointments_for_service(service["id"]),
    }


@app.route("/admin/doctors")
@owner_required
def admin_doctors():
    rows = [_doctor_list_row(doc) for doc in db.list_doctors()]
    time_off_by_doctor = {}
    for item in db.list_time_off_days(days=365):
        time_off_by_doctor.setdefault(item["doctor_id"], []).append(item)
    return render_template("admin_doctors.html", nav="doctors", rows=rows,
                           weekday_names=WEEKDAY_NAMES,
                           time_off_by_doctor=time_off_by_doctor)


@app.route("/admin/doctors/new", methods=["GET", "POST"])
@owner_required
def admin_doctor_new():
    values = _doctor_form_values()
    errors = []
    if request.method == "POST":
        values, errors = _validate_doctor_fields(request.form)
        schedule, sched_errors = _parse_schedule_form(
            request.form, values["slot_minutes"])
        errors.extend(sched_errors)
        if not errors and not schedule and values["active"]:
            errors.append("An active doctor needs at least one weekly range — "
                          "otherwise no slots can be offered.")
        if not errors:
            doctor_id = db.create_doctor(
                name_en=values["name_en"], name_bn=values["name_bn"],
                specialty_en=values["specialty_en"],
                specialty_bn=values["specialty_bn"],
                bio_en=values["bio_en"], bio_bn=values["bio_bn"],
                slot_minutes=values["slot_minutes"], active=values["active"],
                schedule=schedule)
            db.audit("doctor_created", target="#%d" % doctor_id,
                     detail="Created %s; %d weekly range(s)."
                            % (values["name_en"] or values["name_bn"],
                               len(schedule)))
            flash("%s was added." % (values["name_en"] or values["name_bn"]),
                  "success")
            return redirect(url_for("admin_doctor_edit", doctor_id=doctor_id))
    return render_template("admin_doctor_edit.html", nav="doctors",
                           mode="new", doctor=None, values=values,
                           schedule=_schedule_view_rows(
                               form=request.form if request.method == "POST"
                               else None),
                           errors=errors,
                           weekday_names=WEEKDAY_NAMES,
                           time_off=[],
                           slot_min=SLOT_MINUTES_MIN,
                           slot_max=SLOT_MINUTES_MAX)


@app.route("/admin/doctors/<int:doctor_id>", methods=["GET", "POST"])
@owner_required
def admin_doctor_edit(doctor_id):
    doctor = db.get_doctor(doctor_id)
    if doctor is None:
        flash("That doctor no longer exists.", "error")
        return redirect(url_for("admin_doctors"))
    errors = []

    if request.method == "POST":
        if request.form.get("action") == "add_time_off":
            return _admin_add_time_off(doctor)

        confirmed = request.form.get("impact_token") == "confirmed"
        values, errors = _validate_doctor_fields(request.form)
        schedule, sched_errors = _parse_schedule_form(
            request.form, values["slot_minutes"])
        errors.extend(sched_errors)
        if not errors:
            # Impact first: removing hours may strand bookings, and the owner
            # must see those appointments before anything changes. The
            # "needs a weekly range" rule is applied once they confirm.
            affected = _schedule_impact(doctor, schedule,
                                        values["slot_minutes"],
                                        values["active"])
            if affected and not confirmed:
                # Nothing is written yet: show exactly what would break.
                payload = {"kind": "doctor", "doctor_id": doctor_id,
                           "values": values, "schedule": schedule}
                return render_template(
                    "admin_confirm_impact.html", nav="doctors",
                    impact="doctor", doctor=doctor, affected=affected,
                    back_url=url_for("admin_doctor_edit",
                                     doctor_id=doctor_id),
                    token=_impact_token(payload),
                    summary="Saving this would affect %d upcoming "
                            "appointment%s." % (len(affected),
                                                "s" if len(affected) != 1 else ""))
            if not schedule and values["active"]:
                errors.append("An active doctor needs at least one weekly range "
                              "— otherwise no slots can be offered.")
        if not errors:
            db.save_doctor_with_schedule(doctor_id, schedule=schedule, **values)
            db.audit("doctor_edited", target="#%d" % doctor_id,
                     detail="Updated %s; %d weekly range(s), slot %s min, %s."
                            % (values["name_en"] or values["name_bn"],
                               len(schedule), values["slot_minutes"],
                               "active" if values["active"] else "disabled"))
            flash("Doctor updated.", "success")
            return redirect(url_for("admin_doctor_edit", doctor_id=doctor_id))

        # A failed save: keep exactly what was typed.
        return render_template("admin_doctor_edit.html", nav="doctors",
                               mode="edit", doctor=doctor, values=values,
                               schedule=_schedule_view_rows(form=request.form),
                               errors=errors,
                               weekday_names=WEEKDAY_NAMES,
                               time_off=db.list_doctor_time_off(
                                   doctor_id, upcoming_only=True),
                               slot_min=SLOT_MINUTES_MIN,
                               slot_max=SLOT_MINUTES_MAX)

    return render_template("admin_doctor_edit.html", nav="doctors",
                           mode="edit", doctor=doctor,
                           values=_doctor_form_values(doctor),
                           schedule=_schedule_view_rows(doctor=doctor),
                           errors=errors,
                           weekday_names=WEEKDAY_NAMES,
                           time_off=db.list_doctor_time_off(
                               doctor_id, upcoming_only=True),
                           slot_min=SLOT_MINUTES_MIN,
                           slot_max=SLOT_MINUTES_MAX)


def _admin_add_time_off(doctor):
    """Handle the add-days-off form (with the same impact confirmation)."""
    doctor_id = doctor["id"]
    payload, errors = _validate_time_off_form(request.form, doctor_id)
    if errors:
        return render_template(
            "admin_doctor_edit.html", nav="doctors", mode="edit",
            doctor=doctor, values=_doctor_form_values(doctor),
            schedule=_schedule_view_rows(doctor=doctor),
            errors=errors, weekday_names=WEEKDAY_NAMES,
            time_off=db.list_doctor_time_off(doctor_id, upcoming_only=True),
            slot_min=SLOT_MINUTES_MIN, slot_max=SLOT_MINUTES_MAX)
    affected = _time_off_impact(doctor_id, payload["date_from"],
                                payload["date_to"])
    if affected and request.form.get("impact_token") != "confirmed":
        return render_template(
            "admin_confirm_impact.html", nav="doctors", impact="time_off",
            doctor=doctor, affected=affected,
            back_url=url_for("admin_doctor_edit", doctor_id=doctor_id),
            token=_impact_token({"kind": "time_off", **payload}),
            summary="%d upcoming appointment%s fall%s inside this days-off "
                    "range." % (len(affected),
                                "s" if len(affected) != 1 else "",
                                "" if len(affected) != 1 else "s"))
    db.create_time_off(doctor_id, payload["date_from"], payload["date_to"],
                       payload["reason"])
    db.audit("time_off_added", target="#%d" % doctor_id,
             detail="Days off %s to %s."
                    % (payload["date_from"], payload["date_to"]))
    flash("Days off added — the doctor is now unavailable on those dates.",
          "success")
    return redirect(url_for("admin_doctor_edit", doctor_id=doctor_id))


@app.route("/admin/doctors/<int:doctor_id>/time-off/<int:time_off_id>/delete",
           methods=["POST"])
@owner_required
def admin_time_off_delete(doctor_id, time_off_id):
    row = db.get_time_off(time_off_id)
    if row is None or row["doctor_id"] != doctor_id:
        flash("That days-off range no longer exists.", "error")
    elif db.delete_time_off(time_off_id):
        db.audit("time_off_removed", target="#%d" % doctor_id,
                 detail="Removed days off %s to %s."
                        % (row.get("date_from"), row.get("date_to")))
        flash("Days off removed.", "success")
    else:
        flash("That days-off range no longer exists.", "error")
    return redirect(url_for("admin_doctor_edit", doctor_id=doctor_id))


@app.route("/admin/doctors/<int:doctor_id>/toggle", methods=["POST"])
@owner_required
def admin_doctor_toggle(doctor_id):
    doctor = db.get_doctor(doctor_id)
    if doctor is None:
        flash("That doctor no longer exists.", "error")
        return redirect(url_for("admin_doctors"))
    new_active = 0 if doctor.get("active") else 1
    if not new_active:
        affected = _schedule_impact(doctor, doctor.get("schedule") or [],
                                    doctor.get("slot_minutes"),
                                    new_active)
        if affected and request.form.get("impact_token") != "confirmed":
            payload = {"kind": "disable_doctor", "doctor_id": doctor_id,
                       "affected": affected}
            return render_template(
                "admin_confirm_impact.html", nav="doctors",
                impact="disable_doctor", doctor=doctor, affected=affected,
                back_url=url_for("admin_doctors"),
                token=_impact_token(payload),
                summary="Disabling %s would affect %d upcoming appointment%s."
                        % (doctor.get("name_en") or doctor.get("name_bn"),
                           len(affected), "s" if len(affected) != 1 else ""))
    db.update_doctor(doctor_id, active=new_active)
    db.audit("doctor_enabled" if new_active else "doctor_disabled",
             target="#%d" % doctor_id,
             detail="%s %s."
                    % (doctor.get("name_en") or doctor.get("name_bn"),
                       "enabled" if new_active else "disabled"))
    flash("%s is now %s." % (doctor.get("name_en") or doctor.get("name_bn"),
                             "enabled" if new_active else "disabled"),
          "success")
    return redirect(url_for("admin_doctors"))


@app.route("/admin/doctors/<int:doctor_id>/delete", methods=["POST"])
@owner_required
def admin_doctor_delete(doctor_id):
    doctor = db.get_doctor(doctor_id)
    if doctor is None:
        flash("That doctor no longer exists.", "error")
        return redirect(url_for("admin_doctors"))
    total = db.count_all_appointments_for_doctor(doctor_id)
    if total:
        flash("%s has %d appointment%s on record, so it can't be deleted. "
              "Disable the doctor instead to hide them from the chatbot."
              % (doctor.get("name_en") or doctor.get("name_bn"), total,
                 "s" if total != 1 else ""), "error")
        return redirect(url_for("admin_doctors"))
    db.delete_doctor(doctor_id)
    db.audit("doctor_deleted", target="#%d" % doctor_id,
             detail="Deleted %s." % (doctor.get("name_en")
                                     or doctor.get("name_bn")))
    flash("Doctor deleted.", "success")
    return redirect(url_for("admin_doctors"))


@app.route("/admin/services")
@owner_required
def admin_services():
    rows = [_service_list_row(svc) for svc in db.list_services()]
    return render_template("admin_services.html", nav="services", rows=rows)


@app.route("/admin/services/new", methods=["GET", "POST"])
@owner_required
def admin_service_new():
    values = {"name_en": "", "name_bn": "", "price": "", "price_max": "",
              "duration_minutes": 30, "active": 1}
    errors = []
    if request.method == "POST":
        values, errors = _validate_service_form(request.form)
        if not errors:
            service_id = db.create_service(**values)
            db.audit("service_created", target="#%d" % service_id,
                     detail="Created %s."
                            % (values["name_en"] or values["name_bn"]))
            flash("%s was added." % (values["name_en"] or values["name_bn"]),
                  "success")
            return redirect(url_for("admin_services"))
    return render_template("admin_service_edit.html", nav="services",
                           mode="new", service=None, values=values,
                           errors=errors,
                           duration_min=SERVICE_DURATION_MIN,
                           duration_max=SERVICE_DURATION_MAX)


@app.route("/admin/services/<int:service_id>", methods=["GET", "POST"])
@owner_required
def admin_service_edit(service_id):
    service = db.get_service(service_id)
    if service is None:
        flash("That service no longer exists.", "error")
        return redirect(url_for("admin_services"))
    values = {"name_en": service.get("name_en") or "",
              "name_bn": service.get("name_bn") or "",
              "price": "" if service.get("price") is None else service["price"],
              "price_max": ("" if service.get("price_max") is None
                            else service["price_max"]),
              "duration_minutes": service.get("duration_minutes") or 30,
              "active": 1 if service.get("active", 1) else 0}
    errors = []
    if request.method == "POST":
        values, errors = _validate_service_form(request.form)
        if not errors:
            db.update_service(service_id, **values)
            db.audit("service_edited", target="#%d" % service_id,
                     detail="Updated %s (%s)."
                            % (values["name_en"] or values["name_bn"],
                               "active" if values["active"] else "disabled"))
            flash("Service updated.", "success")
            return redirect(url_for("admin_services"))
    return render_template("admin_service_edit.html", nav="services",
                           mode="edit", service=service, values=values,
                           errors=errors,
                           duration_min=SERVICE_DURATION_MIN,
                           duration_max=SERVICE_DURATION_MAX)


@app.route("/admin/services/<int:service_id>/toggle", methods=["POST"])
@owner_required
def admin_service_toggle(service_id):
    service = db.get_service(service_id)
    if service is None:
        flash("That service no longer exists.", "error")
        return redirect(url_for("admin_services"))
    new_active = 0 if service.get("active") else 1
    db.update_service(service_id, active=new_active)
    db.audit("service_enabled" if new_active else "service_disabled",
             target="#%d" % service_id,
             detail="%s %s."
                    % (service.get("name_en") or service.get("name_bn"),
                       "enabled" if new_active else "disabled"))
    flash("%s is now %s." % (service.get("name_en") or service.get("name_bn"),
                             "enabled" if new_active else "disabled"),
          "success")
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/delete", methods=["POST"])
@owner_required
def admin_service_delete(service_id):
    service = db.get_service(service_id)
    if service is None:
        flash("That service no longer exists.", "error")
        return redirect(url_for("admin_services"))
    total = db.count_all_appointments_for_service(service_id)
    if total:
        flash("%s is used by %d appointment%s, so it can't be deleted. "
              "Disable the service instead to hide it from the chatbot."
              % (service.get("name_en") or service.get("name_bn"), total,
                 "s" if total != 1 else ""), "error")
        return redirect(url_for("admin_services"))
    db.delete_service(service_id)
    db.audit("service_deleted", target="#%d" % service_id,
             detail="Deleted %s." % (service.get("name_en")
                                     or service.get("name_bn")))
    flash("Service deleted.", "success")
    return redirect(url_for("admin_services"))


@app.route("/admin/confirm-impact", methods=["POST"])
@owner_required
def admin_confirm_impact():
    """Apply a change the owner has explicitly confirmed on the warning page."""
    payload = _impact_payload(request.form.get("impact_token"))
    if not payload:
        flash("That confirmation expired. Please make the change again.",
              "error")
        return redirect(url_for("admin_doctors"))
    kind = payload.get("kind")
    if kind == "doctor":
        doctor_id = payload.get("doctor_id")
        if db.get_doctor(doctor_id):
            db.save_doctor_with_schedule(doctor_id,
                                         schedule=payload.get("schedule"),
                                         **payload.get("values", {}))
            values = payload.get("values", {})
            db.audit("doctor_edited", target="#%d" % doctor_id,
                     detail="Confirmed impact: updated %s; %d weekly range(s)."
                            % (values.get("name_en") or values.get("name_bn"),
                               len(payload.get("schedule") or [])))
            flash("Doctor updated. The listed appointments were left "
                  "untouched.", "success")
        return redirect(url_for("admin_doctor_edit", doctor_id=doctor_id))
    if kind == "time_off":
        doctor_id = payload.get("doctor_id")
        if db.get_doctor(doctor_id):
            db.create_time_off(doctor_id, payload["date_from"],
                               payload["date_to"], payload.get("reason"))
            db.audit("time_off_added", target="#%d" % doctor_id,
                     detail="Confirmed impact: days off %s to %s."
                            % (payload["date_from"], payload["date_to"]))
            flash("Days off added. The listed appointments were left "
                  "untouched.", "success")
        return redirect(url_for("admin_doctor_edit", doctor_id=doctor_id))
    if kind == "disable_doctor":
        doctor_id = payload.get("doctor_id")
        if db.get_doctor(doctor_id):
            db.update_doctor(doctor_id, active=0)
            db.audit("doctor_disabled", target="#%d" % doctor_id,
                     detail="Confirmed impact: doctor disabled.")
            flash("Doctor disabled. The listed appointments were left "
                  "untouched.", "success")
        return redirect(url_for("admin_doctors"))
    if kind == "deactivate_admin":
        admin_id = payload.get("admin_id")
        target = db.get_admin_by_id(admin_id)
        if not target:
            flash("That user no longer exists.", "error")
            return redirect(url_for("admin_team"))
        if admin_id == session.get("admin_id"):
            flash("You can't deactivate your own account.", "error")
            return redirect(url_for("admin_team"))
        if (target.get("role") == "owner"
                and db.count_active_owners(exclude_id=admin_id) == 0):
            flash("The last active owner can't be deactivated.", "error")
            return redirect(url_for("admin_team"))
        handed_back = db.deactivate_admin(admin_id)
        db.audit("team_deactivated", target=target["username"],
                 detail="Account deactivated; %d active chat(s) handed back "
                        "to the bot." % len(handed_back))
        flash("%s was deactivated and their active chats went back to the bot."
              % target["username"], "success")
        return redirect(url_for("admin_team"))
    flash("Nothing was changed.", "error")
    return redirect(url_for("admin_doctors"))


# ─── ROUTES — AGENT (login + JSON API) ────────────────────────────────────────
def _agent_session_admin():
    """Return the signed-in agent when its session is still valid, else None.

    Clears the session when the account was deleted, deactivated or its
    ``session_version`` was bumped (password reset / forced sign-out).
    """
    agent_id = session.get("agent_id")
    if not agent_id:
        return None
    admin = db.get_admin_by_id(agent_id)
    if (not admin or not int(admin.get("active", 1) or 0)
            or not _session_version_matches(admin, "agent_session_version")):
        session.clear()
        return None
    return admin


def agent_login_required(f):
    """Require a valid, active agent session for JSON agent endpoints."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        admin = _agent_session_admin()
        if admin is None:
            return jsonify({"error": "unauthorized"}), 401
        if admin.get("must_change_password"):
            return jsonify({"error": "must_change_password"}), 403
        return f(*args, **kwargs)
    return decorated


def agent_post_required(f):
    """Require a valid agent session AND a CSRF token for state changes."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        admin = _agent_session_admin()
        if admin is None:
            return jsonify({"error": "unauthorized"}), 401
        if admin.get("must_change_password"):
            return jsonify({"error": "must_change_password"}), 403
        if not _csrf_ok():
            return jsonify({"error": "csrf"}), 403
        return f(*args, **kwargs)
    return decorated


def _agent_id():
    return session.get("agent_id")


def _agent_role():
    return session.get("agent_role")


def _agent_can_view(conv):
    """Agents may read waiting conversations or ones assigned to them."""
    if conv is None:
        return False
    if _agent_role() == "owner":
        return True
    if conv["status"] == "waiting":
        return True
    return conv["status"] == "with_agent" and conv["agent_id"] == _agent_id()


def _agent_can_act(conv):
    """Agents may act on conversations assigned to them (owners: any)."""
    if conv is None:
        return False
    if _agent_role() == "owner":
        return True
    return conv["status"] == "with_agent" and conv["agent_id"] == _agent_id()


def _start_agent_session(admin):
    """Start a clean agent session, storing the account's session version."""
    session.clear()
    session["agent_id"] = admin["id"]
    session["agent_role"] = admin["role"]
    session["agent_username"] = admin["username"]
    session["agent_display_name"] = admin.get("display_name") or admin["username"]
    session["agent_session_version"] = int(admin.get("session_version") or 0)
    _csrf_token()
    # Owners can also use the admin area.
    if admin["role"] == "owner":
        session["admin_logged_in"] = True
        session["admin_id"] = admin["id"]
        session["admin_role"] = "owner"
        session["admin_username"] = admin["username"]
        session["admin_display_name"] = (admin.get("display_name")
                                         or admin["username"])
        session["admin_session_version"] = int(admin.get("session_version") or 0)
        session["last_active"] = time.time()
        session.permanent = True


@app.route("/agent/login", methods=["GET", "POST"])
def agent_login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        admin = db.verify_admin(username, password) if username else None
        if admin and admin.get("role") in ("agent", "owner"):
            _start_agent_session(admin)
            return redirect(url_for("agent_home"))
        # Inactive accounts fail exactly like a wrong password.
        ADMIN_LOGGER.warning("Agent login failed: username=%r ip=%s",
                             username, _client_ip())
        db.audit("login_failed", target=username,
                 detail="Agent login failed (bad credentials or inactive account).")
        error = "Invalid username or password."
    return render_template("agent_login.html", error=error, logged_in=False,
                           display_name=None, csrf_token=_csrf_token())


@app.route("/agent/logout")
def agent_logout():
    session.clear()
    return redirect(url_for("agent_login"))


@app.route("/agent/password", methods=["GET", "POST"])
def agent_password():
    """Let an agent change their password — also clears a forced change."""
    admin = _agent_session_admin()
    if admin is None:
        return redirect(url_for("agent_login"))

    errors = []
    if request.method == "POST":
        if not _csrf_ok():
            return ("Missing or invalid security token (CSRF). "
                    "Reload the page and try again.", 403)
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not db.verify_admin(admin["username"], current):
            errors.append("Your current password is incorrect.")
        if new != confirm:
            errors.append("The two new passwords don't match.")
        errors.extend(_password_policy_errors(admin["username"], new, current))
        if not errors:
            db.change_own_password(admin["id"], new)
            db.audit("password_changed", target=admin["username"],
                     detail="Agent changed their own password.")
            return redirect(url_for("agent_home"))

    return render_template("agent_password.html", errors=errors,
                           must_change=bool(admin.get("must_change_password")),
                           username=admin["username"],
                           display_name=admin.get("display_name")
                           or admin["username"],
                           csrf_token=_csrf_token())


@app.route("/agent")
def agent_home():
    admin = _agent_session_admin()
    if admin is None:
        return redirect(url_for("agent_login"))
    if admin.get("must_change_password"):
        return redirect(url_for("agent_password"))
    return render_template(
        "agent_console.html",
        display_name=admin.get("display_name") or admin["username"],
        agent_username=admin["username"],
        agent_id=admin["id"],
        agent_role=admin["role"],
        is_online=bool(admin.get("is_online")),
        persona_name=db.get_setting("persona_name", "Mita"),
        persona_name_bn=db.get_setting("persona_name_bn", "Mita"),
        csrf_token=_csrf_token(),
    )


@app.route("/agent/api/heartbeat", methods=["POST"])
@agent_post_required
def agent_heartbeat():
    data = request.get_json(silent=True) or {}
    is_online = bool(data.get("is_online", True))
    admin = db.heartbeat(_agent_id(), is_online)
    return jsonify({"ok": True, "is_online": admin["is_online"] if admin else None})


@app.route("/agent/api/queue")
@agent_login_required
def agent_queue():
    db.run_housekeeping()
    return jsonify({
        "waiting": db.list_waiting(),
        "mine": db.list_my_conversations(_agent_id()),
    })


@app.route("/agent/api/conversation/<int:cid>")
@agent_login_required
def agent_conversation(cid):
    after_id = request.args.get("after_id", type=int)
    data = db.get_conversation_with_messages(cid, after_id=after_id)
    if not data:
        return jsonify({"error": "not_found"}), 404
    if not _agent_can_view(data["conversation"]):
        return jsonify({"error": "forbidden"}), 403
    # Attach anything the customer left behind: bookings + callback requests.
    data["appointments"] = db.list_appointments_by_session(
        data["conversation"].get("session_id"))
    data["callbacks"] = db.list_callback_requests_for_conversation(cid)
    return jsonify(data)


@app.route("/agent/api/conversation/<int:cid>/accept", methods=["POST"])
@agent_post_required
def agent_accept(cid):
    conv = db.get_conversation(cid)
    if not conv:
        return jsonify({"error": "not_found"}), 404
    if _agent_role() != "owner":
        if conv["status"] == "with_agent":
            return jsonify({"error": "already_taken"}), 409
        if conv["status"] != "waiting":
            return jsonify({"error": "forbidden"}), 403
    if not db.accept_conversation(cid, _agent_id()):
        return jsonify({"error": "already_taken"}), 409
    return jsonify({"ok": True, "conversation": db.get_conversation(cid)})


@app.route("/agent/api/conversation/<int:cid>/message", methods=["POST"])
@agent_post_required
def agent_message(cid):
    data = request.get_json(silent=True) or {}
    content = (data.get("message") or "").strip()
    if not content:
        return jsonify({"error": "empty"}), 400
    if len(content) > MAX_MESSAGE_LEN:
        return jsonify({"error": "too_long"}), 400
    if _rate_check(_agent_buckets, _agent_id(), AGENT_RATE_LIMIT, 60):
        return jsonify({"error": "rate_limited"}), 429
    conv = db.get_conversation(cid)
    if not conv:
        return jsonify({"error": "not_found"}), 404
    if not _agent_can_act(conv):
        return jsonify({"error": "forbidden"}), 403
    mid = db.agent_send_message(cid, _agent_id(), content)
    if not mid:
        return jsonify({"error": "not_assigned"}), 409
    return jsonify({"ok": True, "message_id": mid})


@app.route("/agent/api/conversation/<int:cid>/handback", methods=["POST"])
@agent_post_required
def agent_handback(cid):
    conv = db.get_conversation(cid)
    if not conv:
        return jsonify({"error": "not_found"}), 404
    if not _agent_can_act(conv):
        return jsonify({"error": "forbidden"}), 403
    ok = db.hand_back_to_bot(cid, _agent_id())
    return jsonify({"ok": ok})


@app.route("/agent/api/conversation/<int:cid>/close", methods=["POST"])
@agent_post_required
def agent_close(cid):
    conv = db.get_conversation(cid)
    if not conv:
        return jsonify({"error": "not_found"}), 404
    if not _agent_can_act(conv):
        return jsonify({"error": "forbidden"}), 403
    ok = db.close_conversation(cid, by="agent")
    return jsonify({"ok": ok})


# ─── ROUTES — AGENT (callbacks + canned replies) ──────────────────────────────
@app.route("/agent/api/callbacks")
@agent_login_required
def agent_callbacks():
    """List callback requests (newest first) for the console's Callbacks tab."""
    return jsonify({"callbacks": db.list_callback_requests()})


@app.route("/agent/api/callbacks/<int:cb_id>/status", methods=["POST"])
@agent_post_required
def agent_callback_status(cb_id):
    """Move a callback request through new -> contacted -> done."""
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in db.CALLBACK_STATUSES:
        return jsonify({"error": "bad_status"}), 400
    if not db.set_callback_status(cb_id, status):
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True, "callback": db.get_callback_request(cb_id)})


@app.route("/agent/api/canned")
@agent_login_required
def agent_canned():
    """Return canned (quick) replies with placeholders substituted, per language."""
    admin = db.get_admin_by_id(_agent_id()) or {}
    agent_name = admin.get("display_name") or admin.get("username") or "Agent"
    clinic_en = (db.get_setting("clinic_name")
                 or db.get_setting("clinic_name_bn") or "the clinic")
    clinic_bn = (db.get_setting("clinic_name_bn")
                 or db.get_setting("clinic_name") or "ক্লিনিক")
    return jsonify({
        "en": [_substitute_canned(t, agent_name, clinic_en)
               for t in db.get_canned_replies("en")],
        "bn": [_substitute_canned(t, agent_name, clinic_bn)
               for t in db.get_canned_replies("bn")],
    })


def _substitute_canned(text, agent_name, clinic_name):
    """Replace only the supported canned-reply placeholders.

    Unknown braces (``{something_else}``) are deliberately left untouched.
    """
    text = str(text or "")
    return (text.replace("{agent_name}", str(agent_name or ""))
                .replace("{clinic_name}", str(clinic_name or "")))



if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes", "on")
    clinic_name = (db.get_settings().get("clinic_name")
                   or db.get_settings().get("clinic_name_bn") or "AI Receptionist")
    print(f"\n🦷 {clinic_name} — starting...")
    print("🌐 Website : http://localhost:5000")
    print("🔐 Admin   : http://localhost:5000/admin\n")
    app.run(debug=debug, port=5000)
