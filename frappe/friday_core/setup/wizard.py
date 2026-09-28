# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
The Friday Setup wizard controller (design 64).

The whitelisted backend for the ``friday-setup`` Desk page — a re-runnable,
state-aware onboarding that *does the work* (the Hermes ``hermes setup``
experience, Desk-native). Each endpoint delegates to the EXACT plumbing the CLI
already uses (``cli/setup.py`` + the surface/tool bootstraps), so there is one
implementation of "provision a provider / profile / surface / tool", not two.

The five steps:
  1. ``save_provider``     — LLM Provider + key + model + cost rates + default
  2. ``save_agent``        — the Friday Agent Profile (seeds a default soul)
  3. ``provision_surfaces``— Raven war room (Raven is required; this step
     fails loudly when it is missing)
  4. ``provision_tools``   — read + file tools onto the profile
  5. ``verify_and_complete``— live provider probe + pipeline health, then flag

``setup_status`` is the single read the page renders from; every flag is derived
from live state, so the wizard is honest regardless of how things were created
(wizard, CLI, or by hand).

Every endpoint is **System-Manager-gated** (``frappe.only_for``) — setup is an
administrative act. Each is idempotent (re-running updates, never duplicates).
"""

from __future__ import annotations

import frappe
from frappe.friday_core.cli import setup as cli_setup
from frappe.friday_core.doctype.agent_settings.agent_settings import (
	SETTINGS_DOCTYPE as _SETTINGS,
)
from frappe.friday_core.doctype.agent_settings.agent_settings import (
	SETTINGS_NAME as _SETTINGS_NAME,
)

FRIDAY_PROFILE = "Friday"


@frappe.whitelist()
def setup_status() -> dict:
	"""Return per-step state for the wizard page. Derived from live records."""
	frappe.only_for("System Manager")
	from frappe.friday_core.health.pipeline_health import pipeline_health

	providers = frappe.get_all("LLM Provider", pluck="name") or []
	# Agent Settings is a regular single-row DocType (NOT `issingle`), read with
	# get_cached_doc by its canonical row name (_SETTINGS_NAME, "__default") —
	# reading it as "Agent Settings" raises DoesNotExistError (see the controller).
	settings = frappe.get_cached_doc(_SETTINGS, _SETTINGS_NAME)
	default_provider = settings.get("default_provider")

	profile = frappe.db.get_value(
		"Agent Profile",
		FRIDAY_PROFILE,
		["name", "system_prompt", "status", "model_provider"],
		as_dict=True,
	)

	raven_installed = bool(frappe.db.table_exists("Raven Channel"))
	war_room = bool(
		raven_installed and frappe.db.exists("Raven Channel", {"channel_name": "friday_war_room"})
	)

	health = pipeline_health()
	friday_worker = bool((health.get("workers") or {}).get("friday", {}).get("present"))
	setup_complete = bool(settings.get("setup_complete"))

	return {
		"provider": {
			"configured": bool(providers),
			"default_provider": default_provider,
			"providers": providers,
		},
		"agent": {
			"configured": bool(profile),
			"profile": profile.get("name") if profile else None,
			"has_system_prompt": bool(profile and profile.get("system_prompt")),
			"active": bool(profile and profile.get("status") == "Active"),
		},
		"surfaces": {
			"raven_installed": raven_installed,
			"war_room": war_room,
			# Locked: Raven is the surface, Friday is the engine. Surfaced here so
			# an operator can SEE that Raven's own AI is off by design, rather than
			# discovering the toggle later and quietly splitting the audit trail.
			"raven_ai_enabled": bool(
				raven_installed and frappe.db.get_single_value("Raven Settings", "enable_ai_integration")
			),
			"engine": "friday",
		},
		"tools": {
			"read": bool(frappe.db.exists("Skill", "read-record")),
			"files": bool(frappe.db.exists("Skill", "attach-deliverable")),
		},
		"health": {"verdict": health.get("verdict"), "friday_worker": friday_worker},
		"setup_complete": setup_complete,
	}


@frappe.whitelist()
def save_provider(
	provider_name: str,
	provider_type: str,
	api_key: str,
	base_url: str | None = None,
	default_model: str | None = None,
	input_cost_per_million: float | str | None = None,
	output_cost_per_million: float | str | None = None,
	make_default: int | str = 1,
) -> dict:
	"""Step 1 — create/update an LLM Provider, set cost rates, optionally default.

	Delegates the row write to ``cli_setup.provision_provider`` (encrypted key
	field, idempotent). If no model is chosen yet, falls back to the per-type
	default so the row is always usable; Discover Models refines it later.
	"""
	frappe.only_for("System Manager")

	model = default_model or cli_setup.DEFAULT_MODELS.get(provider_type, "")
	name = cli_setup.provision_provider(
		provider_name=provider_name,
		provider_type=provider_type,
		api_key=api_key,
		default_model=model,
		base_url=base_url or None,
	)

	# Cost rates (design 65d) — only write what was actually provided; a blank
	# rate must stay blank (cost renders "—", never a fabricated 0).
	updates = {}
	if input_cost_per_million not in (None, ""):
		updates["input_cost_per_million"] = frappe.utils.flt(input_cost_per_million)
	if output_cost_per_million not in (None, ""):
		updates["output_cost_per_million"] = frappe.utils.flt(output_cost_per_million)
	if updates:
		frappe.db.set_value("LLM Provider", name, updates)

	make_default = int(make_default or 0)
	if make_default:
		cli_setup.set_default_provider(name)

	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	return {"provider": name, "default": bool(make_default), "model": model}


@frappe.whitelist()
def save_agent(
	provider_name: str,
	profile_name: str = FRIDAY_PROFILE,
	model_name: str | None = None,
	system_prompt: str | None = None,
) -> dict:
	"""Step 2 — create/update the Friday Agent Profile.

	Delegates to ``cli_setup.provision_profile``, which sets status Active and
	seeds a default system prompt (Friday's "soul") when none is given — so the
	agent is never prompt-less, mirroring Hermes seeding SOUL.md.
	"""
	frappe.only_for("System Manager")
	name = cli_setup.provision_profile(
		profile_name=profile_name,
		provider_name=provider_name,
		system_prompt=system_prompt or None,
		model_name=model_name or None,
	)
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	return {"profile": name}


@frappe.whitelist()
def provision_surfaces(profile_name: str = FRIDAY_PROFILE) -> dict:
	"""Step 3 — provision the Raven war room, if Raven is installed."""
	frappe.only_for("System Manager")
	if not frappe.db.table_exists("Raven Channel"):
		# Raven is required, not optional — say so plainly instead of reporting a
		# tidy "skipped" that reads like everything is fine.
		frappe.throw(
			frappe._(
				"Raven is not installed on this site, and Friday requires it — it is the "
				"chat front door (bot identity, project channels, the war room). "
				"Install it with: bench get-app --branch develop "
				"https://github.com/The-Commit-Company/raven && "
				"bench --site {0} install-app raven"
			).format(frappe.local.site)
		)

	from frappe.friday_core.surfaces import bootstrap_raven

	bootstrap_raven.provision(profile_name=profile_name)
	return {"status": "provisioned"}


@frappe.whitelist()
def provision_tools(profile_name: str = FRIDAY_PROFILE) -> dict:
	"""Step 4 — provision the read + file tools onto the profile."""
	frappe.only_for("System Manager")
	from frappe.friday_core.skills import bootstrap_files, bootstrap_read

	bootstrap_read.provision(profile_name=profile_name)
	bootstrap_files.provision(profile_name=profile_name)
	return {"status": "provisioned", "read": True, "files": True}


@frappe.whitelist()
def verify_and_complete(provider_name: str | None = None) -> dict:
	"""Step 5 — verify the setup actually works, then flag it complete.

	Two gates (the surpass-Hermes axis — Hermes never tests the model):
	  - a LIVE provider probe: ``list_models`` must return ``source == "live"``
	    (a real ``/v1/models`` round-trip). The curated catalog fallback means
	    the key did NOT work, so that does not count as verified.
	  - pipeline health must not be ``down`` (worker + scheduler up).

	Only when both pass do we set ``Agent Settings.setup_complete``.
	"""
	frappe.only_for("System Manager")
	from frappe.friday_core.health.pipeline_health import pipeline_health
	from frappe.friday_core.llm.model_discovery import list_models

	provider_name = provider_name or frappe.get_cached_doc(_SETTINGS, _SETTINGS_NAME).get("default_provider")

	probe = list_models(provider_name=provider_name) if provider_name else {"source": "none", "models": []}
	health = pipeline_health()

	provider_ok = probe.get("source") == "live" and bool(probe.get("models"))
	ready = bool(provider_ok and health.get("verdict") != "down")

	if ready:
		# db_set on the cached doc (Agent Settings is a regular single-row DocType,
		# not `issingle` — set_single_value would write to tabSingles, wrong here).
		frappe.get_cached_doc(_SETTINGS, _SETTINGS_NAME).db_set("setup_complete", 1)
		frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit

	return {
		"ready": ready,
		"provider_ok": provider_ok,
		"provider_source": probe.get("source"),
		"provider_error": probe.get("error"),
		"health": health,
	}
