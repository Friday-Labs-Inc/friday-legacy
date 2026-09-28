# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the Agent Role Contract (design 68).

Three roles — Orchestrator | Specialist | Worker — drive three things:

1. The system prompt: a short role preamble injected after the GOVERNANCE block.
2. The default approval threshold on first insert (when the field is blank).
3. For Orchestrators only: a default read-tool skill seed on first insert (when
   permitted_skills is empty).

These are pure unit tests with `frappe` mocked — same shape as
`test_agent_identity`. The DocType JSON change is verified by reading the file.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── DocType field test ──────────────────────────────────────────────────────


class TestAgentRoleField(unittest.TestCase):
	"""The agent_role Select field is present, defaults to Specialist."""

	def setUp(self):
		self.json_path = (
			Path(__file__).resolve().parents[1] / "doctype" / "agent_profile" / "agent_profile.json"
		)
		self.spec = json.loads(self.json_path.read_text())

	def _field(self, name):
		for f in self.spec["fields"]:
			if f["fieldname"] == name:
				return f
		return None

	def test_agent_role_field_exists(self):
		f = self._field("agent_role")
		self.assertIsNotNone(f, "agent_role field must exist on Agent Profile")
		self.assertEqual(f["fieldtype"], "Select")
		options = (f.get("options") or "").splitlines()
		self.assertEqual(options, ["Orchestrator", "Specialist", "Worker"])

	def test_agent_role_default_specialist(self):
		f = self._field("agent_role")
		self.assertEqual(f.get("default"), "Specialist")

	def test_agent_role_in_field_order(self):
		order = self.spec["field_order"]
		self.assertIn("agent_role", order)
		# Placed right after profile_name so it's the second thing operators see.
		self.assertEqual(order[order.index("profile_name") + 1], "agent_role")


# ── Prompt scaffolding ──────────────────────────────────────────────────────


class _FakeProfile:
	def __init__(self, agent_role="Specialist", system_prompt=""):
		self.agent_role = agent_role
		self.system_prompt = system_prompt


class TestRolePromptScaffolding(unittest.TestCase):
	"""The role preamble rides after the governance block, before the operator
	prompt — same chain pattern as design 66 governance."""

	def _build(self, role, operator_prompt=""):
		from frappe.friday_core.llm.prompt_builder import _build_system_prompt

		profile = _FakeProfile(agent_role=role, system_prompt=operator_prompt)
		return _build_system_prompt(profile)

	def test_orchestrator_preamble(self):
		text = self._build("Orchestrator")
		self.assertIn("ORCHESTRATOR ROLE", text)
		self.assertIn("plan and coordinate", text.lower())

	def test_specialist_preamble(self):
		text = self._build("Specialist")
		self.assertIn("SPECIALIST ROLE", text)
		self.assertIn("domain expert", text.lower())

	def test_worker_preamble(self):
		text = self._build("Worker")
		self.assertIn("WORKER ROLE", text)
		self.assertIn("one task precisely", text.lower())

	def test_preamble_rides_after_governance(self):
		text = self._build("Orchestrator")
		gov_idx = text.index("GOVERNANCE:")
		role_idx = text.index("ORCHESTRATOR ROLE")
		self.assertLess(gov_idx, role_idx, "role preamble must come after governance")

	def test_operator_prompt_rides_after_role(self):
		text = self._build("Worker", operator_prompt="You are the Bug Triage Bot.")
		role_idx = text.index("WORKER ROLE")
		op_idx = text.index("You are the Bug Triage Bot.")
		self.assertLess(role_idx, op_idx)

	def test_missing_role_falls_back_to_specialist(self):
		# Existing profiles (pre-migration) may have agent_role unset; treat as
		# Specialist so prompt assembly never crashes.
		profile = _FakeProfile(agent_role=None)
		from frappe.friday_core.llm.prompt_builder import _build_system_prompt

		text = _build_system_prompt(profile)
		self.assertIn("SPECIALIST ROLE", text)


# ── Default approval per role on first insert ───────────────────────────────


