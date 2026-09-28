# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Dispatcher Console backend API (Design 72).

Three whitelisted endpoints feed the page:

- ``pulse()`` — returns the 6 Pulse cells in one call (scheduler tick,
  reconciler last sweep, active leases, dispatchable, RQ queues, workers).
- ``lifecycle_trace(task)`` — returns every Dispatcher Event for the given
  task ordered by creation, with cursor-paged tail support.
- ``recent_tasks(limit)`` — feeds the task-picker dropdown with the most
  recently active tasks.

All three are read-only and gated by System Manager (Q9 of Design 72).
The 2-second polling endpoint stays cheap by computing each cell from a
single query / Redis check.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_to_date, now_datetime, time_diff_in_seconds


def _require_system_manager() -> None:
	"""Q9 — Dispatcher Console is admin-only."""
	if "System Manager" not in frappe.get_roles():
		frappe.throw("Dispatcher Console is System Manager only.", frappe.PermissionError)


# ---------------------------------------------------------------------------
# Pulse — six cells
# ---------------------------------------------------------------------------


@frappe.whitelist()
def pulse() -> dict:
	"""Return all 6 Pulse cells in one payload (Q7 of Design 72)."""
	_require_system_manager()
	return {
		"scheduler": _cell_scheduler(),
		"reconciler": _cell_reconciler(),
		"active_leases": _cell_active_leases(),
		"dispatchable": _cell_dispatchable(),
		"queues": _cell_queues(),
		"workers": _cell_workers(),
		"generated_at": str(now_datetime()),
	}


def _cell_scheduler() -> dict:
	"""Cell 1 — scheduler last tick + age. Red if >5 min stale."""
	try:
		# Read Max(Scheduled Job Type.last_execution). Same query pattern as
		# pipeline_health._scheduler_tick_age — avoids the sparse
		# Scheduled Job Log table that misled the Project Console earlier.
		row = frappe.db.sql(
			"SELECT MAX(last_execution) AS last FROM `tabScheduled Job Type` WHERE last_execution IS NOT NULL"
		)
		last = row[0][0] if row and row[0] else None
		age_sec = int(time_diff_in_seconds(now_datetime(), last)) if last else None
		return {
			"headline": "stale" if (age_sec is None or age_sec > 300) else "live",
			"last": str(last) if last else "(never)",
			"age_seconds": age_sec,
			"status": "red" if (age_sec is None or age_sec > 300) else "green",
		}
	except Exception as exc:
		return {"headline": "error", "last": str(exc)[:80], "age_seconds": None, "status": "red"}


def _cell_reconciler() -> dict:
	"""Cell 2 — last reconciler.tick event with phase action counts."""
	try:
		rows = frappe.db.sql(
			"SELECT creation, summary, payload_json FROM `tabDispatcher Event` "
			"WHERE event_type = 'reconciler.tick' "
			"ORDER BY creation DESC LIMIT 1",
			as_dict=True,
		)
		if not rows:
			return {"headline": "no ticks recorded yet", "status": "amber"}
		row = rows[0]
		age_sec = int(time_diff_in_seconds(now_datetime(), row["creation"]))
		try:
			payload = json.loads(row["payload_json"] or "{}")
		except Exception:
			payload = {}
		actions = payload.get("phase_actions") or {}
		total_actions = sum(int(v) for v in actions.values()) if isinstance(actions, dict) else 0
		errors = payload.get("phase_errors") or []
		status = "red" if errors else ("green" if age_sec < 180 else "amber")
		return {
			"headline": row["summary"] or "tick",
			"last": str(row["creation"]),
			"age_seconds": age_sec,
			"total_actions": total_actions,
			"errors": errors,
			"status": status,
		}
	except Exception as exc:
		return {"headline": "error", "last": str(exc)[:80], "status": "red"}


def _cell_active_leases() -> dict:
	"""Cell 3 — Executing tasks + stalest heartbeat age."""
	try:
		rows = frappe.db.sql(
			"""
			SELECT name, last_heartbeat_at
			FROM `tabTask`
			WHERE workflow_state = 'Executing'
			""",
			as_dict=True,
		)
		count = len(rows)
		stalest_age = None
		stalest_name = None
		for r in rows:
			if r["last_heartbeat_at"]:
				age = int(time_diff_in_seconds(now_datetime(), r["last_heartbeat_at"]))
				if stalest_age is None or age > stalest_age:
					stalest_age = age
					stalest_name = r["name"]
		status = "green"
		if count == 0:
			status = "idle"
		elif stalest_age is not None and stalest_age > 300:
			status = "red"
		return {
			"count": count,
			"stalest_age_seconds": stalest_age,
			"stalest_task": stalest_name,
			"status": status,
		}
	except Exception as exc:
		return {"count": None, "status": "red", "error": str(exc)[:80]}


