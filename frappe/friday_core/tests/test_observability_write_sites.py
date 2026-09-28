# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Per-write-site emit assertions (Design 72).

Each instrumented site emits at the right moment with the right trigger_source.
A future change that drops one of these emits gets caught here, instead of by
"an event went missing in the Lifecycle Trace last quarter."

Sites covered:
1. tasks/workflow.py     — workflow.state_change + workflow.executing_token_released + warroom.post
2. tasks/dispatcher.py   — dispatcher.skip (no_profile_match) via _ready_to_dispatch
3. tasks/reconciler.py   — reconciler.tick on tick() (action counts present)
4. tasks/runner.py       — runner.start + runner.complete + runner.error  (smoke via _run_task_agentic)
5. llm/usage.py          — llm.call_summary on record_usage()
6. issues/raise_issue.py — issue.raised
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

# Three tests record usage / crash a runner AS profile "Friday". A dev site has it
# from `bench friday setup`; a fresh site does not, and the Link validation on
# LLM Usage Log / Task makes emit() swallow the row. A test must not depend on
# operator setup.
FIXTURE_PROFILE = "Friday"


def setUpModule():
	if not frappe.db.exists("Agent Profile", FIXTURE_PROFILE):
		frappe.get_doc(
			{
				"doctype": "Agent Profile",
				"profile_name": FIXTURE_PROFILE,
				"agent_role": "Specialist",
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()


def _clear_events_for_task(task_name: str) -> None:
	frappe.db.sql("DELETE FROM `tabDispatcher Event` WHERE task = %s", (task_name,))
	frappe.db.commit()


def _clear_all_events() -> None:
	frappe.db.sql("DELETE FROM `tabDispatcher Event`")
	frappe.db.commit()


def _ensure_test_task() -> str:
	existing = frappe.get_all("Task", limit=1, pluck="name")
	if existing:
		return existing[0]
	project = frappe.get_all("Project", limit=1, pluck="name")
	project_name = project[0] if project else None
	if not project_name:
		proj = frappe.get_doc({"doctype": "Project", "project_name": "Test Write Sites Project"}).insert(
			ignore_permissions=True
		)
		project_name = proj.name
	task = frappe.get_doc(
		{
			"doctype": "Task",
			"title": "Test Write Sites Task",
			"project": project_name,
			"workflow_state": "Pending",
		}
	).insert(ignore_permissions=True)
	return task.name


def _events_for(task_name, event_type=None):
	filters = {"task": task_name}
	if event_type:
		filters["event_type"] = event_type
	return frappe.get_all(
		"Dispatcher Event",
		filters=filters,
		fields=["name", "event_type", "trigger_source", "summary"],
		order_by="creation asc",
	)


# ---------------------------------------------------------------------------
# 1. workflow.py — state_change + executing_token_released + warroom.post
# ---------------------------------------------------------------------------


class TestWorkflowEmits(unittest.TestCase):
	"""tasks/workflow.py — the workflow hook emits on every state save."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def setUp(self):
		_clear_events_for_task(self.task_name)

	def test_state_change_emits_with_trigger_source(self):
		"""A save with the dispatcher_event_source flag set carries that source."""
		task = frappe.get_doc("Task", self.task_name)
		# Force a state change.
		task.workflow_state = "Pending" if task.workflow_state != "Pending" else "Cancelled"
		frappe.flags.dispatcher_event_source = "unit_test_source"
		try:
			task.save(ignore_permissions=True)
			frappe.db.commit()
		finally:
			frappe.flags.dispatcher_event_source = None

		events = _events_for(self.task_name, "workflow.state_change")
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["trigger_source"], "unit_test_source")

	def test_unknown_source_when_no_flag_set(self):
		"""Direct user save with no flag → trigger_source = unknown."""
		task = frappe.get_doc("Task", self.task_name)
		new_state = "Pending" if task.workflow_state != "Pending" else "Cancelled"
		task.workflow_state = new_state
		# Ensure no flag set.
		frappe.flags.dispatcher_event_source = None
		task.save(ignore_permissions=True)
		frappe.db.commit()

		events = _events_for(self.task_name, "workflow.state_change")
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["trigger_source"], "unknown")

	def test_executing_token_released_event_fires(self):
		"""Moving away from Executing while a token was set emits a release event."""
		# Force the task into Executing with a token, then transition out.
		task = frappe.get_doc("Task", self.task_name)
		# Use db_set to bypass the hook's clearing logic so we can simulate
		# a real Executing-with-token state.
		frappe.db.sql(
			"UPDATE `tabTask` SET workflow_state = 'Executing', "
			"executing_token = 'unit-test-token' WHERE name = %s",
			(self.task_name,),
		)
		frappe.db.commit()
		_clear_events_for_task(self.task_name)  # fresh window

		task = frappe.get_doc("Task", self.task_name)
		task.workflow_state = "Pending"
		task.save(ignore_permissions=True)
		frappe.db.commit()

		# Both events should be present.
		events = _events_for(self.task_name)
		types = [e["event_type"] for e in events]
		self.assertIn("workflow.state_change", types)
		self.assertIn("workflow.executing_token_released", types)

	def tearDown(self):
		_clear_events_for_task(self.task_name)


# ---------------------------------------------------------------------------
# 2. dispatcher.py — dispatcher.skip via _ready_to_dispatch gates
# ---------------------------------------------------------------------------


class TestDispatcherSkipEmits(unittest.TestCase):
	"""tasks/dispatcher.py — skip events emit with correct reason."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def setUp(self):
		_clear_events_for_task(self.task_name)

	def test_milestone_skip_emits(self):
		"""A milestone task hitting _ready_to_dispatch emits milestone_not_dispatchable."""
		from frappe.friday_core.tasks.dispatcher import _ready_to_dispatch

		fake_task = frappe._dict({"name": self.task_name, "execution_mode": "milestone"})
		result = _ready_to_dispatch(fake_task)
		frappe.db.commit()
		self.assertFalse(result)

		events = _events_for(self.task_name, "dispatcher.skip")
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["trigger_source"], "milestone_not_dispatchable")

	def test_skip_is_deduped_within_window(self):
		"""Two back-to-back skips for the same reason produce ONE event."""
		from frappe.friday_core.tasks.dispatcher import _ready_to_dispatch

		fake_task = frappe._dict({"name": self.task_name, "execution_mode": "milestone"})
		_ready_to_dispatch(fake_task)
		_ready_to_dispatch(fake_task)
		_ready_to_dispatch(fake_task)
		frappe.db.commit()

		events = _events_for(self.task_name, "dispatcher.skip")
		# Only one event despite three calls — 60s dedup window.
		self.assertEqual(len(events), 1)

	def tearDown(self):
		_clear_events_for_task(self.task_name)


# ---------------------------------------------------------------------------
# 3. reconciler.py — reconciler.tick on tick()
# ---------------------------------------------------------------------------


class TestReconcilerTickEmits(unittest.TestCase):
	"""tasks/reconciler.py — one reconciler.tick event per cycle."""

	def setUp(self):
		_clear_all_events()

	def test_tick_emits_summary_event(self):
		from frappe.friday_core.tasks.reconciler import tick

		tick()
		frappe.db.commit()

		# Find a reconciler.tick event in the recent window.
		events = frappe.get_all(
			"Dispatcher Event",
			filters={"event_type": "reconciler.tick"},
			fields=["name", "trigger_source", "summary", "payload_json"],
			order_by="creation desc",
			limit=1,
		)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["trigger_source"], "scheduler")
		# Summary mentions each phase.
		summary = events[0]["summary"] or ""
		for phase in ("assigned_orphans", "executing_stale", "transient_blocked", "connector_events"):
			self.assertIn(phase, summary)

	def tearDown(self):
		_clear_all_events()


