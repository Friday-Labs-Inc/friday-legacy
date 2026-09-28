# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""A2A task lifecycle store — lightweight, Redis-backed (Design 92, v1).

PLAIN ENGLISH
=============
An A2A `message/send` creates a task; a caller can later poll it with `tasks/get`
or stop it with `tasks/cancel`. For v1 these are SHORT request/response turns (sync
dispatch — the agent reply comes back in the same HTTP call), so the task's state is
kept in Redis keyed by the A2A taskId — deliberately NOT the heavy Friday `Task`
doctype, which is project-pipeline machinery (dispatcher, reconciler, dependency
graph). When A2A is later used to delegate real *project* work, THAT is when a taskId
maps onto a Friday Task with full lifecycle states; a conversational turn doesn't need
that weight.

States (A2A spec):  submitted → working → completed | failed | canceled.

The store is a thin wrapper over `frappe.cache()` so it is trivially fakeable in tests
(inject any object with get_value/set_value); the server core takes an injected store.
"""

from __future__ import annotations

import frappe

# One record per task, JSON-able dict, expired after a day (a polled task is short-lived).
_KEY = "friday:a2a:task:{id}"
_TTL_SECONDS = 24 * 60 * 60

# A2A task lifecycle states (spec spelling — "canceled", one l).
SUBMITTED = "submitted"
WORKING = "working"
COMPLETED = "completed"
FAILED = "failed"
CANCELED = "canceled"

# Terminal states never transition further; cancel on a terminal task is a no-op.
TERMINAL = frozenset({COMPLETED, FAILED, CANCELED})


class A2ATaskStore:
	"""Redis-backed A2A task records — one dict per taskId, TTL-expired.

	`cache` is injectable (defaults to `frappe.cache()`), so a unit test can pass a
	plain dict-backed fake instead of standing up Redis.
	"""

	def __init__(self, cache=None):
		self._cache = cache if cache is not None else frappe.cache()

	def create(self, task_id: str, message: str) -> dict:
		"""Record a freshly-received task in the SUBMITTED state."""
		return self._write(
			{"id": task_id, "state": SUBMITTED, "message": message, "artifacts": [], "error": None}
		)

	def set_state(self, task_id: str, state: str, *, artifacts=None, error=None) -> dict | None:
		"""Move a known task to `state` (optionally attaching artifacts / an error).

		Returns the updated record, or None if the task id is unknown/expired.
		"""
		task = self.get(task_id)
		if task is None:
			return None
		task["state"] = state
		if artifacts is not None:
			task["artifacts"] = artifacts
		if error is not None:
			task["error"] = error
		return self._write(task)

	def get(self, task_id: str) -> dict | None:
		"""Read a task record, or None when absent/expired."""
		return self._cache.get_value(_KEY.format(id=task_id)) or None

	def _write(self, task: dict) -> dict:
		self._cache.set_value(_KEY.format(id=task["id"]), task, expires_in_sec=_TTL_SECONDS)
		return task
