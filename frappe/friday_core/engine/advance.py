# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Advance (Design 75) — when an agent finishes a phase, move the work-item on.

PLAIN ENGLISH
=============
A Task that the engine created carries `phase_key` + `work_item_*`. When that
Task reaches "Completed", the work-item should advance to its next state — which
re-fires the interpreter and dispatches the next phase. That is what makes the
pipeline flow on its own.

Two rules from the design's adversarial critic are baked in here:

  - NEVER advance synchronously (CRITICAL-2). The advance fires a workflow
    transition, which saves the work-item, which creates the NEXT task — all
    inside whatever transaction completed the first task. A rollback there
    would double-run. So `on_task_update` only ENQUEUES `advance_work_item`,
    after commit, in its own job.
  - ALWAYS fire the transition as the agent, never as Administrator
    (CRITICAL-1, via engine.governance.acting_as). The agent that ran the
    phase owns the transition that leaves it, so it is authorised; firing as
    Administrator would silently skip role-gating. If the agent has no system
    user we refuse to advance and log — a visible stall, not a governance
    bypass.

This is registered as a SECOND on_update handler for Task (alongside
tasks.workflow.on_state_change) so the proven Task lifecycle code is untouched.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.engine import bundle
from frappe.friday_core.engine.governance import acting_as
from frappe.model.workflow import apply_workflow


def on_task_update(doc, method: str | None = None) -> None:
	"""Task on_update hook: enqueue the work-item advance when an engine task
	completes. No-op for tasks that aren't part of a metadata workflow."""
	if doc.workflow_state != "Completed":
		return
	if not (doc.get("phase_key") and doc.get("work_item_doctype") and doc.get("work_item_name")):
		return  # not an engine-driven task (e.g. legacy pipeline / freeform / delegation)
	if not doc.has_value_changed("workflow_state"):
		return

	frappe.enqueue(
		"frappe.friday_core.engine.advance.advance_work_item",
		queue="friday",
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		job_id=f"advance:{doc.name}",
		task_name=doc.name,
	)


def advance_work_item(task_name: str) -> None:
	"""Fire the workflow transition that leaves the completed phase's state,
	acting as the agent that did the work. Idempotent: a duplicate job finds the
	work-item already past the phase's from_state and no-ops."""
	task = frappe.get_doc("Task", task_name)
	if task.workflow_state != "Completed":
		return

	workflow = bundle.workflow_for(task.work_item_doctype)
	if not workflow:
		return

	meta = frappe.db.get_value(
		"Friday Workflow Transition Meta",
		{"workflow": workflow, "phase_key": task.phase_key},
		["action", "from_state"],
		as_dict=True,
	)
	if not meta or not meta.action:
		frappe.log_error(
			message=(
				f"No transition meta for phase {task.phase_key!r} in workflow {workflow!r}; "
				f"cannot advance work item {task.work_item_doctype} {task.work_item_name}."
			),
			title="Design 75 advance — no meta",
		)
		return

	state_field = bundle.state_field_for(workflow)
	work_item = frappe.get_doc(task.work_item_doctype, task.work_item_name)
	if work_item.get(state_field) != meta.from_state:
		# Already advanced — a duplicate advance job, or a human moved it. No-op.
		return

	actor = _actor_for(task)
	if not actor:
		frappe.log_error(
			message=(
				f"Task {task.name} profile {task.assigned_to_profile!r} has no frappe_user; "
				"refusing to fire the transition as Administrator (that would bypass "
				"role-gating). Work item stalls until the agent user is provisioned."
			),
			title="Design 75 advance — no actor",
		)
		return

	with acting_as(actor):
		apply_workflow(work_item, meta.action)


def _actor_for(task) -> str | None:
	"""The system user that should fire the advance — the agent that ran the
	phase. It holds the agent role that owns the transition, so apply_workflow
	accepts it."""
	if not task.assigned_to_profile:
		return None
	return frappe.db.get_value("Agent Profile", task.assigned_to_profile, "frappe_user")
