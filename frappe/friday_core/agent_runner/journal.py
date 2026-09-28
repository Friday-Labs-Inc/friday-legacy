# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
The durable turn journal — the diary that makes a crashed turn resumable.

PLAIN ENGLISH
=============

While the ReAct loop runs one turn, everything it has done so far (which LLM
calls happened, which tools returned what) normally lives only in the worker
process's memory. If that process dies, the retry starts the whole turn from
scratch: every LLM call is paid again, every tool runs again.

This module writes each step to the `Turn Event` table the moment it happens
— an append-only diary keyed by `turn_id`. On retry, `rebuild()` reads the
diary and reconstructs the loop's exact in-memory state, so the turn picks up
from the last line instead of the beginning.

TWO PARTS
=========

- `TurnJournal` — the DB-facing half. `open()` loads a turn's diary;
  `record()` appends one line (savepoint-guarded + committed immediately, so
  a crash one millisecond later still finds it). A journaling failure NEVER
  breaks a turn — worst case we degrade to today's behaviour (re-run more).
- `rebuild()` — the PURE half. Events in, `ReplayState` out. No frappe, no
  DB; unit-testable anywhere.

REFERENCED DESIGN
=================
- `docs/design/93-durable-turn-journal.md` — the locked contract (Q1–Q6).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import frappe

# Event types — the six diary line kinds (design 93, Q3), plus the
# provider-failover hop (design 94, Q4).
EVENT_TURN_STARTED = "turn.started"
EVENT_LLM_RESPONSE = "llm.response"
EVENT_TOOL_RESULT = "tool.result"
EVENT_STEER_INJECTED = "steer.injected"
EVENT_TURN_COMPLETED = "turn.completed"
EVENT_REPLY_DELIVERED = "reply.delivered"
EVENT_PROVIDER_FAILOVER = "provider.failover"

_logger = frappe.logger("friday.agent_runner.journal")


@dataclass
class ReplayState:
	"""The ReAct loop's reconstructed in-memory state (design 93, Q4).

	- `messages` — the rebuilt conversation buffer.
	- `model` — the model the interrupted turn was using.
	- `iterations_used` — LLM calls already made; counts against the same
	  15-cycle budget so a crash can't grant extra iterations.
	- `pending_tool_calls` — calls the last llm.response asked for that have
	  no journaled result yet; the resume dispatches exactly these.
	- `tokens_used` — the last response's total_tokens (dispatch() audit arg).
	- `final_reply` — set when the crash happened AFTER the model's final
	  plain-text answer but BEFORE turn.completed: nothing to resume, just
	  finish the bookkeeping and return this.
	"""

	messages: list = field(default_factory=list)
	model: "str | None" = None
	iterations_used: int = 0
	pending_tool_calls: list = field(default_factory=list)
	tokens_used: int = 0
	final_reply: "str | None" = None


def rebuild(events: list[dict], profile_name: str, inject_steer) -> "ReplayState | None":
	"""Reconstruct the loop state from journal events. Pure — no DB.

	Returns None when there is nothing to replay: no `turn.started` yet, or
	the journal belongs to a different Agent Profile (a reconciler
	re-assignment must not replay another profile's prompt — design 93, Q4).

	The base is the LAST `turn.started` — a mid-turn context-overflow
	compression writes a fresh one, and replay must ignore everything before
	it (the compressed prompt superseded it).

	`inject_steer` is the runner's `_inject_steer` passed in, so replayed
	steers land in the conversation exactly the way live ones do (and this
	module needs no import from the runner).
	"""
	base_index = None
	for index, ev in enumerate(events):
		if ev["event_type"] == EVENT_TURN_STARTED:
			base_index = index
	if base_index is None:
		return None

	base = events[base_index]["payload"]
	if base.get("agent_profile") != profile_name:
		return None

	state = ReplayState(
		messages=[dict(m) for m in base.get("messages") or []],
		model=base.get("model"),
	)

	for ev in events[base_index + 1 :]:
		etype, payload = ev["event_type"], ev["payload"]

		if etype == EVENT_LLM_RESPONSE:
			state.iterations_used += 1
			state.tokens_used = payload.get("total_tokens") or 0
			content = payload.get("content") or ""
			tool_calls = payload.get("tool_calls")
			if tool_calls:
				state.messages.append(_assistant_wire_message(content, tool_calls))
				state.pending_tool_calls = list(tool_calls)
				state.final_reply = None
			elif content:
				# The model's final plain-text answer — the turn was done,
				# only the completion bookkeeping was lost.
				state.final_reply = content
			# empty content + no tools = an empty-retry; iteration consumed,
			# nothing appended.

		elif etype == EVENT_TOOL_RESULT:
			call_id = payload.get("tool_call_id") or ""
			state.messages.append(
				{"role": "tool", "tool_call_id": call_id, "content": payload.get("content") or ""}
			)
			state.pending_tool_calls = [c for c in state.pending_tool_calls if c.get("id") != call_id]

		elif etype == EVENT_STEER_INJECTED:
			inject_steer(state.messages, payload.get("text") or "")

		elif etype == EVENT_PROVIDER_FAILOVER:
			# Design 94 (Q4) — the turn switched providers mid-flight. A
			# resumed turn re-resolves from the profile's PRIMARY provider,
			# so the journaled model (possibly the backup's) must not be
			# pinned: None lets the resumed provider use its own default.
			state.model = None

	return state


