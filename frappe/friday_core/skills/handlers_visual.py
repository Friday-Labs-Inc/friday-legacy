# Copyright (c) 2026, Friday Labs and contributors
# For license information, please see license.txt

"""
Visual-generation skills — `generate-image` over ANY routed image provider.

PLAIN ENGLISH
=============
This gives a visual agent (a domain app's design lead; graphic/web designers
later) the ability to produce ACTUAL images, not just describe them. The agent
calls `generate-image` with a prompt; we call the image API of whichever
provider the site routes the "image" medium to, download or decode the result,
and save it as a Frappe File attached to the work-item (so it shows in the
brief's attachment sidebar and can be shared). The agent gets back the file
URL to reference in its reply.

HOW IT FITS
===========
- It is a generic, domain-agnostic skill — any visual agent can use it.
- The provider comes from design 96's medium routing
  (`get_provider_for_medium(profile, "image")` semantics, row-level): the
  Model Route table on Agent Settings wins, else the agent's own provider.
  That is what frees agents chatting on a text-only provider (e.g. Codex)
  to still make visuals through a routed image provider.
- Image APIs differ too much for the LLMProvider ABC (sync vs poll, URL vs
  base64 payloads), so the dispatch is function-level per provider_type —
  mirroring `_build_provider`'s if-chain. Supported today: `minimax`
  (`/v1/image_generation`, returns URLs that expire in 24h — we download
  immediately) and `openai` (`/v1/images/generations`, gpt-image models,
  returns base64 — we decode). The provider row's `image_model` field names
  the model.
- It never raises for an expected failure (bad prompt, no balance, policy
  block): it returns a dict whose `result` string explains what happened, so
  the agent can adapt — the dispatcher contract.
"""

from __future__ import annotations

import base64
import re

import requests

import frappe
from frappe.friday_core.agent_runner.dispatcher import register_skill_handler

GENERATE_IMAGE = "generate-image"

_GEN_TIMEOUT = 120
_DOWNLOAD_TIMEOUT = 60

# --- MiniMax specifics -------------------------------------------------------
_MINIMAX_IMAGE_PATH = "/v1/image_generation"
_MINIMAX_DEFAULT_MODEL = "image-01"
# MiniMax image hosts are region-coupled with the API key; pick by the
# provider's configured base_url (the China host carries the extra 'i').
_MINIMAX_HOST_INTL = "https://api.minimax.io"
_MINIMAX_HOST_CN = "https://api.minimaxi.com"

# --- OpenAI specifics --------------------------------------------------------
_OPENAI_IMAGE_PATH = "/v1/images/generations"
_OPENAI_DEFAULT_HOST = "https://api.openai.com"
_OPENAI_DEFAULT_MODEL = "gpt-image-1"


def generate_image(skill_name: str, parameters: dict) -> dict:
	"""Generate image(s) from a text prompt and save them as Frappe Files.

	Params: prompt (required), aspect_ratio (default "1:1"), n (1-9, default 1),
	attach_to_doctype + attach_to_name (optional; default = the current work
	item). Returns {"result": <human summary>, "image_urls": [<file urls>]}.
	"""
	params = parameters or {}
	prompt = (params.get("prompt") or "").strip()
	if not prompt:
		return {"result": "generate-image needs a non-empty 'prompt' describing the image."}
	aspect_ratio = params.get("aspect_ratio") or "1:1"
	try:
		count = max(1, min(9, int(params.get("n") or 1)))
	except (TypeError, ValueError):
		count = 1

	ctx = frappe.flags.get("friday_dispatch_context") or {}
	agent_profile = ctx.get("agent_profile")
	if not agent_profile:
		return {"result": "generate-image must be called by an agent (no profile in context)."}

	row, err = _image_provider_row(agent_profile)
	if err:
		return {"result": err}

	backend = _IMAGE_BACKENDS[(row.get("provider_type") or "")]
	items, err = backend(row, prompt, aspect_ratio, count)
	if err:
		return {"result": err}
	if not items:
		return {"result": "Image generation returned no images."}

	dt, dn = _attach_target(params, ctx)
	saved = _save_images(items, prompt, dt, dn)
	if not saved:
		return {"result": "Images were generated but could not be downloaded (URLs may have expired)."}

	where = f" (attached to {dt} {dn})" if (dt and dn) else ""
	return {
		"result": f"Generated {len(saved)} image(s){where}: " + ", ".join(saved),
		"image_urls": saved,
	}


def _image_provider_row(agent_profile: str):
	"""Return (provider_row, error). Resolves through the design-96 medium
	routes (Model Route on Agent Settings, falling through to the agent's
	own provider), then checks the resolved provider_type has an image
	backend here."""
	from frappe.friday_core.llm.provider import resolve_provider_row_for_medium

	try:
		row = resolve_provider_row_for_medium(agent_profile, "image")
	except Exception as exc:
		return None, f"Cannot resolve an LLM provider for image generation: {exc}"
	if not row:
		return None, (
			"No active LLM provider is configured for image generation. Add a "
			"Model Route (medium=image) in Agent Settings or set the agent's provider."
		)
	provider_type = row.get("provider_type") or ""
	if provider_type not in _IMAGE_BACKENDS:
		supported = ", ".join(sorted(_IMAGE_BACKENDS))
		return None, (
			f"generate-image supports {supported} providers; the resolved provider "
			f"{row.get('name')!r} is {provider_type!r}. Route the 'image' medium to a "
			"supported provider in Agent Settings → Model Routes."
		)
	return row, None


