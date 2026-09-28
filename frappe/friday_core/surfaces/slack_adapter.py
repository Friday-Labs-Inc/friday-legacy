# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
The Slack surface adapter — Friday as a 2nd chat surface (Design 90).

PLAIN ENGLISH
=============
Proves the unified-gateway claim that surfaces are interchangeable: this mirrors
`raven_adapter.py` almost line-for-line, only the transport differs (Slack's
Events API webhook in / Web API out instead of Raven DocType hooks).

  INBOUND   Slack POSTs an event to `receive_event` (a webhook). We verify the
            Slack signature, apply the same DM/@mention guardrails as Raven, then
            write ONE inbound Chat Message row (platform "slack", session = the
            Slack channel id). Gateway v2 does the rest.

  OUTBOUND  the gateway writes an outbound Chat Message row for platform "slack"
            → `handle_outbound_to_slack` posts it back via Slack `chat.postMessage`.

  COMMANDS  a leading "/" is caught at the edge and routed through the shared
            `gateway.commands.dispatch_command` (Design 82) — so Slack gets
            /approve /deny /stop /steer /status /help for free.

SECRETS live in the encrypted `Slack Config` single (Password fields), never in
JSON. The webhook is guest-reachable by design: the Slack signature IS the auth.

This module never imports `agent_runner` (design 47 §9 — the one rule).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import frappe

SLACK_PLATFORM = "slack"
_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
# Slack requires the signed timestamp to be within 5 minutes (replay defence).
_SIG_TOLERANCE_SECONDS = 60 * 5


# ---------------------------------------------------------------------------
# INBOUND — Slack Events API webhook → Chat Message row
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_event():
	"""POST /api/method/frappe.friday_core.surfaces.slack_adapter.receive_event

	The Slack Events API endpoint. Guest-reachable; the Slack signature is the
	authentication. Acks fast (Slack's 3s budget) — the gateway worker does the
	real work asynchronously off the Chat Message row we insert.
	"""
	raw = frappe.request.get_data() if frappe.request else b""
	headers = frappe.request.headers if frappe.request else {}

	if not _verify_signature(raw, headers):
		frappe.local.response["http_status_code"] = 403
		return {"ok": False, "error": "bad signature"}

	try:
		body = json.loads(raw or b"{}")
	except (ValueError, TypeError):
		return {"ok": False}

	# Slack's one-time endpoint handshake.
	if body.get("type") == "url_verification":
		return {"challenge": body.get("challenge")}

	if body.get("type") == "event_callback":
		try:
			_handle_event(body.get("event") or {})
		except Exception:
			frappe.log_error(title="friday.slack receive_event failed")

	# Always 200 fast so Slack doesn't retry.
	return {"ok": True}


def _handle_event(event: dict) -> None:
	"""Apply guardrails, then route a Slack message event (command or inbound row)."""
	# Only plain user messages. Skip the bot's own posts + any bot/subtype event
	# (edits, joins, bot_message) — no self-loops (mirrors Raven's is_bot_message).
	if event.get("type") != "message" or event.get("subtype") or event.get("bot_id"):
		return

	text = (event.get("text") or "").strip()
	channel = event.get("channel")
	user = event.get("user")
	if not text or not channel or not user:
		return

	bot_user_id = _bot_user_id()
	if user == bot_user_id:
		return  # our own message echoed back

	# Guardrails (mirror Raven Q3): DMs (channel_type "im") always answered;
	# channels only when the bot is explicitly @mentioned (Slack encodes a mention
	# as `<@BOTID>` in the text).
	is_dm = event.get("channel_type") == "im"
	if not is_dm:
		if not bot_user_id or f"<@{bot_user_id}>" not in text:
			return

	# Design 82 — slash commands caught at the edge, before a conversational row.
	from frappe.friday_core.gateway.commands import is_command

	if is_command(text):
		_handle_command(channel, user, text)
		return

	profile = _resolve_profile(channel)
	if not profile:
		frappe.logger("friday.slack").warning(
			"Slack message received but no agent profile resolves for the 'slack' "
			"Chat Platform — set Slack Config.default_agent_profile."
		)
		return

	# The row IS the handoff — gateway v2 routes it to the friday worker.
	frappe.get_doc(
		{
			"doctype": "Chat Message",
			"session_id": channel,
			"platform": SLACK_PLATFORM,
			"direction": "inbound",
			"sender_id": user,
			"agent_profile": profile,
			"content": text,
			"timestamp": frappe.utils.now_datetime(),
			"processed": 0,
		}
	).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# OUTBOUND — Chat Message row → Slack post
