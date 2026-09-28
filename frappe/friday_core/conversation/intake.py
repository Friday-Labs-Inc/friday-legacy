# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""Streaming customer-intake turn — Friday's first synchronous, live front-door.

Every Friday surface today is async server-to-server: an inbound `Chat Message` row →
a `friday` worker → `run_turn` → an outbound row. That returns a single blocking
string with no token stream and no structured side-channel. An intake conversation
needs the opposite: a LIVE turn that (a) streams its reply token-by-token to the
caller and (b) emits structured "wizard deltas" — `{field, value, confidence}` — so a
form pre-fills itself as the customer talks.

This module is that lean path. It deliberately does NOT use the full tool-using
ReAct loop (intake takes no gated actions); it reuses the durable bits — the session
transcript (`Chat Message` rows) and the streaming provider — and adds two things on
top:

  1. **Token streaming** via the provider's `on_token` callback.
  2. **A structured-extraction pass**: a second, tool-less LLM call per turn that reads
     the conversation and emits deltas for any wizard field it can now fill. Deltas are
     produced by THIS deterministic pass, never by parsing the token stream — so the
     pre-fill channel is reliable independent of how the prose streamed.

DESIGN (testability)
====================
The pure pieces — `build_extraction_messages`, `parse_deltas`, `extract_deltas` — take
plain data + an injectable provider and never touch Frappe. `stream_intake_turn`
orchestrates them with injectable `history_fn` / `persist_fn` / `provider`, so the
whole turn is unit-testable with a fake provider and no DB. The field VOCABULARY is
injected (`fields`), never hardcoded — the consuming product's wizard
owns the field names; Friday stays semantic.
"""

from __future__ import annotations

import json
import re

# The extraction pass is a strict, tool-less classifier. It is told the field list and
# must return ONLY the fields it can fill from the conversation so far, with a
# confidence — never guessing. Kept separate from the conversational system prompt so
# the prose reply and the structured pull can't contaminate each other.
_EXTRACTION_SYSTEM = (
	"You extract structured intake fields from a customer conversation. You are given a "
	"list of FIELDS (name + what it means) and the conversation so far. Return ONLY the "
	"fields you can fill with reasonable confidence FROM WHAT THE CUSTOMER ACTUALLY SAID "
	"— never invent or guess. Respond with STRICT JSON and nothing else:\n"
	'{"deltas": [{"field": "<field name>", "value": "<extracted value>", '
	'"confidence": <0.0-1.0>}]}\n'
	"Omit a field entirely if the customer hasn't given it. Return an empty list if "
	"nothing is fillable yet."
)


def build_extraction_messages(transcript: str, fields: list[dict]) -> list[dict]:
	"""Assemble the (system, user) messages for the per-turn extraction pass.

	`fields` is `[{"name": ..., "description": ...}]` — the wizard vocabulary, injected
	by the caller. Factored out so a test can assert the prompt carries the field list
	+ the transcript without a model.
	"""
	field_lines = "\n".join(f"- {f['name']}: {f.get('description', '')}" for f in fields)
	user = f"FIELDS:\n{field_lines}\n\nCONVERSATION SO FAR:\n{transcript}"
	return [
		{"role": "system", "content": _EXTRACTION_SYSTEM},
		{"role": "user", "content": user},
	]


def _extract_json(text: str) -> dict | None:
	"""Pull a JSON object out of a possibly chatty/fenced model reply (robust)."""
	if not text:
		return None
	candidates = [text.strip()]
	fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
	if fence:
		candidates.append(fence.group(1))
	brace = re.search(r"\{.*\}", text, re.DOTALL)
	if brace:
		candidates.append(brace.group(0))
	for cand in candidates:
		try:
			parsed = json.loads(cand)
		except json.JSONDecodeError, TypeError:
			continue
		if isinstance(parsed, dict):
			return parsed
	return None


def parse_deltas(content: str, fields: list[dict]) -> list[dict]:
	"""Parse the extraction reply into validated deltas. Never raises.

	Drops anything malformed or for an unknown field name, and clamps confidence to
	[0,1] — a misbehaving extractor can't inject junk into the wizard.
	"""
	parsed = _extract_json(content or "")
	if not parsed or not isinstance(parsed.get("deltas"), list):
		return []
	known = {f["name"] for f in fields}
	out = []
	for d in parsed["deltas"]:
		if not isinstance(d, dict):
			continue
		name = str(d.get("field", "")).strip()
		if name not in known or "value" not in d:
			continue
		try:
			conf = float(d.get("confidence", 0.0))
		except TypeError, ValueError:
			conf = 0.0
		out.append({"field": name, "value": d["value"], "confidence": max(0.0, min(1.0, conf))})
	return out


def extract_deltas(transcript: str, fields: list[dict], provider, on_usage=None) -> list[dict]:
	"""Run the structured-extraction pass for one turn. Never raises → [] on any failure.

	`on_usage(usage_dict)` — when given — receives the extraction call's token usage so the
	caller can record an LLM Usage Log row for it. This pass IS a real model call and must
	be cost-audited like any other; the callback is best-effort (a usage hiccup can't break
	extraction) and keeps this function Frappe-free (the caller owns the logging).
	"""
	if not fields:
		return []
	try:
		resp = provider.chat(build_extraction_messages(transcript, fields))
		content = resp["content"] if isinstance(resp, dict) else getattr(resp, "content", "")
	except Exception:
		return []
	if on_usage is not None and isinstance(resp, dict):
		try:
			on_usage(resp.get("usage") or {})
		except Exception:
			pass
	return parse_deltas(content or "", fields)


def stream_intake_turn(
	session_id: str,
	message: str,
	*,
	system_prompt: str,
	fields: list[dict],
	provider=None,
	history_fn=None,
	persist_fn=None,
	on_token=None,
) -> dict:
	"""Run ONE intake turn: stream a reply, then extract wizard deltas. Returns a dict.

	Injectable seams (defaults are the real Frappe-backed ones):
	  provider    — built LLM provider (default: resolved from the intake profile).
	  history_fn  — `() -> [ {role, content} ]` prior turns (default: the session's
	                `Chat Message` rows, so the conversation survives a browser refresh).
	  persist_fn  — `(inbound, outbound) -> None` writes the two transcript rows.
	  on_token    — relays each streamed text delta to the live caller.

	Returns `{"reply": <str>, "deltas": [{field, value, confidence}], "session_id": ...}`.
	The conversational reply streams via `on_token`; the deltas come from a SEPARATE
	deterministic extraction pass (not parsed from the stream).
	"""
	provider = provider or _default_provider(system_prompt)
	history_fn = history_fn or (lambda: _default_history(session_id))
	persist_fn = persist_fn if persist_fn is not None else _default_persist

	history = history_fn() or []
	messages = [{"role": "system", "content": system_prompt}, *history, {"role": "user", "content": message}]

	resp = provider.chat(messages, on_token=on_token)
	reply = resp["content"] if isinstance(resp, dict) else getattr(resp, "content", "")
	# Strip any <think>…</think> reasoning a model leaked into the reply (MiniMax-M3
	# does — caught on the first live run). This cleans the returned reply, the persisted
	# transcript, and the extractor's input. NOTE: the LIVE token stream still carries the
	# raw tokens; hiding think blocks token-by-token is a surface-layer concern — the SSE
	# adapter should buffer until a think block closes before relaying (next slice).
	from frappe.friday_core.llm.reasoning import strip_reasoning

	reply = strip_reasoning(reply or "")

	if persist_fn is not None:
		try:
			persist_fn(session_id, message, reply)
		except Exception:
			pass  # a persistence hiccup must not lose the live reply already streamed

	# Build the transcript the extractor sees (history + this turn) and pull deltas.
	transcript = _transcript_text(history, message, reply)
	deltas = extract_deltas(transcript, fields, provider)

	return {"session_id": session_id, "reply": reply, "deltas": deltas}


# ---------------------------------------------------------------------------
# Frappe-backed defaults (kept thin; the logic above is pure + injectable)
# ---------------------------------------------------------------------------


def _transcript_text(history: list[dict], message: str, reply: str) -> str:
	"""Render history + this turn as a plain 'role: text' transcript for the extractor."""
	lines = [f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history]
	lines.append(f"user: {message}")
	lines.append(f"assistant: {reply}")
	return "\n".join(lines)


def _default_provider(system_prompt: str):
	# Resolved lazily so importing this module doesn't pull the provider stack.
	raise RuntimeError(
		"stream_intake_turn needs an explicit `provider` until the intake Agent Profile "
		"wiring lands (next slice). Pass provider=get_provider_for_profile('<intake>')."
	)


def _default_history(session_id: str) -> list[dict]:
	"""Load the session's transcript as chat messages (survives a browser refresh)."""
	import frappe

	rows = frappe.get_all(
		"Chat Message",
		filters={"session_id": session_id, "direction": ("in", ["inbound", "outbound"]), "compacted": 0},
		fields=["direction", "content"],
		order_by="creation asc",
		limit=40,
	)
	return [
		{"role": "user" if r["direction"] == "inbound" else "assistant", "content": r["content"] or ""}
		for r in rows
	]


