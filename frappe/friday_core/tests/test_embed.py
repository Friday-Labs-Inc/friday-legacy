# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the embedding layer (design 80 step 2b-1).

Covers backend dispatch (local default / minimax optional), the Redis cache
(compute once, reuse), best-effort failure (returns None, never raises), and the
per-backend dimension. Backends + Redis + config are mocked — no torch, no live
API, no DB.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.llm import embed as E

_E = "frappe.friday_core.llm.embed"


class TestEmbeddingDim(unittest.TestCase):
	def test_local_dim(self):
		self.assertEqual(E.embedding_dim("local"), E.LOCAL_DIM)

	def test_minimax_dim(self):
		self.assertEqual(E.embedding_dim("minimax"), E.MINIMAX_DIM)


class TestGetEmbedding(unittest.TestCase):
	def test_empty_text_returns_none(self):
		self.assertIsNone(E.get_embedding("   "))
		self.assertIsNone(E.get_embedding(""))

	def test_cache_hit_skips_backend(self):
		with (
			patch(f"{_E}._backend", return_value="local"),
			patch(f"{_E}._cache_get", return_value=[0.1, 0.2]),
			patch(f"{_E}._embed_local") as local,
		):
			out = E.get_embedding("hello")
		self.assertEqual(out, [0.1, 0.2])
		local.assert_not_called()  # served from cache

	def test_cache_miss_computes_and_caches(self):
		with (
			patch(f"{_E}._backend", return_value="local"),
			patch(f"{_E}._cache_get", return_value=None),
			patch(f"{_E}._embed_local", return_value=[0.3, 0.4]) as local,
			patch(f"{_E}._cache_put") as put,
		):
			out = E.get_embedding("hello")
		self.assertEqual(out, [0.3, 0.4])
		local.assert_called_once()
		put.assert_called_once()

	def test_dispatches_to_minimax_backend(self):
		with (
			patch(f"{_E}._backend", return_value="minimax"),
			patch(f"{_E}._cache_get", return_value=None),
			patch(f"{_E}._embed_minimax", return_value=[1.0]) as mm,
			patch(f"{_E}._embed_local") as local,
			patch(f"{_E}._cache_put"),
		):
			out = E.get_embedding("hi")
		self.assertEqual(out, [1.0])
		mm.assert_called_once()
		local.assert_not_called()

	def test_backend_failure_returns_none(self):
		with (
			patch(f"{_E}._backend", return_value="local"),
			patch(f"{_E}._cache_get", return_value=None),
			patch(f"{_E}._embed_local", side_effect=ImportError("no sentence-transformers")),
			patch(f"{_E}.frappe") as fr,
		):
			out = E.get_embedding("hello")
		self.assertIsNone(out)
		fr.logger.return_value.warning.assert_called()  # logged, not raised

	def test_empty_vector_not_cached(self):
		with (
			patch(f"{_E}._backend", return_value="local"),
			patch(f"{_E}._cache_get", return_value=None),
			patch(f"{_E}._embed_local", return_value=[]),
			patch(f"{_E}._cache_put") as put,
		):
			out = E.get_embedding("hello")
		self.assertEqual(out, [])
		put.assert_not_called()


class TestCacheKey(unittest.TestCase):
	def test_key_is_backend_scoped_and_stable(self):
		k_local = E._cache_key("local", "same text")
		k_local2 = E._cache_key("local", "same text")
		k_minimax = E._cache_key("minimax", "same text")
		self.assertEqual(k_local, k_local2)  # stable
		self.assertNotEqual(k_local, k_minimax)  # backend-scoped
		self.assertTrue(k_local.startswith(E._CACHE_PREFIX))


class TestMinimaxBackendShape(unittest.TestCase):
	"""_embed_minimax encodes the VERIFIED MiniMax request/response shape."""

	def _row(self):
		row = MagicMock()
		row.base_url = "https://api.minimax.io"
		row.get_password.return_value = "sk-test"
		return row

	def test_parses_vectors_key(self):
		with patch(f"{_E}.frappe") as fr, patch("requests.post") as post:
			fr.conf.get.return_value = None
			fr.get_doc.return_value = self._row()
			post.return_value.json.return_value = {
				"vectors": [[0.1, 0.2, 0.3]],
				"base_resp": {"status_code": 0},
			}
			out = E._embed_minimax("hello")
		self.assertEqual(out, [0.1, 0.2, 0.3])
		# sent the MiniMax-native body, not OpenAI's
		body = post.call_args.kwargs["json"]
		self.assertIn("texts", body)
		self.assertEqual(body["model"], E.MINIMAX_MODEL)

	def test_rate_limit_raises(self):
		with patch(f"{_E}.frappe") as fr, patch("requests.post") as post:
			fr.conf.get.return_value = None
			fr.get_doc.return_value = self._row()
			post.return_value.json.return_value = {
				"vectors": None,
				"base_resp": {"status_code": 1002, "status_msg": "rate limit exceeded(RPM)"},
			}
			with self.assertRaises(RuntimeError):
				E._embed_minimax("hello")


