# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
The skill-definition registry — keep every deployed skill in lockstep with code.

PLAIN ENGLISH
=============
Every skill bootstrap defines what a tool IS (its description, parameter
schema, risk level, roles and permissions) in code. Three separate incidents
proved the same defect class: those definitions were only written to the site
when an operator manually ran a CLI command, so any later edit to the code
silently never reached existing sites — the deployed tool drifted from what
the repo said it was:

  1. PR #179 — the file skills sat status="Draft" forever (the loader
     hard-excludes Draft), so the CD agent's toolset never contained them.
  2. Finding #18 / PR #190 — remember was allow-listed but its permission
     role never granted; the matrix silently dropped the tool.
  3. Finding #19 / PR #192 — remember's schema gained a `scope` param in
     code that never appeared on prod, because nothing on the migrate path
     refreshed the row.

This module is the class-killer: ONE after_migrate entry that runs every
bootstrap's `ensure_definitions()` (role + perms + Skill rows — definitions
only, never profile wiring; granting a skill to an agent stays an explicit
operator/domain decision). Failure-isolated per bootstrap, so one broken
ensure can never abort a migrate or starve the others.

A reflection test (test_bootstrap_definitions.py) asserts every
``skills/bootstrap_*.py`` module is covered — listed here, contributed by an
app through the ``friday_skill_definitions`` hook, or wired into after_migrate
individually — so the NEXT bootstrap cannot be forgotten.
"""

from __future__ import annotations

import frappe

# Bootstraps whose definitions this registry refreshes on every migrate.
# Dotted paths (resolved via frappe.get_attr) so a broken import in one
# module cannot poison the whole registry at import time.
DEFINITION_ENSURES: tuple[str, ...] = (
	"frappe.friday_core.skills.bootstrap_files.ensure_definitions",
	"frappe.friday_core.skills.bootstrap_read.ensure_definitions",
	"frappe.friday_core.skills.bootstrap_propose_skill.ensure_definitions",
	"frappe.friday_core.skills.bootstrap_delegate.ensure_definitions",
	"frappe.friday_core.skills.bootstrap_deliverables.ensure_definitions",
)

# A domain app adds its own bootstraps here, in its hooks.py:
#     friday_skill_definitions = ["my_app.skills.bootstrap_x.ensure_definitions"]
HOOK = "friday_skill_definitions"


def definition_ensures() -> tuple[str, ...]:
	"""The kernel's bootstraps plus every installed app's."""
	return tuple(DEFINITION_ENSURES) + tuple(frappe.get_hooks(HOOK) or [])


# Bootstraps already on the migrate path through their OWN after_migrate
# entry (kept individual for ordering or gating reasons). The reflection
# test accepts either home; a new bootstrap must land in one of them.
INDIVIDUALLY_WIRED: frozenset[str] = frozenset(
	{
		"bootstrap_memory",  # ensure_memory_provisioned (PR #192)
		"bootstrap_cron",  # provision() directly in after_migrate
		"bootstrap_session_search",  # provision() directly in after_migrate
		"bootstrap_project",  # provision_if_ready() gates on setup state
	}
)


def ensure_all_skill_definitions() -> None:
	"""after_migrate entry: refresh every registered bootstrap's definitions.

	Failure-isolated per bootstrap — a single broken ensure is logged loudly
	and the rest still run; the migrate itself is never aborted.
	"""
	for path in definition_ensures():
		try:
			frappe.get_attr(path)()
		except Exception:
			frappe.log_error(title=f"bootstrap_registry: {path} failed")
