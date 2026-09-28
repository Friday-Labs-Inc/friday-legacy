# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the governed skill-proposal skill (design 79 Slice 2).

`propose_skill_change` records a Pending Skill Proposal for human review — it
never edits a skill. Covers the handler contract, the doctype shape, and the
skill schema. Mock-based — no DB.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_H = "frappe.friday_core.skills.handlers_propose_skill"


class TestSkillProposalDoctype(unittest.TestCase):
	def test_fields_and_pending_default(self):
		p = Path(__file__).resolve().parents[1] / "doctype" / "skill_proposal" / "skill_proposal.json"
		d = json.loads(p.read_text())
		names = {f["fieldname"] for f in d["fields"]}
		self.assertTrue({"agent_profile", "status", "proposal_type", "title", "proposed_content"} <= names)
		status = next(f for f in d["fields"] if f["fieldname"] == "status")
		self.assertEqual(status["default"], "Pending")

	def test_skill_schema(self):
		from frappe.friday_core.skills.bootstrap_propose_skill import _SKILL

		self.assertEqual(_SKILL["skill_name"], "propose_skill_change")
		self.assertIn("title", _SKILL["parameters_schema"]["required"])


class TestProposeHandler(unittest.TestCase):
	def _propose(self, params, profile="P"):
		from frappe.friday_core.skills.handlers_propose_skill import propose_skill_change

		with patch(f"{_H}.frappe") as fr:
			fr.flags.get.return_value = {"agent_profile": profile, "session_id": "s"}
			doc = MagicMock()
			doc.name = "SP-1"
			fr.get_doc.return_value = doc
			result = propose_skill_change("propose_skill_change", params)
			return fr.get_doc.call_args[0][0], result, doc

	def test_creates_pending_proposal(self):
		row, result, doc = self._propose(
			{
				"title": "Embed the no-serif rule",
				"proposal_type": "update_skill",
				"target_skill": "brand-naming",
			}
		)
		self.assertEqual(row["doctype"], "Skill Proposal")
		self.assertEqual(row["status"], "Pending")
		self.assertEqual(row["agent_profile"], "P")
		self.assertEqual(row["proposal_type"], "update_skill")
		self.assertEqual(row["title"], "Embed the no-serif rule")
		doc.insert.assert_called_once()
		self.assertIn("Pending", result["result"])

	def test_invalid_type_coerced(self):
		row, _, _ = self._propose({"title": "x", "proposal_type": "bogus"})
		self.assertEqual(row["proposal_type"], "new_skill")

	def test_missing_title_raises(self):
		with self.assertRaises(ValueError):
			self._propose({"proposal_type": "new_skill"})

	def test_missing_profile_raises(self):
		with self.assertRaises(ValueError):
			self._propose({"title": "x"}, profile="")


if __name__ == "__main__":
	unittest.main()
