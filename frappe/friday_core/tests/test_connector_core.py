# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the generic Connector spine (Design 81a).

Mock-based — no DB, no network. These pin the behaviours that USED to live in
the first connector surface + its client, and are now generic core:
  - Stripe-style signature: valid passes; bad v1 fails BEFORE freshness;
    stale t fails AFTER a valid v1; malformed headers fail closed
  - process_event: Processed replays skip; handler errors → Failed + reason
    (the row is the error ledger); no-handler types → Processed "recorded";
    dispatch reads the connector's handler_module registry
  - outbound client: token auth header from the Connector row, NEVER raises
    (returns None on any failure), disabled/missing connector drops the send
"""

import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.connectors import client as connector_client
from frappe.friday_core.connectors import core

_CORE = "frappe.friday_core.connectors.core"
_CLIENT = "frappe.friday_core.connectors.client"

SECRET = "test-secret"


def _sign(body: bytes, t: float | None = None, secret: str = SECRET) -> str:
	t = t if t is not None else time.time()
	v1 = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
	return f"t={t},v1={v1}"


class TestSignature(unittest.TestCase):
	def test_valid_signature_passes(self):
		body = b'{"id": "evt-1"}'
		self.assertTrue(core.verify_signature(body, _sign(body), SECRET, 300))

	def test_wrong_secret_fails(self):
		body = b"{}"
		self.assertFalse(core.verify_signature(body, _sign(body, secret="other"), SECRET, 300))

	def test_tampered_body_fails(self):
		header = _sign(b'{"amount": 1}')
		self.assertFalse(core.verify_signature(b'{"amount": 9}', header, SECRET, 300))

	def test_stale_t_fails_even_with_valid_v1(self):
		body = b"{}"
		old = time.time() - 3600
		self.assertFalse(core.verify_signature(body, _sign(body, t=old), SECRET, 300))

	def test_fresh_replay_with_new_t_passes(self):
		body = b'{"id": "evt-1"}'
		self.assertTrue(core.verify_signature(body, _sign(body), SECRET, 300))

	def test_malformed_headers_fail_closed(self):
		for header in ("", "t=123", "v1=abc", "junk", "t=abc,v1=zzz"):
			self.assertFalse(core.verify_signature(b"{}", header, SECRET, 300))

	def test_missing_secret_fails_closed(self):
		body = b"{}"
		self.assertFalse(core.verify_signature(body, _sign(body), "", 300))


class TestProcessEvent(unittest.TestCase):
	def _event(self, event_type="payment.received", status="Received", payload=None, connector="c1"):
		event = MagicMock()
		event.connector = connector
		event.event_type = event_type
		event.status = status
		event.payload = json.dumps(payload or {})
		return event

	@patch(f"{_CORE}.frappe")
	def test_processed_replay_is_skipped(self, mock_frappe):
		event = self._event(status="Processed")
		mock_frappe.get_doc.return_value = event
		core.process_event("evt-1")
		event.save.assert_not_called()

	@patch(f"{_CORE}._load_handlers", return_value={})
	@patch(f"{_CORE}.frappe")
	def test_dict_payload_tolerated(self, mock_frappe, _handlers):
		# Frappe's JSON fieldtype returns a parsed dict on Postgres reads.
		event = self._event(event_type="gate.opened")
		event.payload = {"already": "parsed"}
		mock_frappe.get_doc.return_value = event
		core.process_event("evt-1")
		self.assertEqual(event.status, "Processed")

	@patch(f"{_CORE}._load_handlers", return_value={})
	@patch(f"{_CORE}.frappe")
	def test_no_handler_type_is_recorded(self, mock_frappe, _handlers):
		event = self._event(event_type="gate.opened")
		mock_frappe.get_doc.return_value = event
		core.process_event("evt-1")
		self.assertEqual(event.status, "Processed")
		self.assertIn("no handler", event.failure_reason)

	@patch(
		f"{_CORE}._load_handlers", return_value={"boom.event": MagicMock(side_effect=RuntimeError("kaput"))}
	)
	@patch(f"{_CORE}.frappe")
	def test_handler_error_marks_failed_with_reason(self, mock_frappe, _handlers):
		event = self._event(event_type="boom.event")
		mock_frappe.get_doc.return_value = event
		core.process_event("evt-1")
		self.assertEqual(event.status, "Failed")
		self.assertIn("RuntimeError", event.failure_reason)
		mock_frappe.log_error.assert_called_once()

	@patch(f"{_CORE}._load_handlers")
	@patch(f"{_CORE}.frappe")
	def test_handler_dispatched_by_event_type(self, mock_frappe, mock_load):
		handler = MagicMock()
		mock_load.return_value = {"payment.received": handler}
		event = self._event(event_type="payment.received", payload={"x": 1})
		mock_frappe.get_doc.return_value = event
		core.process_event("evt-1")
		handler.assert_called_once()
		self.assertEqual(event.status, "Processed")


class TestOutboundClient(unittest.TestCase):
	def _connector(self, enabled=1, signing_secret=""):
		connector = MagicMock()
		connector.enabled = enabled
		connector.api_base_url = "https://ops.example.com"
		connector.api_key = "key123"
		# Field-aware secrets: token auth always configured; outbound signing
		# only when a test opts in (unsigned = the pre-signing behavior).
		connector.get_password.side_effect = lambda field: {
			"api_secret": "sec456",
			"outbound_signing_secret": signing_secret,
		}.get(field, "")
		return connector

	@patch(f"{_CLIENT}.requests")
	@patch(f"{_CLIENT}.frappe")
	def test_token_auth_and_url_shape(self, mock_frappe, mock_requests):
		mock_frappe.db.exists.return_value = True
		mock_frappe.get_cached_doc.return_value = self._connector()
		mock_requests.post.return_value = MagicMock(json=lambda: {"message": "ok"})
		connector_client.send("c1", "partner_app.api.v1.update_task_progress", {"task": "T-1"})
		url = mock_requests.post.call_args[0][0]
		self.assertEqual(url, "https://ops.example.com/api/method/partner_app.api.v1.update_task_progress")
		headers = mock_requests.post.call_args.kwargs["headers"]
		self.assertEqual(headers["Authorization"], "token key123:sec456")

	@patch(f"{_CLIENT}.requests")
	@patch(f"{_CLIENT}.frappe")
	def test_never_raises_returns_none_on_failure(self, mock_frappe, mock_requests):
		mock_frappe.db.exists.return_value = True
		mock_frappe.get_cached_doc.return_value = self._connector()
		mock_requests.post.side_effect = RuntimeError("network down")
		self.assertIsNone(connector_client.send("c1", "x.y", {"note": "x"}))
		mock_frappe.log_error.assert_called_once()

	@patch(f"{_CLIENT}.frappe")
	def test_disabled_connector_drops_send(self, mock_frappe):
		mock_frappe.db.exists.return_value = True
		mock_frappe.get_cached_doc.return_value = self._connector(enabled=0)
		self.assertIsNone(connector_client.send("c1", "x.y", {"note": "x"}))

	@patch(f"{_CLIENT}.frappe")
	def test_missing_connector_drops_send(self, mock_frappe):
		mock_frappe.db.exists.return_value = False
		self.assertIsNone(connector_client.send("nope", "x.y", {"note": "x"}))


class TestOutboundSigning(unittest.TestCase):
	"""X-Friday-Signature — the outbound identity proof (the Layer-2 wire).

	Found live on the Friday Labs E2E: RandomPack's `_require_friday()` gates its
	integration writes on this header; Friday sent none → every attach_deliverable
	/ request_gate_open / get_project 403'd. The signature must cover the EXACT
	raw bytes posted — re-serializing on the receiving side breaks the HMAC, so
	the client serializes the body itself and posts those bytes."""

	def _connector(self, signing_secret="s3cret"):
		connector = MagicMock()
		connector.enabled = 1
		connector.api_base_url = "https://ops.example.com"
		connector.api_key = "key123"
		connector.get_password.side_effect = lambda field: {
			"api_secret": "sec456",
			"outbound_signing_secret": signing_secret,
		}.get(field, "")
		return connector

	@patch(f"{_CLIENT}.requests")
	@patch(f"{_CLIENT}.frappe")
	def test_signed_call_carries_header_over_exact_bytes(self, mock_frappe, mock_requests):
		import hashlib
		import hmac as hmac_mod

		mock_frappe.db.exists.return_value = True
		mock_frappe.get_cached_doc.return_value = self._connector()
		mock_requests.post.return_value = MagicMock(json=lambda: {"message": "ok"})

		connector_client.send("c1", "partner_app.api.v1.attach_deliverable", {"project": "PROJ-1"})

		kwargs = mock_requests.post.call_args.kwargs
		headers = kwargs["headers"]
		self.assertIn("X-Friday-Signature", headers)
		self.assertEqual(headers["Content-Type"], "application/json")
		# Token auth STILL rides along (the role layer) — signing is additive.
		self.assertEqual(headers["Authorization"], "token key123:sec456")
		# The signature verifies against the EXACT bytes that were posted.
		body = kwargs["data"]
		self.assertIsInstance(body, bytes)
		t_part, v1_part = headers["X-Friday-Signature"].split(",")
		t = t_part.split("=", 1)[1]
		expected = hmac_mod.new(b"s3cret", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
		self.assertEqual(v1_part.split("=", 1)[1], expected)

	@patch(f"{_CLIENT}.requests")
	@patch(f"{_CLIENT}.frappe")
	def test_no_secret_means_no_signature_no_behavior_change(self, mock_frappe, mock_requests):
		mock_frappe.db.exists.return_value = True
		mock_frappe.get_cached_doc.return_value = self._connector(signing_secret="")
		mock_requests.post.return_value = MagicMock(json=lambda: {"message": "ok"})

		connector_client.send("c1", "x.y.z", {"a": 1})

		kwargs = mock_requests.post.call_args.kwargs
		self.assertNotIn("X-Friday-Signature", kwargs["headers"])
		self.assertIn("json", kwargs)  # the original json= path, untouched

	@patch(f"{_CLIENT}.requests")
	@patch(f"{_CLIENT}.frappe")
	def test_multipart_uploads_are_not_signed(self, mock_frappe, mock_requests):
		# No canonical byte representation for multipart; the stock upload
		# endpoint is ungated anyway. Signing only the JSON calls is the contract.
		mock_frappe.db.exists.return_value = True
		mock_frappe.get_cached_doc.return_value = self._connector()
		mock_requests.post.return_value = MagicMock(json=lambda: {"message": {"file_url": "/f"}})

		connector_client.send("c1", "upload_file", {"is_private": 1}, files={"file": ("x.md", b"hi")})

		kwargs = mock_requests.post.call_args.kwargs
		self.assertNotIn("X-Friday-Signature", kwargs["headers"])


if __name__ == "__main__":
	unittest.main()
