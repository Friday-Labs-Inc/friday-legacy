# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Post-turn self-improvement review (design 79/80 — "the agent that learns").

PLAIN ENGLISH
=============

Hermes, after a conversation turn, occasionally "reflects": it re-reads the
conversation and asks itself "did the user reveal a preference or fact I should
remember?" — then saves it, so the next conversation starts smarter. Friday had
no such step (a turn just ended). This module adds it.

How it works:
  - After every Nth CONVERSATIONAL turn (N = Agent Settings.memory_review_interval,
    default 3; 0 = off), the gateway calls `enqueue_if_due`.
  - That queues a background job (`run_review`) on the `friday` queue — it never
    blocks the user's reply, which has already been sent.
  - The job runs ONE governed agent turn over the same conversation, but with the
    toolset HARD-restricted to the `remember` skill (it can save memories and
    nothing else) and compression disabled (it reads the live session read-only).
  - Anything it saves surfaces as a "💾 Learned: …" note in the channel; the
    `remember` calls write their own Execution Log rows (the audit trail).

DELIBERATE SCOPE (Slice 1)
==========================
This slice is the MEMORY review only. Hermes also reviews/edits its own skills;
in Friday that becomes a *governed proposal* (propose → human approves, never
self-edit) — built in Slice 2. See the ports ledger.

