# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for `friday_core.observability.emit` — Design 72's data-plane helper.

Coverage:
- emit() writes one Dispatcher Event row with all fields populated
- emit() auto-derives `project` from `task` when project not given
- emit() coerces payload dict to JSON string
- emit() coerces non-JSON-encodable payload (datetime) without raising
- emit() truncates oversize summary
- emit() with no task and no project still writes (system-level events)
- emit() savepoint-rolls-back on DB error and never raises
- emit() returns None on failure
- emit_skip_deduped() emits the first time, suppresses the second within window
- emit_skip_deduped() emits again after window expires
"""

import datetime
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime


def _clear_events_for_task(task_name: str) -> None:
	frappe.db.sql("DELETE FROM `tabDispatcher Event` WHERE task = %s", (task_name,))
	frappe.db.commit()


def _as_dict(payload):
	"""Frappe auto-parses JSON fields to dict on read; fall back to json.loads for strings."""
	if payload is None:
		return {}
	if isinstance(payload, dict):
		return payload
	import json as _json

	try:
		return _json.loads(payload)
	except Exception:
		return {}


def _ensure_test_task() -> str:
	"""Return a Task name to attach events to — finds or creates one."""
	existing = frappe.get_all("Task", limit=1, pluck="name")
	if existing:
		return existing[0]
	# No tasks on this site — create one minimal task for the test.
	# Tasks usually belong to a Project; create one of those too if needed.
	project = frappe.get_all("Project", limit=1, pluck="name")
	project_name = project[0] if project else None
	if not project_name:
		proj = frappe.get_doc({"doctype": "Project", "project_name": "Test Emit Project"}).insert(
			ignore_permissions=True
		)
		project_name = proj.name
	task = frappe.get_doc(
		{
			"doctype": "Task",
			"title": "Test Emit Task",
			"project": project_name,
			"workflow_state": "Pending",
		}
	).insert(ignore_permissions=True)
	return task.name


class TestEmitBasics(unittest.TestCase):
	"""Happy-path writes."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def setUp(self):
		_clear_events_for_task(self.task_name)

	def test_emit_writes_a_row(self):
		from frappe.friday_core.observability import emit

		name = emit(
			"workflow.state_change",
			task=self.task_name,
			trigger_source="manual_save",
			summary="Test event",
			payload={"from": "Pending", "to": "Assigned"},
		)
		self.assertIsNotNone(name, "emit should return the new row's name on success")
		frappe.db.commit()
		doc = frappe.get_doc("Dispatcher Event", name)
		self.assertEqual(doc.event_type, "workflow.state_change")
		self.assertEqual(doc.trigger_source, "manual_save")
		self.assertEqual(doc.task, self.task_name)
		self.assertEqual(doc.summary, "Test event")
		# JSON field auto-parses to dict on read.
		self.assertEqual(_as_dict(doc.payload_json).get("to"), "Assigned")

	def test_emit_auto_derives_project_from_task(self):
		"""If caller omits project, emit looks it up from the task."""
		from frappe.friday_core.observability import emit

		expected_project = frappe.db.get_value("Task", self.task_name, "project")
		name = emit("runner.start", task=self.task_name, summary="started")
		frappe.db.commit()
		doc = frappe.get_doc("Dispatcher Event", name)
		self.assertEqual(doc.project, expected_project)

	def test_emit_with_no_task_or_project(self):
		"""System-level events (reconciler.tick) have no task."""
		from frappe.friday_core.observability import emit

		name = emit(
			"reconciler.tick",
			trigger_source="scheduler",
			summary="tick complete",
			payload={"transient_blocked": 0, "stale_executing": 0},
		)
		self.assertIsNotNone(name)
		frappe.db.commit()
		doc = frappe.get_doc("Dispatcher Event", name)
		self.assertIsNone(doc.task or None)
		self.assertEqual(doc.event_type, "reconciler.tick")
		# Clean up — this row has no task to use _clear_events_for_task on.
		frappe.delete_doc("Dispatcher Event", name, force=True)
		frappe.db.commit()

	def test_emit_truncates_oversize_summary(self):
		from frappe.friday_core.observability import emit

		long_text = "x" * 5000
		name = emit("runner.error", task=self.task_name, summary=long_text)
		frappe.db.commit()
		doc = frappe.get_doc("Dispatcher Event", name)
		self.assertLessEqual(len(doc.summary or ""), 1000)
		self.assertTrue((doc.summary or "").endswith("..."))


