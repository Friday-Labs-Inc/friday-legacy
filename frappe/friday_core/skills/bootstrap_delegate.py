# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
One-command provisioning for the delegate-task skill (design 69a).

Same pattern as `bootstrap_brand`: an "Agent Delegator" role with exactly the
permissions delegation needs (create/read/write on Task — the handler creates
the row, then the pipeline runs it), the Skill row with its model-facing
schema, and the profile wiring. Idempotent.

    bench --site friday.localhost execute \\
        frappe.friday_core.skills.bootstrap_delegate.provision
"""

from __future__ import annotations

import json

import frappe

DELEGATOR_ROLE = "Agent Delegator"

_SKILL = {
	"skill_name": "delegate-task",
	"description": (
		"Delegate a subtask to another agent profile. Creates a durable Task "
		"record that the pipeline dispatches independently — the child runs "
		"asynchronously and reports back when done. Use for work that benefits "
		"from a specialist profile or a separate context."
	),
	"when_to_use": (
		"Use when a piece of work benefits from a specialist profile or a "
		"clean separate context (research, drafting, a multi-step subtask). "
		"Give complete, self-contained instructions — the specialist cannot "
		"see this conversation. Run multiple delegations in parallel when "
		"they do not depend on each other. Do NOT delegate trivial steps "
		"you can do directly."
	),
	"parameters_schema": {
		"type": "object",
		"properties": {
			"agent_profile": {
				"type": "string",
				"description": "The target Agent Profile name to delegate to (must exist and be Active).",
			},
			"instruction": {
				"type": "string",
				"description": "Complete, self-contained instructions for the target agent — include all context it needs; it cannot see this conversation.",
			},
			"title": {
				"type": "string",
				"description": "Short title for the delegated task. Defaults to the first 80 chars of instruction.",
			},
			"project": {
				"type": "string",
				"description": "Override the project for the child task. Defaults to inheriting the parent's project.",
			},
			"priority": {
				"type": "string",
				"enum": ["low", "normal", "high", "urgent"],
				"description": "Priority for the child task. Defaults to normal.",
			},
		},
		"required": ["agent_profile", "instruction"],
	},
	"required_doctypes": [{"target_doctype": "Task", "operation": "create"}],
}

_ROLE_PERMS = {"Task": {"create": 1, "read": 1, "write": 1}}


def ensure_definitions() -> None:
	"""Role + perms + the Skill row only (no profile wiring) — the
	after_migrate path (bootstrap_registry), so definition edits reach
	existing sites."""
	if not frappe.db.exists("Role", DELEGATOR_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": DELEGATOR_ROLE}).insert(ignore_permissions=True)

	for target_doctype, ptypes in _ROLE_PERMS.items():
		if not frappe.db.exists("Custom DocPerm", {"parent": target_doctype, "role": DELEGATOR_ROLE}):
			frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": target_doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": DELEGATOR_ROLE,
					"permlevel": 0,
					**ptypes,
				}
			).insert(ignore_permissions=True)

	skill_name = _SKILL["skill_name"]
	if frappe.db.exists("Skill", skill_name):
		skill = frappe.get_doc("Skill", skill_name)
	else:
		skill = frappe.new_doc("Skill")
		skill.skill_name = skill_name
	skill.description = _SKILL["description"]
	skill.when_to_use = _SKILL["when_to_use"]
	skill.parameters_schema = json.dumps(_SKILL["parameters_schema"])
	skill.risk_level = "medium"
	skill.requires_approval = 0
	skill.status = "Active"
	# Design 78 — populate the Skill.role_gate field so the loader's primary
	# (DB-driven) gate path takes over from the hardcoded _ROLE_GATED_SKILLS
	# fallback. DELEGATOR_ROLE is a Frappe Role; the loader walks the
	# profile's assigned_roles for non-tier role names.
	skill.role_gate = DELEGATOR_ROLE
	skill.required_doctypes = []
	for req in _SKILL["required_doctypes"]:
		skill.append("required_doctypes", req)
	skill.save(ignore_permissions=True)


def provision(profile_name: str = "Friday") -> dict:
	"""Provision role, perms, the Skill row, and profile wiring. Idempotent."""
	ensure_definitions()
	skill_name = _SKILL["skill_name"]

	if not frappe.db.exists("Agent Profile", profile_name):
		frappe.throw(
			frappe._("Agent Profile {0} not found — run `bench friday setup` first.").format(profile_name)
		)
	profile = frappe.get_doc("Agent Profile", profile_name)
	if DELEGATOR_ROLE not in {row.role for row in (profile.assigned_roles or [])}:
		profile.append("assigned_roles", {"role": DELEGATOR_ROLE})
	if skill_name not in {row.skill for row in (profile.permitted_skills or [])}:
		profile.append("permitted_skills", {"skill": skill_name})
	profile.save(ignore_permissions=True)

	from frappe.friday_core.skills.loader import invalidate_for_profile

	invalidate_for_profile(profile_name)
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit

	summary = {"role": DELEGATOR_ROLE, "skill": skill_name, "profile": profile_name}
	print(f"✓ Delegation provisioned: {summary}")
	return summary
