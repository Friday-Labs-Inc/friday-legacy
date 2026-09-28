# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for project-scoped memory + project context injection (Design 73, Part B).

The bug: in a project room (a Raven channel linked to a Project), the agent had
no idea which project it was in and recalled EVERY project's memories — so one
client's facts ("no serifs" for Loop Coffee) bled into another's room.

Coverage:
- project_for_session resolves a Raven channel's linked project.
- project_for_session resolves a task:: session's project.
- project_for_session returns None for an unknown / non-project session.
- recall_block(project=X) returns X's memories + global, excludes other projects'.
- recall_block(project=None) returns all (DM behavior unchanged).
- build() injects PROJECT CONTEXT framing when the session maps to a project.
- the remember skill tags a new memory with the session's project.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe


def _profile() -> str:
	name = "D73-MEM-PROFILE"
	if not frappe.db.exists("Agent Profile", name):
		frappe.get_doc({"doctype": "Agent Profile", "profile_name": name, "status": "Active"}).insert(
			ignore_permissions=True
		)
	return name


def _mem(profile: str, text: str, project: str | None) -> str:
	return (
		frappe.get_doc(
			{
				"doctype": "Agent Memory",
				"memory": text,
				"agent_profile": profile,
				"project": project,
				"status": "Active",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _project(name: str) -> str:
	if not frappe.db.exists("Project", name):
		frappe.get_doc({"doctype": "Project", "project_name": name, "status": "Open"}).insert(
			ignore_permissions=True
		)
	return name


class TestProjectForSession(unittest.TestCase):
	def test_task_session_resolves_project(self):
		from frappe.friday_core.llm.memory import project_for_session

		proj = _project("D73 Mem Proj A")
		task = frappe.get_doc(
			{"doctype": "Task", "title": "mem-scope task", "project": proj, "workflow_state": "Pending"}
		).insert(ignore_permissions=True)
		try:
			self.assertEqual(project_for_session(f"task::{task.name}"), proj)
		finally:
			frappe.delete_doc("Task", task.name, force=True, ignore_permissions=True)

	def test_unknown_session_returns_none(self):
		from frappe.friday_core.llm.memory import project_for_session

		self.assertIsNone(project_for_session("some-random-dm-session"))
		self.assertIsNone(project_for_session(""))

	@unittest.skipUnless(frappe.db.table_exists("Raven Channel"), "Raven not installed")
	def test_raven_channel_session_resolves_linked_project(self):
		from frappe.friday_core.llm.memory import project_for_session

		# A channel linked to a Project (as Slice 1 provisions).
		proj = _project("D73 Mem Proj Raven")
		ws = "Friday" if frappe.db.exists("Raven Workspace", "Friday") else None
		ch = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"channel_name": "d73-mem-scope-test",
				"type": "Open",
				"workspace": ws,
				"linked_doctype": "Project",
				"linked_document": proj,
			}
		).insert(ignore_permissions=True)
		try:
			self.assertEqual(project_for_session(ch.name), proj)
		finally:
			frappe.db.sql("DELETE FROM `tabRaven Channel Member` WHERE channel_id = %s", (ch.name,))
			frappe.delete_doc("Raven Channel", ch.name, force=True, ignore_permissions=True)


class TestRecallScoping(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.profile = _profile()
		_project("D73 Proj FLI")
		_project("D73 Proj Loop")

	def setUp(self):
		frappe.db.sql("DELETE FROM `tabAgent Memory` WHERE agent_profile = %s", (self.profile,))
		frappe.db.commit()

	def tearDown(self):
		frappe.db.sql("DELETE FROM `tabAgent Memory` WHERE agent_profile = %s", (self.profile,))
		frappe.db.commit()

	def test_project_scope_excludes_other_project(self):
		from frappe.friday_core.llm.memory import recall_block

		_mem(self.profile, "FLI fact: chose Mission Control", "D73 Proj FLI")
		_mem(self.profile, "Loop fact: no serifs", "D73 Proj Loop")
		_mem(self.profile, "Global fact: house style is bold", None)
		frappe.db.commit()

		block = recall_block(self.profile, project="D73 Proj FLI") or ""
		self.assertIn("Mission Control", block)  # this project
		self.assertIn("house style is bold", block)  # global
		self.assertNotIn("no serifs", block)  # OTHER project — excluded

	def test_no_project_returns_all(self):
		from frappe.friday_core.llm.memory import recall_block

		_mem(self.profile, "FLI fact: chose Mission Control", "D73 Proj FLI")
		_mem(self.profile, "Loop fact: no serifs", "D73 Proj Loop")
		frappe.db.commit()

		block = recall_block(self.profile, project=None) or ""
		# DM / non-project session — no scoping, both present.
		self.assertIn("Mission Control", block)
		self.assertIn("no serifs", block)


class TestBuildInjectsProjectContext(unittest.TestCase):
	@unittest.skipUnless(frappe.db.table_exists("Raven Channel"), "Raven not installed")
	def test_build_adds_project_framing(self):
		from frappe.friday_core.llm.prompt_builder import build

		profile = _profile()
		proj = _project("D73 Build Proj")
		ws = "Friday" if frappe.db.exists("Raven Workspace", "Friday") else None
		ch = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"channel_name": "d73-build-ctx-test",
				"type": "Open",
				"workspace": ws,
				"linked_doctype": "Project",
				"linked_document": proj,
			}
		).insert(ignore_permissions=True)
		try:
			prompt = build(profile_name=profile, session_id=ch.name, inbound_content="hi", tools=None)
			system_texts = " ".join(m["content"] for m in prompt["messages"] if m["role"] == "system")
			self.assertIn("PROJECT CONTEXT", system_texts)
			self.assertIn(proj, system_texts)
		finally:
			frappe.db.sql("DELETE FROM `tabRaven Channel Member` WHERE channel_id = %s", (ch.name,))
			frappe.delete_doc("Raven Channel", ch.name, force=True, ignore_permissions=True)


class TestRememberTagsProject(unittest.TestCase):
	def test_remember_tags_session_project(self):
		from frappe.friday_core.skills.handlers_memory import remember

		profile = _profile()
		proj = _project("D73 Remember Proj")
		task = frappe.get_doc(
			{"doctype": "Task", "title": "remember-scope task", "project": proj, "workflow_state": "Pending"}
		).insert(ignore_permissions=True)
		frappe.flags.friday_dispatch_context = {
			"agent_profile": profile,
			"session_id": f"task::{task.name}",
		}
		try:
			out = remember("remember", {"memory": "a durable fact for this project"})
			tagged = frappe.db.get_value("Agent Memory", out["record_name"], "project")
			self.assertEqual(tagged, proj)

			# Design 95: scope="global" stores UNTAGGED even in a project session,
			# so cross-project lessons (the apprenticeship) recall everywhere.
			out2 = remember("remember", {"memory": "a cross-project craft lesson", "scope": "global"})
			self.assertIsNone(frappe.db.get_value("Agent Memory", out2["record_name"], "project"))
		finally:
			frappe.flags.friday_dispatch_context = None
			frappe.db.sql("DELETE FROM `tabAgent Memory` WHERE agent_profile = %s", (profile,))
			frappe.delete_doc("Task", task.name, force=True, ignore_permissions=True)
			frappe.db.commit()
