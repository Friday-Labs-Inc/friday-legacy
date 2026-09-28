# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Scheduled agent runs — the cron tick + completion delivery (Design 87, Slice 1).

PLAIN ENGLISH
=============

A `Cron Job` row says "run agent X with prompt P on schedule S, and deliver the
result to target T". This module is the engine:

  tick()              runs every minute (registered in hooks.py). It finds due
                      jobs, advances each job's next_run_at BEFORE spawning work
                      (so a crash skips a run rather than double-firing), and
                      spawns one Task per job — assigned to the job's profile, so
                      Friday's normal durable pipeline runs it.

  on_task_terminal()  fires when a cron-spawned Task finishes (called from the
                      Task workflow's terminal-state handler, beside report_back).
                      It delivers the Task's result via the Design 86 router to
                      the job's target, then updates the job's bookkeeping
                      (last run, completed count, repeat-limit disable).

A cron run IS a Task (Design 87, Q1) — it inherits heartbeat, the reconciler's
rescue, and the audit row for free. The only cron-specific code is scheduling
(here) and delivery-on-completion (here).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from croniter import croniter

import frappe
from frappe.friday_core.gateway.delivery import DeliveryRouter, DeliveryTarget

# How many due jobs one tick will fire. Mirrors the dispatcher's per-tick budget
# so a backlog drains steadily instead of stampeding the worker pool.
FIRE_BUDGET_PER_TICK = 10

# A reply of exactly this suppresses delivery (the run still counts) — faithful
# to Hermes' [SILENT] marker.
SILENT_MARKER = "[SILENT]"


def compute_next_run(kind: str, expr: str, base: datetime) -> datetime | None:
	"""Next fire time after `base` for a schedule. None when a one-shot is spent.

	- `cron`     — a 5-field expression, via `croniter` (Frappe's own lib).
	- `interval` — "every N minutes": `base + N min`.
	- `once`     — an ISO datetime; returns it if still in the future, else None.
	"""
	if kind == "cron":
		return croniter(expr, base).get_next(datetime)
	if kind == "interval":
		return base + timedelta(minutes=int(expr))
	if kind == "once":
		run_at = frappe.utils.get_datetime(expr)
		return run_at if run_at > base else None
	raise ValueError(f"unknown schedule_kind {kind!r}")


# ---------------------------------------------------------------------------
# The tick (every minute)
# ---------------------------------------------------------------------------


def tick() -> None:
	"""Fire every due cron job. Registered at `hooks.py scheduler_events */1`.

	Per-job failure is isolated — one bad job never blocks the rest.
	"""
	now = frappe.utils.now_datetime()
	due = frappe.get_all(
		"Cron Job",
		filters={
			"enabled": 1,
			"state": ("in", ["Scheduled", "Error"]),
			"next_run_at": ("<=", now),
		},
		fields=["name"],
		order_by="next_run_at asc",
		limit=FIRE_BUDGET_PER_TICK,
	)
	for row in due:
		try:
			_fire(row["name"], now)
		except Exception:
			frappe.logger("friday.cron").warning(f"cron fire failed for {row['name']!r}", exc_info=True)


def _fire(job_name: str, now: datetime) -> None:
	"""Advance the job's next_run_at, THEN spawn its Task (at-most-once, Q3)."""
	job = frappe.get_doc("Cron Job", job_name)

	# Advance BEFORE spawning. If the process dies between the save and the spawn,
	# the job simply skips this run instead of double-firing on the next tick.
	nxt = compute_next_run(job.schedule_kind, job.schedule_expr, now)
	job.next_run_at = nxt
	if nxt is None:
		# A spent one-shot: disable it so it never re-fires.
		job.enabled = 0
		job.state = "Completed"
	job.save(ignore_permissions=True)

	# Spawn the run as a Task assigned to the job's profile. Entering "Assigned"
	# with a profile is what makes the runner pick it up (tasks/workflow.py).
	frappe.get_doc(
		{
			"doctype": "Task",
			"title": f"[cron] {job.job_name}",
			"description": _frame_cron_prompt(job.prompt),
			"assigned_to_profile": job.agent_profile,
			"workflow_state": "Assigned",
			"execution_mode": "agentic",
			"cron_job": job.name,
		}
	).insert(ignore_permissions=True)


# Framing prepended to every cron run. Friday's cron contract is "agent GENERATES
# → the Design-86 router DELIVERS" — but a scheduled agent has no live human to
# read intent from, and a prompt phrased as "post X to the channel" makes it hunt
# for a (non-existent) posting tool and refuse. This makes the contract explicit so
# an instructional prompt produces content instead of a refusal; it's a no-op for
# an already-generative prompt. The creation-side fix (manage-cron-jobs schema)
# steers agents to write generative prompts in the first place; this is the safety
# net for the ones that don't.
_CRON_FRAMING = (
	"You are a scheduled job. Produce the requested output as your reply text. It is "
	"delivered automatically to the configured destination, so you do NOT have — and "
	"do NOT need — any tool to post, send, or message a channel; never refuse for lack "
	"of one. Just produce the content.\n\nTask: "
)


def _frame_cron_prompt(prompt: str) -> str:
	"""Wrap a job prompt so the run produces content (delivery is automatic)."""
	return f"{_CRON_FRAMING}{(prompt or '').strip()}"


# ---------------------------------------------------------------------------
# Delivery on completion (called from the Task workflow terminal handler)
# ---------------------------------------------------------------------------


def on_task_terminal(task_doc, state: str) -> None:
	"""Deliver a finished cron Task's result + update its job. No-op otherwise.

	Mirrors the report_back / write-back handlers in `tasks/workflow.py`: it
	no-ops for any task that did not come from a cron job, so it is safe to call
	on every terminal transition.
	"""
	if not task_doc.cron_job:
		return

	job = frappe.get_doc("Cron Job", task_doc.cron_job)
	now = frappe.utils.now_datetime()

	if state != "Completed":
		# A failed/cancelled cron run — record the error, don't count it as a run
		# (so it retries on its next schedule). Delivery is success-only.
		job.last_run_at = now
		job.last_status = "error"
		job.last_task = task_doc.name
		if state == "Blocked":
			job.state = "Error"
		job.save(ignore_permissions=True)
		return

	summary = _result_summary(task_doc.result)
	if summary.strip() != SILENT_MARKER:
		try:
			result = DeliveryRouter().deliver(
				summary,
				[DeliveryTarget.parse(job.deliver or "local")],
				job_id=job.name,
				job_name=job.job_name,
			)
			# A per-target failure OR a silent downgrade-to-local (e.g. an unconfigured
			# platform) does NOT raise — surface it on the row so the operator SEES that
			# the output didn't reach the intended channel, instead of a silent "ok".
			job.last_delivery_error = _delivery_issue(result)
		except Exception as exc:
			job.last_delivery_error = str(exc)
			frappe.logger("friday.cron").warning(f"cron delivery failed for {job.name!r}", exc_info=True)

	# Bookkeeping — record the run and apply the repeat limit.
	job.last_run_at = now
	job.last_status = "ok"
	job.last_task = task_doc.name
	job.completed = (job.completed or 0) + 1
	if job.repeat_times and job.completed >= job.repeat_times:
		# Spent its repeat budget. Disable + keep the row (Q5: not deleted).
		job.enabled = 0
		job.state = "Completed"
	job.save(ignore_permissions=True)


def _delivery_issue(result: dict) -> str | None:
	"""A human note if any delivery target FAILED or was silently DOWNGRADED to local
	(e.g. an unconfigured/unreachable platform) — else None.

	Stored on `Cron Job.last_delivery_error` so a misroute is visible to the operator,
	not buried under a green `last_status="ok"`.
	"""
	notes = []
	for key, r in (result or {}).items():
		if not r.get("success"):
			notes.append(f"{key}: {r.get('error', 'delivery failed')}")
		elif r.get("downgraded"):
			notes.append(f"{key} → saved locally ({r.get('reason', 'downgraded')})")
	return "; ".join(notes) or None


def _result_summary(result: str | None) -> str:
	"""Pull the agent's reply out of a Task's `result` JSON envelope."""
	if not result:
		return ""
	try:
		return (json.loads(result) or {}).get("summary", "") or ""
	except ValueError, TypeError:
		return result