def _assistant_wire_message(content: str, tool_calls: list[dict]) -> dict:
	"""The assistant turn in OpenAI wire shape, from journaled flat calls.

	Mirrors runner._assistant_message (kept separate so neither module
	imports the other; the shape is pinned by tests on both sides).
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


class TurnJournal:
	"""One turn's diary — load it, read it, append to it.

	Construct via `open()`. `record()` commits each line immediately
	(design 93, Q5): the whole point is surviving a crash, so a journal row
	must never sit in an uncommitted transaction. The insert is savepoint-
	guarded — a journal failure logs and degrades, it never poisons the
	Postgres transaction or breaks the turn.
	"""

	def __init__(self, turn_id: str, session_id: str, agent_profile: str, events: list[dict]):
		self.turn_id = turn_id
		self.session_id = session_id
		self.agent_profile = agent_profile
		self.events = events
		self._seq = (max((e["seq"] for e in events), default=0)) + 1 if events else 1

	@classmethod
	def open(cls, turn_id: str, session_id: str, agent_profile: str) -> "TurnJournal":
		"""Load the diary for `turn_id` (empty on a fresh turn)."""
		rows = frappe.get_all(
			"Turn Event",
			filters={"turn_id": turn_id},
			fields=["seq", "event_type", "payload"],
			order_by="seq asc",
		)
		events = []
		for row in rows:
			try:
				payload = (
					json.loads(row["payload"]) if isinstance(row["payload"], str) else (row["payload"] or {})
				)
			except (json.JSONDecodeError, TypeError):
				payload = {}
			events.append({"seq": row["seq"], "event_type": row["event_type"], "payload": payload})
		return cls(turn_id, session_id, agent_profile, events)

	def record(self, event_type: str, payload: dict) -> None:
		"""Append one diary line and commit. Never raises (design 93, Q5)."""
		seq = self._seq
		try:
			frappe.db.savepoint("turn_journal_record")
			frappe.get_doc(
				{
					"doctype": "Turn Event",
					"turn_id": self.turn_id,
					"session_id": self.session_id,
					"agent_profile": self.agent_profile,
					"seq": seq,
					"event_type": event_type,
					"payload": frappe.as_json(payload),
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		except Exception:
			try:
				frappe.db.rollback(save_point="turn_journal_record")
			except Exception:
				pass
			_logger.warning(
				f"journal write failed for turn {self.turn_id!r} ({event_type}); "
				"continuing — the turn degrades to a longer re-run on crash.",
				exc_info=True,
			)
			return
		self._seq = seq + 1
		self.events.append({"seq": seq, "event_type": event_type, "payload": payload})

	def completed_reply(self) -> "str | None":
		"""The journaled final reply, if this turn already finished."""
		for ev in self.events:
			if ev["event_type"] == EVENT_TURN_COMPLETED:
				return ev["payload"].get("reply")
		return None

	def is_delivered(self) -> bool:
		"""True when the gateway already wrote this turn's outbound row."""
		return any(ev["event_type"] == EVENT_REPLY_DELIVERED for ev in self.events)

	def rebuild(self, profile_name: str, inject_steer) -> "ReplayState | None":
		return rebuild(self.events, profile_name, inject_steer)
