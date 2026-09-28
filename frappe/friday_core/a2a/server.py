# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""Friday as an A2A **server** — answer Agent2Agent calls as a GOVERNED agent (Design 92).

Friday already speaks MCP both ways and talks to humans over chat surfaces. This adds
the agent-to-agent surface: another agent framework (one that speaks Google's A2A
protocol) can discover Friday via its Agent Card and send it work over JSON-RPC.

WHY THIS IS DIFFERENT FROM AN ORDINARY A2A NODE
===============================================
The same reason the MCP *server* is different (mcp/server.py): an A2A `message/send`
here does NOT bypass governance. It runs as a designated Agent Profile through Friday's
real agent loop (`run_turn` → permission MATRIX → dispatcher → AUDIT log → approval
gate). The calling agent gets exactly that profile's access — no more — and every turn
it triggers is recorded. A governed, audited peer, not a raw one.

PROTOCOL (v1 subset)
====================
One guest-reachable POST endpoint, authenticated by a static token in the `X-A2A-Token`
header (a CUSTOM header, NOT `Authorization: Bearer` — Frappe's auth middleware rejects
an unknown Bearer before the endpoint runs; see `handle()`). Methods:

  * `message/send`  → run ONE governed turn; return the Task (sync: state=completed).
  * `tasks/get`     → poll a task by id (reads the lightweight Redis store).
  * `tasks/cancel`  → cancel a non-terminal task.

Scoped OUT (v1): streaming, push notifications, multi-turn task history, OUTBOUND
(Friday calling other agents — that becomes a governed `call-a2a-agent` skill later),
per-profile cards. Locked decisions: static-token auth, SYNC dispatch, taskId == the
turn's session id, one global card, Redis task store (see docs/design/92).

