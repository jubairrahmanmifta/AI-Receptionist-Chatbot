import os
import json
import datetime
import functools
import secrets
from flask import (
    Flask, render_template, request,
    jsonify, session, redirect, url_for
)
import requests

import db

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

# ─── CLINIC SYSTEM PROMPTS ─────────────────────────────────────────────────────
SYSTEM_PROMPT_BN = """
তুমি মিতা — স্মাইল ডেন্টাল কেয়ারের রিসেপশনিস্ট। তুমি একজন উষ্ণ, যত্নশীল মানুষের মতো কথা বলো — রোবটের মতো না।

তোমার ব্যক্তিত্ব:
- সহজ, প্রাকৃতিক বাংলায় কথা বলো যেভাবে বাস্তবে মানুষ বলে
- মাঝে মাঝে "আচ্ছা", "হ্যাঁ অবশ্যই", "বুঝতে পেরেছি", "ঠিক আছে" এই ধরনের স্বাভাবিক expression ব্যবহার করো
- রোগী কষ্টে থাকলে সহানুভূতি দেখাও — যেমন "আহ, দাঁতের ব্যথা সত্যিই খুব কষ্টের!"
- কখনো bullet point বা numbered list ব্যবহার করবে না
- এক সাথে অনেক প্রশ্ন করবে না — একটা করে জিজ্ঞেস করো
- উত্তর ছোট রাখো, স্বাভাবিক কথোপকথনের মতো

ক্লিনিকের তথ্য:
- নাম: স্মাইল ডেন্টাল কেয়ার | ঠিকানা: ধানমন্ডি ২৭, ঢাকা
- সময়: সকাল ৯টা — রাত ৮টা (রবিবার বন্ধ)

ডাক্তার:
- ডা. রাহেলা খানম (দাঁতের বিশেষজ্ঞ): সোম, বুধ, শুক্র — সকাল ১০টা–দুপুর ২টা
- ডা. করিম সাহেব (শিশু দন্ত বিশেষজ্ঞ): মঙ্গল, বৃহস্পতি — বিকাল ৪টা–রাত ৮টা

সেবা ও খরচ:
চেকআপ ৩০০–৫০০৳ | স্কেলিং ৮০০–১২০০৳ | ফিলিং ৫০০–১৫০০৳
রুট ক্যানেল ৩০০০–৬০০০৳ | দাঁত তোলা ৫০০–১০০০৳ | ডেনচার ৫০০০–১৫০০০৳

অ্যাপয়েন্টমেন্ট নেওয়ার ধাপ:
আগে রোগীর সমস্যাটা মন দিয়ে শোনো। তারপর স্বাভাবিকভাবে জিজ্ঞেস করো — নাম, তারপর ফোন নম্বর, তারপর কবে আসতে চায়। তারিখ না বললে নিজে suggest করো।

সব তথ্য পেলে এভাবে confirm করো:
"তাহলে আমি আপনার অ্যাপয়েন্টমেন্টটা নিশ্চিত করে দিচ্ছি!"
তারপর উত্তরের একদম শেষে এই JSON রাখো — এটা রোগী দেখতে পাবে না:
SAVE_APPOINTMENT:{"name":"নাম","phone":"ফোন","problem":"সমস্যা","date":"YYYY-MM-DD","time":"HH:MM"}
"""

SYSTEM_PROMPT_EN = """
You are Mita — a receptionist at Smile Dental Care. Talk like a warm, caring human being — not a robot or a formal assistant.

Your personality:
- Speak naturally and conversationally, like a real person would
- Use natural expressions like "Of course!", "Got it!", "Sure thing!", "Oh no, that sounds painful!"
- Show empathy when patients are in pain — e.g. "Oh that must be really uncomfortable, let's get you sorted!"
- Never use bullet points, numbered lists, or formal structured responses
- Ask one question at a time — don't bombard them
- Keep replies short and natural

Clinic Info:
- Name: Smile Dental Care | Address: Dhanmondi 27, Dhaka
- Hours: 9 AM — 8 PM (Closed Sundays)

Doctors:
- Dr. Rahela Khanam (Dental Specialist): Mon, Wed, Fri — 10 AM to 2 PM
- Dr. Karim (Pediatric Dentist): Tue, Thu — 4 PM to 8 PM

Services:
Checkup 300–500 BDT | Scaling 800–1200 BDT | Filling 500–1500 BDT
Root Canal 3000–6000 BDT | Extraction 500–1000 BDT | Denture 5000–15000 BDT

How to take appointments:
First listen carefully to what's bothering them. Then naturally ask for their name, then phone number, then preferred date. If they don't mention a date, suggest one yourself based on the doctor schedule.

Once you have everything, say something like:
"Perfect, let me get that booked for you right away!"
Then at the very end of your message include this JSON — the patient won't see it:
SAVE_APPOINTMENT:{"name":"name","phone":"phone","problem":"problem","date":"YYYY-MM-DD","time":"HH:MM"}
"""

# ─── CONVERSATION STORE ────────────────────────────────────────────────────────
conversations = {}  # session_id -> list of messages

