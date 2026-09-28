# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Deliverables — materialize agent output as real, downloadable artifacts.

The deliverables gap (found 2026-06-15): a "100% complete" project produced
only text buried in ``Task.result`` — no files, nothing a client receives.
This module fixes that: when a task completes, its output is written as
attached artifact files (Markdown + PDF), and a completed project assembles
them into one deliverable package.

Design 73, Slice 5. Best-effort throughout — materialization never blocks a
task or project transition; the structured record stays the source of truth.
"""

from frappe.friday_core.deliverables.materialize import (
	assemble_project_package,
	materialize_task_deliverable,
)

__all__ = ["assemble_project_package", "materialize_task_deliverable"]
