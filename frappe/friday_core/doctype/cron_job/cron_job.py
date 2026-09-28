# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Cron Job controller (Design 87, Slice 1).

Keeps `next_run_at` in sync with the schedule so the tick
(`cron/scheduler.tick`) can find a due job. The heavy lifting — firing, running,
delivering — lives in `cron/scheduler.py`; this controller only validates the
schedule and computes the first/next fire time when the schedule changes.
"""

from __future__ import annotations

import frappe
from frappe.friday_core.cron.scheduler import compute_next_run
from frappe.model.document import Document


class CronJob(Document):
	def validate(self):
		self._validate_schedule()
		# Recompute next_run_at on create or whenever the schedule changed, so a
		# freshly-saved or re-scheduled job has a correct next fire time. We use
		# `now` as the base — the first run lands one interval / next cron tick
		# from now (or at the `once` time).
		schedule_changed = (
			self.is_new()
			or self.has_value_changed("schedule_kind")
			or self.has_value_changed("schedule_expr")
		)
		if schedule_changed:
			self.next_run_at = compute_next_run(
				self.schedule_kind, self.schedule_expr, frappe.utils.now_datetime()
			)
		# A re-enabled / re-saved active job should read as Scheduled (Paused is
		# set by the operator unchecking `enabled`).
		if self.enabled and self.state in (None, "", "Paused"):
			self.state = "Scheduled"
		elif not self.enabled and self.state == "Scheduled":
			self.state = "Paused"

	def _validate_schedule(self):
		"""Reject a schedule the scheduler can't parse — fail at save, not at fire."""
		try:
			compute_next_run(self.schedule_kind, self.schedule_expr, frappe.utils.now_datetime())
		except Exception as exc:
			frappe.throw(f"Invalid {self.schedule_kind} schedule {self.schedule_expr!r}: {exc}")
