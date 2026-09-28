# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""Design 96 Slice 1 — medium-based model routing.

DB-free. Pins the routing contract:
  - A medium with NO route falls through to the profile's own provider
    chain unchanged — which is why text behaviour cannot regress.
  - A route hit wins over the profile chain.
  - A route naming an INACTIVE provider raises (strict, mirrors the
    profile-link rule) — never silently re-routes.
  - A stale route (provider row deleted) falls through.
  - `get_provider_for_medium` stamps row identity, so the Design-94
    failover chain of the ROUTED provider composes with routing.
  - `SkillDefinition` carries the new `medium` field (default "text",
    including for cache entries written before the field existed).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from frappe.friday_core.llm import provider as prov
from frappe.friday_core.skills.loader import SkillDefinition

_P = "frappe.friday_core.llm.provider"


def _provider_doc(row: dict, is_active: int = 1) -> MagicMock:
	doc = MagicMock()
	doc.is_active = is_active
	doc.as_dict.return_value = row
	return doc


class TestResolveProviderRowForMedium(unittest.TestCase):
	@patch(f"{_P}._resolve_provider_row")
	@patch(f"{_P}.frappe")
	def test_unrouted_medium_falls_through_to_profile_chain(self, fr, m_profile):
		fr.db.exists.return_value = True  # Agent Settings row exists
		fr.get_all.return_value = []  # ...but has no route for this medium
		m_profile.return_value = {"name": "Codex", "provider_type": "openai"}

		row = prov.resolve_provider_row_for_medium("Visual Agent", "image")

		self.assertEqual(row["name"], "Codex")
		m_profile.assert_called_once_with("Visual Agent")

	@patch(f"{_P}._resolve_provider_row")
	@patch(f"{_P}.frappe")
	def test_route_hit_wins_over_profile_chain(self, fr, m_profile):
		fr.db.exists.return_value = True  # settings row AND provider row
		fr.get_all.return_value = [{"provider": "OpenAI Images"}]
		routed = {"name": "OpenAI Images", "provider_type": "openai"}
		fr.get_doc.return_value = _provider_doc(routed)

		row = prov.resolve_provider_row_for_medium("Visual Agent", "image")

		self.assertEqual(row, routed)
		m_profile.assert_not_called()
		# the route was looked up for the requested medium
		_, kwargs = fr.get_all.call_args
		self.assertEqual(kwargs["filters"]["medium"], "image")

	@patch(f"{_P}._resolve_provider_row")
	@patch(f"{_P}.frappe")
	def test_inactive_routed_provider_raises_not_reroutes(self, fr, m_profile):
		fr.db.exists.return_value = True
		fr.get_all.return_value = [{"provider": "OpenAI Images"}]
		fr.get_doc.return_value = _provider_doc({"name": "OpenAI Images"}, is_active=0)

		with self.assertRaises(prov.LLMError):
			prov.resolve_provider_row_for_medium("Visual Agent", "image")
		m_profile.assert_not_called()

	@patch(f"{_P}._resolve_provider_row")
	@patch(f"{_P}.frappe")
	def test_stale_route_falls_through(self, fr, m_profile):
		# Settings row exists, route row names a provider that was deleted.
		fr.db.exists.side_effect = [True, False]  # settings yes, provider no
		fr.get_all.return_value = [{"provider": "Gone"}]
		m_profile.return_value = {"name": "Codex"}

		row = prov.resolve_provider_row_for_medium("Visual Agent", "image")
		self.assertEqual(row["name"], "Codex")

	@patch(f"{_P}._resolve_provider_row")
	@patch(f"{_P}.frappe")
	def test_no_settings_row_falls_through_without_route_query(self, fr, m_profile):
		fr.db.exists.return_value = False
		m_profile.return_value = {"name": "Codex"}

		row = prov.resolve_provider_row_for_medium("Visual Agent", "image")

		self.assertEqual(row["name"], "Codex")
		fr.get_all.assert_not_called()


class TestGetProviderForMedium(unittest.TestCase):
	@patch(f"{_P}._build_provider")
	@patch(f"{_P}.resolve_provider_row_for_medium")
	def test_stamps_row_identity_for_design94_failover(self, m_resolve, m_build):
		m_resolve.return_value = {
			"name": "OpenAI Images",
			"provider_type": "openai",
			"fallback_provider": "Minimax",
		}
		built = MagicMock()
		m_build.return_value = built

		provider = prov.get_provider_for_medium("Visual Agent", "image")

		self.assertIs(provider, built)
		self.assertEqual(provider.source_row_name, "OpenAI Images")
		self.assertEqual(provider.fallback_provider_name, "Minimax")

	@patch(f"{_P}.resolve_provider_row_for_medium")
	def test_raises_when_nothing_resolves(self, m_resolve):
		m_resolve.return_value = None
		with self.assertRaises(prov.LLMError):
			prov.get_provider_for_medium("Visual Agent", "image")


class TestSkillDefinitionMedium(unittest.TestCase):
	def test_round_trip_carries_medium(self):
		sd = SkillDefinition(
			name="generate-image",
			description="",
			when_to_use="",
			parameters_schema={},
			risk_level="low",
			requires_approval=False,
			medium="image",
		)
		self.assertEqual(sd.to_dict()["medium"], "image")
		self.assertEqual(SkillDefinition.from_dict(sd.to_dict()).medium, "image")

	def test_legacy_cache_entry_defaults_to_text(self):
		# Cache entries written before design 96 have no medium key.
		sd = SkillDefinition.from_dict({"name": "read-record", "parameters_schema": {}, "risk_level": "low"})
		self.assertEqual(sd.medium, "text")

	def test_from_skill_doc_reads_medium_field(self):
		skill = MagicMock()
		skill.name = "generate-image"
		skill.description = "d"
		skill.when_to_use = "w"
		skill.parameters_schema = "{}"
		skill.risk_level = "low"
		skill.requires_approval = 0
		skill.medium = "image"
		self.assertEqual(SkillDefinition.from_skill_doc(skill).medium, "image")
		skill.medium = None  # pre-migration rows
		self.assertEqual(SkillDefinition.from_skill_doc(skill).medium, "text")


if __name__ == "__main__":
	unittest.main()
