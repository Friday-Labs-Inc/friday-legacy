# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for the per-project conversation channel (Design 73, Slice 1).

Coverage:
- Creating a Project auto-provisions a linked Raven channel (the after_insert hook).
- provision is idempotent — a second call returns the same channel, makes no new one.
- the channel carries Raven's native back-link (linked_doctype/linked_document).
- the Friday bot is a member of the channel.
- provision is graceful when Raven is not installed (returns None, never raises).
- archive_project_channel sets is_archived.
- on_project_update archives the channel on Completed/Cancelled, not other statuses.
- the hook is best-effort: a failure inside provisioning never breaks the Project save.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe


def _raven_installed() -> bool:
	return bool(frappe.db.table_exists("Raven Channel"))


def _make_project(name: str, backend_ref: str | None = None) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Project",
			"project_name": name,
			"status": "Open",
			"backend_ref": backend_ref,
		}
	).insert(ignore_permissions=True)
	return doc.name


def _cleanup_project(project_name: str) -> None:
	"""Delete the project + any channel it provisioned (and the channel's rows)."""
	ch = (
		frappe.db.get_value("Project", project_name, "conversation_channel")
		if frappe.db.exists("Project", project_name)
		else None
	)
	if ch and frappe.db.exists("Raven Channel", ch):
		frappe.db.sql("DELETE FROM `tabRaven Message` WHERE channel_id = %s", (ch,))
		frappe.db.sql("DELETE FROM `tabRaven Channel Member` WHERE channel_id = %s", (ch,))
		frappe.delete_doc("Raven Channel", ch, force=True, ignore_permissions=True)
	if frappe.db.exists("Project", project_name):
		frappe.delete_doc("Project", project_name, force=True, ignore_permissions=True)
	frappe.db.commit()


@unittest.skipUnless(_raven_installed(), "Raven not installed on this site")
class TestProjectChannelProvision(unittest.TestCase):
	"""Channel auto-provisioning on project creation."""

	def setUp(self):
		self.pname = "D73 Test Project Alpha"
		_cleanup_project(self.pname)

	def tearDown(self):
		_cleanup_project(self.pname)

	def test_creating_project_provisions_linked_channel(self):
		project = _make_project(self.pname, backend_ref="D73-ALPHA")
		frappe.db.commit()

		channel = frappe.db.get_value("Project", project, "conversation_channel")
		self.assertTrue(channel, "Project should be linked to a conversation channel")
		self.assertTrue(frappe.db.exists("Raven Channel", channel))

		# Raven's native back-link points to this project.
		linked = frappe.db.get_value(
			"Raven Channel", channel, ["linked_doctype", "linked_document"], as_dict=True
		)
		self.assertEqual(linked["linked_doctype"], "Project")
		self.assertEqual(linked["linked_document"], project)

	def test_channel_name_uses_backend_ref(self):
		project = _make_project(self.pname, backend_ref="D73-ALPHA")
		frappe.db.commit()
		channel = frappe.db.get_value("Project", project, "conversation_channel")
		# Raven lowercases + hyphenates; slug is proj-<backend_ref>.
		cname = frappe.db.get_value("Raven Channel", channel, "channel_name")
		self.assertIn("d73-alpha", cname)

	def test_bot_is_a_member(self):
		from frappe.friday_core.surfaces.raven_adapter import FRIDAY_BOT_NAME

		project = _make_project(self.pname, backend_ref="D73-ALPHA")
		frappe.db.commit()
		channel = frappe.db.get_value("Project", project, "conversation_channel")
		bot_user = frappe.db.get_value("Raven Bot", FRIDAY_BOT_NAME, "raven_user")
		if bot_user:  # bot only exists if the Raven surface was bootstrapped
			self.assertTrue(
				frappe.db.exists("Raven Channel Member", {"channel_id": channel, "user_id": bot_user})
			)

	def test_provision_is_idempotent(self):
		from frappe.friday_core.conversation.project_channel import provision_project_channel

		project = _make_project(self.pname, backend_ref="D73-ALPHA")
		frappe.db.commit()
		first = frappe.db.get_value("Project", project, "conversation_channel")
		# Call again explicitly — must return the same channel, create no new one.
		second = provision_project_channel(project)
		self.assertEqual(first, second)
		count = frappe.db.count("Raven Channel", {"linked_document": project})
		self.assertEqual(count, 1)


@unittest.skipUnless(_raven_installed(), "Raven not installed on this site")
class TestProjectChannelArchive(unittest.TestCase):
	"""Channel lifecycle mirrors project status."""

	def setUp(self):
		self.pname = "D73 Test Project Beta"
		_cleanup_project(self.pname)

	def tearDown(self):
		_cleanup_project(self.pname)

	def test_completing_project_archives_channel(self):
		project = _make_project(self.pname, backend_ref="D73-BETA")
		frappe.db.commit()
		channel = frappe.db.get_value("Project", project, "conversation_channel")
		self.assertFalse(frappe.db.get_value("Raven Channel", channel, "is_archived"))

		doc = frappe.get_doc("Project", project)
		doc.status = "Completed"
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertTrue(frappe.db.get_value("Raven Channel", channel, "is_archived"))

	def test_on_hold_does_not_archive(self):
		project = _make_project(self.pname, backend_ref="D73-BETA")
		frappe.db.commit()
		channel = frappe.db.get_value("Project", project, "conversation_channel")

		doc = frappe.get_doc("Project", project)
		doc.status = "On Hold"
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertFalse(frappe.db.get_value("Raven Channel", channel, "is_archived"))


class TestProjectChannelGraceful(unittest.TestCase):
	"""Best-effort contract: never break a Project save."""

	def test_returns_none_when_raven_not_installed(self):
		from frappe.friday_core.conversation.project_channel import provision_project_channel

		with patch("frappe.db.table_exists", return_value=False):
			# Must not raise; returns None.
			self.assertIsNone(provision_project_channel("any-project"))

	def test_provision_failure_does_not_break_project_insert(self):
		"""If provisioning blows up, the Project must still be created."""
		pname = "D73 Test Project Gamma"
		_cleanup_project(pname)
		try:
			with patch(
				"frappe.friday_core.conversation.project_channel.provision_project_channel",
				side_effect=RuntimeError("simulated Raven explosion"),
			):
				# The after_insert hook calls provision via _guard — the error
				# must be swallowed and the Project must persist.
				project = _make_project(pname)
				frappe.db.commit()
				self.assertTrue(frappe.db.exists("Project", project))
		finally:
			_cleanup_project(pname)
