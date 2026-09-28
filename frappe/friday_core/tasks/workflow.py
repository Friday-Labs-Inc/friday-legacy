# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Task workflow state-machine hook for Task documents.

Registered as ``doc_events["Task"]["on_update"]`` in ``hooks.py``.

Responsibilities
----------------
1. Derive ``dispatchable`` from the current ``workflow_state``.
   dispatchable = True when workflow_state ∈ {Pending, Assigned}.
2. Record ``started_at`` when entering the Executing state.
3. Record ``completed_at`` when entering Completed or Cancelled.
4. Clear ``assigned_to_profile`` when entering Cancelled.
5. Emit ``agent_task.assigned`` on Frappe Redis pub/sub when
   transitioning Pending → Assigned so the task runner can pick it up
   outside the save transaction (avoids holding a DB lock while the
   worker runs).
6. Post a War Room update to the Raven FRIDAY_WAR_ROOM channel on
   every state transition (graceful degradation if Raven is not installed).
"""

from __future__ import annotations

import frappe
from frappe.friday_core.observability import emit

# Lazy import to avoid circular imports — warroom itself doesn't import tasks.
_warroom = None


def _current_trigger_source() -> str:
	"""Read the caller's hint about what triggered this save (Design 72).

	Callers (dispatcher / reconciler / runner) set ``frappe.flags.dispatcher_event_source``
	before saving so the workflow hook can stamp the resulting event with an
	honest trigger. Falls back to ``"unknown"`` for direct user/Desk saves —
	the operator can still see the transition; the source is just not labeled.
	"""
	return frappe.flags.get("dispatcher_event_source") or "unknown"


def _get_warroom():
	global _warroom
	if _warroom is None:
		try:
			from frappe.friday_core import warroom

			_warroom = warroom
		except Exception:
			_warroom = None
	return _warroom


# States that make a task available for the dispatcher to claim.
DISPATCHABLE_STATES = frozenset({"Pending", "Assigned"})


def on_state_change(doc: "Task", method: str) -> None:
	"""
	Recompute dispatchable; record timestamps; emit Redis event.

	Called by Frappe's doc_events system after every save of an
	Task document.  Runs inside the same transaction as the
	save, so all DB writes are atomic with it.

	Args:
		doc: The saved Task document.
		method: The Frappe hook method name (``"on_update"``).
	"""
	# 1. dispatchable is a derived field — always recompute from live state.
	doc.dispatchable = doc.workflow_state in DISPATCHABLE_STATES

	# 1b. executing_token is the runner's claim on the row — valid ONLY while the
	# task is Executing. Release it on every other state. This is the invariant
	# that makes a task re-dispatchable after a reset: the dispatcher's claim
	# (and the runner's own claim) skip any row whose executing_token is
	# non-empty (`COALESCE(executing_token, '') = ''`), so a leftover token
	# strands the task forever — exactly what bit a Blocked→Pending retry that
	# re-set the state but kept the stale token (task sat Pending, undispatched,
	# for 20+ min). Deriving the release HERE keeps the failure→Blocked path, the
	# reconciler reset→Pending path, and any future/manual reset consistent,
	# instead of relying on every caller to remember. We only ever CLEAR it (and
	# only when NOT Executing), so the runner's atomic raw-SQL claim is never
	# clobbered by a stale in-memory value.
	release_token = doc.workflow_state != "Executing"
	if release_token:
		doc.executing_token = None

	# Only act on actual workflow state transitions, not unrelated field saves.
	if doc.has_value_changed("workflow_state"):
		_emit_state_change_event(doc)
		_watch_transition(doc)

	# Design 72 — record token release as its own event so the Lifecycle Trace
	# shows when the runner's claim was given up (the bug that caught us at
	# 2026-06-14: stale token + dispatchable=0 stranded a phase task 20 min).
	if release_token and doc.has_value_changed("executing_token"):
		emit(
			"workflow.executing_token_released",
			task=doc.name,
			project=doc.get("project"),
			trigger_source=_current_trigger_source(),
			summary=f"{doc.name}: token released (state={doc.workflow_state})",
		)

	# Design 65d — derive the display fields from the live state too: the
	# 0/50/100 progress bar and the is_milestone mirror of execution_mode.
	from frappe.friday_core.tasks.rollup import progress_for_state

	# Persist the derived/side-effect fields WITHOUT doc.save(): this function
	# runs ON on_update, so save() here re-fires on_update → this function →
	# save() → RecursionError. db_set writes the columns directly and fires no
	# document hooks (the Frappe idiom for persisting from inside a hook).
	derived = {
		"dispatchable": 1 if doc.dispatchable else 0,
		"started_at": doc.started_at,
		"completed_at": doc.completed_at,
		"assigned_to_profile": doc.assigned_to_profile,
		"progress": progress_for_state(doc.workflow_state),
		"is_milestone": 1 if (doc.get("execution_mode") == "milestone") else 0,
	}
	# Persist the token release only when releasing — never write it while
	# Executing (the in-memory value may be stale vs the raw-SQL claim).
	if release_token:
		derived["executing_token"] = None
	doc.db_set(derived, update_modified=False)


def _emit_state_change_event(doc: "Task") -> None:
	"""Design 72 — record every workflow state transition for the Lifecycle Trace.

	Reads ``frappe.flags.dispatcher_event_source`` to honestly stamp who caused
	the transition (dispatcher_claim, reconciler_reset, runner_complete, etc.).
	"""
	from_state = (
		(doc.get_doc_before_save() or {}).get("workflow_state") if doc.get_doc_before_save() else None
	)
	to_state = doc.workflow_state
	emit(
		"workflow.state_change",
		task=doc.name,
		project=doc.get("project"),
		agent_profile=doc.get("assigned_to_profile"),
		trigger_source=_current_trigger_source(),
		summary=f"{doc.name}: {from_state or '(new)'} → {to_state}",
		payload={"from": from_state, "to": to_state, "retry_count": doc.get("retry_count")},
	)


def _watch_transition(doc: "Task") -> None:
	"""
	Handle side-effects that depend on the specific state transition.

	Runs inside the save transaction — keep DB writes minimal.
	Long-running work (Docker execution) happens in the runner, which
	picks up the ``agent_task.assigned`` pub/sub event.
	"""
	state = doc.workflow_state

	# --- timestamps -------------------------------------------------------
	if state == "Executing" and doc.started_at is None:
		doc.started_at = frappe.utils.now_datetime()

	if state in ("Completed", "Cancelled") and doc.completed_at is None:
		doc.completed_at = frappe.utils.now_datetime()

	# --- clear assignment on cancellation ---------------------------------
	if state == "Cancelled" and doc.assigned_to_profile:
		doc.assigned_to_profile = None

	# --- War Room post ----------------------------------------------------
	_post_warroom_update(doc, state)

	# --- Live console push (design 65c) -----------------------------------
	# Push the transition to every Desk console client over realtime. Never
	# raises; the console's 30s snapshot poll reconciles any dropped frame.
	from frappe.friday_core.console.console_stream import publish_activity

	publish_activity(doc, state)

	# --- Project rollup (design 65d) --------------------------------------
	# Recompute the parent project's counts / %-complete / dates / cost from
	# its child tasks. Writes only the Project row via db_set (fires no Task
	# hooks → no recursion). The current task's just-saved state and cost_usd
	# are visible in this same transaction, so the rollup includes them.
	# Fail-soft inside a savepoint (same contract as the War Room post): a
	# rollup hiccup must not break the task save, and on Postgres a failed
	# statement must roll back to avoid poisoning the transaction. The numbers
	# are derived — they self-heal on the next transition.
	if doc.get("project"):
		try:
			frappe.db.savepoint("friday_rollup")
			from frappe.friday_core.tasks.rollup import recompute_project_rollup

			recompute_project_rollup(doc.project)
		except Exception:
			frappe.db.rollback(save_point="friday_rollup")

	# --- Task-transition hooks (kernel seam) --------------------------------
	# Apps subscribe to task transitions through the `friday_task_transition_hooks`
	# hook: dotted paths called as fn(doc, state) (e.g. a domain app's backend
	# write-back). Each subscriber is failure-isolated — an outage in one cannot
	# break the task save or starve the others.
	for _hook_path in frappe.get_hooks("friday_task_transition_hooks") or []:
		try:
			frappe.get_attr(_hook_path)(doc, state)
		except Exception:
			frappe.log_error(title=f"friday task transition hook failed: {_hook_path}")

	# --- Agent report-back (design 62, Q3) ---------------------------------
	# A terminal task that knows the conversation it came from writes a direct
	# reply to that session, authored by the agent that did the work. No-op
	# for tasks with no originating_session (e.g. domain pipeline tasks, whose
	# report-back is a transition hook above). Never raises.
	from frappe.friday_core.tasks.report_back import report_back

	report_back(doc, state)

	# --- Cron delivery (design 87) -----------------------------------------
	# A cron-spawned task delivers its result to the job's target on completion
	# and updates the job's bookkeeping. No-ops for every non-cron task. Never
	# raises — a delivery hiccup cannot break the task save.
	if state in ("Completed", "Blocked", "Cancelled"):
		try:
			frappe.db.savepoint("friday_cron_deliver")
			from frappe.friday_core.cron.scheduler import on_task_terminal

			on_task_terminal(doc, state)
		except Exception:
			frappe.db.rollback(save_point="friday_cron_deliver")
			frappe.log_error(title="friday.cron on_task_terminal failed")

	# --- Deliverable materialization (design 73, slice 5) ------------------
	# On completion, write the agent's output as attached artifact files
	# (md + pdf) so a finished task produces a real, downloadable deliverable —
	# not just text buried in `result`. Enqueued AFTER COMMIT (PDF rendering is
	# slow and reads the committed result); `now` under test runs it inline.
	if state == "Completed":
		frappe.enqueue(
			"frappe.friday_core.deliverables.materialize.on_task_completed",
			queue="short",
			enqueue_after_commit=True,
			now=bool(frappe.flags.in_test),
			task_name=doc.name,
		)

	# --- emit Redis pub/sub for task runner -------------------------------
	# Only emit when we are moving INTO Assigned AND the profile actually
	# changed (avoids duplicate events on re-save without assignment change).
	if state == "Assigned" and doc.has_value_changed("assigned_to_profile"):
		_emit_assigned_event(doc.name, doc.assigned_to_profile)

	# --- Task Completion Summary (Design 72) ------------------------------
	# Write the permanent compact summary on terminal transitions so the audit
	# trail survives the 30-day Dispatcher Event purge. Idempotent upsert by
	# task name — a Blocked→Pending reset by the reconciler followed by
	# another terminal transition overwrites the prior summary in place.
	# Savepoint-guarded inside the writer; can never break the task save.
	from frappe.friday_core.observability import is_terminal, write_task_completion_summary

	if is_terminal(doc):
		write_task_completion_summary(doc)


def _post_warroom_update(doc: "Task", state: str) -> None:
	"""
	Post a status update to the Raven War Room channel.

	Args:
		doc: The Task document.
		state: The new workflow_state.
	"""
	warroom = _get_warroom()
	if warroom is None:
		return

	try:
		# Savepoint so a failed statement inside the post (e.g. Raven not
		# installed on this site) rolls back WITHOUT aborting the whole
		# transaction — on Postgres a bare `except: pass` is NOT graceful:
		# every later statement would fail with InFailedSqlTransaction.
		frappe.db.savepoint("friday_warroom")
		details = {"profile": doc.assigned_to_profile} if doc.assigned_to_profile else None
		warroom.post_task_update(doc.name, state.lower(), details)
		emit(
			"warroom.post",
			task=doc.name,
			project=doc.get("project"),
			trigger_source="succeeded",
			summary=f"war room: {doc.name} {state.lower()}",
		)
	except Exception:
		# Never block the task pipeline — degrade gracefully.
		frappe.db.rollback(save_point="friday_warroom")
		emit(
			"warroom.post",
			task=doc.name,
			project=doc.get("project"),
			trigger_source="silently_rolled_back_savepoint",
			summary=f"war room post failed for {doc.name} ({state})",
		)


def _emit_assigned_event(task_name: str, assigned_to_profile: str) -> None:
	"""
	Enqueue the background runner to execute a newly-assigned task.

	This is the single trigger that bridges a Pending → Assigned transition
	to the task runner (architecture decision, 2026-06-13). It deliberately
	does NOT use ``frappe.publish_realtime``: Frappe's realtime layer is
	publish-only on the server side (subscriptions live in browser clients
	over socket.io), so a backend worker never receives the event and the
	task stalls in Assigned forever. Instead we push a real RQ job.

	Details that matter:
	  - ``queue="friday"`` (design 61, Q4 LOCKED) — agent work is minutes-long
	    LLM/sandbox runs; isolating it from Frappe's housekeeping (email,
	    link-count, etc.) keeps both pipelines responsive. The bench bring-up
	    docs register the queue, and ``pipeline_health`` (61b) flags an
	    absent ``friday`` worker as ``down`` within 60s so a forgotten setup
	    step is loud, not silent.
	  - ``job_name="task:<name>"`` — the durability reconciler uses this to
	    detect "is this task already in flight" before re-firing a lost
	    enqueue, so a duplicate trigger is a cheap no-op instead of a
	    double-run.
	  - ``enqueue_after_commit=True`` — defer the job until the surrounding
	    save transaction commits, so the runner always reads the committed
	    ``Assigned`` row instead of racing the writer.
	  - ``now`` under test — run the job inline during tests so assertions
	    see the effect synchronously; in production it runs on the worker.

	Args:
		task_name: Task document name (e.g. ``AT-000042``).
		assigned_to_profile: The agent profile assigned to the task.
	"""
	frappe.enqueue(
		"frappe.friday_core.tasks.runner.on_agent_task_assigned",
		queue="friday",
		timeout=600,
		job_name=f"task:{task_name}",
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		message={
			"task_name": task_name,
			"assigned_to_profile": assigned_to_profile,
			"workflow_state": "Assigned",
		},
	)
