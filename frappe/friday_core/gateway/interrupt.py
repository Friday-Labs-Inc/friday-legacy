# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
The interrupt flag — a tiny cross-process signal to stop a running turn.

PLAIN ENGLISH
=============

A conversational turn runs inside a worker process. The operator's `/stop`
arrives in a *different* process (the Raven hook / web request). They can't call
each other directly, so they talk through one Redis key per session:

    friday:interrupt:{session_id}

`/stop` sets the key (`request_interrupt`); the running turn's ReAct loop checks
it once per iteration (`is_interrupt_requested`) and, if set, stops cleanly. The
key carries a short TTL so a `/stop` that lands when nothing is running never
wedges a future turn — and `run_turn` also clears it once at entry (Design 83,
Q3) so only a flag set *during* the current turn is ever honored. The session
lock guarantees one turn per session at a time, which is what makes
clear-at-entry sufficient (no Hermes-style generation counter needed).

This is the cooperative half of the interrupt port (Design 83, Q2). It lands at
ReAct iteration boundaries, not mid-LLM-call — Friday's provider call is one
blocking request, so the soonest a turn can notice is after the current call
returns. A hard kill (`send_stop_job_command`) is a named follow-up.
"""

from __future__ import annotations

from rq.command import send_stop_job_command

import frappe
from frappe.utils.background_jobs import get_queue

# One Redis key per session. Matches the session-lock keyspace convention.
_INTERRUPT_PREFIX = "friday:interrupt:"

# TTL backstop: a flag nobody consumes self-expires, so a stray `/stop` can never
# silently kill a turn that starts minutes later. Generous enough to outlive one
# in-flight LLM call (the worst-case latency before the loop checks it).
_INTERRUPT_TTL_SECONDS = 120


def _key(session_id: str) -> str:
	return f"{_INTERRUPT_PREFIX}{session_id}"


def request_interrupt(session_id: str) -> None:
	"""Ask the running turn for this session to stop (set by `/stop`)."""
	frappe.cache().set_value(_key(session_id), "1", expires_in_sec=_INTERRUPT_TTL_SECONDS)


def is_interrupt_requested(session_id: str) -> bool:
	"""True when an interrupt is pending for this session.

	Best-effort: a cache hiccup degrades to "no interrupt" rather than breaking
	the turn — same posture as the heartbeat/compression/usage callsites. The
	turn keeps running; the operator can `/stop` again.
	"""
	try:
		return bool(frappe.cache().get_value(_key(session_id)))
	except Exception:
		return False


def clear_interrupt(session_id: str) -> None:
	"""Drop the flag — called at turn entry (Q3) and once an interrupt is honored.

	Best-effort: a cache failure here must never break the turn.
	"""
	try:
		frappe.cache().delete_value(_key(session_id))
	except Exception:
		pass


# ---------------------------------------------------------------------------
# Cascade to delegated work (Design 85)
# ---------------------------------------------------------------------------

# The non-terminal Task states — a task in any of these is "active" and worth
# stopping. Matches `skills/handlers_delegate._ACTIVE_STATES`.
_ACTIVE_STATES = ("Pending", "Assigned", "Executing", "Blocked")


def collect_active_subtree(session_id: str) -> list[str]:
	"""Active Task names delegated under this session (descendants included).

	Roots are tasks this session spawned (`Task.originating_session`); descendants
	follow `Task.parent_task`. Only `_ACTIVE_STATES` tasks are collected — a
	finished task has nothing to stop. No schema change: both links already exist
	(`handlers_delegate.py:132,138`).
	"""
	roots = frappe.get_all(
		"Task",
		filters={"originating_session": session_id, "workflow_state": ("in", _ACTIVE_STATES)},
		pluck="name",
	)
	seen: list[str] = []
	frontier = list(roots)
	while frontier:
		name = frontier.pop()
		if name in seen:
			continue
		seen.append(name)
		children = frappe.get_all(
			"Task",
			filters={"parent_task": name, "workflow_state": ("in", _ACTIVE_STATES)},
			pluck="name",
		)
		frontier.extend(children)
	return seen


def cascade_interrupt(session_id: str) -> int:
	"""Stop every active delegated task under this session. Returns the count.

	For each node: set the cooperative interrupt flag on its `task::{name}`
	session (a running turn honors it at its next ReAct boundary, then the Task
	path marks it Cancelled). A node that is NOT currently executing has no turn
	to interrupt, so it is cancelled directly here so it never starts.
	"""
	names = collect_active_subtree(session_id)
	for name in names:
		request_interrupt(f"task::{name}")
		if frappe.db.get_value("Task", name, "workflow_state") != "Executing":
			_cancel_task(name)
	return len(names)


def _cancel_task(task_name: str) -> None:
	"""Mark a not-yet-running task Cancelled/interrupted. Best-effort, per task.

	Direct `set_value` (no workflow hook) keeps the cascade robust — one task's
	failure can't abort the rest, and it can't poison the surrounding txn.
	"""
	try:
		frappe.db.set_value(
			"Task",
			task_name,
			{"workflow_state": "Cancelled", "blocked_reason": "interrupted"},
			update_modified=False,
		)
	except Exception:
		frappe.logger("friday.interrupt").warning(f"cascade cancel failed for {task_name!r}", exc_info=True)


# ---------------------------------------------------------------------------
# Hard kill — `/stop force` (Design 83b)
# ---------------------------------------------------------------------------

# `FORCE_KILLED` is the operator-killed terminal Task state (Design 83b). It is
# set ONLY by an operator `/stop force` — the reconciler's stale-Executing
# auto-heal (Blocked → re-Pend → retry) is deliberately left untouched, so a
# transient runner loss still self-heals.
FORCE_KILLED_STATE = "ForceKilled"
_FRIDAY_QUEUE = "friday"


def force_kill_session(session_id: str, user: str) -> dict:
	"""Hard-kill the in-flight turn(s) for a session. Returns an audit summary.

	Cooperative `/stop` (Design 83/85) waits for the next ReAct boundary — useless
	when a turn is wedged inside one long tool/LLM call. `/stop force` SIGTERMs the
	running RQ job(s) instead, via rq's `send_stop_job_command` (the REAL primitive
	— `Job.cancel()` only dequeues a not-yet-started job, it can't stop a running
	one).

	What it does:
	  - **the chat turn** — SIGTERMs its RQ job. The job id is stored RAW on the
	    latest unprocessed inbound `Chat Message` (`friday-gw::ROW::lockN`); rq
	    keys it under `create_job_id()` (site-prefixed, `:`→`|`), so we MUST apply
	    that transform before `send_stop_job_command` or it targets a non-existent
	    key (the bug AWS caught: "0 jobs cancelled").
	  - **delegated tasks** — these are enqueued with `job_name` (not `job_id`), so
	    frappe gives them a random-UUID rq id that is NOT reconstructable from the
	    task name. They therefore can't be SIGTERM'd by id. Instead we raise the
	    **cooperative interrupt flag** on each task's `task::{name}` session (the
	    horse bails at its next boundary) and mark the row `ForceKilled` + audit.
	    `_run_task_agentic` honours `force_killed_by` so the interrupted task lands
	    in `ForceKilled`, not `Cancelled`/`Completed`.

	Idempotent: a job that already finished is a counted no-op, never an error.
	"""
	from frappe.utils.background_jobs import create_job_id

	result = {"jobs_cancelled": 0, "jobs_already_done": 0, "tasks_now_forcekilled": []}

	# Also raise the cooperative flag on the chat session: if the turn reaches a
	# boundary before the SIGTERM lands, it bails cleanly and releases its lock.
	request_interrupt(session_id)

	chat_job = _latest_running_job(session_id)
	if chat_job:
		# Apply frappe's rq-id transform — `send_stop_job_command` needs the real key.
		_record_stop(create_job_id(chat_job), result)

	now = frappe.utils.now_datetime()
	for task_name in collect_active_subtree(session_id):
		# Task rq jobs aren't addressable by id (UUID) — stop them cooperatively.
		request_interrupt(f"task::{task_name}")
		# Mark ForceKilled + audit now; the task path won't override it (it checks
		# force_killed_by). A not-yet-running task is simply terminal and skipped.
		frappe.db.set_value(
			"Task",
			task_name,
			{
				"workflow_state": FORCE_KILLED_STATE,
				"force_killed_by": user,
				"force_killed_at": now,
				"force_kill_reason": "operator /stop force",
			},
			update_modified=False,
		)
		result["tasks_now_forcekilled"].append(task_name)

	_emit_force_kill(session_id, user, result)
	return result


def _latest_running_job(session_id: str) -> str | None:
	"""The RQ job id of the session's still-running chat turn, if any."""
	rows = frappe.get_all(
		"Chat Message",
		filters={"session_id": session_id, "direction": "inbound", "processed": 0},
		fields=["job_id"],
		order_by="creation desc",
		limit=1,
	)
	return (rows[0].get("job_id") if rows else None) or None


def _record_stop(job_id: str, result: dict) -> None:
	"""SIGTERM one running RQ job by id; tally cancelled vs already-done."""
	try:
		send_stop_job_command(get_queue(_FRIDAY_QUEUE).connection, job_id)
		result["jobs_cancelled"] += 1
	except Exception:
		# Not running / already finished / unknown id — idempotent no-op.
		result["jobs_already_done"] += 1


def _emit_force_kill(session_id: str, user: str, result: dict) -> None:
	"""Write one `gateway.force_kill` audit event. Best-effort."""
	try:
		from frappe.friday_core.observability import emit

		emit(
			"gateway.force_kill",
			trigger_source="operator",
			summary=(
				f"/stop force by {user}: {result['jobs_cancelled']} job(s) cancelled, "
				f"{len(result['tasks_now_forcekilled'])} task(s) ForceKilled"
			),
			payload={"session_id": session_id, "operator": user, **result},
		)
	except Exception:
		frappe.logger("friday.interrupt").warning("force_kill emit failed", exc_info=True)
