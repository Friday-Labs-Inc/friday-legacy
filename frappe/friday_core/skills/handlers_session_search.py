# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
The `session_search` skill — let an agent search its OWN past conversations
(Design 89). Port of Hermes `tools/session_search_tool.py` (SQLite FTS5) to
Friday's Postgres `Chat Message` table.

PLAIN ENGLISH
=============

A long-lived agent answers thousands of messages and forgets them. This lets it
look back: "what did we decide about X last week?" runs a full-text search over
the agent's past `Chat Message` rows and returns the best matches.

GOVERNANCE (disclosed)
======================
Scoped to the CALLING agent's own messages (`agent_profile == caller`) — an agent
can never read another agent's transcripts. Read-only; `risk_level=low`.

The ranking SQL mirrors `llm/memory._recall_scored` (the proven `ts_rank_cd` +
`replace(plainto_tsquery,'&','|')` AND→OR pattern). Postgres-only; falls back to a
recency LIKE scan if the FTS column/index isn't present.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.agent_runner.dispatcher import register_skill_handler

SKILL_NAME = "session_search"
_MAX_RESULTS = 50
_SNIPPET = 240


def session_search(skill_name: str, parameters: dict) -> dict:
	"""Search the calling agent's past Chat Message rows. Returns ranked matches."""
	ctx = frappe.flags.get("friday_dispatch_context") or {}
	agent_profile = ctx.get("agent_profile")

	query = (parameters.get("query") or "").strip()
	if not query:
		raise ValueError("session_search requires a 'query'")
	limit = max(1, min(int(parameters.get("limit") or 10), _MAX_RESULTS))

	rows = _search(agent_profile, query, limit)
	if not rows:
		return {"result": f"No past messages found for {query!r}.", "matches": []}

	lines = [
		f"- [{r.get('direction')} · {r.get('session_id')} · {r.get('timestamp')}] "
		f"{(r.get('content') or '')[:_SNIPPET]}"
		for r in rows
	]
	return {
		"result": f"Found {len(rows)} past message(s) for {query!r}:\n" + "\n".join(lines),
		"matches": rows,
	}


def _search(agent_profile: str, query: str, limit: int) -> list[dict]:
	"""Ranked FTS over the agent's own Chat Message rows; recency-LIKE fallback."""
	if frappe.db.db_type == "postgres":
		try:
			# Mirror llm/memory._recall_scored: ts_rank_cd + the AND→OR plainto_tsquery
			# fix. The `@@` filter returns only ACTUAL matches (search, not recall).
			return frappe.db.sql(
				"""
				SELECT m.content AS content, m.session_id AS session_id,
				       m.timestamp AS timestamp, m.direction AS direction
				FROM `tabChat Message` m
				WHERE m.agent_profile = %(profile)s
				  AND m.content_search @@
				      replace(plainto_tsquery('english', %(q)s)::text, '&', '|')::tsquery
				ORDER BY ts_rank_cd(
				    m.content_search,
				    replace(plainto_tsquery('english', %(q)s)::text, '&', '|')::tsquery
				) DESC, m.creation DESC
				LIMIT %(limit)s
				""",
				{"profile": agent_profile, "q": query, "limit": limit},
				as_dict=True,
			)
		except Exception:
			frappe.logger("friday.memory").warning(
				"session_search FTS query failed; falling back to recency LIKE", exc_info=True
			)

	# Fallback (non-Postgres, or FTS column absent): plain substring + recency.
	return frappe.db.get_all(
		"Chat Message",
		filters={"agent_profile": agent_profile, "content": ("like", f"%{query}%")},
		fields=["content", "session_id", "timestamp", "direction"],
		order_by="creation desc",
		limit=limit,
	)


register_skill_handler(SKILL_NAME, session_search)