def _cell_dispatchable() -> dict:
	"""Cell 4 — count of dispatchable-but-unclaimed tasks + oldest age."""
	try:
		rows = frappe.db.sql(
			"""
			SELECT name, creation
			FROM `tabTask`
			WHERE dispatchable = 1
			  AND assigned_to_profile IS NULL
			  AND workflow_state = 'Pending'
			ORDER BY creation ASC
			LIMIT 1
			""",
			as_dict=True,
		)
		count = frappe.db.count(
			"Task",
			{
				"dispatchable": 1,
				"assigned_to_profile": ["is", "not set"],
				"workflow_state": "Pending",
			},
		)
		oldest_age = None
		oldest_name = None
		if rows:
			oldest_age = int(time_diff_in_seconds(now_datetime(), rows[0]["creation"]))
			oldest_name = rows[0]["name"]
		status = "green"
		if count == 0:
			status = "idle"
		elif oldest_age and oldest_age > 600:
			status = "amber"
		return {
			"count": count,
			"oldest_age_seconds": oldest_age,
			"oldest_task": oldest_name,
			"status": status,
		}
	except Exception as exc:
		return {"count": None, "status": "red", "error": str(exc)[:80]}


def _cell_queues() -> dict:
	"""Cell 5 — RQ default + friday queue depths + worker counts."""
	try:
		from rq import Queue

		from frappe.utils.background_jobs import get_redis_conn

		conn = get_redis_conn()
		out = {}
		for qname in ("default", "friday"):
			try:
				q = Queue(f"{frappe.local.site}:{qname}", connection=conn)
				out[qname] = {"depth": q.count}
			except Exception:
				out[qname] = {"depth": None}
		out["status"] = "green"
		return out
	except Exception as exc:
		return {"status": "red", "error": str(exc)[:80]}


def _cell_workers() -> dict:
	"""Cell 6 — Agent Profile up/down (mirrors the existing 'friday worker: up' line)."""
	try:
		profiles = frappe.get_all(
			"Agent Profile",
			filters={"status": "Active"},
			fields=["name", "status"],
			limit=20,
		)
		return {"profiles": profiles, "status": "green" if profiles else "amber"}
	except Exception as exc:
		return {"profiles": [], "status": "red", "error": str(exc)[:80]}


# ---------------------------------------------------------------------------
# Lifecycle Trace
# ---------------------------------------------------------------------------


@frappe.whitelist()
def lifecycle_trace(task: str, since_cursor: "str | None" = None, limit: int = 200) -> dict:
	"""Return Dispatcher Event rows for ``task`` ordered ascending by creation.

	Args:
		task: Task name.
		since_cursor: ISO datetime string. When given, only events strictly
			NEWER than this are returned (used by the 2 s tail polling).
		limit: Max rows to return per call.

	Returns dict with ``events`` (list of rows), ``next_cursor`` (latest creation
	we returned, or the prior cursor if empty), ``task_state`` (current state +
	flags so the UI can decide to stop tailing on terminal), and ``summary`` (the
	Task Completion Summary row if one exists).
	"""
	_require_system_manager()
	limit = max(1, min(int(limit), 500))

	filters = {"task": task}
	if since_cursor:
		filters["creation"] = [">", since_cursor]

	events = frappe.get_all(
		"Dispatcher Event",
		filters=filters,
		fields=[
			"name",
			"creation",
			"event_type",
			"trigger_source",
			"agent_profile",
			"project",
			"summary",
			"payload_json",
		],
		order_by="creation asc",
		limit=limit,
	)

	next_cursor = str(events[-1]["creation"]) if events else (since_cursor or "")
	for e in events:
		# coerce payload to dict for the JS. Frappe's JSON field handling is
		# inconsistent — get_all may return the value as a JSON string OR an
		# already-parsed dict depending on version + cache state. Handle both.
		raw = e.pop("payload_json", None)
		if raw is None:
			e["payload"] = {}
		elif isinstance(raw, dict):
			e["payload"] = raw
		else:
			try:
				e["payload"] = json.loads(raw)
			except Exception:
				e["payload"] = {"_parse_error": True, "raw": str(raw)}

	task_state = {}
	try:
		t = frappe.db.get_value(
			"Task",
			task,
			[
				"workflow_state",
				"assigned_to_profile",
				"executing_token",
				"dispatchable",
				"blocked_reason",
				"retry_count",
				"project",
			],
			as_dict=True,
		)
		if t:
			task_state = dict(t)
			task_state["is_live"] = t.workflow_state in ("Pending", "Assigned", "Executing")
	except Exception:
		pass

	summary = None
	try:
		if frappe.db.exists("Task Completion Summary", task):
			summary = frappe.db.get_value(
				"Task Completion Summary",
				task,
				[
					"final_state",
					"total_events",
					"total_llm_cost_usd",
					"total_duration_ms",
					"blocked_reason",
					"terminating_issue",
					"profiles_touched",
					"completed_at",
				],
				as_dict=True,
			)
	except Exception:
		pass

	return {
		"events": events,
		"next_cursor": next_cursor,
		"task_state": task_state,
		"summary": summary,
	}


@frappe.whitelist()
def recent_tasks(limit: int = 20) -> list[dict]:
	"""Recent tasks for the picker dropdown — most-recent activity first."""
	_require_system_manager()
	limit = max(1, min(int(limit), 50))
	# Order by modified DESC — gives the most recently touched tasks across all
	# projects, which is what the operator usually wants to inspect.
	return frappe.get_all(
		"Task",
		fields=["name", "title", "workflow_state", "project", "modified"],
		order_by="modified desc",
		limit=limit,
	)
