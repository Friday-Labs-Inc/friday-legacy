# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Deliverable-as-Frappe-File tools (design 66b).

Why this exists, in plain English
=================================
Before today, the agent's "deliverables" were DocType rows — a Brand
Direction row with concept_story text, a Task with result JSON, a memory
note. None of them were FILES. So when an operator opened a Project to
see "what did the agents produce", the Desk attachment sidebar was
empty — because nothing the agent did had ever been written through
Frappe's File layer.

The Friday-beats-Hermes insight (again)
========================================
Hermes builds file handling by hand: a tool to write a file, a tool to
list, a tool to read. **Frappe gives us this for free**: the ``File``
DocType, attached to any other DocType via ``attached_to_doctype`` +
``attached_to_name``, with private/public flags, with native UI in the
Desk attachment sidebar, with permission gating against the parent.
We just need three thin tools that route content through it.

The Project becomes the home of the agent's finished work
=========================================================
A deliverable saved with ``attach-deliverable`` lands as a File row on
the Project. The Desk shows it in the attachment sidebar the moment it
lands — operators see the finished work without any custom UI we wrote.

Governance contract
===================
1. Every attach checks ``frappe.has_permission(parent_doctype, "write",
   parent_name)`` for the agent's User. Denials collapse to one error
   (``denied_or_unreachable``) so an agent cannot probe forbidden
   project existence.
2. Files default to ``is_private=1`` (v0.1 stance — public file URLs
   are too dangerous to grant by accident).
3. ``get-project-file`` verifies the requested File's
   ``attached_to_name`` matches the project the caller named, so an
   agent cannot guess a file name across projects.
4. ``list-project-files`` queries the File table with the
   ``attached_to_doctype`` + ``attached_to_name`` filter — Frappe's
   native shape — so the list IS the Desk sidebar's list.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.agent_runner.dispatcher import register_skill_handler
from frappe.utils.file_manager import save_file

ATTACH_DELIVERABLE = "attach-deliverable"
LIST_PROJECT_FILES = "list-project-files"
GET_PROJECT_FILE = "get-project-file"

# Hard cap so an agent cannot pull every File on the bench in one call.
MAX_LIST_FILES = 200


def _calling_profile() -> str:
	ctx = frappe.flags.get("friday_dispatch_context") or {}
	return (ctx.get("agent_profile") or "").strip()


def _resolve_parent(parameters: dict) -> tuple[str, str]:
	"""Return (doctype, name) of the parent the file attaches to.

	Exactly one of ``project_name`` or ``task_name`` is required. Raises
	ValueError if neither (or both) is supplied.
	"""
	project_name = (parameters.get("project_name") or "").strip()
	task_name = (parameters.get("task_name") or "").strip()
	if project_name and task_name:
		raise ValueError("attach-deliverable: pass either project_name OR task_name, not both")
	if project_name:
		return "Project", project_name
	if task_name:
		return "Task", task_name
	raise ValueError(
		"attach-deliverable requires either 'project_name' (e.g. 'PRJ-1') or 'task_name' (e.g. 'TSK-42')"
	)


