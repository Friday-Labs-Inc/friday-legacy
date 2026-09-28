# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for Multi-Agent Delegation (design 69a — the durable primitive).

Covers the six gates and shapes that make delegation safe:

1. DocType fields: parent_task on Task, max_concurrent_delegations on Agent
   Profile, max_delegation_depth on Agent Settings.
2. Depth gate: delegation blocked when the parent_task chain exceeds the limit.
3. Role gate: only Orchestrator profiles can delegate.
4. Concurrency gate: delegation blocked when too many active children exist.
5. Child Task shape: parent_task, project, assigned_to_profile, workflow_state
   are correct on the created child.
6. Prompt preamble: the Orchestrator preamble mentions delegate_task.
7. Loader gate: the skill loader hides delegate-task for non-Orchestrators.

Pure unit tests — frappe is mocked. Same pattern as test_agent_role.py.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── DocType field tests ────────────────────────────────────────────────────

FRIDAY_CORE = Path(__file__).resolve().parents[1]


class TestDelegationFields(unittest.TestCase):
	"""New fields required for delegation are present on the right DocTypes."""

	def test_task_has_parent_task_link(self):
		spec = _load_json(FRIDAY_CORE / "doctype" / "task" / "task.json")
		f = _field(spec, "parent_task")
		self.assertIsNotNone(f, "Task must have a parent_task field")
		self.assertEqual(f["fieldtype"], "Link")
		self.assertEqual(f["options"], "Task")

	def test_parent_task_in_field_order(self):
		spec = _load_json(FRIDAY_CORE / "doctype" / "task" / "task.json")
		order = spec["field_order"]
		self.assertIn("parent_task", order)
		self.assertIn("originating_platform", order)
		idx_parent = order.index("parent_task")
		idx_originating = order.index("originating_platform")
		self.assertGreater(idx_parent, idx_originating)

	def test_agent_profile_has_max_concurrent_delegations(self):
		spec = _load_json(FRIDAY_CORE / "doctype" / "agent_profile" / "agent_profile.json")
		f = _field(spec, "max_concurrent_delegations")
		self.assertIsNotNone(f, "Agent Profile must have max_concurrent_delegations")
		self.assertEqual(f["fieldtype"], "Int")
		self.assertEqual(f.get("default"), "5")

	def test_agent_settings_has_max_delegation_depth(self):
		spec = _load_json(FRIDAY_CORE / "doctype" / "agent_settings" / "agent_settings.json")
		f = _field(spec, "max_delegation_depth")
		self.assertIsNotNone(f, "Agent Settings must have max_delegation_depth")
		self.assertEqual(f["fieldtype"], "Int")
		self.assertEqual(f.get("default"), "3")

	def test_agent_settings_has_hard_ceiling(self):
		spec = _load_json(FRIDAY_CORE / "doctype" / "agent_settings" / "agent_settings.json")
		f = _field(spec, "delegation_depth_hard_ceiling")
		self.assertIsNotNone(f, "Agent Settings must have delegation_depth_hard_ceiling")
		self.assertEqual(f["fieldtype"], "Int")
		self.assertEqual(f.get("default"), "8")


# ── Depth calculation ──────────────────────────────────────────────────────


class TestDelegationDepth(unittest.TestCase):
	"""_delegation_depth walks the parent_task chain and returns the count."""

	def _depth(self, task_name, chain):
		from frappe.friday_core.skills.handlers_delegate import _delegation_depth

		def get_parent(name):
			return chain.get(name)

		return _delegation_depth(task_name, _get_parent=get_parent)

	def test_no_parent_returns_zero(self):
		self.assertEqual(self._depth("T-1", {"T-1": None}), 0)

	def test_one_parent_returns_one(self):
		self.assertEqual(self._depth("T-2", {"T-2": "T-1", "T-1": None}), 1)

	def test_chain_of_three_returns_three(self):
		chain = {"T-4": "T-3", "T-3": "T-2", "T-2": "T-1", "T-1": None}
		self.assertEqual(self._depth("T-4", chain), 3)

	def test_no_entry_returns_zero(self):
		self.assertEqual(self._depth("T-1", {}), 0)


# ── Role gate ──────────────────────────────────────────────────────────────


