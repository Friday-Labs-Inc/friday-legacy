# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Share a project's deliverables into its chat channel (Design 73, use-case #5).

PLAIN ENGLISH
=============
Design 66b made the agent's finished work real Frappe Files attached to the
Project (``attach-deliverable``), and Slice 5 materialized the deliverable
package (``deliverable-*.md/.pdf``). But a human in the project's chat room still
couldn't GET the files — asked to "share those files here", the agent had no
tool, so it refused or (worse) guessed a filename.

This is the missing verb: **post the project's deliverable files INTO the
channel**, as real downloadable attachments, sent by the Friday bot.

Scope (v0.1)
============
The PROJECT PACKAGE — the ``deliverable-*`` Files attached to the Project itself
(the assembled md + pdf), not every per-task artifact. That's what someone means
by "the deliverables". Per-task files stay reachable via ``list-project-files``.

Governance
==========
- The project comes from the CONVERSATION's channel — never a free parameter —
  so an agent cannot post one project's files into another project's room.
- Posts as the Friday bot (no self-loop: bot messages skip the inbound hook).
- Never invents anything: no deliverables -> it says so plainly.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.agent_runner.dispatcher import register_skill_handler

SHARE_DELIVERABLES = "share-deliverables"
_DELIVERABLE_PREFIX = "deliverable-"
_FRIDAY_BOT = "Friday"


def share_deliverables(skill_name: str, parameters: dict) -> dict:
	"""Post the current project's deliverable package into its Raven channel.

	The project + channel both come from the conversation session, so in a
	project room the agent calls this with no arguments. Returns a short summary
	naming the files it posted (or a plain "nothing to share" when there are
	none) — it never fabricates a deliverable that doesn't exist.
	"""
	ctx = frappe.flags.get("friday_dispatch_context") or {}
	session_id = (ctx.get("session_id") or "").strip()

	# The files post into the conversation's OWN channel — never a guessed one.
	# Raven is optional: check the table before querying it (an absent table
	# raises, and on Postgres that aborts the caller's whole transaction).
	if (
		not session_id
		or not frappe.db.table_exists("Raven Channel")
		or not frappe.db.exists("Raven Channel", session_id)
	):
		raise ValueError(
			"share-deliverables only works inside a project's chat channel "
			"(it posts the files into that channel)."
		)

	from frappe.friday_core.llm.memory import project_for_session

	project = project_for_session(session_id)
	if not project:
		raise ValueError(
			"This channel isn't linked to a project, so there are no project deliverables to share here."
		)

	# Scope = the project PACKAGE: deliverable-* Files on the Project itself.
	files = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Project",
			"attached_to_name": project,
			"file_name": ["like", f"{_DELIVERABLE_PREFIX}%"],
		},
		fields=["name", "file_name", "file_url"],
		order_by="creation asc",
	)
	if not files:
		return {
			"result": f"No deliverable files are attached to '{project}' yet — nothing to share.",
			"posted_files": [],
			"project": project,
		}

	bot = frappe.get_doc("Raven Bot", _FRIDAY_BOT)
	posted: list[str] = []
	for row in files:
		url = row.get("file_url")
		if not url:
			continue
		bot.send_message(channel_id=session_id, file=url)
		posted.append(row.get("file_name") or row.get("name"))

	if not posted:
		return {
			"result": f"The deliverables on '{project}' have no shareable file URL.",
			"posted_files": [],
			"project": project,
		}

	# The files went out via bot.send_message (no Chat Message row), so the agent's
	# own transcript wouldn't record the share. Mirror a compact note into the
	# session so its next turn knows what it posted (best-effort; never fatal).
	from frappe.friday_core.gateway.mirror import mirror_to_session

	mirror_to_session(
		session_id,
		f"Shared {len(posted)} deliverable file(s) into this channel: {', '.join(posted)}.",
		source_label=SHARE_DELIVERABLES,
	)

	return {
		"result": f"Shared {len(posted)} deliverable file(s) into this channel: {', '.join(posted)}.",
		"posted_files": posted,
		"project": project,
	}


register_skill_handler(SHARE_DELIVERABLES, share_deliverables)
