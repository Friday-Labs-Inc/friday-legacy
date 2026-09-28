# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the durability reconciler (design 61a, Q1 + Q7a).

The reconciler is the heartbeat that replaces event load-bearing: every
60s it scans tasks (and RandomPack events) by state and drives them
forward purely from DB facts. A lost enqueue heals on the next tick.

These tests pin the SQL claims and the per-state actions; the
queue/Issue side-effects are mocked.
"""

import unittest
from unittest.mock import MagicMock, patch


def _mock_task(name, state, **fields):
	t = MagicMock(name=name, workflow_state=state, **fields)
	type(t).name = property(lambda self, n=name: n)
	t.get = lambda key, default=None, _f={"workflow_state": state, **fields}: _f.get(key, default)
	return t


class TestReconcilerAssignedOrphans(unittest.TestCase):
	"""Assigned tasks with no in-flight job → re-enqueue the runner trigger."""

	@patch("frappe.friday_core.tasks.reconciler.get_jobs")
	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_reenqueues_assigned_task_with_no_inflight_job(self, mock_frappe, mock_get_jobs):
		from frappe.friday_core.tasks.reconciler import _reconcile_assigned_orphans

		mock_frappe.db.sql.return_value = [{"name": "TASK-1", "assigned_to_profile": "Copywriter"}]
		mock_get_jobs.return_value = {}  # no in-flight jobs

		_reconcile_assigned_orphans()

		# The enqueue must go to the friday queue (Q4) and target the runner.
		mock_frappe.enqueue.assert_called_once()
		call = mock_frappe.enqueue.call_args
		self.assertEqual(call.args[0], "frappe.friday_core.tasks.runner.on_agent_task_assigned")
		self.assertEqual(call.kwargs["queue"], "friday")
		self.assertEqual(call.kwargs["job_name"], "task:TASK-1")
		self.assertEqual(call.kwargs["message"]["task_name"], "TASK-1")

	@patch("frappe.friday_core.tasks.reconciler.get_jobs")
	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_skips_assigned_task_with_inflight_job(self, mock_frappe, mock_get_jobs):
		"""If RQ still has the job, do not double-enqueue."""
		from frappe.friday_core.tasks.reconciler import _reconcile_assigned_orphans

		mock_frappe.db.sql.return_value = [{"name": "TASK-1", "assigned_to_profile": "Copywriter"}]
		mock_get_jobs.return_value = {"friday": ["task:TASK-1"]}

		_reconcile_assigned_orphans()

		mock_frappe.enqueue.assert_not_called()

	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_sql_filters_to_assigned_with_30s_grace(self, mock_frappe):
		from frappe.friday_core.tasks.reconciler import _reconcile_assigned_orphans

		mock_frappe.db.sql.return_value = []

		_reconcile_assigned_orphans()

		sql = mock_frappe.db.sql.call_args[0][0]
		self.assertIn("workflow_state = 'Assigned'", sql)
		self.assertIn("assigned_to_profile IS NOT NULL", sql)
		# 30s grace so we don't race the original enqueue.
		self.assertIn("assigned_at", sql)


class TestReconcilerExecutingStale(unittest.TestCase):
	"""Executing tasks with no fresh heartbeat → Block + Failure Issue."""

	@patch("frappe.friday_core.tasks.reconciler._raise_runner_lost_issue")
	@patch("frappe.friday_core.tasks.reconciler.get_jobs")
	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_blocks_executing_task_with_stale_heartbeat(self, mock_frappe, mock_get_jobs, mock_issue):
		from frappe.friday_core.tasks.reconciler import _reconcile_executing_stale

		mock_frappe.db.sql.return_value = [{"name": "TASK-2"}]
		mock_get_jobs.return_value = {}  # no in-flight = runner truly lost
		task = _mock_task("TASK-2", "Executing")
		mock_frappe.get_doc.return_value = task

		_reconcile_executing_stale()

		self.assertEqual(task.workflow_state, "Blocked")
		self.assertEqual(task.blocked_reason, "runner_lost")
		task.save.assert_called_once_with(ignore_permissions=True)
		mock_issue.assert_called_once_with("TASK-2")

	@patch("frappe.friday_core.tasks.reconciler._raise_runner_lost_issue")
	@patch("frappe.friday_core.tasks.reconciler.get_jobs")
	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_skips_executing_task_with_inflight_job(self, mock_frappe, mock_get_jobs, mock_issue):
		"""A long-running task that's truly executing must not be killed."""
		from frappe.friday_core.tasks.reconciler import _reconcile_executing_stale

		mock_frappe.db.sql.return_value = [{"name": "TASK-2"}]
		mock_get_jobs.return_value = {"friday": ["task:TASK-2"]}

		_reconcile_executing_stale()

		mock_frappe.get_doc.assert_not_called()
		mock_issue.assert_not_called()

	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_sql_uses_heartbeat_not_started_at(self, mock_frappe):
		"""Genuinely long agentic runs heartbeat; we must read THAT, not start time."""
		from frappe.friday_core.tasks.reconciler import _reconcile_executing_stale

		mock_frappe.db.sql.return_value = []

		_reconcile_executing_stale()

		sql = mock_frappe.db.sql.call_args[0][0]
		self.assertIn("last_heartbeat_at", sql)
		self.assertIn("workflow_state = 'Executing'", sql)

	@patch("frappe.friday_core.tasks.reconciler._raise_runner_lost_issue")
	@patch("frappe.friday_core.tasks.reconciler.get_jobs")
	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_one_poisoned_row_is_isolated_and_the_sweep_continues(
		self, mock_frappe, mock_get_jobs, mock_issue
	):
		"""A row whose save() raises must roll back to the per-row savepoint and NOT
		abort the rest of the sweep — the next good row still gets rescued. Without the
		savepoint, Postgres aborts the whole tick tx and every later row/sweep no-ops
		(the #132 transaction-poison class)."""
		from frappe.friday_core.tasks.reconciler import _reconcile_executing_stale

		mock_frappe.db.sql.return_value = [{"name": "BAD"}, {"name": "GOOD"}]
		mock_get_jobs.return_value = {}

		bad = _mock_task("BAD", "Executing")
		bad.save.side_effect = RuntimeError("constraint violation")
		good = _mock_task("GOOD", "Executing")
		mock_frappe.get_doc.side_effect = lambda _dt, name: bad if name == "BAD" else good

		acted = _reconcile_executing_stale()  # must not raise

		# the bad row's partial writes were undone to the per-row savepoint...
		mock_frappe.db.savepoint.assert_any_call("friday_reconcile_row")
		mock_frappe.db.rollback.assert_called_with(save_point="friday_reconcile_row")
		# ...and the GOOD row was still rescued, so the sweep didn't abort.
		good.save.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(acted, 1)  # only GOOD counted