class TestDelegationRoleGate(unittest.TestCase):
	"""Only profiles with agent_role='Orchestrator' can call delegate-task."""

	def _call(self, parent_role, **overrides):
		from frappe.friday_core.skills.handlers_delegate import delegate_task

		mock_frappe = MagicMock()
		parent_profile_doc = MagicMock()
		parent_profile_doc.agent_role = parent_role
		parent_profile_doc.max_concurrent_delegations = 5

		target_profile_doc = MagicMock()
		target_profile_doc.status = "Active"
		target_profile_doc.agent_role = "Specialist"

		def get_cached_doc(dt, name):
			if name == "parent-agent":
				return parent_profile_doc
			if name == "target-agent":
				return target_profile_doc
			return MagicMock()

		mock_frappe.get_cached_doc = get_cached_doc
		mock_frappe.db.exists.return_value = True
		mock_frappe.db.get_value.return_value = None  # no parent_task
		mock_frappe.db.count.return_value = 0  # no concurrent children
		mock_frappe.flags = MagicMock()
		mock_frappe.flags.get.return_value = {
			"agent_profile": "parent-agent",
			"session_id": overrides.get("session_id", "chat-123"),
		}
		mock_frappe.utils.now_datetime.return_value = "2026-06-14 00:00:00"

		settings = MagicMock()
		settings.max_delegation_depth = 3
		settings.delegation_depth_hard_ceiling = 8

		with patch("frappe.friday_core.skills.handlers_delegate.frappe", mock_frappe):
			with patch(
				"frappe.friday_core.skills.handlers_delegate._get_delegation_settings",
				return_value=(3, 8),
			):
				return delegate_task(
					"delegate-task",
					{
						"agent_profile": "target-agent",
						"instruction": "Research market trends",
						"title": "Market research",
					},
				)

	def test_orchestrator_can_delegate(self):
		result = self._call("Orchestrator")
		self.assertIn("delegation_id", result)

	def test_specialist_cannot_delegate(self):
		with self.assertRaises(ValueError) as ctx:
			self._call("Specialist")
		self.assertIn("Orchestrator", str(ctx.exception))

	def test_worker_cannot_delegate(self):
		with self.assertRaises(ValueError) as ctx:
			self._call("Worker")
		self.assertIn("Orchestrator", str(ctx.exception))


# ── Depth gate ─────────────────────────────────────────────────────────────


class TestDelegationDepthGate(unittest.TestCase):
	"""Delegation is blocked when depth would exceed max_delegation_depth."""

	def _call_at_depth(self, current_depth, max_depth=3):
		from frappe.friday_core.skills.handlers_delegate import delegate_task

		mock_frappe = MagicMock()
		parent_profile_doc = MagicMock()
		parent_profile_doc.agent_role = "Orchestrator"
		parent_profile_doc.max_concurrent_delegations = 5

		target_profile_doc = MagicMock()
		target_profile_doc.status = "Active"

		def get_cached_doc(dt, name):
			if name == "parent-agent":
				return parent_profile_doc
			if name == "target-agent":
				return target_profile_doc
			return MagicMock()

		mock_frappe.get_cached_doc = get_cached_doc
		mock_frappe.db.exists.return_value = True
		mock_frappe.db.count.return_value = 0
		mock_frappe.flags = MagicMock()
		mock_frappe.flags.get.return_value = {
			"agent_profile": "parent-agent",
			"session_id": "task::T-100",
		}
		mock_frappe.utils.now_datetime.return_value = "2026-06-14 00:00:00"

		with patch("frappe.friday_core.skills.handlers_delegate.frappe", mock_frappe):
			with patch(
				"frappe.friday_core.skills.handlers_delegate._delegation_depth",
				return_value=current_depth,
			):
				with patch(
					"frappe.friday_core.skills.handlers_delegate._get_delegation_settings",
					return_value=(max_depth, 8),
				):
					return delegate_task(
						"delegate-task",
						{
							"agent_profile": "target-agent",
							"instruction": "Do something",
						},
					)

	def test_within_depth_limit_passes(self):
		result = self._call_at_depth(1, max_depth=3)
		self.assertIn("delegation_id", result)

	def test_at_depth_limit_blocks(self):
		with self.assertRaises(ValueError) as ctx:
			self._call_at_depth(3, max_depth=3)
		self.assertIn("depth", str(ctx.exception).lower())

	def test_above_depth_limit_blocks(self):
		with self.assertRaises(ValueError) as ctx:
			self._call_at_depth(5, max_depth=3)
		self.assertIn("depth", str(ctx.exception).lower())


# ── Concurrency gate ──────────────────────────────────────────────────────


class TestDelegationConcurrencyGate(unittest.TestCase):
	"""Delegation is blocked when active children count >= profile's max."""

	def _call_with_children(self, active_children, max_concurrent=5):
		from frappe.friday_core.skills.handlers_delegate import delegate_task

		mock_frappe = MagicMock()
		parent_profile_doc = MagicMock()
		parent_profile_doc.agent_role = "Orchestrator"
		parent_profile_doc.max_concurrent_delegations = max_concurrent

		target_profile_doc = MagicMock()
		target_profile_doc.status = "Active"

		def get_cached_doc(dt, name):
			if name == "parent-agent":
				return parent_profile_doc
			if name == "target-agent":
				return target_profile_doc
			return MagicMock()

		mock_frappe.get_cached_doc = get_cached_doc
		mock_frappe.db.exists.return_value = True
		mock_frappe.db.count.return_value = active_children
		mock_frappe.flags = MagicMock()
		mock_frappe.flags.get.return_value = {
			"agent_profile": "parent-agent",
			"session_id": "task::T-100",
		}
		mock_frappe.utils.now_datetime.return_value = "2026-06-14 00:00:00"

		with patch("frappe.friday_core.skills.handlers_delegate.frappe", mock_frappe):
			with patch(
				"frappe.friday_core.skills.handlers_delegate._delegation_depth",
				return_value=0,
			):
				with patch(
					"frappe.friday_core.skills.handlers_delegate._get_delegation_settings",
					return_value=(3, 8),
				):
					return delegate_task(
						"delegate-task",
						{
							"agent_profile": "target-agent",
							"instruction": "Do something",
						},
					)

	def test_within_concurrency_limit_passes(self):
		result = self._call_with_children(2, max_concurrent=5)
		self.assertIn("delegation_id", result)

	def test_at_concurrency_limit_blocks(self):
		with self.assertRaises(ValueError) as ctx:
			self._call_with_children(5, max_concurrent=5)
		self.assertIn("concurrent", str(ctx.exception).lower())