# Chat Platform the default persister files transcript rows under. A surface
# that owns its own platform passes its own persist_fn (or `persist_for`).
DEFAULT_INTAKE_PLATFORM = "friday-intake"


def persist_for(platform: str):
	"""A persist_fn bound to `platform` — for surfaces that own a Chat Platform."""
	return lambda session_id, message, reply: _default_persist(session_id, message, reply, platform=platform)


def _default_persist(
	session_id: str, message: str, reply: str, platform: str = DEFAULT_INTAKE_PLATFORM
) -> None:
	"""Write the inbound + outbound transcript rows for this turn (session continuity)."""
	import frappe

	now = frappe.utils.now_datetime()
	for direction, content in (("inbound", message), ("outbound", reply)):
		frappe.get_doc(
			{
				"doctype": "Chat Message",
				"session_id": session_id,
				"direction": direction,
				"platform": platform,
				"content": content,
				"timestamp": now,
				"processed": 1,  # the streaming path already handled it; no worker pickup
			}
		).insert(ignore_permissions=True)


# A sample intake vocabulary for the demo below — illustrative only. A real
# surface injects its own field map; the kernel never hardcodes a domain's.
_DEMO_FIELDS = [
	{"name": "business_name", "description": "the company / brand name"},
	{"name": "industry", "description": "what the business does / its sector"},
	{"name": "audience", "description": "who the brand is for (target customers)"},
	{"name": "personality", "description": "the desired brand personality / vibe"},
]
_DEMO_SYSTEM = (
	"You are Friday's friendly intake assistant for a design studio. In 1-2 warm "
	"sentences, acknowledge what the customer told you and ask the single most useful "
	"next question to scope their project. Never ask more than one question at a time."
)


