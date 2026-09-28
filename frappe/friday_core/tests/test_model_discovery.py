# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for provider model discovery (design 63).

Ports Hermes' models.py pattern: live ``/v1/models`` per provider with a
curated catalog fallback. Tests mock the HTTP layer so they run headless.
"""

import unittest
from unittest.mock import MagicMock, patch

_M = "frappe.friday_core.llm.model_discovery"


def _resp(status=200, payload=None):
	r = MagicMock()
	r.status_code = status
	r.json.return_value = payload or {}
	return r


class TestFetchModelsLive(unittest.TestCase):
	"""A reachable /v1/models endpoint is the authoritative source."""

	@patch(f"{_M}.requests")
	def test_openai_compat_uses_bearer_and_parses_data_ids(self, mock_requests):
		from frappe.friday_core.llm.model_discovery import fetch_models

		mock_requests.get.return_value = _resp(200, {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})

		out = fetch_models("openai", "sk-key", "https://api.openai.com")

		self.assertEqual(out["source"], "live")
		self.assertEqual(out["models"], ["gpt-4o", "gpt-4o-mini"])
		# Bearer auth + the /v1/models path.
		call = mock_requests.get.call_args
		self.assertIn("/v1/models", call.args[0])
		self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer sk-key")

	@patch(f"{_M}.requests")
	def test_minimax_uses_bearer(self, mock_requests):
		from frappe.friday_core.llm.model_discovery import fetch_models

		mock_requests.get.return_value = _resp(200, {"data": [{"id": "MiniMax-M2"}]})

		out = fetch_models("minimax", "key", "https://api.minimax.io")

		self.assertEqual(out["models"], ["MiniMax-M2"])
		self.assertEqual(mock_requests.get.call_args.kwargs["headers"]["Authorization"], "Bearer key")

	@patch(f"{_M}.requests")
	def test_anthropic_uses_x_api_key_header(self, mock_requests):
		from frappe.friday_core.llm.model_discovery import fetch_models

		mock_requests.get.return_value = _resp(200, {"data": [{"id": "claude-3-5-sonnet-20241022"}]})

		out = fetch_models("anthropic", "sk-ant-key", "https://api.anthropic.com")

		self.assertEqual(out["source"], "live")
		self.assertIn("claude-3-5-sonnet-20241022", out["models"])
		headers = mock_requests.get.call_args.kwargs["headers"]
		# Anthropic uses x-api-key + a version header, NOT Bearer.
		self.assertEqual(headers["x-api-key"], "sk-ant-key")
		self.assertIn("anthropic-version", headers)
		self.assertNotIn("Authorization", headers)


class TestFetchModelsFallback(unittest.TestCase):
	"""When live fetch can't run, the curated catalog keeps the picker full."""

	def test_no_api_key_returns_catalog(self):
		from frappe.friday_core.llm.model_discovery import fetch_models

		out = fetch_models("anthropic", "", "https://api.anthropic.com")

		self.assertEqual(out["source"], "catalog")
		self.assertTrue(out["models"])  # non-empty
		self.assertIsNotNone(out["error"])

	@patch(f"{_M}.requests")
	def test_network_error_falls_back_to_catalog(self, mock_requests):
		from frappe.friday_core.llm.model_discovery import fetch_models

		mock_requests.get.side_effect = Exception("connection refused")

		out = fetch_models("openai", "sk-key", "https://api.openai.com")

		self.assertEqual(out["source"], "catalog")
		self.assertIn("gpt-4o", out["models"])
		self.assertIn("connection refused", out["error"])

	@patch(f"{_M}.requests")
	def test_non_200_falls_back_to_catalog(self, mock_requests):
		from frappe.friday_core.llm.model_discovery import fetch_models

		mock_requests.get.return_value = _resp(401, {"error": "unauthorized"})

		out = fetch_models("minimax", "bad-key", "https://api.minimax.io")

		self.assertEqual(out["source"], "catalog")
		self.assertTrue(out["models"])
		self.assertIn("401", out["error"])

	def test_unknown_provider_type_returns_empty_with_error(self):
		from frappe.friday_core.llm.model_discovery import fetch_models

		out = fetch_models("imaginary", "key", "https://x")

		self.assertEqual(out["models"], [])
		self.assertIn("unknown", out["error"].lower())


class TestListModelsEndpoint(unittest.TestCase):
	"""The whitelisted endpoint resolves the provider row and its key."""

	@patch(f"{_M}.fetch_models")
	@patch(f"{_M}.frappe")
	def test_loads_provider_row_and_calls_fetch(self, mock_frappe, mock_fetch):
		from frappe.friday_core.llm.model_discovery import list_models

		doc = MagicMock()
		doc.provider_type = "minimax"
		doc.base_url = "https://api.minimax.io"
		doc.get_password.return_value = "secret-key"
		mock_frappe.get_doc.return_value = doc
		mock_fetch.return_value = {"models": ["MiniMax-M2"], "source": "live", "error": None}

		out = list_models("Minimax")

		mock_fetch.assert_called_once_with("minimax", "secret-key", "https://api.minimax.io")
		self.assertEqual(out["provider"], "Minimax")
		self.assertEqual(out["models"], ["MiniMax-M2"])

	@patch(f"{_M}.frappe")
	def test_missing_api_key_clears_the_phantom_desk_message(self, mock_frappe):
		"""get_password frappe.throw()s when the field was never set; the raise
		is caught, but the message it queued would still POP UP in Desk next
		to a perfectly good catalog dialog ("Password not found…"). The
		endpoint must clear the message log."""
		from frappe.friday_core.llm.model_discovery import list_models

		doc = MagicMock()
		doc.auth_mode = "api_key"
		doc.provider_type = "openai"
		doc.base_url = ""
		doc.get_password.side_effect = Exception("Password not found")
		mock_frappe.get_doc.return_value = doc

		out = list_models("Codex")

		mock_frappe.clear_messages.assert_called_once()
		self.assertEqual(out["source"], "catalog")


