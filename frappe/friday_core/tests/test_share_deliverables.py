# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for the share-deliverables skill (Design 73, use-case #5).

Mock-based — no DB, no Raven needed. Pins the contract:
  - posts the project PACKAGE (deliverable-* Files on the Project) into the
    conversation's channel, as the Friday bot, one file message each;
  - takes the project from the SESSION's channel, never a free parameter
    (no cross-project posting);
  - no deliverables -> a plain "nothing to share", never a fabricated file;
  - skips files with no shareable URL;
  - refuses politely outside a project channel.
"""

import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.skills import handlers_deliverables as hd

_H = "frappe.friday_core.skills.handlers_deliverables"
_MEM = "frappe.friday_core.llm.memory.project_for_session"


class TestShareDeliverables(unittest.TestCase):
	@patch("frappe.friday_core.gateway.mirror.mirror_to_session")
	@patch(_MEM, return_value="Northwind")
	@patch(f"{_H}.frappe")
	def test_posts_package_files_as_bot(self, mock_frappe, _pfs, mock_mirror):
		mock_frappe.flags.get.return_value = {"session_id": "CH-1"}
		mock_frappe.db.exists.return_value = True
		mock_frappe.get_all.return_value = [
			{
				"name": "f1",
				"file_name": "deliverable-pkg.pdf",
				"file_url": "/private/files/deliverable-pkg.pdf",
			},
			{
				"name": "f2",
				"file_name": "deliverable-pkg.md",
				"file_url": "/private/files/deliverable-pkg.md",
			},
		]
		bot = MagicMock()
		mock_frappe.get_doc.return_value = bot

		out = hd.share_deliverables("share-deliverables", {})

		self.assertEqual(bot.send_message.call_count, 2)
		bot.send_message.assert_any_call(channel_id="CH-1", file="/private/files/deliverable-pkg.pdf")
		bot.send_message.assert_any_call(channel_id="CH-1", file="/private/files/deliverable-pkg.md")
		self.assertEqual(out["posted_files"], ["deliverable-pkg.pdf", "deliverable-pkg.md"])
		self.assertIn("2", out["result"])
		self.assertEqual(out["project"], "Northwind")
		# The out-of-band file send is mirrored into the session transcript so the
		# agent's own history records what it shared.
		mock_mirror.assert_called_once()
		self.assertEqual(mock_mirror.call_args[0][0], "CH-1")
		self.assertIn("2 deliverable file(s)", mock_mirror.call_args[0][1])

	@patch(_MEM, return_value="Northwind")
	@patch(f"{_H}.frappe")
	def test_no_deliverables_is_honest_and_posts_nothing(self, mock_frappe, _pfs):
		mock_frappe.flags.get.return_value = {"session_id": "CH-1"}
		mock_frappe.db.exists.return_value = True
		mock_frappe.get_all.return_value = []

		out = hd.share_deliverables("share-deliverables", {})

		self.assertEqual(out["posted_files"], [])
		self.assertIn("nothing to share", out["result"].lower())
		mock_frappe.get_doc.assert_not_called()  # the bot is never fetched

	@patch("frappe.friday_core.gateway.mirror.mirror_to_session")
	@patch(_MEM, return_value="Northwind")
	@patch(f"{_H}.frappe")
	def test_skips_files_without_url(self, mock_frappe, _pfs, _mirror):
		mock_frappe.flags.get.return_value = {"session_id": "CH-1"}
		mock_frappe.db.exists.return_value = True
		mock_frappe.get_all.return_value = [
			{"name": "f1", "file_name": "deliverable-pkg.pdf", "file_url": None},
			{
				"name": "f2",
				"file_name": "deliverable-pkg.md",
				"file_url": "/private/files/deliverable-pkg.md",
			},
		]
		bot = MagicMock()
		mock_frappe.get_doc.return_value = bot

		out = hd.share_deliverables("share-deliverables", {})

		self.assertEqual(bot.send_message.call_count, 1)
		self.assertEqual(out["posted_files"], ["deliverable-pkg.md"])

	@patch(f"{_H}.frappe")
	def test_outside_a_channel_raises(self, mock_frappe):
		mock_frappe.flags.get.return_value = {"session_id": ""}
		with self.assertRaises(ValueError):
			hd.share_deliverables("share-deliverables", {})

	@patch(_MEM, return_value=None)
	@patch(f"{_H}.frappe")
	def test_channel_without_project_raises(self, mock_frappe, _pfs):
		mock_frappe.flags.get.return_value = {"session_id": "CH-1"}
		mock_frappe.db.exists.return_value = True  # Raven Channel exists...
		with self.assertRaises(ValueError):  # ...but it's not linked to a project
			hd.share_deliverables("share-deliverables", {})


if __name__ == "__main__":
	unittest.main()
