"""Team management, audit log, canned replies and forced password changes.

Runs against a throwaway SQLite database (the real ``clinic.db`` is never
touched) and needs no network access.

    python test_admin_team.py -v

Covers:
  * CSRF on every new POST and the agent-role 403 wall on every admin route
  * creating agents/owners with a one-time temporary password
  * display-name and role changes, reset password, deactivate/reactivate,
    force offline, self-service guards and the no-delete policy
  * the impact-confirmation flow for deactivating an agent with active chats
    (chats go back to the bot in the same transaction)
  * session invalidation through ``active`` and ``session_version`` on both
    ``/admin/*`` and ``/agent/api/*``
  * the forced agent password change at ``/agent/password``
  * the canned-replies editor (limits, trimming, duplicates, control chars)
  * placeholder substitution in ``GET /agent/api/canned``
  * audit-log contents and the 200-entry cap, with no secrets written
"""

import json
import os
import re
import tempfile
import unittest
from unittest import mock

# Point the db layer at a temp database before importing the app.
os.environ["SKIP_DB_INIT"] = "1"
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ.pop("COOKIE_SECURE", None)
os.environ.pop("ADMIN_PASSWORD", None)

import db  # noqa: E402

_TMPDIR = tempfile.mkdtemp(prefix="clinic_team_")
db.DB_FILE = os.path.join(_TMPDIR, "team.db")
db.init_db()

import app as app_mod  # noqa: E402

OWNER = ("team_owner", "TeamOwner99!")
AGENT_PASSWORD = "AgentPass99!"

_counter = 0


# ─── helpers ──────────────────────────────────────────────────────────────────
def unique(prefix):
    global _counter
    _counter += 1
    return "%s_%d" % (prefix, _counter)


def csrf_of(client):
    with client.session_transaction() as sess:
        return sess.get("csrf_token", "")


def login_owner(client, username=OWNER[0], password=OWNER[1]):
    client.get("/admin/login")
    client.post("/admin/login", data={"username": username, "password": password,
                                      "csrf_token": csrf_of(client)})
    return csrf_of(client)


def login_agent(client, username, password):
    client.post("/agent/login", data={"username": username,
                                      "password": password})
    return csrf_of(client)


def ensure_account(username, password, role, display=None):
    row = db.get_admin(username)
    if row:
        return row["id"]
    return db.create_admin(username, password,
                           display_name=display or username.title(), role=role)


def temp_password_from(html):
    match = re.search(r'class="secret-val">([^<]+)</span>', html)
    return match.group(1) if match else None


def impact_token_from(html):
    match = re.search(r'name="impact_token" value="([^"]+)"', html)
    return match.group(1) if match else None


