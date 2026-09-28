# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for interrupt cascade — /stop stops delegated work too (Design 85, LOCKED).

Tests-first. Mock-based — no DB, no redis, no worker.

What they pin (the LOCKED decisions):
  Q1 — collect the active delegated subtree: roots by originating_session, then
       descendants by parent_task; only _ACTIVE_STATES; terminal tasks excluded.
  Q2 — cascade sets the cooperative interrupt flag on each running node's
       task:: session; not-yet-running nodes are cancelled directly.
  Q3 — run_turn RAISES TurnInterrupted; the chat pipeline writes a clean
       "(interrupted by operator)" outbound; the Task path marks the child
       Cancelled / blocked_reason "interrupted" — never Completed.
  Q5 — /stop reports the breadth (N delegated tasks).
"""

import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.agent_runner.exceptions import TurnInterrupted

_I = "frappe.friday_core.gateway.interrupt"
_C = "frappe.friday_core.gateway.commands"
_SVC = "frappe.friday_core.gateway.service"
_TR = "frappe.friday_core.tasks.runner"


class TestSubtreeCollection(unittest.TestCase):
	@patch(f"{_I}.frappe")
	def test_collects_roots_and_descendants_active_only(self, fr):
		from frappe.friday_core.gateway import interrupt

		def get_all(doctype, filters=None, pluck=None, **kw):
			# active-state filter is always applied
			assert filters["workflow_state"][0] == "in"
			if filters.get("originating_session") == "CH":
				return ["T1"]
			if filters.get("parent_task") == "T1":
				return ["T2"]
			return []

		fr.get_all.side_effect = get_all
		names = interrupt.collect_active_subtree("CH")
		self.assertEqual(set(names), {"T1", "T2"})

	@patch(f"{_I}.frappe")
	def test_empty_when_no_delegation(self, fr):
		from frappe.friday_core.gateway import interrupt

		fr.get_all.return_value = []
		self.assertEqual(interrupt.collect_active_subtree("CH"), [])


class TestCascade(unittest.TestCase):
	@patch(f"{_I}._cancel_task")
	@patch(f"{_I}.request_interrupt")
	@patch(f"{_I}.collect_active_subtree", return_value=["T1", "T2"])
	@patch(f"{_I}.frappe")
	def test_flags_running_cancels_pending(self, fr, _subtree, req, cancel):
		from frappe.friday_core.gateway import interrupt

		fr.db.get_value.side_effect = lambda dt, n, f: {"T1": "Executing", "T2": "Pending"}[n]
		n = interrupt.cascade_interrupt("CH")

		self.assertEqual(n, 2)
		# Every node gets the cooperative flag on its task:: session.
		req.assert_any_call("task::T1")
		req.assert_any_call("task::T2")
		# Only the not-running node is cancelled directly.
		cancel.assert_called_once_with("T2")


class TestChatPipelineInterrupted(unittest.TestCase):
	def test_pipeline_writes_clean_interrupted_outbound(self):
		from frappe.friday_core.gateway import service

		inbound = MagicMock()
		inbound.name = "CM-1"
		inbound.session_id = "S"
		inbound.agent_profile = "P"
		inbound.platform = "raven"
		inbound.content = "hi"
		inbound.retry_count = 0

		lock = MagicMock()
		lock.acquire.return_value = True
		with (
			patch(f"{_SVC}.frappe") as fr,
			patch(f"{_SVC}.flush_batch", return_value=["hi"]),
			patch(f"{_SVC}.run_turn", side_effect=TurnInterrupted("(interrupted by operator)")),
			patch(f"{_SVC}._write_outbound") as write_out,
			patch(f"{_SVC}._mark_processed") as mark,
			patch(f"{_SVC}._drain_next_in_session"),
		):
			fr.cache.return_value.lock.return_value = lock
			fr.db.get_value.return_value = 0  # not already processed
			service._run_pipeline(inbound)

		# A clean operator-interrupt reply, NOT the generic "(agent error)".
		kwargs = write_out.call_args.kwargs
		self.assertIn("interrupted", kwargs["content"].lower())
		self.assertNotIn("error", kwargs["content"].lower())
		# Processed with no failure/retry bump (interrupt is not a failure).
		self.assertTrue(mark.called)


class TestTaskPathInterrupted(unittest.TestCase):
	def _run_interrupted(self, force_killed_by):
		"""Run _run_task_agentic with run_turn raising TurnInterrupted.

		`force_killed_by` is what `frappe.db.get_value("Task", …, "force_killed_by")`
		returns — None for a plain cascade `/stop`, a user for `/stop force`.
		"""
		from frappe.friday_core.tasks import runner

		task = MagicMock()
		task.name = "T2"
		task.title = "do thing"
		task.description = "..."
		with (
			patch(f"{_TR}.frappe") as fr,
			patch(f"{_TR}.emit"),
			patch(f"{_TR}.now_datetime"),
			patch(f"{_TR}._elapsed_ms", return_value=10),
			patch("frappe.friday_core.tasks.rollup.task_cost_from_usage", return_value=None),
			patch(
				"frappe.friday_core.agent_runner.runner.run_turn",
				side_effect=TurnInterrupted("(interrupted by operator)"),
			),
		):
			fr.db.get_value.return_value = force_killed_by
			runner._run_task_agentic(task, "P")
		return task

	def test_plain_interrupt_marks_cancelled(self):
		# Design 85 — a cascade /stop with no force-kill → Cancelled.
		task = self._run_interrupted(force_killed_by=None)
		self.assertEqual(task.workflow_state, "Cancelled")
		self.assertEqual(task.blocked_reason, "interrupted")

	def test_force_killed_task_is_not_downgraded_to_cancelled(self):
		# Design 83b — the kill path already set ForceKilled; the interrupted
		# horse must NOT overwrite it with Cancelled.
		task = self._run_interrupted(force_killed_by="op@x.com")
		self.assertNotEqual(task.workflow_state, "Cancelled")


class TestStopReportsBreadth(unittest.TestCase):
	@patch(f"{_C}.frappe")
	def test_stop_reports_delegated_count(self, fr):
		from frappe.friday_core.gateway import commands

		fr.get_roles.return_value = ["Friday Operator"]
		with (
			patch("frappe.friday_core.gateway.interrupt.request_interrupt"),
			patch("frappe.friday_core.gateway.interrupt.cascade_interrupt", return_value=2),
		):
			result = commands.dispatch_command(platform="raven", session_id="S", user="op@x.com", raw="/stop")
		self.assertTrue(result.ok)
		self.assertIn("2", result.reply)

	@patch(f"{_C}.frappe")
	def test_stop_plain_message_when_no_delegation(self, fr):
		from frappe.friday_core.gateway import commands

		fr.get_roles.return_value = ["Friday Operator"]
		with (
			patch("frappe.friday_core.gateway.interrupt.request_interrupt"),
			patch("frappe.friday_core.gateway.interrupt.cascade_interrupt", return_value=0),
		):
			result = commands.dispatch_command(platform="raven", session_id="S", user="op@x.com", raw="/stop")
		self.assertTrue(result.ok)
		self.assertNotIn("delegated", result.reply.lower())


if __name__ == "__main__":
	unittest.main()
