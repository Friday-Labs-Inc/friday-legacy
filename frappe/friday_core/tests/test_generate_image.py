# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Tests for the `generate-image` skill (multi-provider since design 96).

The provider HTTP calls and file save are mocked — no network, no API cost,
nothing committed. We assert the happy paths for both backends (correct
host/auth/model + saved file URL returned), and the guarded failure paths
(no prompt, no agent context, unsupported provider type, API error code)
which must return a helpful message and never raise.

`_medium_route_target` is patched to None throughout so the resolution
falls through to the (patched) profile chain — route-hit behaviour is
covered in test_medium_routing.py.
"""

from __future__ import annotations

import base64
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.friday_core.skills import handlers_visual as hv

_P = "frappe.friday_core.llm.provider"


class TestGenerateImage(unittest.TestCase):
	def setUp(self):
		self._route = patch(f"{_P}._medium_route_target", return_value=None)
		self._route.start()

	def tearDown(self):
		self._route.stop()
		frappe.flags["friday_dispatch_context"] = None

	def test_requires_prompt(self):
		out = hv.generate_image("generate-image", {})
		self.assertIn("prompt", out["result"])

	def test_requires_agent_context(self):
		frappe.flags["friday_dispatch_context"] = {}
		out = hv.generate_image("generate-image", {"prompt": "a logo"})
		self.assertIn("agent", out["result"].lower())

	@patch(f"{_P}._resolve_provider_row")
	def test_unsupported_provider_type_rejected(self, m_row):
		m_row.return_value = {"name": "Claude", "provider_type": "anthropic", "base_url": ""}
		frappe.flags["friday_dispatch_context"] = {"agent_profile": "Visual Agent"}
		out = hv.generate_image("generate-image", {"prompt": "a logo"})
		# The message names what IS supported and where to fix the routing.
		self.assertIn("minimax", out["result"])
		self.assertIn("openai", out["result"])
		self.assertIn("Model Routes", out["result"])

	@patch("frappe.utils.file_manager.save_file")
	@patch("frappe.friday_core.skills.handlers_visual.requests")
	@patch(f"{_P}._get_api_key")
	@patch(f"{_P}._resolve_provider_row")
	def test_minimax_success_saves_and_returns_url(self, m_row, m_key, m_req, m_save):
		m_row.return_value = {
			"name": "Minimax",
			"provider_type": "minimax",
			"base_url": "https://api.minimax.io/v1",
		}
		m_key.return_value = "secret"
		post_resp = MagicMock()
		post_resp.json.return_value = {
			"data": {"image_urls": ["https://img.example/1.png"]},
			"base_resp": {"status_code": 0, "status_msg": "success"},
		}
		get_resp = MagicMock()
		get_resp.content = b"PNGBYTES"
		m_req.post.return_value = post_resp
		m_req.get.return_value = get_resp
		m_save.return_value = MagicMock(file_url="/files/glacial-logo-1.png")
		frappe.flags["friday_dispatch_context"] = {"agent_profile": "Visual Agent", "session_id": "chat-uuid"}

		out = hv.generate_image(
			"generate-image", {"prompt": "glacial minimalist logo", "aspect_ratio": "1:1"}
		)

		self.assertEqual(out["image_urls"], ["/files/glacial-logo-1.png"])
		self.assertIn("/files/glacial-logo-1.png", out["result"])
		# correct endpoint, auth, and model
		args, kwargs = m_req.post.call_args
		self.assertIn("api.minimax.io/v1/image_generation", args[0])
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
		self.assertEqual(kwargs["json"]["model"], "image-01")
		self.assertEqual(kwargs["json"]["aspect_ratio"], "1:1")

	@patch("frappe.utils.file_manager.save_file")
	@patch("frappe.friday_core.skills.handlers_visual.requests")
	@patch(f"{_P}._get_api_key")
	@patch(f"{_P}._resolve_provider_row")
	def test_openai_success_decodes_base64(self, m_row, m_key, m_req, m_save):
		m_row.return_value = {
			"name": "OpenAI Images",
			"provider_type": "openai",
			"base_url": "",
			"image_model": "gpt-image-1",
		}
		m_key.return_value = "sk-secret"
		png = b"\x89PNG\r\n\x1a\n" + b"fake"
		post_resp = MagicMock()
		post_resp.json.return_value = {"data": [{"b64_json": base64.b64encode(png).decode()}]}
		m_req.post.return_value = post_resp
		m_save.return_value = MagicMock(file_url="/files/robot-mark-1.png")
		frappe.flags["friday_dispatch_context"] = {"agent_profile": "Visual Agent", "session_id": "chat-uuid"}

		out = hv.generate_image("generate-image", {"prompt": "robot mark", "aspect_ratio": "16:9"})

		self.assertEqual(out["image_urls"], ["/files/robot-mark-1.png"])
		# correct endpoint, auth, model, and ratio→size mapping
		args, kwargs = m_req.post.call_args
		self.assertIn("api.openai.com/v1/images/generations", args[0])
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-secret")
		self.assertEqual(kwargs["json"]["model"], "gpt-image-1")
		self.assertEqual(kwargs["json"]["size"], "1536x1024")
		self.assertNotIn("aspect_ratio", kwargs["json"])  # OpenAI takes sizes, not ratios
		# the decoded bytes were saved directly — no download round-trip
		m_req.get.assert_not_called()
		saved_bytes = m_save.call_args[0][1]
		self.assertEqual(saved_bytes, png)

	@patch("frappe.friday_core.skills.handlers_visual.requests")
	@patch(f"{_P}._get_api_key")
	@patch(f"{_P}._resolve_provider_row")
	def test_openai_api_error_is_reported_not_raised(self, m_row, m_key, m_req):
		m_row.return_value = {"name": "OpenAI Images", "provider_type": "openai", "base_url": ""}
		m_key.return_value = "sk-secret"
		post_resp = MagicMock()
		post_resp.json.return_value = {"error": {"message": "content policy violation"}}
		m_req.post.return_value = post_resp
		frappe.flags["friday_dispatch_context"] = {"agent_profile": "Visual Agent"}

		out = hv.generate_image("generate-image", {"prompt": "a logo"})
		self.assertIn("content policy violation", out["result"])

	@patch("frappe.friday_core.skills.handlers_visual.requests")
	@patch(f"{_P}._get_api_key")
	@patch(f"{_P}._resolve_provider_row")
	def test_minimax_api_error_is_reported_not_raised(self, m_row, m_key, m_req):
		m_row.return_value = {"name": "Minimax", "provider_type": "minimax", "base_url": ""}
		m_key.return_value = "secret"
		post_resp = MagicMock()
		post_resp.json.return_value = {
			"base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}
		}
		m_req.post.return_value = post_resp
		frappe.flags["friday_dispatch_context"] = {"agent_profile": "Visual Agent"}

		out = hv.generate_image("generate-image", {"prompt": "a logo"})
		self.assertIn("1008", out["result"])

	@patch(f"{_P}._get_api_key")
	@patch(f"{_P}._resolve_provider_row")
	def test_china_host_selected_by_base_url(self, m_row, m_key):
		m_row.return_value = {
			"name": "Minimax",
			"provider_type": "minimax",
			"base_url": "https://api.minimaxi.com/v1",
		}
		m_key.return_value = "secret"
		with patch("frappe.friday_core.skills.handlers_visual.requests") as m_req:
			post_resp = MagicMock()
			post_resp.json.return_value = {"data": {"image_urls": []}, "base_resp": {"status_code": 0}}
			m_req.post.return_value = post_resp
			frappe.flags["friday_dispatch_context"] = {"agent_profile": "Visual Agent"}
			hv.generate_image("generate-image", {"prompt": "a logo"})
			args, _ = m_req.post.call_args
			self.assertIn("api.minimaxi.com", args[0])


class TestOpenAISizeMap(unittest.TestCase):
	def test_ratio_to_size(self):
		self.assertEqual(hv._openai_size("1:1"), "1024x1024")
		self.assertEqual(hv._openai_size("16:9"), "1536x1024")
		self.assertEqual(hv._openai_size("4:3"), "1536x1024")
		self.assertEqual(hv._openai_size("9:16"), "1024x1536")
		self.assertEqual(hv._openai_size("weird"), "auto")
		self.assertEqual(hv._openai_size(""), "auto")