class TeamTestBase(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.owner_id = ensure_account(OWNER[0], OWNER[1], "owner",
                                      display="Team Owner")
        db.set_must_change_password(cls.owner_id, False)
        # Keep test agents online long enough to queue a chat reliably.
        db.set_setting("agent_online_window_seconds", "600")
        db.set_setting("handoff_timeout_minutes", "60")
        db.set_setting("idle_close_minutes", "600")

    def setUp(self):
        app_mod._login_failures.clear()
        app_mod._login_blocks.clear()
        db.set_must_change_password(self.owner_id, False)
        self.client = app_mod.app.test_client()
        self.token = login_owner(self.client)

    # ── request helpers ──────────────────────────────────────────────────────
    def post(self, path, data=None, token=None, client=None):
        payload = dict(data or {})
        payload["csrf_token"] = self.token if token is None else token
        return (client or self.client).post(path, data=payload)

    def make_account(self, role="agent", password=AGENT_PASSWORD,
                     must_change=False, display=None):
        username = unique("team_" + role)
        admin_id = db.create_admin(username, password,
                                   display_name=display or username.title(),
                                   role=role,
                                   must_change_password=1 if must_change else 0)
        return username, password, admin_id

    def create_agent_via_form(self, username=None, display="New Agent",
                              role="agent"):
        username = username or unique("new_agent")
        response = self.post("/admin/team/new", {
            "username": username, "display_name": display, "role": role})
        return response, username, temp_password_from(response.get_data(as_text=True))

    def active_chat(self, agent_id, lang="en"):
        db.heartbeat(agent_id, True)
        sid = unique("session")
        conv = db.get_or_create_conversation(sid, lang)
        db.request_handoff(conv["id"], "team test")
        self.assertTrue(db.accept_conversation(conv["id"], agent_id))
        return conv["id"], sid

    def audit_rows(self, action=None, target=None):
        rows = db.list_audit_log(1000)
        if action is not None:
            rows = [row for row in rows if row.get("action") == action]
        if target is not None:
            rows = [row for row in rows if row.get("target") == target]
        return rows


# ─── team management ──────────────────────────────────────────────────────────
class TeamManagementTests(TeamTestBase):

    def test_team_page_and_no_delete_action(self):
        response = self.client.get("/admin/team")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Add a user", body)
        self.assertIn("Accounts", body)
        self.assertIn(OWNER[0], body)
        self.assertIn('name="csrf_token"', body)
        # Accounts are never hard-deleted from the panel.
        self.assertNotIn("/delete", body)

    def test_agents_get_403_on_every_admin_route(self):
        username, password, _agent_id = self.make_account()
        agent = app_mod.app.test_client()
        login_agent(agent, username, password)
        agent_token = csrf_of(agent)

        get_paths = (
            "/admin", "/admin/settings", "/admin/account", "/admin/team",
            "/admin/audit", "/admin/canned", "/admin/doctors",
            "/admin/services", "/admin/doctors/new", "/admin/services/new",
        )
        for path in get_paths:
            with self.subTest(method="GET", path=path):
                response = agent.get(path)
                self.assertEqual(response.status_code, 403)
                self.assertIn("agent accounts cannot",
                              response.get_data(as_text=True))

        post_paths = (
            ("/admin/logout", {}),
            ("/admin/settings", {"persona_name": "Hacked"}),
            ("/admin/settings/test_ai", {}),
            ("/admin/account", {"action": "profile", "display_name": "X",
                                "username": username}),
            ("/admin/team/new", {"username": "agent_creation_by_agent",
                                 "display_name": "Nope", "role": "owner"}),
            ("/admin/team/1/name", {"display_name": "Nope"}),
            ("/admin/team/1/role", {"role": "agent"}),
            ("/admin/team/1/toggle", {}),
            ("/admin/team/1/reset-password", {}),
            ("/admin/team/1/force-offline", {}),
            ("/admin/canned", {"canned_en": "[]", "canned_bn": "[]"}),
            ("/admin/confirm-impact", {"impact_token": "x"}),
            ("/admin/delete/1", {}),
        )
        for path, data in post_paths:
            with self.subTest(method="POST", path=path):
                payload = dict(data)
                payload["csrf_token"] = agent_token
                response = agent.post(path, data=payload)
                self.assertEqual(response.status_code, 403)
                self.assertIn("agent accounts cannot",
                              response.get_data(as_text=True))

        self.assertIsNone(db.get_admin("agent_creation_by_agent"))

    def test_new_posts_require_csrf(self):
        target_username, _password, target_id = self.make_account()
        new_username = unique("csrf_agent")
        targets = (
            ("/admin/team/new", {"username": new_username,
                                 "display_name": "CSRF", "role": "agent"}),
            ("/admin/team/%d/name" % target_id, {"display_name": "Changed"}),
            ("/admin/team/%d/role" % target_id, {"role": "owner"}),
            ("/admin/team/%d/toggle" % target_id, {}),
            ("/admin/team/%d/reset-password" % target_id, {}),
            ("/admin/team/%d/force-offline" % target_id, {}),
            ("/admin/canned", {"canned_en": "[]", "canned_bn": "[]"}),
            ("/admin/confirm-impact", {"impact_token": "x"}),
        )
        for path, data in targets:
            with self.subTest(path=path):
                response = self.client.post(path, data=data)
                self.assertEqual(response.status_code, 403)
                self.assertIn("security token",
                              response.get_data(as_text=True).lower())

        # No side effects from any rejected request.
        self.assertIsNone(db.get_admin(new_username))
        target = db.get_admin_by_id(target_id)
        self.assertEqual(target["display_name"], target_username.title())
        self.assertEqual(target["role"], "agent")
        self.assertEqual(target["active"], 1)

    def test_create_agent_shows_a_one_time_temporary_password(self):
        response, username, temp = self.create_agent_via_form(
            display="Riya Agent")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(temp, "the temporary password should be shown once")
        self.assertEqual(len(temp), 16)
        self.assertIn("shown once", body.lower())
        self.assertIn(username, body)

        row = db.get_admin(username)
        self.assertEqual(row["role"], "agent")
        self.assertEqual(row["display_name"], "Riya Agent")
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["must_change_password"], 1)
        self.assertNotEqual(row["password_hash"], temp)
        self.assertTrue(db.verify_admin(username, temp))
        self.assertIsNone(db.verify_admin(username, "not-the-password"))

        # It is not stored or rendered on the next visit, and never audited.
        fresh = self.client.get("/admin/team").get_data(as_text=True)
        self.assertNotIn(temp, fresh)
        entries = self.audit_rows(action="team_added", target=username)
        self.assertEqual(len(entries), 1)
        self.assertNotIn(temp, json.dumps(entries))
        self.assertIn("temporary password", entries[0]["detail"].lower())

    def test_team_create_validation_and_case_insensitive_uniqueness(self):
        username, _password, _admin_id = self.make_account()
        bad_cases = (
            ({"username": "AB", "display_name": "Too Short",
              "role": "agent"}, "3–30"),
            ({"username": username, "display_name": "Duplicate",
              "role": "agent"}, "already taken"),
            ({"username": unique("bad"), "display_name": "",
              "role": "agent"}, "Display name is required"),
            ({"username": unique("bad"), "display_name": "<b>HTML</b>",
              "role": "agent"}, "contain HTML"),
            ({"username": unique("bad"), "display_name": "Visit www.evil.com",
              "role": "agent"}, "contain a URL"),
            ({"username": unique("bad"), "display_name": "Bad Role",
              "role": "superuser"}, "Role must be agent or owner"),
        )
        for data, needle in bad_cases:
            with self.subTest(needle=needle):
                response = self.post("/admin/team/new", data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(needle, response.get_data(as_text=True))
                if data["username"] != username:
                    self.assertIsNone(db.get_admin(data["username"]))

        # A valid owner account can be created too.
        response, owner_name, temp = self.create_agent_via_form(
            role="owner", display="Second Owner")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(temp)
        self.assertEqual(db.get_admin(owner_name)["role"], "owner")

    def test_display_name_edit_and_validation(self):
        username, _password, admin_id = self.make_account()

        response = self.post("/admin/team/%d/name" % admin_id,
                             {"display_name": "Aisha Rahman"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin(username)["display_name"], "Aisha Rahman")
        self.assertEqual(len(self.audit_rows(action="team_display_name",
                                             target=username)), 1)

        response = self.post("/admin/team/%d/name" % admin_id,
                             {"display_name": "<script>alert(1)</script>"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin(username)["display_name"], "Aisha Rahman")

        # Editing your own name from the Team page refreshes the session label.
        original = db.get_admin(OWNER[0])["display_name"]
        self.addCleanup(db.update_admin, self.owner_id, display_name=original)
        response = self.post("/admin/team/%d/name" % self.owner_id,
                             {"display_name": "Head Owner"})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("admin_display_name"), "Head Owner")

    def test_role_change_and_self_demotion_guard(self):
        username, _password, admin_id = self.make_account()

        response = self.post("/admin/team/%d/role" % admin_id,
                             {"role": "owner"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin(username)["role"], "owner")
        self.assertEqual(len(self.audit_rows(action="team_role",
                                             target=username)), 1)

        response = self.post("/admin/team/%d/role" % admin_id,
                             {"role": "owner"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin(username)["role"], "owner")
        response = self.post("/admin/team/%d/role" % admin_id,
                             {"role": "nonsense"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin(username)["role"], "owner")

        # You can't demote yourself away from owner.
        response = self.post("/admin/team/%d/role" % self.owner_id,
                             {"role": "agent"}, token=self.token,
                             client=self.client)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin(OWNER[0])["role"], "owner")

    def test_self_deactivation_and_force_offline_are_blocked(self):
        response = self.post("/admin/team/%d/toggle" % self.owner_id,
                             {"impact_token": "confirmed"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin_by_id(self.owner_id)["active"], 1)

        db.heartbeat(self.owner_id, True)
        response = self.post("/admin/team/%d/force-offline" % self.owner_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin_by_id(self.owner_id)["is_online"], 1)

    def test_reset_password_invalidates_existing_agent_sessions(self):
        username, password, admin_id = self.make_account()
        before_version = db.get_admin_by_id(admin_id)["session_version"]

        agent = app_mod.app.test_client()
        login_agent(agent, username, password)
        self.assertEqual(agent.get("/agent/api/canned").status_code, 200)

        response = self.post("/admin/team/%d/reset-password" % admin_id)
        body = response.get_data(as_text=True)
        temp = temp_password_from(body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(temp)
        self.assertEqual(len(self.audit_rows(action="team_password_reset",
                                             target=username)), 1)

        row = db.get_admin(username)
        self.assertEqual(row["must_change_password"], 1)
        self.assertEqual(row["session_version"], before_version + 1)
        self.assertTrue(db.verify_admin(username, temp))
        self.assertIsNone(db.verify_admin(username, password))

        # The old session is dead everywhere.
        self.assertEqual(agent.get("/agent/api/canned").status_code, 401)
        self.assertEqual(agent.get("/agent").status_code, 302)
        self.assertIn("/agent/login",
                      agent.get("/agent").headers.get("Location", ""))

        # The temporary password works, but only to reach the change page.
        fresh = app_mod.app.test_client()
        login_agent(fresh, username, temp)
        self.assertEqual(fresh.get("/agent").status_code, 302)
        self.assertIn("/agent/password",
                      fresh.get("/agent").headers.get("Location", ""))

    def test_deactivate_reactivate_and_force_offline(self):
        username, password, admin_id = self.make_account()
        db.heartbeat(admin_id, True)
        agent = app_mod.app.test_client()
        login_agent(agent, username, password)
        self.assertEqual(agent.get("/agent/api/queue").status_code, 200)

        response = self.post("/admin/team/%d/force-offline" % admin_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin_by_id(admin_id)["is_online"], 0)
        self.assertEqual(len(self.audit_rows(action="team_force_offline",
                                             target=username)), 1)
        # Force offline only clears the switch; the login stays valid.
        self.assertEqual(agent.get("/agent/api/queue").status_code, 200)

        before_version = db.get_admin_by_id(admin_id)["session_version"]
        response = self.post("/admin/team/%d/toggle" % admin_id)
        self.assertEqual(response.status_code, 302)
        row = db.get_admin(username)
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["is_online"], 0)
        self.assertEqual(row["session_version"], before_version + 1)
        self.assertEqual(agent.get("/agent/api/queue").status_code, 401)

        failed = app_mod.app.test_client()
        login_agent(failed, username, password)
        self.assertEqual(failed.get("/agent").status_code, 302)
        self.assertIn("/agent/login",
                      failed.get("/agent").headers.get("Location", ""))

        response = self.post("/admin/team/%d/toggle" % admin_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin_by_id(admin_id)["active"], 1)
        self.assertEqual(len(self.audit_rows(action="team_reactivated",
                                             target=username)), 1)

        again = app_mod.app.test_client()
        login_agent(again, username, password)
        self.assertEqual(again.get("/agent/api/queue").status_code, 200)

    def test_deactivating_agent_with_active_chats_requires_confirmation(self):
        username, _password, admin_id = self.make_account()
        chat_id, _sid = self.active_chat(admin_id)
        before_version = db.get_admin_by_id(admin_id)["session_version"]

        response = self.post("/admin/team/%d/toggle" % admin_id)
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("still has active chats", body)
        self.assertIn("#%d" % chat_id, body)
        token = impact_token_from(body)
        self.assertTrue(token)

        # Nothing changed before confirmation.
        self.assertEqual(db.get_admin_by_id(admin_id)["active"], 1)
        conv = db.get_conversation(chat_id)
        self.assertEqual(conv["status"], "with_agent")
        self.assertEqual(conv["agent_id"], admin_id)

        response = self.post("/admin/confirm-impact",
                             {"impact_token": token})
        self.assertEqual(response.status_code, 302)

        row = db.get_admin(username)
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["session_version"], before_version + 1)
        conv = db.get_conversation(chat_id)
        self.assertEqual(conv["status"], "bot")
        self.assertIsNone(conv["agent_id"])
        data = db.get_conversation_with_messages(chat_id)
        self.assertTrue(data["messages"])
        last = data["messages"][-1]
        self.assertEqual(last["sender_type"], "system")
        self.assertEqual(last["content"], db.AGENT_BACK_MESSAGE[0])

        entries = self.audit_rows(action="team_deactivated", target=username)
        self.assertEqual(len(entries), 1)
        self.assertIn("1 active chat", entries[0]["detail"])

    def test_deactivation_rolls_back_as_one_transaction(self):
        username, _password, admin_id = self.make_account()
        chat_id, _sid = self.active_chat(admin_id)
        before_version = db.get_admin_by_id(admin_id)["session_version"]

        with mock.patch.object(db, "_insert_message",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                db.deactivate_admin(admin_id)

        row = db.get_admin_by_id(admin_id)
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["session_version"], before_version)
        conv = db.get_conversation(chat_id)
        self.assertEqual(conv["status"], "with_agent")
        self.assertEqual(conv["agent_id"], admin_id)
        # No half-written system message either.
        contents = [m["content"] for m in
                    db.get_conversation_with_messages(chat_id)["messages"]]
        self.assertNotIn(db.AGENT_BACK_MESSAGE[0], contents)

    def test_expired_impact_token_changes_nothing(self):
        username, _password, admin_id = self.make_account()
        chat_id, _sid = self.active_chat(admin_id)
        response = self.post("/admin/confirm-impact",
                             {"impact_token": "not-a-valid-token"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_admin_by_id(admin_id)["active"], 1)
        self.assertEqual(db.get_conversation(chat_id)["status"], "with_agent")


# ─── session invalidation ─────────────────────────────────────────────────────
class SessionInvalidationTests(TeamTestBase):

    def test_admin_session_dies_on_version_bump_inactive_or_delete(self):
        # (a) a password reset bumps session_version
        username, password, admin_id = self.make_account(role="owner",
                                                         password="OwnerOne99!")
        client = app_mod.app.test_client()
        login_owner(client, username, password)
        self.assertEqual(client.get("/admin/team").status_code, 200)
        db.reset_admin_password(admin_id, "OwnerReset99!")
        response = client.get("/admin/team")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers.get("Location", ""))
        with client.session_transaction() as sess:
            self.assertNotIn("admin_id", sess)

        # (b) an inactive owner is rejected even with a valid session cookie
        username, password, admin_id = self.make_account(role="owner",
                                                         password="OwnerTwo99!")
        client = app_mod.app.test_client()
        login_owner(client, username, password)
        self.assertEqual(client.get("/admin").status_code, 200)
        with db._connect() as conn:
            conn.execute("UPDATE admins SET active = 0 WHERE id = ?", (admin_id,))
        response = client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers.get("Location", ""))

        # (c) a deleted account cannot keep using its session
        username, password, admin_id = self.make_account(role="owner",
                                                         password="OwnerThree99!")
        client = app_mod.app.test_client()
        login_owner(client, username, password)
        self.assertEqual(client.get("/admin/audit").status_code, 200)
        db.delete_admin(admin_id)
        response = client.get("/admin/audit")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers.get("Location", ""))

    def test_agent_sessions_die_on_version_bump_inactive_or_delete(self):
        api_paths = (
            ("GET", "/agent/api/queue"),
            ("GET", "/agent/api/canned"),
            ("GET", "/agent/api/callbacks"),
            ("GET", "/agent/api/conversation/1"),
            ("POST", "/agent/api/heartbeat"),
            ("POST", "/agent/api/conversation/1/accept"),
            ("POST", "/agent/api/conversation/1/message"),
            ("POST", "/agent/api/conversation/1/handback"),
            ("POST", "/agent/api/conversation/1/close"),
            ("POST", "/agent/api/callbacks/1/status"),
        )

        # (a) password reset / forced sign-out bumps the version
        username, password, admin_id = self.make_account()
        agent = app_mod.app.test_client()
        token = login_agent(agent, username, password)
        self.assertEqual(agent.get("/agent/api/queue").status_code, 200)
        db.reset_admin_password(admin_id, "AgentReset99!")
        for method, path in api_paths:
            with self.subTest(method=method, path=path):
                if method == "GET":
                    response = agent.get(path)
                else:
                    response = agent.post(
                        path, json={}, headers={"X-CSRF-Token": token})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_json(), {"error": "unauthorized"})

        # (b) an inactive account is rejected
        username, password, admin_id = self.make_account()
        agent = app_mod.app.test_client()
        login_agent(agent, username, password)
        with db._connect() as conn:
            conn.execute("UPDATE admins SET active = 0 WHERE id = ?", (admin_id,))
        self.assertEqual(agent.get("/agent/api/queue").status_code, 401)
        self.assertEqual(agent.get("/agent").status_code, 302)

        # (c) a deleted account is rejected
        username, password, admin_id = self.make_account()
        agent = app_mod.app.test_client()
        login_agent(agent, username, password)
        db.delete_admin(admin_id)
        self.assertEqual(agent.get("/agent/api/canned").status_code, 401)
        self.assertEqual(agent.get("/agent").status_code, 302)

    def test_sessions_store_the_current_version_at_login(self):
        agent_username, agent_password, agent_id = self.make_account()
        before = db.get_admin_by_id(agent_id)["session_version"]
        agent = app_mod.app.test_client()
        login_agent(agent, agent_username, agent_password)
        with agent.session_transaction() as sess:
            self.assertEqual(sess.get("agent_session_version"), before)

        owner_username, owner_password, owner_id = self.make_account(
            role="owner", password="OwnerVersion99!")
        before = db.get_admin_by_id(owner_id)["session_version"]
        owner = app_mod.app.test_client()
        login_owner(owner, owner_username, owner_password)
        with owner.session_transaction() as sess:
            self.assertEqual(sess.get("admin_session_version"), before)


# ─── forced agent password change ─────────────────────────────────────────────
class AgentPasswordTests(TeamTestBase):

    def test_forced_change_blocks_the_console_and_api_until_done(self):
        response, username, temp = self.create_agent_via_form(display="Forced Agent")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(temp)

        agent = app_mod.app.test_client()
        token = login_agent(agent, username, temp)

        response = agent.get("/agent")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/agent/password", response.headers.get("Location", ""))

        response = agent.get("/agent/password")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("temporary password", body.lower())
        self.assertIn('name="csrf_token"', body)
        self.assertIn("current_password", body)

        # Every agent API endpoint is blocked while the flag is set.
        api_paths = (
            ("GET", "/agent/api/queue"),
            ("GET", "/agent/api/canned"),
            ("GET", "/agent/api/callbacks"),
            ("GET", "/agent/api/conversation/1"),
            ("POST", "/agent/api/heartbeat"),
            ("POST", "/agent/api/conversation/1/accept"),
            ("POST", "/agent/api/conversation/1/message"),
            ("POST", "/agent/api/conversation/1/handback"),
            ("POST", "/agent/api/conversation/1/close"),
            ("POST", "/agent/api/callbacks/1/status"),
        )
        for method, path in api_paths:
            with self.subTest(method=method, path=path):
                if method == "GET":
                    response = agent.get(path)
                else:
                    response = agent.post(
                        path, json={}, headers={"X-CSRF-Token": token})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json(),
                                 {"error": "must_change_password"})

        # CSRF is required before anything is changed.
        response = agent.post("/agent/password", data={
            "current_password": temp, "new_password": "BrandNew99!",
            "confirm_password": "BrandNew99!"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.get_admin(username)["must_change_password"], 1)

    def test_password_policy_and_successful_change(self):
        response, username, temp = self.create_agent_via_form(display="Policy Agent")
        self.assertTrue(temp)
        agent = app_mod.app.test_client()
        login_agent(agent, username, temp)
        token = csrf_of(agent)

        bad_cases = (
            ({"current_password": "wrong", "new_password": "BrandNew99!",
              "confirm_password": "BrandNew99!"}, "current password is incorrect"),
            ({"current_password": temp, "new_password": "short",
              "confirm_password": "short"}, "at least 8"),
            ({"current_password": temp, "new_password": username,
              "confirm_password": username}, "can't be your username"),
            ({"current_password": temp, "new_password": temp,
              "confirm_password": temp}, "different from the current"),
            ({"current_password": temp, "new_password": "BrandNew99!",
              "confirm_password": "BrandNew98!"}, "The two new passwords"),
        )
        for data, needle in bad_cases:
            with self.subTest(needle=needle):
                payload = dict(data)
                payload["csrf_token"] = token
                response = agent.post("/agent/password", data=payload)
                self.assertEqual(response.status_code, 200)
                self.assertIn(needle, response.get_data(as_text=True))
                self.assertEqual(db.get_admin(username)["must_change_password"], 1)

        new_password = "BrandNew99!"
        response = agent.post("/agent/password", data={
            "csrf_token": token, "current_password": temp,
            "new_password": new_password, "confirm_password": new_password})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/agent", response.headers.get("Location", ""))

        row = db.get_admin(username)
        self.assertEqual(row["must_change_password"], 0)
        self.assertIsNone(db.verify_admin(username, temp))
        self.assertTrue(db.verify_admin(username, new_password))

        # The current session stays valid and the console is reachable.
        canned = agent.get("/agent/api/canned")
        self.assertEqual(canned.status_code, 200)
        console = agent.get("/agent")
        self.assertEqual(console.status_code, 200)
        self.assertIn('href="/agent/password"',
                      console.get_data(as_text=True))

        entries = self.audit_rows(action="password_changed", target=username)
        self.assertEqual(len(entries), 1)
        self.assertNotIn(new_password, json.dumps(entries))
        self.assertNotIn(temp, json.dumps(entries))

        # The temporary password can no longer sign in.
        fresh = app_mod.app.test_client()
        login_agent(fresh, username, temp)
        self.assertEqual(fresh.get("/agent").status_code, 302)
        self.assertIn("/agent/login",
                      fresh.get("/agent").headers.get("Location", ""))

    def test_non_forced_agent_can_also_change_password(self):
        username, password, _admin_id = self.make_account()
        agent = app_mod.app.test_client()
        login_agent(agent, username, password)
        token = csrf_of(agent)
        response = agent.post("/agent/password", data={
            "csrf_token": token, "current_password": password,
            "new_password": "AnotherPass99!",
            "confirm_password": "AnotherPass99!"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(db.verify_admin(username, "AnotherPass99!"))
        self.assertEqual(agent.get("/agent").status_code, 200)


# ─── canned replies ───────────────────────────────────────────────────────────
class CannedReplyTests(TeamTestBase):

    def setUp(self):
        super().setUp()
        self.original_en = db.get_canned_replies("en")
        self.original_bn = db.get_canned_replies("bn")

    def tearDown(self):
        db.set_canned_replies("en", self.original_en)
        db.set_canned_replies("bn", self.original_bn)

    def test_editor_renders_both_languages_and_a_single_post(self):
        response = self.client.get("/admin/canned")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="list-en"', body)
        self.assertIn('id="list-bn"', body)
        self.assertIn('id="canned_en"', body)
        self.assertIn('id="canned_bn"', body)
        self.assertIn("Add English reply", body)
        self.assertIn("Add Bengali reply", body)
        self.assertIn("{agent_name}", body)
        self.assertIn("{clinic_name}", body)
        self.assertEqual(body.count('action="/admin/canned"'), 1)
        self.assertEqual(body.count('name="canned_en"'), 1)
        self.assertEqual(body.count('name="canned_bn"'), 1)
        # No inline event handler renders stored text as HTML.
        self.assertNotIn("innerHTML", body)

    def test_canned_save_trims_and_strips_control_characters(self):
        response = self.post("/admin/canned", {
            "canned_en": json.dumps(["  Hello {agent_name}  ",
                                     "He\u0007llo {clinic_name}", "কেমন আছেন?"]),
            "canned_bn": json.dumps(["  আসসালামু আলাইকুম {agent_name}  ",
                                     "আপনার ফোন নম্বর? \u0000"]),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_canned_replies("en"),
                         ["Hello {agent_name}", "Hello {clinic_name}",
                          "কেমন আছেন?"])
        self.assertEqual(db.get_canned_replies("bn"),
                         ["আসসালামু আলাইকুম {agent_name}",
                          "আপনার ফোন নম্বর?"])
        entries = self.audit_rows(action="canned_replies_saved")
        self.assertGreaterEqual(len(entries), 1)

    def test_canned_validation_rejects_without_saving(self):
        self.post("/admin/canned", {
            "canned_en": json.dumps(["Original"]),
            "canned_bn": json.dumps(["মূল"])})
        self.assertEqual(db.get_canned_replies("en"), ["Original"])

        cases = (
            ("not json", "must be valid JSON"),
            ('{"a": 1}', "must be a JSON list"),
            ('[1, "ok"]', "must be text"),
            ('["   "]', "can't be empty"),
            (json.dumps(["x" * 301]), "longer than 300"),
            (json.dumps(["Hello", " hello "]), "is a duplicate"),
            (json.dumps(["Reply %d" % i for i in range(21)]), "at most 20"),
            ('["\\u0000"]', "can't be empty"),
        )
        for raw, needle in cases:
            with self.subTest(needle=needle):
                response = self.post("/admin/canned", {
                    "canned_en": raw, "canned_bn": "[]"})
                self.assertEqual(response.status_code, 200)
                self.assertIn(needle, response.get_data(as_text=True))
                self.assertEqual(db.get_canned_replies("en"), ["Original"])
                self.assertEqual(db.get_canned_replies("bn"), ["মূল"])

        # Boundaries: exactly 20 replies and exactly 300 characters are fine.
        allowed = ["Reply %02d" % i for i in range(20)]
        response = self.post("/admin/canned", {
            "canned_en": json.dumps(allowed), "canned_bn": "[]"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_canned_replies("en"), allowed)

        exact_len = "z" * 300
        response = self.post("/admin/canned", {
            "canned_en": json.dumps([exact_len]), "canned_bn": "[]"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_canned_replies("en"), [exact_len])

    def test_canned_text_is_escaped_in_the_admin_editor(self):
        payload = '<script>alert("x")</script>'
        response = self.post("/admin/canned", {
            "canned_en": json.dumps([payload]), "canned_bn": "[]"})
        self.assertEqual(response.status_code, 302)
        body = self.client.get("/admin/canned").get_data(as_text=True)
        self.assertNotIn(payload, body)
        self.assertIn("\\u003cscript\\u003e", body)
        self.assertEqual(db.get_canned_replies("en"), [payload])

    def test_agent_canned_api_substitutes_placeholders_per_language(self):
        clinic_en = "Smile Dental Care"
        clinic_bn = "স্মাইল ডেন্টাল কেয়ার"
        old_en = db.get_setting("clinic_name")
        old_bn = db.get_setting("clinic_name_bn")
        self.addCleanup(db.set_setting, "clinic_name", old_en)
        self.addCleanup(db.set_setting, "clinic_name_bn", old_bn)
        db.set_setting("clinic_name", clinic_en)
        db.set_setting("clinic_name_bn", clinic_bn)

        db.set_canned_replies("en", [
            "Hi {agent_name} from {clinic_name} {unknown_placeholder}",
            "{agent_name} / {agent_name}",
        ])
        db.set_canned_replies("bn", [
            "হ্যালো {agent_name}, {clinic_name} থেকে বলছি {unknown}",
        ])

        username, password, admin_id = self.make_account(display="Riya Sen")
        self.assertEqual(db.get_admin_by_id(admin_id)["display_name"], "Riya Sen")
        agent = app_mod.app.test_client()
        login_agent(agent, username, password)

        response = agent.get("/agent/api/canned")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["en"], [
            "Hi Riya Sen from %s {unknown_placeholder}" % clinic_en,
            "Riya Sen / Riya Sen",
        ])
        self.assertEqual(body["bn"], [
            "হ্যালো Riya Sen, %s থেকে বলছি {unknown}" % clinic_bn,
        ])

    def test_agent_canned_api_requires_login(self):
        anonymous = app_mod.app.test_client()
        response = anonymous.get("/agent/api/canned")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "unauthorized"})


# ─── audit log ────────────────────────────────────────────────────────────────
class AuditLogTests(TeamTestBase):

    def test_audit_page_shows_the_latest_200_newest_first(self):
        for index in range(205):
            db.audit("bulk_test", target="bulk", detail="row %d" % index)
        rows = db.list_audit_log(200)
        self.assertEqual(len(rows), 200)
        ids = [row["id"] for row in rows]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(ids[0], max(ids))
        self.assertNotEqual(ids[0], ids[-1])

        response = self.client.get("/admin/audit")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Read-only", body)
        self.assertIn("bulk_test", body)
        self.assertIn("newest first", body.lower())

    def test_failed_and_blocked_logins_are_audited_without_passwords(self):
        username = unique("ghost_login")
        secret = "SuperSecret99!"
        attacker = app_mod.app.test_client()
        attacker.get("/admin/login")
        token = csrf_of(attacker)
        with self.assertLogs("clinic.admin", level="WARNING"):
            for _attempt in range(5):
                attacker.post("/admin/login", data={
                    "username": username, "password": secret,
                    "csrf_token": token})
            # The sixth attempt is blocked before the password is checked.
            response = attacker.post("/admin/login", data={
                "username": username, "password": secret,
                "csrf_token": token})
            self.assertIn("Too many failed attempts",
                          response.get_data(as_text=True))

        rows = self.audit_rows(target=username)
        actions = [row["action"] for row in rows]
        self.assertGreaterEqual(actions.count("login_failed"), 5)
        self.assertIn("login_blocked", actions)
        blob = json.dumps(rows)
        self.assertNotIn(secret, blob)
        self.assertNotIn(secret, response.get_data(as_text=True))

        # Agent login failures are audited by username only as well.
        agent_name = unique("ghost_agent")
        agent = app_mod.app.test_client()
        with self.assertLogs("clinic.admin", level="WARNING"):
            agent.post("/agent/login", data={"username": agent_name,
                                             "password": secret})
        rows = self.audit_rows(target=agent_name)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "login_failed")
        self.assertNotIn(secret, json.dumps(rows))

    def test_team_actions_and_password_changes_are_audited(self):
        username, password, admin_id = self.make_account()
        db.heartbeat(admin_id, True)

        self.post("/admin/team/%d/name" % admin_id,
                  {"display_name": "Audited Agent"})
        self.post("/admin/team/%d/role" % admin_id, {"role": "owner"})
        reset = self.post("/admin/team/%d/reset-password" % admin_id)
        reset_temp = temp_password_from(reset.get_data(as_text=True))
        self.post("/admin/team/%d/force-offline" % admin_id)
        self.post("/admin/team/%d/toggle" % admin_id)
        self.post("/admin/team/%d/toggle" % admin_id)

        self.post("/admin/canned", {
            "canned_en": json.dumps(["Audited canned reply"]),
            "canned_bn": json.dumps(["অডিটেড"])})

        # An owner changing their own password is audited too.
        owner_name, owner_password, owner_id = self.make_account(
            role="owner", password="AuditOwner99!")
        owner = app_mod.app.test_client()
        owner_token = login_owner(owner, owner_name, owner_password)
        response = owner.post("/admin/account", data={
            "action": "password", "csrf_token": owner_token,
            "current_password": owner_password,
            "new_password": "AuditOwnerNew99!",
            "confirm_password": "AuditOwnerNew99!"})
        self.assertEqual(response.status_code, 302)

        expected = {"team_display_name", "team_role", "team_password_reset",
                    "team_force_offline", "team_deactivated",
                    "team_reactivated", "canned_replies_saved",
                    "password_changed"}
        for action in expected:
            with self.subTest(action=action):
                self.assertTrue(self.audit_rows(action=action),
                                "missing audit action %s" % action)

        rows = self.audit_rows(target=username)
        self.assertTrue(any(row["action"] == "team_deactivated"
                            for row in rows))
        blob = json.dumps(db.list_audit_log(1000))
        self.assertNotIn(password, blob)
        self.assertNotIn("AuditOwnerNew99!", blob)
        if reset_temp:
            self.assertNotIn(reset_temp, blob)

    def test_new_accounts_get_the_new_columns_and_migrations_are_idempotent(self):
        db.init_db()   # running the migration again must be harmless
        db.init_db()
        with db._connect() as conn:
            columns = {row["name"] for row in
                       conn.execute("PRAGMA table_info(admins)")}
            self.assertIn("active", columns)
            self.assertIn("session_version", columns)
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'admin_audit_log'").fetchone()
            self.assertIsNotNone(table)
            index = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_admin_audit_log_created'").fetchone()
            self.assertIsNotNone(index)

        row = db.get_admin(OWNER[0])
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["session_version"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
