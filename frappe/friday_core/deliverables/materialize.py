# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Artifact materialization (Design 73, Slice 5).

Turns an agent's task output — which otherwise lives only as text in
``Task.result`` — into real, downloadable files attached to the task:

  - ``deliverable-<task>.md``   — the source-of-truth Markdown
  - ``deliverable-<task>.pdf``  — a client-facing render (skipped gracefully
                                  if wkhtmltopdf isn't available)

When a project completes, ``assemble_project_package`` concatenates every
task's deliverable into one package attached to the Project.

Wiring (best-effort, enqueued after commit so rendering never blocks a save):
  - Task -> Completed     -> on_task_completed  -> materialize_task_deliverable
  - Project -> Completed  -> on_project_completed -> assemble_project_package

Idempotent: re-running a task replaces its prior ``deliverable-*`` files.
A task with no success content (a gate, an error, an empty result) produces
no artifact — we never fabricate a deliverable.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import escape_html

_logger = frappe.logger("friday.deliverables")

_PREFIX = "deliverable-"

# ---------------------------------------------------------------------------
# Design 96 Slice 2 — the CUSTOMER materialize layer.
#
# The internal deliverable-*.md/.pdf files above are working artifacts. What a
# paying client receives is different: branded PDFs with HUMAN names ("Friday
# Labs Inc — Brand Guidelines.pdf", not brand-guidelines8e02e8.md), and ONLY
# the finished work — an early E2E pushed a human reviewer's
# internal refinement notes to the customer because nothing marked the
# boundary. The boundary is the `is_customer_facing` flag on File (ensured by
# after_migrate below); the bridge pushes only flagged files.
# ---------------------------------------------------------------------------

CUSTOMER_FLAG_FIELD = "is_customer_facing"

# Phase-output file patterns → the human titles the customer sees. Matched by
# file_name prefix against the Friday Project's attached files; for patterns
# with multiple versions (refine rounds re-attach), the NEWEST wins. Anything
# not in the map (working notes, drafts) stays internal unless a human flags it
# customer-facing in Desk.
#
# The kernel ships the map EMPTY: which phase outputs a customer receives is
# domain knowledge. A domain app contributes its rows through the
# ``friday_customer_title_map`` hook — dotted paths to ``list[tuple[str, str]]``.
CUSTOMER_TITLE_MAP: list[tuple[str, str]] = []


def customer_title_map() -> list[tuple[str, str]]:
	"""The kernel's (empty) map plus every domain app's rows, first match wins."""
	rows = list(CUSTOMER_TITLE_MAP)
	for path in frappe.get_hooks("friday_customer_title_map") or []:
		try:
			rows.extend(frappe.get_attr(path))
		except Exception:
			frappe.logger("friday.deliverables").warning(
				f"friday_customer_title_map entry {path!r} failed to load", exc_info=True
			)
	return rows


def ensure_customer_facing_field() -> None:
	"""Idempotently add the `is_customer_facing` custom field to File. after_migrate-safe.

	Default 0: every file is internal unless the customer-materialize step (or the
	human CD, in Desk) marks it. The bridge's customer push filters on this flag.
	"""
	if frappe.db.exists("Custom Field", {"dt": "File", "fieldname": CUSTOMER_FLAG_FIELD}):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	create_custom_field(
		"File",
		{
			"fieldname": CUSTOMER_FLAG_FIELD,
			"fieldtype": "Check",
			"label": "Customer Facing",
			"default": "0",
			"insert_after": "is_private",
			"description": "Deliverables the customer receives. Set by the customer-materialize "
			"step; set it manually on files (e.g. final logo assets) that should ship.",
		},
	)


# ---------------------------------------------------------------------------
# Hook entry points (enqueued after commit by the workflow / project hooks)
# ---------------------------------------------------------------------------


def on_task_completed(task_name: str) -> None:
	"""After-commit job: materialize a completed task's deliverable."""
	_guard(lambda: materialize_task_deliverable(task_name))


def on_project_completed(project_name: str) -> None:
	"""After-commit job: assemble the project's deliverable package."""
	_guard(lambda: assemble_project_package(project_name))


def on_project_doc_update(doc, method=None) -> None:
	"""doc_events hook: assemble the package when a project reaches Completed."""
	if doc.get("status") == "Completed":
		frappe.enqueue(
			"frappe.friday_core.deliverables.materialize.on_project_completed",
			queue="short",
			enqueue_after_commit=True,
			now=bool(frappe.flags.in_test),
			project_name=doc.name,
		)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def materialize_task_deliverable(task_name: str) -> "dict | None":
	"""Write a completed task's output as attached md + pdf files.

	Returns a dict of created File names, or None if the task has no
	materializable content (gate / error / empty).
	"""
	task = frappe.get_doc("Task", task_name)
	content = _extract_markdown(task.get("result"))
	if not content:
		return None

	title = task.get("title") or task.name
	slug = _slug(task_name)
	body = _wrap_markdown(title, task.get("project"), task_name, content)

	_clear_prior(task_name, "Task")
	created = {"md": _attach(f"{_PREFIX}{slug}.md", body.encode("utf-8"), "Task", task_name)}
	pdf = _render_pdf(title, content)
	if pdf:
		created["pdf"] = _attach(f"{_PREFIX}{slug}.pdf", pdf, "Task", task_name)
	return created


def assemble_project_package(project_name: str) -> "dict | None":
	"""Concatenate every task's deliverable into one package on the Project."""
	tasks = frappe.get_all(
		"Task", filters={"project": project_name}, fields=["name", "title"], order_by="creation asc"
	)
	sections = []
	for t in tasks:
		content = _extract_markdown(frappe.db.get_value("Task", t["name"], "result"))
		if content:
			sections.append(f"## {t['title'] or t['name']}\n\n{content}")
	if not sections:
		return None

	project_title = frappe.db.get_value("Project", project_name, "project_name") or project_name
	body = f"# {project_title} — Deliverable Package\n\n" + "\n\n---\n\n".join(sections)
	slug = _slug(project_name)

	_clear_prior(project_name, "Project")
	created = {"md": _attach(f"{_PREFIX}{slug}.md", body.encode("utf-8"), "Project", project_name)}
	pdf = _render_pdf(f"{project_title} — Deliverable Package", body)
	if pdf:
		created["pdf"] = _attach(f"{_PREFIX}{slug}.pdf", pdf, "Project", project_name)
	return created


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_markdown(raw) -> "str | None":
	"""Pull the deliverable Markdown out of a Task.result value.

	Agentic success: ``{"status": "success", "summary": "<markdown>"}``.
	A non-success envelope (error), a gate (empty), or blank -> None (we never
	fabricate a deliverable). A plain-text result is returned as-is.
	"""
	if not raw:
		return None
	if isinstance(raw, dict):
		data = raw
	else:
		try:
			data = json.loads(raw)
		except Exception:
			text = str(raw).strip()
			return text or None
	if isinstance(data, dict):
		if data.get("status") and data.get("status") != "success":
			return None
		summary = (data.get("summary") or "").strip()
		return summary or None
	return None


def _wrap_markdown(title: str, project: "str | None", task_name: str, content: str) -> str:
	meta = f"*Project: {project or '—'} · Task: {task_name}*"
	return f"# {title}\n\n{meta}\n\n---\n\n{content}\n"


def _render_pdf(title: str, markdown_content: str, brand_context: "dict | None" = None) -> "bytes | None":
	"""Render markdown to a PDF. Returns None if rendering is unavailable.

	Uses Frappe's wkhtmltopdf wrapper; if that binary isn't installed the
	deliverable still ships as Markdown (best-effort, never raises).

	`brand_context` (Design 96) brands the render for the CUSTOMER:
	  {"company": str, "accent": "#hex", "logo_data_uri": "data:image/...;base64,..."}
	All keys optional — absent context = the plain internal render, unchanged.
	"""
	try:
		from frappe.utils import md_to_html
		from frappe.utils.pdf import get_pdf

		html = _deliverable_html(title, str(md_to_html(markdown_content)), brand_context)
		return get_pdf(html)
	except Exception:
		_logger.warning("PDF render unavailable for %s — shipping Markdown only", title, exc_info=True)
		return None


def _deliverable_html(title: str, body_html: str, brand_context: "dict | None" = None) -> str:
	"""The HTML shell around a rendered deliverable. Pure — testable without wkhtmltopdf."""
	ctx = brand_context or {}
	accent = str(ctx.get("accent") or "#111111")
	company = str(ctx.get("company") or "")
	logo = str(ctx.get("logo_data_uri") or "")

	header = ""
	if company or logo:
		logo_img = (
			f"<img src='{logo}' style='height: 28px; vertical-align: middle; margin-right: 12px;'/>"
			if logo
			else ""
		)
		header = (
			f"<div style='border-bottom: 2px solid {accent}; padding-bottom: 12px; margin-bottom: 24px;'>"
			f"{logo_img}<span style='font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; "
			f"color: #555;'>{escape_html(company)}</span></div>"
		)
	return (
		"<div style='font-family: sans-serif; line-height: 1.6; padding: 32px; color: #1a1a1a;'>"
		f"{header}"
		f"<h1 style='font-weight: 500; letter-spacing: -0.01em;'>{escape_html(title)}</h1>"
		f"{body_html}"
		"</div>"
	)


# ---------------------------------------------------------------------------
# Design 96 Slice 2 — materialize FOR THE CUSTOMER (branded PDFs, human names)
# ---------------------------------------------------------------------------


def select_customer_sources(files: "list[dict]") -> "list[tuple[str, dict]]":
	"""Pick the customer-deliverable set from a Project's File rows. Pure.

	`files` rows need {name, file_name, creation}. For each customer_title_map()
	pattern, the NEWEST matching .md wins (refine rounds re-attach new versions —
	the customer gets the latest, once). Returns [(human_title, file_row)] in map
	order. Anything unmatched (refinement notes, the CD's working docs, images)
	is NOT selected — internal stays internal unless the CD flags it himself.
	"""
	out: list[tuple[str, dict]] = []
	for prefix, human_title in customer_title_map():
		candidates = [
			f
			for f in files
			if str(f.get("file_name") or "").startswith(prefix)
			and str(f.get("file_name") or "").endswith(".md")
		]
		if not candidates:
			continue
		latest = sorted(candidates, key=lambda f: str(f.get("creation") or ""))[-1]
		out.append((human_title, latest))
	return out


def _work_item_context_for(work_item_doctype: str, work_item_name: str, project_name: str) -> dict:
	"""Assemble the render branding: the work-item's display name (its bundle's
	`display_name_field`) + the CD's logo if he flagged one."""
	from frappe.friday_core.engine import bundle

	display_field = bundle.fields_for(work_item_doctype).get("display_name_field")
	company = (
		frappe.db.get_value(work_item_doctype, work_item_name, display_field) if display_field else ""
	) or ""
	ctx: dict = {"company": company}
	# The CD's flagged logo (an image File he marked customer-facing) brands the PDFs.
	logo_rows = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Project",
			"attached_to_name": project_name,
			CUSTOMER_FLAG_FIELD: 1,
		},
		fields=["name", "file_name"],
	)
	for row in logo_rows:
		fname = str(row.get("file_name") or "").lower()
		if "logo" in fname and fname.rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "svg"):
			try:
				import base64

				content = frappe.get_doc("File", row["name"]).get_content()
				if isinstance(content, str):
					content = content.encode("utf-8")
				mime = "image/svg+xml" if fname.endswith(".svg") else f"image/{fname.rsplit('.', 1)[-1]}"
				ctx["logo_data_uri"] = f"data:{mime};base64,{base64.b64encode(content).decode()}"
				break
			except Exception:
				continue
	return ctx


