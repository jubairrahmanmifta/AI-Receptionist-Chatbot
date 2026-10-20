"""Admin catalog tests (Prompt 4B) — doctors, days off and services.

Runs against a throwaway database (the real clinic.db is never touched) and
needs no network access.

Covers:
  * CSRF on every new POST and the agent-role 403 wall
  * doctor form validation (name, slot length) and schedule validation
    (inverted and overlapping ranges, ranges shorter than one slot)
  * atomic schedule saves — a rejected save leaves the previous schedule intact
  * the impact-confirmation flow — nothing changes until the owner confirms
  * disabling a doctor removes them from the prompt and from slot generation
  * days off removing dates from slots and the booking validator explaining why
  * delete blocked while appointments exist
  * slot generation with several ranges on one day

    python test_admin_catalog.py -v
"""

import datetime
import os
import re
import tempfile
import unittest
from urllib.parse import urlparse

# Point the db layer at a temp database before importing the app.
os.environ["SKIP_DB_INIT"] = "1"
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ.pop("COOKIE_SECURE", None)
os.environ.pop("ADMIN_PASSWORD", None)

import db  # noqa: E402

_TMPDIR = tempfile.mkdtemp(prefix="clinic_catalog_")
db.DB_FILE = os.path.join(_TMPDIR, "catalog.db")
db.init_db()

import app as app_mod  # noqa: E402
import prompt_builder  # noqa: E402
from werkzeug.datastructures import MultiDict  # noqa: E402

OWNER = ("cat_owner", "CatOwner99!")
AGENT = ("cat_agent", "CatAgent99!")


# ─── helpers ──────────────────────────────────────────────────────────────────
def csrf_of(client):
    with client.session_transaction() as sess:
        return sess.get("csrf_token", "")


def login_owner(client):
    client.get("/admin/login")
    client.post("/admin/login", data={"username": OWNER[0], "password": OWNER[1],
                                      "csrf_token": csrf_of(client)})
    return csrf_of(client)


def login_agent(client):
    """Agents authenticate at /agent/login, not /admin/login."""
    client.post("/agent/login", data={"username": AGENT[0], "password": AGENT[1]})
    return csrf_of(client)


def token_from(html):
    """Pull the signed impact token out of a confirmation page."""
    match = re.search(r'name="impact_token" value="([^"]+)"', html)
    return match.group(1) if match else None


def next_monday(offset_weeks=1):
    today = db.dhaka_today()
    days = (0 - today.weekday()) % 7 or 7
    return today + datetime.timedelta(days=days + 7 * (offset_weeks - 1))


class CatalogTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.owner_id = db.create_admin(OWNER[0], OWNER[1], role="owner")
        cls.agent_id = db.create_admin(AGENT[0], AGENT[1], role="agent")

    def setUp(self):
        db.set_must_change_password(self.owner_id, False)
        self.client = app_mod.app.test_client()
        self.token = login_owner(self.client)
        # A private doctor so tests don't fight over the seeded ones.
        self.doctor_id = db.create_doctor(
            name_en="Dr. Test", name_bn="ডা. টেস্ট", specialty_en="Testing",
            specialty_bn="", bio_en="", bio_bn="", slot_minutes=30, active=1,
            schedule=[{"weekday": 0, "start_time": "10:00", "end_time": "13:00"}])

    def tearDown(self):
        # Remove anything this test created (schedule cascades with the doctor).
        for appt in db.list_appointments():
            if appt.get("doctor_id") == self.doctor_id:
                db.delete_appointment(appt["id"])
        db.delete_doctor(self.doctor_id)

    # ── request helpers ──────────────────────────────────────────────────────
    def post(self, path, pairs, token=None):
        token = self.token if token is None else token
        return self.client.post(path, data=MultiDict([("csrf_token", token)] + pairs))

    def doctor_form(self, **overrides):
        base = [("name_en", "Dr. Test"), ("name_bn", "ডা. টেস্ট"),
                ("specialty_en", "Testing"), ("specialty_bn", ""),
                ("bio_en", ""), ("bio_bn", ""), ("slot_minutes", "30"),
                ("active", "1"),
                ("start_0", "10:00"), ("end_0", "13:00")]
        return overrides.get("pairs", base)

    def make_appointment(self, date, time, doctor_id=None):
        return db.create_appointment(
            patient_name="Catalog Patient", phone="01712345678",
            doctor_id=doctor_id or self.doctor_id, service_id=None,
            date=date, time=time, status="pending", language="en")

    # ── 1. security: CSRF + agent wall ───────────────────────────────────────
    def test_every_new_post_requires_csrf(self):
        targets = [
            ("/admin/doctors/new", self.doctor_form()),
            ("/admin/doctors/%d" % self.doctor_id, self.doctor_form()),
            ("/admin/doctors/%d/toggle" % self.doctor_id, []),
            ("/admin/doctors/%d/delete" % self.doctor_id, []),
            ("/admin/doctors/%d" % self.doctor_id,
             [("action", "add_time_off"), ("date_from", next_monday().isoformat()),
              ("date_to", next_monday().isoformat())]),
            ("/admin/services/new",
             [("name_en", "CSRF Svc"), ("duration_minutes", "30")]),
            ("/admin/services/1/toggle", []),
            ("/admin/services/1/delete", []),
            ("/admin/confirm-impact", [("impact_token", "x.y")]),
        ]
        for path, pairs in targets:
            response = self.post(path, pairs, token="")
            self.assertEqual(response.status_code, 403, path)

    def test_agent_role_is_forbidden_on_catalog_routes(self):
        client = app_mod.app.test_client()
        token = login_agent(client)
        paths = ["/admin/doctors", "/admin/doctors/new",
                 "/admin/doctors/%d" % self.doctor_id,
                 "/admin/services", "/admin/services/new",
                 "/admin/services/1"]
        for path in paths:
            self.assertEqual(client.get(path).status_code, 403, path)
        response = client.post("/admin/doctors/%d/toggle" % self.doctor_id,
                               data={"csrf_token": token})
        self.assertEqual(response.status_code, 403)

    # ── 2. doctor validation ─────────────────────────────────────────────────
    def test_doctor_requires_a_name_and_valid_slot_length(self):
        response = self.post("/admin/doctors/new",
                             [("name_en", ""), ("name_bn", ""),
                              ("slot_minutes", "30"), ("active", "1"),
                              ("start_0", "10:00"), ("end_0", "12:00")])
        self.assertIn("needs a name", response.get_data(as_text=True))

        for bad in ("7", "150", "33", "abc"):
            response = self.post("/admin/doctors/%d" % self.doctor_id,
                                 self.doctor_form(pairs=[
                                     ("name_en", "Dr. Test"),
                                     ("slot_minutes", bad), ("active", "1"),
                                     ("start_0", "10:00"), ("end_0", "13:00")]))
            body = response.get_data(as_text=True)
            self.assertIn("Slot length", body, bad)

    def test_schedule_ranges_must_be_ordered_and_not_overlap(self):
        inverted = self.post("/admin/doctors/%d" % self.doctor_id,
                             [("name_en", "Dr. Test"), ("slot_minutes", "30"),
                              ("active", "1"),
                              ("start_0", "12:00"), ("end_0", "10:00")])
        self.assertIn("before the end", inverted.get_data(as_text=True))

        overlap = self.post("/admin/doctors/%d" % self.doctor_id,
                            self.doctor_form(pairs=[
                                ("name_en", "Dr. Test"), ("slot_minutes", "30"),
                                ("active", "1"),
                                ("start_0", "10:00"), ("end_0", "12:00"),
                                ("start_0", "11:00"), ("end_0", "13:00")]))
        self.assertIn("overlap", overlap.get_data(as_text=True))

    def test_range_shorter_than_a_slot_is_rejected(self):
        response = self.post("/admin/doctors/%d" % self.doctor_id,
                             [("name_en", "Dr. Test"), ("slot_minutes", "60"),
                              ("active", "1"),
                              ("start_0", "10:00"), ("end_0", "10:30")])
        self.assertIn("shorter than one", response.get_data(as_text=True))

    def test_schedule_save_is_atomic_on_failure(self):
        before = [(s["weekday"], s["start_time"], s["end_time"])
                  for s in db.get_doctor(self.doctor_id)["schedule"]]
        # An invalid save (overlap) must leave the stored schedule untouched.
        self.post("/admin/doctors/%d" % self.doctor_id,
                  self.doctor_form(pairs=[
                      ("name_en", "Dr. Test"), ("slot_minutes", "30"),
                      ("active", "1"),
                      ("start_0", "10:00"), ("end_0", "12:00"),
                      ("start_0", "11:00"), ("end_0", "13:00")]))
        after = [(s["weekday"], s["start_time"], s["end_time"])
                 for s in db.get_doctor(self.doctor_id)["schedule"]]
        self.assertEqual(before, after)

    def test_valid_multi_range_schedule_saves(self):
        response = self.post("/admin/doctors/%d" % self.doctor_id,
                             self.doctor_form(pairs=[
                                 ("name_en", "Dr. Test"), ("slot_minutes", "30"),
                                 ("active", "1"),
                                 ("start_0", "09:00"), ("end_0", "11:00"),
                                 ("start_0", "14:00"), ("end_0", "16:00")]))
        self.assertIn(response.status_code, (301, 302))
        saved = sorted((s["weekday"], s["start_time"], s["end_time"])
                       for s in db.get_doctor(self.doctor_id)["schedule"])
        self.assertEqual(saved, [(0, "09:00", "11:00"), (0, "14:00", "16:00")])

    # ── 3. slot generation with several ranges on one day ────────────────────
    def test_slots_from_multiple_ranges_on_one_day(self):
        self.post("/admin/doctors/%d" % self.doctor_id,
                  self.doctor_form(pairs=[
                      ("name_en", "Dr. Test"), ("slot_minutes", "30"),
                      ("active", "1"),
                      ("start_0", "09:00"), ("end_0", "10:00"),
                      ("start_0", "15:00"), ("end_0", "16:00")]))
        slots = db.get_available_slots(self.doctor_id, next_monday().isoformat())
        self.assertEqual(slots, ["09:00", "09:30", "15:00", "15:30"])

    # ── 4. impact confirmation ───────────────────────────────────────────────
    def test_impact_warning_blocks_until_confirmed(self):
        monday = next_monday()
        appt_id = self.make_appointment(monday.isoformat(), "10:30")
        self.addCleanup(db.delete_appointment, appt_id)

        # Removing Monday would strand the appointment.
        response = self.post("/admin/doctors/%d" % self.doctor_id,
                             [("name_en", "Dr. Test"), ("slot_minutes", "30"),
                              ("active", "1")])
        body = response.get_data(as_text=True)
        self.assertIn("This change affects upcoming appointments", body)
        self.assertIn("Catalog Patient", body)
        # Nothing written yet — Monday's range is still there.
        self.assertTrue(any(s["weekday"] == 0
                            for s in db.get_doctor(self.doctor_id)["schedule"]))

        token = token_from(body)
        self.assertTrue(token)
        confirmed = self.post("/admin/confirm-impact",
                              [("impact_token", token)])
        self.assertIn(confirmed.status_code, (301, 302))
        # Now the schedule is empty and the appointment still exists.
        self.assertEqual(db.get_doctor(self.doctor_id)["schedule"], [])
        self.assertIsNotNone(db.get_appointment(appt_id))

    def test_disabling_doctor_removes_from_prompt_and_slots(self):
        monday = next_monday().isoformat()
        db.update_doctor(self.doctor_id, active=1)
        self.assertIn("Dr. Test", prompt_builder.build_system_prompt("en"))
        self.assertTrue(db.get_available_slots(self.doctor_id, monday))

        appt_id = self.make_appointment(monday, "10:00")
        self.addCleanup(db.delete_appointment, appt_id)

        # Toggle off — an impacted appointment forces the confirmation page.
        response = self.post("/admin/doctors/%d/toggle" % self.doctor_id, [])
        body = response.get_data(as_text=True)
        self.assertIn("Disabling", body)
        self.post("/admin/confirm-impact", [("impact_token", token_from(body))])

        self.assertFalse(db.get_doctor(self.doctor_id)["active"])
        self.assertNotIn("Dr. Test", prompt_builder.build_system_prompt("en"))
        self.assertEqual(db.get_available_slots(self.doctor_id, monday), [])

    # ── 5. days off ──────────────────────────────────────────────────────────
    def test_days_off_remove_dates_from_slots(self):
        monday = next_monday()
        self.assertTrue(db.get_available_slots(self.doctor_id, monday.isoformat()))
        response = self.post("/admin/doctors/%d" % self.doctor_id,
                             [("action", "add_time_off"),
                              ("date_from", monday.isoformat()),
                              ("date_to", monday.isoformat()),
                              ("reason", "Conference")])
        self.assertIn(response.status_code, (301, 302))
        self.assertEqual(db.get_available_slots(self.doctor_id, monday.isoformat()), [])
        self.assertTrue(db.is_doctor_off(self.doctor_id, monday.isoformat()))

    def test_days_off_make_the_validator_reject_with_a_clear_reason(self):
        monday = next_monday()
        db.create_time_off(self.doctor_id, monday.isoformat(), monday.isoformat(),
                           "Leave")
        booking, message = app_mod._validate_booking(
            {"patient_name": "X", "phone": "01712345678",
             "doctor_id": self.doctor_id, "date": monday.isoformat(),
             "time": "10:30"}, "en")
        self.assertIsNone(booking)
        self.assertIn("on leave", message)

    def test_time_off_validation(self):
        today = db.dhaka_today()
        # Inverted range.
        response = self.post("/admin/doctors/%d" % self.doctor_id,
                             [("action", "add_time_off"),
                              ("date_from", (today + datetime.timedelta(days=5)).isoformat()),
                              ("date_to", (today + datetime.timedelta(days=2)).isoformat())])
        self.assertIn("on or after the start", response.get_data(as_text=True))
        # In the past.
        response = self.post("/admin/doctors/%d" % self.doctor_id,
                             [("action", "add_time_off"),
                              ("date_from", (today - datetime.timedelta(days=2)).isoformat()),
                              ("date_to", (today - datetime.timedelta(days=1)).isoformat())])
        self.assertIn("past", response.get_data(as_text=True))
        # Longer than 90 days.
        response = self.post("/admin/doctors/%d" % self.doctor_id,
                             [("action", "add_time_off"),
                              ("date_from", (today + datetime.timedelta(days=1)).isoformat()),
                              ("date_to", (today + datetime.timedelta(days=120)).isoformat())])
        self.assertIn("at most 90 days", response.get_data(as_text=True))

    def test_days_off_show_in_the_system_prompt(self):
        monday = next_monday()
        end = (monday + datetime.timedelta(days=2)).isoformat()
        db.create_time_off(self.doctor_id, monday.isoformat(), end, "Conference")
        english = prompt_builder.build_system_prompt("en")
        self.assertIn("Upcoming days off", english)
        self.assertIn("Dr. Test is on leave", english)
        bengali = prompt_builder.build_system_prompt("bn")
        self.assertIn("আসন্ন ছুটি", bengali)

    def test_disabled_doctor_days_off_leave_the_prompt(self):
        """A disabled doctor must not leak in through the days-off line."""
        monday = next_monday()
        db.create_time_off(self.doctor_id, monday.isoformat(),
                           monday.isoformat(), "Leave")
        self.assertIn("Dr. Test is on leave",
                      prompt_builder.build_system_prompt("en"))
        db.update_doctor(self.doctor_id, active=0)
        english = prompt_builder.build_system_prompt("en")
        self.assertNotIn("Dr. Test", english)
        # The days-off section heading is gone (the generic booking rule stays).
        self.assertNotIn("Upcoming days off (next 30 days):", english)

    # ── 6. delete blocked while appointments exist ───────────────────────────
    def test_delete_blocked_when_appointments_exist(self):
        monday = next_monday()
        appt_id = self.make_appointment(monday.isoformat(), "10:00")
        self.addCleanup(db.delete_appointment, appt_id)
        response = self.post("/admin/doctors/%d/delete" % self.doctor_id, [],
                             token=login_owner(self.client))
        self.assertIn(response.status_code, (301, 302))
        self.assertIsNotNone(db.get_doctor(self.doctor_id))
        # The rejection message is flashed to the doctors list on the redirect.
        follow = self.client.get("/admin/doctors")
        self.assertIn("can&#39;t be deleted", follow.get_data(as_text=True))

    def test_delete_allowed_without_appointments(self):
        token = login_owner(self.client)
        response = self.post("/admin/doctors/%d/delete" % self.doctor_id, [],
                             token=token)
        self.assertIn(response.status_code, (301, 302))
        self.assertIsNone(db.get_doctor(self.doctor_id))

    # ── 7. services ──────────────────────────────────────────────────────────
    def test_service_field_validation(self):
        cases = [
            ([("name_en", ""), ("name_bn", ""), ("duration_minutes", "30")],
             "needs a name"),
            ([("name_en", "S"), ("price", "-5"), ("duration_minutes", "30")],
             "negative"),
            ([("name_en", "S"), ("price", "500"), ("price_max", "100"),
              ("duration_minutes", "30")], "greater than or equal"),
            ([("name_en", "S"), ("price", "500"), ("duration_minutes", "3")],
             "between"),
            ([("name_en", "S"), ("price", "500"), ("duration_minutes", "42")],
             "multiple of 5"),
        ]
        for pairs, expected in cases:
            response = self.post("/admin/services/new", pairs)
            self.assertIn(expected, response.get_data(as_text=True), expected)

    def test_service_create_edit_toggle(self):
        token = login_owner(self.client)
        before = {s["name_en"] for s in db.list_services()}
        self.post("/admin/services/new",
                  [("name_en", "Catalog Service"), ("name_bn", "সেবা"),
                   ("price", "500"), ("price_max", "750"),
                   ("duration_minutes", "45"), ("active", "1")], token=token)
        created = [s for s in db.list_services()
                   if s["name_en"] == "Catalog Service"
                   and s["name_en"] not in before]
        self.assertTrue(created)
        service_id = created[0]["id"]
        self.addCleanup(db.delete_service, service_id)

        self.post("/admin/services/%d/toggle" % service_id, [], token=token)
        self.assertFalse(db.get_service(service_id)["active"])

    def test_disabled_service_leaves_the_prompt(self):
        service_id = db.create_service(name_en="Zenith Whitening",
                                       name_bn="", price=1000, price_max=None,
                                       duration_minutes=30, active=1)
        self.addCleanup(db.delete_service, service_id)
        self.assertIn("Zenith Whitening", prompt_builder.build_system_prompt("en"))
        db.update_service(service_id, active=0)
        self.assertNotIn("Zenith Whitening", prompt_builder.build_system_prompt("en"))

    def test_service_delete_blocked_when_used(self):
        service_id = db.create_service(name_en="Used Service", name_bn="",
                                       price=100, price_max=None,
                                       duration_minutes=30, active=1)
        self.addCleanup(db.delete_service, service_id)
        appt_id = db.create_appointment(
            patient_name="User", phone="01711111111", doctor_id=self.doctor_id,
            service_id=service_id, date=next_monday().isoformat(), time="11:00",
            status="pending", language="en")
        self.addCleanup(db.delete_appointment, appt_id)
        token = login_owner(self.client)
        self.post("/admin/services/%d/delete" % service_id, [], token=token)
        self.assertIsNotNone(db.get_service(service_id))

    # ── 8. pages render ──────────────────────────────────────────────────────
    def test_catalog_pages_render(self):
        for path in ("/admin/doctors", "/admin/doctors/new",
                     "/admin/doctors/%d" % self.doctor_id,
                     "/admin/services", "/admin/services/new"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn(b"Traceback", response.data, path)

    def test_view_what_the_bot_sees_link_present(self):
        for path in ("/admin/doctors", "/admin/services"):
            body = self.client.get(path).get_data(as_text=True)
            self.assertIn("/admin/settings#prompt-preview", body, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
