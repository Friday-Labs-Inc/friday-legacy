# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
The `manage-cron-jobs` skill — let an agent schedule its own recurring work
(Design 87, Slice 2). The Slice 1 engine (Cron Job doctype + tick + delivery)
already exists; this is the agent-facing verb on top of it.

PLAIN ENGLISH
=============

One skill, an `action` parameter drives it (mirrors Hermes' `cronjob` tool):

    create   — schedule a new recurring run
    list     — list this agent's own cron jobs
    pause    — disable a job
    resume   — re-enable a job
    remove   — delete a job
    trigger  — fire a job on the next tick (set next_run_at = now)

SAFETY BOUNDARIES (disclosed)
=============================
- **Own jobs only.** pause/resume/remove/trigger refuse a Cron Job whose
  `agent_profile` is not the calling agent — an agent cannot touch another
  agent's schedule.
- **Delivers to the current channel by default.** A job created in a chat channel
  defaults `deliver` to `raven:{channel}` so its output lands where the agent was
  asked; falls back to `local` (a private file) when there is no channel.
- The skill is role-gated (`Friday Cron Manager`) and first-party-trusted
  (`requires_approval=0`, per `feedback_v01-skills-first-party-trust`).
"""

from __future__ import annotations

import frappe
from frappe.friday_core.agent_runner.dispatcher import register_skill_handler

SKILL_NAME = "manage-cron-jobs"
_ACTIONS = ("create", "list", "pause", "resume", "remove", "trigger")


def manage_cron_jobs(skill_name: str, parameters: dict) -> dict:
	"""Dispatch a cron action for the calling agent. Returns a result dict."""
	ctx = frappe.flags.get("friday_dispatch_context") or {}
	agent_profile = ctx.get("agent_profile")
	session_id = (ctx.get("session_id") or "").strip()

	action = (parameters.get("action") or "").strip().lower()
	if action not in _ACTIONS:
		raise ValueError(f"action must be one of {list(_ACTIONS)}")

	if action == "create":
		return _create(agent_profile, session_id, parameters)
	if action == "list":
		return _list(agent_profile)
	# pause / resume / remove / trigger all operate on one named, owned job.
	return _act_on_job(agent_profile, action, parameters)


def _create(agent_profile: str, session_id: str, p: dict) -> dict:
	"""Schedule a new recurring run owned by the calling agent."""
	prompt = (p.get("prompt") or "").strip()
	expr = (p.get("schedule_expression") or "").strip()
	if not prompt:
		raise ValueError("create requires 'prompt'")
	if not expr:
		raise ValueError("create requires 'schedule_expression'")

	# Default delivery: back into the channel the agent is in; else a private file.
	# "origin" is accepted as a friendly alias for "this channel" — agents reach
	# for it naturally, and a cron run has no live origin session at fire time, so
	# resolve it here to the channel the job was created in.
	deliver = (p.get("deliver") or "").strip()
	if not deliver or deliver.lower() == "origin":
		deliver = f"raven:{session_id}" if session_id else "local"

	job = frappe.get_doc(
		{
			"doctype": "Cron Job",
			"job_name": (p.get("name") or prompt[:60]).strip(),
			# OWNER IS NEVER LLM-CONTROLLED. The owning profile comes ONLY from the
			# unspoofable dispatch context — never from the model's parameters. Honoring
			# `p.get("agent_profile")` would let a prompt-injected/buggy agent create a
			# Cron Job owned by (and running with the authority of) any other profile —
			# a privilege escalation, and a direct violation of this skill's own
			# "Own jobs only" boundary.
			"agent_profile": agent_profile,
			"prompt": prompt,
			"schedule_kind": (p.get("schedule_kind") or "cron").strip().lower(),
			"schedule_expr": expr,
			"deliver": deliver,
			"repeat_times": int(p.get("repeat_times") or 0),
		}
	).insert(ignore_permissions=True)
	return {
		"result": (
			f"Cron job {job.name} created — next run {job.next_run_at or 'n/a'}, delivering to {deliver}."
		),
		"status": "created",
		"job": job.name,
		"next_run_at": str(job.next_run_at) if job.next_run_at else None,
		"deliver": deliver,
	}


def _list(agent_profile: str) -> dict:
	"""List the calling agent's own cron jobs."""
	rows = frappe.get_all(
		"Cron Job",
		filters={"agent_profile": agent_profile},
		fields=["name", "schedule_kind", "schedule_expr", "enabled", "state", "next_run_at"],
		order_by="next_run_at asc",
	)
	if rows:
		listing = "\n".join(
			f"  - {r.get('name')}: {r.get('schedule_kind')} '{r.get('schedule_expr')}' "
			f"({'enabled' if r.get('enabled') else 'disabled'}, {r.get('state')}, "
			f"next: {r.get('next_run_at')})"
			for r in rows
		)
		result_text = f"{len(rows)} cron job(s):\n{listing}"
	else:
		result_text = "You have no cron jobs."
	return {"result": result_text, "status": "ok", "count": len(rows), "jobs": rows}


def _act_on_job(agent_profile: str, action: str, p: dict) -> dict:
	"""pause / resume / remove / trigger one job the calling agent owns."""
	name = (p.get("name") or "").strip()
	if not name:
		raise ValueError(f"{action} requires the cron job 'name'")
	if not frappe.db.exists("Cron Job", name):
		raise ValueError(f"Cron Job {name!r} not found")

	# Ownership guard — an agent may only touch its own schedule.
	owner = frappe.db.get_value("Cron Job", name, "agent_profile")
	if owner != agent_profile:
		raise ValueError(f"Cron Job {name!r} belongs to another agent — refused.")

	if action == "remove":
		frappe.delete_doc("Cron Job", name, ignore_permissions=True)
		return {"result": f"Cron job {name} removed.", "status": "removed", "job": name}

	job = frappe.get_doc("Cron Job", name)
	if action == "pause":
		job.enabled = 0
		job.state = "Paused"
	elif action == "resume":
		job.enabled = 1
		job.state = "Scheduled"
	elif action == "trigger":
		# Fire on the next tick by making it due now.
		job.next_run_at = frappe.utils.now_datetime()
	job.save(ignore_permissions=True)
	return {
		"result": f"Cron job {name} {action}d — next run {job.next_run_at}.",
		"status": action + "d",
		"job": name,
		"next_run_at": str(job.next_run_at),
	}


register_skill_handler(SKILL_NAME, manage_cron_jobs)