# ---------------------------------------------------------------------------


def handle_outbound_to_slack(doc, method=None) -> None:
	"""`Chat Message.after_insert` hook: post slack-bound replies to Slack.

	Runs for EVERY Chat Message insert (alongside the gateway + raven hooks) and
	returns immediately unless this is an outbound row for the slack platform.
	Best-effort with a savepoint — a Slack API failure must never break the
	gateway pipeline that wrote the row.
	"""
	if doc.direction != "outbound" or doc.platform != SLACK_PLATFORM:
		return
	# Mirror rows are recorded for the agent's own history only — never re-posted
	# (the content already went out out-of-band). See gateway/mirror.py.
	if doc.is_mirror:
		return

	token = _bot_token()
	if not token:
		return  # surface not configured

	try:
		frappe.db.savepoint("friday_slack_post")
		import requests

		requests.post(
			_SLACK_POST_URL,
			headers={"Authorization": f"Bearer {token}"},
			json={"channel": doc.session_id, "text": doc.content or ""},
			timeout=10,
		)
	except Exception:
		frappe.db.rollback(save_point="friday_slack_post")
		frappe.log_error(title="friday.slack outbound post failed")


# ---------------------------------------------------------------------------
# Commands (Design 82) — mirror raven_adapter
# ---------------------------------------------------------------------------


def _handle_command(channel: str, user: str, content: str) -> None:
	"""Dispatch a slash command + audit it as two `is_command` rows.

	The reply (outbound) row's insert fires `handle_outbound_to_slack`, which
	posts it to Slack. Both rows carry `is_command=1` so the gateway skips them.
	"""
	from frappe.friday_core.gateway.commands import dispatch_command

	result = dispatch_command(platform=SLACK_PLATFORM, session_id=channel, user=user, raw=content)
	_insert_command_row(channel, sender_id=user, content=content, direction="inbound")
	_insert_command_row(channel, sender_id="system", content=result.reply, direction="outbound")


def _insert_command_row(channel: str, sender_id: str, content: str, direction: str) -> None:
	"""Insert one `is_command=1` Chat Message row (processed, never run)."""
	frappe.get_doc(
		{
			"doctype": "Chat Message",
			"session_id": channel,
			"platform": SLACK_PLATFORM,
			"direction": direction,
			"sender_id": sender_id,
			"content": content,
			"timestamp": frappe.utils.now_datetime(),
			"processed": 1,
			"is_command": 1,
		}
	).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Signature + config helpers
# ---------------------------------------------------------------------------


def _verify_signature(raw_body: bytes, headers) -> bool:
	"""Verify Slack's `X-Slack-Signature` (HMAC-SHA256 of `v0:{ts}:{body}`).

	Constant-time compare; rejects stale timestamps (replay defence). Returns
	False on any missing piece. Modelled on `connectors.core.verify_signature`,
	adapted to Slack's exact scheme.
	"""
	secret = _signing_secret()
	if not secret:
		return False
	try:
		sig = headers.get("X-Slack-Signature")
		ts = headers.get("X-Slack-Request-Timestamp")
	except Exception:
		return False
	if not sig or not ts:
		return False
	try:
		if abs(time.time() - int(ts)) > _SIG_TOLERANCE_SECONDS:
			return False
	except (ValueError, TypeError):
		return False

	body = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else (raw_body or "")
	basestring = f"v0:{ts}:{body}".encode()
	expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
	return hmac.compare_digest(expected, sig)


def _config():
	"""The `Slack Config` single, or None if the doctype isn't present yet."""
	if not frappe.db.exists("DocType", "Slack Config"):
		return None
	return frappe.get_cached_doc("Slack Config")


def _signing_secret() -> str | None:
	# raise_exception=False — `get_password` RAISES on an UNSET Password field, and
	# bootstrap creates an empty Slack Config on migrate. Before the operator fills
	# it in, an unset secret must read as None (→ verify fails → clean 403), not 500.
	cfg = _config()
	return cfg.get_password("signing_secret", raise_exception=False) if cfg else None


def _bot_token() -> str | None:
	cfg = _config()
	return cfg.get_password("bot_token", raise_exception=False) if cfg else None


def _bot_user_id() -> str | None:
	cfg = _config()
	return (cfg.bot_user_id or None) if cfg else None


def _resolve_profile(channel: str | None = None) -> str | None:
	"""The agent behind the bot for this Slack channel (project lead → default)."""
	from frappe.friday_core.routing.resolve import resolve_profile

	return resolve_profile(platform=SLACK_PLATFORM, chat_id=channel)
