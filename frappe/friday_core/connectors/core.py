# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""Generic signed-event intake + dispatch for `system` connectors (Design 81a).

PLAIN ENGLISH
=============
An external system POSTs a signed event. The generic spine, for ANY system
connector:

  1. Verifies the Stripe-style per-attempt signature
     (`<header>: t=<unix>,v1=HMAC(secret,"{t}.{body}"),v2=HMAC(secret,"{t}.{path}.{body}")`):
     constant-time compare FIRST, then a freshness window on `t`. v2 binds the
     endpoint so a captured signature cannot be replayed at another method that
     accepts the same body shape. v2 is REQUIRED whenever the receiver knows its
     own path; v1 answers only where there is no request to have one.
  2. Persists the envelope as a Connector Event row (`event_id` UNIQUE → a
     duplicate delivery is a 200 no-op), tagged with the connector.
  3. Acks 200 immediately; the handler runs on the dedicated `friday` queue.

The handler map is NOT hard-coded here — the connector's `handler_module` is
imported at process time and its `HANDLERS` dict is used. That keeps the seam
generic (core) while the event meaning stays in the connector's domain module.
This is the verbatim generalisation of the first connector surface's spine.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import time

import frappe

SIGNATURE_HEADER = "X-RP-Signature"
DEFAULT_TOLERANCE_SECONDS = 300


# ---------------------------------------------------------------------------
# Inbound intake (called by a connector's thin whitelisted wrapper)
# ---------------------------------------------------------------------------


def receive_event(connector_name: str) -> dict:
	"""Verify + persist + queue one inbound signed event for `connector_name`.

	Guest-reachable by design (the connector's wrapper is whitelisted with
	allow_guest): the HMAC signature IS the authentication. Returns fast;
	processing is queued on the `friday` queue.
	"""
	connector = frappe.get_cached_doc("Connector", connector_name)
	if not connector.enabled:
		frappe.throw(frappe._("Connector {0} is disabled.").format(connector_name), frappe.PermissionError)

	raw_body = frappe.request.get_data() or b""
	header = frappe.get_request_header(SIGNATURE_HEADER) or ""
	try:
		secret = connector.get_password("webhook_secret") or ""
	except Exception:
		secret = ""  # unset secret → verify_signature fails closed (401)
	tolerance = connector.signature_tolerance_seconds or DEFAULT_TOLERANCE_SECONDS

	# The path we are answering on, which the sender bound into v2. nginx
	# proxies without rewriting, so both ends compute the same string.
	try:
		endpoint = frappe.request.path if frappe.request else ""
	except Exception:
		endpoint = ""

	if not verify_signature(raw_body, header, secret, tolerance, endpoint=endpoint):
		frappe.throw(frappe._("Invalid event signature."), frappe.AuthenticationError)

	envelope = json.loads(raw_body)
	event_id = envelope.get("id")
	event_type = envelope.get("type")
	if not event_id or not event_type:
		frappe.throw(frappe._("Envelope must carry id and type."), frappe.ValidationError)

	# UUID dedupe: the unique event_id makes duplicates a clean no-op.
	if frappe.db.exists("Connector Event", event_id):
		return {"ok": True, "deduped": True, "event": event_id}

	frappe.get_doc(
		{
			"doctype": "Connector Event",
			"connector": connector_name,
			"event_id": event_id,
			"event_type": event_type,
			"version": str(envelope.get("version") or ""),
			"occurred_at": str(envelope.get("occurred_at") or ""),
			"payload": frappe.as_json(envelope.get("data") or {}),
			"status": "Received",
		}
	).insert(ignore_permissions=True)

	frappe.enqueue(
		"frappe.friday_core.connectors.core.process_event",
		event_id=event_id,
		queue="friday",
		timeout=600,
		enqueue_after_commit=True,
	)
	return {"ok": True, "event": event_id}


def verify_signature(
	raw_body: bytes, header: str, secret: str, tolerance_seconds: int, endpoint: str | None = None
) -> bool:
	"""Verify the Stripe-style per-attempt signature (locked contract).

	Order matters: constant-time verify the digest FIRST (an attacker learns
	nothing from timing), then enforce the freshness window on `t`.

	v2 binds the endpoint, so a captured signature cannot be replayed at a
	different method that happens to accept the same body shape. Preferred
	whenever the caller sent one and we know our own path; v1 stays accepted
	while both benches change over.
	"""
	if not secret or not header:
		return False
	parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
	t = parts.get("t")
	if not t:
		return False

	if endpoint:
		# REQUIRED, not preferred. Accepting v1 as a fallback was the changeover
		# and it left the hole open: anyone able to capture a signed request
		# could strip v2 from the header and be back to the old guarantee. Both
		# ends were confirmed sending v2 in production before this closed.
		if not parts.get("v2"):
			return False
		signed = f"{t}.{endpoint}.".encode() + raw_body
		expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
		if not hmac.compare_digest(expected, parts["v2"]):
			return False
	else:
		# No request means no path to bind — the CLI and the test path. A caller
		# cannot reach this: the server always knows its own path when a request
		# exists.
		v1 = parts.get("v1")
		if not v1:
			return False
		signed = f"{t}.".encode() + raw_body
		expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
		if not hmac.compare_digest(expected, v1):
			return False

	try:
		age = abs(time.time() - float(t))
	except (TypeError, ValueError):
		return False
	return age <= tolerance_seconds


# ---------------------------------------------------------------------------
# Processing (runs on the friday queue)
# ---------------------------------------------------------------------------


def _load_handlers(connector_name: str) -> dict:
	"""Import the connector's handler_module and return its HANDLERS dict.

	The module declares `HANDLERS = {event_type: callable(data, event)}`. A
	connector with no handler_module (or no HANDLERS) records events without
	acting — the audit ledger still captures them.
	"""
	module_path = frappe.db.get_value("Connector", connector_name, "handler_module")
	if not module_path:
		return {}
	module = importlib.import_module(module_path)
	return getattr(module, "HANDLERS", {}) or {}


def process_event(event_id: str) -> None:
	"""Route one persisted event through its connector's handler registry. Idempotent."""
	event = frappe.get_doc("Connector Event", event_id)
	if event.status == "Processed":
		return  # replay of a success — skip
	# Design 99: an inbound event acts as the SYSTEM on behalf of its connector;
	# the envelope id becomes the trace id so every row it causes is traceable
	# back to the message that caused it.
	frappe.set_actor("system", f"connector:{event.connector}", event.get("event_id") or event.name)

	handlers = _load_handlers(event.connector) if event.connector else {}
	handler = handlers.get(event.event_type)
	# Frappe's JSON fieldtype returns an already-parsed dict on Postgres reads
	# and a string elsewhere — tolerate both.
	data = event.payload or {}
	if isinstance(data, str):
		data = json.loads(data or "{}")
	try:
		if handler:
			handler(data, event)
			note = ""
		else:
			note = "recorded (no handler registered)"
		event.status = "Processed"
		event.failure_reason = note
		event.processed_at = frappe.utils.now_datetime()
		event.save(ignore_permissions=True)
	except Exception as exc:
		event.status = "Failed"
		event.failure_reason = f"{type(exc).__name__}: {str(exc)[:300]}"
		event.save(ignore_permissions=True)
		frappe.log_error(title=f"friday.connector handler failed: {event.connector}/{event.event_type}")
