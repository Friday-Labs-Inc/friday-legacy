# ADR-0004 — One Friday bench hosts every domain app

**Status:** Accepted · 2026-09-17
**Relates:** ADR-0003, ADR-0005, ADR-0006

> _**Note · 2026-09-28** — the Mark 1 rover programme ended, a product decision
> taken outside this repository. **Status stays Accepted:** one Friday bench
> hosts every domain app, and a product never shares a bench with the platform.
> Only the Decision's second example is moot — there is no rover command centre
> left to host. The Decision text below is unedited; it was true when the
> decision was taken on 2026-09-17._

## Context

"Multi-domain" was undefined between two shapes: one Friday site hosting every
domain, or a site per domain. The engine already supports several active Domain
Bundles on one site (`engine/bundle.py:59` `active_bundles`), and recall is
scoped per profile and per project rather than per site.

Separately, the boundary between Friday and the products it serves had never
been stated as a rule, only as a seam contract for one product.

## Decision

**Friday runs on its own isolated bench** — Postgres with pgvector, Redis, its
own site — and **every domain app installs onto that one bench**. Domains are
Frappe apps: `randompack_ai` today, others later.

**Products run on separate benches.** RandomPack plus ERPNext is its own bench.
The rover's command centre is its own Frappe app on its own host. A product and
the platform never share a bench.

## Consequences

- Domains on one bench share Agent Settings, LLM providers and the team's Raven.
  Work is isolated per project, not per domain. If a domain ever needs isolated
  providers or settings, that is the trigger to revisit this ADR.
- Cross-bench communication is the only communication: signed connector events
  inbound, a signing client outbound. No shared database, ever.
- The blast radius of a bad domain app is the Friday bench, not a product.
  Hook failures must stay failure-isolated.

## Evidence

- `knc-infra/10-friday-bench.sh` already provisions exactly this: a `friday-bench`
  with Postgres, `CREATE EXTENSION vector`, `pg_trgm`, Raven, and
  `install-app randompack_ai`; `20-rp-bench.sh` provisions the product bench
  separately.
- `engine/bundle.py:38,59` — `active_bundle_for(doctype)` and `active_bundles()`.
