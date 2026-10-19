import os
import re
import json
import time
import hmac
import datetime
import functools
import secrets
import threading
from flask import (
    Flask, render_template, request,
    jsonify, session, redirect, url_for
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

# ─── FILES ─────────────────────────────────────────────────────────────────────
# Only secrets/API config stay in a JSON file (gitignored). Everything dynamic
# — settings, doctors, services, appointments, admins — lives in SQLite (db.py).
CONFIG_FILE = "config.json"

# ─── LOAD CONFIG ───────────────────────────────────────────────────────────────
# Environment variables take priority; config.json is the fallback (and is
# still used by the /admin/config page for local, gitignored settings).
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

def save_config(cfg):
    """Write the local, gitignored config.json (used by /admin/config)."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

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
    """Verify the X-CSRF-Token header against the session token."""
    token = session.get("csrf_token")
    sent = request.headers.get("X-CSRF-Token", "")
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
def _openrouter_chat(messages, lang="bn"):
    """Send ``messages`` to OpenRouter and return the reply text.

    The model comes from the ``ai_model`` setting (fallback ``openrouter/auto``)
    and the API key from ``OPENROUTER_API_KEY`` first, then ``config.json``.
    Errors are returned as friendly bilingual strings.
    """
    cfg     = load_config()
    api_key = os.environ.get("OPENROUTER_API_KEY") or cfg.get("openrouter_api_key", "")
    settings = db.get_settings()
    model   = (settings.get("ai_model") or "").strip() or "openrouter/auto"
    title   = (settings.get("clinic_name") or settings.get("clinic_name_bn")
               or "AI Receptionist")

    if not api_key or api_key.startswith("YOUR_") or "বসান" in api_key:
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
def owner_required(f):
    """Allow only a logged-in owner; agent accounts get a hard 403."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if session.get("admin_logged_in"):
            if session.get("admin_role") != "owner":
                return ("Forbidden — agent accounts cannot open /admin.", 403)
            return f(*args, **kwargs)
        # Logged in as an agent (but not an owner) — refuse the admin area.
        if session.get("agent_id"):
            return ("Forbidden — agent accounts cannot open /admin.", 403)
        return redirect(url_for("admin_login"))
    return decorated


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        admin = db.verify_admin(username, password)
        if admin and admin.get("role") == "owner":
            session["admin_logged_in"] = True
            session["admin_id"] = admin["id"]
            session["admin_role"] = "owner"
            session["admin_username"] = admin["username"]
            _csrf_token()
            return redirect(url_for("admin_panel"))
        if admin and admin.get("role") == "agent":
            error = "This is an agent account — please use the agent login."
        else:
            error = "Invalid username or password."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@owner_required
def admin_panel():
    appts = db.list_appointments()
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return render_template("admin.html", appointments=appts, now_date=today)


@app.route("/admin/delete/<int:appt_id>", methods=["POST"])
@owner_required
def admin_delete(appt_id):
    db.delete_appointment(appt_id)
    return redirect(url_for("admin_panel"))


# ─── CONFIG API (for live status check on settings page) ──────────────────────
@app.route("/admin/config", methods=["GET", "POST"])
@owner_required
def admin_config():
    msg = None
    if request.method == "POST":
        cfg = load_config()
        cfg["openrouter_api_key"] = request.form.get("api_key", "").strip()
        cfg["openrouter_model"]   = request.form.get("model", "").strip()
        save_config(cfg)
        db.set_setting("ai_model", cfg["openrouter_model"])

        # Admin credentials live in SQLite as a hash.
        current = db.list_admins()
        if current:
            admin_id = current[0]["id"]
            new_username = request.form.get("admin_username", "").strip() or None
            new_pass     = request.form.get("admin_password", "").strip() or None
            db.update_admin(admin_id, username=new_username, password=new_pass)
        msg = "Settings saved!"
    cfg = load_config()
    admins = db.list_admins()
    cfg["admin_username"] = admins[0]["username"] if admins else ""
    return render_template("admin_config.html", cfg=cfg, msg=msg)


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
