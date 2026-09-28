# Copyright (c) 2026, Friday Labs and contributors
"""`on_actor_write` hook (Design 99): record every row an agent writes.

The framework calls this from inside another document's write path, so this
must be the cheapest possible write: a parameterised INSERT, not
``frappe.get_doc(...).insert()``. A nested Document insert would run a second
full save cycle — validation, Link and Dynamic Link checks, post-save methods,
realtime notification — *inside* the outer document's save, which is how the
first version of this hook stalled the whole agent turn (gateway pipeline jobs
died with DoesNotExistError and callers waited out their 180s timeout).

Only AGENT actors are recorded: a human's identity is already `modified_by`.
Never raises — the audit trail must not be able to break the write it records.
"""

import frappe

SKIP_DOCTYPES = frozenset(
	{
		"Agent Write Log",
		"Execution Log",
		"Permission Decision Log",
		"LLM Usage Log",
		"Dispatcher Event",
		"Turn Event",
		"Task Completion Summary",
		"Version",
		"Error Log",
		"Comment",
		"Activity Log",
		"Access Log",
		"Deleted Document",
		"Notification Log",
		"Scheduled Job Log",
		"Route History",
		"Energy Point Log",
	}
)


def on_actor_write(doc, action: str) -> None:
	actor = frappe.get_actor()
	if actor.get("kind") != "agent" or doc.doctype in SKIP_DOCTYPES:
		return
	try:
		frappe.db.savepoint("friday_actor_write")
		now = frappe.utils.now()
		frappe.db.sql(
			"""insert into `tabAgent Write Log`
				(name, creation, modified, owner, modified_by, docstatus, idx,
				 ref_doctype, ref_name, action, actor, actor_kind, `user`, trace_id)
			values (%(name)s, %(now)s, %(now)s, %(user)s, %(user)s, 0, 0,
				 %(ref_doctype)s, %(ref_name)s, %(action)s, %(actor)s, %(kind)s, %(user)s, %(trace)s)""",
			{
				"name": frappe.generate_hash(length=10),
				"now": now,
				"user": frappe.session.user,
				"ref_doctype": doc.doctype,
				"ref_name": doc.name,
				"action": action,
				"actor": actor.get("id"),
				"kind": actor.get("kind"),
				"trace": actor.get("trace_id"),
			},
		)
	except Exception:
		try:
			frappe.db.rollback(save_point="friday_actor_write")
		except Exception:
			pass
		frappe.logger("friday.audit").warning("agent write log failed", exc_info=True)
