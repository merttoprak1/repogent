# Repogent

**Approval-gated, evidence-backed Python repository changes.**

[![CI](https://github.com/merttoprak1/repogent/actions/workflows/ci.yml/badge.svg)](https://github.com/merttoprak1/repogent/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/repogent.svg)](https://pypi.org/project/repogent/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2a6db2.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/badge/lint-ruff-d7ff64.svg)](https://docs.astral.sh/ruff/)

Repogent is an open-source CLI and Codex plugin. A model may propose
requirements, a plan, and a patch. Deterministic services keep control of
repository scope, patch policy, validation, evidence, and mutation. Nothing
touches the real checkout until you approve the exact displayed patch.

It is built for Codex Desktop operators who need a reviewable Python change,
not an unsupervised edit.

Released under the [MIT License](LICENSE).

## Five-minute Codex install

Repogent requires Python 3.11 or newer. Codex starts the plugin MCP server with
the bare `repogent` command, so install it on a **persistent PATH**. A
repository-only virtual environment is not enough for Codex Desktop.

```bash
python3 --version
pipx install repogent
pipx ensurepath
command -v repogent
repogent --version
codex plugin marketplace add merttoprak1/repogent
```

If `command -v repogent` prints nothing, open a new login shell after
`pipx ensurepath`. Fully restart Codex Desktop, install **Repogent** from the
Plugin Directory, and start a new task.

Then diagnose without mutation:

```text
Use Repogent Repository Readiness for /path/to/repository.
```

For a change, name the mutating skill explicitly:

```text
Use Repogent Verified Change to safely add a health endpoint to /path/to/repository.
Show the requirements, plan, and exact patch before applying it.
```

Requirements, the plan, and the final patch are three separate digest-bound
approvals. Executor selection is a later, target-bound decision — not a fourth
content approval. Docker is optional until that decision. Local execution always
requires explicit reduced-isolation consent and is never a silent fallback.

Unreleased commits can still be installed with
`pipx install 'git+https://github.com/merttoprak1/repogent.git'`.

## Why Repogent

Most coding agents optimize for producing a patch. Repogent optimizes for being
able to explain and verify that patch:

- **Explicit human control** — requirements, plan, executor, and exact patch
  decisions are bound to the artifact being approved.
- **Deterministic guardrails** — Git-bounded scope, typed MCP contracts, patch
  policy, and allowlisted validation commands do not depend on model judgment.
- **Evidence you can inspect** — each terminal run records checkout state,
  validation, trust label, and bounded audit artifacts. Reprint with
  `repogent report <run-directory>`.
- **Honest execution boundaries** — Docker is the isolated validator. Local
  execution is always `REDUCED ISOLATION`. Docker never falls back to local.

## What it does

The Codex plugin exposes two capabilities:

- **Repository Readiness** inspects a Git-bounded repository, provider
  readiness, validation-command availability, and executor options without
  editing files or running repository code.
- **Verified Change** prepares a bounded change through explicit requirements,
  plan, executor, and patch approvals. It validates the selected patch before
  it can touch the real checkout.

Repogent supports conventional Python packages, CLIs, data transforms, and the
bundled FastAPI example. Terminal commands `doctor`, `analyze`, `run`,
`report`, and `mcp` cover the same kernel.

## Safety model

Repository content and tests are untrusted.

- Git-bounded input scope and explicit size limits.
- Candidate patches previewed and validated in disposable copies.
- Docker is the isolated validator (no network, read-only checkout mount,
  resource limits). Local execution requires explicit reduced-isolation
  consent.
- No silent Docker→local fallback, and no automatic install of target-repository
  dependencies.
- Durable evidence, checkout state, validation status, trust label, and bounded
  typed errors for every terminal run.

Only the final, explicitly approved patch can modify the real checkout. If
recovery cannot be proved, validation is incomplete, or evidence is ambiguous,
Repogent stops and asks for human intervention.

Read the [security model](docs/security.md) and
[architecture](docs/architecture.md) before using Repogent on sensitive code.

## CLI

`doctor` is read-only. It reports whether a repository can enter a workflow and
separates required base checks from optional executor availability:

```bash
repogent doctor ./tests/fixtures/python_library
```

The default provider is `codex-cli` (local Codex login). Use `--provider openai`
only when `OPENAI_API_KEY` is set for the Repogent process. The default doctor
executor is `deferred`: a missing Docker daemon is an unavailable isolation
option, not a base-readiness failure.

`analyze` prints a bounded inventory, Python symbol graph, and request-ranked
localization:

```bash
repogent analyze ./tests/fixtures/python_library \
  --request "Reject inverted clamp bounds"
```

`report` reprints a run's markdown evidence:

```bash
repogent report ./.repogent/runs/run-<id>
```

For a reproducible local demo, copy the bundled project so tracked files stay
unchanged:

```bash
REPOGENT_DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/repogent-demo.XXXXXX")"
cp -R examples/fastapi_demo/. "$REPOGENT_DEMO_DIR"/
repogent run --repository "$REPOGENT_DEMO_DIR" \
  --request "Add a health endpoint" \
  --provider scripted --script ./examples/scripted_run.json \
  --executor local --output-dir ./.repogent/runs
```

The clamp-library fixture uses the same path with
`tests/fixtures/python_library` and `examples/scripted_clamp.json`.

The demo asks for three approvals: requirements, plan, and exact patch. The
explicit local executor keeps the demo usable without Docker; it is a weaker
boundary than container isolation.

## Executors and providers

`repogent run` defaults to the Codex CLI provider and the Docker executor.
Build the reviewed validator image before a Docker-backed local run:

```bash
make validator-image
```

The image runs without network access, mounts the checkout read-only, and
applies CPU, memory, process, output, and time limits. If Docker or the image
is unavailable, choose local execution explicitly.

Proposal providers (`codex-cli`, `openai`, `scripted`) only produce typed
artifacts. Repogent still validates schemas and patches, records evidence, and
enforces approvals. Additional local CLI providers are planned (including Grok
CLI) on the same contract; they are not available in this release. Keep
credentials out of the target repository and use a disposable checkout for live
runs.

## Evidence and terminal states

Evidence is written outside the target repository by default, under
`.repogent/runs/run-<id>/`. A completed run includes `run.json`, `events.jsonl`,
`report.json`, `report.md`, approval artifacts, candidate evidence, and bounded
validation output.

Terminal statuses are `completed`, `completed_with_findings`,
`changes_requested`, `cancelled`, and `human_intervention_required`. Only the
two completed states return a successful CLI exit.

Trust labels: `REDUCED ISOLATION` for any local run, `ISOLATED VERIFIED` only
for a passing Docker run, otherwise `UNVALIDATED`.

## Scope

Repogent does not provide autonomous deployment, arbitrary model-authored
commands, automatic dependency installation, background workers, hosted
mutation, or non-Python repository support. These are deliberate boundaries.

## Roadmap

Future work will extend Repogent without weakening approval or evidence
boundaries:

- a Grok CLI proposal provider on the existing `ModelProvider` contract;
- additional read-only and mutation capabilities on the capability kernel;
- GitHub and headless CI integrations that preserve human authorization;
- published benchmarks and broader fixture coverage;
- broader Python project and validator-image support; and
- optional interfaces for reviewing runs and evidence.

These are planned directions, not capabilities of 0.4.0.

## Development setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
make verify
# or: make verify PYTHON=.venv/bin/python
```

The gate runs tests, coverage, lint, format, type checking, security checks,
package build and inspection, plugin checks, and real stdio integration. See
[CONTRIBUTING.md](CONTRIBUTING.md).
