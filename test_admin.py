"""Admin panel foundation tests (Prompt 4A) — unittest on a throwaway database.

Covers the security hardening: CSRF on every admin POST, agent accounts being
locked out of /admin, login throttling, the forced first-login password change,
open-redirect protection, session cookie flags, API-key non-exposure, settings
that reach the prompt without a restart, and the removal of /admin/config.

No network access: the OpenRouter call inside "Test AI connection" is mocked.

    python test_admin.py -v
"""

import os
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import urlparse

# Skip app's startup DB init and point the db layer at a temp database before
# anything in the app is imported. The real clinic.db is never touched.
os.environ["SKIP_DB_INIT"] = "1"
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ.pop("COOKIE_SECURE", None)
os.environ.pop("ADMIN_PASSWORD", None)

import db  # noqa: E402

_TMPDIR = tempfile.mkdtemp(prefix="clinic_admin_")
db.DB_FILE = os.path.join(_TMPDIR, "admin.db")
db.init_db()

import app as app_mod  # noqa: E402
import prompt_builder  # noqa: E402

OWNER = ("owner_admin", "OwnerPass99!")
AGENT = ("agent_admin", "AgentPass99!")
FAKE_KEY = "sk-or-TESTKEY1234"   # recognisable, must never be echoed back


# ─── helpers ──────────────────────────────────────────────────────────────────
def new_client():
    return app_mod.app.test_client()


def csrf_of(client):
    with client.session_transaction() as sess:
        return sess.get("csrf_token", "")


def admin_login(client, username, password, next_url=None,
                environ_overrides=None):
    """Open the login form (which mints the CSRF token), then POST credentials.

    ``environ_overrides`` lets a test pretend to come from another IP address.
    """
    env = environ_overrides or {}
    client.get("/admin/login", environ_overrides=env)
    data = {"username": username, "password": password,
            "csrf_token": csrf_of(client)}
    if next_url is not None:
        data["next"] = next_url
    return client.post("/admin/login", data=data, environ_overrides=env)


def location_parts(response):
    """Split a Location header into ``(netloc, path)`` (it may be absolute)."""
    parsed = urlparse(response.headers.get("Location", ""))
    return parsed.netloc, parsed.path


def ensure_account(username, password, role):
    row = db.get_admin(username)
    if row:
        return row["id"]
    return db.create_admin(username, password, display_name=username.title(),
                           role=role)


def book_one():
    """A throwaway appointment; returns its id (or None if seeding failed)."""
    doctors = db.list_doctors()
    doctor_id = doctors[0]["id"] if doctors else 1
    for hour in ("10:30", "15:45", "17:15"):
        appt_id = db.book_appointment("CSRF Test", "01712345678", doctor_id,
                                      None, "2031-06-10", hour, notes="test row")
        if appt_id:
            return appt_id
    return None