DESIGN
======
`handle_jsonrpc(...)` is the PURE core — parsed request in, response dict out, with
`run_fn` / `store` / `new_id` injected, so it is unit-testable with no Frappe, no DB,
no LLM. `handle()` is the thin whitelisted endpoint: auth + body I/O + a raw JSON-RPC
response body (mirrors mcp/server.py exactly).
"""

from __future__ import annotations

import json

import frappe
from frappe.friday_core.a2a.task_store import (
	CANCELED,
	COMPLETED,
	FAILED,
	TERMINAL,
	WORKING,
	A2ATaskStore,
)

# The A2A protocol revision we target.
PROTOCOL_VERSION = "0.2.0"

# JSON-RPC 2.0 error codes (subset) + one A2A-specific code for an unknown task.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603
_TASK_NOT_FOUND = -32001


# ---------------------------------------------------------------------------
# JSON-RPC envelope helpers
# ---------------------------------------------------------------------------


def _result(req_id, result: dict) -> dict:
	return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code: int, message: str) -> dict:
	return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Default (real) backend — injected so the core stays pure for tests
# ---------------------------------------------------------------------------


def _default_run_fn(profile: str, session_id: str, text: str) -> str:
	"""Run ONE governed agent turn and return its reply.

	`run_turn` IS the governed path: it drives the ReAct loop through the permission
	matrix, the dispatcher, the Execution Log audit, and the approval gate — the same
	primitive the gateway and the eval harness use. For a SYNC A2A turn we call it
	directly (no async Chat Message queue needed).
	"""
	from frappe.friday_core.agent_runner.runner import run_turn

	return run_turn(profile, session_id, text)


# ---------------------------------------------------------------------------
# Message / Task shaping
# ---------------------------------------------------------------------------


def _extract_text(message: dict | None) -> str:
	"""Pull plain text out of an A2A message: {role, parts: [{type:"text", text}]}."""
	parts = (message or {}).get("parts") or []
	chunks = [
		p.get("text", "")
		for p in parts
		if isinstance(p, dict) and p.get("type") in (None, "text") and p.get("text")
	]
	return "\n".join(chunks).strip()


def _task_view(task: dict) -> dict:
	"""Map a stored task record to the A2A Task object the client expects."""
	return {
		"id": task["id"],
		"status": {"state": task["state"]},
		"artifacts": task.get("artifacts") or [],
	}


# ---------------------------------------------------------------------------
# The pure protocol core
# ---------------------------------------------------------------------------


def handle_jsonrpc(body: dict, *, profile: str, run_fn=None, store=None, new_id=None) -> dict:
	"""Route one A2A JSON-RPC request and return the response dict.

	`run_fn(profile, session_id, text) -> reply`, `store` (an A2ATaskStore-like), and
	`new_id() -> str` are injectable for tests; defaults are the real governed turn +
	Redis store + a random hash. Never raises — a handler fault becomes a JSON-RPC error
	so the endpoint always replies.
	"""
	if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
		return _error(body.get("id") if isinstance(body, dict) else None, _INVALID_REQUEST, "Invalid Request")

	method = body["method"]
	req_id = body.get("id")
	params = body.get("params") or {}

	# Resolve backends only once we know the method needs them — a malformed or
	# unknown request shouldn't spin up a Redis client / run loop to be rejected.
	if method == "message/send":
		run_fn = run_fn or _default_run_fn
		store = store if store is not None else A2ATaskStore()
		new_id = new_id or (lambda: frappe.generate_hash(length=24))
		return _message_send(req_id, params, profile, run_fn, store, new_id)
	if method == "tasks/get":
		return _tasks_get(req_id, params, store if store is not None else A2ATaskStore())
	if method == "tasks/cancel":
		return _tasks_cancel(req_id, params, store if store is not None else A2ATaskStore())

	return _error(req_id, _METHOD_NOT_FOUND, f"Method not found: {method}")


def _message_send(req_id, params: dict, profile: str, run_fn, store, new_id) -> dict:
	"""Run a governed turn for an inbound message and return the resulting Task.

	SYNC (Decision 2): the reply comes back in this same response with state=completed.
	The session id IS the taskId (Decision 3): each A2A task is its own conversation.
	A turn that raises is surfaced as a Task with state=failed — NOT a JSON-RPC error,
	which the spec reserves for protocol faults (the caller still gets a task to poll).
	"""
	text = _extract_text(params.get("message"))
	if not text:
		return _error(req_id, _INVALID_REQUEST, "message/send requires a message with text parts")

	task_id = (params.get("taskId") or "").strip() or new_id()
	store.create(task_id, text)
	store.set_state(task_id, WORKING)

	try:
		reply = run_fn(profile, task_id, text)
	except Exception as exc:
		store.set_state(task_id, FAILED, error=type(exc).__name__)
		return _result(req_id, _task_view(store.get(task_id)))

	artifacts = [{"name": "reply", "parts": [{"type": "text", "text": reply or ""}]}]
	store.set_state(task_id, COMPLETED, artifacts=artifacts)
	return _result(req_id, _task_view(store.get(task_id)))


def _tasks_get(req_id, params: dict, store) -> dict:
	"""Return a task by id (the lightweight store), or a not-found JSON-RPC error."""
	task_id = (params.get("id") or "").strip()
	task = store.get(task_id) if task_id else None
	if task is None:
		return _error(req_id, _TASK_NOT_FOUND, f"Task not found: {task_id}")
	return _result(req_id, _task_view(task))


def _tasks_cancel(req_id, params: dict, store) -> dict:
	"""Cancel a non-terminal task. Idempotent: a finished task is returned unchanged.

	v1 turns complete synchronously, so by the time a caller cancels, the task is usually
	already terminal — cancel then just echoes the terminal state (the spec-correct,
	race-free behaviour) instead of pretending it stopped something.
	"""
	task_id = (params.get("id") or "").strip()
	task = store.get(task_id) if task_id else None
	if task is None:
		return _error(req_id, _TASK_NOT_FOUND, f"Task not found: {task_id}")
	if task["state"] in TERMINAL:
		return _result(req_id, _task_view(task))
	store.set_state(task_id, CANCELED)
	return _result(req_id, _task_view(store.get(task_id)))


# ---------------------------------------------------------------------------
# The whitelisted HTTP endpoint
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True, methods=["POST"])
def handle():
	"""POST /api/method/frappe.friday_core.a2a.server.handle — the A2A server endpoint.

	Guest-reachable; a static token in the `X-A2A-Token` header IS the authentication.
	Reads a JSON-RPC request, runs `handle_jsonrpc`, and writes the response as a RAW
	body (not Frappe's `{"message": ...}` wrapper — A2A clients expect the body to BE
	the JSON-RPC message).

	WHY `X-A2A-Token` AND NOT `Authorization: Bearer`: Frappe's `validate_auth` rejects
	any `Authorization: Bearer <token>` it can't validate as OAuth/API-key with a 401
	BEFORE a whitelisted function runs (frappe/auth.py). A custom header sidesteps that —
	identical to the MCP server's `X-MCP-Token`.
	"""
	config = _load_config()
	if not config.get("enabled"):
		return _raw(_error(None, _INVALID_REQUEST, "A2A server is disabled."), status=404)

	if not _authorized(config):
		return _raw(_error(None, _INVALID_REQUEST, "Unauthorized."), status=401)

	raw = frappe.request.get_data() if frappe.request else b"{}"
	try:
		body = json.loads(raw or b"{}")
	except (ValueError, TypeError):
		return _raw(_error(None, _PARSE_ERROR, "Parse error."), status=400)

	response = handle_jsonrpc(body, profile=config["profile"])
	return _raw(response, status=200)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def agent_card():
	"""GET the A2A Agent Card as a raw JSON body (the discovery document).

	Served unauthenticated (the A2A spec requires the card to be public). The canonical
	A2A path is `/.well-known/agent.json` (wired separately); this whitelisted method is
	the always-available builder behind it and the testable entry point. Returns 404 when
	the server is disabled so a disabled node advertises nothing.
	"""
	from frappe.friday_core.a2a.card import build_agent_card

	config = _load_config()
	if not config.get("enabled"):
		return _raw({"error": "A2A server is disabled."}, status=404)

	base_url = frappe.utils.get_url()
	card = build_agent_card(config["profile"], base_url=base_url)
	return _raw(card, status=200)


def _authorized(config: dict) -> bool:
	"""Constant-time check of the A2A token from the `X-A2A-Token` request header.

	Returns False when no token is configured (a misconfigured server can't be reached
	even if enabled) or the header is missing/wrong.
	"""
	import hmac

	provided = (frappe.get_request_header("X-A2A-Token") or "").strip()
	expected = config.get("token") or ""
	return bool(expected) and hmac.compare_digest(provided, expected)


def _raw(payload: dict, *, status: int):
	"""Send `payload` as a raw JSON body with `status` (bypasses Frappe's message wrapper)."""
	from werkzeug.wrappers import Response

	return Response(json.dumps(payload), status=status, content_type="application/json")


def _load_config() -> dict:
	"""Read the A2A Server Settings single: enabled, auth token, exposed profile."""
	doc = frappe.get_cached_doc("A2A Server Settings")
	return {
		"enabled": bool(doc.enabled),
		"token": doc.get_password("auth_token") if doc.auth_token else "",
		"profile": doc.agent_profile or "",
	}
