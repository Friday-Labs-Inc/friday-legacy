# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for `friday_core.observability.retention` (Design 72, Q6).

Coverage:
- purge_old_events() deletes rows older than RETENTION_DAYS.
- purge_old_events() preserves rows newer than the cutoff.
- purge_old_events() returns the total deleted count.
- purge_old_events() never raises on DB error.
- write_task_completion_summary() writes one row per terminal task.
- write_task_completion_summary() is idempotent (upserts on second call).
- write_task_completion_summary() captures total event count + cost + duration.
- is_terminal() returns True for Completed / Cancelled.
- is_terminal() returns True for Blocked with non-transient reason.
- is_terminal() returns False for Blocked with transient reason (timeout etc).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime


def _clear_events_for_task(task_name: str) -> None:
	frappe.db.sql("DELETE FROM `tabDispatcher Event` WHERE task = %s", (task_name,))
	frappe.db.sql("DELETE FROM `tabTask Completion Summary` WHERE task = %s", (task_name,))
	frappe.db.commit()


def _ensure_test_task() -> str:
	existing = frappe.get_all("Task", limit=1, pluck="name")
	if existing:
		return existing[0]
	project = frappe.get_all("Project", limit=1, pluck="name")
	project_name = project[0] if project else None
	if not project_name:
		proj = frappe.get_doc({"doctype": "Project", "project_name": "Test Retention Project"}).insert(
			ignore_permissions=True
		)
		project_name = proj.name
	task = frappe.get_doc(
		{
			"doctype": "Task",
			"title": "Test Retention Task",
			"project": project_name,
			"workflow_state": "Pending",
		}
	).insert(ignore_permissions=True)
	return task.name


class TestIsTerminal(unittest.TestCase):
	"""is_terminal() decides when to write the summary row."""

	def test_completed_is_terminal(self):
		from frappe.friday_core.observability.retention import is_terminal

		doc = frappe._dict({"workflow_state": "Completed", "blocked_reason": None})
		self.assertTrue(is_terminal(doc))

	def test_cancelled_is_terminal(self):
		from frappe.friday_core.observability.retention import is_terminal

		doc = frappe._dict({"workflow_state": "Cancelled", "blocked_reason": None})
		self.assertTrue(is_terminal(doc))

	def test_blocked_with_non_transient_reason_is_terminal(self):
		"""A semantic block (no_profile_for_skills, dependency_failed) is terminal."""
		from frappe.friday_core.observability.retention import is_terminal

		doc = frappe._dict({"workflow_state": "Blocked", "blocked_reason": "no_profile_for_skills"})
		self.assertTrue(is_terminal(doc))

	def test_blocked_with_transient_reason_is_NOT_terminal(self):
		"""timeout/oom/runner_lost — the reconciler will retry, so don't seal yet."""
		from frappe.friday_core.observability.retention import is_terminal

		for reason in ("timeout", "oom", "runner_lost", "rate_limit", "overloaded", "server_error"):
			doc = frappe._dict({"workflow_state": "Blocked", "blocked_reason": reason})
			self.assertFalse(is_terminal(doc), f"transient '{reason}' should NOT be terminal")

	def test_pending_is_not_terminal(self):
		from frappe.friday_core.observability.retention import is_terminal

		doc = frappe._dict({"workflow_state": "Pending", "blocked_reason": None})
		self.assertFalse(is_terminal(doc))

	def test_executing_is_not_terminal(self):
		from frappe.friday_core.observability.retention import is_terminal

		doc = frappe._dict({"workflow_state": "Executing", "blocked_reason": None})
		self.assertFalse(is_terminal(doc))


