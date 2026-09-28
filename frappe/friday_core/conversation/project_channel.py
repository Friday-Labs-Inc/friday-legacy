# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Per-project conversation channel (Design 73, Slice 1).

When a Project is created, Friday provisions a dedicated Raven channel for it —
the room where the agent team, the human director, deliverables, and gate
discussions live. Lifecycle mirrors the project:

  - Project insert            -> provision_project_channel (create + link + seed)
  - Project -> Completed/Cancelled -> archive_project_channel (is_archived = 1)

Architecture (thin-skin, [[interface-style-a-conversational]]): the Project row
is the source of truth. The channel is a window onto it. Everything here is
best-effort and savepoint-guarded — a Raven hiccup, or Raven not installed at
all, must NEVER block creating or updating a Project.

Reuses the proven channel-creation pattern from
``surfaces.bootstrap_raven`` (Raven enrolls the channel creator as a member on
insert, lowercases channel names, and throws ``DuplicateEntryError`` on a
re-add — so member-adds are guarded with ``exists()``).
"""

from __future__ import annotations

import frappe

_logger = frappe.logger("friday.conversation")

# Reaching either of these means the project is finished -> archive its channel.
_CLOSED_STATUSES = frozenset({"Completed", "Cancelled"})


# ---------------------------------------------------------------------------
# doc_events hooks (registered in hooks.py for "Project")
# ---------------------------------------------------------------------------


def on_project_after_insert(doc, method=None) -> None:
	"""Provision a conversation channel for a newly-created Project."""
	_guard(lambda: provision_project_channel(doc.name))


def on_project_update(doc, method=None) -> None:
	"""Mirror project status onto the channel — archive when the project closes."""
	if doc.get("status") in _CLOSED_STATUSES:
		_guard(lambda: archive_project_channel(doc.name))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def provision_project_channel(project_name: str) -> "str | None":
	"""Create (idempotently) the Raven channel for a project; return its id.

	Returns None when Raven is not installed on this site. Idempotent: if the
	project is already linked to a live channel, that channel id is returned
	and nothing new is created.
	"""
	if not frappe.db.table_exists("Raven Channel"):
		return None

	project = frappe.get_doc("Project", project_name)

	existing = project.get("conversation_channel")
	if existing and frappe.db.exists("Raven Channel", existing):
		return existing

	# Raven enrolls the channel CREATOR as a member on insert, so the current
	# user must be a Raven User first (mirrors bootstrap_raven). Workspace must
	# exist because Raven autonames channels `<workspace>-<channel_name>`.
	from frappe.friday_core.surfaces.bootstrap_raven import (
		FRIDAY_BOT_NAME,
		_ensure_raven_user,
		_ensure_workspace,
	)

	_ensure_raven_user(frappe.session.user)
	workspace = _ensure_workspace()

	channel = frappe.get_doc(
		{
			"doctype": "Raven Channel",
			"channel_name": _channel_slug(project),
			"channel_description": _channel_description(project),
			# Open within the Friday workspace for v0.1 — simplest, no member
			# management. Private + an explicit team (assigned agents + human
			# director) is a Slice-2 refinement.
			"type": "Open",
			"workspace": workspace,
			# Raven's native back-link so the channel knows its Project.
			"linked_doctype": "Project",
			"linked_document": project.name,
		}
	)
	channel.insert(ignore_permissions=True)

	# Reverse link on the Project (console nav + idempotency). db_set fires no
	# document hooks — no second on_update, no recursion.
	frappe.db.set_value("Project", project.name, "conversation_channel", channel.name, update_modified=False)

	_ensure_bot_member(channel.name, FRIDAY_BOT_NAME)

	# Post the welcome AFTER the channel + members commit. Sending it inline
	# (same transaction as channel creation) fails — Raven's message path reads
	# committed channel/member state. enqueue_after_commit defers it to a fresh
	# transaction; `now` under test runs it inline so assertions are synchronous.
	frappe.enqueue(
		"frappe.friday_core.conversation.project_channel.post_seed_welcome",
		queue="short",
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		channel_id=channel.name,
		project_name=project.name,
	)
	return channel.name


def post_seed_welcome(channel_id: str, project_name: str) -> None:
	"""Post the opening context message to a freshly-provisioned channel.

	Runs as an after-commit job (see provision_project_channel) so the channel
	and its members are committed before Raven's message path reads them.
	Best-effort — a failed welcome never matters to correctness.
	"""
	if not frappe.db.exists("Raven Channel", channel_id):
		return
	if not frappe.db.exists("Project", project_name):
		return
	project = frappe.get_doc("Project", project_name)
	_seed_welcome(channel_id, project)


def archive_project_channel(project_name: str) -> None:
	"""Archive the project's channel when the project closes (best-effort)."""
	channel = frappe.db.get_value("Project", project_name, "conversation_channel")
	if not channel or not frappe.db.exists("Raven Channel", channel):
		return
	if not frappe.db.get_value("Raven Channel", channel, "is_archived"):
		frappe.db.set_value("Raven Channel", channel, "is_archived", 1, update_modified=False)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _channel_slug(project) -> str:
	"""A unique, readable channel name (Raven lowercases + hyphenates it).

	Prefer the backend_ref (e.g. FLI-001 -> proj-fli-001) for a short stable
	handle; fall back to the project docname, which is always unique.
	"""
	base = (project.get("backend_ref") or project.name or "project").strip()
	return f"proj-{base}"