def run_demo(
	message: str = "Hi! We're launching Loop Coffee, a specialty roastery for remote workers.",
	provider_name: str = "Minimax",
) -> dict:
	"""Sandbox showcase: drive ONE real streaming intake turn, printing tokens live.

	    bench --site <sandbox> execute frappe.friday_core.conversation.intake.run_demo

	Streams a real reply token-by-token to stdout, then prints the structured wizard
	deltas the extraction pass pulled. No transcript rows are written (demo persist is a
	no-op). Real LLM calls — sandbox only.
	"""
	from frappe.friday_core.llm.provider import get_provider_by_name

	provider = get_provider_by_name(provider_name)
	print(f"\n=== Friday streaming intake demo (provider: {provider_name}) ===")
	print(f"customer> {message}\nfriday>   ", end="", flush=True)

	def _emit(tok):
		print(tok, end="", flush=True)

	out = stream_intake_turn(
		"intake-demo",
		message,
		system_prompt=_DEMO_SYSTEM,
		fields=_DEMO_FIELDS,
		provider=provider,
		history_fn=lambda: [],
		persist_fn=lambda *a: None,
		on_token=_emit,
	)
	print("\n\nextracted wizard deltas (live form pre-fill):")
	for d in out["deltas"]:
		print(f"  - {d['field']} = {d['value']!r}  (confidence {d['confidence']})")
	if not out["deltas"]:
		print("  (none yet)")
	return {"reply_chars": len(out["reply"]), "deltas": out["deltas"]}
