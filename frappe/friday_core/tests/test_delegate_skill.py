# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the delegate-task skill (design 69a, LOCKED).

Mock-based — no DB, no model. Design 69a rewrote the original design 57 handler
from synchronous-inline to async-durable. Pins the new locked contract:

  ROLE GATE   only agent_role == "Orchestrator" may delegate (Q4).
  DEPTH GATE  the parent_task chain must be < max_delegation_depth (Q3).
  CONCURRENCY each parent's active children must be < max_concurrent (Q10).
  CREATE      a Pending child Task — parent_task set, project inherited (Q2/Q8);
              the handler returns immediately, the pipeline runs the child.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.friday_core.skills import handlers_delegate

_H = "frappe.friday_core.skills.handlers_delegate"


def _params(**overrides):
	p = {
		"agent_profile": "Copywriter",
		"instruction": "Write 3 tagline options for Loop Coffee. Warm, crafted, honest.",
	}
	p.update(overrides)
	return p


def _profile(role="Orchestrator", status="Active", max_concurrent=5):
	p = MagicMock()
	p.agent_role = role
	p.status = status
	p.max_concurrent_delegations = max_concurrent
	return p


def _settings(max_depth=3, hard_ceiling=8):
	s = MagicMock()
	s.max_delegation_depth = max_depth
	s.delegation_depth_hard_ceiling = hard_ceiling
	return s


def _wire(
	mock_frappe,
	*,
	session="chat-session-1",
	parent_profile_name="Friday",
	parent_role="Orchestrator",
	target_name="Copywriter",
	target_status="Active",
	target_exists=True,
	max_concurrent=5,
	active_children=0,
	max_depth=3,
	hard_ceiling=8,
	parent_chain=None,
	project="PROJ-1",
):
	"""Wire a patched frappe for the 69a delegate_task contract.

	Routes ``get_cached_doc`` by (doctype, name) — the handler asks for the
	parent profile, Agent Settings, and the target profile in one call each.
	``parent_chain`` maps task_name -> parent_task_name for the depth walk.
	Returns a namespace with the mock docs so a test can tweak/assert them.
	"""
	mock_frappe.flags.get.return_value = {
		"session_id": session,
		"agent_profile": parent_profile_name,
	}

	parent_profile = _profile(role=parent_role, max_concurrent=max_concurrent)
	target_profile = _profile(status=target_status)
	settings = _settings(max_depth, hard_ceiling)

	def get_cached_doc(doctype, name):
		if doctype == "Agent Settings":
			return settings
		if doctype == "Agent Profile" and name == parent_profile_name:
			return parent_profile
		if doctype == "Agent Profile" and name == target_name:
			return target_profile
		return MagicMock()

	mock_frappe.get_cached_doc.side_effect = get_cached_doc
	mock_frappe.db.exists.return_value = target_exists
	mock_frappe.db.count.return_value = active_children

	chain = parent_chain or {}

	def db_get_value(doctype, name, field):
		if field == "parent_task":
			return chain.get(name)
		if field == "project":
			return project
		return None

	mock_frappe.db.get_value.side_effect = db_get_value

	child = MagicMock()
	child.name = "TASK-CHILD-1"
	mock_frappe.get_doc.return_value = child

	return SimpleNamespace(
		parent_profile=parent_profile,
		target_profile=target_profile,
		settings=settings,
		child=child,
	)


class TestDepthAndValidation(unittest.TestCase):
	@patch(f"{_H}.frappe")
	def test_child_session_cannot_delegate(self, mock_frappe):
		# A 3-deep parent_task chain reaches the default cap (max_delegation_depth=3).
		_wire(
			mock_frappe,
			session="task::TASK-0001",
			parent_chain={
				"TASK-0001": "TASK-0002",
				"TASK-0002": "TASK-0003",
				"TASK-0003": "TASK-0004",
			},
		)
		with self.assertRaises(ValueError) as ctx:
			handlers_delegate.delegate_task("delegate-task", _params())
		self.assertIn("depth", str(ctx.exception))

	@patch(f"{_H}.frappe")
	def test_missing_required_params_raise(self, mock_frappe):
		# 69a requires agent_profile + instruction; title is optional.
		for missing in ("agent_profile", "instruction"):
			_wire(mock_frappe)
			with self.assertRaises(ValueError) as ctx:
				handlers_delegate.delegate_task("delegate-task", _params(**{missing: ""}))
			self.assertIn(missing, str(ctx.exception))

	@patch(f"{_H}.frappe")
	def test_unknown_target_profile_raises(self, mock_frappe):
		_wire(mock_frappe, target_name="Ghost", target_exists=False)
		with self.assertRaises(ValueError) as ctx:
			handlers_delegate.delegate_task("delegate-task", _params(agent_profile="Ghost"))
		self.assertIn("Ghost", str(ctx.exception))

	@patch(f"{_H}.frappe")
	def test_inactive_target_profile_raises(self, mock_frappe):
		# Target exists but is not Active — 69a reads status off the cached doc.
		_wire(mock_frappe, target_status="Draft")
		with self.assertRaises(ValueError) as ctx:
			handlers_delegate.delegate_task("delegate-task", _params())
		self.assertIn("Active", str(ctx.exception))


