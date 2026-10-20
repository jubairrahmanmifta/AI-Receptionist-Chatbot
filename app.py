import os
import re
import json
import time
import hmac
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


# Failed admin logins: 5 per IP *or* per username in 10 minutes blocks that key
# for 5 more minutes. In-memory on purpose — no background thread, no new deps.
LOGIN_FAIL_LIMIT   = 5
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


def _validate_booking(payload, lang):
    """Validate a parsed booking payload.

    Returns ``(booking_dict_or_None, error_message_or_None)``.
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
    free = db.get_available_slots(doctor_id, date)
    if slot not in free:
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
    keys = ["ip:%s" % (ip or "unknown")]
    if username:
        keys.append("user:%s" % str(username).lower())
    return keys


def _login_blocked(ip, username):
    """True while this IP or username is inside a lockout window."""
    now = time.time()
    with _login_lock:
        for key in _login_keys(ip, username):
            until = _login_blocks.get(key)
            if until is None:
                continue
            if now < until:
                return True
            del _login_blocks[key]
    return False


def _login_failure(ip, username):
    """Record a failed attempt; block the key once the limit is reached."""
    now = time.time()
    with _login_lock:
        for key in _login_keys(ip, username):
            recent = [t for t in _login_failures.get(key, [])
                      if now - t < LOGIN_FAIL_WINDOW]
            recent.append(now)
            _login_failures[key] = recent
            if len(recent) >= LOGIN_FAIL_LIMIT:
                _login_blocks[key] = now + LOGIN_BLOCK_SECONDS
                _login_failures.pop(key, None)


def _login_success(ip, username):
    """Clear counters for a key once it authenticates successfully."""
    with _login_lock:
        for key in _login_keys(ip, username):
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
    session["last_active"] = time.time()
    session.permanent = True
    _csrf_token()   # a brand-new token after clearing the session


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
    if not admin or admin.get("role") != "owner":
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
    return {"admin_name": name or ""}


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
    appts = db.list_appointments()
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return render_template("admin.html", appointments=appts, now_date=today,
                           nav="appointments")


@app.route("/admin/delete/<int:appt_id>", methods=["POST"])
@owner_post_required
def admin_delete(appt_id):
    if db.delete_appointment(appt_id):
        flash("Appointment deleted.", "success")
    else:
        flash("That appointment no longer exists.", "error")
    return redirect(url_for("admin_panel"))


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

        for key, _label, _max in ADMIN_TEXT_FIELDS:
            if key in request.form:
                db.set_setting(key, request.form.get(key, "").strip())
        for key, _label, _max in ADMIN_TEXTAREA_FIELDS:
            if key in request.form:
                db.set_setting(key, request.form.get(key, ""))
        for key, _label, _lo, _hi, _unit in ADMIN_INT_FIELDS:
            if key in request.form:
                db.set_setting(key, typed.get(key, "").strip())
        if new_key:
            db.set_setting("openrouter_api_key", new_key)
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


# ─── ROUTES — AGENT (login + JSON API) ────────────────────────────────────────
def agent_login_required(f):
    """Require a logged-in agent (or owner) for JSON agent endpoints."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("agent_id"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def agent_post_required(f):
    """Require a logged-in agent AND a valid CSRF token for state changes."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("agent_id"):
            return jsonify({"error": "unauthorized"}), 401
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


@app.route("/agent/login", methods=["GET", "POST"])
def agent_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        admin = db.verify_admin(username, password)
        if admin and admin.get("role") in ("agent", "owner"):
            session["agent_id"] = admin["id"]
            session["agent_role"] = admin["role"]
            session["agent_username"] = admin["username"]
            session["agent_display_name"] = admin.get("display_name") or admin["username"]
            _csrf_token()
            # Owners can also use the admin area.
            if admin["role"] == "owner":
                session["admin_logged_in"] = True
                session["admin_id"] = admin["id"]
                session["admin_role"] = "owner"
                session["admin_username"] = admin["username"]
            return redirect(url_for("agent_home"))
        error = "Invalid username or password."
    return render_template("agent_login.html", error=error, logged_in=False,
                           display_name=None, csrf_token=_csrf_token())


@app.route("/agent/logout")
def agent_logout():
    session.clear()
    return redirect(url_for("agent_login"))


@app.route("/agent")
def agent_home():
    if not session.get("agent_id"):
        return redirect(url_for("agent_login"))
    admin = db.get_admin_by_id(session.get("agent_id")) or {}
    return render_template(
        "agent_console.html",
        display_name=session.get("agent_display_name"),
        agent_username=session.get("agent_username"),
        agent_id=session.get("agent_id"),
        agent_role=session.get("agent_role"),
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
    """Return the canned (quick) replies for the composer, per language."""
    return jsonify({
        "en": db.get_canned_replies("en"),
        "bn": db.get_canned_replies("bn"),
    })


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes", "on")
    clinic_name = (db.get_settings().get("clinic_name")
                   or db.get_settings().get("clinic_name_bn") or "AI Receptionist")
    print(f"\n🦷 {clinic_name} — starting...")
    print("🌐 Website : http://localhost:5000")
    print("🔐 Admin   : http://localhost:5000/admin\n")
    app.run(debug=debug, port=5000)
