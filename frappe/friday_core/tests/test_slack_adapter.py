# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the Slack surface adapter (Design 90). Mock-based — no real Slack.

Pins: signature verification (good/bad/stale); the DM/@mention guardrails; bot's
own messages skipped; a slash command routes to dispatch_command and does NOT
become a conversational row; inbound writes a correct Chat Message row; outbound
posts via the Slack client inside a savepoint and never raises.
"""

import hashlib
import hmac
import time
import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.surfaces import slack_adapter

_SA = "frappe.friday_core.surfaces.slack_adapter"


def _msg_event(**over):
	e = {"type": "message", "channel": "C1", "user": "U_HUMAN", "text": "hello", "channel_type": "im"}
	e.update(over)
	return e


class TestSignature(unittest.TestCase):
	def _sig(self, secret, ts, body):
		return "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()

	@patch(f"{_SA}._signing_secret", return_value="shhh")
	def test_valid_signature_passes(self, _s):
		ts = str(int(time.time()))
		body = b'{"type":"x"}'
		headers = {"X-Slack-Signature": self._sig("shhh", ts, body.decode()), "X-Slack-Request-Timestamp": ts}
		self.assertTrue(slack_adapter._verify_signature(body, headers))

	@patch(f"{_SA}._signing_secret", return_value="shhh")
	def test_bad_signature_fails(self, _s):
		ts = str(int(time.time()))
		headers = {"X-Slack-Signature": "v0=deadbeef", "X-Slack-Request-Timestamp": ts}
		self.assertFalse(slack_adapter._verify_signature(b'{"type":"x"}', headers))

	@patch(f"{_SA}._signing_secret", return_value="shhh")
	def test_stale_timestamp_fails(self, _s):
		old = str(int(time.time()) - 9999)
		body = b'{"type":"x"}'
		headers = {
			"X-Slack-Signature": self._sig("shhh", old, body.decode()),
			"X-Slack-Request-Timestamp": old,
		}
		self.assertFalse(slack_adapter._verify_signature(body, headers))


class TestInbound(unittest.TestCase):
	@patch(f"{_SA}._bot_user_id", return_value="U_BOT")
	@patch(f"{_SA}.frappe")
	def test_bot_own_message_skipped(self, fr, _b):
		slack_adapter._handle_event(_msg_event(user="U_BOT"))
		fr.get_doc.assert_not_called()

	@patch(f"{_SA}._bot_user_id", return_value="U_BOT")
	@patch(f"{_SA}.frappe")
	def test_bot_subtype_skipped(self, fr, _b):
		slack_adapter._handle_event(_msg_event(bot_id="B1"))
		slack_adapter._handle_event(_msg_event(subtype="message_changed"))
		fr.get_doc.assert_not_called()

	@patch(f"{_SA}._resolve_profile", return_value="Friday")
	@patch(f"{_SA}._bot_user_id", return_value="U_BOT")
	@patch(f"{_SA}.frappe")
	def test_channel_without_mention_skipped(self, fr, _b, _p):
		slack_adapter._handle_event(_msg_event(channel_type="channel", text="just chatting"))
		fr.get_doc.assert_not_called()

	@patch(f"{_SA}._resolve_profile", return_value="Friday")
	@patch(f"{_SA}._bot_user_id", return_value="U_BOT")
	@patch(f"{_SA}.frappe")
	def test_channel_with_mention_writes_row(self, fr, _b, _p):
		slack_adapter._handle_event(_msg_event(channel_type="channel", text="<@U_BOT> hi"))
		payload = fr.get_doc.call_args[0][0]
		self.assertEqual(payload["platform"], "slack")
		self.assertEqual(payload["direction"], "inbound")
		self.assertEqual(payload["session_id"], "C1")
		self.assertEqual(payload["agent_profile"], "Friday")

	@patch(f"{_SA}._resolve_profile", return_value="Friday")
	@patch(f"{_SA}._bot_user_id", return_value="U_BOT")
	@patch(f"{_SA}.frappe")
	def test_dm_writes_inbound_row(self, fr, _b, _p):
		slack_adapter._handle_event(_msg_event(text="what's up"))
		payload = fr.get_doc.call_args[0][0]
		self.assertEqual(payload["session_id"], "C1")
		self.assertEqual(payload["content"], "what's up")
		self.assertEqual(payload["sender_id"], "U_HUMAN")

	@patch(f"{_SA}._handle_command")
	@patch(f"{_SA}._bot_user_id", return_value="U_BOT")
	@patch(f"{_SA}.frappe")
	def test_slash_command_routed_not_conversational(self, fr, _b, mock_cmd):
		slack_adapter._handle_event(_msg_event(text="/status"))
		mock_cmd.assert_called_once_with("C1", "U_HUMAN", "/status")
		fr.get_doc.assert_not_called()  # no conversational row


class TestOutbound(unittest.TestCase):
	def _row(self, platform="slack"):
		d = MagicMock()
		d.direction = "outbound"
		d.platform = platform
		d.session_id = "C1"
		d.content = "hi from the agent"
		d.is_mirror = 0
		return d

	@patch(f"{_SA}._bot_token", return_value="xoxb-1")
	@patch(f"{_SA}.frappe")
	def test_mirror_row_is_not_posted(self, fr, _t):
		row = self._row()
		row.is_mirror = 1
		slack_adapter.handle_outbound_to_slack(row)
		_t.assert_not_called()  # never reaches the token/post path

	@patch(f"{_SA}._bot_token", return_value="xoxb-1")
	@patch(f"{_SA}.frappe")
	def test_non_slack_row_ignored(self, fr, _t):
		slack_adapter.handle_outbound_to_slack(self._row(platform="raven"))
		_t.assert_not_called()

	@patch(f"{_SA}._bot_token", return_value=None)
	@patch(f"{_SA}.frappe")
	def test_no_token_noop(self, fr, _t):
		slack_adapter.handle_outbound_to_slack(self._row())
		fr.db.savepoint.assert_not_called()

	@patch("requests.post")
	@patch(f"{_SA}._bot_token", return_value="xoxb-1")
	@patch(f"{_SA}.frappe")
	def test_posts_with_bearer_token(self, fr, _t, post):
		slack_adapter.handle_outbound_to_slack(self._row())
		kw = post.call_args.kwargs
		self.assertEqual(kw["headers"]["Authorization"], "Bearer xoxb-1")
		self.assertEqual(kw["json"]["channel"], "C1")
		self.assertEqual(kw["json"]["text"], "hi from the agent")

	@patch("requests.post", side_effect=RuntimeError("slack down"))
	@patch(f"{_SA}._bot_token", return_value="xoxb-1")
	@patch(f"{_SA}.frappe")
	def test_post_failure_rolls_back_and_never_raises(self, fr, _t, _post):
		slack_adapter.handle_outbound_to_slack(self._row())  # must not raise
		fr.db.rollback.assert_called_once_with(save_point="friday_slack_post")
		fr.log_error.assert_called_once()


class TestCommandAudit(unittest.TestCase):
	@patch("frappe.friday_core.gateway.commands.dispatch_command")
	@patch(f"{_SA}.frappe")
	def test_handle_command_writes_two_is_command_rows(self, fr, disp):
		disp.return_value = MagicMock(reply="🛑 ok")
		slack_adapter._handle_command("C1", "U_HUMAN", "/stop")
		disp.assert_called_once()
		rows = [c[0][0] for c in fr.get_doc.call_args_list]
		self.assertEqual(len(rows), 2)
		self.assertTrue(all(r["is_command"] == 1 and r["platform"] == "slack" for r in rows))
		self.assertEqual(rows[0]["direction"], "inbound")
		self.assertEqual(rows[1]["direction"], "outbound")


class TestUnconfiguredSecret(unittest.TestCase):
	"""frappe's get_password() RAISES on an UNSET Password field, and bootstrap
	creates an empty Slack Config on migrate — so an unconfigured secret must read
	as None (→ clean 403), never blow up the webhook with a 500."""

	@staticmethod
	def _cfg_with_unset_passwords():
		# Mimic frappe: raises unless raise_exception=False is passed.
		def get_password(field, raise_exception=True):
			if raise_exception:
				raise Exception("Password not found")
			return None

		doc = MagicMock()
		doc.get_password.side_effect = get_password
		return doc

	@patch(f"{_SA}._config")
	def test_unset_secret_reads_none_not_raise(self, cfg):
		cfg.return_value = self._cfg_with_unset_passwords()
		# These must NOT raise (they would if get_password were called without the flag).
		self.assertIsNone(slack_adapter._signing_secret())
		self.assertIsNone(slack_adapter._bot_token())

	@patch(f"{_SA}._config")
	def test_verify_signature_clean_false_when_unconfigured(self, cfg):
		cfg.return_value = self._cfg_with_unset_passwords()
		# A probe to the webhook before the operator configures Slack → clean reject.
		headers = {"X-Slack-Signature": "v0=abc", "X-Slack-Request-Timestamp": "123"}
		self.assertFalse(slack_adapter._verify_signature(b'{"type":"x"}', headers))


if __name__ == "__main__":
	unittest.main()
