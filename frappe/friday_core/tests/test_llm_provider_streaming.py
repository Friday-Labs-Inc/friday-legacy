# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for the Hermes-parity streaming chat completion path
(``_OpenAICompatibleProvider._stream_with_recovery`` + ``_consume_stream``).

The pre-existing raw-``requests.post`` blocking path was 60x tighter than
Hermes and killed every long-form generation (caught 2026-06-15: FLI-001
guidelines task hit a Minimax ReadTimeout after 30s while Hermes was happily
running the same model for hours of coding/research). This port matches the
Hermes pattern: OpenAI SDK + ``stream=True`` + a per-chunk httpx read-timeout
acting as the stall watchdog.

Coverage:
- Streaming chat() assembles a non-streaming-shaped response from chunks.
- Tool-call deltas accumulate across chunks (name assigned, args concatenated).
- Usage from the terminal chunk lands on the response.
- The OpenAI client is constructed with httpx.Timeout(connect/read/write/pool)
  derived from the row's request_timeout_seconds + request_stale_seconds.
- Stale-stream watchdog (httpx read timeout) maps to ``request_stale_seconds``,
  not the overall budget.
- _effective_stale_seconds default + invalid-value fallback to 120s.
- _build_provider attaches request_stale_seconds from the row.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _fake_chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None, model=None):
	"""Construct one fake SDK chunk matching the chunk.choices[0].delta shape."""
	delta = SimpleNamespace(content=content, tool_calls=tool_calls)
	choice = SimpleNamespace(delta=delta, finish_reason=finish_reason, index=0)
	return SimpleNamespace(choices=[choice], usage=usage, model=model)


def _terminal_usage_chunk(*, prompt=10, completion=20, total=30, model="MiniMax-M3"):
	"""Final chunk: no choices, only usage (matches SDK behavior with
	``stream_options={"include_usage": True}``)."""
	usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)
	return SimpleNamespace(choices=[], usage=usage, model=model)