class _FakeDoc(dict):
	def __getattr__(self, k):
		try:
			return self[k]
		except KeyError as e:
			raise AttributeError(k) from e

	def __setattr__(self, k, v):
		self[k] = v

	def append(self, field, row):
		self.setdefault(field, []).append(row)
		return row


class TestRoleDefaults(unittest.TestCase):
	"""before_insert fills role-driven defaults only when fields are blank.
	Never overrides an operator-set value."""

	def _apply(self, doc):
		from frappe.friday_core.doctype.agent_profile.agent_profile import apply_role_defaults

		apply_role_defaults(doc)
		return doc

	def test_orchestrator_default_approval_high(self):
		doc = _FakeDoc(agent_role="Orchestrator", permitted_skills=[])
		self._apply(doc)
		self.assertEqual(doc.get("requires_approval_above_risk"), "high")

	def test_specialist_default_approval_medium(self):
		doc = _FakeDoc(agent_role="Specialist", permitted_skills=[])
		self._apply(doc)
		self.assertEqual(doc.get("requires_approval_above_risk"), "medium")

	def test_worker_default_approval_low(self):
		doc = _FakeDoc(agent_role="Worker", permitted_skills=[])
		self._apply(doc)
		self.assertEqual(doc.get("requires_approval_above_risk"), "low")

	def test_operator_set_approval_is_never_overridden(self):
		doc = _FakeDoc(
			agent_role="Worker",
			permitted_skills=[],
			requires_approval_above_risk="always",
		)
		self._apply(doc)
		self.assertEqual(doc.get("requires_approval_above_risk"), "always")

	def test_orchestrator_seeds_read_skills_when_empty(self):
		doc = _FakeDoc(agent_role="Orchestrator", permitted_skills=[])
		self._apply(doc)
		names = [row.get("skill") for row in doc.get("permitted_skills", [])]
		self.assertIn("read_record", names)
		self.assertIn("list_records", names)
		self.assertIn("list_project_files", names)

	def test_orchestrator_does_not_reseed_when_skills_already_present(self):
		existing = [{"skill": "custom_research"}]
		doc = _FakeDoc(agent_role="Orchestrator", permitted_skills=list(existing))
		self._apply(doc)
		# The seed is skipped because the operator already chose skills.
		self.assertEqual(doc.get("permitted_skills"), existing)

	def test_specialist_does_not_seed_skills(self):
		doc = _FakeDoc(agent_role="Specialist", permitted_skills=[])
		self._apply(doc)
		self.assertEqual(doc.get("permitted_skills"), [])

	def test_worker_does_not_seed_skills(self):
		doc = _FakeDoc(agent_role="Worker", permitted_skills=[])
		self._apply(doc)
		self.assertEqual(doc.get("permitted_skills"), [])

	def test_missing_role_treated_as_specialist(self):
		doc = _FakeDoc(agent_role=None, permitted_skills=[])
		self._apply(doc)
		self.assertEqual(doc.get("requires_approval_above_risk"), "medium")


# ── Backfill patch ──────────────────────────────────────────────────────────


class TestBackfillPatch(unittest.TestCase):
	"""The v0_2 patch sets agent_role=Specialist on every row where it's blank.
	Never touches rows that already have a role set."""

	def test_patch_backfills_only_blank_rows(self):
		from frappe.friday_core.patches.v1_0 import backfill_agent_role

		mock_frappe = MagicMock()
		mock_frappe.get_all.return_value = [
			{"name": "Copywriter"},
			{"name": "Researcher"},
		]
		with patch.object(backfill_agent_role, "frappe", mock_frappe):
			backfill_agent_role.execute()

		# get_all filtered for blank agent_role
		filters = mock_frappe.get_all.call_args.kwargs["filters"]
		self.assertIn("agent_role", filters)
		# set_value called once per blank row, always to "Specialist"
		calls = mock_frappe.db.set_value.call_args_list
		self.assertEqual(len(calls), 2)
		for call in calls:
			self.assertEqual(call.args[3], "Specialist")
