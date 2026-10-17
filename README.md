# 🦷 Smile Dental Care — AI Receptionist v4

## Setup

### 1. Configure credentials (choose ONE)

**Option A — `.env` / environment variables (recommended):**
```bash
cp config.example.json config.json   # optional local fallback
export OPENROUTER_API_KEY="sk-or-v1-your-key-here"
export OPENROUTER_MODEL="openrouter/auto"
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="change-me"
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
# optional: export FLASK_DEBUG=1
```
If `python-dotenv` is installed (it is in `requirements.txt`), you can put the
same lines in a local `.env` file and they are loaded automatically on startup.

**Option B — local `config.json` (gitignored, never commit real keys):**
```bash
cp config.example.json config.json
```
```json
{
  "openrouter_api_key": "sk-or-v1-your-key-here",
  "openrouter_model": "openrouter/auto",
  "admin_username": "admin",
  "admin_password": "change-me"
}
```

Environment variables take priority over `config.json`. The `/admin/config`
page still reads/writes the local `config.json`.

> ⚠️ `config.json`, `appointments.json`, `venv/`, `__pycache__/`, `*.pyc`, and
> `.env` are gitignored. Never commit real secrets.

### 2. Install
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Run
```bash
python app.py
```

## URLs
- Website  : http://localhost:5000
- Admin    : http://localhost:5000/admin  (username/password from your config or env)
- Settings : http://localhost:5000/admin/config

## File structure
```
dental_v4/
├── app.py              ← Main server
├── config.example.json ← Template for config.json (safe to commit)
├── config.json         ← Your real config (gitignored)
├── requirements.txt    ← flask + requests + python-dotenv
├── appointments.json   ← Auto-created when first appointment saved
└── templates/
    ├── index.html          ← Main dental website + chat popup
    ├── admin_login.html    ← Admin login
    ├── admin.html          ← Appointments table
    └── admin_config.html   ← Settings page
```