class TestStreamAssembly(unittest.TestCase):
	"""_consume_stream assembles the expected non-streaming-shaped dict."""

	def _provider(self):
		from frappe.friday_core.llm.provider import MinimaxProvider

		return MinimaxProvider(api_key="fake-key", default_model="MiniMax-M3")

	def test_text_content_chunks_concatenate(self):
		p = self._provider()
		stream = iter(
			[
				_fake_chunk(content="Hello "),
				_fake_chunk(content="world", finish_reason="stop"),
				_terminal_usage_chunk(),
			]
		)
		data = p._consume_stream(stream, model="MiniMax-M3")
		self.assertEqual(data["choices"][0]["message"]["content"], "Hello world")
		self.assertEqual(data["choices"][0]["finish_reason"], "stop")

	def test_usage_from_terminal_chunk(self):
		p = self._provider()
		stream = iter(
			[
				_fake_chunk(content="hi"),
				_terminal_usage_chunk(prompt=100, completion=50, total=150),
			]
		)
		data = p._consume_stream(stream, model="MiniMax-M3")
		self.assertEqual(data["usage"]["prompt_tokens"], 100)
		self.assertEqual(data["usage"]["completion_tokens"], 50)
		self.assertEqual(data["usage"]["total_tokens"], 150)

	def test_tool_call_deltas_accumulate_by_index(self):
		"""Function name lands as an atomic assign; args concatenate as deltas arrive."""
		p = self._provider()
		# Simulate a tool-call streamed across 3 deltas: id, name, then args fragment 1, args fragment 2.
		tc0 = SimpleNamespace(
			index=0,
			id="call_abc",
			function=SimpleNamespace(name="get_brand_brief", arguments=""),
		)
		tc1 = SimpleNamespace(
			index=0,
			id=None,
			function=SimpleNamespace(name=None, arguments='{"brand'),
		)
		tc2 = SimpleNamespace(
			index=0,
			id=None,
			function=SimpleNamespace(name=None, arguments='_id": "WI-0009"}'),
		)
		stream = iter(
			[
				_fake_chunk(tool_calls=[tc0]),
				_fake_chunk(tool_calls=[tc1]),
				_fake_chunk(tool_calls=[tc2], finish_reason="tool_calls"),
				_terminal_usage_chunk(),
			]
		)
		data = p._consume_stream(stream, model="MiniMax-M3")
		tcs = data["choices"][0]["message"]["tool_calls"]
		self.assertEqual(len(tcs), 1)
		self.assertEqual(tcs[0]["id"], "call_abc")
		self.assertEqual(tcs[0]["function"]["name"], "get_brand_brief")
		self.assertEqual(tcs[0]["function"]["arguments"], '{"brand_id": "WI-0009"}')

	def test_function_name_is_assigned_not_concatenated(self):
		"""Some providers resend the full name in every chunk — must NOT concat.

		Matches Hermes's MiniMax M2.7 workaround comment: concatenation would
		produce 'get_brand_briefget_brand_brief'.
		"""
		p = self._provider()
		tc_a = SimpleNamespace(
			index=0,
			id="call_x",
			function=SimpleNamespace(name="get_brand_brief", arguments=""),
		)
		tc_b = SimpleNamespace(
			index=0,
			id="call_x",
			function=SimpleNamespace(name="get_brand_brief", arguments='{"x": 1}'),
		)
		stream = iter(
			[
				_fake_chunk(tool_calls=[tc_a]),
				_fake_chunk(tool_calls=[tc_b], finish_reason="tool_calls"),
				_terminal_usage_chunk(),
			]
		)
		data = p._consume_stream(stream, model="MiniMax-M3")
		self.assertEqual(
			data["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
			"get_brand_brief",
		)


class TestClientTimeoutWiring(unittest.TestCase):
	"""_build_openai_client wires httpx.Timeout from the per-row config."""

	def _provider(self, *, total=None, stale=None):
		from frappe.friday_core.llm.provider import MinimaxProvider

		p = MinimaxProvider(api_key="fake-key", default_model="MiniMax-M3")
		p.request_timeout_seconds = total
		p.request_stale_seconds = stale
		return p

	def test_client_timeout_uses_row_values(self):
		p = self._provider(total=600, stale=90)
		with patch("openai.OpenAI") as mock_openai_cls:
			p._build_openai_client()
		_, kwargs = mock_openai_cls.call_args
		t = kwargs["timeout"]
		# httpx.Timeout(connect=..., read=stale, write=total, pool=...).
		self.assertEqual(t.read, 90.0)
		self.assertEqual(t.write, 600.0)
		# Connect/pool capped at min(total, 60s).
		self.assertEqual(t.connect, 60.0)
		self.assertEqual(t.pool, 60.0)

	def test_client_timeout_uses_defaults(self):
		"""No row values → 1800s write, 120s read (Hermes parity)."""
		p = self._provider(total=None, stale=None)
		with patch("openai.OpenAI") as mock_openai_cls:
			p._build_openai_client()
		_, kwargs = mock_openai_cls.call_args
		t = kwargs["timeout"]
		self.assertEqual(t.read, 120.0)
		self.assertEqual(t.write, 1800.0)

	def test_zero_values_fall_back_to_defaults(self):
		"""Misconfigured 0 / negative values must not produce 0s timeouts."""
		p = self._provider(total=0, stale=-1)
		with patch("openai.OpenAI") as mock_openai_cls:
			p._build_openai_client()
		_, kwargs = mock_openai_cls.call_args
		t = kwargs["timeout"]
		self.assertEqual(t.write, 1800.0)
		self.assertEqual(t.read, 120.0)

	def test_sdk_base_url_for_minimax_legacy_chatpath(self):
		"""Minimax's legacy CHAT_PATH ends in chatcompletion_v2, not /chat/completions.
		The SDK constructs {base}/chat/completions, so we must pass {base}/v1
		so the final URL is https://api.minimax.io/v1/chat/completions
		(matches Hermes's MINIMAX_BASE_URL)."""
		from frappe.friday_core.llm.provider import MinimaxProvider

		p = MinimaxProvider(api_key="fake-key", default_model="MiniMax-M3")
		# Confirm fixture: Minimax has a non-standard chat path.
		self.assertNotEqual(p.CHAT_PATH, "/chat/completions")
		with patch("openai.OpenAI") as mock_openai_cls:
			p._build_openai_client()
		_, kwargs = mock_openai_cls.call_args
		self.assertEqual(kwargs["base_url"], "https://api.minimax.io/v1")

	def test_sdk_base_url_for_openai_standard_chatpath(self):
		"""OpenAI's CHAT_PATH IS /v1/chat/completions — strip the suffix."""
		from frappe.friday_core.llm.provider import OpenAIProvider

		p = OpenAIProvider(api_key="fake-key", default_model="gpt-4o")
		with patch("openai.OpenAI") as mock_openai_cls:
			p._build_openai_client()
		_, kwargs = mock_openai_cls.call_args
		self.assertEqual(kwargs["base_url"], "https://api.openai.com/v1")


class TestChatGoesThroughStreaming(unittest.TestCase):
	"""chat() calls _stream_with_recovery, not the old _post_with_recovery."""

	def test_chat_calls_streaming_path(self):
		from frappe.friday_core.llm.provider import MinimaxProvider

		p = MinimaxProvider(api_key="fake-key", default_model="MiniMax-M3")

		fake_data = {
			"choices": [
				{
					"message": {"role": "assistant", "content": "ok", "tool_calls": None},
					"finish_reason": "stop",
				}
			],
			"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
			"model": "MiniMax-M3",
		}
		with patch.object(p, "_stream_with_recovery", return_value=fake_data) as mock_stream:
			resp = p.chat([{"role": "user", "content": "hi"}])
		mock_stream.assert_called_once()
		# Confirm the kwargs include stream=True + stream_options.
		_, kwargs = mock_stream.call_args
		sk = kwargs["stream_kwargs"]
		self.assertTrue(sk["stream"])
		self.assertEqual(sk["stream_options"], {"include_usage": True})
		# LLMResponse is a TypedDict; use dict access.
		self.assertEqual(resp["content"], "ok")


class TestBuildProviderAttachesStaleSeconds(unittest.TestCase):
	"""_build_provider propagates request_stale_seconds."""

	def test_attaches_row_value(self):
		from frappe.friday_core.llm import provider as provider_mod

		row = {
			"name": "Test",
			"provider_type": "minimax",
			"default_model": "MiniMax-M3",
			"auth_mode": "api_key",
			"api_key": "fake-key",
			"request_stale_seconds": 90,
		}
		with patch.object(provider_mod, "_get_api_key", return_value="fake-key"):
			provider = provider_mod._build_provider(row)
		self.assertEqual(provider.request_stale_seconds, 90)

	def test_leaves_none_when_field_missing(self):
		from frappe.friday_core.llm import provider as provider_mod

		row = {
			"name": "Test",
			"provider_type": "minimax",
			"default_model": "MiniMax-M3",
			"auth_mode": "api_key",
			"api_key": "fake-key",
		}
		with patch.object(provider_mod, "_get_api_key", return_value="fake-key"):
			provider = provider_mod._build_provider(row)
		self.assertIsNone(provider.request_stale_seconds)


# ---------------------------------------------------------------------------
# Retry / classifier behavior over the SDK transport
# ---------------------------------------------------------------------------
#
# These tests replace the obsolete `TestMinimaxProviderChat` block in
# test_llm_provider.py — that block mocked `requests.post`, which is no
# longer the transport. The behavior they validated (retry on 429/500/503,
# fail-fast on 400/404, auth error on 401, transient → retryable LLMError)
# must still hold; we just exercise it via the OpenAI SDK exception
# hierarchy instead of HTTP-level mocks.


def _api_status_error(status_code, message="server says no", *, headers=None):
	"""Construct an openai.APIStatusError suitable for raising in tests.

	The SDK builds these from an httpx.Response. We fake just enough of the
	response object for the classifier + retry logic to read.
	"""
	import openai

	resp = MagicMock()
	resp.status_code = status_code
	resp.text = message
	resp.headers = headers or {}

	body = {"error": {"message": message}}
	exc = openai.APIStatusError(message=message, response=resp, body=body)
	# Some SDK versions don't populate status_code directly on the exception.
	exc.status_code = status_code
	return exc


def _api_timeout_error():
	import openai

	return openai.APITimeoutError(request=MagicMock())


def _api_connection_error():
	import openai

	return openai.APIConnectionError(request=MagicMock())


def _success_data(content="ok"):
	return {
		"choices": [
			{
				"message": {"role": "assistant", "content": content, "tool_calls": None},
				"finish_reason": "stop",
			}
		],
		"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
		"model": "MiniMax-M3",
	}


class _StubProvider:
	"""Minimal helper to build a MinimaxProvider with patched _build_openai_client."""

	@staticmethod
	def make():
		from frappe.friday_core.llm.provider import MinimaxProvider

		p = MinimaxProvider(api_key="fake-key", default_model="MiniMax-M3")
		return p


class TestStreamingRetryBehavior(unittest.TestCase):
	"""Retry on transient errors; fail fast on non-retryable; cap at MAX_RETRIES."""

	def _patch_create(self, side_effect):
		"""Return a context that replaces _build_openai_client to return a Mock
		whose .chat.completions.create has the given side_effect."""
		client = MagicMock()
		client.chat.completions.create = MagicMock(side_effect=side_effect)
		return client

	def test_retries_on_500_then_succeeds(self):
		from frappe.friday_core.llm.provider import MinimaxProvider

		p = _StubProvider.make()

		# First call: 500 (retryable). Second call: success.
		def _success_stream():
			yield _fake_chunk(content="hi", finish_reason="stop")
			yield _terminal_usage_chunk()

		client = self._patch_create(
			[
				_api_status_error(500, "server error"),
				_success_stream(),
			]
		)
		with (
			patch.object(p, "_build_openai_client", return_value=client),
			patch("time.sleep", return_value=None),
		):
			data = p._stream_with_recovery(
				stream_kwargs={"model": "MiniMax-M3", "messages": [], "stream": True},
				model="MiniMax-M3",
			)
		self.assertEqual(data["choices"][0]["message"]["content"], "hi")
		self.assertEqual(client.chat.completions.create.call_count, 2)

	def test_retries_on_429_then_succeeds(self):
		from frappe.friday_core.llm.provider import MinimaxProvider

		p = _StubProvider.make()

		def _success_stream():
			yield _fake_chunk(content="ok", finish_reason="stop")
			yield _terminal_usage_chunk()

		client = self._patch_create(
			[
				_api_status_error(429, "rate limited"),
				_success_stream(),
			]
		)
		with (
			patch.object(p, "_build_openai_client", return_value=client),
			patch("time.sleep", return_value=None),
		):
			data = p._stream_with_recovery(
				stream_kwargs={"model": "MiniMax-M3", "messages": [], "stream": True},
				model="MiniMax-M3",
			)
		self.assertEqual(data["choices"][0]["message"]["content"], "ok")

	def test_fails_fast_on_400(self):
		"""400 is not retryable — raise on first attempt without burning retries."""
		from frappe.friday_core.llm.provider import LLMError

		p = _StubProvider.make()
		client = self._patch_create([_api_status_error(400, "bad request")])
		with (
			patch.object(p, "_build_openai_client", return_value=client),
			patch("time.sleep", return_value=None),
		):
			with self.assertRaises(LLMError):
				p._stream_with_recovery(
					stream_kwargs={"model": "MiniMax-M3", "messages": [], "stream": True},
					model="MiniMax-M3",
				)
		# Single attempt — never retried.
		self.assertEqual(client.chat.completions.create.call_count, 1)

	def test_raises_auth_error_on_401(self):
		from frappe.friday_core.llm.provider import LLMAuthError

		p = _StubProvider.make()
		client = self._patch_create([_api_status_error(401, "unauthorized")])
		with (
			patch.object(p, "_build_openai_client", return_value=client),
			patch("time.sleep", return_value=None),
		):
			with self.assertRaises(LLMAuthError):
				p._stream_with_recovery(
					stream_kwargs={"model": "MiniMax-M3", "messages": [], "stream": True},
					model="MiniMax-M3",
				)

	def test_transport_timeout_classified_as_retryable(self):
		"""APITimeoutError → retried, then LLMError with reason='timeout' on exhaust."""
		from frappe.friday_core.llm.provider import LLMError

		p = _StubProvider.make()
		client = self._patch_create(
			[
				_api_timeout_error(),
				_api_timeout_error(),
				_api_timeout_error(),
			]
		)
		with (
			patch.object(p, "_build_openai_client", return_value=client),
			patch("time.sleep", return_value=None),
		):
			with self.assertRaises(LLMError) as ctx:
				p._stream_with_recovery(
					stream_kwargs={"model": "MiniMax-M3", "messages": [], "stream": True},
					model="MiniMax-M3",
				)
		# The exhausted retry should carry a classified reason so the runner
		# records blocked_reason for the reconciler to retry the task.
		self.assertTrue(getattr(ctx.exception, "retryable", False))
		# MAX_RETRIES = 3.
		self.assertEqual(client.chat.completions.create.call_count, 3)

	def test_connection_error_retried_then_raised(self):
		from frappe.friday_core.llm.provider import LLMError

		p = _StubProvider.make()
		client = self._patch_create(
			[
				_api_connection_error(),
				_api_connection_error(),
				_api_connection_error(),
			]
		)
		with (
			patch.object(p, "_build_openai_client", return_value=client),
			patch("time.sleep", return_value=None),
		):
			with self.assertRaises(LLMError):
				p._stream_with_recovery(
					stream_kwargs={"model": "MiniMax-M3", "messages": [], "stream": True},
					model="MiniMax-M3",
				)


class TestStreamingPayloadShape(unittest.TestCase):
	"""chat() builds the right stream_kwargs for the SDK."""

	def test_chat_includes_tools_when_provided(self):
		from frappe.friday_core.llm.provider import MinimaxProvider

		p = MinimaxProvider(api_key="fake-key", default_model="MiniMax-M3")
		tools = [{"type": "function", "function": {"name": "get_brief"}}]
		with patch.object(p, "_stream_with_recovery", return_value=_success_data()) as mock_stream:
			p.chat([{"role": "user", "content": "hi"}], tools=tools)
		_, kwargs = mock_stream.call_args
		self.assertEqual(kwargs["stream_kwargs"]["tools"], tools)

	def test_chat_omits_tools_when_none(self):
		from frappe.friday_core.llm.provider import MinimaxProvider

		p = MinimaxProvider(api_key="fake-key", default_model="MiniMax-M3")
		with patch.object(p, "_stream_with_recovery", return_value=_success_data()) as mock_stream:
			p.chat([{"role": "user", "content": "hi"}])
		_, kwargs = mock_stream.call_args
		self.assertNotIn("tools", kwargs["stream_kwargs"])

	def test_chat_uses_provided_model_override(self):
		from frappe.friday_core.llm.provider import MinimaxProvider

		p = MinimaxProvider(api_key="fake-key", default_model="MiniMax-M3")
		with patch.object(p, "_stream_with_recovery", return_value=_success_data()) as mock_stream:
			p.chat([{"role": "user", "content": "hi"}], model="MiniMax-M2.7")
		_, kwargs = mock_stream.call_args
		self.assertEqual(kwargs["stream_kwargs"]["model"], "MiniMax-M2.7")
		self.assertEqual(kwargs["model"], "MiniMax-M2.7")