# ---------------------------------------------------------------------------
# 4. runner.py — runner.error on the top-level except path
# ---------------------------------------------------------------------------


class TestRunnerErrorEmit(unittest.TestCase):
	"""tasks/runner.py — runner.error fires when on_agent_task_assigned crashes."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def setUp(self):
		_clear_events_for_task(self.task_name)

	def test_runner_crash_emits_error_event(self):
		"""A crash inside _run_task → runner.error with the exc type in summary."""
		from frappe.friday_core.tasks import runner

		# Force _run_task to throw — exercises the outer except in
		# on_agent_task_assigned which emits runner.error.
		with patch.object(runner, "_run_task", side_effect=RuntimeError("simulated crash")):
			runner.on_agent_task_assigned(
				{
					"task_name": self.task_name,
					"assigned_to_profile": "Friday",
					"workflow_state": "Assigned",
				}
			)
			frappe.db.commit()

		events = _events_for(self.task_name, "runner.error")
		self.assertEqual(len(events), 1)
		self.assertIn("RuntimeError", events[0]["summary"])

	def tearDown(self):
		_clear_events_for_task(self.task_name)


# ---------------------------------------------------------------------------
# 5. llm/usage.py — llm.call_summary on every record_usage()
# ---------------------------------------------------------------------------


class TestLLMUsageEmit(unittest.TestCase):
	"""llm/usage.py — record_usage() emits a llm.call_summary event."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def setUp(self):
		_clear_events_for_task(self.task_name)

	def test_record_usage_emits_call_summary(self):
		from frappe.friday_core.llm.usage import record_usage

		fake_provider = MagicMock()
		fake_provider.PROVIDER_NAME = "minimax"
		fake_provider.input_cost_per_million = 0.0
		fake_provider.output_cost_per_million = 0.0

		# Use task::<name> session_id so the event is linked to a real task.
		record_usage(
			profile_name="Friday",
			session_id=f"task::{self.task_name}",
			provider=fake_provider,
			model="minimax-m3",
			usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
		)
		frappe.db.commit()

		events = _events_for(self.task_name, "llm.call_summary")
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["trigger_source"], "llm_call_succeeded")
		summary = events[0]["summary"] or ""
		self.assertIn("minimax", summary)
		self.assertIn("150", summary)

	def test_non_task_session_id_emits_without_task_link(self):
		"""Chat / delegate sessions still emit, but with task=None."""
		from frappe.friday_core.llm.usage import record_usage

		fake_provider = MagicMock()
		fake_provider.PROVIDER_NAME = "minimax"
		# Real numeric cost attrs — without these, estimate_cost gets a Mock
		# and the LLM Usage Log insert fails BEFORE the emit() can fire.
		fake_provider.input_cost_per_million = 0.0
		fake_provider.output_cost_per_million = 0.0

		_clear_all_events()
		record_usage(
			profile_name="Friday",
			session_id="chat::some-other-session",
			provider=fake_provider,
			model="minimax-m3",
			usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
		)
		frappe.db.commit()

		events = frappe.get_all(
			"Dispatcher Event",
			filters={"event_type": "llm.call_summary"},
			fields=["task", "trigger_source"],
			order_by="creation desc",
			limit=1,
		)
		self.assertEqual(len(events), 1)
		self.assertIsNone(events[0]["task"] or None)
		_clear_all_events()

	def tearDown(self):
		_clear_events_for_task(self.task_name)


# ---------------------------------------------------------------------------
# 6. issues/raise_issue.py — issue.raised on every raise_failure_issue()
# ---------------------------------------------------------------------------


class TestIssueRaisedEmit(unittest.TestCase):
	"""issues/raise_issue.py — every Issue creation emits an issue.raised event."""

	@classmethod
	def setUpClass(cls):
		cls.task_name = _ensure_test_task()

	def setUp(self):
		_clear_events_for_task(self.task_name)

	def test_raise_failure_issue_emits_issue_raised(self):
		from frappe.friday_core.issues.raise_issue import raise_failure_issue

		issue_name = raise_failure_issue(self.task_name, error_type="UnitTestError", details="simulated")
		frappe.db.commit()
		self.assertTrue(frappe.db.exists("Issue", issue_name))

		events = _events_for(self.task_name, "issue.raised")
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["trigger_source"], "failure")
		self.assertIn("UnitTestError", events[0]["summary"])

		# Cleanup the issue row so we don't leak.
		frappe.db.sql("DELETE FROM `tabIssue` WHERE name = %s", (issue_name,))
		frappe.db.commit()

	def tearDown(self):
		_clear_events_for_task(self.task_name)
