# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the post-turn self-improvement review (design 79/80, Slice 1).

Covers the cadence gate, the review job's contract (restricted toolset,
skip-compression, dispatch-context, best-effort), the channel-note surfacing,
and the review-model resolution. Mock-based — no DB, no LLM.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.agent_runner import self_review as SR

_S = "frappe.friday_core.agent_runner.self_review"
_SETTINGS = "frappe.friday_core.doctype.agent_settings.agent_settings.get_agent_settings"


class TestEnqueueIfDue(unittest.TestCase):
	"""Two independent cadences: memory review and skill-proposal review."""

	def test_off_when_both_intervals_zero(self):
		with (
			patch(f"{_S}._review_interval", return_value=0),
			patch(f"{_S}._skill_interval", return_value=0),
			patch(f"{_S}.frappe") as fr,
		):
			fr.db.count.return_value = 3
			SR.enqueue_if_due("s", "P")
		fr.enqueue.assert_not_called()

	def test_memory_fires_on_multiple(self):
		with (
			patch(f"{_S}._review_interval", return_value=3),
			patch(f"{_S}._skill_interval", return_value=0),
			patch(f"{_S}.frappe") as fr,
		):
			fr.db.count.return_value = 3
			SR.enqueue_if_due("s", "P")
		fr.enqueue.assert_called_once()
		kw = fr.enqueue.call_args.kwargs
		self.assertEqual(kw["queue"], "friday")
		self.assertTrue(kw["enqueue_after_commit"])
		self.assertEqual(kw["job_id"], "friday-review::s::3")
		self.assertTrue(fr.enqueue.call_args.args[0].endswith(".run_review"))

	def test_skill_fires_on_multiple(self):
		with (
			patch(f"{_S}._review_interval", return_value=0),
			patch(f"{_S}._skill_interval", return_value=5),
			patch(f"{_S}.frappe") as fr,
		):
			fr.db.count.return_value = 5
			SR.enqueue_if_due("s", "P")
		fr.enqueue.assert_called_once()
		kw = fr.enqueue.call_args.kwargs
		self.assertEqual(kw["job_id"], "friday-skillreview::s::5")
		self.assertTrue(fr.enqueue.call_args.args[0].endswith(".run_skill_review"))

	def test_both_fire_when_both_due(self):
		with (
			patch(f"{_S}._review_interval", return_value=3),
			patch(f"{_S}._skill_interval", return_value=3),
			patch(f"{_S}.frappe") as fr,
		):
			fr.db.count.return_value = 3
			SR.enqueue_if_due("s", "P")
		self.assertEqual(fr.enqueue.call_count, 2)

	def test_skips_off_cadence(self):
		with (
			patch(f"{_S}._review_interval", return_value=3),
			patch(f"{_S}._skill_interval", return_value=3),
			patch(f"{_S}.frappe") as fr,
		):
			fr.db.count.return_value = 4  # 4 % 3 != 0
			SR.enqueue_if_due("s", "P")
		fr.enqueue.assert_not_called()

	def test_skips_at_zero_count(self):
		with (
			patch(f"{_S}._review_interval", return_value=3),
			patch(f"{_S}._skill_interval", return_value=3),
			patch(f"{_S}.frappe") as fr,
		):
			fr.db.count.return_value = 0
			SR.enqueue_if_due("s", "P")
		fr.enqueue.assert_not_called()


class TestRunReview(unittest.TestCase):
	_RT = "frappe.friday_core.agent_runner.runner.run_turn"

	def test_runs_memory_only_restricted_turn(self):
		with (
			patch(f"{_S}.frappe") as fr,
			patch(self._RT, return_value="Nothing to save.") as rt,
			patch(f"{_S}._resolve_review_provider", return_value=None),
			patch(f"{_S}._surface") as surf,
		):
			SR.run_review("s", "P")
		kw = rt.call_args.kwargs
		self.assertEqual(kw["allowed_skills"], {"remember"})
		self.assertTrue(kw["skip_compression"])
		self.assertEqual(kw["inbound_content"], SR._MEMORY_REVIEW_PROMPT)
		surf.assert_called_once()
		# dispatch context cleared in finally
		self.assertIsNone(fr.flags.friday_dispatch_context)

	def test_best_effort_swallows_errors(self):
		with (
			patch(f"{_S}.frappe") as fr,
			patch(self._RT, side_effect=RuntimeError("boom")),
			patch(f"{_S}._resolve_review_provider", return_value=None),
		):
			SR.run_review("s", "P")  # must not raise
		fr.log_error.assert_called()
		self.assertIsNone(fr.flags.friday_dispatch_context)


class TestRunSkillReview(unittest.TestCase):
	_RT = "frappe.friday_core.agent_runner.runner.run_turn"

	def test_runs_propose_only_restricted_turn(self):
		with (
			patch(f"{_S}.frappe") as fr,
			patch(self._RT, return_value="Nothing to propose.") as rt,
			patch(f"{_S}._resolve_review_provider", return_value=None),
			patch(f"{_S}._surface") as surf,
		):
			SR.run_skill_review("s", "P")
		kw = rt.call_args.kwargs
		self.assertEqual(kw["allowed_skills"], {"propose_skill_change"})
		self.assertTrue(kw["skip_compression"])
		self.assertEqual(kw["inbound_content"], SR._SKILL_REVIEW_PROMPT)
		# surfaced with the skill prefix + its own "nothing" sentinel
		self.assertEqual(surf.call_args.kwargs["skip_phrase"], "nothing to propose")
		self.assertIn("Skill proposal", surf.call_args.kwargs["prefix"])
		self.assertIsNone(fr.flags.friday_dispatch_context)


class TestSurface(unittest.TestCase):
	def test_nothing_to_save_is_silent(self):
		with patch(f"{_S}.frappe") as fr:
			SR._surface("s", "P", "Nothing to save.")
		fr.get_doc.assert_not_called()

	def test_empty_reply_is_silent(self):
		with patch(f"{_S}.frappe") as fr:
			SR._surface("s", "P", "   ")
		fr.get_doc.assert_not_called()

	def test_posts_learned_note(self):
		with patch(f"{_S}.frappe") as fr:
			fr.db.get_value.return_value = "cli"
			SR._surface("s", "P", "User prefers sans-serif fonts")
		row = fr.get_doc.call_args[0][0]
		self.assertEqual(row["direction"], "outbound")
		self.assertEqual(row["sender_id"], "P")
		self.assertIn("💾 Learned: User prefers sans-serif fonts", row["content"])
		fr.get_doc.return_value.insert.assert_called_once()


class TestResolveReviewProvider(unittest.TestCase):
	def test_blank_returns_none(self):
		doc = MagicMock()
		doc.get.return_value = ""
		with patch(_SETTINGS, return_value=doc):
			self.assertIsNone(SR._resolve_review_provider())

	def test_named_resolves_provider(self):
		doc = MagicMock()
		doc.get.return_value = "Cheap Model"
		prov = object()
		with (
			patch(_SETTINGS, return_value=doc),
			patch("frappe.friday_core.llm.provider.get_provider_by_name", return_value=prov) as gp,
		):
			self.assertIs(SR._resolve_review_provider(), prov)
		gp.assert_called_once_with("Cheap Model")


if __name__ == "__main__":
	unittest.main()