def materialize_for_customer(work_item_doctype: str, work_item_name: str) -> "dict | None":
	"""Render the customer package: branded, human-named PDFs on the Project,
	flagged `is_customer_facing` so a domain app's push delivers them (and ONLY
	them, plus whatever assets the CD flagged himself).

	Called by the domain app when its work-item reaches delivery, BEFORE the
	push. Idempotent: prior customer PDFs are replaced. The Project is read
	through the bundle's `project_field`.
	"""
	from frappe.friday_core.engine import bundle

	project_field = bundle.fields_for(work_item_doctype)["project_field"]
	project = frappe.db.get_value(work_item_doctype, work_item_name, project_field)
	if not project:
		return None
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Project", "attached_to_name": project},
		fields=["name", "file_name", "creation"],
	)
	sources = select_customer_sources(files)
	if not sources:
		return None

	ctx = _work_item_context_for(work_item_doctype, work_item_name, project)
	company = ctx.get("company") or ""
	created: dict = {}
	for human_title, row in sources:
		try:
			content = frappe.get_doc("File", row["name"]).get_content()
			if isinstance(content, bytes):
				content = content.decode("utf-8")
		except Exception:
			continue
		display = f"{company} — {human_title}" if company else human_title
		pdf = _render_pdf(display, content, brand_context=ctx)
		out_name = f"{display}.pdf" if pdf else f"{display}.md"
		payload = pdf if pdf else content.encode("utf-8")
		# Replace a prior render of the same deliverable (re-delivery, refine rounds).
		for prior in frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Project", "attached_to_name": project, "file_name": out_name},
			pluck="name",
		):
			try:
				frappe.delete_doc("File", prior, force=True, ignore_permissions=True)
			except Exception:
				pass
		file_id = _attach(out_name, payload, "Project", project)
		frappe.db.set_value("File", file_id, CUSTOMER_FLAG_FIELD, 1, update_modified=False)
		created[human_title] = file_id
	return created or None


def _attach(file_name: str, content: bytes, dt: str, dn: str) -> str:
	from frappe.utils.file_manager import save_file

	f = save_file(file_name, content, dt, dn, is_private=1)
	return f.name


def _clear_prior(dn: str, dt: str) -> None:
	"""Remove this doc's prior ``deliverable-*`` files so re-runs don't pile up.

	Only touches files we created (the ``deliverable-`` prefix) — never a
	user-uploaded attachment.
	"""
	prior = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": dt,
			"attached_to_name": dn,
			"file_name": ["like", f"{_PREFIX}%"],
		},
		pluck="name",
	)
	for name in prior:
		try:
			frappe.delete_doc("File", name, force=True, ignore_permissions=True)
		except Exception:
			pass


def _slug(name: str) -> str:
	return "".join(c if c.isalnum() else "-" for c in str(name)).strip("-").lower()[:60]


def _guard(fn) -> None:
	"""Run a materialization side-effect best-effort: savepoint-guarded, never raises."""
	try:
		frappe.db.savepoint("friday_deliverable")
		fn()
	except Exception:
		try:
			frappe.db.rollback(save_point="friday_deliverable")
		except Exception:
			pass
		_logger.warning("deliverable materialization failed (swallowed)", exc_info=True)
