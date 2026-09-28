# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Post-migrate setup for scheduled agent runs (Design 87, Slice 1).

Ensures the `Friday Cron Manager` role exists. The `Cron Job` doctype grants this
role create/read/write so an operator can manage scheduled jobs without being a
full System Manager. Idempotent — safe on every migrate.
"""

from __future__ import annotations

import frappe

CRON_MANAGER_ROLE = "Friday Cron Manager"


def ensure_cron_role() -> None:
	"""Create the `Friday Cron Manager` role if a site does not have it yet."""
	if not frappe.db.exists("Role", CRON_MANAGER_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": CRON_MANAGER_ROLE, "desk_access": 1}).insert(
			ignore_permissions=True
		)
