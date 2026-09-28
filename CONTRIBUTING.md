# Contributing to Friday

Friday is a Frappe-derived agentic framework for governed business agents.

The project is early. Contributions should protect the fundamentals first:

- agents operate inside typed DocTypes;
- skills are schema-validated and permission-gated;
- every execution is auditable;
- Kanban is a view over configurable workflow, not the workflow itself;
- ERPNext PO automation is the first Phase 1 flagship dogfood after the framework loop is green.

## Start Here

Read these documents before opening implementation work:

1. `CONTEXT.md` — the vocabulary, and what is not true yet
2. `docs/adr/` — the decisions, newest wins
3. `docs/ports/` — the Hermes port ledger: what Friday must contain

Older design reasoning is in `docs/archive/` — read it for *why*, never for
*what is true now* (ADR-0009).

## Development Rules

- Keep Frappe core divergence minimal and documented.
- Prefer Friday modules/apps unless framework-level behavior truly requires a core patch.
- Do not activate agent-created profiles, skills, or workflows without validation and review.
- Do not bypass permission checks, even temporarily.
- Do not commit secrets, tokens, database dumps, or private customer data.
- Update design docs when implementation proves a design wrong.

## Pull Requests

Every PR should include:

- what changed;
- why it changed;
- design docs affected;
- tests or verification performed;
- security/audit implications if any.

Small, focused PRs are preferred.

## Continuous Integration — what green means

Four checks run on an ordinary pull request. All four are expected to pass, and
all four are a function of your diff. If one is red, it is about your change.

| Check | Workflow | What it means |
| --- | --- | --- |
| `Semantic Commits` | `linters.yml` | Every commit title is a Conventional Commit. |
| `Documentation Required` | `linters.yml` | The PR is labelled for docs impact. |
| `Semgrep Rules` | `linters.yml` | Frappe's semgrep ruleset finds nothing new. |
| `Pre-Commit` | `linters.yml` | `pre-commit run --all-files` is clean. |

Two more run only when they are relevant to the diff:

| Check | Runs when |
| --- | --- |
| `Vulnerable Dependency Check` | `dependency-audit.yml` — the PR changes a dependency manifest. It also runs daily on a schedule and on pushes to `develop`. |
| `Friday Core Tests` | `tests.yml` — the PR changes a `.py` file. |

### Pre-Commit runs on the whole tree, not your diff

`pre-commit/action` runs `pre-commit run --all-files`. It audits the entire
repository on every PR, so an unformatted file anywhere makes your PR red even
if you never touched it. Keep the tree clean:

```bash
pip install pre-commit
pre-commit install          # also installs the commit-msg hook
pre-commit run --all-files  # what CI will run
```

The hook versions in `.pre-commit-config.yaml` are pinned. Run the pinned
versions rather than whatever `ruff` or `prettier` happens to be on your PATH —
a different ruff will reformat differently and you will fight CI.

### The dependency scan is not a per-PR gate

`pip-audit` compares the resolved dependency tree against an advisory database
that changes daily. Running it on every PR means a CVE published against a
transitive dependency turns someone's unrelated documentation PR red. That is
noise, not a gate, so the scan runs on dependency-manifest changes, on a daily
schedule, and on pushes to `develop`.

Accepted advisories are listed in `.github/workflows/dependency-audit.yml`, each
with a date, a fix version if one exists, and the reason it is accepted. If the
scan is red, either the advisory is new — triage it and either bump or add it to
that list with a reason — or your manifest change introduced it.

Do not add an entry to that list without a reason and a retirement condition.
An ignore nobody can explain is how the job became meaningless the first time.

### Known gap: `Friday Core Tests` does not currently assert anything

Every step in `tests.yml` ends in `|| true` and `bench` is never installed, so
the job reports success without running a test. Treat a green `Friday Core
Tests` as "not yet evidence" and verify Python changes locally until that is
fixed. Nothing on `develop` is enforced by a required status check either — the
default-branch ruleset requires a pull request, but no checks.

## Contributing as an AI Agent (or Sponsoring One)

Friday accepts AI agents as first-class contributors under a published policy.
Read `docs/contributing/AI_CONTRIBUTORS.md` before submitting AI-authored work.
Every AI contribution requires a registered human sponsor, a written proposal,
sandboxed execution, and a human co-signature on the PR.

