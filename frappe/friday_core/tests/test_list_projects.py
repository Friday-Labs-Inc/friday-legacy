# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for the list-projects skill (the 'what projects exist?' verb). Mock-based.

NOTE: the handler-level tests below can't prove the skill actually LOADS for an
agent (the lesson from session_search — a green handler test hid a loader-gate
drop). That is verified separately on the real loader path; see the PR's live
check (load_for_profile includes 'list-projects', riding the Project Commander
role's Project:read grant).
"""

import unittest
from unittest.mock import patch

_HP = "frappe.friday_core.skills.handlers_project"


class TestListProjects(unittest.TestCase):
	@patch(f"{_HP}.frappe")
	def test_lists_projects_formatted(self, fr):
		from frappe.friday_core.skills import handlers_project as h

		fr.get_all.return_value = [
			{
				"name": "PRJ-1",
				"status": "Open",
				"priority": "high",
				"percent_complete": 40,
				"total_tasks": 5,
				"completed_tasks": 2,
				"project_lead_profile": "Friday",
			}
		]
		res = h.list_projects("list-projects", {})
		self.assertEqual(res["count"], 1)
		self.assertIn("PRJ-1", res["result"])
		self.assertIn("Open", res["result"])
		self.assertIn("2/5 tasks", res["result"])
		# No status param → unfiltered query.
		self.assertIsNone(fr.get_all.call_args.kwargs["filters"])

	@patch(f"{_HP}.frappe")
	def test_status_filter_is_passed(self, fr):
		from frappe.friday_core.skills import handlers_project as h

		fr.get_all.return_value = []
		res = h.list_projects("list-projects", {"status": "Completed"})
		self.assertEqual(fr.get_all.call_args.kwargs["filters"], {"status": "Completed"})
		self.assertEqual(res["count"], 0)
		self.assertIn("Completed", res["result"])


if __name__ == "__main__":
	unittest.main()
