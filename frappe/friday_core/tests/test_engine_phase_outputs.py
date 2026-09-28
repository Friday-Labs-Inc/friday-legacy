# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Tests for the generic `get-phase-outputs` skill (Design 75 Phase 1.1).

The skill lets a phase read what the earlier phases of the same work-item
produced. These tests cover the pure result-parsing helper and the handler's
two ways of finding the work-item (explicit `work_item` arg, or derived from
the executing Task via the dispatch context). Background side effects are
neutralised by patching `frappe.enqueue`; nothing is committed.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import frappe
from frappe.friday_core.skills.handlers_engine import _summary_text, get_phase_outputs


class TestSummaryText(unittest.TestCase):
	def test_json_summary(self):
		self.assertEqual(_summary_text(json.dumps({"status": "success", "summary": "HELLO"})), "HELLO")

	def test_json_result_fallback(self):
		self.assertEqual(_summary_text(json.dumps({"result": "R"})), "R")

	def test_bare_string(self):
		self.assertEqual(_summary_text("plain text"), "plain text")

	def test_empty(self):
		self.assertEqual(_summary_text(None), "")
		self.assertEqual(_summary_text(""), "")


class TestGetPhaseOutputs(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.db.rollback()
		# Neutralise background jobs so creating tasks doesn't fire the runner,
		# advance, or deliverables — this test only exercises the read path.
		cls._enq = patch("frappe.enqueue", lambda *a, **k: None)
		cls._enq.start()

	@classmethod
	def tearDownClass(cls):
		cls._enq.stop()
		frappe.db.rollback()

	def tearDown(self):
		frappe.flags["friday_dispatch_context"] = None
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _work_item(self):
		# get-phase-outputs reads tasks by work_item, independent of the item's
		# own workflow state — so any DocType serves as the work-item here. We
		# use a kernel one (Project) on purpose: this test must pass on a bare
		# kernel install with no domain app present.
		return frappe.get_doc({"doctype": "Project", "project_name": "PhaseOut Co"}).insert(
			ignore_permissions=True
		)

	def _completed(self, item_name, phase_key, summary):
		return frappe.get_doc(
			{
				"doctype": "Task",
				"title": f"t-{phase_key}",
				"priority": "normal",
				"work_item_doctype": "Project",
				"work_item_name": item_name,
				"phase_key": phase_key,
				"workflow_state": "Completed",
				"result": json.dumps({"status": "success", "summary": summary}),
			}
		).insert(ignore_permissions=True)

	def test_named_work_item_returns_prior_outputs(self):
		item = self._work_item()
		self._completed(item.name, "strategy", "STRAT_BODY_123")
		self._completed(item.name, "naming", "NAME_BODY_456")
		out = get_phase_outputs("get-phase-outputs", {"work_item": item.name, "work_item_doctype": "Project"})
		self.assertIn("STRAT_BODY_123", out["result"])
		self.assertIn("NAME_BODY_456", out["result"])
		self.assertIn("strategy", out["result"])

	def test_derives_work_item_from_task_context(self):
		item = self._work_item()
		self._completed(item.name, "strategy", "CTX_BODY_789")
		current = frappe.get_doc(
			{
				"doctype": "Task",
				"title": "cur",
				"priority": "normal",
				"work_item_doctype": "Project",
				"work_item_name": item.name,
				"phase_key": "gate1_prep",
				"workflow_state": "Executing",
			}
		).insert(ignore_permissions=True)
		frappe.flags["friday_dispatch_context"] = {"session_id": f"task::{current.name}"}
		out = get_phase_outputs("get-phase-outputs", {})
		self.assertIn("CTX_BODY_789", out["result"])

	def test_no_context_returns_helpful_message(self):
		frappe.flags["friday_dispatch_context"] = {}
		out = get_phase_outputs("get-phase-outputs", {})
		self.assertIn("No work item", out["result"])
