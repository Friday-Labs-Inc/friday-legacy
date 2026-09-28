# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Project command-loop skills (design 60, Q7) — steer the pipeline from chat.

Four small, auditable handlers behind the dispatcher chokepoint:
  project-status  — the pipeline at a glance (states, blockers, gates)
  update-task     — complete / cancel one task (human steering via chat)
  pause-project   — park a whole pipeline (the dispatcher skips On Hold)

Like every Friday skill: no intelligence inside — validate, act on rows,
report. The agent supplies the judgment; the rows carry the audit.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.agent_runner.dispatcher import register_skill_handler


def project_status(skill_name: str, parameters: dict) -> dict:
	"""The pipeline at a glance: every task's state, mode, and blockers."""
	project = (parameters.get("project") or "").strip()
	if not project:
		raise ValueError("project-status requires a 'project' parameter")
	if not frappe.db.exists("Project", project):
		raise ValueError(f"Project {project!r} not found")

	status = frappe.db.get_value("Project", project, "status")
	tasks = frappe.get_all(
		"Task",
		filters={"project": project},
		fields=["name", "title", "workflow_state", "execution_mode", "backend_ref"],
		order_by="creation asc",
		limit_page_length=0,
	)
	lines = [f"Project {project} — status: {status}, {len(tasks)} tasks:"]
	for t in tasks:
		marker = (
			"⏳"
			if t.workflow_state in ("Pending", "Assigned")
			else (
				"▶️"
				if t.workflow_state == "Executing"
				else ("✅" if t.workflow_state == "Completed" else "⛔")
			)
		)
		lines.append(f"{marker} [{t.name}] {t.title} — {t.workflow_state} ({t.execution_mode})")
	return {"result": "\n".join(lines), "doctype": "Project", "record_name": project}


def update_task(skill_name: str, parameters: dict) -> dict:
	"""Complete or cancel one task — human steering, through the agent."""
	task_name = (parameters.get("task") or "").strip()
	action = (parameters.get("action") or "").strip().lower()
	if not task_name:
		raise ValueError("update-task requires a 'task' parameter (the Task ID)")
	if action not in ("complete", "cancel"):
		raise ValueError("update-task 'action' must be 'complete' or 'cancel'")
	if not frappe.db.exists("Task", task_name):
		raise ValueError(f"Task {task_name!r} not found")

	task = frappe.get_doc("Task", task_name)
	target = "Completed" if action == "complete" else "Cancelled"
	if task.workflow_state == target:
		return {"result": f"Task {task_name} is already {target}."}
	task.workflow_state = target
	if target == "Completed":
		task.completed_at = frappe.utils.now_datetime()
	task.save(ignore_permissions=True)
	return {
		"result": f"Task {task_name} ({task.title}) → {target}. Dependent tasks "
		f"{'unblock on the next dispatch cycle' if target == 'Completed' else 'stay parked'}.",
		"doctype": "Task",
		"record_name": task_name,
	}


def pause_project(skill_name: str, parameters: dict) -> dict:
	"""Park (or resume) a pipeline — the dispatcher skips On Hold projects."""
	project = (parameters.get("project") or "").strip()
	resume = bool(parameters.get("resume"))
	if not project:
		raise ValueError("pause-project requires a 'project' parameter")
	if not frappe.db.exists("Project", project):
		raise ValueError(f"Project {project!r} not found")

	new_status = "Open" if resume else "On Hold"
	frappe.db.set_value("Project", project, "status", new_status, update_modified=False)
	return {
		"result": f"Project {project} → {new_status}. "
		+ ("Dispatching resumes next cycle." if resume else "No further tasks will dispatch until resumed."),
		"doctype": "Project",
		"record_name": project,
	}


def list_projects(skill_name: str, parameters: dict) -> dict:
	"""List Friday Projects at a glance (the missing 'what projects exist?' verb).

	`project-status` answers about ONE named project; this lists them all so an
	agent can answer 'are there any projects' without being given a name. Optional
	`status` filter. Read-only; the Project Commander role's Project:read is the gate.
	"""
	status = (parameters.get("status") or "").strip()
	rows = frappe.get_all(
		"Project",
		filters={"status": status} if status else None,
		fields=[
			"name",
			"status",
			"priority",
			"percent_complete",
			"total_tasks",
			"completed_tasks",
			"project_lead_profile",
		],
		order_by="modified desc",
		limit=50,
	)
	if not rows:
		suffix = f" with status {status!r}" if status else ""
		return {"result": f"No projects found{suffix}.", "count": 0, "projects": []}

	lines = [f"{len(rows)} project(s):"]
	for r in rows:
		lead = f", lead {r['project_lead_profile']}" if r.get("project_lead_profile") else ""
		lines.append(
			f"- {r['name']} — {r['status']}, {r.get('percent_complete') or 0}% "
			f"({r.get('completed_tasks') or 0}/{r.get('total_tasks') or 0} tasks){lead}"
		)
	return {"result": "\n".join(lines), "count": len(rows), "projects": rows}


register_skill_handler("project-status", project_status)
register_skill_handler("update-task", update_task)
register_skill_handler("pause-project", pause_project)
register_skill_handler("list-projects", list_projects)