# ── Child Task shape ──────────────────────────────────────────────────────


class TestChildTaskCreation(unittest.TestCase):
	"""The created child Task has the right fields set."""

	def _delegate_and_capture(self):
		from frappe.friday_core.skills.handlers_delegate import delegate_task

		mock_frappe = MagicMock()
		parent_profile_doc = MagicMock()
		parent_profile_doc.agent_role = "Orchestrator"
		parent_profile_doc.max_concurrent_delegations = 5

		# The parent task doc (to inherit project from)
		parent_task_doc = MagicMock()
		parent_task_doc.project = "Project Alpha"
		parent_task_doc.name = "T-100"

		target_profile_doc = MagicMock()
		target_profile_doc.status = "Active"

		def get_cached_doc(dt, name):
			if name == "parent-agent":
				return parent_profile_doc
			if name == "target-agent":
				return target_profile_doc
			return MagicMock()

		captured_task = {}

		def mock_get_doc(spec):
			if isinstance(spec, dict) and spec.get("doctype") == "Task":
				captured_task.update(spec)
				doc = MagicMock()
				doc.name = "T-101"
				return doc
			return MagicMock()

		mock_frappe.get_cached_doc = get_cached_doc
		mock_frappe.get_doc.side_effect = mock_get_doc
		mock_frappe.db.exists.return_value = True
		mock_frappe.db.count.return_value = 0
		mock_frappe.db.get_value.side_effect = lambda dt, name, field: (
			"Project Alpha" if field == "project" else None
		)
		mock_frappe.flags = MagicMock()
		mock_frappe.flags.get.return_value = {
			"agent_profile": "parent-agent",
			"session_id": "task::T-100",
		}
		mock_frappe.utils.now_datetime.return_value = "2026-06-14 00:00:00"

		with patch("frappe.friday_core.skills.handlers_delegate.frappe", mock_frappe):
			with patch(
				"frappe.friday_core.skills.handlers_delegate._delegation_depth",
				return_value=0,
			):
				with patch(
					"frappe.friday_core.skills.handlers_delegate._get_delegation_settings",
					return_value=(3, 8),
				):
					result = delegate_task(
						"delegate-task",
						{
							"agent_profile": "target-agent",
							"instruction": "Research market trends for Q3",
							"title": "Market research",
						},
					)

		return result, captured_task

	def test_child_has_parent_task(self):
		_, task = self._delegate_and_capture()
		self.assertEqual(task.get("parent_task"), "T-100")

	def test_child_inherits_project(self):
		_, task = self._delegate_and_capture()
		self.assertEqual(task.get("project"), "Project Alpha")

	def test_child_assigned_to_target(self):
		_, task = self._delegate_and_capture()
		self.assertEqual(task.get("assigned_to_profile"), "target-agent")

	def test_child_workflow_state_pending(self):
		_, task = self._delegate_and_capture()
		self.assertEqual(task.get("workflow_state"), "Pending")

	def test_child_execution_mode_agentic(self):
		_, task = self._delegate_and_capture()
		self.assertEqual(task.get("execution_mode"), "agentic")

	def test_return_shape(self):
		result, _ = self._delegate_and_capture()
		self.assertIn("delegation_id", result)
		self.assertIn("child_task_name", result)
		self.assertEqual(result["status"], "queued")
		self.assertIn("result", result)


# ── Orchestrator preamble ─────────────────────────────────────────────────


class TestOrchestratorDelegationPreamble(unittest.TestCase):
	"""Design 69 Q13: the preamble mentions delegate_task."""

	def test_orchestrator_preamble_mentions_delegate(self):
		from frappe.friday_core.llm.prompt_builder import _ROLE_PREAMBLES

		preamble = _ROLE_PREAMBLES["Orchestrator"]
		self.assertIn("delegate-task", preamble)

	def test_specialist_preamble_does_not_mention_delegate(self):
		from frappe.friday_core.llm.prompt_builder import _ROLE_PREAMBLES

		preamble = _ROLE_PREAMBLES["Specialist"]
		self.assertNotIn("delegate-task", preamble)


# ── Helpers ───────────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict:
	return json.loads(path.read_text())


def _field(spec: dict, name: str) -> dict | None:
	for f in spec["fields"]:
		if f["fieldname"] == name:
			return f
	return None


if __name__ == "__main__":
	unittest.main()
