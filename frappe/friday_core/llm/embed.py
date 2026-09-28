# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Embedding generation for semantic memory recall (design 80, step 2b).

PLAIN ENGLISH
=============

To rank memories by *meaning* (not just keywords), each memory's text is turned
into a vector of numbers — an "embedding". This module produces those vectors.

PLUGGABLE BACKEND — local by default
====================================
- "local" (DEFAULT): a small on-box model (sentence-transformers). Nothing
  leaves the tenant, no rate limit, free. This is the governance-consistent
  default. The library is an OPTIONAL heavy dependency — if it isn't installed,
  embedding simply returns None (see "best-effort" below).
- "minimax": MiniMax `embo-01` over the provider you already configure. Verified
  request shape (`{"model","texts","type"}`) and response (`{"vectors": [...]}`);
  RPM-rate-limited, so it can fail — handled as a best-effort miss.

Pick the backend with the `friday_embedding_backend` site-config key
("local" | "minimax"); blank = local. No migration needed.

BEST-EFFORT (load-bearing)
==========================
`get_embedding` NEVER raises. If embeddings are unavailable for ANY reason
(model not installed, API rate-limited or errored, bad config) it returns None,
and the caller (memory recall) falls back to keyword + recency ranking
(step 2a). Semantic recall is an enhancement layered on top — never a hard
dependency on the conversation path.

