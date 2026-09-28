# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""Generic outbound client for `system` connectors (Design 81a).

PLAIN ENGLISH
=============
A typed, never-raises POST to an external system's Frappe-style API, with auth
sourced from the Connector row's encrypted secrets (token key:secret). This is
the generalisation of the first domain client's `send` — the transport is
generic; the connector's specific endpoint names + payload shapes stay in its
domain adapter (which resolves the dotted `path` and calls us).

RELIABILITY CONTRACT (ported verbatim): `send()` never raises into a caller's
turn — failures log and return None. Calls that MUST happen eventually go
through `enqueue_send()` (the `friday` queue retries).
"""

from __future__ import annotations

import json

import requests

import frappe

_TIMEOUT_SECONDS = 20


def _connector(connector_name: str):
	"""Return the enabled Connector doc, or None if disabled/missing."""
	if not frappe.db.exists("Connector", connector_name):
		return None
	connector = frappe.get_cached_doc("Connector", connector_name)
	if not connector.enabled:
		return None
	return connector


def _headers(connector) -> dict:
	# get_password RAISES (not None) when the encrypted field was never set —
	# an unconfigured secret must mean "send unauthenticated", not "crash".
	try:
		secret = connector.get_password("api_secret") or ""
	except Exception:
		secret = ""
	return {"Authorization": f"token {connector.api_key}:{secret}"}


# Outbound identity-proof header — the reverse-direction twin of the inbound
# `X-RP-Signature` seam. When the Connector carries an `outbound_signing_secret`,
# every JSON call is signed over the EXACT raw bytes sent, so the external system
# can verify "this caller really is Friday" beyond the token auth. (Found live:
# A consuming system's inbound guard (e.g. a `_require_friday()`) gates its writes on this header;
# without it every attach_deliverable/request_gate_open/get_project 403'd.)
SIGNATURE_HEADER_OUT = "X-Friday-Signature"


def _outbound_signature(secret: str, raw_body: bytes, endpoint: str = "") -> str:
	"""`t=<unix>,v1=HMAC(secret,"t."+body),v2=HMAC(secret,"t."+endpoint+"."+body)`.

	v2 binds the endpoint. v1 covered the body alone, so within the freshness
	window a captured signed request stayed valid at any other method that
	accepted the same body shape — a signed `post_project_note` replayed at
	`request_gate_open`, because `{"project": "X"}` is a legal body for both.
	The signature proved who was calling and what they said, never what they
	were calling.

	Both are sent while the two benches change over, so they can be deployed in
	either order: a receiver that only knows v1 ignores v2; one that knows v2
	prefers it.
	"""
	import hashlib
	import hmac
	import time

	t = int(time.time())
	v1 = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
	if not endpoint:
		return f"t={t},v1={v1}"
	v2 = hmac.new(secret.encode(), f"{t}.{endpoint}.".encode() + raw_body, hashlib.sha256).hexdigest()
	return f"t={t},v1={v1},v2={v2}"


def _signing_secret(connector) -> str:
	"""The outbound signing secret, or "" when unconfigured (sign only if set)."""
	try:
		return connector.get_password("outbound_signing_secret") or ""
	except Exception:
		return ""


def send(connector_name: str, path: str, payload: dict, files: dict | None = None) -> dict | None:
	"""POST one external API call through `connector_name`. NEVER raises.

	`path` is the fully-resolved dotted endpoint (the domain adapter handles any
	module prefixing). Full URL: {api_base_url}/api/method/{path}.

	When the connector has an `outbound_signing_secret`, JSON calls are signed:
	we serialize the body OURSELVES and post those exact bytes, because the
	signature must cover the raw body verbatim — letting `requests` re-serialize
	would break the HMAC. Multipart (files) calls are not signed (no canonical
	byte representation; the receiving side's stock upload endpoint is ungated).
	"""
	connector = _connector(connector_name)
	if connector is None:
		frappe.logger("friday.connector").warning(
			f"Connector {connector_name!r} disabled/missing — dropped outbound {path!r}"
		)
		return None
	url = f"{(connector.api_base_url or '').rstrip('/')}/api/method/{path}"
	try:
		headers = _headers(connector)
		if files:
			response = requests.post(
				url, data=payload, files=files, headers=headers, timeout=_TIMEOUT_SECONDS
			)
		else:
			secret = _signing_secret(connector)
			if secret:
				body = json.dumps(payload or {}).encode("utf-8")
				# The endpoint is the URL's path, which is what the receiver
				# reads back as frappe.request.path — nginx proxies without
				# rewriting, so both ends compute the same string.
				from urllib.parse import urlparse

				headers[SIGNATURE_HEADER_OUT] = _outbound_signature(secret, body, urlparse(url).path)
				headers["Content-Type"] = "application/json"
				response = requests.post(url, data=body, headers=headers, timeout=_TIMEOUT_SECONDS)
			else:
				response = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT_SECONDS)
		response.raise_for_status()
		return response.json()
	except Exception as exc:
		frappe.logger("friday.connector").warning(
			f"Outbound {path!r} on {connector_name!r} failed: {type(exc).__name__}"
		)
		frappe.log_error(title=f"friday.connector outbound failed: {connector_name}/{path}")
		return None


def enqueue_send(connector_name: str, path: str, payload: dict) -> None:
	"""Fire-and-forget with the queue's retry semantics (for must-deliver calls)."""
	frappe.enqueue(
		"frappe.friday_core.connectors.client.send",
		connector_name=connector_name,
		path=path,
		payload=payload,
		queue="friday",
		timeout=120,
		enqueue_after_commit=True,
	)