def _channel_description(project) -> str:
	name = project.get("project_name") or project.name
	return f"Friday project room: {name}. Progress, deliverables, and gate decisions live here."


def _ensure_bot_member(channel_id: str, bot_name: str) -> None:
	"""Add the Friday bot to the channel so it can post. Guarded re-add."""
	bot_user = frappe.db.get_value("Raven Bot", bot_name, "raven_user")
	if not bot_user:
		return
	# Guard with exists() — Raven throws DuplicateEntryError on a re-add.
	if frappe.db.exists("Raven Channel Member", {"channel_id": channel_id, "user_id": bot_user}):
		return
	frappe.get_doc({"doctype": "Raven Channel Member", "channel_id": channel_id, "user_id": bot_user}).insert(
		ignore_permissions=True
	)


def _seed_welcome(channel_id: str, project) -> None:
	"""Post the opening context message AS THE FRIDAY BOT. Best-effort.

	Posting as the bot (not the session user) is both correct — project-room
	messages come *from Friday* — and a workaround for a Raven bug: the non-bot
	message path runs ``track_channel_visit`` (raven/utils.py:44), which looks up
	membership by the message ``owner`` but then inserts a member for
	``frappe.session.user`` — throwing ``DuplicateEntryError`` ("already a
	member") when that user is already in the channel. Bot messages skip that
	path entirely (``raven_message.on_update`` gates it on ``not is_bot_message``).
	"""
	from frappe.friday_core.surfaces.raven_adapter import FRIDAY_BOT_NAME

	if not frappe.db.exists("Raven Bot", FRIDAY_BOT_NAME):
		return
	name = project.get("project_name") or project.name
	desc = (project.get("description") or "").strip()
	text = (
		f"📁 **{name}** — project room opened.\n"
		+ (f"{desc}\n\n" if desc else "\n")
		+ "Friday posts progress and deliverables here. "
		+ "Talk to **@Friday** to give direction or ask for changes."
	)
	try:
		frappe.db.savepoint("friday_seed_channel")
		bot = frappe.get_doc("Raven Bot", FRIDAY_BOT_NAME)
		bot.send_message(channel_id=channel_id, text=text, markdown=True)
	except Exception:
		# A failed seed must not undo the channel itself — roll back only the
		# message attempt.
		try:
			frappe.db.rollback(save_point="friday_seed_channel")
		except Exception:
			pass


def _guard(fn) -> None:
	"""Run a channel side-effect best-effort: savepoint-guarded, never raises.

	A Raven failure (or Raven absent) must not break the Project save — the
	Project is the source of truth; the channel is a window onto it.
	"""
	try:
		frappe.db.savepoint("friday_project_channel")
		fn()
	except Exception:
		try:
			frappe.db.rollback(save_point="friday_project_channel")
		except Exception:
			pass
		_logger.warning("project channel side-effect failed (swallowed)", exc_info=True)
