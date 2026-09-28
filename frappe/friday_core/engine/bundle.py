# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Domain Bundle lookups for the engine (Design 75).

A Domain Bundle is the record that says "this work-item DocType is governed by
this Frappe Workflow" — plus the few field names the kernel needs to talk about
a work-item without knowing its domain (display name, project link, external
reference). The engine asks these questions here and nowhere else:

  - Is this DocType governed at all?          (active_bundle_for)
  - Which Workflow drives it?                 (workflow_for)
  - Which field holds the state?              (state_field_for)
  - Which fields name / link / reference it?  (fields_for)
  - Is this the pipeline's idle entry state?  (is_entry_state)
  - Is this a terminal state?                 (is_terminal_state)

These are thin, read-only helpers on purpose — the bundle is just data, and the
engine should never reach past it into domain specifics. ``active_bundle_for``
is cached because it now runs on EVERY document save (the engine subscribes to
``doc_events["*"]``); the Domain Bundle controller clears the cache on change.
"""

from __future__ import annotations

import frappe

_CACHE_KEY = "friday_bundle_for"

DEFAULT_FIELDS = {
	"display_name_field": "",
	"project_field": "project",
	"external_ref_field": "",
}


def active_bundle_for(doctype: str) -> str | None:
	"""Name of the single active Domain Bundle governing `doctype`, or None.

	The Domain Bundle controller enforces at-most-one-active per DocType, so this
	is unambiguous when it returns a value. Cached per doctype ("" = none).
	"""
	cached = frappe.cache().hget(_CACHE_KEY, doctype)
	if cached is None:
		cached = (
			frappe.db.get_value("Domain Bundle", {"domain_doctype": doctype, "is_active": 1}, "name") or ""
		)
		frappe.cache().hset(_CACHE_KEY, doctype, cached)
	return cached or None


def clear_cache() -> None:
	"""Forget every doctype → bundle mapping (called when a Domain Bundle changes)."""
	frappe.cache().delete_key(_CACHE_KEY)


def active_bundles() -> list[dict]:
	"""Every active bundle with the fields the kernel reads, for iteration
	(console tiles, exports). Ordered by name for stable output."""
	rows = frappe.get_all(
		"Domain Bundle",
		filters={"is_active": 1},
		fields=["name", "domain_doctype", "workflow_name", *DEFAULT_FIELDS.keys()],
		order_by="name asc",
	)
	return [_with_defaults(r) for r in rows]


def workflow_for(doctype: str) -> str | None:
	"""The Frappe Workflow name that drives `doctype`'s pipeline, or None if the
	DocType isn't governed (or its bundle has no workflow wired yet)."""
	name = active_bundle_for(doctype)
	if not name:
		return None
	return frappe.db.get_value("Domain Bundle", name, "workflow_name")


def state_field_for(workflow: str) -> str:
	"""The fieldname on the work-item that holds its workflow state. Frappe lets a
	Workflow name this field; it defaults to `workflow_state`."""
	return frappe.db.get_value("Workflow", workflow, "workflow_state_field") or "workflow_state"


def fields_for(doctype: str) -> dict:
	"""The bundle's field map for `doctype` (display_name_field, project_field,
	external_ref_field), with defaults applied. Defaults alone for an
	ungoverned doctype, so callers never branch on None."""
	name = active_bundle_for(doctype)
	if not name:
		return dict(DEFAULT_FIELDS)
	row = frappe.db.get_value("Domain Bundle", name, list(DEFAULT_FIELDS.keys()), as_dict=True) or {}
	return _with_defaults(row)


def display_name(doc) -> str:
	"""A human label for a work-item: its bundle's display field, else its name."""
	field = fields_for(doc.doctype).get("display_name_field")
	value = doc.get(field) if field else None
	return str(value or doc.name)


def is_entry_state(workflow: str, state: str) -> bool:
	"""True when `state` is the workflow's idle entry state — the first state
	in the Workflow's state table, where a work-item rests before a system
	transition (e.g. "Start Pipeline") kicks it off. The engine stays silent
	there: nothing waits on a human."""
	first = frappe.get_all(
		"Workflow Document State",
		filters={"parent": workflow, "parenttype": "Workflow"},
		fields=["state"],
		order_by="idx asc",
		limit_page_length=1,
		ignore_permissions=True,
	)
	return bool(first) and first[0].state == state


def is_terminal_state(workflow: str, state: str) -> bool:
	"""True when `state` has no outgoing transition (the pipeline is done)."""
	outgoing = frappe.get_all(
		"Workflow Transition",
		filters={"parent": workflow, "state": state},
		fields=["name"],
		limit_page_length=1,
		ignore_permissions=True,
	)
	return not outgoing


def terminal_states(workflow: str) -> set[str]:
	"""Every state of `workflow` with no outgoing transition."""
	states = {
		r.state
		for r in frappe.get_all(
			"Workflow Document State",
			filters={"parent": workflow, "parenttype": "Workflow"},
			fields=["state"],
			ignore_permissions=True,
		)
	}
	with_outgoing = {
		r.state
		for r in frappe.get_all(
			"Workflow Transition",
			filters={"parent": workflow, "parenttype": "Workflow"},
			fields=["state"],
			ignore_permissions=True,
		)
	}
	return states - with_outgoing


def _with_defaults(row: dict) -> dict:
	out = dict(row)
	for key, default in DEFAULT_FIELDS.items():
		if not out.get(key):
			out[key] = default
	return out