class TestGates(unittest.TestCase):
	@patch(f"{_H}.frappe")
	def test_non_orchestrator_cannot_delegate(self, mock_frappe):
		# ROLE GATE (Q4) — a Specialist must be refused even if it reaches the handler.
		_wire(mock_frappe, parent_role="Specialist")
		with self.assertRaises(ValueError) as ctx:
			handlers_delegate.delegate_task("delegate-task", _params())
		self.assertIn("Orchestrator", str(ctx.exception))

	@patch(f"{_H}.frappe")
	def test_concurrency_limit_blocks_delegation(self, mock_frappe):
		# CONCURRENCY GATE (Q10) — 5 active children already == default cap.
		_wire(mock_frappe, max_concurrent=5, active_children=5)
		with self.assertRaises(ValueError) as ctx:
			handlers_delegate.delegate_task("delegate-task", _params())
		self.assertIn("concurrent", str(ctx.exception))


class TestHappyPath(unittest.TestCase):
	@patch(f"{_H}.frappe")
	def test_creates_pending_child_and_returns_queued(self, mock_frappe):
		# Delegating from inside a parent task: child is Pending, parent_task set,
		# project inherited (Q2/Q8), and the handler returns immediately (Q1 async).
		w = _wire(
			mock_frappe,
			session="task::TASK-PARENT",
			parent_chain={"TASK-PARENT": None},  # depth 0 — root parent
			project="PROJ-1",
		)

		out = handlers_delegate.delegate_task("delegate-task", _params())

		# Child Task row shape.
		payload = mock_frappe.get_doc.call_args[0][0]
		self.assertEqual(payload["doctype"], "Task")
		self.assertEqual(payload["workflow_state"], "Pending")
		self.assertEqual(payload["execution_mode"], "agentic")
		self.assertEqual(payload["assigned_to_profile"], "Copywriter")
		self.assertEqual(payload["parent_task"], "TASK-PARENT")
		self.assertEqual(payload["project"], "PROJ-1")  # inherited from parent
		self.assertEqual(payload["description"], _params()["instruction"])
		w.child.insert.assert_called_once_with(ignore_permissions=True)

		# Return envelope — async/queued, never a completed result.
		self.assertEqual(out["status"], "queued")
		self.assertEqual(out["delegation_id"], "TASK-CHILD-1")
		self.assertEqual(out["child_task_name"], "TASK-CHILD-1")
		self.assertEqual(out["assigned_profile"], "Copywriter")
		self.assertEqual(out["record_name"], "TASK-CHILD-1")

	@patch(f"{_H}.frappe")
	def test_top_level_delegation_has_no_parent_task(self, mock_frappe):
		# Delegating from a chat session (not task::) is a root delegation:
		# parent_task is None, and an explicit project param is honoured.
		_wire(mock_frappe, session="chat-session-1")
		out = handlers_delegate.delegate_task("delegate-task", _params(project="PROJ-OVERRIDE"))
		payload = mock_frappe.get_doc.call_args[0][0]
		self.assertIsNone(payload["parent_task"])
		self.assertEqual(payload["project"], "PROJ-OVERRIDE")  # explicit wins
		self.assertEqual(payload["originating_session"], "chat-session-1")
		self.assertEqual(out["status"], "queued")


class TestRegistrationAndBootstrap(unittest.TestCase):
	def test_registered_with_dispatcher(self):
		from frappe.friday_core.agent_runner import dispatcher

		self.assertIs(
			dispatcher._SKILL_HANDLERS.get("delegate-task"),
			handlers_delegate.delegate_task,
		)

	def test_bootstrap_spec_is_valid(self):
		import json as _json

		from frappe.friday_core.skills.bootstrap_delegate import _SKILL

		schema = _SKILL["parameters_schema"]
		_json.dumps(schema)
		for required_field in schema["required"]:
			self.assertIn(required_field, schema["properties"])
		self.assertEqual(_SKILL["required_doctypes"], [{"target_doctype": "Task", "operation": "create"}])


if __name__ == "__main__":
	unittest.main()