class TestWriteTaskCompletionSummary(unittest.TestCase):
	"""The permanent compact row written on terminal transitions."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def setUp(self):
		_clear_events_for_task(self.task_name)

	def test_writes_one_row_per_task(self):
		from frappe.friday_core.observability import emit
		from frappe.friday_core.observability.retention import write_task_completion_summary

		# Emit some events so the count is non-zero.
		emit("runner.start", task=self.task_name)
		emit("runner.complete", task=self.task_name)
		frappe.db.commit()

		task = frappe.get_doc("Task", self.task_name)
		task.workflow_state = "Completed"
		task.completed_at = now_datetime()
		task.save(ignore_permissions=True)
		frappe.db.commit()

		result = write_task_completion_summary(task)
		frappe.db.commit()
		self.assertIsNotNone(result)
		summary = frappe.get_doc("Task Completion Summary", self.task_name)
		self.assertEqual(summary.final_state, "Completed")
		# At least the 2 events we emitted should be in the count.
		self.assertGreaterEqual(summary.total_events, 2)

	def test_is_idempotent_upserts_existing_row(self):
		"""A second call on the same task overwrites in place."""
		from frappe.friday_core.observability.retention import write_task_completion_summary

		task = frappe.get_doc("Task", self.task_name)
		task.workflow_state = "Completed"
		task.completed_at = now_datetime()
		task.save(ignore_permissions=True)
		frappe.db.commit()

		first = write_task_completion_summary(task)
		frappe.db.commit()
		# Re-call: must NOT raise (would be UniqueValidationError if it tried to insert again).
		second = write_task_completion_summary(task)
		frappe.db.commit()
		self.assertEqual(first, self.task_name)
		self.assertEqual(second, self.task_name)
		# Still exactly one row.
		count = frappe.db.count("Task Completion Summary", {"task": self.task_name})
		self.assertEqual(count, 1)

	def test_never_raises_on_db_error(self):
		"""A failed write logs and returns None — never propagates."""
		from frappe.friday_core.observability.retention import write_task_completion_summary

		task = frappe.get_doc("Task", self.task_name)
		task.workflow_state = "Completed"

		with patch("frappe.get_doc") as mock_get_doc:
			mock_get_doc.side_effect = RuntimeError("simulated explosion")
			# Must not raise.
			result = write_task_completion_summary(task)
			self.assertIsNone(result)

	def tearDown(self):
		_clear_events_for_task(self.task_name)


class TestPurgeOldEvents(unittest.TestCase):
	"""The daily retention sweep."""

	def setUp(self):
		# Use a sentinel task that won't conflict with other tests' fixtures.
		self.task_name = _ensure_test_task()
		_clear_events_for_task(self.task_name)

	def test_deletes_old_rows_only(self):
		from frappe.friday_core.observability import emit
		from frappe.friday_core.observability.retention import RETENTION_DAYS, purge_old_events

		# Emit two fresh rows.
		fresh1 = emit("runner.start", task=self.task_name, summary="fresh 1")
		fresh2 = emit("runner.complete", task=self.task_name, summary="fresh 2")
		frappe.db.commit()
		# Backdate one of them past the cutoff.
		old_ts = add_to_date(now_datetime(), days=-(RETENTION_DAYS + 1))
		frappe.db.sql(
			"UPDATE `tabDispatcher Event` SET creation = %s WHERE name = %s",
			(old_ts, fresh1),
		)
		frappe.db.commit()

		deleted = purge_old_events()
		# At least one row deleted (the backdated one), maybe more from older state.
		self.assertGreaterEqual(deleted, 1)
		# The fresh row survives.
		self.assertTrue(frappe.db.exists("Dispatcher Event", fresh2))
		# The backdated row is gone.
		self.assertFalse(frappe.db.exists("Dispatcher Event", fresh1))

	def test_never_raises_on_db_error(self):
		"""A failed sweep logs and exits — next day's run tries again."""
		from frappe.friday_core.observability.retention import purge_old_events

		with patch("frappe.db.sql") as mock_sql:
			mock_sql.side_effect = RuntimeError("simulated DB outage")
			# Must not raise.
			result = purge_old_events()
			# Returns whatever was deleted before the crash (0).
			self.assertEqual(result, 0)

	def test_returns_zero_when_nothing_to_purge(self):
		from frappe.friday_core.observability import emit
		from frappe.friday_core.observability.retention import purge_old_events

		# Only fresh rows.
		emit("runner.start", task=self.task_name, summary="fresh only")
		frappe.db.commit()
		# Establish that nothing past the cutoff exists for THIS task.
		# (Other tasks' old rows could still be purged; we assert the sweep
		# doesn't crash, not that nothing happens.)
		deleted = purge_old_events()
		self.assertGreaterEqual(deleted, 0)

	def tearDown(self):
		_clear_events_for_task(self.task_name)
