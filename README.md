# Repogent

**Approval-gated, evidence-backed Python repository changes for Codex.**

[![CI](https://github.com/merttoprak1/repogent/actions/workflows/ci.yml/badge.svg)](https://github.com/merttoprak1/repogent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2a6db2.svg)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/badge/lint-ruff-d7ff64.svg)](https://docs.astral.sh/ruff/)

Repogent is an open-source CLI and Codex plugin for turning scoped repository
requests into safe, reviewable Python changes. A model can propose requirements,
a plan, and a patch; deterministic services retain control of repository scope,
patch policy, validation, evidence, and mutation. A change is never applied
until you approve the exact displayed patch.

Repogent is released under the [MIT License](LICENSE).

## Why Repogent

Most coding agents optimize for producing a patch. Repogent optimizes for being
able to explain and verify that patch:

- **Explicit human control** — requirements, plan, executor, and exact patch
  decisions are bounded to the artifact being approved.
- **Deterministic guardrails** — Git-bounded scope, typed MCP contracts, patch
  policy, and allowlisted validation commands do not depend on model judgment.
- **Evidence you can inspect** — each terminal run preserves its checkout state,
  validation result, trust label, and bounded audit artifacts.
- **Honest execution boundaries** — Docker is the default validator; local
  execution requires explicit reduced-isolation consent and is never a silent
  fallback.

This makes Repogent useful both as a practical Codex capability and as a
reference implementation for building AI developer tooling with clear authority
boundaries.

## What it does

The Codex plugin exposes two focused capabilities:

- **Repository Readiness** inspects a Git-bounded repository, provider
  readiness, validation-command availability, and executor options without
  editing files or running repository code.
- **Verified Change** prepares a bounded change through explicit requirements,
  plan, executor, and patch approvals. It validates the selected patch before
  it can touch the real checkout.

Repogent supports conventional Python packages, CLIs, data transforms, and the
bundled FastAPI example. The standalone `repogent doctor`, `repogent analyze`, and `repogent run`
commands remain available for terminal workflows and automation.

## Install for Codex

Repogent requires Python 3.11 or newer. Codex starts the plugin's MCP server
with the bare `repogent` command, so install it on a persistent `PATH`; a
repository-only virtual environment is not sufficient for Codex Desktop.

Install [pipx](https://pipx.pypa.io/) with your operating-system package
manager, then run:

```bash
pipx install 'git+https://github.com/merttoprak1/repogent.git'
pipx ensurepath
command -v repogent
codex plugin marketplace add merttoprak1/repogent
```

`command -v repogent` must print an executable path. If it does not, open a new
login shell after `pipx ensurepath`. Then fully restart Codex Desktop, install
**Repogent** from the Plugin Directory, and start a new task.

## Use it from Codex

Start with readiness when you want a safe diagnosis:

```text
Use Repogent Repository Readiness for /path/to/repository.
```

For a change, invoke the mutating capability explicitly:

```text
Use Repogent Verified Change to safely add a health endpoint to /path/to/repository.
Show the requirements, plan, and exact patch before applying it.
```

Repogent first inspects repository readiness, then starts the verified-change
workflow with a deferred executor. Requirements, the plan, and the final patch
each require a separate approval bound to the displayed digest. Executor
selection is a separate target-bound decision, not an additional patch
approval.

## Safety model

Repogent treats repository content and tests as untrusted.

- It uses a Git-bounded input scope and explicit size limits.
- It previews and validates candidate patches in disposable copies.
- Docker is the default validation executor; local execution requires explicit
  acceptance of reduced isolation.
- It never silently falls back from Docker to local execution and never
  installs target-repository dependencies automatically.
- It records durable evidence, checkout state, validation status, trust label,
  and bounded typed errors for every terminal run.

Only the final, explicitly approved patch can modify the real checkout. If
recovery cannot be proved, validation is incomplete, or evidence is ambiguous,
Repogent stops and asks for human intervention rather than guessing.

Read the full [security model](docs/security.md) and
[architecture](docs/architecture.md) before using Repogent on sensitive code.

## Development setup

Create an independent editable environment for repository development:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the same quality gate used by CI:

```bash
make verify
# or, when using an existing environment:
make verify PYTHON=.venv/bin/python
```

The gate runs tests, coverage, linting, formatting, type checking, security
checks, package build and inspection, isolated wheel installation, plugin
checks, and real stdio integration.

## CLI quick start

`doctor` is read-only. It reports whether a repository can enter a workflow, and
separates required base checks from optional executor availability:

```bash
repogent doctor ./tests/fixtures/python_library
```

The default provider is `codex-cli`, which reuses a local Codex login. Use
`--provider openai` only when `OPENAI_API_KEY` is set for the Repogent process.
The default executor for diagnosis is `deferred`, so a missing Docker daemon is
an unavailable isolation option rather than a base-readiness failure.

`analyze` is also read-only and prints a bounded repository inventory, deterministic
Python symbol graph, and request-ranked localization:

```bash
repogent analyze ./tests/fixtures/python_library \
  --request "Reject inverted clamp bounds"
```

For a reproducible local demo, copy the bundled project first so tracked files
remain unchanged:

```bash
REPOGENT_DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/repogent-demo.XXXXXX")"
cp -R examples/fastapi_demo/. "$REPOGENT_DEMO_DIR"/
repogent run --repository "$REPOGENT_DEMO_DIR" \
  --request "Add a health endpoint" \
  --provider scripted --script ./examples/scripted_run.json \
  --executor local --output-dir ./.repogent/runs
```

The demo asks for three approvals: requirements, plan, and exact patch. The
explicit local executor keeps the demo usable without Docker, but it is a
weaker boundary than container isolation.

## Executors and providers

`repogent run` defaults to the Codex CLI provider and the Docker executor. Build
the reviewed validator image before a Docker-backed local run:

```bash
make validator-image
```

The fixed image runs without network access, uses a read-only checkout mount,
and applies bounded CPU, memory, process, output, and time limits. If Docker or
the image is unavailable, choose local execution explicitly; Repogent will not
downgrade silently.

Repogent can use OpenAI structured outputs or a locally authenticated Codex CLI
as proposal intelligence. These providers propose artifacts only; Repogent
still independently validates schemas and patches, records evidence, and
enforces approvals. Keep credentials out of the target repository and use a
disposable checkout for live runs.

## Evidence and terminal states

Evidence is written outside the target repository by default, under
`.repogent/runs/run-<id>/`. A completed run includes `run.json`, `events.jsonl`,
`report.json`, `report.md`, approval artifacts, candidate evidence, and bounded
validation output. The report records whether the checkout changed, final
validation, trust label, and recovery guidance.

Terminal statuses are `completed`, `completed_with_findings`,
`changes_requested`, `cancelled`, and `human_intervention_required`. Only the
two completed states return a successful CLI exit.

## Scope

Repogent intentionally does not provide autonomous deployment, arbitrary
model-authored commands, automatic dependency installation, background workers,
or non-Python repository support. These are deliberate boundaries, not hidden
capabilities.

## Roadmap

Future work will extend Repogent without weakening its explicit approval and
evidence boundaries:

- additional read-only and mutation capabilities built on the capability kernel;
- GitHub and headless CI integrations that preserve human authorization;
- published benchmark scenarios, reliability metrics, and broader fixture
  coverage;
- broader Python project and validator-image support; and
- optional operational interfaces for reviewing runs and evidence.

These are planned directions, not capabilities of the current release.