class TestEmitPayloadCoercion(unittest.TestCase):
	"""Payload should JSON-encode, surviving non-encodable values."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def setUp(self):
		_clear_events_for_task(self.task_name)

	def test_payload_dict_becomes_json_string(self):
		from frappe.friday_core.observability import emit

		name = emit(
			"llm.call_summary",
			task=self.task_name,
			payload={"model": "minimax-m3", "tokens": 1234},
		)
		frappe.db.commit()
		doc = frappe.get_doc("Dispatcher Event", name)
		payload = _as_dict(doc.payload_json)
		self.assertEqual(payload.get("model"), "minimax-m3")
		self.assertEqual(payload.get("tokens"), 1234)

	def test_payload_with_datetime_does_not_raise(self):
		"""datetime objects in payload should coerce via default=str — no exception."""
		from frappe.friday_core.observability import emit

		name = emit(
			"workflow.state_change",
			task=self.task_name,
			payload={"when": datetime.datetime(2026, 6, 14, 12, 0, 0)},
		)
		self.assertIsNotNone(name)
		frappe.db.commit()
		doc = frappe.get_doc("Dispatcher Event", name)
		payload = _as_dict(doc.payload_json)
		# default=str coerces datetime -> ISO string like "2026-06-14 12:00:00"
		self.assertIn("2026", str(payload.get("when") or ""))


class TestEmitSafetyContract(unittest.TestCase):
	"""emit() never raises and never poisons the surrounding transaction."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def test_emit_never_raises_on_db_error(self):
		"""Force the insert path to fail; emit returns None and does not raise."""
		from frappe.friday_core.observability import emit

		with patch("frappe.get_doc") as mock_get_doc:
			mock_get_doc.side_effect = RuntimeError("simulated DB explosion")
			# Must not raise.
			result = emit("runner.start", task=self.task_name, summary="will fail")
			self.assertIsNone(result)

	def test_emit_failure_does_not_poison_outer_transaction(self):
		"""After a failing emit, the caller can still do DB work in the same transaction."""
		from frappe.friday_core.observability import emit

		with patch("frappe.get_doc") as mock_get_doc:
			mock_get_doc.side_effect = RuntimeError("simulated explosion")
			emit("runner.start", task=self.task_name, summary="will fail")

		# After the savepoint rollback, the outer transaction must still be usable.
		# A direct read here would fail with InFailedSqlTransaction if the savepoint
		# rollback didn't happen.
		val = frappe.db.get_value("Task", self.task_name, "title")
		self.assertIsNotNone(val)


class TestEmitSkipDeduped(unittest.TestCase):
	"""dispatcher.skip dedup window — keeps a busy tick from flooding the table."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def setUp(self):
		_clear_events_for_task(self.task_name)

	def test_first_emit_writes_row(self):
		from frappe.friday_core.observability.emit import emit_skip_deduped

		name = emit_skip_deduped(self.task_name, "no_profile_match", window_seconds=60)
		frappe.db.commit()
		self.assertIsNotNone(name)
		doc = frappe.get_doc("Dispatcher Event", name)
		self.assertEqual(doc.event_type, "dispatcher.skip")
		self.assertEqual(doc.trigger_source, "no_profile_match")

	def test_second_emit_within_window_is_suppressed(self):
		from frappe.friday_core.observability.emit import emit_skip_deduped

		first = emit_skip_deduped(self.task_name, "no_profile_match", window_seconds=60)
		frappe.db.commit()
		self.assertIsNotNone(first)
		# Same task + same reason within window → suppressed.
		second = emit_skip_deduped(self.task_name, "no_profile_match", window_seconds=60)
		self.assertIsNone(second)
		# Only one row should exist.
		count = frappe.db.count(
			"Dispatcher Event",
			{"task": self.task_name, "trigger_source": "no_profile_match"},
		)
		self.assertEqual(count, 1)

	def test_different_reason_is_not_suppressed(self):
		"""Same task, different reason → both rows."""
		from frappe.friday_core.observability.emit import emit_skip_deduped

		a = emit_skip_deduped(self.task_name, "no_profile_match", window_seconds=60)
		frappe.db.commit()
		b = emit_skip_deduped(self.task_name, "concurrency_cap", window_seconds=60)
		frappe.db.commit()
		self.assertIsNotNone(a)
		self.assertIsNotNone(b)

	def test_emit_again_after_window_expires(self):
		"""Simulate window expiry by clearing the older row and re-emitting."""
		from frappe.friday_core.observability.emit import emit_skip_deduped

		first = emit_skip_deduped(self.task_name, "role_gate", window_seconds=60)
		frappe.db.commit()
		# Backdate the row beyond the window to simulate time passing.
		old_creation = add_to_date(now_datetime(), seconds=-120)
		frappe.db.sql(
			"UPDATE `tabDispatcher Event` SET creation = %s WHERE name = %s",
			(old_creation, first),
		)
		frappe.db.commit()
		second = emit_skip_deduped(self.task_name, "role_gate", window_seconds=60)
		self.assertIsNotNone(second, "expired dedup row should allow a fresh emit")
