"""Design 99 — actor context is a framework primitive.

Who acted (human / agent / system) is set once per context, stamped on every
non-child row the framework writes, carried into background jobs, restored
after `acting_as`, and turned into an Agent Write Log row for every document an
AGENT writes — with or without the skill dispatcher in the loop.
"""

import unittest
from unittest.mock import patch

import frappe

PROFILE = "FRIDAY-ACTOR-TEST-PROFILE"


def _ensure_profile():
	if not frappe.db.exists("Agent Profile", PROFILE):
		frappe.get_doc(
			{
				"doctype": "Agent Profile",
				"profile_name": PROFILE,
				"agent_role": "Specialist",
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	return frappe.get_doc("Agent Profile", PROFILE)


class TestActorPrimitive(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.set_actor("human")

	def test_default_actor_is_human_for_a_user(self):
		self.assertEqual(frappe.get_actor().kind, "human")
		self.assertTrue(frappe.get_actor().trace_id)

	def test_set_actor_rejects_unknown_kind(self):
		with self.assertRaises(ValueError):
			frappe.set_actor("robot")

	def test_acting_as_restores_user_and_actor_even_on_error(self):
		before_user, before_actor = frappe.session.user, dict(frappe.get_actor())
		with self.assertRaises(RuntimeError):
			with frappe.acting_as("Guest", kind="agent", id="X"):
				self.assertEqual(frappe.get_actor().kind, "agent")
				self.assertEqual(frappe.get_actor().id, "X")
				raise RuntimeError("boom")
		self.assertEqual(frappe.session.user, before_user)
		self.assertEqual(dict(frappe.get_actor()), before_actor)

	def test_trace_id_survives_user_switch(self):
		trace = frappe.get_actor().trace_id
		with frappe.acting_as("Guest", kind="system"):
			self.assertEqual(frappe.get_actor().trace_id, trace)

	def test_agent_user_resolves_to_agent_actor(self):
		profile = _ensure_profile()
		user = profile.frappe_user
		self.assertTrue(user, "identity provisioning must give the profile a User")
		frappe.cache().hdel("friday:agent_users", user)  # force the DB path once
		with frappe.acting_as(user):  # kind not given: the resolve_actor hook decides
			self.assertEqual(frappe.get_actor().kind, "agent")
			self.assertEqual(frappe.get_actor().id, PROFILE)

	def test_rows_are_stamped_with_the_actor(self):
		profile = _ensure_profile()
		with frappe.acting_as(profile.frappe_user, kind="agent", id=PROFILE, trace_id="trace-stamp-1"):
			note = frappe.get_doc({"doctype": "Note", "title": "actor stamp", "content": "x"}).insert(
				ignore_permissions=True
			)
		row = frappe.db.get_value("Note", note.name, ["_actor_kind", "_actor", "_trace_id"], as_dict=True)
		self.assertEqual((row._actor_kind, row._actor, row._trace_id), ("agent", PROFILE, "trace-stamp-1"))
		frappe.delete_doc("Note", note.name, force=True, ignore_permissions=True)

	def test_agent_write_is_audited_without_the_dispatcher(self):
		profile = _ensure_profile()
		with frappe.acting_as(profile.frappe_user, kind="agent", id=PROFILE, trace_id="trace-audit-1"):
			note = frappe.get_doc({"doctype": "Note", "title": "audited", "content": "y"}).insert(
				ignore_permissions=True
			)
		rows = frappe.get_all(
			"Agent Write Log",
			filters={"ref_doctype": "Note", "ref_name": note.name},
			fields=["action", "actor", "trace_id", "user"],
		)
		self.assertEqual(len(rows), 1, "one audit row per agent write")
		self.assertEqual(
			(rows[0].action, rows[0].actor, rows[0].trace_id, rows[0].user),
			("save", PROFILE, "trace-audit-1", profile.frappe_user),
		)
		frappe.delete_doc("Note", note.name, force=True, ignore_permissions=True)

	def test_human_write_is_not_audited(self):
		before = frappe.db.count("Agent Write Log")
		note = frappe.get_doc({"doctype": "Note", "title": "human", "content": "z"}).insert(
			ignore_permissions=True
		)
		self.assertEqual(frappe.db.count("Agent Write Log"), before)
		frappe.delete_doc("Note", note.name, force=True, ignore_permissions=True)

	def test_job_carries_the_actor(self):
		frappe.set_actor("agent", PROFILE, "trace-job-1")
		captured = {}

		def fake_enqueue_call(*args, **kwargs):
			captured.update(kwargs.get("kwargs") or {})

			class J:
				id = "j"

			return J()

		with patch("frappe.utils.background_jobs.get_queue") as gq:
			gq.return_value.enqueue_call.side_effect = fake_enqueue_call
			frappe.enqueue("frappe.ping", now=False)
		self.assertEqual(captured.get("actor", {}).get("id"), PROFILE)
		self.assertEqual(captured.get("actor", {}).get("trace_id"), "trace-job-1")
