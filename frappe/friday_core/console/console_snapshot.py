# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
The Project Console snapshot endpoint (design 65c, Q6 — poll fallback + seed).

The console is realtime-PUSH first (``console_stream``). This whitelisted
endpoint is the other half: it seeds the page on open and is re-polled every
30s as a correctness backstop, so a dropped realtime frame self-heals on the
next poll (the same "state is the source of truth" principle as the Design 61
reconciler — the push is the optimization, the snapshot is the truth).

One call returns everything the three console zones need:
  - ``health``         — the Pipeline Health verdict (reused verbatim from 61b;
                          the header status strip).
  - ``projects``       — per-project rollup cards (status, %-complete, counts).
  - ``active_tasks``   — what is in flight right now (Assigned / Executing).
  - ``recent_activity``— the latest terminal transitions (the seed for the feed
                          before any live event arrives).

Pass ``project`` to scope every section to one project (the per-project view).

Fail-loud envelope
------------------
If any data path errors, we log loudly and return an envelope with an ``error``
string and a ``down`` health verdict — never a silent empty "all good" page.
Mirrors ``pipeline_health``'s contract.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.console.console_stream import TERMINAL_STATES
from frappe.friday_core.health.pipeline_health import pipeline_health

ACTIVE_STATES = ("Assigned", "Executing")
RECENT_LIMIT = 20
PROJECTS_LIMIT = 50


@frappe.whitelist()
def console_snapshot(project: str | None = None) -> dict:
	"""Return the full console snapshot. Optionally scoped to one ``project``.

	Whitelisted; permission is the same low bar as ``pipeline_health`` — any
	authenticated Desk user. No PII, just operational state.
	"""
	try:
		return _build(project)
	except Exception as exc:
		frappe.log_error(title="friday.console console_snapshot failed")
		return {
			"generated_at": frappe.utils.now(),
			"error": f"{type(exc).__name__}: {str(exc)[:300]}",
			"health": {"verdict": "down", "error": "snapshot failed"},
			"projects": [],
			"active_tasks": [],
			"recent_activity": [],
		}


def _build(project: str | None) -> dict:
	task_filter = {"project": project} if project else {}

	# Merge legacy Project tiles with Design 75 work-item tiles (one set per
	# active Domain Bundle) in the unscoped view. When the user has scoped to a
	# specific Project, work-items stay out (they're a separate plane, not
	# nested under a Project).
	projects = _projects(project)
	if not project:
		projects = projects + _work_item_tiles()

	return {
		"generated_at": frappe.utils.now(),
		"health": pipeline_health(),
		"projects": projects,
		"active_tasks": _active_tasks(task_filter),
		"recent_activity": _recent_activity(task_filter),
	}


def _projects(project: str | None) -> list[dict]:
	filters = {"name": project} if project else {}
	return frappe.get_all(
		"Project",
		filters=filters,
		fields=[
			"name",
			"project_name",
			"status",
			"priority",
			"percent_complete",
			"total_tasks",
			"completed_tasks",
			"actual_cost_usd",
		],
		order_by="modified desc",
		limit=PROJECTS_LIMIT,
	)


def _work_item_tiles() -> list[dict]:
	"""Render the work-items the metadata engine has touched (Design 75) as
	project tiles — one pass per active Domain Bundle, so every domain app's
	pipeline shows up in the console without the console knowing the domain.
	Only work-items with at least one engine Task are listed; tiles are ordered
	by most-recent task activity so today's work surfaces at the top. The JS
	uses ``kind="work_item"`` + ``doctype`` to route a click to the work-item's
	form instead of trying to scope tasks by project.
	"""
	from frappe.friday_core.engine import bundle as bundle_api

	tiles: list[dict] = []
	for b in bundle_api.active_bundles():
		tiles.extend(_tiles_for_bundle(b))
	return tiles


def _tiles_for_bundle(b: dict) -> list[dict]:
	doctype = b["domain_doctype"]
	workflow = b.get("workflow_name")
	display_field = b.get("display_name_field") or "name"
	state_field = "workflow_state"
	terminal: set[str] = set()
	if workflow:
		from frappe.friday_core.engine import bundle as bundle_api

		state_field = bundle_api.state_field_for(workflow)
		terminal = bundle_api.terminal_states(workflow)

	# Single Task query: get every engine Task on this work-item doctype,
	# aggregate in Python. This both filters out work-items the engine never
	# touched (no noise) and gives us the per-item "last activity" for ordering.
	tasks = frappe.get_all(
		"Task",
		filters={"work_item_doctype": doctype},
		fields=["work_item_name", "workflow_state", "modified"],
	)
	if not tasks:
		return []

	rollup: dict[str, dict] = {}  # work-item -> {total, completed, last_activity}
	for t in tasks:
		item = t.work_item_name
		if not item:
			continue
		row = rollup.setdefault(item, {"total": 0, "completed": 0, "last_activity": t.modified})
		row["total"] += 1
		if t.workflow_state == "Completed":
			row["completed"] += 1
		if t.modified and (not row["last_activity"] or t.modified > row["last_activity"]):
			row["last_activity"] = t.modified

	# Most-recently-active first; cap at the same limit as legacy Projects.
	names = sorted(rollup, key=lambda n: rollup[n]["last_activity"], reverse=True)[:PROJECTS_LIMIT]
	fields = ["name", state_field] + ([display_field] if display_field != "name" else [])
	items = {r.name: r for r in frappe.get_all(doctype, filters={"name": ["in", names]}, fields=fields)}

	tiles: list[dict] = []
	for name in names:
		item = items.get(name)
		if not item:
			continue
		row = rollup[name]
		total, completed = row["total"], row["completed"]
		pct = round(completed / total * 100) if total else 0
		state = item.get(state_field)
		tiles.append(
			{
				"name": item.name,
				"project_name": item.get(display_field) or item.name,
				"status": "Completed" if state in terminal else "Open",
				"priority": "",
				"percent_complete": pct,
				"total_tasks": total,
				"completed_tasks": completed,
				"actual_cost_usd": 0,
				"kind": "work_item",
				"doctype": doctype,
			}
		)
	return tiles


def _active_tasks(task_filter: dict) -> list[dict]:
	return frappe.get_all(
		"Task",
		filters={**task_filter, "workflow_state": ["in", list(ACTIVE_STATES)]},
		fields=["name", "title", "project", "assigned_to_profile", "workflow_state", "started_at"],
		order_by="started_at asc",
	)


def _recent_activity(task_filter: dict) -> list[dict]:
	return frappe.get_all(
		"Task",
		filters={**task_filter, "workflow_state": ["in", list(TERMINAL_STATES)]},
		fields=[
			"name",
			"title",
			"project",
			"assigned_to_profile",
			"workflow_state",
			"modified",
			"duration_ms",
		],
		order_by="modified desc",
		limit=RECENT_LIMIT,
	)
