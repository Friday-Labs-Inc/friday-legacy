# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for conversation compression (Feature C, doc 51 §4.C).

Mock-based — no DB. Covers the pure logic (token estimate, trigger, head/tail
split, transcript assembly, the safety preamble), the orchestrator's decision
paths with the DB + provider mocked, and prompt_builder's summary-aware
assembly. The DB-backed integration (real Compaction Summary rows, the
`compacted` flag, runner wiring) runs on a bench.
"""

import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.llm.compression import (
	SUMMARY_PREFIX,
	_format_transcript,
	_split_middle_tail,
	_summariser_messages,
	estimate_tokens,
	maybe_compress_session,
	should_compress,
)
from frappe.friday_core.llm.prompt_builder import _load_history
from frappe.friday_core.llm.provider import LLMError

_C = "frappe.friday_core.llm.compression"


def _rows(count, chars, start=0):
	"""`count` Chat Message rows of `chars` length, alternating direction."""
	return [
		{
			"name": str(start + i),
			"direction": "inbound" if (start + i) % 2 == 0 else "outbound",
			"content": "x" * chars,
		}
		for i in range(count)
	]


# --- Pure helpers -----------------------------------------------------------


class TestEstimateTokens(unittest.TestCase):
	def test_char_over_four(self):
		# 400 chars total → 100 tokens.
		msgs = [{"content": "a" * 200}, {"content": "b" * 200}]
		self.assertEqual(estimate_tokens(msgs), 100)

	def test_handles_non_string_content(self):
		# Block-list content (e.g. Anthropic) is estimated via JSON length, not crash.
		msgs = [{"content": [{"type": "text", "text": "hi"}]}]
		self.assertGreater(estimate_tokens(msgs), 0)

	def test_missing_content_is_zero(self):
		self.assertEqual(estimate_tokens([{"role": "user"}]), 0)


class TestShouldCompress(unittest.TestCase):
	def test_under_threshold_is_false(self):
		self.assertFalse(should_compress([{"content": "short"}], context_window=1000))

	def test_over_threshold_is_true(self):
		# window 1000 → threshold 500 tokens → need > 2000 chars.
		self.assertTrue(should_compress([{"content": "x" * 4000}], context_window=1000))

	def test_threshold_is_fifty_percent(self):
		# Hermes' default threshold_percent is 0.50. Exactly at 50% must NOT
		# trigger (strictly greater). window 1000 → 500 tokens → 2000 chars == 500.
		self.assertFalse(should_compress([{"content": "x" * 2000}], context_window=1000))
		self.assertTrue(should_compress([{"content": "x" * 2004}], context_window=1000))


class TestSplitMiddleTail(unittest.TestCase):
	def test_recent_turns_protected_older_become_middle(self):
		# 6 rows of 30000 chars (~7510 tokens each); tail budget 20000 fits 2.
		rows = _rows(6, 30000)
		middle, tail = _split_middle_tail(rows)
		self.assertEqual(len(middle), 4)
		self.assertEqual(len(tail), 2)
		# tail is the most-recent slice; middle is the oldest.
		self.assertEqual(tail, rows[4:])
		self.assertEqual(middle, rows[:4])

	def test_small_history_all_fits_in_tail(self):
		rows = _rows(3, 100)
		middle, tail = _split_middle_tail(rows)
		self.assertEqual(middle, [])
		self.assertEqual(len(tail), 3)

	def test_last_user_message_always_in_tail(self):
		# Design 80 — a huge final user message exceeds the tail budget on its
		# own; without the guarantee it would land in the middle (and be folded
		# into the summary). The fix forces it into the verbatim tail.
		rows = [
			{"name": "1", "direction": "outbound", "content": "x" * 100},
			{"name": "2", "direction": "inbound", "content": "x" * 100},
			{"name": "3", "direction": "outbound", "content": "x" * 100},
			{"name": "4", "direction": "inbound", "content": "x" * 90000},  # ~22500 tok > 20000 budget
		]
		middle, tail = _split_middle_tail(rows)
		self.assertEqual(tail, [rows[3]])  # the latest user turn is kept verbatim
		self.assertNotIn(rows[3], middle)


class TestFormatTranscript(unittest.TestCase):
	def test_labels_speakers_by_direction(self):
		rows = [
			{"direction": "inbound", "content": "hello"},
			{"direction": "outbound", "content": "hi back"},
		]
		out = _format_transcript(rows, None)
		self.assertIn("User: hello", out)
		self.assertIn("Assistant: hi back", out)

	def test_prepends_previous_summary(self):
		out = _format_transcript([{"direction": "inbound", "content": "x"}], "PRIOR SUMMARY")
		self.assertIn("PRIOR SUMMARY", out)
		self.assertTrue(out.startswith("## Earlier summary"))


class TestSummaryPrefix(unittest.TestCase):
	def test_carries_load_bearing_safety_phrases(self):
		# These phrases stop the next model re-answering compacted requests.
		self.assertIn("REFERENCE ONLY", SUMMARY_PREFIX)
		self.assertIn("Do NOT answer", SUMMARY_PREFIX)
		self.assertIn("## Active Task", SUMMARY_PREFIX)
		self.assertIn("ONLY to the latest user message", SUMMARY_PREFIX)

	def test_verbatim_to_hermes_wording(self):
		# Faithful to Hermes' SUMMARY_PREFIX: US spelling + the parenthetical
		# clause that earlier silently diverged. (The only intended divergence is
		# the MEMORY.md→system-prompt sentence, asserted separately below.)
		self.assertIn("fulfill requests", SUMMARY_PREFIX)
		self.assertIn("deprioritize", SUMMARY_PREFIX)
		self.assertIn("(files, config, etc.)", SUMMARY_PREFIX)
		self.assertNotIn("fulfil ", SUMMARY_PREFIX)
		self.assertNotIn("deprioritise", SUMMARY_PREFIX)

	def test_discloses_the_one_frappe_adaptation(self):
		# Friday has no MEMORY.md/USER.md; that one sentence points at the system
		# prompt instead — the single, disclosed divergence from Hermes.
		self.assertIn("system prompt (above) is ALWAYS authoritative", SUMMARY_PREFIX)
		self.assertNotIn("MEMORY.md", SUMMARY_PREFIX)

	def test_summariser_messages_shape(self):
		msgs = _summariser_messages("transcript here")
		self.assertEqual(msgs[0]["role"], "system")
		self.assertEqual(msgs[1]["role"], "user")
		self.assertEqual(msgs[1]["content"], "transcript here")

	def test_summariser_prompt_has_structured_sections(self):
		# Design 80 Gap 2 — the summariser must request the named sections so
		# blockers / pending asks / in-progress state survive compaction.
		from frappe.friday_core.llm.compression import _SUMMARISER_SYSTEM

		for section in ("## Active Task", "## Blocked", "## Pending User Asks", "## Remaining Work"):
			self.assertIn(section, _SUMMARISER_SYSTEM)


# --- Orchestrator (DB + provider mocked) ------------------------------------


@patch(f"{_C}._extract_facts_before_compaction", return_value=0)
@patch(f"{_C}._persist_compaction", return_value="CS-1")
@patch(f"{_C}.latest_summary", return_value=None)
@patch(f"{_C}._resolve_aux_provider")
@patch(f"{_C}._load_uncompacted_rows")
class TestMaybeCompressSession(unittest.TestCase):
	# A history that comfortably exceeds the default threshold (76,800 tokens):
	# 40 rows × 8000 chars = 320,000 chars = 80,000 tokens.
	def _big_history(self):
		return _rows(40, 8000)

	def _provider(self, content="SUMMARY"):
		prov = MagicMock()
		prov.chat.return_value = {"content": content, "tool_calls": None, "usage": {}}
		return prov

	def test_skips_when_under_threshold(
		self, mock_load, mock_resolve, mock_latest, mock_persist, mock_extract
	):
		mock_load.return_value = _rows(2, 50)
		self.assertIsNone(maybe_compress_session("P", "S"))
		mock_resolve.assert_not_called()
		mock_persist.assert_not_called()

	def test_skips_when_no_uncompacted_rows(
		self, mock_load, mock_resolve, mock_latest, mock_persist, mock_extract
	):
		mock_load.return_value = []
		self.assertIsNone(maybe_compress_session("P", "S"))
		mock_persist.assert_not_called()

	def test_compresses_when_over_threshold(
		self, mock_load, mock_resolve, mock_latest, mock_persist, mock_extract
	):
		mock_load.return_value = self._big_history()
		prov = self._provider()
		mock_resolve.return_value = (prov, "aux-model")
		result = maybe_compress_session("P", "S")
		self.assertEqual(result, "CS-1")
		prov.chat.assert_called_once()
		# _persist_compaction(session_id, summary_text, middle_rows, model_label)
		args, _ = mock_persist.call_args
		self.assertEqual(args[0], "S")
		self.assertEqual(args[1], "SUMMARY")
		self.assertEqual(args[3], "aux-model")
		# Only the middle (not the protected tail) is folded.
		self.assertLess(len(args[2]), 40)

	def test_no_aux_model_skips_and_preserves_turns(
		self, mock_load, mock_resolve, mock_latest, mock_persist, mock_extract
	):
		mock_load.return_value = self._big_history()
		mock_resolve.return_value = (None, None)
		self.assertIsNone(maybe_compress_session("P", "S"))
		mock_persist.assert_not_called()

	def test_empty_summary_skips(self, mock_load, mock_resolve, mock_latest, mock_persist, mock_extract):
		mock_load.return_value = self._big_history()
		mock_resolve.return_value = (self._provider(content="   "), "m")
		self.assertIsNone(maybe_compress_session("P", "S"))
		mock_persist.assert_not_called()

	def test_aux_failure_skips(self, mock_load, mock_resolve, mock_latest, mock_persist, mock_extract):
		mock_load.return_value = self._big_history()
		prov = MagicMock()
		prov.chat.side_effect = LLMError("boom")
		mock_resolve.return_value = (prov, "m")
		self.assertIsNone(maybe_compress_session("P", "S"))
		mock_persist.assert_not_called()

	def test_previous_summary_is_fed_into_recompression(
		self, mock_load, mock_resolve, mock_latest, mock_persist, mock_extract
	):
		mock_load.return_value = self._big_history()
		mock_latest.return_value = "PRIOR"
		prov = self._provider()
		mock_resolve.return_value = (prov, "m")
		maybe_compress_session("P", "S")
		transcript = prov.chat.call_args.kwargs["messages"][1]["content"]
		self.assertIn("PRIOR", transcript)


# --- prompt_builder summary-aware assembly ----------------------------------


class TestPromptBuilderCompaction(unittest.TestCase):
	@patch("frappe.get_all")
	@patch("frappe.friday_core.llm.prompt_builder.latest_summary", return_value="THE SUMMARY")
	def test_summary_leads_history_with_prefix(self, _mock_latest, mock_get_all):
		mock_get_all.return_value = [
			{"direction": "inbound", "content": "recent question"},
			{"direction": "outbound", "content": "recent answer"},
		]
		msgs = _load_history("S", 10)
		# First entry is the reference-only summary as a system message.
		self.assertEqual(msgs[0]["role"], "system")
		self.assertTrue(msgs[0]["content"].startswith(SUMMARY_PREFIX))
		self.assertIn("THE SUMMARY", msgs[0]["content"])
		# Then the uncompacted tail, chronological.
		self.assertEqual(msgs[1], {"role": "user", "content": "recent question"})
		self.assertEqual(msgs[2], {"role": "assistant", "content": "recent answer"})

	@patch("frappe.get_all")
	@patch("frappe.friday_core.llm.prompt_builder.latest_summary", return_value=None)
	def test_no_summary_means_no_lead_message(self, _mock_latest, mock_get_all):
		mock_get_all.return_value = [{"direction": "inbound", "content": "hi"}]
		msgs = _load_history("S", 10)
		self.assertEqual(msgs, [{"role": "user", "content": "hi"}])

	@patch("frappe.get_all")
	@patch("frappe.friday_core.llm.prompt_builder.latest_summary", return_value=None)
	def test_history_query_excludes_compacted_rows(self, _mock_latest, mock_get_all):
		mock_get_all.return_value = []
		_load_history("S", 10)
		filters = mock_get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["compacted"], 0)


if __name__ == "__main__":
	unittest.main()
