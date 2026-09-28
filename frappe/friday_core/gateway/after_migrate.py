# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Post-migrate setup for the gateway command surface (Design 82).

Ensures the `Friday Operator` role exists. Operator-tier slash commands
(`/approve`, `/deny`) are gated on this role (Design 82, Q4): until it exists
and is granted to a human, nobody can approve from chat. Creating it here means
a fresh bench clone has the role the moment it migrates — an operator just needs
to be granted it (Desk → User → Roles, or `bootstrap_raven` enrolment).

Idempotent — safe to call on every migrate.
"""

from __future__ import annotations

import frappe

# The Frappe role that may run operator-tier gateway commands. Must match
# `gateway/commands._OPERATOR_ROLE`.
OPERATOR_ROLE = "Friday Operator"


def ensure_command_roles() -> None:
	"""Create the `Friday Operator` role if a site does not already have it."""
	if not frappe.db.exists("Role", OPERATOR_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": OPERATOR_ROLE, "desk_access": 1}).insert(
			ignore_permissions=True
		)