# ---------------------------------------------------------------------------
# Per-provider image backends. Each returns (items, error) where an item is
# {"url": ...} (fetch later) or {"bytes": ...} (already decoded) — _save_images
# handles both. New providers: add a function + an _IMAGE_BACKENDS entry.
# ---------------------------------------------------------------------------


def _generate_minimax(row: dict, prompt: str, aspect_ratio: str, count: int):
	"""MiniMax image generation: synchronous POST, returns URLs expiring in 24h."""
	api_key, err = _api_key(row, "MiniMax")
	if err:
		return None, err
	base_url = (row.get("base_url") or "").lower()
	host = _MINIMAX_HOST_CN if "minimaxi" in base_url else _MINIMAX_HOST_INTL

	try:
		resp = requests.post(
			host + _MINIMAX_IMAGE_PATH,
			headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
			json={
				"model": row.get("image_model") or _MINIMAX_DEFAULT_MODEL,
				"prompt": prompt,
				"aspect_ratio": aspect_ratio,
				"n": count,
				"response_format": "url",
			},
			timeout=_GEN_TIMEOUT,
		)
		payload = resp.json()
	except Exception as exc:  # network / JSON / timeout — recoverable, report it
		return None, f"Image generation request failed: {type(exc).__name__}: {exc}"

	base_resp = payload.get("base_resp") or {}
	status = base_resp.get("status_code")
	if status not in (0, None):
		return None, (f"MiniMax image generation error {status}: {base_resp.get('status_msg') or ''}".strip())
	urls = (payload.get("data") or {}).get("image_urls") or []
	return [{"url": u} for u in urls], None


def _generate_openai(row: dict, prompt: str, aspect_ratio: str, count: int):
	"""OpenAI image generation (gpt-image models): synchronous POST, returns
	base64 image data — no download step, we decode directly."""
	api_key, err = _api_key(row, "OpenAI")
	if err:
		return None, err
	host = (row.get("base_url") or _OPENAI_DEFAULT_HOST).rstrip("/")

	try:
		resp = requests.post(
			host + _OPENAI_IMAGE_PATH,
			headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
			json={
				"model": row.get("image_model") or _OPENAI_DEFAULT_MODEL,
				"prompt": prompt,
				"n": count,
				"size": _openai_size(aspect_ratio),
			},
			timeout=_GEN_TIMEOUT,
		)
		payload = resp.json()
	except Exception as exc:
		return None, f"Image generation request failed: {type(exc).__name__}: {exc}"

	error = payload.get("error")
	if error:
		return None, f"OpenAI image generation error: {error.get('message') or error}"
	items = []
	for entry in payload.get("data") or []:
		if entry.get("b64_json"):
			try:
				items.append({"bytes": base64.b64decode(entry["b64_json"])})
			except Exception:
				continue  # one corrupt entry shouldn't sink the batch
		elif entry.get("url"):
			items.append({"url": entry["url"]})
	return items, None


_IMAGE_BACKENDS = {
	"minimax": _generate_minimax,
	"openai": _generate_openai,
}


def _api_key(row: dict, label: str):
	"""Return (api_key, error) for a provider row."""
	from frappe.friday_core.llm.provider import _get_api_key

	api_key = _get_api_key(row)
	if not api_key:
		return None, f"The {label} provider has no API key configured."
	return api_key, None


def _openai_size(aspect_ratio: str) -> str:
	"""Map an aspect-ratio string to a gpt-image size. The API takes fixed
	sizes, not ratios: square, landscape (1536x1024) or portrait (1024x1536).
	An unparseable ratio lets the model pick ('auto')."""
	match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", aspect_ratio or "")
	if not match:
		return "auto"
	w, h = int(match.group(1)), int(match.group(2))
	if w > h:
		return "1536x1024"
	if h > w:
		return "1024x1536"
	return "1024x1024"


def _attach_target(params: dict, ctx: dict):
	"""Where to attach the saved files: an explicit target, else the current
	work-item (derived from the executing Task), else nowhere (unattached)."""
	dt = params.get("attach_to_doctype")
	dn = params.get("attach_to_name")
	if dt and dn:
		return dt, dn
	session = ctx.get("session_id") or ""
	if session.startswith("task::"):
		task_name = session.removeprefix("task::")
		wi = frappe.db.get_value("Task", task_name, ["work_item_doctype", "work_item_name"], as_dict=True)
		if wi and wi.work_item_doctype and wi.work_item_name:
			return wi.work_item_doctype, wi.work_item_name
	return None, None


def _save_images(items, prompt: str, dt, dn) -> list[str]:
	"""Persist each generated image as a Frappe File and return the file URLs.
	Items carry either raw bytes (decoded base64) or a URL to fetch first.
	Bytes are passed through untouched."""
	from frappe.utils.file_manager import save_file

	slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:24] or "image"
	saved: list[str] = []
	for index, item in enumerate(items):
		image_bytes = item.get("bytes")
		if image_bytes is None:
			try:
				image_bytes = requests.get(item["url"], timeout=_DOWNLOAD_TIMEOUT).content
			except Exception:
				break  # URL expired or unreachable — keep whatever already saved
		ext = _image_ext(image_bytes)  # sniff magic bytes; don't trust the URL
		file_doc = save_file(f"{slug}-{index + 1}{ext}", image_bytes, dt or None, dn or None, is_private=0)
		saved.append(file_doc.file_url)
	return saved


def _image_ext(data: bytes) -> str:
	"""Pick a file extension from the image's magic bytes (don't trust the URL)."""
	if data[:3] == b"\xff\xd8\xff":
		return ".jpg"
	if data[:8] == b"\x89PNG\r\n\x1a\n":
		return ".png"
	if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
		return ".webp"
	return ".jpg"


register_skill_handler(GENERATE_IMAGE, generate_image)
