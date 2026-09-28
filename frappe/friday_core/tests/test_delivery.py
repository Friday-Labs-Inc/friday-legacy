# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Tests for the delivery target DSL + router (Design 86, LOCKED).

Tests-first. Mock-based — no DB, no redis.

What they pin (the LOCKED decisions):
  DSL  — parse origin / local / platform:chat[:thread] / bare platform; parse is
         pure (no DB), so an unconfigured platform is just a string until deliver.
  Q1   — platform delivery = ONE outbound Chat Message row.
  Q2   — unknown/unconfigured platform falls back to the local sink at deliver.
  Q3   — the local sink is a private Frappe File with a job/metadata header.
  Q4   — content over MAX_PLATFORM_OUTPUT is truncated; full output saved to a File.
  router — per-target try/except: one target failing never aborts the others.
"""

import unittest
from unittest.mock import MagicMock, patch

_D = "frappe.friday_core.gateway.delivery"


def _make_get_doc(record):
	"""A frappe.get_doc stub that records each payload and returns a doc mock."""

	def get_doc(payload):
		record.append(payload)
		doc = MagicMock()
		doc.name = "DOC-1"
		doc.file_url = "/private/files/out.md"
		return doc

	return get_doc


class TestParse(unittest.TestCase):
	def test_origin_with_source(self):
		from frappe.friday_core.gateway import delivery

		origin = delivery.DeliveryOrigin(platform="raven", session_id="CH-9")
		t = delivery.DeliveryTarget.parse("origin", origin)
		self.assertTrue(t.is_origin)
		self.assertEqual(t.platform, "raven")
		self.assertEqual(t.chat_id, "CH-9")

	def test_origin_without_source_falls_back_local(self):
		from frappe.friday_core.gateway import delivery

		t = delivery.DeliveryTarget.parse("origin", None)
		self.assertEqual(t.platform, delivery.LOCAL)
		self.assertTrue(t.is_origin)

	def test_local(self):
		from frappe.friday_core.gateway import delivery

		self.assertEqual(delivery.DeliveryTarget.parse("local").platform, delivery.LOCAL)

	def test_platform_chat(self):
		from frappe.friday_core.gateway import delivery

		t = delivery.DeliveryTarget.parse("raven:CH-1")
		self.assertEqual(t.platform, "raven")
		self.assertEqual(t.chat_id, "CH-1")
		self.assertTrue(t.is_explicit)
		self.assertEqual(t.to_string(), "raven:CH-1")

	def test_platform_chat_thread(self):
		from frappe.friday_core.gateway import delivery

		t = delivery.DeliveryTarget.parse("raven:CH-1:T9")
		self.assertEqual(t.thread_id, "T9")
		self.assertEqual(t.to_string(), "raven:CH-1:T9")

	def test_bare_platform(self):
		from frappe.friday_core.gateway import delivery

		t = delivery.DeliveryTarget.parse("raven")
		self.assertEqual(t.platform, "raven")
		self.assertIsNone(t.chat_id)

	def test_unknown_token_parses_as_platform_string(self):
		# Parse is pure — "wat" is just a platform string; the local fallback
		# happens at deliver time (Q2), not here.
		from frappe.friday_core.gateway import delivery

		self.assertEqual(delivery.DeliveryTarget.parse("wat:x").platform, "wat")


class TestDeliverPlatform(unittest.TestCase):
	@patch(f"{_D}.frappe")
	def test_delivers_to_session_as_outbound_row(self, fr):
		from frappe.friday_core.gateway import delivery

		fr.db.exists.return_value = True  # raven is a configured platform
		seen = []
		fr.get_doc.side_effect = _make_get_doc(seen)

		res = delivery.DeliveryRouter().deliver("hello", [delivery.DeliveryTarget.parse("raven:CH-1")])

		row = next(p for p in seen if p["doctype"] == "Chat Message")
		self.assertEqual(row["direction"], "outbound")
		self.assertEqual(row["platform"], "raven")
		self.assertEqual(row["session_id"], "CH-1")
		self.assertEqual(row["content"], "hello")
		self.assertTrue(res["raven:CH-1"]["success"])

	@patch(f"{_D}.frappe")
	def test_unknown_platform_downgrades_to_local_VISIBLY(self, fr):
		# The fix: an explicit platform target we can't deliver to is saved locally so
		# content isn't lost, but the misroute is marked VISIBLY (downgraded + reason) —
		# never a silent plain success (the bug: a raven channel fell back to a file with
		# the delivery reported as ok, so the operator never knew it didn't reach the channel).
		from frappe.friday_core.gateway import delivery

		fr.db.exists.return_value = False  # 'wat' is not a Chat Platform
		seen = []
		fr.get_doc.side_effect = _make_get_doc(seen)

		res = delivery.DeliveryRouter().deliver("hello", [delivery.DeliveryTarget.parse("wat:x")])

		# Content saved locally (never lost) — and NO channel row.
		self.assertTrue(any(p["doctype"] == "File" for p in seen))
		self.assertFalse(any(p["doctype"] == "Chat Message" for p in seen))
		# ...but the downgrade is now SURFACED, not silent.
		self.assertTrue(res["wat:x"]["success"])
		self.assertTrue(res["wat:x"]["downgraded"])
		self.assertIn("wat", res["wat:x"]["reason"])

	@patch(f"{_D}.frappe")
	def test_known_platform_is_not_marked_downgraded(self, fr):
		# Regression guard: a real channel delivery must NOT carry the downgraded flag.
		from frappe.friday_core.gateway import delivery

		fr.db.exists.return_value = True
		fr.get_doc.side_effect = _make_get_doc([])
		res = delivery.DeliveryRouter().deliver("hi", [delivery.DeliveryTarget.parse("raven:CH-1")])
		self.assertTrue(res["raven:CH-1"]["success"])
		self.assertNotIn("downgraded", res["raven:CH-1"])

	@patch(f"{_D}.frappe")
	def test_per_target_isolation(self, fr):
		from frappe.friday_core.gateway import delivery

		fr.db.exists.return_value = True

		def get_doc(payload):
			if payload.get("session_id") == "BAD":
				raise RuntimeError("boom")
			doc = MagicMock()
			doc.name = "CM"
			return doc

		fr.get_doc.side_effect = get_doc
		res = delivery.DeliveryRouter().deliver(
			"x",
			[
				delivery.DeliveryTarget.parse("raven:OK"),
				delivery.DeliveryTarget.parse("raven:BAD"),
			],
		)
		self.assertTrue(res["raven:OK"]["success"])
		self.assertFalse(res["raven:BAD"]["success"])
		self.assertIn("boom", res["raven:BAD"]["error"])


class TestTruncation(unittest.TestCase):
	@patch(f"{_D}.frappe")
	def test_oversized_output_truncated_and_full_saved(self, fr):
		from frappe.friday_core.gateway import delivery

		fr.db.exists.return_value = True
		seen = []
		fr.get_doc.side_effect = _make_get_doc(seen)

		big = "A" * 5000
		delivery.DeliveryRouter().deliver("hi " + big, [delivery.DeliveryTarget.parse("raven:CH-1")])

		doctypes = [p["doctype"] for p in seen]
		self.assertIn("File", doctypes)  # full output saved
		self.assertIn("Chat Message", doctypes)
		row = next(p for p in seen if p["doctype"] == "Chat Message")
		self.assertLessEqual(len(row["content"]), delivery.MAX_PLATFORM_OUTPUT + 200)
		self.assertIn("truncated", row["content"].lower())


class TestLocalSink(unittest.TestCase):
	@patch(f"{_D}.frappe")
	def test_local_writes_private_file_with_header(self, fr):
		from frappe.friday_core.gateway import delivery

		seen = []
		fr.get_doc.side_effect = _make_get_doc(seen)

		res = delivery.DeliveryRouter().deliver(
			"body text",
			[delivery.DeliveryTarget.parse("local")],
			job_id="JOB1",
			job_name="Nightly Digest",
		)
		f = next(p for p in seen if p["doctype"] == "File")
		self.assertTrue(f.get("is_private"))
		self.assertIn("Nightly Digest", f["content"])
		self.assertIn("body text", f["content"])
		self.assertTrue(res["local"]["success"])


if __name__ == "__main__":
	unittest.main()