class FakeResponse(object):
    """Stand-in for ``requests.Response`` so no network is used."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"choices": [{"message": {"content": "PONG"}}]}

    def json(self):
        return self._payload


class AdminPanelTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.owner_id = ensure_account(OWNER[0], OWNER[1], "owner")
        cls.agent_id = ensure_account(AGENT[0], AGENT[1], "agent")

    def setUp(self):
        # The throttle is process-global, so every test starts from a clean slate.
        app_mod._login_failures.clear()
        app_mod._login_blocks.clear()
        os.environ.pop("COOKIE_SECURE", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("ADMIN_PASSWORD", None)
        db.set_must_change_password(self.owner_id, False)
        self.client = new_client()

    # ── 1. CSRF ──────────────────────────────────────────────────────────────
    def test_every_admin_post_requires_csrf(self):
        original_clinic = db.get_setting("clinic_name")
        db.set_setting("clinic_name", "Original Clinic Name")
        self.addCleanup(db.set_setting, "clinic_name", original_clinic)
        appt_id = book_one()
        if appt_id:
            self.addCleanup(db.delete_appointment, appt_id)

        admin_login(self.client, *OWNER)
        self.assertTrue(csrf_of(self.client), "login should mint a CSRF token")

        targets = [
            ("/admin/settings", {"csrf_token": ""}),
            ("/admin/settings", {"csrf_token": "not-the-right-token"}),
            ("/admin/settings/test_ai", {}),
            ("/admin/account", {"action": "profile", "display_name": "X",
                                "username": OWNER[0]}),
            ("/admin/logout", {}),
        ]
        if appt_id:
            targets.append(("/admin/delete/%d" % appt_id, {}))

        for path, data in targets:
            with self.subTest(path=path, data=data):
                response = self.client.post(path, data=data)
                self.assertEqual(response.status_code, 403)
                body = response.get_data(as_text=True).lower()
                self.assertIn("security token", body)

        # Rejections had no side effects.
        self.assertEqual(db.get_setting("clinic_name"), "Original Clinic Name")
        if appt_id:
            # The appointment must still exist — the delete never went through.
            self.assertIn(appt_id, [a["id"] for a in db.list_appointments()])
        self.assertTrue(db.get_admin(OWNER[0]))

    # ── 2. agent role ────────────────────────────────────────────────────────
    def test_agent_role_forbidden_on_every_admin_route(self):
        agent_client = new_client()
        response = agent_client.post("/agent/login",
                                     data={"username": AGENT[0],
                                           "password": AGENT[1]})
        self.assertIn(response.status_code, (200, 302))

        for path in ("/admin", "/admin/settings", "/admin/account"):
            with self.subTest(path=path, method="GET"):
                response = agent_client.get(path)
                self.assertEqual(response.status_code, 403)
                self.assertIn("agent accounts cannot",
                              response.get_data(as_text=True))

        for path in ("/admin/logout", "/admin/settings",
                     "/admin/settings/test_ai", "/admin/account",
                     "/admin/delete/1"):
            with self.subTest(path=path, method="POST"):
                response = agent_client.post(path, data={"csrf_token": "x"})
                self.assertEqual(response.status_code, 403)
                self.assertIn("agent accounts cannot",
                              response.get_data(as_text=True))

    # ── 3. login throttle ────────────────────────────────────────────────────
    def test_login_throttle_blocks_after_five_failures(self):
        client = new_client()
        with self.assertLogs("clinic.admin", level="WARNING") as captured:
            for attempt in range(5):
                response = admin_login(client, OWNER[0],
                                       "wrong-password-%d" % attempt)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Invalid username or password",
                              response.get_data(as_text=True))

        logged = "\n".join(captured.output)
        self.assertIn("Admin login failed", logged)
        self.assertNotIn("wrong-password", logged)   # passwords are never logged

        # Sixth attempt: refused up front, even with the correct password.
        response = admin_login(client, OWNER[0], OWNER[1])
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Too many failed attempts", body)
        self.assertNotIn("Invalid username", body)   # generic message only
        self.assertNotIn(OWNER[1], body)

        # The block is per (IP, username) pair, so the same address is refused.
        response = admin_login(new_client(), OWNER[0], OWNER[1])
        self.assertIn("Too many failed attempts", response.get_data(as_text=True))

        # Once the 5-minute block elapses the pair is released again.
        now = time.time()
        for key in list(app_mod._login_blocks):
            app_mod._login_blocks[key] = now - 1
        response = admin_login(new_client(), OWNER[0], OWNER[1])
        self.assertIn(response.status_code, (301, 302))

    def test_throttle_pair_does_not_lock_username_from_other_ip(self):
        """A username attacked from one address still works from another."""
        attacker = new_client()
        for attempt in range(5):
            admin_login(attacker, OWNER[0], "bad-%d" % attempt,
                        environ_overrides={"REMOTE_ADDR": "10.0.0.9"})

        # The attacking IP is now blocked for that username...
        blocked = admin_login(new_client(), OWNER[0], OWNER[1],
                              environ_overrides={"REMOTE_ADDR": "10.0.0.9"})
        self.assertIn("Too many failed attempts",
                      blocked.get_data(as_text=True))

        # ...but the same username from a different IP is untouched.
        allowed = admin_login(new_client(), OWNER[0], OWNER[1],
                              environ_overrides={"REMOTE_ADDR": "10.0.0.10"})
        self.assertIn(allowed.status_code, (301, 302))

    def test_throttle_per_ip_cap_blocks_many_usernames(self):
        """A single address spraying many usernames trips the looser per-IP cap."""
        attacker = new_client()
        env = {"REMOTE_ADDR": "10.0.0.20"}
        for index in range(app_mod.LOGIN_IP_FAIL_LIMIT):
            admin_login(attacker, "spray_%d" % index, "bad",
                        environ_overrides=env)
        blocked = admin_login(attacker, "spray_final", "bad",
                              environ_overrides=env)
        self.assertIn("Too many failed attempts",
                      blocked.get_data(as_text=True))

    # ── 4. forced password change ────────────────────────────────────────────
    def test_must_change_password_redirects_and_is_cleared(self):
        username, password = "owner_gate", "GatePass99!"
        gate_id = ensure_account(username, password, "owner")
        db.set_must_change_password(gate_id, True)
        self.addCleanup(db.set_must_change_password, gate_id, False)
        self.addCleanup(db.update_admin, gate_id, password=password)

        client = new_client()
        response = admin_login(client, username, password)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/account", location_parts(response)[1])

        # Every other admin page funnels to Account with a clear message.
        for path in ("/admin", "/admin/settings"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/account", location_parts(response)[1])

        page = client.get("/admin/account")
        self.assertEqual(page.status_code, 200)
        self.assertIn("change your password",
                      page.get_data(as_text=True).lower())
        self.assertEqual(db.get_admin(username)["must_change_password"], 1)

        # Logging out is still allowed while the flag is pending.
        response = client.post("/admin/logout",
                               data={"csrf_token": csrf_of(client)})
        self.assertEqual(response.status_code, 302)

        # A wrong current password does NOT clear the flag.
        client = new_client()
        admin_login(client, username, password)
        response = client.post("/admin/account", data={
            "action": "password", "csrf_token": csrf_of(client),
            "current_password": "not-my-password",
            "new_password": "BrandNew99!", "confirm_password": "BrandNew99!"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.get_admin(username)["must_change_password"], 1)

        # A valid change clears it and unlocks the panel.
        response = client.post("/admin/account", data={
            "action": "password", "csrf_token": csrf_of(client),
            "current_password": password,
            "new_password": "BrandNew99!", "confirm_password": "BrandNew99!"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin(username)["must_change_password"], 0)
        self.assertEqual(client.get("/admin").status_code, 200)
        self.assertEqual(client.get("/admin/settings").status_code, 200)

    # ── 5. open redirect ─────────────────────────────────────────────────────
    def test_open_redirect_on_next_is_ignored(self):
        for evil in ("https://evil.com/steal", "http://evil.com",
                     "//evil.com", "/\\evil.com",
                     "/\\/evil.com", "javascript:alert(1)"):
            with self.subTest(next=evil):
                client = new_client()
                response = admin_login(client, *OWNER, next_url=evil)
                self.assertEqual(response.status_code, 302)
                netloc, path = location_parts(response)
                self.assertIn(netloc, ("", "localhost"), "left the site: %r" % netloc)
                self.assertTrue(path.startswith("/admin"), path)

        # A same-site path is still honoured.
        client = new_client()
        response = admin_login(client, *OWNER, next_url="/admin/settings")
        netloc, path = location_parts(response)
        self.assertIn(netloc, ("", "localhost"))
        self.assertEqual(path.rstrip("/"), "/admin/settings")

    # ── 6. session cookie flags ──────────────────────────────────────────────
    def test_session_cookie_flags(self):
        response = self.client.get("/admin/login")
        cookie = response.headers.get("Set-Cookie", "")
        self.assertTrue(cookie, "login page should set a session cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)

        os.environ["COOKIE_SECURE"] = "1"
        secure_cookie = new_client().get("/admin/login").headers.get(
            "Set-Cookie", "")
        self.assertIn("Secure", secure_cookie)
        self.assertIn("HttpOnly", secure_cookie)

    # ── 7. API key never rendered ────────────────────────────────────────────
    def test_api_key_value_never_appears_in_any_response(self):
        # (a) key coming from the environment
        os.environ["OPENROUTER_API_KEY"] = FAKE_KEY
        self.addCleanup(os.environ.pop, "OPENROUTER_API_KEY", None)
        admin_login(self.client, *OWNER)

        body = self.client.get("/admin/settings").get_data(as_text=True)
        self.assertNotIn(FAKE_KEY, body)
        self.assertIn("••••1234", body)                    # masked tail shown
        self.assertIn("Environment variable OPENROUTER_API_KEY", body)

        # The prompt preview on that same page can't leak it either.
        for lang in ("en", "bn"):
            self.assertNotIn(FAKE_KEY, prompt_builder.build_system_prompt(lang))

        # The connection test reports the outcome without the key.
        for status in (200, 401, 429, 500):
            with self.subTest(status=status):
                with mock.patch.object(app_mod.requests, "post",
                                       return_value=FakeResponse(status)):
                    response = self.client.post(
                        "/admin/settings/test_ai",
                        data={"csrf_token": csrf_of(self.client)})
                body = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(FAKE_KEY, body)
                self.assertIn('"ok"', body)
        os.environ.pop("OPENROUTER_API_KEY", None)

        # (b) key saved through the panel
        db.set_setting("openrouter_api_key", FAKE_KEY)
        self.addCleanup(db.set_setting, "openrouter_api_key", "")
        body = self.client.get("/admin/settings").get_data(as_text=True)
        self.assertNotIn(FAKE_KEY, body)
        self.assertIn("••••1234", body)
        self.assertIn("Admin panel", body)

        # (c) whatever legacy key config.json holds stays hidden as well
        legacy = app_mod.load_config().get("openrouter_api_key") or ""
        if app_mod._usable_key(legacy):
            self.assertNotIn(legacy, body)
            self.assertNotIn(legacy, prompt_builder.build_system_prompt("en"))

    # ── 8. settings reach the prompt immediately ─────────────────────────────
    def test_settings_change_is_visible_in_the_built_prompt(self):
        original_en = db.get_setting("persona_name")
        original_bn = db.get_setting("persona_name_bn")
        self.addCleanup(db.set_setting, "persona_name", original_en)
        self.addCleanup(db.set_setting, "persona_name_bn", original_bn)

        admin_login(self.client, *OWNER)
        response = self.client.post("/admin/settings", data={
            "csrf_token": csrf_of(self.client),
            "persona_name": "Zaraqa",
            "persona_name_bn": "জারা",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_setting("persona_name"), "Zaraqa")

        # No restart, no re-import: the builder reads the new value at once.
        self.assertIn("Zaraqa", prompt_builder.build_system_prompt("en"))
        self.assertIn("জারা", prompt_builder.build_system_prompt("bn"))

        # And the preview tab on the page shows the very same text.
        body = self.client.get("/admin/settings").get_data(as_text=True)
        preview = body.split('id="prompt-en"', 1)[-1].split("</pre>", 1)[0]
        self.assertIn("Zaraqa", preview)

    # ── 9. /admin/config is gone ─────────────────────────────────────────────
    def test_old_admin_config_route_and_template_removed(self):
        admin_login(self.client, *OWNER)
        self.assertEqual(self.client.get("/admin/config").status_code, 404)
        self.assertEqual(
            self.client.post("/admin/config", data={"csrf_token": csrf_of(self.client)}).status_code,
            404)
        self.assertFalse(
            os.path.exists(os.path.join("templates", "admin_config.html")),
            "admin_config.html should be deleted")
        body = self.client.get("/admin").get_data(as_text=True)
        self.assertNotIn("/admin/config", body)

    # ── extra coverage for the new pages ─────────────────────────────────────
    def test_settings_numeric_validation(self):
        original = db.get_setting("handoff_timeout_minutes")
        self.addCleanup(db.set_setting, "handoff_timeout_minutes", original)
        admin_login(self.client, *OWNER)
        token = csrf_of(self.client)

        response = self.client.post("/admin/settings", data={
            "csrf_token": token, "handoff_timeout_minutes": "999"})
        self.assertEqual(response.status_code, 200)          # re-rendered
        self.assertIn("between 1 and 60", response.get_data(as_text=True))
        self.assertEqual(db.get_setting("handoff_timeout_minutes"), original)

        response = self.client.post("/admin/settings", data={
            "csrf_token": token, "handoff_timeout_minutes": "soon"})
        self.assertIn("whole number", response.get_data(as_text=True))
        self.assertEqual(db.get_setting("handoff_timeout_minutes"), original)

        response = self.client.post("/admin/settings", data={
            "csrf_token": token, "handoff_timeout_minutes": "7"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_setting("handoff_timeout_minutes"), "7")

    def test_account_password_policy(self):
        original_display = db.get_admin(OWNER[0])["display_name"]
        self.addCleanup(db.update_admin, self.owner_id,
                        display_name=original_display)
        admin_login(self.client, *OWNER)
        token = csrf_of(self.client)

        cases = [
            ({"new_password": "short", "confirm_password": "short"}, "at least 8"),
            ({"new_password": OWNER[0], "confirm_password": OWNER[0]}, "username"),
            ({"new_password": OWNER[1], "confirm_password": OWNER[1]},
             "different from the current"),
            ({"new_password": "AlphaBeta1", "confirm_password": "AlphaBeta2"},
             "The two new passwords"),
        ]
        for payload, needle in cases:
            with self.subTest(needle=needle):
                data = {"action": "password", "csrf_token": token,
                        "current_password": OWNER[1]}
                data.update(payload)
                response = self.client.post("/admin/account", data=data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(needle, response.get_data(as_text=True))

        # The configured default password is refused.
        os.environ["ADMIN_PASSWORD"] = "dental123"
        response = self.client.post("/admin/account", data={
            "action": "password", "csrf_token": token,
            "current_password": OWNER[1],
            "new_password": "dental123", "confirm_password": "dental123"})
        self.assertIn("default password", response.get_data(as_text=True))

        # Wrong current password, nothing changed.
        response = self.client.post("/admin/account", data={
            "action": "password", "csrf_token": token,
            "current_password": "nope-nope",
            "new_password": "AlphaBeta9", "confirm_password": "AlphaBeta9"})
        self.assertIn("current password is incorrect",
                      response.get_data(as_text=True))
        self.assertTrue(db.verify_admin(OWNER[0], OWNER[1]))

        # A valid profile update keeps the session in sync.
        response = self.client.post("/admin/account", data={
            "action": "profile", "csrf_token": token,
            "display_name": "Head Honcho", "username": OWNER[0]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin(OWNER[0])["display_name"], "Head Honcho")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("admin_display_name"), "Head Honcho")

    def test_logout_and_delete_are_post_only(self):
        admin_login(self.client, *OWNER)
        self.assertEqual(self.client.get("/admin/logout").status_code, 405)
        self.assertEqual(self.client.get("/admin/delete/1").status_code, 405)

    def test_session_is_regenerated_on_login(self):
        self.client.get("/admin/login")
        with self.client.session_transaction() as sess:
            before = sess.get("csrf_token")
            sess["planted"] = "pre-auth-value"     # simulated fixation marker
        admin_login(self.client, *OWNER)
        with self.client.session_transaction() as sess:
            self.assertNotIn("planted", sess)
            after = sess.get("csrf_token")
        self.assertTrue(after)
        self.assertNotEqual(before, after)

    def test_owner_can_open_every_admin_page(self):
        admin_login(self.client, *OWNER)
        for path in ("/admin", "/admin/settings", "/admin/account"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                self.assertIn("csrf_token", body)        # every form carries it
                self.assertNotIn("Traceback", body)
        # The shared layout links the console, settings, account and logout.
        body = self.client.get("/admin").get_data(as_text=True)
        for needle in ("/admin/settings", "/admin/account", "/agent",
                       "/admin/logout"):
            self.assertIn(needle, body)

    def test_default_password_owners_are_flagged(self):
        default_id = ensure_account("owner_default", "DefaultPass77", "owner")
        changed_id = ensure_account("owner_changed", "TotallyNew88", "owner")
        self.addCleanup(db.set_must_change_password, default_id, False)
        self.addCleanup(db.set_must_change_password, changed_id, False)

        # Still on the configured default -> forced to change it.
        db._flag_default_password_owners("owner_default", "DefaultPass77")
        self.assertEqual(db.get_admin("owner_default")["must_change_password"], 1)
        self.assertEqual(db.get_admin("owner_changed")["must_change_password"], 0)

        # Even if it *is* the configured username, a changed password means the
        # owner keeps access without being forced through the reset flow.
        db.set_must_change_password(changed_id, False)
        db._flag_default_password_owners("owner_changed", "DefaultPass77")
        self.assertEqual(db.get_admin("owner_changed")["must_change_password"], 0)
        # ... while the account still on the default is caught by its password.
        db.set_must_change_password(default_id, False)
        db._flag_default_password_owners("owner_changed", "DefaultPass77")
        self.assertEqual(db.get_admin("owner_default")["must_change_password"], 1)

    def test_anonymous_visitors_are_sent_to_login(self):
        for path in ("/admin", "/admin/settings", "/admin/account"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login", location_parts(response)[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