class TestReconcilerTransientBlockedRetry(unittest.TestCase):
	"""Blocked tasks with a transient reason → re-Pend (up to 3 retries)."""

	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_retries_transient_blocked_under_budget(self, mock_frappe):
		from frappe.friday_core.tasks.reconciler import _reconcile_transient_blocked

		mock_frappe.db.sql.return_value = [{"name": "TASK-3", "retry_count": 1}]
		task = _mock_task("TASK-3", "Blocked", blocked_reason="oom", retry_count=1)
		mock_frappe.get_doc.return_value = task

		_reconcile_transient_blocked()

		self.assertEqual(task.workflow_state, "Pending")
		self.assertEqual(task.retry_count, 2)
		# Lease + assignment must reset so the dispatcher can re-claim it cleanly.
		self.assertEqual(task.assigned_to_profile, None)
		self.assertEqual(task.executing_token, None)
		task.save.assert_called_once_with(ignore_permissions=True)

	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_poisoned_row_is_isolated_and_the_sweep_continues(self, mock_frappe):
		"""Same #132 isolation for the re-pend sweep: a bad row rolls back to the
		savepoint and the next good row is still re-Pended."""
		from frappe.friday_core.tasks.reconciler import _reconcile_transient_blocked

		mock_frappe.db.sql.return_value = [
			{"name": "BAD", "retry_count": 0},
			{"name": "GOOD", "retry_count": 0},
		]
		bad = _mock_task("BAD", "Blocked", blocked_reason="oom", retry_count=0)
		bad.save.side_effect = RuntimeError("constraint violation")
		good = _mock_task("GOOD", "Blocked", blocked_reason="timeout", retry_count=0)
		mock_frappe.get_doc.side_effect = lambda _dt, name: bad if name == "BAD" else good

		acted = _reconcile_transient_blocked()  # must not raise

		mock_frappe.db.rollback.assert_called_with(save_point="friday_reconcile_row")
		good.save.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(acted, 1)

	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_sql_caps_retry_count_at_3(self, mock_frappe):
		from frappe.friday_core.tasks.reconciler import _reconcile_transient_blocked

		mock_frappe.db.sql.return_value = []

		_reconcile_transient_blocked()

		sql = mock_frappe.db.sql.call_args[0][0]
		# A task that has already burned its retry budget must NOT be re-Pended;
		# it stays Blocked for a human. The cap is part of the claim query.
		# The budget value lives in a module constant (RETRY_BUDGET = 3) and is
		# passed as a SQL parameter, so we check the parameterised shape.
		self.assertIn("retry_count <", sql)
		self.assertIn("%(budget)s", sql)
		# Transient reasons are retried; semantic blocks (dependency_failed,
		# no_profile_for_skills) wait for humans. The reason list is a SQL param
		# sourced from TRANSIENT_BLOCKED_REASONS and now includes the retryable
		# LLM transport reasons so a provider hiccup self-heals.
		self.assertIn("blocked_reason IN", sql)
		params = mock_frappe.db.sql.call_args[0][1]
		for reason in ("timeout", "rate_limit", "server_error", "overloaded"):
			self.assertIn(reason, params["reasons"])


