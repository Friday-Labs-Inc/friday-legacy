# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Retention sweep + Task Completion Summary writer (Design 72, Q6).

Two responsibilities:

1. **purge_old_events()** — daily scheduler job that deletes ``Dispatcher Event``
   rows older than 30 days, in 1000-row batches with explicit commits between
   batches so a busy table doesn't lock under one giant DELETE.

2. **write_task_completion_summary(task)** — called from the workflow hook on
   every terminal state transition (Completed / Cancelled / Blocked-non-transient).
   Writes one permanent ``Task Completion Summary`` row capturing the compact
   audit: final state, event count, total LLM cost, duration, blocked reason,
   profiles that touched the task. The summary outlives the 30-day raw event
   purge so a project's history is never irrecoverably gone.

Both functions are savepoint-guarded and never raise — they are bookkeeping,
not load-bearing logic.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime

_logger = frappe.logger("friday.observability.retention")

RETENTION_DAYS = 30
PURGE_BATCH_SIZE = 1000
PURGE_MAX_BATCHES = 100  # safety cap: at most 100,000 rows per sweep

# Design 93 (Q6) — Turn Event rows are REPLAY STATE, not audit trail (that
# stays in Execution Log / LLM Usage Log / Chat Message). By 7 days every
# turn is long since finished or given up on, so the diary can go.
TURN_EVENT_RETENTION_DAYS = 7


# Terminal states for the Task Completion Summary writer. A task reaching any
# of these is "done" enough to lock its summary; a Blocked task that the
# reconciler later re-Pends will overwrite (the summary is upserted by
# `task` Link uniqueness, so a re-block writes a fresh row).
TERMINAL_STATES = frozenset({"Completed", "Cancelled"})

# Only "non-transient" Blocked tasks get a summary — a task blocked on
# `timeout`/`rate_limit`/etc. is expected to retry, so writing a summary
# now would be premature. The reconciler's TRANSIENT_BLOCKED_REASONS
# enumerates these; we conservatively treat anything in that set as
# "still moving" and skip the summary.
_TRANSIENT_BLOCK_REASONS = frozenset(
	("oom", "timeout", "runner_lost", "rate_limit", "overloaded", "server_error")
)


def is_terminal(doc) -> bool:
	"""True if this task's current state should write a Task Completion Summary."""
	state = doc.workflow_state
	if state in TERMINAL_STATES:
		return True
	if state == "Blocked" and (doc.blocked_reason or "") not in _TRANSIENT_BLOCK_REASONS:
		return True
	return False


def write_task_completion_summary(doc) -> "str | None":
	"""Upsert one ``Task Completion Summary`` for a terminal task.

	Called from the workflow hook on every terminal state transition. Idempotent
	(re-runs overwrite) — important because a task may transition into a
	terminal state, then a reconciler re-Pend, then back to terminal.

	Savepoint-guarded; never raises.
	"""
	try:
		frappe.db.savepoint("dispatcher_summary")

		task_name = doc.name
		# Count events; sum cost; collect profiles. These are derived; if any
		# fails, fall back to the doc fields.
		try:
			total_events = frappe.db.count("Dispatcher Event", {"task": task_name})
		except Exception:
			total_events = 0
		try:
			profile_rows = frappe.db.sql(
				"SELECT DISTINCT agent_profile FROM `tabDispatcher Event` "
				"WHERE task = %s AND agent_profile IS NOT NULL AND agent_profile != ''",
				(task_name,),
				as_dict=True,
			)
			profiles = [r["agent_profile"] for r in profile_rows]
		except Exception:
			profiles = []
			if doc.get("assigned_to_profile"):
				profiles = [doc.assigned_to_profile]

		summary_payload = {
			"task": task_name,
			"project": doc.get("project"),
			"final_state": doc.workflow_state,
			"total_events": total_events,
			"total_llm_cost_usd": doc.get("cost_usd"),
			"total_duration_ms": doc.get("duration_ms"),
			"blocked_reason": doc.get("blocked_reason"),
			"profiles_touched": json.dumps(profiles) if profiles else None,
			"completed_at": doc.get("completed_at") or now_datetime(),
		}

		existing = frappe.db.exists("Task Completion Summary", task_name)
		if existing:
			# Upsert: update the existing row in place — preserves the original
			# auditing key without churning rows.
			for k, v in summary_payload.items():
				if k != "task":
					frappe.db.set_value("Task Completion Summary", task_name, k, v, update_modified=True)
			return task_name

		row = frappe.get_doc({"doctype": "Task Completion Summary", **summary_payload})
		row.insert(ignore_permissions=True)
		return row.name
	except Exception:
		try:
			frappe.db.rollback(save_point="dispatcher_summary")
		except Exception:
			pass
		_logger.warning(
			"write_task_completion_summary failed for task %s — swallowed",
			doc.name,
			exc_info=True,
		)
		return None


def purge_old_events() -> int:
	"""Delete Dispatcher Event rows older than ``RETENTION_DAYS``.

	Runs daily via the scheduler (wired in ``hooks.py``). Deletes in batches
	of ``PURGE_BATCH_SIZE`` with explicit commits between batches so the
	table is never held under a long lock. Returns the total rows deleted.

	Never raises — a failed sweep just logs and exits. The next day's run
	tries again.
	"""
	cutoff = frappe.utils.add_to_date(now_datetime(), days=-RETENTION_DAYS)
	total_deleted = 0
	try:
		for _ in range(PURGE_MAX_BATCHES):
			rows = frappe.db.sql(
				"""
				DELETE FROM `tabDispatcher Event`
				WHERE name IN (
					SELECT name FROM `tabDispatcher Event`
					WHERE creation < %(cutoff)s
					LIMIT %(batch)s
				)
				""",
				{"cutoff": cutoff, "batch": PURGE_BATCH_SIZE},
			)
			# rows is empty tuple from DELETE; affected count is in cursor.rowcount
			deleted = frappe.db._cursor.rowcount or 0
			total_deleted += deleted
			frappe.db.commit()
			if deleted < PURGE_BATCH_SIZE:
				break
		_logger.info("purge_old_events: deleted %d rows older than %s", total_deleted, cutoff)
	except Exception:
		_logger.exception("purge_old_events failed at batch %d", total_deleted)
		# Don't re-raise — a failed sweep is logged, not a worker crash.

	# Design 93 — same batched sweep for the turn journal, shorter window.
	turn_cutoff = frappe.utils.add_to_date(now_datetime(), days=-TURN_EVENT_RETENTION_DAYS)
	try:
		for _ in range(PURGE_MAX_BATCHES):
			frappe.db.sql(
				"""
				DELETE FROM `tabTurn Event`
				WHERE name IN (
					SELECT name FROM `tabTurn Event`
					WHERE creation < %(cutoff)s
					LIMIT %(batch)s
				)
				""",
				{"cutoff": turn_cutoff, "batch": PURGE_BATCH_SIZE},
			)
			deleted = frappe.db._cursor.rowcount or 0
			total_deleted += deleted
			frappe.db.commit()
			if deleted < PURGE_BATCH_SIZE:
				break
	except Exception:
		_logger.exception("purge_old_events (Turn Event) failed")
	return total_deleted
