# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for provider failover (Design 94).

Mock-based — no DB, same harness as test_react_loop.py / test_turn_journal.py.
Pins the failover contract:

  - a failed primary hands the SAME turn to its fallback provider, which
    answers with ITS OWN default model (model=None on the hop)
  - the hop is journaled (provider.failover) and the diary replay resets the
    pinned model so a resumed turn re-resolves fresh
  - no chain configured = today's behavior (the error propagates)
  - a cycle (A→B→A) or hop budget exhaustion stops cleanly with the error
  - usage is recorded against the provider that actually served the call
"""

import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.agent_runner.journal import (
	EVENT_PROVIDER_FAILOVER,
	EVENT_TURN_STARTED,
	rebuild,
)
from frappe.friday_core.llm.provider import LLMError

_R = "frappe.friday_core.agent_runner.runner"

_PROFILE = "test-profile"
_BASE_MESSAGES = [{"role": "user", "content": "hi"}]


def _resp(content="", tool_calls=None):
	return {
		"content": content,
		"finish_reason": "tool_calls" if tool_calls else "stop",
		"usage": {"total_tokens": 7},
		"tool_calls": tool_calls,
	}


def _provider(name, *responses):
	p = MagicMock()
	p.source_row_name = name
	if len(responses) == 1:
		p.chat.return_value = responses[0]
	else:
		p.chat.side_effect = list(responses)
	return p


def _failing_provider(name, exc=None):
	p = MagicMock()
	p.source_row_name = name
	p.chat.side_effect = exc or LLMError("minimax call failed: rate_limit (HTTP 429) after 3 attempt(s).")
	return p


class _FakeJournal:
	preloaded: list = []
	instances: list = []

	def __init__(self, turn_id, session_id, agent_profile):
		self.turn_id = turn_id
		self.events = list(self.__class__.preloaded)
		self.recorded = []
		self.__class__.instances.append(self)

	@classmethod
	def open(cls, turn_id, session_id, agent_profile):
		return cls(turn_id, session_id, agent_profile)

	def record(self, event_type, payload):
		self.recorded.append((event_type, payload))
		self.events.append({"event_type": event_type, "payload": payload})

	def completed_reply(self):
		return None

	def is_delivered(self):
		return False

	def rebuild(self, profile_name, inject_steer):
		return rebuild(self.events, profile_name, inject_steer)


def _run(primary, fallbacks=None, turn_id=None):
	"""run_turn with all externals patched; `fallbacks` scripts the chain."""
	from frappe.friday_core.agent_runner.runner import run_turn

	_FakeJournal.preloaded = []
	_FakeJournal.instances = []
	prompt = {"messages": list(_BASE_MESSAGES), "tools": [], "model": "primary-model"}
	chain = list(fallbacks or [])

	def next_fallback(provider):
		return chain.pop(0) if chain else None

	with (
		patch(f"{_R}.maybe_compress_session"),
		patch(f"{_R}.load_for_profile", return_value=[]),
		patch(f"{_R}.build", return_value=prompt),
		patch(f"{_R}.get_provider_for_profile", return_value=primary),
		patch(f"{_R}.get_fallback_provider", side_effect=next_fallback) as mf,
		patch(f"{_R}.dispatch"),
		patch(f"{_R}._is_permission_denial", return_value=False),
		patch(f"{_R}.record_usage") as mu,
		patch(f"{_R}.emit") as me,
		patch(f"{_R}.clear_interrupt"),
		patch(f"{_R}.clear_steer"),
		patch(f"{_R}.is_interrupt_requested", return_value=False),
		patch(f"{_R}.drain_steer", return_value=None),
		patch(f"{_R}.TurnJournal", _FakeJournal),
	):
		result = run_turn(_PROFILE, "sess-1", "hi", turn_id=turn_id)
	journal = _FakeJournal.instances[0] if _FakeJournal.instances else None
	return result, mu, me, journal, mf


class TestProviderFailover(unittest.TestCase):
	def test_failed_primary_hands_the_turn_to_the_fallback(self):
		primary = _failing_provider("Minimax")
		backup = _provider("Codex", _resp(content="rescued"))
		result, mu, me, _, _ = _run(primary, fallbacks=[backup])

		self.assertEqual(result, "rescued")
		primary.chat.assert_called_once()
		backup.chat.assert_called_once()
		# Q3 — the backup runs its OWN default model
		self.assertIsNone(backup.chat.call_args.kwargs["model"])
		# Q5 — usage attributed to the provider that actually served
		self.assertIs(mu.call_args.kwargs["provider"], backup)
		# Q5 — the hop is observable
		self.assertEqual(me.call_args.kwargs.get("event_type") or me.call_args.args[0], "llm.failover")

	def test_no_chain_means_the_error_propagates_as_today(self):
		primary = _failing_provider("Minimax")
		with self.assertRaises(LLMError):
			_run(primary, fallbacks=[])

	def test_hop_is_journaled_and_replay_resets_the_model(self):
		primary = _failing_provider("Minimax")
		backup = _provider("Codex", _resp(content="rescued"))
		_, _, _, journal, _ = _run(primary, fallbacks=[backup], turn_id="CM-0001")

		types = [e for e, _ in journal.recorded]
		self.assertIn(EVENT_PROVIDER_FAILOVER, types)
		hop = dict(journal.recorded)[EVENT_PROVIDER_FAILOVER]
		self.assertEqual(hop["from"], "Minimax")
		self.assertEqual(hop["to"], "Codex")
		self.assertTrue(hop["reason"])

		# Q4 — replay after a failover must NOT pin the (possibly foreign)
		# model; the resumed turn re-resolves fresh.
		events = [
			{
				"event_type": EVENT_TURN_STARTED,
				"payload": {
					"messages": list(_BASE_MESSAGES),
					"model": "primary-model",
					"agent_profile": _PROFILE,
				},
			},
			{
				"event_type": EVENT_PROVIDER_FAILOVER,
				"payload": {"from": "Minimax", "to": "Codex", "reason": "rate_limit"},
			},
		]
		state = rebuild(events, _PROFILE, lambda m, t: None)
		self.assertIsNone(state.model)

	def test_chain_is_transitive(self):
		primary = _failing_provider("Minimax")
		second = _failing_provider("Codex")
		third = _provider("Claude", _resp(content="third time lucky"))
		result, _, _, _, _ = _run(primary, fallbacks=[second, third])
		self.assertEqual(result, "third time lucky")

	def test_cycle_stops_cleanly(self):
		primary = _failing_provider("Minimax")
		looper = _failing_provider("Minimax")  # chain points back at the primary
		with self.assertRaises(LLMError):
			_run(primary, fallbacks=[looper])
		# the cycle guard refused the revisit: the duplicate never ran
		looper.chat.assert_not_called()

	def test_hop_budget_caps_a_long_chain(self):
		primary = _failing_provider("P0")
		chain = [_failing_provider(f"P{i}") for i in range(1, 5)]
		with self.assertRaises(LLMError):
			_run(primary, fallbacks=chain)
		# 3-hop cap: P1..P3 tried, P4 never reached
		self.assertEqual(chain[3].chat.call_count, 0)


class TestFallbackResolution(unittest.TestCase):
	"""get_fallback_provider follows the row's link; missing/inactive → None."""

	def test_follows_the_link(self):
		from frappe.friday_core.llm.provider import get_fallback_provider

		primary = MagicMock()
		primary.fallback_provider_name = "Codex"
		backup = MagicMock()
		with patch("frappe.friday_core.llm.provider.get_provider_by_name", return_value=backup) as mg:
			out = get_fallback_provider(primary)
		mg.assert_called_once_with("Codex")
		self.assertIs(out, backup)

	def test_no_link_returns_none(self):
		from frappe.friday_core.llm.provider import get_fallback_provider

		primary = MagicMock()
		primary.fallback_provider_name = None
		self.assertIsNone(get_fallback_provider(primary))

	def test_broken_link_returns_none(self):
		from frappe.friday_core.llm.provider import get_fallback_provider

		primary = MagicMock()
		primary.fallback_provider_name = "Gone"
		with patch(
			"frappe.friday_core.llm.provider.get_provider_by_name",
			side_effect=LLMError("LLM Provider 'Gone' not found."),
		):
			self.assertIsNone(get_fallback_provider(primary))


if __name__ == "__main__":
	unittest.main()
