# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
The agent runner — the ReAct loop that produces a reply for one chat turn.

PLAIN ENGLISH
=============

Given an Agent Profile, a session ID, and the user's message, this function
returns the agent's reply text. It runs a **ReAct loop**: call the model, and
while the model keeps asking for tools, dispatch them, feed the results back,
and let the model observe and decide its next step — up to
MAX_REACT_ITERATIONS (15) cycles. The turn ends when:

  - the model returns a plain-text answer (no tool calls), or
  - a tool call is **denied** by the permission engine (a governance signal —
    we surface it, we don't let the model route around it), or
  - the iteration budget is exhausted.

The prompt the loop starts from is:
  - the agent's system prompt + conversation history (built by prompt_builder),
  - the list of permitted tools (loaded by the skill loader).

WHAT THIS MODULE DOES NOT DO
============================

- Does not write Chat Message rows. The gateway does that — **one inbound +
  one outbound row per turn**, regardless of how many loop iterations run.
- Does not check permissions itself. The dispatcher calls
  `permissions.matrix.check` before executing any skill; a *denial* breaks
  this loop (doc 48 §1 A.4).
- Does not run skills in a sandbox. That's the dispatcher's sandbox wrapper.
- Does not stream tokens. Deferred until a real-time surface needs it.

REFERENCED DESIGN
=================
- `docs/design/48-hermes-port-decisions.md` §1 — Feature A, the locked ReAct
  loop contract (decisions A.1–A.6).
"""

from __future__ import annotations

import hashlib
import json

import frappe
from frappe.friday_core.agent_runner.dispatcher import DispatchResult, dispatch
from frappe.friday_core.agent_runner.exceptions import TurnInterrupted
from frappe.friday_core.agent_runner.journal import (
	EVENT_LLM_RESPONSE,
	EVENT_PROVIDER_FAILOVER,
	EVENT_STEER_INJECTED,
	EVENT_TOOL_RESULT,
	EVENT_TURN_COMPLETED,
	EVENT_TURN_STARTED,
	TurnJournal,
)
from frappe.friday_core.agent_runner.message_hygiene import (
	sanitize_messages_surrogates,
	sanitize_surrogates,
)
from frappe.friday_core.gateway.interrupt import clear_interrupt, is_interrupt_requested
from frappe.friday_core.gateway.steer import clear_steer, drain_steer
from frappe.friday_core.llm import get_provider_for_profile
from frappe.friday_core.llm.compression import maybe_compress_session
from frappe.friday_core.llm.error_classifier import classify_api_error
from frappe.friday_core.llm.prompt_builder import build
from frappe.friday_core.llm.provider import (
	MAX_FAILOVER_HOPS,
	LLMError,
	get_fallback_provider,
)
from frappe.friday_core.llm.reasoning import strip_reasoning
from frappe.friday_core.llm.usage import record_usage
from frappe.friday_core.observability.emit import emit
from frappe.friday_core.skills.loader import load_for_profile

# A.1 — the loop is capped at 15 think/act cycles per turn. A module constant
# (not a per-profile field for v0.1, to avoid premature config surface) so
# tests can patch it.
MAX_REACT_ITERATIONS = 15

# A.2 — what the user sees when the loop runs out of budget.
_BUDGET_EXHAUSTED_SUFFIX = "\n\n[loop budget exhausted after {n} iterations]"
_EMPTY_REPLY_FALLBACK = "I'm unable to complete this in the time allotted."

# Port from Hermes (conversation_loop §"Empty response retry"): a model can
# return nothing usable — no content AND no tool call. Retry up to 3 times
# before giving up, instead of handing the user a blank turn.
MAX_EMPTY_RETRIES = 3
_EMPTY_RESPONSE_FALLBACK = (
	"The model returned an empty response after several retries. Please try rephrasing your request."
)

# Design 80 — on a context-window-overflow error, compress the session and retry
# the LLM call (Hermes' compress-then-restart, conversation_loop.py). Capped so a
# persistent overflow surfaces as a real error instead of looping.
MAX_COMPRESS_RETRIES = 1

# Design 83 — the clean reply written when an operator `/stop`s the turn. The
# already-executed tool calls stay (governed + audited + committed); only the
# ReAct loop stops.
_INTERRUPTED_REPLY = "(interrupted by operator)"


def run_turn(
	profile_name: str,
	session_id: str,
	inbound_content: str,
	heartbeat=None,
	allowed_skills: "set[str] | None" = None,
	skip_compression: bool = False,
	provider_override=None,
	turn_id: "str | None" = None,
) -> str:
	"""Produce one agent reply for one user message via the ReAct loop.

	Arguments:
	  - `profile_name`: the Agent Profile name (Frappe primary key).
	  - `session_id`: the conversation's session UUID.
	  - `inbound_content`: the user's message text.
	  - `allowed_skills`: when given, HARD-restrict the toolset to skills whose
	    name is in this set. Used by the self-improvement review (design 79) to
	    run a memory/skill-proposal-only turn — the model cannot reach any other
	    tool. `None` (the default) = the profile's full permitted toolset, i.e.
	    today's behaviour. Mirrors Hermes' `set_thread_tool_whitelist`.
	  - `skip_compression`: when True, do NOT run the session-compression pass.
	    The review turn (design 79) reads the live session as read-only context
	    and must never compress the real conversation. Default False = today's
	    behaviour.
	  - `turn_id`: when given, turns ON the durable turn journal (design 93):
	    every step is written to `Turn Event` as it happens, and a retry with
	    the SAME turn_id resumes from the journal instead of re-running the
	    turn from scratch. Callers pass a deterministic id (the inbound Chat
	    Message name; `task::<name>` for tasks). `None` (self-review, evals)
	    = journaling off = old behaviour.

	Returns the reply text the gateway writes to the single outbound Chat
	Message row for this turn.

	The loop (doc 48 §1):
	  1. Call the LLM with the running conversation.
	  2. No tool calls → that's the final answer; return it.
	  3. Tool calls → dispatch each in order; feed every result back as a
	     `{role:"tool", ...}` message; then re-prompt so the model observes
	     the results and decides the next step.
	  4. A permission *denial* breaks the loop and is surfaced to the user.
	  5. After MAX_REACT_ITERATIONS without a plain-text reply, return the last
	     assistant text with a budget-exhausted suffix.

	Tool problems never raise here — a tool *error* is fed back to the model so
	it can adapt (A.3). LLM transport errors propagate to the gateway, which is
	the last error catcher.
	"""
	# Sanitize lone surrogates from user input — clipboard pastes (Google Docs,
	# Word) inject them and they crash json.dumps in the model SDK. Hermes does
	# this at the top of run_conversation.
	if isinstance(inbound_content, str):
		inbound_content = sanitize_surrogates(inbound_content)

	# Design 83/84 (Q3/Q4) — clear any stale interrupt flag and steer slot at
	# entry. The session lock guarantees one turn per session, so only a `/stop`
	# or `/steer` issued DURING this turn will be seen by the loop below.
	clear_interrupt(session_id)
	clear_steer(session_id)

	# Design 93 — open the turn's diary. A journaled turn.completed means a
	# crash/retry re-entered a turn that already finished: return the reply
	# as-is — no LLM call, no tool run, the user never pays twice. Best-effort
	# like every journal touch: an open failure degrades to an unjournaled
	# turn, it never breaks one.
	journal = None
	if turn_id:
		try:
			journal = TurnJournal.open(turn_id, session_id, profile_name)
		except Exception:
			frappe.logger("friday.agent_runner.journal").warning(
				f"journal open failed for turn {turn_id!r}; running unjournaled", exc_info=True
			)
	if journal is not None:
		_done = journal.completed_reply()
		if _done is not None:
			return _done

	skill_definitions = load_for_profile(profile_name)

	# Design 79 — the self-improvement review runs a tool-restricted turn: keep
	# only the skills it is allowed to call (memory / skill-proposal). A None
	# allowlist (the default) leaves the profile's full toolset untouched.
	if allowed_skills is not None:
		skill_definitions = [s for s in skill_definitions if s.name in allowed_skills]

	# Feature C: fold old turns into a summary if this session has grown large,
	# BEFORE assembling the prompt — build() then sees the summary + the
	# shortened (uncompacted) tail. Best-effort: a compression failure must
	# never break the turn, so we log and continue with the full prompt.
	# Design 79 — a review turn skips this: it reads the live session as
	# read-only context and must not compress the real conversation.
	# Design 93 — a RESUMED turn skips it too: the replayed prompt is pinned
	# by the journal; compressing now would desync prompt and diary.
	replay = journal.rebuild(profile_name, _inject_steer) if journal is not None else None
	if not skip_compression and replay is None:
		try:
			maybe_compress_session(profile_name, session_id)
		except Exception as exc:
			frappe.logger("friday.compression").warning(
				f"Compression pass errored for session {session_id!r}: "
				f"{type(exc).__name__}; continuing with the full prompt."
			)

	prompt = build(
		profile_name=profile_name,
		session_id=session_id,
		inbound_content=inbound_content,
		tools=skill_definitions,
	)
	# Design 80 — the self-improvement review may run on a cheaper model
	# (Agent Settings.review_model); the caller passes it as provider_override.
	# Default (None) resolves the profile's own provider — today's behaviour.
	provider = provider_override or get_provider_for_profile(profile_name)

	# A working copy of the conversation that the loop appends to. On a
	# design-93 resume, the journal (not the freshly built prompt) is the
	# source of truth for the conversation — only `tools` come from build(),
	# so the model always sees the CURRENT skill definitions.
	tools = prompt["tools"]
	if replay is None:
		messages: list[dict] = list(prompt["messages"])
		model = prompt["model"]
		if journal is not None:
			journal.record(
				EVENT_TURN_STARTED,
				{"messages": messages, "model": model, "agent_profile": profile_name},
			)
	else:
		messages = replay.messages
		model = replay.model or prompt["model"]

	last_assistant_content = ""
	empty_retries = 0
	compress_retries = 0

	# Design 94 — provider failover bookkeeping: how many hops this turn has
	# taken, and which provider rows already ran (the cycle guard — A→B→A
	# must stop, not loop).
	_failover_hops = 0
	_visited_providers = {getattr(provider, "source_row_name", None)}

	# Design 93 — the crash happened AFTER the model's final plain-text answer
	# but BEFORE the completion bookkeeping. Nothing to resume: finish it.
	if replay is not None and replay.final_reply is not None:
		if journal is not None:
			journal.record(EVENT_TURN_COMPLETED, {"reply": replay.final_reply})
		return replay.final_reply

	# Design 93 — the interrupted turn still owes tool results from its last
	# journaled LLM response. Dispatch exactly those, then fall into the loop
	# so the model observes them and continues.
	if replay is not None and replay.pending_tool_calls:
		_terminal = _dispatch_and_journal(
			replay.pending_tool_calls, profile_name, session_id, replay.tokens_used, messages, journal
		)
		if _terminal is not None:
			return _terminal

	# Design 93 — replayed LLM calls count against the SAME 15-cycle budget;
	# a crash must not grant extra iterations.
	_start_iteration = replay.iterations_used if replay is not None else 0

	for _iteration in range(_start_iteration, MAX_REACT_ITERATIONS):
		# Design 83 — cooperative interrupt: an operator `/stop` set the flag.
		# Check it at the iteration boundary (before the next LLM call) and bail
		# cleanly. Friday's blocking provider call means this is the soonest the
		# turn can notice — see docs/design/83.
		if is_interrupt_requested(session_id):
			clear_interrupt(session_id)
			# Design 85 (Q3) — RAISE, don't return. A typed signal can't be mistaken
			# for a real reply, so the Task path marks an interrupted child Cancelled
			# (not Completed) and the chat pipeline writes a clean outbound.
			raise TurnInterrupted(_INTERRUPTED_REPLY)

		# Design 84 — cooperative steer: an operator `/steer`ed mid-turn. Drain
		# the nudge and let the model see it on THIS iteration's call. Hermes
		# frames it as part of the tool output ("User guidance: …") rather than a
		# new user demand — see docs/design/84.
		_steer = drain_steer(session_id)
		if _steer:
			_inject_steer(messages, _steer)
			if journal is not None:
				journal.record(EVENT_STEER_INJECTED, {"text": _steer})

		# Design 61b — heartbeat once per ReAct iteration so the durability
		# reconciler's executing-stale sweep distinguishes a long but healthy
		# agentic turn from a runner that died mid-execution. Optional + best-
		# effort: a heartbeat failure must never break the turn.
		if heartbeat is not None:
			try:
				heartbeat()
			except Exception:
				pass
		# Scrub surrogates from the whole message list before the API call —
		# reasoning/tool fields can carry them too (Hermes sanitizes pre-call).
		sanitize_messages_surrogates(messages)
		try:
			response = provider.chat(messages=messages, tools=tools, model=model)
		except LLMError as exc:
			# Design 80 — on a context-window overflow the classifier sets
			# should_compress. Compress the session, rebuild the (now shorter)
			# prompt, and retry once. Any other LLM error — or a second overflow —
			# propagates unchanged. Review turns (skip_compression) never retry.
			classified = classify_api_error(error=exc)
			if (
				classified.should_compress
				and not skip_compression
				and compress_retries < MAX_COMPRESS_RETRIES
			):
				compress_retries += 1
				frappe.logger("friday.compression").warning(
					f"Context overflow on session {session_id!r}; compressing and "
					f"retrying (attempt {compress_retries})."
				)
				try:
					maybe_compress_session(profile_name, session_id)
				except Exception:
					pass
				prompt = build(
					profile_name=profile_name,
					session_id=session_id,
					inbound_content=inbound_content,
					tools=skill_definitions,
				)
				messages = list(prompt["messages"])
				tools = prompt["tools"]
				model = prompt["model"]
				# Design 93 — the compressed prompt supersedes the old one;
				# a fresh turn.started re-bases the diary so replay uses IT.
				if journal is not None:
					journal.record(
						EVENT_TURN_STARTED,
						{"messages": messages, "model": model, "agent_profile": profile_name},
					)
				continue
			# Design 94 — provider failover. By the time an LLMError reaches
			# here, the provider's transport layer has already exhausted its
			# same-provider retry budget — switching to the configured backup
			# is the only move left that isn't "block and wait". The backup
			# takes over THIS turn, mid-conversation, and answers with its
			# own default model (Q3). No chain configured = raise as today.
			if _failover_hops < MAX_FAILOVER_HOPS:
				_next = get_fallback_provider(provider)
				_next_name = getattr(_next, "source_row_name", None) if _next is not None else None
				if _next is not None and _next_name not in _visited_providers:
					_from = getattr(provider, "source_row_name", None) or ""
					_failover_hops += 1
					_visited_providers.add(_next_name)
					provider = _next
					model = None  # Q3 — the backup's own default model
					if journal is not None:
						journal.record(
							EVENT_PROVIDER_FAILOVER,
							{"from": _from, "to": _next_name, "reason": classified.reason.value},
						)
					try:
						emit(
							"llm.failover",
							agent_profile=profile_name,
							trigger_source="runner_failover",
							summary=f"{_from} → {_next_name} ({classified.reason.value})",
							payload={"session_id": session_id},
						)
					except Exception:
						pass  # observability must never break a turn
					frappe.logger("friday.runner").warning(
						f"provider failover: {_from} → {_next_name} "
						f"({classified.reason.value}, session {session_id!r})"
					)
					continue
			raise
		# Usage accounting — one LLM Usage Log row per call (tokens + estimated
		# cost). record_usage never raises; a logging failure can't break a turn.
		record_usage(
			profile_name=profile_name,
			session_id=session_id,
			provider=provider,
			model=model or provider.get_default_model(),
			usage=response.get("usage") or {},
		)
		# Strip reasoning/<think> blocks before `content` is used as the reply
		# OR appended to history — reasoning models (e.g. MiniMax-M2) leak their
		# chain-of-thought, which must not reach the user or pollute context.
		content = strip_reasoning(response.get("content"))
		tool_calls = response.get("tool_calls")
		tokens_used = (response.get("usage") or {}).get("total_tokens", 0)

		if not tool_calls:
			# Empty-response retry — port from Hermes conversation_loop
			# §"Empty response retry". A model can return nothing usable (no
			# content, no tool call); retry up to MAX_EMPTY_RETRIES before
			# giving up, instead of handing the user a blank turn. DISCLOSED
			# divergences: Friday skips Hermes' fallback-provider switch
			# (single provider) and the reasoning-prefill nuance (strip_reasoning
			# already yields the "truly empty" check), and returns a friendly
			# message in place of Hermes' "(empty)" sentinel.
			if not content and empty_retries < MAX_EMPTY_RETRIES:
				empty_retries += 1
				# Design 93 — an empty response still consumed an iteration;
				# journal it so a resumed turn keeps the same budget.
				if journal is not None:
					journal.record(
						EVENT_LLM_RESPONSE,
						{"content": "", "tool_calls": None, "total_tokens": tokens_used},
					)
				frappe.logger("friday.runner").warning(
					f"Empty model response — retry {empty_retries}/{MAX_EMPTY_RETRIES} "
					f"(session {session_id!r})"
				)
				continue
			if not content:
				if journal is not None:
					journal.record(EVENT_TURN_COMPLETED, {"reply": _EMPTY_RESPONSE_FALLBACK})
				return _EMPTY_RESPONSE_FALLBACK
			# Plain-text reply — the agent is done.
			if journal is not None:
				journal.record(
					EVENT_LLM_RESPONSE,
					{"content": content, "tool_calls": None, "total_tokens": tokens_used},
				)
				journal.record(EVENT_TURN_COMPLETED, {"reply": content})
			return content

		# A usable (tool-calling) response resets the empty-response streak.
		empty_retries = 0

		# D (doc 51 §4.D) — drop duplicate (name, arguments) calls in this
		# response, then give every call a stable id (deterministic when the
		# provider omitted one) so the wire's assistant<->tool links hold and
		# identical calls don't churn ids across re-serialisation.
		tool_calls = _deduplicate_tool_calls(tool_calls)
		for _index, _call in enumerate(tool_calls):
			if not _call.get("id"):
				_call["id"] = _deterministic_call_id(
					_call.get("name", ""), _call.get("arguments", ""), _index
				)

		# Design 93 — journal the response with its CANONICAL tool calls
		# (post-dedup, ids assigned) so replay redispatches by stable id.
		if journal is not None:
			journal.record(
				EVENT_LLM_RESPONSE,
				{"content": content, "tool_calls": tool_calls, "total_tokens": tokens_used},
			)

		last_assistant_content = content
		# Put the assistant's turn (with its tool calls) back into the
		# conversation so the next call shows the model what it asked for.
		messages.append(_assistant_message(content, tool_calls))

		_terminal = _dispatch_and_journal(
			tool_calls, profile_name, session_id, tokens_used, messages, journal
		)
		if _terminal is not None:
			return _terminal
		# Loop continues: re-prompt so the model observes the tool results.

	# A.2 — hit the iteration cap without a clean plain-text reply.
	final = last_assistant_content or _EMPTY_REPLY_FALLBACK
	reply = final + _BUDGET_EXHAUSTED_SUFFIX.format(n=MAX_REACT_ITERATIONS)
	if journal is not None:
		journal.record(EVENT_TURN_COMPLETED, {"reply": reply})
	return reply


def _dispatch_and_journal(
	tool_calls: list[dict],
	profile_name: str,
	session_id: str,
	tokens_used: int,
	messages: list[dict],
	journal: "TurnJournal | None",
) -> "str | None":
	"""Dispatch tool calls sequentially, in order (A.6), journaling each result.

	Shared by the main loop and the design-93 resume path (which owes the
	pending calls of the last journaled LLM response). Appends every result to
	`messages` as a `{role:"tool", ...}` message (A.3). Returns a TERMINAL
	reply when the loop must stop — a permission denial (A.4) or an approval
	pause (H2) — or None to continue; terminal replies are journaled as
	turn.completed so a crash-retry never redoes a denied or gated turn.
	"""
	for tool_call in tool_calls:
		result = dispatch(
			tool_call=tool_call,
			agent_profile=profile_name,
			session_id=session_id,
			tokens_used=tokens_used,
		)
		if _is_permission_denial(result):
			# A.4 — the operator said NO. Break the loop and surface it;
			# letting the model silently route around it is a governance hole.
			if journal is not None:
				journal.record(EVENT_TURN_COMPLETED, {"reply": result.content, "reason": "permission_denial"})
			return result.content

		if result.pending_approval:
			# H2 — the skill needs human approval. Pause the turn and
			# surface the request; a human resumes it later via approve().
			if journal is not None:
				journal.record(EVENT_TURN_COMPLETED, {"reply": result.content, "reason": "pending_approval"})
			return result.content

		# A.3 — feed the tool result (success or error) back verbatim so
		# the model can observe it and adapt. The loop then continues.
		call_id = result.tool_call_id or tool_call.get("id", "")
		messages.append({"role": "tool", "tool_call_id": call_id, "content": result.content})
		if journal is not None:
			journal.record(EVENT_TOOL_RESULT, {"tool_call_id": call_id, "content": result.content})
	return None


def _assistant_message(content: str, tool_calls: list[dict]) -> dict:
	"""Rebuild the assistant turn in OpenAI wire shape for re-sending.

	The provider hands tool calls to the runner in Friday's flat canonical form
	(`{id, name, arguments}`, which the dispatcher consumes). To put that turn
	*back* into the conversation for the next API call, expand it to the OpenAI
	`{id, type:"function", function:{name, arguments}}` shape. `arguments` must
	be a JSON string on the wire.
	"""
	wire_calls = []
	for tc in tool_calls:
		args = tc.get("arguments", "{}")
		if not isinstance(args, str):
			args = json.dumps(args)
		wire_calls.append(
			{
				"id": tc.get("id", ""),
				"type": "function",
				"function": {"name": tc.get("name", ""), "arguments": args},
			}
		)
	return {"role": "assistant", "content": content or "", "tool_calls": wire_calls}


def _inject_steer(messages: list[dict], text: str) -> None:
	"""Append an operator steer to the running conversation (Design 84, Q2).

	Hermes-faithful: ride the nudge on the LAST tool result as
	"User guidance: {text}", so the model reads it as more context on what just
	happened rather than a new user demand (`conversation_loop.py:754`). At the
	no-tool-yet edge (iteration 0), there is no tool result to append to, so it
	goes in as a plain user message instead.

	Replaces the last element with a NEW dict rather than mutating in place — the
	tail dict may be shared with the prompt's message list (shallow-copied at
	turn start).
	"""
	marker = f"User guidance: {text}"
	if messages and messages[-1].get("role") == "tool":
		last = messages[-1]
		joined = f"{last.get('content') or ''}\n\n{marker}"
		messages[-1] = {**last, "content": joined}
	else:
		messages.append({"role": "user", "content": marker})


def _is_permission_denial(result: DispatchResult) -> bool:
	"""True only when a dispatch failed *because the operator denied it* (A.4).

	A denial is `success == False` AND the linked Execution Log row is
	`status == "rejected"`. A plain tool *error* (status "error") is NOT a
	denial — it gets fed back to the model instead of breaking the loop.
	Keeping this distinction in one helper keeps the loop readable.
	"""
	if result.success or not result.execution_log_name:
		return False
	status = frappe.db.get_value("Execution Log", result.execution_log_name, "status")
	return status == "rejected"


def _deduplicate_tool_calls(tool_calls: list[dict]) -> list[dict]:
	"""D.1 — drop tool calls whose (name, arguments) match an earlier call in the
	SAME response; keep the first. Ported from Hermes `_deduplicate_tool_calls`.
	Logs each drop so a model that repeats itself is visible. Within-response
	only (D.3) — cross-turn idempotency is explicitly deferred.
	"""
	seen: set[tuple[str, str]] = set()
	deduped: list[dict] = []
	for tc in tool_calls:
		key = (tc.get("name", ""), _arguments_key(tc.get("arguments", "")))
		if key in seen:
			frappe.logger("friday.agent_runner.runner").info(
				f"dropped duplicate tool call {tc.get('name', '')!r} "
				f"(identical name+arguments repeated in one response)"
			)
			continue
		seen.add(key)
		deduped.append(tc)
	return deduped


def _deterministic_call_id(name: str, arguments, index: int) -> str:
	"""D.2 — a stable id for a tool call the provider left id-less, derived from
	(name, arguments, index). Ported from Hermes `_deterministic_call_id`.
	Identical content yields an identical id (so re-serialising the same
	conversation doesn't churn ids and bust prompt caching); the index keeps two
	*different* calls in one response distinct.
	"""
	digest = hashlib.sha1(f"{name}:{_arguments_key(arguments)}:{index}".encode()).hexdigest()[:12]
	return f"call_{digest}"


def _arguments_key(arguments) -> str:
	"""Normalise a tool call's arguments to a stable string for comparison and
	hashing. Arguments arrive as a JSON string (OpenAI-shaped providers) or a
	dict; either way the same content yields the same key (sorted keys), so
	dedup and the deterministic id are independent of key order.
	"""
	if isinstance(arguments, str):
		try:
			arguments = json.loads(arguments)
		except (json.JSONDecodeError, TypeError):
			return arguments  # not JSON — compare/hash verbatim
	try:
		return json.dumps(arguments, sort_keys=True, separators=(",", ":"))
	except (TypeError, ValueError):
		return str(arguments)