class TestOAuthDiscovery(unittest.TestCase):
	"""OAuth providers (design 63b) discover models with their subscription
	token — never via get_password('api_key'), which they don't have."""

	def test_codex_oauth_uses_the_live_codex_models_endpoint(self):
		from frappe.friday_core.llm.model_discovery import fetch_models_oauth

		resp = MagicMock(status_code=200)
		resp.json.return_value = {
			"models": [
				{"slug": "gpt-5.5", "priority": 2, "visibility": "show"},
				{"slug": "gpt-5.4", "priority": 1, "visibility": "show"},
				{"slug": "secret-internal", "priority": 0, "visibility": "hidden"},
			]
		}
		with patch(f"{_M}.requests") as mock_requests:
			mock_requests.get.return_value = resp
			out = fetch_models_oauth("openai-codex", "tok-1", "")

		url = mock_requests.get.call_args.args[0]
		headers = mock_requests.get.call_args.kwargs["headers"]
		self.assertIn("chatgpt.com/backend-api/codex/models", url)
		self.assertEqual(headers["Authorization"], "Bearer tok-1")
		# hidden models dropped, remainder sorted by priority
		self.assertEqual(out["models"], ["gpt-5.4", "gpt-5.5"])
		self.assertEqual(out["source"], "live")

	def test_codex_oauth_live_failure_falls_back_to_codex_catalog(self):
		from frappe.friday_core.llm.model_discovery import fetch_models_oauth

		with patch(f"{_M}.requests") as mock_requests:
			mock_requests.get.side_effect = Exception("boom")
			out = fetch_models_oauth("openai-codex", "tok-1", "")

		self.assertEqual(out["source"], "catalog")
		self.assertIn("gpt-5.4", out["models"])
		# the codex catalog, NOT the api-key openai catalog
		self.assertNotIn("gpt-4o", out["models"])

	def test_anthropic_oauth_uses_bearer_and_oauth_beta(self):
		from frappe.friday_core.llm.model_discovery import fetch_models_oauth

		resp = MagicMock(status_code=200)
		resp.json.return_value = {"data": [{"id": "claude-sonnet-4-6"}]}
		with patch(f"{_M}.requests") as mock_requests:
			mock_requests.get.return_value = resp
			out = fetch_models_oauth("anthropic-claude", "tok-2", "")

		headers = mock_requests.get.call_args.kwargs["headers"]
		self.assertEqual(headers["authorization"], "Bearer tok-2")
		self.assertIn("oauth-2025-04-20", headers["anthropic-beta"])
		self.assertEqual(out["models"], ["claude-sonnet-4-6"])
		self.assertEqual(out["source"], "live")

	def test_no_token_returns_flavor_catalog_not_an_error(self):
		from frappe.friday_core.llm.model_discovery import fetch_models_oauth

		out = fetch_models_oauth("openai-codex", "", "")
		self.assertEqual(out["source"], "catalog")
		self.assertIn("gpt-5.4", out["models"])

	@patch(f"{_M}.frappe")
	def test_list_models_routes_oauth_rows_without_touching_api_key(self, mock_frappe):
		from frappe.friday_core.llm.model_discovery import list_models

		doc = MagicMock()
		doc.auth_mode = "oauth"
		doc.oauth_flavor = "openai-codex"
		doc.provider_type = "openai"
		doc.base_url = ""
		mock_frappe.get_doc.return_value = doc

		with (
			patch(
				f"{_M}.fetch_models_oauth",
				return_value={"models": ["gpt-5.4"], "source": "live", "error": None},
			) as mock_oauth,
			patch(f"{_M}._fresh_oauth_token", return_value="tok-3"),
		):
			out = list_models("Codex")

		doc.get_password.assert_not_called()
		mock_oauth.assert_called_once_with("openai-codex", "tok-3", "")
		self.assertEqual(out["models"], ["gpt-5.4"])

	@patch(f"{_M}.fetch_models")
	@patch(f"{_M}.frappe")
	def test_missing_key_still_returns_catalog(self, mock_frappe, mock_fetch):
		"""get_password raises when the field was never set — treat as no key."""
		from frappe.friday_core.llm.model_discovery import list_models

		doc = MagicMock()
		doc.provider_type = "anthropic"
		doc.base_url = None
		doc.get_password.side_effect = Exception("no password")
		mock_frappe.get_doc.return_value = doc
		mock_fetch.return_value = {
			"models": ["claude-3-5-sonnet-20241022"],
			"source": "catalog",
			"error": "no key",
		}

		out = list_models("Anthropic")

		# Empty key passed through; base_url falls back to provider default (None → "").
		args = mock_fetch.call_args.args
		self.assertEqual(args[0], "anthropic")
		self.assertEqual(args[1], "")
		self.assertEqual(out["source"], "catalog")


if __name__ == "__main__":
	unittest.main()
