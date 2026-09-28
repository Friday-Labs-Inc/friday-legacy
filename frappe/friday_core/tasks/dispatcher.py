# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Task dispatcher — cron-scheduled task claim and profile assignment.

Registered as ``scheduler_events["cron"]["*/1 * * * *"]`` in ``hooks.py``.

One dispatcher cycle (``tick()``) runs every 60 seconds and:

1. Atomically fetches a window of dispatchable, unclaimed ``Pending`` tasks
   using ``SELECT … FOR UPDATE SKIP LOCKED`` so concurrent dispatcher
   instances never double-claim a row.
2. For each task, matches required skills against active Agent Profiles,
   stopping once ``DISPATCH_BUDGET_PER_TICK`` ready tasks have been handed off.
3. Assigns the first eligible profile and transitions the task to ``Assigned``.
   That state change fires ``tasks.workflow.on_state_change``, which enqueues
   the background runner (the single trigger chokepoint).
4. Logs a warning if a task has no eligible profile (it stays Pending and
   will be retried on the next tick).
"""

from __future__ import annotations

import frappe
from frappe.friday_core.observability import emit
from frappe.friday_core.observability.emit import emit_skip_deduped
from frappe.utils import now_datetime

_logger = frappe.logger("friday.tasks.dispatcher")


# How many tasks one tick will actually hand off to runners.
DISPATCH_BUDGET_PER_TICK = 5
# How many candidate rows we scan to FIND that many ready tasks. Larger than
# the budget so that tasks parked by _ready_to_dispatch (unmet dependencies,
# On Hold projects, milestone gates) cannot sit at the front of the queue and
# starve newer, ready tasks behind them.
DISPATCH_SCAN_WINDOW = 100


def tick() -> None:
	"""
	One dispatcher cycle — scan up to ``DISPATCH_SCAN_WINDOW`` candidates and
	hand off at most ``DISPATCH_BUDGET_PER_TICK`` ready tasks.

	Safe to call concurrently from multiple scheduler workers;
	``FOR UPDATE SKIP LOCKED`` ensures tasks are claimed exactly once.

	The scan/budget split matters: a small fetch (the old ``limit=5``) could
	return five tasks that are all parked by ``_ready_to_dispatch`` — the tick
	then dispatches nothing while ready tasks wait behind them. Scanning a
	wider window and only spending the budget on ready tasks prevents that
	starvation.
	"""
	tasks = _fetch_dispatchable_tasks(limit=DISPATCH_SCAN_WINDOW)
	dispatched = 0
	for task_doc in tasks:
		if not _ready_to_dispatch(task_doc):
			continue
		_claim_and_dispatch(task_doc)
		dispatched += 1
		if dispatched >= DISPATCH_BUDGET_PER_TICK:
			break


def _ready_to_dispatch(task_doc) -> bool:
	"""Design 60 gating, applied after the locked fetch.

	- milestone tasks are NEVER auto-dispatched (gates: completed only by
	  gate.decided events or humans),
	- AND-dependencies: every linked task must be Completed (Q3),
	- a paused project ("On Hold") parks its whole pipeline.

	Design 72 — every "not ready" path emits a deduped ``dispatcher.skip`` so
	the Dispatcher Queue panel can answer *why* a task is sitting unclaimed.
	Dedup window is 60s (one row per task per reason per tick).
	"""
	if (task_doc.get("execution_mode") or "mechanical") == "milestone":
		emit_skip_deduped(task_doc.name, "milestone_not_dispatchable")
		return False
	for dep in task_doc.get("depends_on") or []:
		if frappe.db.get_value("Task", dep.task, "workflow_state") != "Completed":
			emit_skip_deduped(
				task_doc.name,
				"parent_pending",
				payload={"waiting_on": dep.task},
			)
			return False
	if task_doc.get("project"):
		if frappe.db.get_value("Project", task_doc.project, "status") == "On Hold":
			emit_skip_deduped(task_doc.name, "project_on_hold")
			return False
	return True


def _fetch_dispatchable_tasks(limit: int = 5) -> list["Task"]:
	"""
	Fetch tasks that are dispatchable, unclaimed, and in Pending state.

	Uses ``FOR UPDATE SKIP LOCKED`` so concurrent dispatcher instances
	skip already-locked rows instead of waiting. Ordered by strict
	priority: urgent > high > normal > low, then FIFO by creation time.

	Args:
		limit: Maximum number of tasks to claim in one cycle.

	Returns:
		List of ``Task`` documents (already locked for update).
	"""
	# Frappe's ORM doesn't support FOR UPDATE SKIP LOCKED directly,
	# so we use raw SQL.  The query selects the document names; we then
	# load each document individually so Frappe tracks the row lock.
	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabTask`
		WHERE dispatchable = 1
		  AND assigned_to_profile IS NULL
		  AND workflow_state = 'Pending'
		ORDER BY
		  CASE priority
		    WHEN 'urgent' THEN 1
		    WHEN 'high'   THEN 2
		    WHEN 'normal' THEN 3
		    WHEN 'low'    THEN 4
		    ELSE 5
		  END,
		  creation ASC
		LIMIT %(limit)s
		FOR UPDATE SKIP LOCKED
		""",
		{"limit": limit},
		as_dict=True,
	)

	return [frappe.get_doc("Task", row.name) for row in rows]


