# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Phase dispatcher (Design 75) — turn one agentic transition into one Task.

PLAIN ENGLISH
=============
The interpreter found an agent step waiting at the work-item's current state.
This module builds the actual work order:

  1. Resolve the OWNER — the active Agent Profile whose discriminator_role
     matches the transition's agent_role. This is the O(1) routing that
     replaces the old skill-subset guessing (Design 75 §2).
  2. Render the prompt — the transition's Jinja template against the
     work-item's own fields, so the instruction is about THIS client/incident.
  3. Create the Task already in "Assigned" state with the owner set. That is
     what fires the proven runner path (workflow.on_state_change ->
     _emit_assigned_event) — the same machinery a dispatcher-assigned task
     uses. Creating it Pending-with-an-owner would strand it, because the
     dispatcher only claims tasks whose assigned_to_profile IS NULL.

If there is no active owner for the role, we do NOT crash the work-item save —
we log loudly and return. The work-item simply waits in its state, visibly
stalled, until an operator fixes the profile. Loud-and-stalled beats
silent-and-lost.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.engine import bundle


def dispatch(work_item, meta_name: str) -> str | None:
	"""Create (and assign) the Task for one agentic transition. Returns the new
	Task name, or None if the step could not be dispatched (logged)."""
	meta = frappe.get_doc("Friday Workflow Transition Meta", meta_name)

	role = meta.agent_role
	if not role:
		frappe.log_error(
			message=(
				f"Phase {meta.phase_key!r} is execution_mode=agentic but has no agent_role; "
				"cannot route. Set an Agent Role on the transition meta."
			),
			title="Design 75 dispatch — no agent_role",
		)
		return None

	profile = frappe.db.get_value("Agent Profile", {"discriminator_role": role, "status": "Active"}, "name")
	if not profile:
		frappe.log_error(
			message=(
				f"No Active Agent Profile holds discriminator_role {role!r} (phase "
				f"{meta.phase_key!r}). Work item {work_item.doctype} {work_item.name} stalls "
				"in its current state until a profile is provisioned."
			),
			title="Design 75 dispatch — no owner",
		)
		return None

	task = frappe.get_doc(
		{
			"doctype": "Task",
			"title": _title(work_item, meta),
			"description": _render(meta.prompt_template, work_item),
			"work_item_doctype": work_item.doctype,
			"work_item_name": work_item.name,
			"phase_key": meta.phase_key,
			"project": _project_of(work_item),
			"assigned_to_profile": profile,
			"workflow_state": "Assigned",
			"assigned_at": frappe.utils.now_datetime(),
			"execution_mode": "agentic",
			"priority": "normal",
			"required_skills": [{"skill": row.skill} for row in (meta.required_skills or [])],
		}
	)
	task.insert(ignore_permissions=True)
	return task.name


def _render(template: str | None, work_item) -> str:
	"""Render the Jinja prompt template against the work-item. Exposes both the
	whole doc (`{{ doc.field }}`) and its flattened fields (`{{ field }}`).
	Templates are syntax-checked at save (the transition-meta controller), so a
	render failure here is unexpected; we surface it rather than swallow it."""
	template = (template or "").strip()
	if not template:
		return ""
	return frappe.render_template(template, {"doc": work_item, **work_item.as_dict()})


def _title(work_item, meta) -> str:
	return f"{work_item.doctype} {work_item.name} — {meta.phase_key}"


def _project_of(work_item):
	"""Carry the work-item's Project onto the Task if it has one, so existing
	project rollups / War Room / Raven wiring keep working. None otherwise."""
	if work_item.meta.has_field("project"):
		return work_item.get(bundle.fields_for(work_item.doctype)["project_field"])
	return None
