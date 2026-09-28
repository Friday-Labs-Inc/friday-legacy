# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for pre-compaction fact extraction (design 80 step 1).

Before the middle turns are folded into a lossy summary, the agent mines them
for durable facts and persists them as governed `Agent Memory` rows (Hermes'
`on_pre_compress`, adapted). Covered here:

  - `_parse_extracted_facts` — pure parsing of the extractor's reply (robust to
    prose/fences, validation, trimming, cap).
  - `_extract_facts_before_compaction` — the write path, with frappe + provider
    mocked (dedup, best-effort failure, project tagging).
  - `maybe_compress_session` — that extraction fires before the fold, and only
    when the flag is on.

Mock-based, no DB — same shape as test_compression / test_worker_pool.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.llm import compression as C

# embed.enqueue_embed runs the REAL frappe.enqueue(enqueue_after_commit=True) — the
# handler module's frappe is mocked here, embed's is not — so without this patch a
# MagicMock doc name leaks into frappe.db.after_commit and poisons the next commit
# anywhere in the suite (a fresh-site CI run saw it kill eight unrelated modules).
_EMB = "frappe.friday_core.llm.embed.enqueue_embed"
_C = "frappe.friday_core.llm.compression"
_MEM = "frappe.friday_core.llm.memory.project_for_session"


class TestParseExtractedFacts(unittest.TestCase):
	"""Pure parsing — no DB."""

	def test_clean_json_array(self):
		out = C._parse_extracted_facts('[{"memory": "Loop hates serifs", "subject": "WI-1"}]')
		self.assertEqual(out, [{"memory": "Loop hates serifs", "subject": "WI-1"}])

	def test_strips_surrounding_prose_and_fences(self):
		text = 'Sure! Here you go:\n```json\n[{"memory": "Prefers SMS"}]\n```\nDone.'
		out = C._parse_extracted_facts(text)
		self.assertEqual(out, [{"memory": "Prefers SMS", "subject": ""}])

	def test_empty_array_and_nothing_to_save(self):
		self.assertEqual(C._parse_extracted_facts("[]"), [])
		self.assertEqual(C._parse_extracted_facts("Nothing to save."), [])
		self.assertEqual(C._parse_extracted_facts(""), [])

	def test_drops_items_without_memory(self):
		out = C._parse_extracted_facts('[{"subject": "x"}, {"memory": "  "}, {"memory": "keep"}]')
		self.assertEqual(out, [{"memory": "keep", "subject": ""}])

	def test_trims_lengths(self):
		long_mem = "m" * 800
		long_sub = "s" * 200
		out = C._parse_extracted_facts(f'[{{"memory": "{long_mem}", "subject": "{long_sub}"}}]')
		self.assertEqual(len(out[0]["memory"]), 500)
		self.assertEqual(len(out[0]["subject"]), 140)

	def test_caps_count(self):
		items = ",".join(f'{{"memory": "m{i}"}}' for i in range(50))
		out = C._parse_extracted_facts(f"[{items}]")
		self.assertEqual(len(out), C.MAX_EXTRACTED_FACTS)

	def test_non_list_json_returns_empty(self):
		self.assertEqual(C._parse_extracted_facts('{"memory": "x"}'), [])


class TestExtractFactsBeforeCompaction(unittest.TestCase):
	"""The write path — frappe + provider mocked."""

	def _provider(self, content):
		p = MagicMock()
		p.chat.return_value = {"content": content}
		return p

	def test_writes_new_memory_rows(self):
		provider = self._provider('[{"memory": "Loop hates serifs", "subject": "WI-1"}]')
		with patch(f"{_C}.frappe") as fr, patch(_EMB), patch(_MEM, return_value="PROJ-1"):
			fr.db.exists.return_value = False
			doc = MagicMock()
			fr.get_doc.return_value = doc
			written = C._extract_facts_before_compaction(
				"Friday", "sess-1", [{"direction": "inbound", "content": "..."}], provider
			)
		self.assertEqual(written, 1)
		args = fr.get_doc.call_args[0][0]
		self.assertEqual(args["doctype"], "Agent Memory")
		self.assertEqual(args["memory"], "Loop hates serifs")
		self.assertEqual(args["agent_profile"], "Friday")
		self.assertEqual(args["project"], "PROJ-1")
		self.assertEqual(args["source_session"], "sess-1")
		doc.insert.assert_called_once()

	def test_dedup_skips_existing(self):
		provider = self._provider('[{"memory": "already known"}]')
		with patch(f"{_C}.frappe") as fr, patch(_EMB), patch(_MEM, return_value=None):
			fr.db.exists.return_value = True  # the agent already holds it
			written = C._extract_facts_before_compaction("Friday", "s", [{"content": "x"}], provider)
		self.assertEqual(written, 0)
		fr.get_doc.assert_not_called()

	def test_provider_failure_is_best_effort(self):
		provider = MagicMock()
		provider.chat.side_effect = RuntimeError("model down")
		with patch(f"{_C}.frappe") as fr, patch(_EMB), patch(_MEM, return_value=None):
			written = C._extract_facts_before_compaction("Friday", "s", [{"content": "x"}], provider)
		self.assertEqual(written, 0)
		fr.logger.return_value.warning.assert_called()  # logged, not raised

	def test_no_facts_writes_nothing(self):
		provider = self._provider("[]")
		with patch(f"{_C}.frappe") as fr, patch(_EMB), patch(_MEM, return_value=None):
			written = C._extract_facts_before_compaction("Friday", "s", [{"content": "x"}], provider)
		self.assertEqual(written, 0)
		fr.get_doc.assert_not_called()


class TestCompactionFiresExtraction(unittest.TestCase):
	"""maybe_compress_session runs extraction before the fold, gated by the flag."""

	def _drive(self, extract_flag):
		rows = [{"name": str(i), "direction": "inbound", "content": "x" * 100} for i in range(50)]
		provider = MagicMock()
		provider.chat.return_value = {"content": "a summary"}
		with (
			patch(f"{_C}._load_uncompacted_rows", return_value=rows),
			patch(f"{_C}.should_compress", return_value=True),
			patch(f"{_C}._split_middle_tail", return_value=(rows[:40], rows[40:])),
			patch(f"{_C}._resolve_aux_provider", return_value=(provider, "m")),
			patch(f"{_C}.latest_summary", return_value=None),
			patch(f"{_C}._persist_compaction", return_value="CS-1") as persist,
			patch(f"{_C}._extract_facts_before_compaction", return_value=2) as extract,
			patch(f"{_C}.EXTRACT_FACTS_ON_COMPACTION", extract_flag),
		):
			result = C.maybe_compress_session("Friday", "sess-1")
		return result, extract, persist

	def test_extraction_fires_when_enabled(self):
		result, extract, persist = self._drive(extract_flag=True)
		self.assertEqual(result, "CS-1")
		extract.assert_called_once()
		# extraction receives the middle rows (the turns about to be folded)
		self.assertEqual(extract.call_args[0][0], "Friday")
		self.assertEqual(extract.call_args[0][1], "sess-1")
		persist.assert_called_once()

	def test_extraction_skipped_when_flag_off(self):
		result, extract, persist = self._drive(extract_flag=False)
		self.assertEqual(result, "CS-1")
		extract.assert_not_called()
		persist.assert_called_once()


if __name__ == "__main__":
	unittest.main()
