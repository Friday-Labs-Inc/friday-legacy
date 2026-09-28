# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
The generic workflow interpreter (Design 75).

PLAIN ENGLISH
=============
This is the doc_events["<work-item>"]["on_update"] handler. Every time a
governed work-item is saved, it asks one question: "the work-item just landed
in a new state — is there an AGENT step waiting at this state?" If yes, it
hands off to the phase dispatcher to create the agent's task. If the state is a
human gate, a terminal state, or anything with no agentic step, it does
nothing and waits.

The interpreter knows NOTHING about brands, data centers, or research. It only
reads data: the Domain Bundle (which workflow governs this DocType), the
Workflow (the states + transitions), and the Friday Workflow Transition Meta
rows (the agentic config bound to each transition). Add a new domain by adding
data — never by editing this file.

Phase 1 is sequential-only (Design 75 §8): each state has at most one outgoing
agentic transition. A state with two would be a parallel fan-out, which is
Phase 2; the interpreter logs and takes the first so a misconfig degrades
loudly rather than crashing a save.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.engine import bundle, phase_dispatcher


def on_work_item_update(doc, method: str | None = None) -> None:
	"""Fire on every save of a governed work-item; dispatch the agentic step (if
	any) waiting at the work-item's current state."""
	workflow = bundle.workflow_for(doc.doctype)
	if not workflow:
		return  # not a governed DocType — the hook is a no-op here

	state_field = bundle.state_field_for(workflow)
	state = doc.get(state_field)
	if not state:
		return

	# Only react when the STATE actually changed. An unrelated field edit on a
	# work-item sitting mid-pipeline must not re-dispatch the current phase.
	if not doc.has_value_changed(state_field):
		return

	meta = _agentic_meta_for_state(workflow, state)
	if not meta:
		# No agentic phase at this state. Either a human gate (has outgoing
		# transitions waiting for a person) or the terminal state (no outgoing).
		# Announce the human pause so the war room doesn't go silent at the
		# very moment the human is needed (otherwise it looks like Friday hung).
		_announce_human_pause(doc, workflow, state)
		return

	if _has_active_task(doc, meta.phase_key):
		return  # belt-and-suspenders: never double-dispatch one state-occupancy

	phase_dispatcher.dispatch(doc, meta.name)


def _announce_human_pause(doc, workflow: str, state: str) -> None:
	"""Post a 'waiting for you' message to the war room when a work-item lands
	in a state with no agentic phase AND no system-driven outgoing transition.

	- Terminal (no outgoing transitions): silent. Surrounding handlers already
	  announce delivery.
	- Idle entry state (the workflow's first state): silent. A system
	  transition starts the pipeline; nothing for a human here.
	- Otherwise (a real gate waiting on a human): post AFTER the current
	  transaction commits.

	WHY ENQUEUE-AFTER-COMMIT: when this hook runs inside apply_workflow inside
	an advance_work_item job, a Raven Message inserted in the same transaction
	is rolled back downstream (confirmed empirically — the row is visible at
	function-exit but never persists). Posting from a fresh job that runs only
	after the current transaction commits sidesteps that rollback entirely.
	Permissions: the announce query bypasses perms because the hook runs as the
	agent user (no implicit Workflow Transition read).
	Failure-isolated: a war room outage NEVER breaks the engine save."""
	try:
		if bundle.is_entry_state(workflow, state):
			return  # idle entry state — a system transition starts the pipeline, nothing for a human

		# bypass perms — the agent user has no implicit read on Workflow Transition
		outgoing = frappe.get_all(
			"Workflow Transition",
			filters={"parent": workflow, "state": state},
			fields=["name"],
			limit_page_length=1,
			ignore_permissions=True,
		)
		if not outgoing:
			return  # terminal — nothing for a human to do

		# The bundle says which fields name / externally reference this work-item;
		# the engine never hardcodes a domain's field names.
		fields = bundle.fields_for(doc.doctype)
		label_parts = []
		display = doc.get(fields["display_name_field"]) if fields.get("display_name_field") else None
		external_ref = doc.get(fields["external_ref_field"]) if fields.get("external_ref_field") else None
		if display:
			label_parts.append(str(display))
		if external_ref:
			label_parts.append(f"REF {external_ref}")
		label = " — ".join(label_parts) or doc.name

		text = (
			f"🛑 **[{doc.name}]** {label} is at **{state}** — "
			"waiting for the human decision. Pipeline paused."
		)

		frappe.enqueue(
			"frappe.friday_core.engine.workflow_engine._post_pause_message",
			text=text,
			job_id=f"pause:{doc.name}:{state}",
			enqueue_after_commit=True,
			queue="short",
		)
	except Exception:
		# War room outages must never break the engine save.
		frappe.log_error(title="friday.engine human-pause announce failed")


def _post_pause_message(text: str) -> None:
	"""Post a pause message to the war room from a fresh job (after-commit).
	Runs in its own transaction so it can never be rolled back by the engine
	save that scheduled it. Whitelisted-ish but only ever called via enqueue."""
	try:
		from frappe.friday_core.warroom.publisher import _get_channel_id, _post_to_raven

		channel = _get_channel_id()
		if not channel:
			return
		_post_to_raven(
			channel,
			{"text": text, "message_type": "Text", "hide_in_message_history": False},
		)
	except Exception:
		frappe.log_error(title="friday.engine pause-post worker failed")


def _agentic_meta_for_state(workflow: str, state: str):
	"""The agentic transition-meta leaving `state`, or None. Returns the row's
	name + phase_key. Phase 1 expects at most one; more than one is a fan-out
	(Phase 2) — we log loudly and take the first."""
	rows = frappe.get_all(
		"Friday Workflow Transition Meta",
		filters={"workflow": workflow, "from_state": state, "execution_mode": "agentic"},
		fields=["name", "phase_key"],
		order_by="creation asc",
		limit_page_length=0,
	)
	if not rows:
		return None
	if len(rows) > 1:
		frappe.log_error(
			message=(
				f"Workflow {workflow!r} state {state!r} has {len(rows)} agentic outgoing "
				"transitions. Phase 1 is sequential-only (Design 75 §8); a true fan-out is "
				f"Phase 2. Dispatching the first ({rows[0].phase_key!r}); the rest are ignored."
			),
			title="Design 75 engine — unexpected fan-out",
		)
	return rows[0]


def _has_active_task(doc, phase_key: str) -> bool:
	"""True if a non-terminal Task already exists for this work-item + phase. A
	*completed* task does NOT block re-dispatch, so back-edges (a 'revise' loop
	re-entering a state) correctly spawn fresh work."""
	return bool(
		frappe.db.exists(
			"Task",
			{
				"work_item_doctype": doc.doctype,
				"work_item_name": doc.name,
				"phase_key": phase_key,
				"workflow_state": ["not in", ["Completed", "Cancelled"]],
			},
		)
	)
