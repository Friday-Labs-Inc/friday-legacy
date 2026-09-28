# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
The `delegate-task` skill — an orchestrator hands work to another agent.

Design 69a rewrites the original design 57 handler. The old handler ran the
child inline (parent blocked). The new handler returns immediately — the child
is a durable Pending Task that the pipeline dispatches independently, so the
parent can fan out multiple children in parallel.

Gates enforced at dispatch (defense in depth — the loader also hides the skill
from non-Orchestrators):

  1. ROLE GATE — only agent_role == "Orchestrator" may call this.
  2. DEPTH GATE — the parent_task chain depth must be < max_delegation_depth.
  3. CONCURRENCY GATE — active children of this parent must be < profile's
     max_concurrent_delegations.

The child Task is created as Pending with execution_mode=agentic, parent_task
pointing at the caller, and project inherited from the parent. The existing
dispatcher tick -> runner pipeline picks it up exactly like any other task.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.agent_runner.dispatcher import register_skill_handler
from frappe.friday_core.doctype.agent_settings.agent_settings import (
	SETTINGS_DOCTYPE as _SETTINGS,
)
from frappe.friday_core.doctype.agent_settings.agent_settings import (
	SETTINGS_NAME as _SETTINGS_NAME,
)

DELEGATE_TASK = "delegate-task"

_ACTIVE_STATES = ("Pending", "Assigned", "Executing", "Blocked")


def delegate_task(skill_name: str, parameters: dict) -> dict:
	ctx = frappe.flags.get("friday_dispatch_context") or {}
	parent_session = ctx.get("session_id") or ""
	parent_profile_name = ctx.get("agent_profile") or ""

	if not parent_profile_name:
		raise ValueError(
			"delegate-task called outside a dispatch context — "
			"agent_profile not set in friday_dispatch_context"
		)

	# ── role gate ─────────────────────────────────────────────────────
	parent_profile = frappe.get_cached_doc("Agent Profile", parent_profile_name)
	if getattr(parent_profile, "agent_role", None) != "Orchestrator":
		raise ValueError(
			"only Orchestrator profiles can delegate — this profile's role is "
			f"{getattr(parent_profile, 'agent_role', 'unset')!r}"
		)

	# ── extract parent task name (if running inside a task) ───────────
	parent_task_name = None
	if parent_session.startswith("task::"):
		parent_task_name = parent_session.removeprefix("task::")

	# ── depth gate ────────────────────────────────────────────────────
	max_depth, hard_ceiling = _get_delegation_settings()
	effective_max = min(max_depth, hard_ceiling)

	if parent_task_name:
		depth = _delegation_depth(parent_task_name)
	else:
		depth = 0

	if depth >= effective_max:
		raise ValueError(
			f"delegation depth limit reached ({depth} >= {effective_max}) — "
			f"do the work directly instead of delegating further"
		)

	# ── concurrency gate ──────────────────────────────────────────────
	max_concurrent = getattr(parent_profile, "max_concurrent_delegations", 5) or 5
	if parent_task_name:
		active = frappe.db.count(
			"Task",
			filters={
				"parent_task": parent_task_name,
				"workflow_state": ("in", _ACTIVE_STATES),
			},
		)
	else:
		active = frappe.db.count(
			"Task",
			filters={
				"originating_session": parent_session,
				"parent_task": ("is", "not set"),
				"workflow_state": ("in", _ACTIVE_STATES),
			},
		)
	if active >= max_concurrent:
		raise ValueError(
			f"concurrent delegation limit reached ({active} >= {max_concurrent}) — "
			f"wait for existing children to complete before delegating more"
		)

	# ── validate parameters ───────────────────────────────────────────
	target_profile_name = (parameters.get("agent_profile") or "").strip()
	instruction = (parameters.get("instruction") or "").strip()
	title = (parameters.get("title") or "").strip() or (instruction[:80] if instruction else "")

	if not target_profile_name:
		raise ValueError("delegate-task requires an 'agent_profile' parameter")
	if not instruction:
		raise ValueError("delegate-task requires an 'instruction' parameter")
	if not frappe.db.exists("Agent Profile", target_profile_name):
		raise ValueError(f"Agent Profile {target_profile_name!r} not found")

	target_profile = frappe.get_cached_doc("Agent Profile", target_profile_name)
	if getattr(target_profile, "status", None) != "Active":
		raise ValueError(f"Agent Profile {target_profile_name!r} is not Active")

	# ── inherit project from parent task ──────────────────────────────
	project = (parameters.get("project") or "").strip() or None
	if not project and parent_task_name:
		project = frappe.db.get_value("Task", parent_task_name, "project")

	# ── create child Task ─────────────────────────────────────────────
	child = frappe.get_doc(
		{
			"doctype": "Task",
			"title": title,
			"description": instruction,
			"parent_task": parent_task_name,
			"project": project,
			"assigned_to_profile": target_profile_name,
			"workflow_state": "Pending",
			"execution_mode": "agentic",
			"priority": parameters.get("priority") or "normal",
			"originating_session": parent_session,
			"originating_platform": "delegation",
		}
	)
	child.insert(ignore_permissions=True)

	return {
		"result": (
			f"Delegated to {target_profile_name!r} as task {child.name}. "
			f"The child is queued and will run through the pipeline. "
			f"Use wait_for_result or tail_child to monitor progress."
		),
		"delegation_id": child.name,
		"child_task_name": child.name,
		"status": "queued",
		"assigned_profile": target_profile_name,
		"doctype": "Task",
		"record_name": child.name,
	}


def _delegation_depth(task_name: str, *, _get_parent=None) -> int:
	"""Walk the parent_task chain and return the depth (0 = root)."""
	if _get_parent is None:

		def _get_parent(name):
			return frappe.db.get_value("Task", name, "parent_task")

	depth = 0
	current = task_name
	seen = {task_name}
	while current:
		parent = _get_parent(current)
		if not parent:
			break
		if parent in seen:
			break
		seen.add(parent)
		depth += 1
		current = parent
	return depth


def _get_delegation_settings() -> tuple[int, int]:
	"""Read max_delegation_depth and hard ceiling from Agent Settings."""
	settings = frappe.get_cached_doc(_SETTINGS, _SETTINGS_NAME)
	max_depth = getattr(settings, "max_delegation_depth", 3) or 3
	hard_ceiling = getattr(settings, "delegation_depth_hard_ceiling", 8) or 8
	return max_depth, hard_ceiling


register_skill_handler(DELEGATE_TASK, delegate_task)