class TestReconcilerRandomPackEvents(unittest.TestCase):
	"""Q7a: stuck-Received and Failed-retriable RandomPack events re-enqueue."""

	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_received_event_over_60s_old_reenqueues(self, mock_frappe):
		from frappe.friday_core.tasks.reconciler import _reconcile_connector_events

		mock_frappe.db.sql.return_value = [{"name": "EV-1", "status": "Received"}]

		_reconcile_connector_events()

		mock_frappe.enqueue.assert_called_once()
		call = mock_frappe.enqueue.call_args
		self.assertEqual(call.args[0], "frappe.friday_core.connectors.core.process_event")
		self.assertEqual(call.kwargs["queue"], "friday")
		self.assertEqual(call.kwargs["event_id"], "EV-1")

	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_sql_filters_received_and_failed_under_retry_budget(self, mock_frappe):
		from frappe.friday_core.tasks.reconciler import _reconcile_connector_events

		mock_frappe.db.sql.return_value = []

		_reconcile_connector_events()

		sql = mock_frappe.db.sql.call_args[0][0]
		# Received OR Failed → both eligible, distinct grace periods (60s / 5min).
		self.assertIn("status IN ('Received', 'Failed')", sql)
		# The budget value lives in a module constant (RETRY_BUDGET = 3) and is
		# passed as a SQL parameter, so we check the parameterised shape.
		self.assertIn("retry_count <", sql)
		self.assertIn("%(budget)s", sql)


class TestTickWiring(unittest.TestCase):
	"""tick() drives all four sub-reconciles in order, failures are isolated."""

	@patch("frappe.friday_core.tasks.reconciler._reconcile_connector_events")
	@patch("frappe.friday_core.tasks.reconciler._reconcile_transient_blocked")
	@patch("frappe.friday_core.tasks.reconciler._reconcile_executing_stale")
	@patch("frappe.friday_core.tasks.reconciler._reconcile_assigned_orphans")
	def test_tick_runs_all_phases(self, mock_a, mock_e, mock_b, mock_r):
		from frappe.friday_core.tasks.reconciler import tick

		tick()

		mock_a.assert_called_once()
		mock_e.assert_called_once()
		mock_b.assert_called_once()
		mock_r.assert_called_once()

	@patch("frappe.friday_core.tasks.reconciler._reconcile_connector_events")
	@patch("frappe.friday_core.tasks.reconciler._reconcile_transient_blocked")
	@patch("frappe.friday_core.tasks.reconciler._reconcile_executing_stale")
	@patch("frappe.friday_core.tasks.reconciler._reconcile_assigned_orphans")
	@patch("frappe.friday_core.tasks.reconciler.frappe")
	def test_one_phase_failing_does_not_abort_the_rest(self, mock_frappe, mock_a, mock_e, mock_b, mock_r):
		"""Reconciler is the heartbeat; a failure in one sweep must not kill the others."""
		from frappe.friday_core.tasks.reconciler import tick

		mock_a.side_effect = RuntimeError("simulated DB blip")

		tick()  # must not raise

		mock_e.assert_called_once()
		mock_b.assert_called_once()
		mock_r.assert_called_once()
		mock_frappe.log_error.assert_called()


if __name__ == "__main__":
	unittest.main()
