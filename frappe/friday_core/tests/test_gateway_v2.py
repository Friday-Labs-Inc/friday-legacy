# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Unit tests for Gateway v2 — the dedicated friday worker (design 55, LOCKED).

Mock-based — no DB, no redis, no worker — so they run on the dev Mac and in
CI. They pin the LOCKED router contract:

  Q1 — default path enqueues to the dedicated queue; inline fallback when no
       friday worker is alive; "sync" dispatch_mode forces inline.
  Q2 — queue="friday", timeout=600.
  Q3 — in a worker, the first session-lock failure requeues ONCE
       (lock_retry 0 → 1); the second writes the busy message.
  Q6 — the deterministic job id is stamped onto the Chat Message row.

The pipeline body itself (lock → run_turn → outbound) is covered by the
existing bench-level gateway tests; here we only pin the v2 routing seams.
"""

import unittest
from unittest.mock import MagicMock, patch

_S = "frappe.friday_core.gateway.service"


def _inbound(name="CM-1", dispatch_unused=None):
	doc = MagicMock()
	doc.name = name
	doc.direction = "inbound"
	doc.processed = 0
	doc.agent_profile = "Friday"
	doc.platform = "cli"
	doc.session_id = "S-1"
	doc.content = "hi"
	doc.retry_count = 0
	# Design 82: handle_inbound skips command rows. A real inbound message is
	# not a command — pin doc.get("is_command") falsy so the guard passes.
	doc.get.return_value = 0
	return doc


class TestRouterWorkerPath(unittest.TestCase):
	def test_async_with_worker_enqueues_to_friday_queue(self):
		# Q1 + Q2: worker alive → enqueue(queue="friday", timeout=600), no inline run.
		from frappe.friday_core.gateway import service

		doc = _inbound()
		with (
			patch(f"{_S}._get_dispatch_mode", return_value="async"),
			patch(f"{_S}._friday_worker_alive", return_value=True),
			patch(f"{_S}._run_pipeline") as mock_inline,
			patch(f"{_S}.frappe") as mock_frappe,
		):
			service.handle_inbound(doc)

		mock_inline.assert_not_called()
		mock_frappe.enqueue.assert_called_once()
		kwargs = mock_frappe.enqueue.call_args.kwargs
		self.assertEqual(kwargs["queue"], "friday")
		self.assertEqual(kwargs["timeout"], 600)
		self.assertEqual(kwargs["row_name"], "CM-1")
		self.assertEqual(kwargs["lock_retry"], 0)
		self.assertTrue(kwargs["enqueue_after_commit"])

	def test_job_id_is_deterministic_and_stamped_on_row(self):
		# Q6: job id derivable from the row, written via set_value.
		from frappe.friday_core.gateway import service

		doc = _inbound(name="CM-42")
		with (
			patch(f"{_S}._get_dispatch_mode", return_value="async"),
			patch(f"{_S}._friday_worker_alive", return_value=True),
			patch(f"{_S}._run_pipeline"),
			patch(f"{_S}.frappe") as mock_frappe,
		):
			service.handle_inbound(doc)

		expected_id = "friday-gw::CM-42::lock0"
		self.assertEqual(mock_frappe.enqueue.call_args.kwargs["job_id"], expected_id)
		mock_frappe.db.set_value.assert_called_once_with(
			"Chat Message", "CM-42", "job_id", expected_id, update_modified=False
		)

	def test_async_without_worker_falls_back_inline(self):
		# Q1 fallback: no worker alive → pipeline runs inline, nothing enqueued.
		from frappe.friday_core.gateway import service

		doc = _inbound()
		with (
			patch(f"{_S}._get_dispatch_mode", return_value="async"),
			patch(f"{_S}._friday_worker_alive", return_value=False),
			patch(f"{_S}._run_pipeline") as mock_inline,
			patch(f"{_S}.frappe") as mock_frappe,
		):
			service.handle_inbound(doc)

		mock_inline.assert_called_once_with(doc)
		mock_frappe.enqueue.assert_not_called()

	def test_sync_override_forces_inline_without_worker_check(self):
		# "sync" = explicit operator/test override — never touches the queue.
		from frappe.friday_core.gateway import service

		doc = _inbound()
		with (
			patch(f"{_S}._get_dispatch_mode", return_value="sync"),
			patch(f"{_S}._friday_worker_alive") as mock_alive,
			patch(f"{_S}._run_pipeline") as mock_inline,
			patch(f"{_S}.frappe") as mock_frappe,
		):
			service.handle_inbound(doc)

		mock_inline.assert_called_once_with(doc)
		mock_alive.assert_not_called()
		mock_frappe.enqueue.assert_not_called()

	def test_outbound_and_processed_rows_are_ignored(self):
		from frappe.friday_core.gateway import service

		outbound = _inbound()
		outbound.direction = "outbound"
		done = _inbound()
		done.processed = 1
		with patch(f"{_S}._run_pipeline") as mock_inline, patch(f"{_S}.frappe") as mock_frappe:
			service.handle_inbound(outbound)
			service.handle_inbound(done)
		mock_inline.assert_not_called()
		mock_frappe.enqueue.assert_not_called()


class TestBusySessionQueues(unittest.TestCase):
	"""Design 80 queue phase — a busy session QUEUES the message, never drops it."""

	def _run_with_lock_denied(self, in_worker, lock_retry):
		from frappe.friday_core.gateway import service

		doc = _inbound()
		lock = MagicMock()
		lock.acquire.return_value = False  # session lock denied
		with (
			patch(f"{_S}.frappe") as mock_frappe,
			patch(f"{_S}.flush_batch", side_effect=AssertionError("pipeline must not run")),
			patch(f"{_S}._enqueue_pipeline") as mock_enqueue,
			patch(f"{_S}._write_outbound") as mock_out,
			patch(f"{_S}._mark_processed") as mock_mark,
			patch(f"{_S}._drain_next_in_session") as mock_drain,
		):
			mock_frappe.cache.return_value.lock.return_value = lock
			service._run_pipeline(doc, in_worker=in_worker, lock_retry=lock_retry)
		return mock_enqueue, mock_out, mock_mark, mock_drain

	def test_busy_leaves_row_unprocessed_no_drop(self):
		# The whole point: nothing is written, nothing is marked processed —
		# the row stays processed=0 so the holder's drain / sweeper re-runs it.
		for in_worker in (True, False):
			_enqueue, out, mark, drain = self._run_with_lock_denied(in_worker, lock_retry=0)
			self.assertFalse(out.called, "no busy message — message is queued, not rejected")
			self.assertFalse(mark.called, "row must stay processed=0")
			# busy path returns before the try/finally, so no drain on this path
			self.assertFalse(drain.called)


class TestDrainNextInSession(unittest.TestCase):
	"""The drain re-dispatches the oldest queued inbound after the lock frees."""

	def test_enqueues_oldest_unprocessed_when_worker_alive(self):
		from frappe.friday_core.gateway import service

		with (
			patch(f"{_S}.frappe") as mock_frappe,
			patch(f"{_S}._friday_worker_alive", return_value=True),
			patch(f"{_S}._enqueue_pipeline") as mock_enqueue,
		):
			mock_frappe.get_all.return_value = [{"name": "CM-NEXT"}]
			service._drain_next_in_session("sess-1")
		mock_enqueue.assert_called_once_with("CM-NEXT", lock_retry=0)

	def test_noop_when_nothing_queued(self):
		from frappe.friday_core.gateway import service

		with (
			patch(f"{_S}.frappe") as mock_frappe,
			patch(f"{_S}._friday_worker_alive", return_value=True),
			patch(f"{_S}._enqueue_pipeline") as mock_enqueue,
		):
			mock_frappe.get_all.return_value = []
			service._drain_next_in_session("sess-1")
		mock_enqueue.assert_not_called()

	def test_noop_when_no_worker(self):
		from frappe.friday_core.gateway import service

		with (
			patch(f"{_S}.frappe") as mock_frappe,
			patch(f"{_S}._friday_worker_alive", return_value=False),
			patch(f"{_S}._enqueue_pipeline") as mock_enqueue,
		):
			mock_frappe.get_all.return_value = [{"name": "CM-NEXT"}]
			service._drain_next_in_session("sess-1")
		mock_enqueue.assert_not_called()


class TestDoubleDispatchGuard(unittest.TestCase):
	"""A row already processed while we waited on the lock is skipped (no dup reply)."""

	def test_skips_already_processed_after_lock(self):
		from frappe.friday_core.gateway import service

		doc = _inbound()
		lock = MagicMock()
		lock.acquire.return_value = True  # we GET the lock...
		with (
			patch(f"{_S}.frappe") as mock_frappe,
			patch(f"{_S}.flush_batch", side_effect=AssertionError("turn must not run")),
			patch(f"{_S}._drain_next_in_session"),
		):
			mock_frappe.cache.return_value.lock.return_value = lock
			mock_frappe.db.get_value.return_value = 1  # ...but the row is already processed
			service._run_pipeline(doc, in_worker=True, lock_retry=0)
		lock.release.assert_called_once()  # released the lock, ran no turn


class TestWorkerAliveDetection(unittest.TestCase):
	def test_any_infra_failure_means_inline_fallback(self):
		# Unconfigured queue / dead redis must read as "no worker", never raise.
		from frappe.friday_core.gateway import service

		with patch(
			"frappe.utils.background_jobs.get_queue",
			side_effect=Exception("queue 'friday' not configured"),
		):
			self.assertFalse(service._friday_worker_alive())


if __name__ == "__main__":
	unittest.main()
