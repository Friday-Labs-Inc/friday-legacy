# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
The Pipeline Health endpoint (design 61b, Q3 LOCKED — strict thresholds).

The single signal that answers "is the autonomous loop alive?" — for the
operator opening the Desk page, AND for the AI watching the same data
through this whitelisted method. Same source of truth, same verdict, no
more "all settings seem fine" while a task is dead for four hours.

The verdict (Q3 LOCKED):
  - down     : no scheduler tick in 5 min, OR friday worker absent,
               OR any stuck.* > 0 (the reconciler will heal, but the
               operator should know it's happening).
  - degraded : pending Tasks > 10, OR open Friday Issues > 5.
  - ok       : the loop is running cleanly.

Strict on purpose. The disease this design treats is silent reassurance.
"""

from __future__ import annotations

import frappe

# Q3 LOCKED thresholds — Strict.
SCHEDULER_TICK_MAX_AGE_SECONDS = 5 * 60
PENDING_DEGRADED_THRESHOLD = 10
OPEN_ISSUES_DEGRADED_THRESHOLD = 5

# Queues that matter for verdicts. The runner runs on `friday`; the dispatcher
# itself is a scheduled job, but its enqueues hit `friday` too.
CRITICAL_QUEUES = ("friday",)


@frappe.whitelist()
def pipeline_health() -> dict:
	"""
	Return a structured health snapshot.

	Whitelisted so the Desk page and the agent's own health-check skill (a
	future toolbox slice) both read it. Permission: any authenticated user
	— no PII here, just operational state.
	"""
	# Fail-loud envelope: if ANY of the data-gathering paths blow up, we
	# return verdict='down' with the error string. NEVER silently 'ok'.
	try:
		return _build_snapshot()
	except Exception as exc:
		frappe.log_error(title="friday.health pipeline_health failed")
		return {
			"verdict": "down",
			"error": f"{type(exc).__name__}: {str(exc)[:300]}",
		}


def _build_snapshot() -> dict:
	tick_age = _scheduler_tick_age()
	inflight = _inflight_jobs_by_queue()

	workers = {
		"default": {"present": "default" in inflight, "inflight": inflight.get("default", 0)},
		"friday": {"present": "friday" in inflight, "inflight": inflight.get("friday", 0)},
	}

	tasks_by_state = {
		state: frappe.db.count("Task", {"workflow_state": state})
		for state in ("Pending", "Assigned", "Executing", "Blocked", "Review", "Completed", "Cancelled")
	}

	# "Stuck" mirrors what the reconciler will actually act on next tick — not
	# just the raw state count. A healthy Executing task heartbeats, so we
	# only count Executing rows whose heartbeat is older than the reconciler's
	# 15-minute stale grace. Same for Assigned (executing_token + 30s grace).
	# This is the difference between "you have work in flight" (good) and
	# "you have stale work the reconciler will sweep" (the actual alert).
	from frappe.friday_core.tasks.reconciler import (
		ASSIGNED_GRACE_SECONDS,
		EXECUTING_STALE_MINUTES,
		TRANSIENT_BLOCKED_REASONS,
	)

	assigned_cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-ASSIGNED_GRACE_SECONDS)
	executing_cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-EXECUTING_STALE_MINUTES)
	stuck = {
		"assigned_orphaned": frappe.db.count(
			"Task",
			{
				"workflow_state": "Assigned",
				"assigned_at": ("<", assigned_cutoff),
			},
		),
		"executing_stale": frappe.db.count(
			"Task",
			{
				"workflow_state": "Executing",
				"last_heartbeat_at": ("<", executing_cutoff),
			},
		),
		"transient_blocked_pending_retry": frappe.db.count(
			"Task",
			{
				"workflow_state": "Blocked",
				"blocked_reason": ("in", TRANSIENT_BLOCKED_REASONS),
			},
		),
	}

	connectors = {
		"events_received_pending": frappe.db.count("Connector Event", {"status": "Received"}),
		"events_failed_retriable": frappe.db.count("Connector Event", {"status": "Failed"}),
	}

	# Raven is a REQUIRED part of the platform, not an optional surface: it is
	# Friday's chat front door (bot identity, project channels, the war room).
	# Frappe can't express "a fork requires an app", so the health check is where
	# a missing Raven has to be loud — otherwise it degrades into silence
	# (channels never provision, the war room swallows every post).
	surfaces = {"raven_installed": bool(frappe.db.table_exists("Raven Channel"))}
	# Raven is the SURFACE; Friday is the engine (locked — see design 58). Raven
	# ships its own agent runtime whose bots write to Frappe documents with no
	# permission matrix, no Execution Log and no approval gate. If an operator
	# switches it on, half the site's AI writes leave the audit trail — so the
	# health strip reports it rather than letting it happen quietly.
	surfaces["raven_ai_enabled"] = bool(
		surfaces["raven_installed"] and frappe.db.get_single_value("Raven Settings", "enable_ai_integration")
	)

	open_issues = frappe.db.count("Issue", {"status": "Open"})

	verdict = _verdict(
		tick_age=tick_age,
		workers=workers,
		stuck=stuck,
		pending=tasks_by_state.get("Pending", 0),
		open_issues=open_issues,
		raven_installed=surfaces["raven_installed"],
		raven_ai_enabled=surfaces["raven_ai_enabled"],
	)

	return {
		"verdict": verdict,
		"scheduler_tick_age_seconds": tick_age,
		"workers": workers,
		"tasks_by_state": tasks_by_state,
		"stuck": stuck,
		"connectors": connectors,
		"surfaces": surfaces,
		"open_issues": open_issues,
		"thresholds": {
			"scheduler_tick_max_age_seconds": SCHEDULER_TICK_MAX_AGE_SECONDS,
			"pending_degraded": PENDING_DEGRADED_THRESHOLD,
			"open_issues_degraded": OPEN_ISSUES_DEGRADED_THRESHOLD,
		},
	}


def _verdict(
	*, tick_age, workers, stuck, pending, open_issues, raven_installed=True, raven_ai_enabled=False
) -> str:
	"""
	Q3 LOCKED — Strict thresholds.

	Order matters: 'down' supersedes 'degraded'. If multiple bad signals
	stack, the operator gets the worst one — the goal is "is it broken?"
	being answered honestly, not the prettiest possible reading.
	"""
	# 'down' conditions
	if not raven_installed:
		# Required platform component. Without it there is no chat front door:
		# no bot to DM, no per-project channels, no war room.
		return "down"
	if tick_age is None or tick_age > SCHEDULER_TICK_MAX_AGE_SECONDS:
		return "down"
	for q in CRITICAL_QUEUES:
		if not workers.get(q, {}).get("present"):
			return "down"
	if any(v > 0 for v in stuck.values()):
		return "down"

	# 'degraded' conditions
	if raven_ai_enabled:
		# A second, ungoverned agent engine on the same records. Not a Friday
		# outage — a governance gap, and the operator should see it.
		return "degraded"
	if pending > PENDING_DEGRADED_THRESHOLD:
		return "degraded"
	if open_issues > OPEN_ISSUES_DEGRADED_THRESHOLD:
		return "degraded"

	return "ok"


def _scheduler_tick_age() -> int | None:
	"""
	Seconds since the scheduler last fired ANY enabled job. None if we can't tell.

	Truth source is ``max(Scheduled Job Type.last_execution)`` over enabled
	(``stopped = 0``) job types — Frappe stamps ``last_execution`` on EVERY
	fire. Anything older than SCHEDULER_TICK_MAX_AGE_SECONDS means the scheduler
	probably is not running — the Legion ``bench serve`` trap. The age applies
	to the WHOLE scheduler (any job), because if the scheduler is dead nothing
	fires.

	WHY NOT ``Scheduled Job Log`` (the original 61b implementation): a log row is
	only written for job types with ``create_log = 1``. Most jobs (including the
	frequent per-minute ones) run with ``create_log = 0``, so the log is sparse —
	a perfectly healthy scheduler whose last *logged* job ran 20 min ago looked
	stale and forced a false ``down``. Caught on a live bench 2026-06-14 when the
	reconciler was firing every minute (last_execution ~50s) yet the log was
	~19 min old. ``last_execution`` updates on every fire, logged or not.

	We read it with a ``Max(last_execution)`` aggregate (query builder), which
	ignores never-run types whose ``last_execution`` is NULL — and avoids the
	``("is", "set")`` filter form, which Frappe compiles to ``last_execution !=
	''`` and Postgres rejects for a timestamp column (MariaDB tolerates it).
	"""
	try:
		from frappe.query_builder.functions import Max

		job_type = frappe.qb.DocType("Scheduled Job Type")
		rows = (
			frappe.qb.from_(job_type).select(Max(job_type.last_execution)).where(job_type.stopped == 0)
		).run()
	except Exception:
		return None
	last = rows[0][0] if rows and rows[0] else None
	if not last:
		return None
	from frappe.utils import now_datetime, time_diff_in_seconds

	try:
		return int(time_diff_in_seconds(now_datetime(), last))
	except Exception:
		return None


def _inflight_jobs_by_queue() -> dict[str, int]:
	"""
	Return ``{queue_short_name: in_flight_count}`` for every queue that has
	at least one worker subscribed.

	Truth source is the Redis set ``rq:workers:<full_queue_name>`` — RQ
	maintains one such set per queue, containing the keys of every worker
	currently listening on that queue. A non-empty set means the queue is
	being served; an empty (or missing) set means the queue has no listener
	even if jobs are getting enqueued.

	WHY NOT ``get_jobs()`` (the original 61b implementation): it returns a
	map of queue → IN-FLIGHT job names. An idle worker on a quiet queue
	does not appear, so we falsely reported it absent. Caught the first
	time this endpoint was called on a live bench (2026-06-13).

	WHY NOT ``rq.Worker.all()[i].queue_names``: in RQ 2.x the worker hash
	stored in Redis only carries ``last_heartbeat``; the queue list is held
	in the worker process and not persisted. Reading it from outside the
	worker returns ``[]``.

	Queue short names: Frappe namespaces queues as ``<bench-name>:<short>``
	(e.g. ``Users-alphaworkz-Documents-friday-bench:friday``). Callers ask
	for ``"friday"``, so we strip the prefix.
	"""
	from frappe.utils.background_jobs import get_queues, get_redis_conn

	try:
		conn = get_redis_conn()
		queues = get_queues(connection=conn) or []
	except Exception:
		return {}

	out: dict[str, int] = {}
	for q in queues:
		full = q.name
		short = full.split(":", 1)[1] if ":" in full else full
		try:
			workers = conn.smembers(f"rq:workers:{full}")
		except Exception:
			workers = None
		if workers:
			try:
				inflight = q.count
			except Exception:
				inflight = 0
			out[short] = inflight
	return out