class TestStoreEmbedding(unittest.TestCase):
	def test_noop_on_non_postgres(self):
		with patch(f"{_E}.frappe") as fr:
			fr.db.db_type = "mariadb"
			E.store_embedding("MEM-1", [0.1, 0.2])
		fr.db.sql.assert_not_called()

	def test_noop_on_empty_vector(self):
		with patch(f"{_E}.frappe") as fr:
			fr.db.db_type = "postgres"
			E.store_embedding("MEM-1", [])
		fr.db.sql.assert_not_called()

	def test_upserts_vector_on_postgres(self):
		with patch(f"{_E}.frappe") as fr:
			fr.db.db_type = "postgres"
			E.store_embedding("MEM-1", [0.1, 0.2, 0.3], model="local:test")
		sql, params = fr.db.sql.call_args[0][0], fr.db.sql.call_args[0][1]
		self.assertIn("ON CONFLICT", sql)
		self.assertIn("::vector", sql)
		self.assertEqual(params["n"], "MEM-1")
		self.assertEqual(params["v"], "[0.1, 0.2, 0.3]")


class TestEmbedMemoryJob(unittest.TestCase):
	def test_embeds_and_stores(self):
		with (
			patch(f"{_E}.frappe") as fr,
			patch(f"{_E}.get_embedding", return_value=[0.5]) as ge,
			patch(f"{_E}.store_embedding") as store,
		):
			fr.db.get_value.return_value = {"memory": "loop hates serifs", "subject": "WI-1"}
			E.embed_memory("MEM-1")
		ge.assert_called_once()
		store.assert_called_once_with("MEM-1", [0.5])

	def test_no_embedding_skips_store(self):
		with (
			patch(f"{_E}.frappe") as fr,
			patch(f"{_E}.get_embedding", return_value=None),
			patch(f"{_E}.store_embedding") as store,
		):
			fr.db.get_value.return_value = {"memory": "x", "subject": ""}
			E.embed_memory("MEM-1")
		store.assert_not_called()

	def test_missing_row_is_noop(self):
		with (
			patch(f"{_E}.frappe") as fr,
			patch(f"{_E}.get_embedding") as ge,
			patch(f"{_E}.store_embedding") as store,
		):
			fr.db.get_value.return_value = None
			E.embed_memory("GONE")
		ge.assert_not_called()
		store.assert_not_called()


class TestEnqueueEmbed(unittest.TestCase):
	def test_enqueues_after_commit(self):
		with patch(f"{_E}.frappe") as fr:
			E.enqueue_embed("MEM-1")
		args, kwargs = fr.enqueue.call_args
		self.assertEqual(args[0], "frappe.friday_core.llm.embed.embed_memory")
		self.assertEqual(kwargs["memory_name"], "MEM-1")
		self.assertTrue(kwargs["enqueue_after_commit"])


class TestBackfill(unittest.TestCase):
	def test_noop_on_non_postgres(self):
		with patch(f"{_E}.frappe") as fr:
			fr.db.db_type = "mariadb"
			self.assertEqual(E.backfill_embeddings(), 0)

	def test_queues_rows_without_embeddings_respecting_limit(self):
		with patch(f"{_E}.frappe") as fr, patch(f"{_E}.enqueue_embed") as enq:
			fr.db.db_type = "postgres"
			fr.db.sql.return_value = [{"name": f"MEM-{i}"} for i in range(5)]
			queued = E.backfill_embeddings(limit=3)
		self.assertEqual(queued, 3)
		self.assertEqual(enq.call_count, 3)


class _Vec:
	"""Stand-in for a numpy embedding vector (has .tolist())."""

	def __init__(self, v):
		self._v = v

	def tolist(self):
		return self._v


class TestLocalBackendDispatch(unittest.TestCase):
	"""_embed_local dispatches on the cached model kind (fastembed | st)."""

	def test_fastembed_path(self):
		model = MagicMock()
		model.embed.return_value = [_Vec([0.1, 0.2, 0.3])]  # fastembed yields vectors
		with patch(f"{_E}._local_model", return_value=(model, "fastembed")):
			out = E._embed_local("hello")
		self.assertEqual(out, [0.1, 0.2, 0.3])
		model.embed.assert_called_once_with(["hello"])

	def test_sentence_transformers_path(self):
		model = MagicMock()
		model.encode.return_value = _Vec([0.4, 0.5])
		with patch(f"{_E}._local_model", return_value=(model, "st")):
			out = E._embed_local("hello")
		self.assertEqual(out, [0.4, 0.5])
		model.encode.assert_called_once()

	def test_local_model_default_is_bge_384(self):
		# The shadow table is sized to LOCAL_DIM; keep them in lockstep.
		self.assertEqual(E.LOCAL_DIM, 384)
		self.assertEqual(E.LOCAL_MODEL, "BAAI/bge-small-en-v1.5")


if __name__ == "__main__":
	unittest.main()