def _claim_and_dispatch(task_doc: "Task") -> None:
	"""
	Match an eligible profile, atomically assign the task, and emit event.

	Args:
		task_doc: The claimed ``Task`` document (row-locked).
	"""
	eligible = _match_profiles(task_doc)

	if not eligible:
		_logger.warning(
			"No eligible profile for task %s (required_skills=%s)",
			task_doc.name,
			[trow.skill for trow in task_doc.required_skills],
		)
		# Design 72 — surface the skip reason so the operator can see *why*
		# this task is sitting unclaimed. Deduped by (task, reason) within 60s.
		emit_skip_deduped(
			task_doc.name,
			"no_profile_match",
			payload={
				"required_skills": [r.skill for r in task_doc.required_skills if r.skill],
			},
		)
		return

	chosen_profile = eligible[0]

	# Design 72 — flag the upcoming save so the workflow hook can stamp the
	# resulting workflow.state_change event with the honest source.
	frappe.flags.dispatcher_event_source = "dispatcher_claim"

	# Assign the profile AND transition Pending → Assigned in a single save.
	# The state change is what fires ``tasks.workflow.on_state_change``, which
	# enqueues the runner — that hook is the single trigger chokepoint. The
	# original code set only ``assigned_to_profile`` and left the task in
	# Pending, so the transition hook never ran and the task was never picked
	# up. Setting the state directly mirrors the runner's own transitions
	# (``runner._task_transition`` falls back to a direct set when no Frappe
	# Workflow is configured on the Task DocType).
	#
	# ``assigned_at`` is the timestamp the durability reconciler reads to
	# detect Assigned-but-never-picked-up tasks (lost enqueue) — without it
	# the reconciler can't tell a brand-new assignment from a stale one.
	task_doc.assigned_to_profile = chosen_profile
	task_doc.workflow_state = "Assigned"
	task_doc.assigned_at = frappe.utils.now_datetime()
	task_doc.save(ignore_permissions=True)

	# Design 72 — record the successful claim. The workflow.state_change event
	# is also emitted by the hook (with the same trigger_source flag), so the
	# claim_attempt event is the lower-level confirmation that this dispatcher
	# tick was the actor (vs the runner's own atomic claim race).
	emit(
		"dispatcher.claim_attempt",
		task=task_doc.name,
		project=task_doc.get("project"),
		agent_profile=chosen_profile,
		trigger_source="won",
		summary=f"{task_doc.name} claimed for {chosen_profile}",
	)
	# Clear the flag so subsequent saves in this tick don't inherit it.
	frappe.flags.dispatcher_event_source = None


def _match_profiles(task_doc: "Task") -> list[str]:
	"""
	Return Agent Profile names whose permitted skills cover all required skills.

	Phase 1 uses exact subset matching: every skill listed in
	``task_doc.required_skills`` must be explicitly permitted by the
	profile.  Profiles with no required skills can handle any task.

	Args:
		task_doc: The ``Task`` document.

	Returns:
		List of matching ``Agent Profile`` names, in creation order.
	"""
	required = {row.skill for row in task_doc.required_skills if row.skill}

	if not required:
		# No required skills — any active profile can take it.
		return [
			p.name
			for p in frappe.get_all(
				"Agent Profile",
				filters={"status": "Active"},
				order_by="creation asc",
			)
		]

	active = frappe.get_all(
		"Agent Profile",
		filters={"status": "Active"},
		order_by="creation asc",
	)

	matched = []
	for profile_row in active:
		permitted = _load_permitted_skills(profile_row.name)
		if required.issubset(permitted):
			matched.append(profile_row.name)

	return matched


def _load_permitted_skills(profile_name: str) -> set[str]:
	"""
	Return the set of skill names the profile can execute.

	Checks the profile's explicit ``permitted_skills`` table first,
	then falls back to the ``agent_role_profile`` linked on the profile.

	Args:
		profile_name: Name of the ``Agent Profile`` document.

	Returns:
		Frozenset of skill names.
	"""
	profile = frappe.get_doc("Agent Profile", profile_name)

	skills = {row.skill for row in profile.permitted_skills if row.skill}

	# Use .get(): ``agent_role_profile`` is an optional link that is not a
	# guaranteed column on every Agent Profile. A direct ``profile.x`` access
	# raises AttributeError when the field is absent, and because tick() runs
	# in the scheduler that exception was swallowed upstream — tasks just sat
	# Pending with no Error Log. .get() returns None instead of raising.
	if not skills and profile.get("agent_role_profile"):
		try:
			role_profile = frappe.get_doc("Agent Role Profile", profile.get("agent_role_profile"))
			skills |= {row.skill for row in role_profile.get("assigned_roles", []) if row.skill}
		except Exception:
			# Role profile may not exist or have no assigned_roles — ignore.
			pass

	return skills