REDIS CACHE
===========
Text → vector is stable, so successful embeddings are cached in Redis (keyed by
backend + text hash). The same memory text is never re-embedded — the main win
for the rate-limited MiniMax path, and a latency win for the local one.
"""

from __future__ import annotations

import hashlib
import json

import frappe

# --- Backend config ---------------------------------------------------------

DEFAULT_BACKEND = "local"

LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
LOCAL_DIM = 384

MINIMAX_MODEL = "embo-01"
MINIMAX_DIM = 1536

_CACHE_PREFIX = "friday:embed:"
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # a week — text→vector is stable

# Process-local singleton for the (expensive to load) local model.
_LOCAL_MODEL_CACHE: dict = {}


def _backend() -> str:
	"""The active embedding backend — `friday_embedding_backend` site config, else local."""
	try:
		return (frappe.conf.get("friday_embedding_backend") or DEFAULT_BACKEND).strip().lower()
	except Exception:
		return DEFAULT_BACKEND


def embedding_dim(backend: "str | None" = None) -> int:
	"""Vector dimension for a backend. The pgvector column (step 2b-2) is sized to this."""
	return MINIMAX_DIM if (backend or _backend()) == "minimax" else LOCAL_DIM


def get_embedding(text: str) -> "list[float] | None":
	"""Return the embedding vector for ``text``, or None if unavailable.

	NEVER raises. Checks the Redis cache first; on a miss, embeds via the active
	backend and caches the result. Any failure (missing local model, API
	rate-limit/error, bad config) logs and returns None so recall degrades to
	keyword + recency rather than breaking the turn.
	"""
	text = (text or "").strip()
	if not text:
		return None

	backend = _backend()
	cached = _cache_get(backend, text)
	if cached is not None:
		return cached

	try:
		vec = _embed_minimax(text) if backend == "minimax" else _embed_local(text)
	except Exception:
		frappe.logger("friday.embed").warning(
			f"embedding failed (backend={backend}); semantic recall degrades to "
			f"keyword+recency for this query",
			exc_info=True,
		)
		return None

	if vec:
		_cache_put(backend, text, vec)
	return vec


# --- Backends ---------------------------------------------------------------


def _embed_local(text: str) -> "list[float]":
	"""On-box embedding, torch-free via fastembed (ONNX), or sentence-transformers.

	Prefers ``fastembed`` (onnxruntime + tokenizers, no torch — works on Python
	3.14 where torch has no wheels yet); falls back to ``sentence-transformers``
	if that's what's installed. If neither is present it raises ImportError,
	caught by get_embedding → returns None → recall falls back to keyword+recency.
	Install to activate: ``./env/bin/pip install fastembed``.
	"""
	model, kind = _local_model()
	if kind == "fastembed":
		# fastembed yields one normalised numpy vector per input text.
		return next(iter(model.embed([text]))).tolist()
	# sentence-transformers: normalize so cosine == dot product.
	return model.encode(text, normalize_embeddings=True).tolist()


def _local_model():
	"""Load (once) and cache the local embedding model + its kind ('fastembed'|'st')."""
	if "model" not in _LOCAL_MODEL_CACHE:
		try:
			from fastembed import TextEmbedding

			_LOCAL_MODEL_CACHE["model"] = (TextEmbedding(model_name=LOCAL_MODEL), "fastembed")
		except ImportError:
			from sentence_transformers import SentenceTransformer

			_LOCAL_MODEL_CACHE["model"] = (SentenceTransformer(LOCAL_MODEL), "st")
	return _LOCAL_MODEL_CACHE["model"]


def _embed_minimax(text: str) -> "list[float]":
	"""MiniMax `embo-01` embedding over the configured provider row.

	Uses the VERIFIED request/response shape: POST ``{base}/v1/embeddings`` with
	``{"model","texts","type"}`` and Bearer auth; vectors come back under
	``"vectors"`` and errors under ``base_resp.status_code`` (0 = ok). A non-zero
	status (e.g. 1002 RPM rate limit) raises → get_embedding returns None.
	"""
	import requests

	name = frappe.conf.get("friday_embedding_provider") or "Minimax"
	row = frappe.get_doc("LLM Provider", name)
	base = (row.base_url or "https://api.minimax.io").rstrip("/")
	key = row.get_password("api_key")
	resp = requests.post(
		f"{base}/v1/embeddings",
		headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
		json={"model": MINIMAX_MODEL, "texts": [text], "type": "db"},
		timeout=30,
	)
	body = resp.json()
	base_resp = body.get("base_resp") or {}
	if base_resp.get("status_code") not in (0, None):
		raise RuntimeError(
			f"minimax embeddings error {base_resp.get('status_code')}: {base_resp.get('status_msg')}"
		)
	vectors = body.get("vectors")
	if not vectors:
		raise RuntimeError("minimax embeddings returned no vectors")
	return vectors[0]


# --- Redis cache ------------------------------------------------------------


def _cache_key(backend: str, text: str) -> str:
	digest = hashlib.sha256(f"{backend}:{text}".encode()).hexdigest()
	return f"{_CACHE_PREFIX}{digest}"


def _cache_get(backend: str, text: str) -> "list[float] | None":
	try:
		raw = frappe.cache().get_value(_cache_key(backend, text))
		return json.loads(raw) if raw else None
	except Exception:
		return None


def _cache_put(backend: str, text: str, vec: "list[float]") -> None:
	try:
		frappe.cache().set_value(
			_cache_key(backend, text), json.dumps(vec), expires_in_sec=_CACHE_TTL_SECONDS
		)
	except Exception:
		pass


# --- Vector storage + async write path (design 80 step 2b-2) ----------------


def _model_label() -> str:
	"""A short provenance label for a stored embedding (backend:model)."""
	b = _backend()
	return f"{b}:{MINIMAX_MODEL if b == 'minimax' else LOCAL_MODEL}"


def store_embedding(name: str, vector: "list[float]", model: "str | None" = None) -> None:
	"""Upsert one memory's embedding into the pgvector shadow table.

	Postgres-only; no-op on a missing vector. The vector is passed as a bound
	param cast to ``::vector`` (no string interpolation of the values).
	"""
	if frappe.db.db_type != "postgres" or not vector:
		return
	frappe.db.sql(
		"INSERT INTO `tabAgent Memory Embedding` (name, embedding, model, embedded_at) "
		"VALUES (%(n)s, %(v)s::vector, %(m)s, NOW()) "
		"ON CONFLICT (name) DO UPDATE SET "
		"embedding = EXCLUDED.embedding, model = EXCLUDED.model, embedded_at = NOW()",
		{"n": name, "v": json.dumps(vector), "m": model or _model_label()},
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job, own txn


def embed_memory(memory_name: str) -> None:
	"""RQ job: embed one Agent Memory row and store the vector. Best-effort.

	Reads the row's subject+memory text, embeds it (returns None → skip when no
	model/backend is available), and upserts the vector. Never raises — a failed
	embedding just means that memory has no semantic vector yet and is recalled
	by keyword+recency until re-embedded.
	"""
	try:
		row = frappe.db.get_value("Agent Memory", memory_name, ["memory", "subject"], as_dict=True)
		if not row:
			return
		text = f"{(row.get('subject') or '').strip()} {(row.get('memory') or '').strip()}".strip()
		vec = get_embedding(text)
		if vec:
			store_embedding(memory_name, vec)
	except Exception:
		frappe.logger("friday.embed").warning(f"embed_memory failed for {memory_name!r}", exc_info=True)


def enqueue_embed(memory_name: str) -> None:
	"""Queue an embedding job for a just-written memory (after the insert commits)."""
	try:
		frappe.enqueue(
			"frappe.friday_core.llm.embed.embed_memory",
			memory_name=memory_name,
			queue="short",
			enqueue_after_commit=True,
		)
	except Exception:
		frappe.logger("friday.embed").warning("enqueue_embed failed", exc_info=True)


def backfill_embeddings(limit: "int | None" = None) -> int:
	"""Queue embedding jobs for every Active memory that has no vector yet.

	Run this once after enabling embeddings (e.g. installing the local model):
	``bench --site <site> execute frappe.friday_core.llm.embed.backfill_embeddings``.
	Returns the number of jobs queued. Postgres-only.
	"""
	if frappe.db.db_type != "postgres":
		return 0
	rows = frappe.db.sql(
		"SELECT m.name AS name FROM `tabAgent Memory` m "
		"LEFT JOIN `tabAgent Memory Embedding` e ON e.name = m.name "
		"WHERE m.status = 'Active' AND e.name IS NULL "
		"ORDER BY m.creation DESC",
		as_dict=True,
	)
	queued = 0
	for r in rows:
		enqueue_embed(r["name"])
		queued += 1
		if limit and queued >= limit:
			break
	return queued
