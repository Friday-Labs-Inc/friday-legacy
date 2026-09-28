# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for OAuth at the provider layer (design 63b-OAuth).

Covers the AnthropicProvider auth-header switch (Bearer + Claude-Code betas for
OAuth vs x-api-key for API key) and the _build_provider OAuth branch.
"""

import unittest
from unittest.mock import MagicMock, patch

_ANTHROPIC_OK = {
	"content": [{"type": "text", "text": "hi"}],
	"usage": {"input_tokens": 1, "output_tokens": 1},
	"stop_reason": "end_turn",
}


class TestAnthropicAuthHeaders(unittest.TestCase):
	def _capture_headers(self, **provider_kwargs):
		from frappe.friday_core.llm.provider import AnthropicProvider

		p = AnthropicProvider(default_model="claude-x", **provider_kwargs)
		p._post_with_recovery = MagicMock(return_value=_ANTHROPIC_OK)
		p.chat(messages=[{"role": "user", "content": "hi"}])
		return p._post_with_recovery.call_args.kwargs["headers"]

	def test_oauth_uses_bearer_and_betas(self):
		headers = self._capture_headers(api_key="", oauth_token="tok-123")
		self.assertEqual(headers["authorization"], "Bearer tok-123")
		self.assertIn("oauth-2025-04-20", headers["anthropic-beta"])
		self.assertIn("claude-code-20250219", headers["anthropic-beta"])
		self.assertTrue(headers["user-agent"].startswith("claude-cli/"))
		self.assertEqual(headers["x-app"], "cli")
		# OAuth must NOT send the API-key header
		self.assertNotIn("x-api-key", headers)

	def test_api_key_uses_x_api_key(self):
		headers = self._capture_headers(api_key="sk-ant-api-xyz")
		self.assertEqual(headers["x-api-key"], "sk-ant-api-xyz")
		self.assertNotIn("authorization", headers)
		self.assertNotIn("anthropic-beta", headers)


class TestBuildProviderOAuthBranch(unittest.TestCase):
	@patch("frappe.friday_core.llm.oauth.tokens.get_fresh_access_token", return_value="fresh-tok")
	def test_anthropic_oauth_builds_provider_with_token(self, _tok):
		from frappe.friday_core.llm.provider import AnthropicProvider, _build_provider

		row = {
			"name": "Claude Max",
			"provider_type": "anthropic",
			"auth_mode": "oauth",
			"oauth_flavor": "anthropic-claude",
			"default_model": "claude-sonnet-4",
		}
		provider = _build_provider(row)
		self.assertIsInstance(provider, AnthropicProvider)
		self.assertEqual(provider.oauth_token, "fresh-tok")

	@patch("frappe.friday_core.llm.oauth.tokens.get_fresh_access_token", return_value="fresh-tok")
	def test_codex_oauth_builds_codex_provider(self, _tok):
		from frappe.friday_core.llm.provider import CodexProvider, _build_provider

		row = {
			"name": "Codex",
			"provider_type": "openai",
			"auth_mode": "oauth",
			"oauth_flavor": "openai-codex",
			"oauth_account_id": "acc-123",
			"default_model": "gpt-5-codex",
		}
		provider = _build_provider(row)
		self.assertIsInstance(provider, CodexProvider)
		self.assertEqual(provider.oauth_token, "fresh-tok")
		self.assertEqual(provider.account_id, "acc-123")


# The Codex backend REJECTS non-streaming requests ("Stream must be set to
# true", found live on prod 2026-07-02), so the provider must send stream:true
# and assemble the reply from the SSE event stream. Output items arrive in
# `response.output_item.done` events; the terminal `response.completed` event
# carries status + usage but an EMPTY output array (live backend behaviour) —
# both facts are pinned by this fixture.
_CODEX_SSE_OK = (
	"event: response.created\n"
	'data: {"type":"response.created","response":{"id":"resp_1","status":"in_progress"}}\n'
	"\n"
	"event: response.output_item.done\n"
	'data: {"type":"response.output_item.done","item":'
	'{"type":"message","content":[{"type":"output_text","text":"OK"}]}}\n'
	"\n"
	"event: response.completed\n"
	'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed",'
	'"output":[],"usage":{"input_tokens":3,"output_tokens":2}}}\n'
)


class TestCodexSSE(unittest.TestCase):
	def _codex(self):
		from frappe.friday_core.llm.provider import CodexProvider

		return CodexProvider(oauth_token="tok", account_id="acc-123", default_model="gpt-5.4")

	def test_parse_codex_sse_returns_completed_response(self):
		from frappe.friday_core.llm.provider import _parse_codex_sse

		data = _parse_codex_sse(_CODEX_SSE_OK)
		self.assertEqual(data["id"], "resp_1")
		self.assertEqual(data["status"], "completed")
		# completed's output is empty on the live backend — the parser must
		# backfill it from the response.output_item.done events.
		self.assertEqual(data["output"][0]["content"][0]["text"], "OK")

	def test_parse_codex_sse_without_completed_raises(self):
		from frappe.friday_core.llm.provider import LLMError, _parse_codex_sse

		truncated = 'event: response.created\ndata: {"type":"response.created"}\n'
		with self.assertRaises(LLMError):
			_parse_codex_sse(truncated)

	def test_chat_sends_stream_true_and_parses_sse(self):
		p = self._codex()
		p._post_with_recovery = MagicMock(return_value=_CODEX_SSE_OK)
		result = p.chat(messages=[{"role": "user", "content": "Reply with exactly: OK"}])

		kwargs = p._post_with_recovery.call_args.kwargs
		self.assertIs(kwargs["payload"]["stream"], True)
		self.assertIs(kwargs["raw"], True)
		self.assertEqual(result["content"], "OK")
		self.assertEqual(result["usage"]["total_tokens"], 5)

	def test_chat_never_sends_temperature(self):
		"""The Codex backend rejects temperature outright ('Unsupported
		parameter: temperature', HTTP 400 live) — a configured
		default_temperature must not leak onto the wire."""
		from frappe.friday_core.llm.provider import CodexProvider

		p = CodexProvider(
			oauth_token="tok", account_id="acc-123", default_model="gpt-5.4", default_temperature=0.7
		)
		p._post_with_recovery = MagicMock(return_value=_CODEX_SSE_OK)
		p.chat(messages=[{"role": "user", "content": "hi"}])
		self.assertNotIn("temperature", p._post_with_recovery.call_args.kwargs["payload"])


if __name__ == "__main__":
	unittest.main()