# ─── DATABASE INIT ─────────────────────────────────────────────────────────────
# Creates clinic.db (if needed), seeds clinic data from the prompts above,
# seeds an admin account, and migrates any legacy appointments.json.
# Set SKIP_DB_INIT=1 to skip this (used for tooling/preview).
if os.environ.get("SKIP_DB_INIT") != "1":
    _cfg = load_config()
    db.init_db(
        SYSTEM_PROMPT_BN,
        SYSTEM_PROMPT_EN,
        ai_model=_cfg.get("openrouter_model") or "openrouter/auto",
        admin_username=_cfg.get("admin_username"),
        admin_password=_cfg.get("admin_password"),
    )

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def _public_appt(appt):
    """Shape an appointment dict like the old JSON records (for /chat output)."""
    return {
        "name":    appt.get("patient_name"),
        "phone":   appt.get("phone"),
        "problem": appt.get("notes"),
        "date":    appt.get("date"),
        "time":    appt.get("time"),
        "saved_at": appt.get("created_at"),
    }

# ─── AI CHAT ──────────────────────────────────────────────────────────────────
def chat_with_ai(session_id, user_message, lang="bn"):
    cfg   = load_config()
    api_key = cfg.get("openrouter_api_key", "")
    model   = cfg.get("openrouter_model", "google/gemma-4-31b-it:free")

    if not api_key or "বসান" in api_key or api_key.startswith("YOUR_"):
        return ("OPENROUTER_API_KEY env var অথবা config.json ফাইলে API key বসান।" if lang == "bn"
                else "Please set OPENROUTER_API_KEY or your API key in config.json.")

    if session_id not in conversations:
        conversations[session_id] = []
    conversations[session_id].append({"role": "user", "content": user_message})

    system = SYSTEM_PROMPT_BN if lang == "bn" else SYSTEM_PROMPT_EN
    today  = datetime.datetime.now().strftime("%Y-%m-%d (%A)")
    system += f"\n\nআজকের তারিখ / Today: {today}"

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:5000",
                "X-Title": "Smile Dental Care",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    *conversations[session_id],
                ],
                "max_tokens": 400,
                "temperature": 0.7,
            },
            timeout=30,
        )
        print(f"[OpenRouter] status={resp.status_code}")
        print(f"[OpenRouter] response={resp.text[:500]}")
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"]
        conversations[session_id].append({"role": "assistant", "content": reply})
        return reply

    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        if code == 401:
            return ("API key ভুল। config.json চেক করুন।" if lang == "bn"
                    else "Invalid API key. Please check config.json.")
        if code == 404:
            return ("Model পাওয়া যাচ্ছে না। config.json এ model name চেক করুন।" if lang == "bn"
                    else f"Model not found ({model}). Check model name in config.json.")
        if code == 429:
            return ("অনেক বেশি request হয়েছে। একটু পরে আবার চেষ্টা করুন।" if lang == "bn"
                    else "Too many requests. Please wait a moment and try again.")
        return (f"সার্ভার error ({code})। আবার চেষ্টা করুন।" if lang == "bn"
                else f"Server error ({code}). Please try again.")

    except requests.exceptions.ConnectionError:
        return ("ইন্টারনেট সংযোগ নেই।" if lang == "bn"
                else "No internet connection.")

    except requests.exceptions.Timeout:
        return ("সময় শেষ হয়ে গেছে। আবার চেষ্টা করুন।" if lang == "bn"
                else "Request timed out. Please try again.")

    except Exception as e:
        return (f"সমস্যা হয়েছে: {str(e)}" if lang == "bn"
                else f"Something went wrong: {str(e)}")

# ─── ROUTES — PUBLIC ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    sid  = data.get("session_id", "default")
    msg  = data.get("message", "").strip()
    lang = data.get("lang", "bn")

    if not msg:
        return jsonify({"error": "empty"}), 400

    reply = chat_with_ai(sid, msg, lang)

    # Parse and save appointment if present
    saved = False
    appt  = None
    if "SAVE_APPOINTMENT:" in reply:
        try:
            raw  = reply.split("SAVE_APPOINTMENT:")[1].strip().splitlines()[0]
            data = json.loads(raw)
            appt_id = db.create_appointment(
                patient_name=data.get("name"),
                phone=data.get("phone"),
                date=data.get("date"),
                time=data.get("time"),
                notes=data.get("problem"),
                language=lang,
            )
            appt  = _public_appt(db.get_appointment(appt_id))
            saved = True
        except Exception as e:
            print(f"[Appointment save error] {e}")

    clean = reply.split("SAVE_APPOINTMENT:")[0].strip()
    return jsonify({"reply": clean, "appointment_saved": saved, "appointment_data": appt})

@app.route("/reset", methods=["POST"])
def reset():
    sid = request.get_json().get("session_id", "default")
    conversations.pop(sid, None)
    return jsonify({"ok": True})

# ─── ROUTES — ADMIN ───────────────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if db.verify_admin(username, password):
            session["admin_logged_in"] = True
            session["admin_username"] = username
            return redirect(url_for("admin_panel"))
        error = "Invalid username or password."
    return render_template("admin_login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin")
@login_required
def admin_panel():
    appts = db.list_appointments()
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return render_template("admin.html", appointments=appts, now_date=today)

@app.route("/admin/delete/<int:appt_id>", methods=["POST"])
@login_required
def admin_delete(appt_id):
    db.delete_appointment(appt_id)
    return redirect(url_for("admin_panel"))

# ─── CONFIG API (for live status check on settings page) ──────────────────────
@app.route("/admin/config", methods=["GET", "POST"])
@login_required
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

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes", "on")
    print("\n🦷 Smile Dental Care — starting...")
    print("🌐 Website : http://localhost:5000")
    print("🔐 Admin   : http://localhost:5000/admin\n")
    app.run(debug=debug, port=5000)
