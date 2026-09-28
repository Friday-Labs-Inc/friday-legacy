# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""Unit tests for the A2A server (Friday as a governed Agent2Agent node, Design 92).

DB-free: `handle_jsonrpc` is the pure protocol core with injectable `run_fn` / `store` /
`new_id`, so these exercise the JSON-RPC routing, the task lifecycle, the Agent Card
shape, and — the whole point — that `message/send` runs through the GOVERNED turn
(`run_turn`) as the exposed profile with the taskId as the session id. No Frappe site,
no DB, no LLM, no HTTP.
"""

from __future__ import annotations

import unittest
from unittest import mock

from frappe.friday_core.a2a import card as card_mod
from frappe.friday_core.a2a import server
from frappe.friday_core.a2a import task_store as ts


class _FakeCache:
	"""A dict-backed stand-in for frappe.cache() (ignores TTL)."""

	def __init__(self):
		self.d = {}

	def get_value(self, k):
		return self.d.get(k)

	def set_value(self, k, v, expires_in_sec=None):
		self.d[k] = v


def _store():
	return ts.A2ATaskStore(cache=_FakeCache())


# ---------------------------------------------------------------------------
# Task store
# ---------------------------------------------------------------------------


class TestTaskStore(unittest.TestCase):
	def test_create_starts_submitted(self):
		s = _store()
		rec = s.create("T1", "hello")
		self.assertEqual(rec["state"], ts.SUBMITTED)
		self.assertEqual(rec["message"], "hello")
		self.assertEqual(s.get("T1")["state"], ts.SUBMITTED)

	def test_set_state_attaches_artifacts(self):
		s = _store()
		s.create("T1", "hi")
		s.set_state("T1", ts.COMPLETED, artifacts=[{"name": "reply"}])
		rec = s.get("T1")
		self.assertEqual(rec["state"], ts.COMPLETED)
		self.assertEqual(rec["artifacts"], [{"name": "reply"}])

	def test_unknown_task_is_none(self):
		self.assertIsNone(_store().get("nope"))

	def test_set_state_on_unknown_is_none(self):
		self.assertIsNone(_store().set_state("nope", ts.WORKING))


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------


class TestAgentCard(unittest.TestCase):
	def test_card_advertises_the_profiles_skills(self):
		def list_fn(profile):
			self.assertEqual(profile, "Friday")
			return [{"name": "list-projects", "description": "List projects", "tags": ["projects"]}]

		c = card_mod.build_agent_card("Friday", base_url="https://x.test/", list_fn=list_fn)
		self.assertEqual(c["skills"][0]["id"], "list-projects")
		self.assertEqual(c["skills"][0]["tags"], ["projects"])
		# Endpoint URL is the handle path on the given origin (no double slash).
		self.assertEqual(c["url"], "https://x.test/api/method/frappe.friday_core.a2a.server.handle")
		# v1 scope + honest auth scheme.
		self.assertFalse(c["capabilities"]["streaming"])
		self.assertEqual(c["authentication"]["schemes"], ["x-a2a-token"])

	def test_card_with_no_skills_is_still_valid(self):
		c = card_mod.build_agent_card("P", base_url="https://x.test", list_fn=lambda p: [])
		self.assertEqual(c["skills"], [])
		self.assertEqual(c["version"], card_mod.CARD_VERSION)


# ---------------------------------------------------------------------------
# message/send — the governed turn
# ---------------------------------------------------------------------------


def _msg(text):
	return {"role": "user", "parts": [{"type": "text", "text": text}]}


class TestMessageSendIsGoverned(unittest.TestCase):
	def test_runs_governed_turn_as_profile_with_taskid_as_session(self):
		seen = {}

		def run_fn(profile, session_id, text):
			seen.update(profile=profile, session_id=session_id, text=text)
			return "Three options: A, B, C."

		resp = server.handle_jsonrpc(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "message/send",
				"params": {"message": _msg("name my brand")},
			},
			profile="BrandAgent",
			run_fn=run_fn,
			store=_store(),
			new_id=lambda: "TASK-XYZ",
		)

		# Routed through the governed primitive AS the exposed profile, session == taskId.
		self.assertEqual(seen["profile"], "BrandAgent")
		self.assertEqual(seen["session_id"], "TASK-XYZ")
		self.assertEqual(seen["text"], "name my brand")
		# Sync result: a completed task carrying the reply as an artifact.
		result = resp["result"]
		self.assertEqual(result["id"], "TASK-XYZ")
		self.assertEqual(result["status"]["state"], ts.COMPLETED)
		self.assertEqual(result["artifacts"][0]["parts"][0]["text"], "Three options: A, B, C.")

	def test_client_supplied_taskid_is_used_as_session(self):
		seen = {}

		def run_fn(profile, session_id, text):
			seen["session_id"] = session_id
			return "ok"

		resp = server.handle_jsonrpc(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "message/send",
				"params": {"message": _msg("hi"), "taskId": "client-123"},
			},
			profile="P",
			run_fn=run_fn,
			store=_store(),
		)
		self.assertEqual(seen["session_id"], "client-123")
		self.assertEqual(resp["result"]["id"], "client-123")

	def test_turn_failure_is_a_failed_task_not_a_protocol_error(self):
		# A turn that raises is surfaced as a Task with state=failed — the caller still
		# gets a pollable task; JSON-RPC errors are reserved for protocol faults.
		def run_fn(profile, session_id, text):
			raise RuntimeError("provider down")

		store = _store()
		resp = server.handle_jsonrpc(
			{"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": _msg("hi")}},
			profile="P",
			run_fn=run_fn,
			store=store,
			new_id=lambda: "T9",
		)
		self.assertIn("result", resp)
		self.assertEqual(resp["result"]["status"]["state"], ts.FAILED)
		# And the failed task is persisted for a follow-up tasks/get.
		self.assertEqual(store.get("T9")["state"], ts.FAILED)

	def test_message_without_text_is_invalid(self):
		resp = server.handle_jsonrpc(
			{"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {"parts": []}}},
			profile="P",
			run_fn=lambda *a: "x",
			store=_store(),
		)
		self.assertEqual(resp["error"]["code"], -32600)

	def test_completed_task_is_pollable_via_tasks_get(self):
		store = _store()
		server.handle_jsonrpc(
			{"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": _msg("hi")}},
			profile="P",
			run_fn=lambda *a: "done",
			store=store,
			new_id=lambda: "T1",
		)
		resp = server.handle_jsonrpc(
			{"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": "T1"}},
			profile="P",
			store=store,
		)
		self.assertEqual(resp["result"]["status"]["state"], ts.COMPLETED)


# ---------------------------------------------------------------------------
# tasks/get + tasks/cancel
# ---------------------------------------------------------------------------


class TestTasksGetCancel(unittest.TestCase):
	def test_get_unknown_task_is_not_found(self):
		resp = server.handle_jsonrpc(
			{"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "ghost"}},
			profile="P",
			store=_store(),
		)
		self.assertEqual(resp["error"]["code"], -32001)

	def test_cancel_non_terminal_task_cancels_it(self):
		store = _store()
		store.create("T1", "hi")
		store.set_state("T1", ts.WORKING)
		resp = server.handle_jsonrpc(
			{"jsonrpc": "2.0", "id": 1, "method": "tasks/cancel", "params": {"id": "T1"}},
			profile="P",
			store=store,
		)
		self.assertEqual(resp["result"]["status"]["state"], ts.CANCELED)

	def test_cancel_terminal_task_is_idempotent(self):
		store = _store()
		store.create("T1", "hi")
		store.set_state("T1", ts.COMPLETED)
		resp = server.handle_jsonrpc(
			{"jsonrpc": "2.0", "id": 1, "method": "tasks/cancel", "params": {"id": "T1"}},
			profile="P",
			store=store,
		)
		# Already finished — returned unchanged, not falsely "canceled".
		self.assertEqual(resp["result"]["status"]["state"], ts.COMPLETED)

	def test_cancel_unknown_task_is_not_found(self):
		resp = server.handle_jsonrpc(
			{"jsonrpc": "2.0", "id": 1, "method": "tasks/cancel", "params": {"id": "ghost"}},
			profile="P",
			store=_store(),
		)
		self.assertEqual(resp["error"]["code"], -32001)


# ---------------------------------------------------------------------------
# Protocol errors
# ---------------------------------------------------------------------------


class TestProtocolErrors(unittest.TestCase):
	def test_unknown_method(self):
		resp = server.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tasks/list"}, profile="P")
		self.assertEqual(resp["error"]["code"], -32601)

	def test_malformed_request_is_invalid(self):
		resp = server.handle_jsonrpc({"not": "jsonrpc"}, profile="P")
		self.assertEqual(resp["error"]["code"], -32600)


# ---------------------------------------------------------------------------
# HTTP routing + auth (mirrors the MCP server's hard-won real-path guards)
# ---------------------------------------------------------------------------


class TestEndpointIsRoutable(unittest.TestCase):
	"""Guards the MCP-server bug class: a handler shipped WITHOUT `@frappe.whitelist`
	is unreachable (403) even when enabled, and invisible to core tests that call
	`handle_jsonrpc` directly. Assert the Frappe ROUTING registration itself."""

	def test_handle_is_whitelisted_guest_post(self):
		import frappe

		self.assertIn(server.handle, frappe.whitelisted)
		self.assertIn(server.handle, frappe.guest_methods)
		self.assertIn("POST", frappe.allowed_http_methods_for_whitelisted_func[server.handle])

	def test_agent_card_is_whitelisted_guest_get(self):
		import frappe

		self.assertIn(server.agent_card, frappe.whitelisted)
		self.assertIn(server.agent_card, frappe.guest_methods)
		self.assertIn("GET", frappe.allowed_http_methods_for_whitelisted_func[server.agent_card])


class TestAuth(unittest.TestCase):
	"""The token rides the `X-A2A-Token` header, NOT `Authorization: Bearer` — Frappe's
	auth middleware 401s any non-OAuth Bearer before the endpoint runs (the trap the MCP
	server hit live). These pin the header name + the constant-time compare + fail-closed."""

	def test_reads_the_x_a2a_token_header(self):
		with mock.patch.object(server, "frappe") as fr:
			fr.get_request_header.return_value = "s3cr3t"
			self.assertTrue(server._authorized({"token": "s3cr3t"}))
		fr.get_request_header.assert_called_once_with("X-A2A-Token")  # NOT "Authorization"

	def test_wrong_token_rejected(self):
		with mock.patch.object(server, "frappe") as fr:
			fr.get_request_header.return_value = "nope"
			self.assertFalse(server._authorized({"token": "s3cr3t"}))

	def test_missing_header_rejected(self):
		with mock.patch.object(server, "frappe") as fr:
			fr.get_request_header.return_value = None
			self.assertFalse(server._authorized({"token": "s3cr3t"}))

	def test_unconfigured_token_rejects_everything(self):
		with mock.patch.object(server, "frappe") as fr:
			fr.get_request_header.return_value = "anything"
			self.assertFalse(server._authorized({"token": ""}))


if __name__ == "__main__":
	unittest.main()