def attach_deliverable(skill_name: str, parameters: dict) -> dict:
	"""
	Save ``content`` as a private Frappe File attached to a Project (or Task).

	Parameters
	----------
	project_name OR task_name : str (exactly one)
	    The parent record the file attaches to.
	file_name : str
	    The filename as it should appear in the Desk attachment sidebar.
	content : str or bytes
	    The deliverable's bytes. Text is encoded as UTF-8 by Frappe's
	    save_file; bytes are passed through unchanged.
	is_private : bool, default True
	    v0.1 stance: private by default. Public requires explicit opt-in.
	description : str, optional
	    Logged in the response; v0.1 not persisted (no description field
	    on the File DocType by default).
	"""
	content = parameters.get("content")
	if content is None or content == "":
		raise ValueError("attach-deliverable requires non-empty 'content' (str or bytes)")
	file_name = (parameters.get("file_name") or "").strip()
	if not file_name:
		raise ValueError(
			"attach-deliverable requires 'file_name' — the filename as it will appear in the Desk sidebar"
		)

	profile = _calling_profile()
	if not profile:
		raise ValueError("attach-deliverable could not determine the calling agent profile")

	parent_doctype, parent_name = _resolve_parent(parameters)

	# Denied-and-unreachable collapse to the same error so an agent cannot
	# probe forbidden project existence by attaching. The `result` string is
	# what the LLM actually sees (dispatcher contract) — without it a denial
	# rendered as a bare "Done.", reading like success.
	if not frappe.db.exists(parent_doctype, parent_name):
		return {
			"result": f"Could not attach: {parent_doctype} '{parent_name}' is not reachable (missing or not permitted).",
			"error": "denied_or_unreachable",
			"parent": f"{parent_doctype}/{parent_name}",
		}
	if not frappe.has_permission(parent_doctype, "write", doc=parent_name):
		return {
			"result": f"Could not attach: {parent_doctype} '{parent_name}' is not reachable (missing or not permitted).",
			"error": "denied_or_unreachable",
			"parent": f"{parent_doctype}/{parent_name}",
		}

	is_private = 1 if parameters.get("is_private", True) else 0
	# save_file accepts str or bytes; let Frappe handle the encoding (it
	# does utf-8 internally for str). DO NOT coerce bytes — that would
	# corrupt binary deliverables (PDFs, images, etc.).
	file_doc = save_file(file_name, content, parent_doctype, parent_name, is_private=is_private)

	description = (parameters.get("description") or "").strip()
	return {
		"result": f"Saved '{file_name}' as {file_doc.name} attached to {parent_doctype}/{parent_name}",
		"file_name": file_doc.name,
		"file_url": getattr(file_doc, "file_url", None),
		"attached_to": f"{parent_doctype}/{parent_name}",
		"is_private": bool(is_private),
		"description": description,
	}


def list_project_files(skill_name: str, parameters: dict) -> dict:
	"""
	List the Frappe Files attached to a Project — the same set the Desk
	sidebar shows. Permission-gated against the Project.
	"""
	project_name = (parameters.get("project_name") or "").strip()
	if not project_name:
		raise ValueError("list-project-files requires 'project_name' (e.g. 'PRJ-1')")

	profile = _calling_profile()
	if not profile:
		raise ValueError("list-project-files could not determine the calling agent profile")

	if not frappe.db.exists("Project", project_name):
		return {
			"result": f"Project '{project_name}' is not reachable (missing or not permitted).",
			"error": "denied_or_unreachable",
			"project_name": project_name,
		}

	if not frappe.has_permission("Project", "read", doc=project_name):
		return {
			"result": f"You don't have permission to list files on project '{project_name}'.",
			"project_name": project_name,
			"files": [],
			"denied": True,
		}

	limit = max(1, min(MAX_LIST_FILES, int(parameters.get("limit") or MAX_LIST_FILES)))
	rows = frappe.db.get_all(
		"File",
		filters={
			"attached_to_doctype": "Project",
			"attached_to_name": project_name,
		},
		fields=["name", "file_name", "file_url", "is_private", "file_size", "creation"],
		limit_page_length=limit,
		order_by="creation desc",
	)
	# `result` is the ONLY thing the LLM sees (dispatcher renders
	# outcome["result"], defaulting to "Done." — the E2E bug: the agent got
	# "Done." instead of the file list and fabricated filenames). Render the
	# listing as text, with the exact ids/names to pass to get-project-file.
	if rows:
		listing = "\n".join(
			f"- {r['file_name']}  (id: {r['name']}, {r.get('file_size') or '?'} bytes)" for r in rows
		)
		result_text = (
			f"{len(rows)} file(s) attached to project '{project_name}':\n{listing}\n"
			"Read one with get-project-file using either the id or the file name."
		)
	else:
		result_text = f"No files are attached to project '{project_name}' yet."
	return {
		"result": result_text,
		"project_name": project_name,
		"files": rows,
		"row_count": len(rows),
	}


