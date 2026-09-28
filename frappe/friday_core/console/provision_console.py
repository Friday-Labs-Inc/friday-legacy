# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Native-view provisioner for the project console (design 65b).

65a gave the Project and Task DocTypes the fields a console needs (dates,
%-complete, cost, color). 65b turns those into *views* — using Frappe's
**built-in** rendering, not bespoke UI:

- **Number Cards** — Active Projects, Tasks Executing, Tasks Blocked, Open
  Issues. The at-a-glance "is the machine busy / stuck" tiles.
- **Dashboard Charts** — Tasks by State (group-by bar) and Tasks Completed
  per day (timeseries line).
- **A "Task Pipeline" Kanban** — Task cards flowing across ``workflow_state``
  columns. This is the literal "see task executions" the user asked for. It
  only works because 65b also converts ``workflow_state`` from a free-text
  Data field to a **Select** over the canonical state set (``TASK_STATES``);
  Frappe's Kanban can only column on a Select. The conversion doubles as
  validation hardening — no task can be saved into a state outside the machine.
- **A "Projects" Workspace** — the Desk landing that lays the cards, charts and
  shortcuts out in one place. The ERPNext-style "project dashboard."
- **A "Friday" hub Workspace** — the top-level Desk landing that ties every
  Friday surface together: shortcut tiles for the daily-use pages plus nav
  cards grouping every Friday DocType by domain, so nothing has to be hunted
  for in global search. It sorts above "Projects" in the sidebar, so it's the
  first thing an operator lands on. (Frappe-native: Hermes, being a React app,
  has no Desk/workspace concept to port — this is a pure platform win from the
  fork, not a port.)

All of it is created idempotently on every ``after_migrate`` and is failure-
isolated: one artifact that errors is logged loudly and skipped, never aborting
the rest. We never ship these as ``is_standard`` file fixtures — they are plain
public DB records, so Frappe doesn't try to reconcile them against on-disk JSON.

