# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""A2A Agent Card — the `/.well-known/agent.json` discovery document (Design 92, v1).

PLAIN ENGLISH
=============
Before another agent talks to Friday over A2A, it fetches this small JSON document —
UNAUTHENTICATED, at the well-known path — to learn who Friday is, what it can do, and
how to reach it. The `skills` list is built from the SAME skill loader the agent runs
with, so the card advertises EXACTLY the configured profile's permitted skills (role +
matrix scoped) and never leaks a capability the profile can't actually use.

This module is pure: `build_agent_card(...)` takes the inputs and returns a dict. The
HTTP serving (at `/.well-known/agent.json`) and the settings read live in the caller,
so the card shape is unit-testable with no Frappe and no DB.
"""

from __future__ import annotations

# The A2A Agent Card schema version we emit.
CARD_VERSION = "1.0"

# The auth scheme label the card advertises. A2A's blessed list is Bearer-oriented, but
# Frappe's middleware rejects an unknown `Authorization: Bearer` before our endpoint runs
# (see a2a/server.py), so Friday authenticates with the custom `X-A2A-Token` header — and
# names that scheme honestly here rather than mislabelling it "bearer".
AUTH_SCHEME = "x-a2a-token"

_ENDPOINT_PATH = "/api/method/frappe.friday_core.a2a.server.handle"


def build_agent_card(
	profile: str,
	*,
	base_url: str,
	list_fn=None,
	name: str = "Friday",
	description: str | None = None,
) -> dict:
	"""Build the A2A Agent Card for the exposed `profile`.

	`list_fn(profile) -> [{name, description, tags?}]` is injectable for tests; the
	default reads the real skill loader. `base_url` is the site's external origin
	(e.g. ``https://ai.example.com``) onto which the endpoint path is appended.
	"""
	list_fn = list_fn or _default_skill_list
	skills = [
		{
			"id": s["name"],
			"name": s["name"],
			"description": s.get("description") or "",
			"tags": s.get("tags") or [],
		}
		for s in list_fn(profile)
	]
	return {
		"name": name,
		"description": description
		or "Enterprise agentic orchestration on Frappe — every call governed and audited.",
		"url": base_url.rstrip("/") + _ENDPOINT_PATH,
		"version": CARD_VERSION,
		# v1 scope: no streaming, no push, no history. A short sync turn covers the case.
		"capabilities": {
			"streaming": False,
			"pushNotifications": False,
			"stateTransitionHistory": False,
		},
		"defaultInputModes": ["text/plain"],
		"defaultOutputModes": ["text/plain"],
		"authentication": {"schemes": [AUTH_SCHEME]},
		"skills": skills,
	}


def _default_skill_list(profile: str) -> list[dict]:
	"""The exposed profile's permitted skills, via the real loader (role + matrix scoped)."""
	from frappe.friday_core.skills.loader import load_for_profile

	return [{"name": s.name, "description": s.description, "tags": []} for s in load_for_profile(profile)]
