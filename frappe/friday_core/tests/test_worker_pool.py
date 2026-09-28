# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for the flag-gated concurrent friday worker (worker_pool).

Covers the two pieces of behaviour that matter:

1. ``resolve_concurrency`` — turns the ``max_concurrent_turns`` setting (or a
   CLI override) into a safe worker count: default 1, clamped to 1..ceiling,
   and degrading to 1 on any read error so worker start-up can never crash.
2. ``start_friday_worker`` — dispatches to Frappe's single-worker
   ``start_worker`` when the count is 1 (today's behaviour, unchanged) and to
   the ``start_worker_pool`` when it is >1.

Plus a file-level check that the Agent Settings DocType carries the new field
defaulting to "1" (flag off).

Pure unit tests — ``frappe`` and the background-job helpers are mocked, so no
live site is needed (same shape as test_agent_role).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from frappe.friday_core.agent_runner import worker_pool

_SETTINGS_GETTER = "frappe.friday_core.doctype.agent_settings.agent_settings.get_agent_settings"


def _settings_returning(value):
	"""Build a fake get_agent_settings() whose .get('max_concurrent_turns') == value."""
	doc = MagicMock()
	doc.get.return_value = value
	getter = MagicMock(return_value=doc)
	return getter


class TestResolveConcurrency(unittest.TestCase):
	"""resolve_concurrency: setting -> safe, clamped worker count."""

	def test_default_when_setting_blank(self):
		with patch(_SETTINGS_GETTER, _settings_returning("")):
			self.assertEqual(worker_pool.resolve_concurrency(), 1)

	def test_default_when_setting_none(self):
		with patch(_SETTINGS_GETTER, _settings_returning(None)):
			self.assertEqual(worker_pool.resolve_concurrency(), 1)

	def test_reads_setting_value(self):
		with patch(_SETTINGS_GETTER, _settings_returning(8)):
			self.assertEqual(worker_pool.resolve_concurrency(), 8)

	def test_reads_string_setting_value(self):
		# Frappe Int fields can surface as strings; resolve must coerce.
		with patch(_SETTINGS_GETTER, _settings_returning("5")):
			self.assertEqual(worker_pool.resolve_concurrency(), 5)

	def test_clamps_below_one(self):
		with patch(_SETTINGS_GETTER, _settings_returning(0)):
			self.assertEqual(worker_pool.resolve_concurrency(), 1)

	def test_clamps_above_ceiling(self):
		with patch(_SETTINGS_GETTER, _settings_returning(9999)):
			self.assertEqual(worker_pool.resolve_concurrency(), worker_pool.CONCURRENCY_HARD_CEILING)

	def test_override_takes_precedence_over_setting(self):
		# An explicit override must NOT read the setting at all.
		getter = _settings_returning(2)
		with patch(_SETTINGS_GETTER, getter):
			self.assertEqual(worker_pool.resolve_concurrency(override=4), 4)
		getter.assert_not_called()

	def test_override_is_clamped(self):
		self.assertEqual(worker_pool.resolve_concurrency(override=1000), worker_pool.CONCURRENCY_HARD_CEILING)

	def test_read_error_degrades_to_default(self):
		boom = MagicMock(side_effect=RuntimeError("db down"))
		with patch(_SETTINGS_GETTER, boom), patch.object(worker_pool, "frappe"):
			self.assertEqual(worker_pool.resolve_concurrency(), 1)


class TestStartFridayWorker(unittest.TestCase):
	"""start_friday_worker: single worker at 1, pool above 1."""

	def test_single_worker_when_concurrency_one(self):
		with (
			patch("frappe.utils.background_jobs.start_worker") as sw,
			patch("frappe.utils.background_jobs.start_worker_pool") as swp,
			patch.object(worker_pool, "frappe"),
		):
			worker_pool.start_friday_worker(concurrency=1)
		sw.assert_called_once_with(queue=worker_pool.FRIDAY_QUEUE, quiet=False)
		swp.assert_not_called()

	def test_pool_when_concurrency_above_one(self):
		with (
			patch("frappe.utils.background_jobs.start_worker") as sw,
			patch("frappe.utils.background_jobs.start_worker_pool") as swp,
			patch.object(worker_pool, "frappe"),
		):
			worker_pool.start_friday_worker(concurrency=6)
		swp.assert_called_once_with(queue=worker_pool.FRIDAY_QUEUE, num_workers=6, quiet=False)
		sw.assert_not_called()

	def test_pool_count_is_clamped(self):
		with (
			patch("frappe.utils.background_jobs.start_worker"),
			patch("frappe.utils.background_jobs.start_worker_pool") as swp,
			patch.object(worker_pool, "frappe"),
		):
			worker_pool.start_friday_worker(concurrency=10_000)
		swp.assert_called_once_with(
			queue=worker_pool.FRIDAY_QUEUE,
			num_workers=worker_pool.CONCURRENCY_HARD_CEILING,
			quiet=False,
		)


class TestAgentSettingsField(unittest.TestCase):
	"""The max_concurrent_turns Int field exists and defaults to '1' (flag off)."""

	def _load(self):
		p = Path(__file__).resolve().parents[1] / "doctype" / "agent_settings" / "agent_settings.json"
		return json.loads(p.read_text())

	def test_field_present_and_defaults_to_one(self):
		doc = self._load()
		field = next((f for f in doc["fields"] if f.get("fieldname") == "max_concurrent_turns"), None)
		self.assertIsNotNone(field, "max_concurrent_turns field missing from Agent Settings")
		self.assertEqual(field["fieldtype"], "Int")
		self.assertEqual(field["default"], "1")
		self.assertIn("max_concurrent_turns", doc["field_order"])


if __name__ == "__main__":
	unittest.main()