def get_project_file(skill_name: str, parameters: dict) -> dict:
	"""
	Read the content of one File attached to a Project. Verifies the
	file actually belongs to the named project — no cross-project guess.

	The `file_name` param accepts EITHER the File docname (e.g. `a1d916290f`)
	or the human filename (e.g. `friday-labs-design-system.md`) — real agent
	behavior on a live E2E showed the confusion is easy: `list-project-files`
	returns both `name` (docname) AND `file_name` (human) per file, and the
	agent guessed `file_name` was the human one. A missed guess used to
	collapse to `denied_or_unreachable` (identical to a permission denial),
	which the agent couldn't self-correct from. Now we first try the docname,
	then fall back to a project-scoped lookup by human filename — the scope
	still guards against cross-project guesses.
	"""
	project_name = (parameters.get("project_name") or "").strip()
	file_name = (parameters.get("file_name") or "").strip()
	if not project_name or not file_name:
		raise ValueError(
			"get-project-file requires both 'project_name' and 'file_name' "
			"(the File docname, e.g. `a1d916290f`, OR the human file_name, "
			"e.g. `friday-labs-design-system.md` — both work)"
		)

	profile = _calling_profile()
	if not profile:
		raise ValueError("get-project-file could not determine the calling agent profile")

	# Refuse permission denial AND not-found AND "this file belongs to a
	# different project" all with the same error shape — the agent cannot
	# probe.
	if not frappe.has_permission("Project", "read", doc=project_name):
		return {
			"result": (
				f"File '{file_name}' is not readable on project '{project_name}' "
				"(missing, not attached to this project, or not permitted). Call "
				"list-project-files first and use a listed id or file name."
			),
			"error": "denied_or_unreachable",
			"project_name": project_name,
			"file_name": file_name,
		}

	# Resolve docname vs human filename. Try docname first (the documented
	# shape), then fall back to a project-scoped lookup by human filename.
	# The project-scope is load-bearing — it means an agent guessing a
	# filename it saw on a different project still gets `denied_or_unreachable`,
	# never a cross-project leak.
	resolved_name = file_name
	if not frappe.db.exists("File", file_name):
		resolved_name = frappe.db.get_value(
			"File",
			{
				"attached_to_doctype": "Project",
				"attached_to_name": project_name,
				"file_name": file_name,
			},
			"name",
		)
		if not resolved_name:
			return {
				"result": (
					f"File '{file_name}' is not readable on project '{project_name}' "
					"(missing, not attached to this project, or not permitted). Call "
					"list-project-files first and use a listed id or file name."
				),
				"error": "denied_or_unreachable",
				"project_name": project_name,
				"file_name": file_name,
			}

	try:
		file_doc = frappe.get_doc("File", resolved_name)
	except Exception:
		return {
			"result": (
				f"File '{file_name}' is not readable on project '{project_name}' "
				"(missing, not attached to this project, or not permitted). Call "
				"list-project-files first and use a listed id or file name."
			),
			"error": "denied_or_unreachable",
			"project_name": project_name,
			"file_name": file_name,
		}

	if (
		getattr(file_doc, "attached_to_doctype", None) != "Project"
		or getattr(file_doc, "attached_to_name", None) != project_name
	):
		return {
			"result": (
				f"File '{file_name}' is not readable on project '{project_name}' "
				"(missing, not attached to this project, or not permitted). Call "
				"list-project-files first and use a listed id or file name."
			),
			"error": "denied_or_unreachable",
			"project_name": project_name,
			"file_name": file_name,
		}

	try:
		content = file_doc.get_content()
	except Exception:
		return {
			"result": (
				f"File '{file_name}' is not readable on project '{project_name}' "
				"(missing, not attached to this project, or not permitted). Call "
				"list-project-files first and use a listed id or file name."
			),
			"error": "denied_or_unreachable",
			"project_name": project_name,
			"file_name": file_name,
		}

	# `result` is what the LLM sees (dispatcher contract). The E2E bug: content
	# lived only under "content", so the model received a bare "Done." and could
	# never read the human reviewer's design system.
	human_name = getattr(file_doc, "file_name", resolved_name)
	if isinstance(content, bytes):
		try:
			content = content.decode("utf-8")
		except UnicodeDecodeError:
			return {
				"result": (
					f"'{human_name}' is a binary file ({len(content)} bytes) — its raw "
					"content can't be shown as text."
				),
				"project_name": project_name,
				"file_name": human_name,
				"is_private": bool(getattr(file_doc, "is_private", 0)),
			}
	return {
		"result": f"Content of '{human_name}':\n\n{content}",
		"project_name": project_name,
		"file_name": human_name,
		"is_private": bool(getattr(file_doc, "is_private", 0)),
		"content": content,
	}


register_skill_handler(ATTACH_DELIVERABLE, attach_deliverable)
register_skill_handler(LIST_PROJECT_FILES, list_project_files)
register_skill_handler(GET_PROJECT_FILE, get_project_file)
