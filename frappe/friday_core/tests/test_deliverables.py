# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for artifact materialization (Design 73, Slice 5).

Coverage:
- a completed task with success content gets an attached .md file (+ .pdf if available)
- the .md content includes the agent's summary + a title header
- a gate / error / empty result produces NO artifact (we never fabricate)
- re-materializing replaces prior deliverable-* files (idempotent)
- assemble_project_package concatenates task deliverables into one Project file
- _extract_markdown handles success envelope, error envelope, plain text, blank
- materialization is best-effort: on_task_completed never raises
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import frappe


def _project() -> str:
	existing = frappe.get_all("Project", limit=1, pluck="name")
	if existing:
		return existing[0]
	return (
		frappe.get_doc({"doctype": "Project", "project_name": "Deliverables Test Project", "status": "Open"})
		.insert(ignore_permissions=True)
		.name
	)


def _task(project: str, title: str, result: "dict | None") -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Task",
			"title": title,
			"project": project,
			"workflow_state": "Pending",
			"result": frappe.as_json(result) if result is not None else None,
		}
	).insert(ignore_permissions=True)
	return doc.name


def _deliverable_files(dt: str, dn: str) -> list[str]:
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": dt, "attached_to_name": dn, "file_name": ["like", "deliverable-%"]},
		pluck="file_name",
	)


def _cleanup_task(task_name: str) -> None:
	for f in frappe.get_all(
		"File", filters={"attached_to_doctype": "Task", "attached_to_name": task_name}, pluck="name"
	):
		frappe.delete_doc("File", f, force=True, ignore_permissions=True)
	if frappe.db.exists("Task", task_name):
		frappe.delete_doc("Task", task_name, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestExtractMarkdown(unittest.TestCase):
	"""The content extractor never fabricates a deliverable."""

	def test_success_envelope_returns_summary(self):
		from frappe.friday_core.deliverables.materialize import _extract_markdown

		raw = json.dumps({"status": "success", "summary": "# Strategy\nGo to market via X."})
		self.assertIn("Go to market", _extract_markdown(raw))

	def test_error_envelope_returns_none(self):
		from frappe.friday_core.deliverables.materialize import _extract_markdown

		self.assertIsNone(_extract_markdown(json.dumps({"status": "error", "error_type": "LLMError"})))

	def test_empty_returns_none(self):
		from frappe.friday_core.deliverables.materialize import _extract_markdown

		self.assertIsNone(_extract_markdown(""))
		self.assertIsNone(_extract_markdown(None))
		self.assertIsNone(_extract_markdown(json.dumps({"status": "success", "summary": ""})))

	def test_plain_text_returned_as_is(self):
		from frappe.friday_core.deliverables.materialize import _extract_markdown

		self.assertEqual(_extract_markdown("just text"), "just text")


class TestMaterializeTask(unittest.TestCase):
	"""Completed task -> attached artifact files."""

	@classmethod
	def setUpClass(cls):
		cls.project = _project()

	def setUp(self):
		self.task = _task(
			self.project,
			"Strategy & positioning",
			{"status": "success", "summary": "# Strategy\n\nPosition at the infra layer of the agent stack."},
		)

	def tearDown(self):
		_cleanup_task(self.task)

	def test_creates_markdown_artifact(self):
		from frappe.friday_core.deliverables.materialize import materialize_task_deliverable

		created = materialize_task_deliverable(self.task)
		frappe.db.commit()
		self.assertIsNotNone(created)
		self.assertIn("md", created)
		files = _deliverable_files("Task", self.task)
		self.assertTrue(any(f.endswith(".md") for f in files))

	def test_markdown_includes_summary_and_title(self):
		from frappe.friday_core.deliverables.materialize import materialize_task_deliverable

		created = materialize_task_deliverable(self.task)
		frappe.db.commit()
		md_doc = frappe.get_doc("File", created["md"])
		body = md_doc.get_content()
		if isinstance(body, bytes):
			body = body.decode("utf-8")
		self.assertIn("Strategy & positioning", body)  # title header
		self.assertIn("infra layer of the agent stack", body)  # the summary

	def test_idempotent_replaces_prior_files(self):
		from frappe.friday_core.deliverables.materialize import materialize_task_deliverable

		materialize_task_deliverable(self.task)
		frappe.db.commit()
		materialize_task_deliverable(self.task)
		frappe.db.commit()
		# Same logical files, not doubled.
		count_md = sum(1 for f in _deliverable_files("Task", self.task) if f.endswith(".md"))
		self.assertEqual(count_md, 1)


class TestNoArtifactForNonDeliverable(unittest.TestCase):
	"""Gates / errors / empties never produce an artifact."""

	@classmethod
	def setUpClass(cls):
		cls.project = _project()

	def test_gate_with_empty_result_no_artifact(self):
		from frappe.friday_core.deliverables.materialize import materialize_task_deliverable

		task = _task(self.project, "Gate 1 — client picks", None)
		try:
			self.assertIsNone(materialize_task_deliverable(task))
			self.assertEqual(_deliverable_files("Task", task), [])
		finally:
			_cleanup_task(task)

	def test_error_result_no_artifact(self):
		from frappe.friday_core.deliverables.materialize import materialize_task_deliverable

		task = _task(self.project, "Failed task", {"status": "error", "error_type": "LLMError"})
		try:
			self.assertIsNone(materialize_task_deliverable(task))
			self.assertEqual(_deliverable_files("Task", task), [])
		finally:
			_cleanup_task(task)


class TestProjectPackage(unittest.TestCase):
	"""assemble_project_package concatenates task deliverables onto the Project."""

	def test_package_combines_task_deliverables(self):
		from frappe.friday_core.deliverables.materialize import assemble_project_package

		project = (
			frappe.get_doc({"doctype": "Project", "project_name": "Pkg Test Project", "status": "Open"})
			.insert(ignore_permissions=True)
			.name
		)
		t1 = _task(project, "Strategy", {"status": "success", "summary": "Strategy content ABC"})
		t2 = _task(project, "Naming", {"status": "success", "summary": "Naming content XYZ"})
		try:
			created = assemble_project_package(project)
			frappe.db.commit()
			self.assertIsNotNone(created)
			md_doc = frappe.get_doc("File", created["md"])
			body = md_doc.get_content()
			if isinstance(body, bytes):
				body = body.decode("utf-8")
			self.assertIn("Strategy content ABC", body)
			self.assertIn("Naming content XYZ", body)
			self.assertIn("Deliverable Package", body)
		finally:
			_cleanup_task(t1)
			_cleanup_task(t2)
			for f in frappe.get_all(
				"File", filters={"attached_to_doctype": "Project", "attached_to_name": project}, pluck="name"
			):
				frappe.delete_doc("File", f, force=True, ignore_permissions=True)
			frappe.delete_doc("Project", project, force=True, ignore_permissions=True)
			frappe.db.commit()


class TestBestEffort(unittest.TestCase):
	"""Materialization never raises."""

	def test_on_task_completed_swallows_errors(self):
		from frappe.friday_core.deliverables.materialize import on_task_completed

		with patch(
			"frappe.friday_core.deliverables.materialize.materialize_task_deliverable",
			side_effect=RuntimeError("boom"),
		):
			# Must not raise.
			on_task_completed("nonexistent-task")
