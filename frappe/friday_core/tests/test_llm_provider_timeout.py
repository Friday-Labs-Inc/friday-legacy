# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for the configurable LLM provider request timeout (Friday-vs-Hermes parity).

Friday's hardcoded ``TIMEOUT_SECONDS = 30`` was 60x tighter than Hermes's
1800s default, killing every long-running goal-mode task (caught 2026-06-15:
the FLI-001 ``Brand guidelines draft`` task timed out on Minimax after the
30s cap, even though Minimax was generating fine — Hermes's same model and
key were succeeding because Hermes waits up to 30 min by default).

The fix:
- ``LLM Provider`` rows carry ``request_timeout_seconds`` (Int, nullable).
- ``_build_provider`` attaches it to the constructed provider instance.
- ``LLMProvider._post_with_recovery`` reads ``self.request_timeout_seconds``
  with a 1800s fallback (Hermes parity).
- Class-level ``TIMEOUT_SECONDS = 30`` is retained as a deprecated
  fallback but never won over the row value.

Coverage:
- Class default attribute exists at None (preserves test/no-row constructions).
- ``_build_provider`` attaches the row's request_timeout_seconds.
- ``_build_provider`` leaves it None when the row field is absent / blank.
- ``_post_with_recovery`` passes the row's value as the ``timeout=`` kwarg
  to ``requests.post`` when set.
- ``_post_with_recovery`` uses the 1800s fallback when no row value is set.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestProviderHasTimeoutAttr(unittest.TestCase):
	"""The class declares the attr so directly-constructed providers work."""

	def test_class_default_is_none(self):
		from frappe.friday_core.llm.provider import LLMProvider

		self.assertTrue(hasattr(LLMProvider, "request_timeout_seconds"))
		self.assertIsNone(LLMProvider.request_timeout_seconds)


class TestBuildProviderAttachesTimeout(unittest.TestCase):
	"""_build_provider must propagate the row's request_timeout_seconds."""

	def test_attaches_row_value(self):
		from frappe.friday_core.llm import provider as provider_mod

		row = {
			"name": "test-provider",
			"provider_type": "minimax",
			"default_model": "MiniMax-M3",
			"auth_mode": "api_key",
			"api_key": "fake-key",
			"request_timeout_seconds": 600,
		}
		with patch.object(provider_mod, "_get_api_key", return_value="fake-key"):
			provider = provider_mod._build_provider(row)
		self.assertEqual(provider.request_timeout_seconds, 600)

	def test_leaves_none_when_row_field_missing(self):
		"""Absent field → None on instance; _post_with_recovery applies the default."""
		from frappe.friday_core.llm import provider as provider_mod

		row = {
			"name": "test-provider",
			"provider_type": "minimax",
			"default_model": "MiniMax-M3",
			"auth_mode": "api_key",
			"api_key": "fake-key",
			# request_timeout_seconds intentionally absent
		}
		with patch.object(provider_mod, "_get_api_key", return_value="fake-key"):
			provider = provider_mod._build_provider(row)
		self.assertIsNone(provider.request_timeout_seconds)


class TestPostWithRecoveryHonorsTimeout(unittest.TestCase):
	"""_post_with_recovery passes the resolved timeout to requests.post."""

	def _make_provider(self, request_timeout_seconds=None):
		# Construct a bare concrete provider for transport testing.
		# MinimaxProvider has the simplest init contract here.
		from frappe.friday_core.llm.provider import LLMProvider, MinimaxProvider

		p = MinimaxProvider(
			api_key="fake-key",
			default_model="MiniMax-M3",
		)
		p.request_timeout_seconds = request_timeout_seconds
		return p

	def test_uses_row_value_when_set(self):
		p = self._make_provider(request_timeout_seconds=600)

		fake_response = MagicMock()
		fake_response.status_code = 200
		fake_response.json.return_value = {"ok": True}

		with patch("frappe.friday_core.llm.provider.requests.post", return_value=fake_response) as mp:
			p._post_with_recovery(
				url="https://example.test/v1/chat",
				headers={"Authorization": "Bearer fake-key"},
				payload={"messages": []},
				model="MiniMax-M3",
			)
		# requests.post was called with timeout=600
		_, kwargs = mp.call_args
		self.assertEqual(kwargs["timeout"], 600)

	def test_uses_1800_fallback_when_row_value_none(self):
		"""Hermes parity default."""
		p = self._make_provider(request_timeout_seconds=None)

		fake_response = MagicMock()
		fake_response.status_code = 200
		fake_response.json.return_value = {"ok": True}

		with patch("frappe.friday_core.llm.provider.requests.post", return_value=fake_response) as mp:
			p._post_with_recovery(
				url="https://example.test/v1/chat",
				headers={"Authorization": "Bearer fake-key"},
				payload={"messages": []},
				model="MiniMax-M3",
			)
		_, kwargs = mp.call_args
		self.assertEqual(kwargs["timeout"], 1800.0)

	def test_zero_or_negative_falls_back_to_default(self):
		"""Defensive: 0 / negative / non-numeric must not produce a 0s timeout."""
		from frappe.friday_core.llm.provider import LLMProvider

		# Direct effective-timeout resolution check via the helper used by
		# _post_with_recovery. We test the resolver, not the network call.
		p = self._make_provider(request_timeout_seconds=0)
		self.assertEqual(p._effective_request_timeout(), 1800.0)

		p = self._make_provider(request_timeout_seconds=-1)
		self.assertEqual(p._effective_request_timeout(), 1800.0)

		p = self._make_provider(request_timeout_seconds="not-a-number")
		self.assertEqual(p._effective_request_timeout(), 1800.0)
