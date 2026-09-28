# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
One-command provisioning for the project command-loop skills (design 60, Q7).

A "Project Commander" role with exactly what steering needs (create/read/
write on Project and Task), four Skill rows, and the profile wiring.
Idempotent.

    bench --site friday.localhost execute \\
        frappe.friday_core.skills.bootstrap_project.provision
"""

from __future__ import annotations

import json

import frappe

COMMANDER_ROLE = "Project Commander"

_SKILLS: dict[str, dict] = {
	"project-status": {
		"description": "Report a Project's pipeline at a glance: every task, its state, mode, and what is blocking.",
		"when_to_use": "When asked where a project stands ('status of PRJ x', 'where is the Loop Coffee pipeline').",
		"schema": {
			"type": "object",
			"properties": {"project": {"type": "string", "description": "The Friday Project name."}},
			"required": ["project"],
		},
		"docs": [{"target_doctype": "Project", "operation": "read"}],
	},
	"list-projects": {
		"description": "List ALL Friday Projects at a glance — name, status, %complete, task counts. Answers 'what projects exist / are there any (new) projects / list projects'.",
		"when_to_use": "Use this whenever asked to LIST projects, show all projects, or 'are there any (new) projects' — i.e. when NO specific project name is given. For ONE named project's detail use project-status instead. Optional 'status' filter.",
		"schema": {
			"type": "object",
			"properties": {
				"status": {
					"type": "string",
					"description": "Optional status filter (e.g. Open, On Hold, Completed).",
				}
			},
			"required": [],
		},
		"docs": [{"target_doctype": "Project", "operation": "read"}],
	},
	"update-task": {
		"description": "Complete or cancel ONE pipeline task on a human's instruction (completing a gate milestone unblocks its dependents).",
		"when_to_use": "Only when a human explicitly asks to mark a task done or cancel it. Never to skip work you should do.",
		"schema": {
			"type": "object",
			"properties": {
				"task": {"type": "string", "description": "The Task ID."},
				"action": {"type": "string", "enum": ["complete", "cancel"], "description": "What to do."},
			},
			"required": ["task", "action"],
		},
		"docs": [{"target_doctype": "Task", "operation": "write"}],
	},
	"pause-project": {
		"description": "Pause (or resume) a whole pipeline — no further tasks dispatch while a project is On Hold.",
		"when_to_use": "Only on explicit human instruction to pause or resume a project.",
		"schema": {
			"type": "object",
			"properties": {
				"project": {"type": "string", "description": "The Friday Project name."},
				"resume": {"type": "boolean", "description": "true to resume instead of pause."},
			},
			"required": ["project"],
		},
		"docs": [{"target_doctype": "Project", "operation": "write"}],
	},
}

_ROLE_PERMS = {
	"Project": {"create": 1, "read": 1, "write": 1},
	"Task": {"create": 1, "read": 1, "write": 1},
}


def provision(profile_name: str = "Friday") -> dict:
	"""Provision role, perms, the four Skill rows, and profile wiring. Idempotent."""
	if not frappe.db.exists("Role", COMMANDER_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": COMMANDER_ROLE}).insert(ignore_permissions=True)

	for target_doctype, ptypes in _ROLE_PERMS.items():
		if not frappe.db.exists("Custom DocPerm", {"parent": target_doctype, "role": COMMANDER_ROLE}):
			frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": target_doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": COMMANDER_ROLE,
					"permlevel": 0,
					**ptypes,
				}
			).insert(ignore_permissions=True)

	for skill_name, spec in _SKILLS.items():
		if frappe.db.exists("Skill", skill_name):
			skill = frappe.get_doc("Skill", skill_name)
		else:
			skill = frappe.new_doc("Skill")
			skill.skill_name = skill_name
		skill.description = spec["description"]
		skill.when_to_use = spec["when_to_use"]
		skill.parameters_schema = json.dumps(spec["schema"])
		skill.risk_level = "low"
		skill.requires_approval = 0
		skill.status = "Active"
		skill.required_doctypes = []
		for req in spec["docs"]:
			skill.append("required_doctypes", req)
		skill.save(ignore_permissions=True)

	if not frappe.db.exists("Agent Profile", profile_name):
		frappe.throw(
			frappe._("Agent Profile {0} not found — run `bench friday setup` first.").format(profile_name)
		)
	profile = frappe.get_doc("Agent Profile", profile_name)
	if COMMANDER_ROLE not in {row.role for row in (profile.assigned_roles or [])}:
		profile.append("assigned_roles", {"role": COMMANDER_ROLE})
	existing_skills = {row.skill for row in (profile.permitted_skills or [])}
	for skill_name in _SKILLS:
		if skill_name not in existing_skills:
			profile.append("permitted_skills", {"skill": skill_name})
	profile.save(ignore_permissions=True)

	from frappe.friday_core.skills.loader import invalidate_for_profile

	invalidate_for_profile(profile_name)
	# Manual commit is required: provision() runs via `bench execute` (no
	# request lifecycle to commit for us) and must persist before returning.
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit

	summary = {"role": COMMANDER_ROLE, "skills": sorted(_SKILLS.keys()), "profile": profile_name}
	print(f"✓ Project command loop provisioned: {summary}")
	return summary


def provision_if_ready() -> None:
	"""after_migrate-safe wrapper: provision only when the default Agent Profile
	exists, so a fresh site (pre-`bench friday setup`) doesn't break migrate on the
	`provision()` throw. Idempotent; this is how the project command-loop skills
	(incl. list-projects) reliably reach every site without a manual bench-execute.
	"""
	if frappe.db.exists("Agent Profile", "Friday"):
		provision()
