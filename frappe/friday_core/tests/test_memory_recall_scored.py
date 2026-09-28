# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for scored memory recall (design 80 step 2a).

Recall now ranks memories by relevance-to-the-current-message (Postgres
full-text) blended with recency, instead of a newest-first dump that drops
old-but-relevant facts past the token budget. Covered here:

  - `_format_block` — budget cap + subject rendering (pure-ish).
  - `_recall_scored` — the Postgres FTS+recency query path (frappe.db mocked).
  - `_recall_recency` — the safe fallback (newest-first, project-scoped).
  - `recall_block` — routing: scored on a Postgres site with a query, recency
    otherwise, and recency fallback when the scored path errors.

Mock-based, no DB — same shape as the other friday_core unit tests.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.llm import memory as M

_M = "frappe.friday_core.llm.memory"


class TestFormatBlock(unittest.TestCase):
	def test_renders_subject_and_plain_lines(self):
		block = M._format_block(
			[{"memory": "no serifs", "subject": "WI-1"}, {"memory": "ships fridays"}], 2000
		)
		self.assertIn("- [WI-1] no serifs", block)
		self.assertIn("- ships fridays", block)
		self.assertIn("<memory-context>", block)  # fenced

	def test_budget_caps(self):
		rows = [{"memory": "x" * 100} for _ in range(50)]
		# Each line is "- " + 100 chars = 102 chars. Budget 30 tokens × 4 = 120
		# chars fits exactly one line (102) but not two (204) → stops at one.
		block = M._format_block(rows, 30)
		self.assertEqual(block.count("- "), 1)

	def test_empty_returns_none(self):
		self.assertIsNone(M._format_block([], 2000))
		self.assertIsNone(M._format_block([{"memory": "  "}], 2000))


class TestRecallScored(unittest.TestCase):
	def test_runs_fts_query_and_formats(self):
		with patch(f"{_M}.frappe") as fr:
			fr.db.sql.return_value = [{"memory": "loop hates serifs", "subject": "WI-1"}]
			block = M._recall_scored("Friday", None, 2000, "what fonts does loop like")
		self.assertIn("loop hates serifs", block)
		sql = fr.db.sql.call_args[0][0]
		self.assertIn("ts_rank_cd", sql)
		self.assertIn("plainto_tsquery", sql)

	def test_project_adds_scope_clause(self):
		with patch(f"{_M}.frappe") as fr:
			fr.db.sql.return_value = []
			M._recall_scored("Friday", "PROJ-1", 2000, "q")
		sql, params = fr.db.sql.call_args[0][0], fr.db.sql.call_args[0][1]
		self.assertIn("m.project", sql)
		self.assertEqual(params["project"], "PROJ-1")

	def test_no_project_has_no_scope_clause(self):
		with patch(f"{_M}.frappe") as fr:
			fr.db.sql.return_value = []
			M._recall_scored("Friday", None, 2000, "q")
		sql = fr.db.sql.call_args[0][0]
		self.assertNotIn("m.project =", sql)

	def test_no_rows_returns_none(self):
		with patch(f"{_M}.frappe") as fr:
			fr.db.sql.return_value = []
			self.assertIsNone(M._recall_scored("Friday", None, 2000, "q"))

	def test_uses_or_tsquery_idiom(self):
		# plainto_tsquery ANDs all terms (a memory must contain EVERY word) — too
		# strict for relevance. We swap & for | so any matching term scores.
		with patch(f"{_M}.frappe") as fr:
			fr.db.sql.return_value = []
			M._recall_scored("Friday", None, 2000, "which fonts for the brand")
		sql = fr.db.sql.call_args[0][0]
		self.assertIn("replace(plainto_tsquery", sql)
		self.assertIn("'&', '|'", sql)


class TestRecallRecency(unittest.TestCase):
	def test_project_filters_other_projects(self):
		with patch(f"{_M}.frappe") as fr:
			fr.get_all.return_value = [
				{"memory": "keep-global", "subject": "", "project": None},
				{"memory": "keep-mine", "subject": "", "project": "PROJ-1"},
				{"memory": "drop-theirs", "subject": "", "project": "PROJ-2"},
			]
			block = M._recall_recency("Friday", "PROJ-1", 2000)
		self.assertIn("keep-global", block)
		self.assertIn("keep-mine", block)
		self.assertNotIn("drop-theirs", block)

	def test_no_rows_returns_none(self):
		with patch(f"{_M}.frappe") as fr:
			fr.get_all.return_value = []
			self.assertIsNone(M._recall_recency("Friday", None, 2000))


class TestRecallBlockRouting(unittest.TestCase):
	def test_query_on_postgres_uses_scored(self):
		with (
			patch(f"{_M}.frappe") as fr,
			patch(f"{_M}._recall_scored", return_value="SCORED") as sc,
			patch(f"{_M}._recall_recency", return_value="REC") as rec,
		):
			fr.db.db_type = "postgres"
			self.assertEqual(M.recall_block("Friday", query="hello"), "SCORED")
		sc.assert_called_once()
		rec.assert_not_called()

	def test_scored_error_falls_back_to_recency(self):
		with (
			patch(f"{_M}.frappe") as fr,
			patch(f"{_M}._recall_scored", side_effect=RuntimeError("bad sql")),
			patch(f"{_M}._recall_recency", return_value="REC") as rec,
		):
			fr.db.db_type = "postgres"
			self.assertEqual(M.recall_block("Friday", query="hello"), "REC")
		rec.assert_called_once()

	def test_no_query_uses_recency(self):
		with (
			patch(f"{_M}.frappe") as fr,
			patch(f"{_M}._recall_scored") as sc,
			patch(f"{_M}._recall_recency", return_value="REC"),
		):
			fr.db.db_type = "postgres"
			self.assertEqual(M.recall_block("Friday"), "REC")
		sc.assert_not_called()

	def test_non_postgres_uses_recency(self):
		with (
			patch(f"{_M}.frappe") as fr,
			patch(f"{_M}._recall_scored") as sc,
			patch(f"{_M}._recall_recency", return_value="REC"),
		):
			fr.db.db_type = "mariadb"
			self.assertEqual(M.recall_block("Friday", query="hello"), "REC")
		sc.assert_not_called()


class TestRecallScoredVectorBlend(unittest.TestCase):
	"""Step 2b-2: the semantic term appears only when a query embedding exists."""

	_EMB = "frappe.friday_core.llm.embed.get_embedding"

	def test_vector_term_when_embedding_available(self):
		with patch(f"{_M}.frappe") as fr, patch(self._EMB, return_value=[0.1, 0.2, 0.3]):
			fr.db.sql.return_value = [{"memory": "x", "subject": ""}]
			M._recall_scored("Friday", None, 2000, "fonts")
		sql = fr.db.sql.call_args[0][0]
		params = fr.db.sql.call_args[0][1]
		self.assertIn("<=>", sql)  # pgvector cosine distance
		self.assertIn("Agent Memory Embedding", sql)  # LEFT JOIN the shadow table
		self.assertIn("qvec", params)

	def test_keyword_only_when_no_embedding(self):
		with patch(f"{_M}.frappe") as fr, patch(self._EMB, return_value=None):
			fr.db.sql.return_value = []
			M._recall_scored("Friday", None, 2000, "fonts")
		sql = fr.db.sql.call_args[0][0]
		self.assertNotIn("<=>", sql)  # no semantic term → 2a behaviour
		self.assertIn("ts_rank_cd", sql)


if __name__ == "__main__":
	unittest.main()