WHY AN RQ JOB (not Hermes' daemon thread)
=========================================
Frappe has no long-lived process; the faithful adaptation of Hermes' background
daemon thread is a best-effort RQ job. The review turn writes NO Chat Message
rows (run_turn is pure; only the gateway writes them), so it never pollutes the
conversation nor perturbs the turn-count cadence.
"""

from __future__ import annotations

import frappe

# The dedicated agent queue (mirrors gateway.service.FRIDAY_QUEUE). Defined
# locally to avoid importing the gateway from inside agent_runner.
_FRIDAY_QUEUE = "friday"

# Faithful port of Hermes' memory-review prompt (background_review.py
# _MEMORY_REVIEW_PROMPT), adapted to Friday's `remember` skill and its
# do-not-save norms. Functional tool instruction, not creative text.
_MEMORY_REVIEW_PROMPT = (
	"Review the conversation above and decide whether anything durable is worth "
	"remembering for future conversations.\n\n"
	"Focus on:\n"
	"1. Did the user reveal something about themselves — their role, preferences, "
	"working style, or personal details worth remembering?\n"
	"2. Did the user express how they want you to behave, or make a durable "
	"decision or standing instruction?\n\n"
	"If something stands out, save it with the `remember` skill — one durable fact "
	"per call. If nothing is worth saving, reply exactly 'Nothing to save.' and "
	"stop. Do NOT save transient task chatter, one-off requests, environment "
	"errors, or anything already in your memory."
)

# Skill-review prompt (design 79 Slice 2). Governed: the agent PROPOSES a skill
# change for a human to approve — it never edits a skill itself. Original
# wording; ports Hermes' skill-review intent + its do-not-capture guidance.
_SKILL_REVIEW_PROMPT = (
	"Review the conversation above for a reusable how-to lesson worth capturing "
	"for future sessions. If you find one, PROPOSE a skill change with the "
	"`propose_skill_change` skill — it records a proposal a human approves; you "
	"never edit a skill yourself.\n\n"
	"Propose when: the user corrected your approach, workflow, or format; a "
	"non-trivial technique or fix worked; or a skill you used was wrong or "
	"missing a step. Prefer proposing an UPDATE to the most relevant existing "
	"skill over a brand-new one.\n\n"
	"Do NOT propose for: environment/setup failures, negative claims about "
	"tools, transient errors that resolved, or one-off task narratives — these "
	"harden into bad standing rules. If nothing reusable stands out, reply "
	"exactly 'Nothing to propose.' and stop."
)


def enqueue_if_due(session_id: str, profile_name: str) -> None:
	"""Queue the memory and/or skill review(s) this session is due for. Best-effort.

	Called by the gateway after a CONVERSATIONAL turn completes (never the task
	pipeline). Two independent cadences (like Hermes' two counters): the memory
	review fires every `memory_review_interval` turns, the skill-proposal review
	every `skill_review_interval` turns (0 = off). Any failure here must never
	affect the turn that just completed, so everything is swallowed.
	"""
	try:
		count = frappe.db.count(
			"Chat Message",
			{"session_id": session_id, "direction": "inbound", "processed": 1},
		)
		if count == 0:
			return
		_maybe_enqueue(session_id, profile_name, count, _review_interval(), "run_review", "review")
		_maybe_enqueue(session_id, profile_name, count, _skill_interval(), "run_skill_review", "skillreview")
	except Exception:
		frappe.logger("friday.self_review").warning(
			f"enqueue_if_due failed for session {session_id!r}", exc_info=True
		)


def _maybe_enqueue(session_id: str, profile_name: str, count: int, interval: int, fn: str, tag: str) -> None:
	"""Enqueue review `fn` if `count` is a non-zero multiple of `interval`."""
	if interval <= 0 or count % interval != 0:
		return
	frappe.enqueue(
		f"frappe.friday_core.agent_runner.self_review.{fn}",
		session_id=session_id,
		profile_name=profile_name,
		queue=_FRIDAY_QUEUE,
		enqueue_after_commit=True,
		job_id=f"friday-{tag}::{session_id}::{count}",  # idempotent per cadence point
	)


def run_review(session_id: str, profile_name: str) -> None:
	"""RQ job: run one memory-only review turn over the session. Never raises.

	Sets the dispatch context so a saved memory is attributed to the right agent
	and session, runs `run_turn` with the toolset restricted to `remember` and
	compression skipped, then surfaces anything it saved.
	"""
	from frappe.friday_core.agent_runner.runner import run_turn

	try:
		frappe.flags.friday_dispatch_context = {
			"agent_profile": profile_name,
			"session_id": session_id,
		}
		reply = run_turn(
			profile_name=profile_name,
			session_id=session_id,
			inbound_content=_MEMORY_REVIEW_PROMPT,
			allowed_skills={"remember"},
			skip_compression=True,
			provider_override=_resolve_review_provider(),
		)
		_surface(session_id, profile_name, reply)
	except Exception:
		frappe.log_error(title="friday.self_review.run_review failure")
	finally:
		frappe.flags.friday_dispatch_context = None


def run_skill_review(session_id: str, profile_name: str) -> None:
	"""RQ job: run one skill-proposal review turn over the session. Never raises.

	Like `run_review`, but the toolset is restricted to `propose_skill_change` —
	the agent PROPOSES skill changes (Pending Skill Proposal rows for human
	approval) and edits nothing. Design 79 Slice 2; the governed half.
	"""
	from frappe.friday_core.agent_runner.runner import run_turn

	try:
		frappe.flags.friday_dispatch_context = {
			"agent_profile": profile_name,
			"session_id": session_id,
		}
		reply = run_turn(
			profile_name=profile_name,
			session_id=session_id,
			inbound_content=_SKILL_REVIEW_PROMPT,
			allowed_skills={"propose_skill_change"},
			skip_compression=True,
			provider_override=_resolve_review_provider(),
		)
		_surface(
			session_id,
			profile_name,
			reply,
			prefix="📝 Skill proposal: ",
			skip_phrase="nothing to propose",
		)
	except Exception:
		frappe.log_error(title="friday.self_review.run_skill_review failure")
	finally:
		frappe.flags.friday_dispatch_context = None


# --- helpers ----------------------------------------------------------------


def _review_interval() -> int:
	"""The memory-review cadence (turns between reviews); 0 = off."""
	try:
		from frappe.friday_core.doctype.agent_settings.agent_settings import get_agent_settings

		return int(get_agent_settings().get("memory_review_interval") or 0)
	except Exception:
		return 0


def _skill_interval() -> int:
	"""The skill-proposal-review cadence; 0 = off (default until validated)."""
	try:
		from frappe.friday_core.doctype.agent_settings.agent_settings import get_agent_settings

		return int(get_agent_settings().get("skill_review_interval") or 0)
	except Exception:
		return 0


def _resolve_review_provider():
	"""The provider to run the review on — Agent Settings.review_model, else None.

	None lets `run_turn` resolve the agent's own provider (default). A bad/blank
	value also degrades to None rather than failing the review.
	"""
	try:
		from frappe.friday_core.doctype.agent_settings.agent_settings import get_agent_settings

		name = (get_agent_settings().get("review_model") or "").strip()
		if not name:
			return None
		from frappe.friday_core.llm.provider import get_provider_by_name

		return get_provider_by_name(name)
	except Exception:
		frappe.logger("friday.self_review").warning(
			"review_model could not be resolved; using the agent's own provider", exc_info=True
		)
		return None


def _surface(
	session_id: str,
	profile_name: str,
	reply: "str | None",
	*,
	prefix: str = "💾 Learned: ",
	skip_phrase: str = "nothing to save",
) -> None:
	"""Post a review note ("💾 Learned: …" / "📝 Skill proposal: …") to the session.

	The per-item audit is the Execution Log each `remember` / `propose_skill_change`
	call writes; this is the human-visible channel note. Silent when the review's
	reply is empty or its "nothing" sentinel.
	"""
	text = (reply or "").strip()
	if not text or skip_phrase in text.lower():
		return
	platform = (
		frappe.db.get_value(
			"Chat Message",
			{"session_id": session_id, "direction": "inbound"},
			"platform",
			order_by="creation desc",
		)
		or ""
	)
	frappe.get_doc(
		{
			"doctype": "Chat Message",
			"session_id": session_id,
			"platform": platform,
			"direction": "outbound",
			"sender_id": profile_name,
			"agent_profile": profile_name,
			"content": f"{prefix}{text}",
			"timestamp": frappe.utils.now_datetime(),
			"processed": 1,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job, own txn
