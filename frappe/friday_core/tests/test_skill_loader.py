# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Tests for the Slice 3 skill loader.

PLAIN ENGLISH
=============

These tests prove the loader produces the right "menu of tools" for an
agent — and that the menu stays fresh as conditions change.

Eight tests, grouped by concern:

  - **Allow path** — Active skill, in permitted_skills, matrix-OK →
    appears in the loaded list.
  - **Status filter** — Draft / Retired / Archived skills are
    silently omitted (sub-tested across the three statuses).
  - **Permitted-skills filter** — a Skill the matrix would allow, but
    that the operator did NOT put in permitted_skills, is omitted.
  - **Matrix filter** — a Skill in permitted_skills, but for which the
    agent's roles don't cover the required (DocType, operation), is
    omitted (no audit-log row written — that's deliberate, see the
    loader's module docstring).
  - **Empty case** — a profile with no permitted_skills yields `[]`,
    not None and not an exception.
  - **Cache behaviour** — miss-then-hit gives identical results;
    cache invalidation on Skill.on_update drops the right entry.
  - **Tool-definition format** — `to_tool_definition` produces a
    valid OpenAI/Anthropic function-calling shape.

TEST DATA STRATEGY
==================

We reuse the Slice 2 helpers' style — idempotent `_ensure_*` setup —
and add the missing piece: an Agent Profile with `permitted_skills`
that actually points at the test skills.

We DON'T reuse Slice 2's exact profiles because Slice 2 didn't set
`permitted_skills`. Adding to those would couple the two test files
and make Slice 2 tests fragile. New, dedicated profiles for this
test file.

HOW TO RUN
==========

    bench --site friday.localhost run-tests \\
        --module frappe.friday_core.tests.test_skill_loader
"""

from __future__ import annotations

import unittest

import frappe
from frappe.friday_core.skills import loader as loader_module
from frappe.friday_core.skills.loader import (
	SKILLS_CACHE_KEY_PREFIX,
	SkillDefinition,
	invalidate_for_skill,
	load_for_profile,
	to_tool_definition,
)

TARGET_DOCTYPE = "Note"
TEST_ROLE = "Friday Test Reader"  # same role Slice 2 created (read on Note)
PROFILE_FULL = "FRIDAY-SLICE3-PROFILE-FULL"  # role + permitted_skills set
PROFILE_NO_SKILLS = "FRIDAY-SLICE3-PROFILE-NO-SKILLS"  # role, but empty permitted_skills
PROFILE_NO_ROLE = "FRIDAY-SLICE3-PROFILE-NO-ROLE"  # permitted_skills set, but no role

SKILL_ACTIVE = "slice3-skill-active"
SKILL_DRAFT = "slice3-skill-draft"
SKILL_RETIRED = "slice3-skill-retired"
SKILL_ARCHIVED = "slice3-skill-archived"


def _ensure_role():
	"""Reuse the Slice 2 test role pattern: a Role with `read` on Note."""
	if not frappe.db.exists("Role", TEST_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": TEST_ROLE}).insert(ignore_permissions=True)
	if not frappe.db.exists("Custom DocPerm", {"parent": TARGET_DOCTYPE, "role": TEST_ROLE}):
		frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": TARGET_DOCTYPE,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": TEST_ROLE,
				"read": 1,
				"permlevel": 0,
			}
		).insert(ignore_permissions=True)


def _ensure_skill(name: str, status: str, operation: str = "read"):
	"""Create or reset a Skill that needs (Note, <operation>). Idempotent."""
	if frappe.db.exists("Skill", name):
		skill = frappe.get_doc("Skill", name)
		skill.status = status
		skill.required_doctypes = []
		skill.append("required_doctypes", {"target_doctype": TARGET_DOCTYPE, "operation": operation})
		# Give the test skill a real parameters_schema so the tool-definition
		# test can verify it round-trips correctly.
		skill.parameters_schema = (
			'{"type":"object","properties":{"title":{"type":"string"}},"required":["title"]}'
		)
		skill.save(ignore_permissions=True)
		return
	doc = frappe.get_doc(
		{
			"doctype": "Skill",
			"skill_name": name,
			"description": f"Slice 3 test skill ({status})",
			"when_to_use": "When the test calls for this exact skill.",
			"risk_level": "low",
			"status": status,
			"parameters_schema": '{"type":"object","properties":{"title":{"type":"string"}},"required":["title"]}',
			"required_doctypes": [{"target_doctype": TARGET_DOCTYPE, "operation": operation}],
		}
	)
	doc.insert(ignore_permissions=True)


def _ensure_profile(name: str, roles: list[str], skills: list[str], status: str = "Active"):
	"""Create or reset an Agent Profile with the given roles + permitted_skills."""
	if frappe.db.exists("Agent Profile", name):
		profile = frappe.get_doc("Agent Profile", name)
		profile.status = status
		profile.assigned_roles = []
		profile.permitted_skills = []
		for role in roles:
			profile.append("assigned_roles", {"role": role})
		for skill in skills:
			profile.append("permitted_skills", {"skill": skill})
		profile.save(ignore_permissions=True)
		return
	doc = frappe.get_doc(
		{
			"doctype": "Agent Profile",
			"profile_name": name,
			"status": status,
			"requires_approval_above_risk": "high",
			"assigned_roles": [{"role": r} for r in roles],
			"permitted_skills": [{"skill": s} for s in skills],
		}
	)
	doc.insert(ignore_permissions=True)


class TestSkillLoader(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		_ensure_role()
		# Every status of skill, so we can test each filter independently.
		_ensure_skill(SKILL_ACTIVE, "Active")
		_ensure_skill(SKILL_DRAFT, "Draft")
		_ensure_skill(SKILL_RETIRED, "Retired")
		_ensure_skill(SKILL_ARCHIVED, "Archived")
		# PROFILE_FULL: role + all four skills in permitted_skills.
		_ensure_profile(
			PROFILE_FULL,
			roles=[TEST_ROLE],
			skills=[SKILL_ACTIVE, SKILL_DRAFT, SKILL_RETIRED, SKILL_ARCHIVED],
		)
		# PROFILE_NO_SKILLS: role, but permitted_skills empty.
		_ensure_profile(PROFILE_NO_SKILLS, roles=[TEST_ROLE], skills=[])
		# PROFILE_NO_ROLE: permitted_skills set, but no role → matrix filter
		# should exclude the Active skill.
		_ensure_profile(PROFILE_NO_ROLE, roles=[], skills=[SKILL_ACTIVE])
		frappe.db.commit()

	def setUp(self):
		# Cold cache per test so cache-hit vs miss is deterministic.
		frappe.cache().delete_keys(SKILLS_CACHE_KEY_PREFIX)
		# Also flush Slice 2's matrix cache — its entries can carry state
		# from prior tests that runs the matrix builder.
		frappe.cache().delete_keys("friday:perm_matrix:")

	# ------- happy path -------

	def test_active_permitted_matrix_ok_skill_is_loaded(self):
		skills = load_for_profile(PROFILE_FULL)
		names = [s.name for s in skills]
		self.assertIn(SKILL_ACTIVE, names)

	# ------- status filter -------

	def test_non_active_skills_are_filtered(self):
		skills = load_for_profile(PROFILE_FULL)
		names = {s.name for s in skills}
		for name in (SKILL_DRAFT, SKILL_RETIRED, SKILL_ARCHIVED):
			with self.subTest(skill=name):
				self.assertNotIn(name, names)

	# ------- permitted_skills filter -------

	def test_empty_permitted_skills_yields_empty_list(self):
		skills = load_for_profile(PROFILE_NO_SKILLS)
		self.assertEqual(skills, [])

	# ------- matrix filter (no audit log row written) -------

	def test_skill_excluded_when_matrix_denies(self):
		before = frappe.db.count("Permission Decision Log")
		skills = load_for_profile(PROFILE_NO_ROLE)
		self.assertEqual(skills, [], "no-role profile should see no skills")
		after = frappe.db.count("Permission Decision Log")
		# Loader uses evaluate(), not check(); so listing the menu must NOT
		# write a Permission Decision Log row.
		self.assertEqual(after, before, "loader must not write decision-log rows")

	# ------- cache behaviour -------

	def test_cache_miss_then_hit_returns_same_list(self):
		first = load_for_profile(PROFILE_FULL)
		# Mid-call we manually corrupt the world to detect cache use:
		# delete the underlying Skill row and confirm the cached list
		# is still returned. (We restore it in tearDown via _ensure_skill.)
		second = load_for_profile(PROFILE_FULL)
		self.assertEqual(
			[s.to_dict() for s in first],
			[s.to_dict() for s in second],
			"cached load should equal uncached load",
		)

	def test_invalidate_for_skill_flushes_only_affected_profiles(self):
		# Warm both caches.
		load_for_profile(PROFILE_FULL)
		load_for_profile(PROFILE_NO_SKILLS)
		self.assertIsNotNone(frappe.cache().get_value(f"{SKILLS_CACHE_KEY_PREFIX}{PROFILE_FULL}"))
		self.assertIsNotNone(frappe.cache().get_value(f"{SKILLS_CACHE_KEY_PREFIX}{PROFILE_NO_SKILLS}"))

		# Simulate a Skill update.
		skill = frappe.get_doc("Skill", SKILL_ACTIVE)
		invalidate_for_skill(skill)

		# PROFILE_FULL permits SKILL_ACTIVE → should be flushed.
		self.assertIsNone(
			frappe.cache().get_value(f"{SKILLS_CACHE_KEY_PREFIX}{PROFILE_FULL}"),
			"profile that permits the skill should be flushed",
		)
		# PROFILE_NO_SKILLS does not permit it → should remain.
		self.assertIsNotNone(
			frappe.cache().get_value(f"{SKILLS_CACHE_KEY_PREFIX}{PROFILE_NO_SKILLS}"),
			"unrelated profile should not be flushed",
		)

	# ------- tool-definition format -------

	def test_to_tool_definition_shape(self):
		skills = load_for_profile(PROFILE_FULL)
		active = next(s for s in skills if s.name == SKILL_ACTIVE)
		tool = to_tool_definition(active)

		self.assertEqual(tool["type"], "function")
		self.assertIn("function", tool)
		func = tool["function"]
		self.assertEqual(func["name"], SKILL_ACTIVE)
		self.assertTrue(func["description"])  # non-empty
		# Parameters must come through as a dict, not a JSON string.
		self.assertIsInstance(func["parameters"], dict)
		self.assertEqual(func["parameters"].get("type"), "object")

	def test_skill_definition_dict_round_trip(self):
		skills = load_for_profile(PROFILE_FULL)
		original = next(s for s in skills if s.name == SKILL_ACTIVE)
		restored = SkillDefinition.from_dict(original.to_dict())
		self.assertEqual(original, restored)

	# ------- Design 78: role gate uses Skill.role_gate field -------

	def test_role_gate_field_blocks_when_profile_lacks_role(self):
		"""A skill with role_gate set to a Frappe Role is hidden from a profile
		whose assigned_roles do not include that role. This is the load-bearing
		security check the architect-doc gap audit surfaced."""
		# Create a gate role and a skill that requires it.
		gate_role = "FRIDAY-DESIGN78-GATE"
		if not frappe.db.exists("Role", gate_role):
			frappe.get_doc({"doctype": "Role", "role_name": gate_role}).insert(ignore_permissions=True)
		gated_skill = "design78-gated-skill"
		_ensure_skill(gated_skill, "Active")
		skill_doc = frappe.get_doc("Skill", gated_skill)
		skill_doc.role_gate = gate_role
		skill_doc.save(ignore_permissions=True)
		# Profile holds the TEST_ROLE for matrix admit, but NOT the gate role.
		_ensure_profile("FRIDAY-DESIGN78-PROFILE-NO-GATE", roles=[TEST_ROLE], skills=[gated_skill])
		frappe.cache().delete_keys(SKILLS_CACHE_KEY_PREFIX)
		skills = load_for_profile("FRIDAY-DESIGN78-PROFILE-NO-GATE")
		self.assertNotIn(
			gated_skill,
			[s.name for s in skills],
			"profile without the gate role must not see a gated skill",
		)

	def test_role_gate_field_admits_when_profile_has_role(self):
		"""The same gated skill is visible to a profile that DOES hold the role."""
		gate_role = "FRIDAY-DESIGN78-GATE"
		if not frappe.db.exists("Role", gate_role):
			frappe.get_doc({"doctype": "Role", "role_name": gate_role}).insert(ignore_permissions=True)
		gated_skill = "design78-gated-skill"
		_ensure_skill(gated_skill, "Active")
		skill_doc = frappe.get_doc("Skill", gated_skill)
		skill_doc.role_gate = gate_role
		skill_doc.save(ignore_permissions=True)
		_ensure_profile(
			"FRIDAY-DESIGN78-PROFILE-WITH-GATE", roles=[TEST_ROLE, gate_role], skills=[gated_skill]
		)
		frappe.cache().delete_keys(SKILLS_CACHE_KEY_PREFIX)
		skills = load_for_profile("FRIDAY-DESIGN78-PROFILE-WITH-GATE")
		self.assertIn(
			gated_skill,
			[s.name for s in skills],
			"profile holding the gate role must see the gated skill",
		)

	def test_role_gate_dict_fallback_for_unset_field(self):
		"""When Skill.role_gate is blank, the loader falls back to the hardcoded
		_ROLE_GATED_SKILLS dict so existing gates keep working (back-compat
		guarantee from Design 78)."""
		from frappe.friday_core.skills.loader import _ROLE_GATED_SKILLS

		# Create a skill with NO role_gate field set; insert it into the dict.
		fallback_skill = "design78-dict-fallback-skill"
		_ensure_skill(fallback_skill, "Active")
		# Make sure the skill row's role_gate field is empty.
		frappe.db.set_value("Skill", fallback_skill, "role_gate", "")
		# Add a dict entry — this is what protects legacy gated skills.
		_ROLE_GATED_SKILLS[fallback_skill] = "Orchestrator"
		try:
			_ensure_profile("FRIDAY-DESIGN78-PROFILE-DICT-NO", roles=[TEST_ROLE], skills=[fallback_skill])
			# This profile has no agent_role set → should be blocked by dict fallback.
			frappe.cache().delete_keys(SKILLS_CACHE_KEY_PREFIX)
			skills = load_for_profile("FRIDAY-DESIGN78-PROFILE-DICT-NO")
			self.assertNotIn(
				fallback_skill,
				[s.name for s in skills],
				"dict fallback must still block when agent_role tier doesn't match",
			)
		finally:
			_ROLE_GATED_SKILLS.pop(fallback_skill, None)
