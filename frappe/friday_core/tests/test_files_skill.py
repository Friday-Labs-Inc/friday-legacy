# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the deliverable-as-Frappe-File tools (design 66b).

The Project becomes the home of the agent's finished files: every saved
deliverable is a Frappe ``File`` attached to a Project (or Task), visible
in the Desk attachment sidebar, governed by Frappe's permission model.
No custom file infrastructure — Hermes would have to build a tool per
file type; Friday inherits the whole thing.
"""

import unittest
from unittest.mock import MagicMock, patch

_H = "frappe.friday_core.skills.handlers_files"


def _ctx(profile="Friday", session="sess-1"):
	return {"agent_profile": profile, "session_id": session}


class TestAttachDeliverableParameterValidation(unittest.TestCase):
	"""Parameters validated before any DB or permission check."""

	@patch(f"{_H}.frappe")
	def test_missing_content_raises(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = _ctx()
		with self.assertRaises(ValueError):
			attach_deliverable(
				"attach-deliverable",
				{"project_name": "PRJ-1", "file_name": "x.txt"},
			)

	@patch(f"{_H}.frappe")
	def test_missing_filename_raises(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = _ctx()
		with self.assertRaises(ValueError):
			attach_deliverable(
				"attach-deliverable",
				{"project_name": "PRJ-1", "content": "hello"},
			)

	@patch(f"{_H}.frappe")
	def test_must_have_project_or_task(self, mock_frappe):
		"""Either project_name or task_name is required — the file MUST attach to something."""
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = _ctx()
		with self.assertRaises(ValueError) as cm:
			attach_deliverable(
				"attach-deliverable",
				{"file_name": "x.txt", "content": "hello"},
			)
		self.assertIn("project", str(cm.exception).lower())

	@patch(f"{_H}.frappe")
	def test_missing_profile_raises(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = {}
		with self.assertRaises(ValueError):
			attach_deliverable(
				"attach-deliverable",
				{"project_name": "PRJ-1", "file_name": "x.txt", "content": "hello"},
			)


class TestAttachDeliverablePermissions(unittest.TestCase):
	"""Attach checks write permission on the parent (Project/Task)."""

	@patch(f"{_H}.frappe")
	def test_permission_denied_returns_structured_error(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.db.exists.return_value = "PRJ-1"
		mock_frappe.has_permission.return_value = False

		out = attach_deliverable(
			"attach-deliverable",
			{"project_name": "PRJ-1", "file_name": "x.txt", "content": "hello"},
		)
		self.assertEqual(out["error"], "denied_or_unreachable")

	@patch(f"{_H}.frappe")
	def test_project_not_found_returns_structured_error(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.db.exists.return_value = None

		out = attach_deliverable(
			"attach-deliverable",
			{"project_name": "PRJ-NOPE", "file_name": "x.txt", "content": "hello"},
		)
		self.assertEqual(out["error"], "denied_or_unreachable")


class TestAttachDeliverableSuccess(unittest.TestCase):
	"""On allowed write, save_file is called with the right parent linkage."""

	@patch(f"{_H}.save_file")
	@patch(f"{_H}.frappe")
	def test_attaches_to_project_with_is_private_default_true(self, mock_frappe, mock_save):
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.db.exists.return_value = "PRJ-1"
		mock_frappe.has_permission.return_value = True

		file_doc = MagicMock(name="FileDoc")
		file_doc.name = "f-abc"
		file_doc.file_url = "/private/files/x.txt"
		mock_save.return_value = file_doc

		out = attach_deliverable(
			"attach-deliverable",
			{"project_name": "PRJ-1", "file_name": "x.txt", "content": "hello"},
		)

		# Linkage: save_file(fname, content, dt, dn, ...) — Project, PRJ-1
		args, kwargs = mock_save.call_args
		self.assertEqual(args[0], "x.txt")
		self.assertEqual(args[2], "Project")
		self.assertEqual(args[3], "PRJ-1")
		self.assertEqual(kwargs.get("is_private"), 1)  # default private
		self.assertEqual(out["file_name"], "f-abc")
		self.assertEqual(out["attached_to"], "Project/PRJ-1")

	@patch(f"{_H}.save_file")
	@patch(f"{_H}.frappe")
	def test_attaches_to_task_when_task_name_given(self, mock_frappe, mock_save):
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.db.exists.return_value = "TSK-1"
		mock_frappe.has_permission.return_value = True
		mock_save.return_value = MagicMock(name="TaskFile", file_url="/u")
		mock_save.return_value.name = "f-tsk"

		attach_deliverable(
			"attach-deliverable",
			{"task_name": "TSK-1", "file_name": "out.txt", "content": "result"},
		)
		args, _ = mock_save.call_args
		self.assertEqual(args[2], "Task")
		self.assertEqual(args[3], "TSK-1")

	@patch(f"{_H}.save_file")
	@patch(f"{_H}.frappe")
	def test_bytes_content_is_passed_through(self, mock_frappe, mock_save):
		"""Binary content (e.g. PDF bytes) must not be coerced to text."""
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.db.exists.return_value = "PRJ-1"
		mock_frappe.has_permission.return_value = True
		mock_save.return_value = MagicMock(name="F", file_url="/u")
		mock_save.return_value.name = "f"

		binary = b"\x89PNG\r\n\x1a\n..."
		attach_deliverable(
			"attach-deliverable",
			{"project_name": "PRJ-1", "file_name": "logo.png", "content": binary},
		)
		args, _ = mock_save.call_args
		self.assertEqual(args[1], binary)


class TestListProjectFiles(unittest.TestCase):
	"""list-project-files returns the File rows attached to a Project."""

	@patch(f"{_H}.frappe")
	def test_returns_attached_files(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import list_project_files

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = True
		mock_frappe.db.exists.return_value = "PRJ-1"
		mock_frappe.db.get_all.return_value = [
			{"name": "f1", "file_name": "naming.txt", "file_url": "/u/1", "is_private": 1, "file_size": 42},
			{"name": "f2", "file_name": "logo.png", "file_url": "/u/2", "is_private": 1, "file_size": 1024},
		]

		out = list_project_files("list-project-files", {"project_name": "PRJ-1"})

		self.assertEqual(out["row_count"], 2)
		self.assertEqual(out["files"][0]["file_name"], "naming.txt")
		# The query filters by attached_to_doctype=Project, attached_to_name=PRJ-1.
		call = mock_frappe.db.get_all.call_args
		filters = call.kwargs["filters"]
		self.assertEqual(filters["attached_to_doctype"], "Project")
		self.assertEqual(filters["attached_to_name"], "PRJ-1")

	@patch(f"{_H}.frappe")
	def test_permission_denied_returns_empty_list(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import list_project_files

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = False

		out = list_project_files("list-project-files", {"project_name": "PRJ-1"})

		self.assertEqual(out["files"], [])
		self.assertTrue(out["denied"])

	@patch(f"{_H}.frappe")
	def test_project_not_found_returns_structured_error(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import list_project_files

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.db.exists.return_value = None

		out = list_project_files("list-project-files", {"project_name": "PRJ-NOPE"})

		self.assertEqual(out["error"], "denied_or_unreachable")


class TestGetProjectFile(unittest.TestCase):
	"""get-project-file returns content of one File attached to a Project."""

	@patch(f"{_H}.frappe")
	def test_returns_content_when_allowed(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import get_project_file

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = True
		mock_frappe.db.exists.return_value = True  # docname resolves

		file_doc = MagicMock()
		file_doc.attached_to_doctype = "Project"
		file_doc.attached_to_name = "PRJ-1"
		file_doc.file_name = "naming.txt"
		file_doc.is_private = 1
		file_doc.get_content.return_value = "Three names: A, B, C."
		mock_frappe.get_doc.return_value = file_doc

		out = get_project_file("get-project-file", {"project_name": "PRJ-1", "file_name": "f-abc"})

		self.assertEqual(out["file_name"], "naming.txt")
		self.assertEqual(out["content"], "Three names: A, B, C.")

	@patch(f"{_H}.frappe")
	def test_refuses_when_file_not_attached_to_named_project(self, mock_frappe):
		"""Cross-project file access via name guess must be refused."""
		from frappe.friday_core.skills.handlers_files import get_project_file

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = True
		mock_frappe.db.exists.return_value = True

		file_doc = MagicMock()
		file_doc.attached_to_doctype = "Project"
		file_doc.attached_to_name = "PRJ-SOMETHING-ELSE"  # not the one asked
		mock_frappe.get_doc.return_value = file_doc

		out = get_project_file("get-project-file", {"project_name": "PRJ-1", "file_name": "f-abc"})
		self.assertEqual(out["error"], "denied_or_unreachable")

	@patch(f"{_H}.frappe")
	def test_permission_denied_returns_structured_error(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import get_project_file

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = False

		out = get_project_file("get-project-file", {"project_name": "PRJ-1", "file_name": "f-abc"})
		self.assertEqual(out["error"], "denied_or_unreachable")

	@patch(f"{_H}.frappe")
	def test_falls_back_to_human_filename_scoped_to_project(self, mock_frappe):
		"""The bug caught on a live E2E: `list-project-files` returns each File as
		{name, file_name, ...}, so an agent easily passes the HUMAN name where the
		handler expected the docname. Now we resolve either — scoped to the project
		so a filename from a different project still returns denied_or_unreachable.
		"""
		from frappe.friday_core.skills.handlers_files import get_project_file

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = True
		# Docname lookup fails (agent passed the human filename)...
		mock_frappe.db.exists.return_value = False
		# ...but a project-scoped lookup by human filename resolves to the real docname.
		mock_frappe.db.get_value.return_value = "a1d916290f"

		file_doc = MagicMock()
		file_doc.attached_to_doctype = "Project"
		file_doc.attached_to_name = "PRJ-1"
		file_doc.file_name = "friday-labs-design-system.md"
		file_doc.is_private = 1
		file_doc.get_content.return_value = "# Friday Labs Inc — Brand & Design System\n..."
		mock_frappe.get_doc.return_value = file_doc

		out = get_project_file(
			"get-project-file",
			{"project_name": "PRJ-1", "file_name": "friday-labs-design-system.md"},
		)
		# The fallback lookup MUST be project-scoped — otherwise an agent could
		# guess a filename from a different project and read across boundaries.
		gv_call = mock_frappe.db.get_value.call_args
		self.assertEqual(gv_call.args[0], "File")
		self.assertEqual(gv_call.args[1]["attached_to_doctype"], "Project")
		self.assertEqual(gv_call.args[1]["attached_to_name"], "PRJ-1")
		self.assertEqual(gv_call.args[1]["file_name"], "friday-labs-design-system.md")
		# And the get_doc call used the RESOLVED docname, not the human string.
		self.assertEqual(mock_frappe.get_doc.call_args.args, ("File", "a1d916290f"))
		self.assertIn("Friday Labs", out["content"])

	@patch(f"{_H}.frappe")
	def test_fallback_still_not_found_returns_denied(self, mock_frappe):
		"""Neither the docname nor a project-scoped filename resolves → denied."""
		from frappe.friday_core.skills.handlers_files import get_project_file

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = True
		mock_frappe.db.exists.return_value = False
		mock_frappe.db.get_value.return_value = None  # not on this project

		out = get_project_file(
			"get-project-file",
			{"project_name": "PRJ-1", "file_name": "hallucinated-name.md"},
		)
		self.assertEqual(out["error"], "denied_or_unreachable")
		# We did NOT call get_doc — the fallback failed first.
		mock_frappe.get_doc.assert_not_called()

	@patch(f"{_H}.frappe")
	def test_fallback_wont_leak_across_projects(self, mock_frappe):
		"""Project-scope guard: an agent that names a file living on a DIFFERENT
		project (matching by human filename) still gets denied. The scope is on
		the fallback query itself; simulate by returning None from get_value —
		i.e., no file with that human name is attached to this project."""
		from frappe.friday_core.skills.handlers_files import get_project_file

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = True
		mock_frappe.db.exists.return_value = False
		mock_frappe.db.get_value.return_value = None  # scoped lookup found nothing

		out = get_project_file(
			"get-project-file",
			{"project_name": "PRJ-1", "file_name": "someone-elses-secrets.md"},
		)
		self.assertEqual(out["error"], "denied_or_unreachable")


if __name__ == "__main__":
	unittest.main()


class TestDispatcherResultContract(unittest.TestCase):
	"""THE contract the E2E bug hid behind: the dispatcher shows the LLM ONLY
	outcome["result"] (dispatcher.py: `content=outcome.get("result", "Done.")`).
	A handler return without a `result` key renders as a bare "Done." — the agent
	is blind to file lists, file contents, and even denials (a denial that renders
	"Done." reads like success). Every agent-visible return MUST carry `result`.
	Caught live: gpt-5.5 got "Done." for list-project-files, fabricated filenames,
	and could never read the human CD's design system."""

	@patch(f"{_H}.frappe")
	def test_list_project_files_success_renders_result(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import list_project_files

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.db.exists.return_value = True
		mock_frappe.has_permission.return_value = True
		mock_frappe.db.get_all.return_value = [
			{
				"name": "a1d916290f",
				"file_name": "friday-labs-design-system.md",
				"file_url": "/x",
				"is_private": 1,
				"file_size": 4585,
				"creation": "t",
			},
		]
		out = list_project_files("list-project-files", {"project_name": "PRJ-1"})
		self.assertIn("result", out)
		# The LLM-visible text must carry BOTH the human name and the docname id.
		self.assertIn("friday-labs-design-system.md", out["result"])
		self.assertIn("a1d916290f", out["result"])

	@patch(f"{_H}.frappe")
	def test_list_project_files_denial_renders_result(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import list_project_files

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.db.exists.return_value = True
		mock_frappe.has_permission.return_value = False
		out = list_project_files("list-project-files", {"project_name": "PRJ-1"})
		self.assertIn("permission", out["result"])  # a denial must NOT render "Done."

	@patch(f"{_H}.frappe")
	def test_get_project_file_content_is_the_result(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import get_project_file

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = True
		mock_frappe.db.exists.return_value = True
		file_doc = MagicMock()
		file_doc.attached_to_doctype = "Project"
		file_doc.attached_to_name = "PRJ-1"
		file_doc.file_name = "friday-labs-design-system.md"
		file_doc.is_private = 1
		file_doc.get_content.return_value = "# Friday Labs — tokens: #0B0D0F, #3DD6C4"
		mock_frappe.get_doc.return_value = file_doc

		out = get_project_file("get-project-file", {"project_name": "PRJ-1", "file_name": "a1d916290f"})
		# The FILE BODY must be inside result — that's all the model sees.
		self.assertIn("#0B0D0F", out["result"])
		self.assertIn("#3DD6C4", out["result"])

	@patch(f"{_H}.frappe")
	def test_get_project_file_denial_renders_result(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import get_project_file

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.has_permission.return_value = True
		mock_frappe.db.exists.return_value = False
		mock_frappe.db.get_value.return_value = None
		out = get_project_file("get-project-file", {"project_name": "PRJ-1", "file_name": "nope.md"})
		self.assertIn("not readable", out["result"])
		self.assertIn("list-project-files", out["result"])  # tells the agent how to self-correct

	@patch(f"{_H}.frappe")
	def test_attach_deliverable_denial_renders_result(self, mock_frappe):
		from frappe.friday_core.skills.handlers_files import attach_deliverable

		mock_frappe.flags.get.return_value = _ctx()
		mock_frappe.db.exists.return_value = False
		out = attach_deliverable(
			"attach-deliverable",
			{"project_name": "PRJ-1", "file_name": "x.md", "content": "hello"},
		)
		self.assertIn("Could not attach", out["result"])