Compare with Hermes: its dashboard hand-rolls every panel in React. Friday gets
Kanban, Gantt, charts and number cards from the platform it forked — we only
declare *what* to show. The surpass axis (a real project plane + live console)
lands in 65c; 65b is the structured-PM half the user asked for as "full ERPNext
project features without ERPNext."
"""

from __future__ import annotations

import json

import frappe

MODULE = "Friday Core"

# The canonical Task workflow states, in pipeline order. This list is the single
# source of truth shared by the Kanban columns AND the Task.workflow_state Select
# options — ``test_console_views`` pins the two together so they can't drift.
#
# ``ForceKilled`` (Design 83b) is the operator-/reconciler-killed terminal state.
# Distinct from ``Blocked`` (which is "stuck, waiting for a human or a dependency")
# because operators need to see at-a-glance which tasks they (or the auto-heal)
# have force-terminated — a Kanban column next to ``Blocked`` makes the signal
# visible during an incident without joining the Dispatcher Event table.
TASK_STATES = [
	"Pending",
	"Assigned",
	"Executing",
	"Review",
	"Completed",
	"Blocked",
	"Cancelled",
	"ForceKilled",
]

# Kanban column indicator colour per state (Frappe's fixed indicator palette).
_STATE_INDICATOR = {
	"Pending": "Gray",
	"Assigned": "Light Blue",
	"Executing": "Blue",
	"Review": "Purple",
	"Completed": "Green",
	"Blocked": "Red",
	"Cancelled": "Gray",
	"ForceKilled": "Orange",
}

WORKSPACE_NAME = "Projects"
KANBAN_NAME = "Task Pipeline"

# The top-level "Friday" hub workspace — the Desk landing that ties every Friday
# surface together for navigation. "Projects" (above) stays the monitoring
# dashboard (number cards + charts); this one is the front door: quick-access
# shortcut tiles for the daily-use surfaces, then nav cards grouping every
# Friday DocType by domain so nothing has to be found via global search.
HUB_WORKSPACE_NAME = "Friday"

# Shortcut tiles (the big buttons), in display order. Pages link by route slug,
# DocTypes by name; ``doc_view`` picks the list view they open into.
HUB_SHORTCUTS: list[dict] = [
	{"type": "Page", "label": "Studio", "link_to": "studio", "color": "Cyan"},
	{"type": "Page", "label": "Project Console", "link_to": "project-console", "color": "Green"},
	{"type": "Page", "label": "Dispatcher Console", "link_to": "dispatcher-console", "color": "Red"},
	{"type": "DocType", "label": "Projects", "link_to": "Project", "doc_view": "List", "color": "Blue"},
	{"type": "DocType", "label": "Tasks", "link_to": "Task", "doc_view": "List", "color": "Cyan"},
	{"type": "DocType", "label": "Agents", "link_to": "Agent Profile", "doc_view": "List", "color": "Purple"},
	{"type": "Page", "label": "Setup Friday", "link_to": "friday-setup", "color": "Orange"},
]

# Navigation cards: each is a titled group of DocType links. The link label is
# the DocType name itself — one source of truth, no separate display string.
HUB_CARDS: list[dict] = [
	{"label": "Work", "links": ["Project", "Task", "Issue", "Task Dependency"]},
	{"label": "Agents", "links": ["Agent Profile", "Agent Settings", "Agent Memory"]},
	{"label": "Models & Skills", "links": ["LLM Provider", "LLM Usage Log", "Skill", "MCP Server"]},
	{
		"label": "Conversations & Surfaces",
		"links": ["Chat Message", "Chat Platform", "Connector", "Connector Event"],
	},
	{
		"label": "Governance & Logs",
		"links": ["Permission Decision Log", "Execution Log", "Workflow Request", "Compaction Summary"],
	},
	{
		"label": "Observability",
		"links": ["Dispatcher Event", "Task Completion Summary"],
	},
]

# --- artifact specs --------------------------------------------------------

NUMBER_CARDS: list[dict] = [
	{
		"label": "Friday Active Projects",
		"document_type": "Project",
		"filters_json": json.dumps([["Project", "status", "in", ["Open", "In Progress"], False]]),
		"color": "#29CD42",
	},
	{
		"label": "Friday Tasks Executing",
		"document_type": "Task",
		"filters_json": json.dumps([["Task", "workflow_state", "=", "Executing", False]]),
		"color": "#1890FF",
	},
	{
		"label": "Friday Tasks Blocked",
		"document_type": "Task",
		"filters_json": json.dumps([["Task", "workflow_state", "=", "Blocked", False]]),
		"color": "#FF5858",
	},
	{
		"label": "Friday Open Issues",
		"document_type": "Issue",
		"filters_json": json.dumps([["Issue", "status", "=", "Open", False]]),
		"color": "#FFA00A",
	},
]

DASHBOARD_CHARTS: list[dict] = [
	{
		"chart_name": "Friday Tasks by State",
		"chart_type": "Group By",
		"document_type": "Task",
		"group_by_type": "Count",
		"group_by_based_on": "workflow_state",
		"type": "Bar",
		"number_of_groups": 0,
		"filters_json": "[]",
	},
	{
		"chart_name": "Friday Tasks Completed",
		"chart_type": "Count",
		"document_type": "Task",
		"timeseries": 1,
		"based_on": "completed_at",
		"timespan": "Last Month",
		"time_interval": "Daily",
		"type": "Line",
		"filters_json": "[]",
	},
]


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def provision_console() -> dict:
	"""Create every console view artifact idempotently. The ``after_migrate`` entry.

	Returns a summary ``{created, skipped, failed}``. Each artifact is attempted
	inside its own try/except so one failure can't abort the rest — the same
	failure-isolation contract as the agent-identity backfill (65a).
	"""
	created: list[str] = []
	skipped: list[str] = []
	failed: list[str] = []

	def _attempt(kind: str, key: str, fn) -> None:
		try:
			if fn():
				created.append(f"{kind}:{key}")
			else:
				skipped.append(f"{kind}:{key}")
		except Exception:
			frappe.log_error(
				title=f"provision_console: failed to create {kind} {key!r}",
				message=frappe.get_traceback(),
			)
			failed.append(f"{kind}:{key}")

	for spec in NUMBER_CARDS:
		_attempt("Number Card", spec["label"], lambda spec=spec: _ensure_number_card(spec))
	for spec in DASHBOARD_CHARTS:
		_attempt("Dashboard Chart", spec["chart_name"], lambda spec=spec: _ensure_dashboard_chart(spec))
	_attempt("Kanban Board", KANBAN_NAME, _ensure_kanban_board)
	# Hub before the monitoring workspace so both always land; order is otherwise
	# free (the hub has no dependency on the cards/charts/kanban above).
	_attempt("Workspace", HUB_WORKSPACE_NAME, _ensure_hub_workspace)
	_attempt("Workspace", WORKSPACE_NAME, _ensure_workspace)

	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	return {"created": created, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# Per-artifact ensure helpers (each returns True if it created the artifact)
# ---------------------------------------------------------------------------


def _ensure_number_card(spec: dict) -> bool:
	if frappe.db.exists("Number Card", spec["label"]):
		return False
	frappe.get_doc(
		{
			"doctype": "Number Card",
			"name": spec["label"],
			"label": spec["label"],
			"type": "Document Type",
			"document_type": spec["document_type"],
			"function": "Count",
			"filters_json": spec["filters_json"],
			"dynamic_filters_json": "[]",
			"is_public": 1,
			"show_percentage_stats": 1,
			"stats_time_interval": "Daily",
			"color": spec.get("color"),
			# DB record, not a file fixture — keep is_standard off so Frappe
			# doesn't try to reconcile it against on-disk JSON on migrate.
			"is_standard": 0,
		}
	).insert(ignore_permissions=True)
	return True


def _ensure_dashboard_chart(spec: dict) -> bool:
	if frappe.db.exists("Dashboard Chart", spec["chart_name"]):
		return False
	doc = {
		"doctype": "Dashboard Chart",
		"chart_name": spec["chart_name"],
		"chart_type": spec["chart_type"],
		"document_type": spec["document_type"],
		"type": spec.get("type", "Line"),
		"filters_json": spec.get("filters_json", "[]"),
		"dynamic_filters_json": "[]",
		"is_public": 1,
		"is_standard": 0,
		"number_of_groups": spec.get("number_of_groups", 0),
		"group_by_type": spec.get("group_by_type", "Count"),
	}
	# Only the fields each chart_type actually uses, so we don't set a date
	# field on a Group By chart (which Frappe would reject).
	for k in ("group_by_based_on", "timeseries", "based_on", "timespan", "time_interval"):
		if k in spec:
			doc[k] = spec[k]
	frappe.get_doc(doc).insert(ignore_permissions=True)
	return True


def _ensure_kanban_board() -> bool:
	if frappe.db.exists("Kanban Board", KANBAN_NAME):
		return False
	board = frappe.get_doc(
		{
			"doctype": "Kanban Board",
			"kanban_board_name": KANBAN_NAME,
			"reference_doctype": "Task",
			"field_name": "workflow_state",
			"private": 0,
			"filters": "[]",
			"fields": "[]",
			"show_labels": 0,
		}
	)
	for state in TASK_STATES:
		board.append(
			"columns",
			{
				"column_name": state,
				"status": "Active",
				"indicator": _STATE_INDICATOR.get(state, "Gray"),
				"order": "[]",
			},
		)
	board.insert(ignore_permissions=True)
	return True


def _ensure_workspace() -> bool:
	if frappe.db.exists("Workspace", WORKSPACE_NAME):
		return False
	ws = frappe.get_doc(
		{
			"doctype": "Workspace",
			"name": WORKSPACE_NAME,
			"label": WORKSPACE_NAME,
			"title": WORKSPACE_NAME,
			"module": MODULE,
			"type": "Workspace",
			"icon": "project",
			"public": 1,
			"is_hidden": 0,
			"content": _build_content(),
		}
	)
	for spec in NUMBER_CARDS:
		ws.append("number_cards", {"number_card_name": spec["label"], "label": spec["label"]})
	for spec in DASHBOARD_CHARTS:
		ws.append("charts", {"chart_name": spec["chart_name"], "label": spec["chart_name"]})

	# Shortcuts: setup wizard, the live console, then the Project list and Task Kanban.
	ws.append(
		"shortcuts",
		{"type": "Page", "link_to": "friday-setup", "label": "Friday Setup", "color": "Orange"},
	)
	ws.append(
		"shortcuts",
		{"type": "Page", "link_to": "project-console", "label": "Project Console", "color": "Green"},
	)
	ws.append(
		"shortcuts",
		{"type": "DocType", "link_to": "Project", "label": "Projects", "doc_view": "List", "color": "Blue"},
	)
	ws.append(
		"shortcuts",
		{
			"type": "DocType",
			"link_to": "Task",
			"label": "Task Pipeline",
			"doc_view": "Kanban",
			"kanban_board": KANBAN_NAME,
			"color": "Green",
		},
	)

	# Sidebar links card.
	ws.append("links", {"type": "Card Break", "label": "Work", "hidden": 0})
	ws.append(
		"links",
		{"type": "Link", "label": "Project", "link_type": "DocType", "link_to": "Project", "hidden": 0},
	)
	ws.append(
		"links",
		{"type": "Link", "label": "Task", "link_type": "DocType", "link_to": "Task", "hidden": 0},
	)
	ws.append("links", {"type": "Card Break", "label": "Monitoring", "hidden": 0})
	ws.append(
		"links",
		{"type": "Link", "label": "Issue", "link_type": "DocType", "link_to": "Issue", "hidden": 0},
	)

	ws.insert(ignore_permissions=True)
	return True


def _build_content() -> str:
	"""Build the Workspace ``content`` layout blob (a JSON-encoded string).

	Layout: a header, the four number cards in a row (col 3 each = 12), then the
	two charts side by side (col 6 each). Each block references a card/chart by
	the label used in the child row, which Frappe resolves at render time.
	Deterministic block ids (no randomness) so re-runs are stable.
	"""
	blocks: list[dict] = [
		{"id": "fridayhdr01", "type": "header", "data": {"text": "Projects", "col": 12}},
	]
	for i, spec in enumerate(NUMBER_CARDS):
		blocks.append(
			{
				"id": f"fridaycard{i}",
				"type": "number_card",
				"data": {"number_card_name": spec["label"], "col": 3},
			}
		)
	for i, spec in enumerate(DASHBOARD_CHARTS):
		blocks.append(
			{
				"id": f"fridaychart{i}",
				"type": "chart",
				"data": {"chart_name": spec["chart_name"], "col": 6},
			}
		)
	return json.dumps(blocks)


def _ensure_hub_workspace() -> bool:
	"""Create the top-level "Friday" hub workspace (idempotent).

	The hub is pure navigation — no number cards or charts of its own. It carries
	the shortcut tiles (``HUB_SHORTCUTS``) and the nav cards (``HUB_CARDS``), and
	sorts above "Projects" via a low ``sequence_id`` so it lands first in the
	sidebar. Returns True only when it actually creates the record.
	"""
	if frappe.db.exists("Workspace", HUB_WORKSPACE_NAME):
		return False
	ws = frappe.get_doc(
		{
			"doctype": "Workspace",
			"name": HUB_WORKSPACE_NAME,
			"label": HUB_WORKSPACE_NAME,
			"title": HUB_WORKSPACE_NAME,
			"module": MODULE,
			"type": "Workspace",
			"icon": "dashboard",
			"public": 1,
			"is_hidden": 0,
			# Negative so the hub sorts above every default workspace (min is 0).
			"sequence_id": -1,
			"content": _build_hub_content(),
		}
	)
	for spec in HUB_SHORTCUTS:
		ws.append("shortcuts", spec)
	# Each card is a "Card Break" row followed by its DocType "Link" rows. The
	# link label is the DocType name itself. We include only DocTypes that
	# actually exist on this site: a renamed/removed DocType would otherwise
	# raise LinkValidationError and — under failure-isolation — abort the whole
	# hub, leaving the operator with no navigation at all. Skip the missing one,
	# keep the rest. Cards left with no present links are dropped entirely.
	for card in HUB_CARDS:
		present = [dt for dt in card["links"] if frappe.db.exists("DocType", dt)]
		if not present:
			continue
		ws.append("links", {"type": "Card Break", "label": card["label"], "hidden": 0})
		for dt in present:
			ws.append(
				"links",
				{"type": "Link", "label": dt, "link_type": "DocType", "link_to": dt, "hidden": 0},
			)
	ws.insert(ignore_permissions=True)
	return True


def _build_hub_content() -> str:
	"""Build the hub Workspace ``content`` layout blob (a JSON-encoded string).

	Layout: a "Friday" header, the shortcut tiles in a row (col 3 each), a
	spacer, a "Navigate" header, then the nav cards (col 4 each = 3 per row).
	Blocks reference shortcuts by label and cards by their Card-Break label,
	which Frappe resolves at render time. Deterministic ids so re-runs are stable.
	"""
	blocks: list[dict] = [
		{"id": "fridayhub00", "type": "header", "data": {"text": "Friday", "col": 12}},
	]
	for i, spec in enumerate(HUB_SHORTCUTS):
		blocks.append(
			{
				"id": f"fridayhubsc{i}",
				"type": "shortcut",
				"data": {"shortcut_name": spec["label"], "col": 3},
			}
		)
	blocks.append({"id": "fridayhubsp0", "type": "spacer", "data": {"col": 12}})
	blocks.append({"id": "fridayhubh01", "type": "header", "data": {"text": "Navigate", "col": 12}})
	for i, card in enumerate(HUB_CARDS):
		blocks.append(
			{
				"id": f"fridayhubcd{i}",
				"type": "card",
				"data": {"card_name": card["label"], "col": 4},
			}
		)
	return json.dumps(blocks)
