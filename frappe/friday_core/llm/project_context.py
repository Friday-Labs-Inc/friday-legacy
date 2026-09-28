# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Project status snapshot — what the orchestrator sees when it's standing in a
project room (Design 73, Slice 3).

PLAIN ENGLISH
=============
When a human talks to Friday inside a project's chat room, the agent should
already know where that project stands — its status, what work is still open,
and what's been delivered — without having to ask or go digging. This module
builds that snapshot as ONE system message, injected fresh on every turn by the
prompt builder.

It is the richer successor to the name-only "PROJECT CONTEXT" line that shipped
with project-scoped memory: same placement, same anti-bleed scoping instruction
(so one client's room never pulls in another's details), now carrying live
state.

Truth still lives in Friday's doctypes (Project, Task, and the deliverable
Files materialized by Slice 5); this only *reads* them. A snapshot is a
projection of the record, never the record itself — consistent with the
thin-skin spine in design 73.
"""

from __future__ import annotations

import frappe

# Non-terminal task states — "open" work the orchestrator still owns. Completed
# and Cancelled are terminal (done / abandoned) and stay out of the open list.
_OPEN_STATES = ("Pending", "Assigned", "Executing", "Review", "Blocked")

# Keep the snapshot bounded — a 200-task project must not blow up every prompt.
_MAX_OPEN_TASKS = 20
_MAX_DELIVERABLES = 8


def project_snapshot_block(project: str) -> "str | None":
	"""One system-message block describing a project's current state.

	Returns None when the project no longer exists — a snapshot must never break
	a turn. The block leads with the same scoping instruction the name-only block
	used, then a CURRENT STATE section: status + progress, open tasks, and
	deliverables.
	"""
	row = frappe.db.get_value(
		"Project",
		project,
		["project_name", "status", "percent_complete", "total_tasks", "completed_tasks"],
		as_dict=True,
	)
	if not row:
		return None

	name = row.get("project_name") or project
	lines = [
		(
			f"PROJECT CONTEXT: This conversation is the dedicated room for project "
			f"'{name}' ({project}). Scope every answer to THIS project. Treat 'updates', "
			f"'status', and 'the brief' as referring to this project. Do NOT reference or "
			f"pull in other projects' details unless explicitly asked."
		),
		"",
		(
			"CURRENT STATE (snapshot at the start of this turn — read-only background, "
			"may change as work proceeds):"
		),
		f"- Status: {row.get('status') or 'Unknown'}{_progress_suffix(row)}",
		_open_tasks_section(project),
		_deliverables_section(project),
	]
	return "\n".join(lines)


def _progress_suffix(row) -> str:
	"""'(40% complete, 2/5 tasks done)' — or '' when no rollup exists yet."""
	total = row.get("total_tasks") or 0
	done = row.get("completed_tasks") or 0
	pct = row.get("percent_complete")
	if not total and pct is None:
		return ""
	pct_txt = f"{int(pct)}%" if pct is not None else "—"
	return f" ({pct_txt} complete, {done}/{total} tasks done)"


def _open_tasks_section(project: str) -> str:
	"""The non-terminal tasks, newest-work-last, capped and labelled by state."""
	tasks = frappe.get_all(
		"Task",
		filters={"project": project, "workflow_state": ("in", _OPEN_STATES)},
		fields=["title", "workflow_state"],
		order_by="creation asc",
		limit=_MAX_OPEN_TASKS + 1,
	)
	if not tasks:
		return "- Open tasks: none"
	out = ["- Open tasks:"]
	for task in tasks[:_MAX_OPEN_TASKS]:
		title = (task.get("title") or "(untitled)").strip()
		out.append(f"    - [{task.get('workflow_state')}] {title}")
	if len(tasks) > _MAX_OPEN_TASKS:
		out.append(f"    - …and more (showing the first {_MAX_OPEN_TASKS})")
	return "\n".join(out)


def _deliverables_section(project: str) -> str:
	"""The deliverable artifact filenames — the project package + per-task files."""
	names = _deliverable_file_names(project)
	if not names:
		return "- Deliverables: none yet"
	shown = names[:_MAX_DELIVERABLES]
	extra = len(names) - _MAX_DELIVERABLES
	suffix = "" if extra <= 0 else f" (+{extra} more)"
	return "- Deliverables: " + ", ".join(shown) + suffix


def _deliverable_file_names(project: str) -> list:
	"""Deliverable filenames: the project package plus per-task md/pdf files.

	Deliverables are Files named ``deliverable-*`` attached to the Project (the
	assembled package) or to its Tasks (per-task artifacts), materialized by
	Slice 5. We read filenames only — never the bytes.
	"""
	names: list = [
		f.file_name
		for f in frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Project",
				"attached_to_name": project,
				"file_name": ("like", "deliverable-%"),
			},
			fields=["file_name"],
			order_by="creation desc",
		)
	]
	task_names = [t.name for t in frappe.get_all("Task", filters={"project": project}, fields=["name"])]
	if task_names:
		names += [
			f.file_name
			for f in frappe.get_all(
				"File",
				filters={
					"attached_to_doctype": "Task",
					"attached_to_name": ("in", task_names),
					"file_name": ("like", "deliverable-%"),
				},
				fields=["file_name"],
				order_by="creation desc",
				limit=_MAX_DELIVERABLES + 1,
			)
		]
	return names
