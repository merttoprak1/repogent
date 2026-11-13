# Repogent

**Review and test AI-proposed Python changes before applying the exact patch.**

[![CI](https://github.com/merttoprak1/repogent/actions/workflows/ci.yml/badge.svg)](https://github.com/merttoprak1/repogent/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Repogent is an open-source CLI and Codex plugin for small changes to Python
repositories. A model proposes requirements, a plan, and candidate patches.
Repogent controls repository scope, runs fixed checks in disposable copies,
and asks you to approve the exact diff before it applies a change.

Use it when you want to inspect both the proposed code and the test evidence
before changing your checkout.

[Quickstart](#quickstart) · [Codex Desktop](#use-with-codex-desktop) ·
[Workflow](#how-a-verified-change-works) · [CLI](#use-the-cli) ·
[Validation](#validation-and-isolation) · [Reports](#read-the-report) ·
[Development](#development)

## What you can do

| Capability | What it gives you |
| --- | --- |
| **Repository Readiness** | Checks repository scope, provider readiness, and executor options without running target code or editing files. |
| **Verified Change** | Proposes a bounded change, tests candidates, requests separate approvals, and records what happened before and after application. |

The current scope covers conventional Python packages, CLIs, data transforms,
and the bundled FastAPI example. Patch application requires an existing pytest
suite and a runtime that has the project's dependencies.

## Quickstart

Install **Python 3.11 or newer**, **Git**, and [pipx](https://pipx.pypa.io/stable/installation/).
The commands below use a POSIX shell. Docker and a provider account are optional
for the demo.

### 1. Install from source

These instructions install the checkout itself, so the runtime and examples
use the same code:

```bash
git clone https://github.com/merttoprak1/repogent.git
cd repogent
pipx install '.[demo]'
pipx ensurepath
```

Open a new terminal if pipx changed your PATH. Then check the installation:

```bash
command -v repogent
repogent --version
```

The `demo` extra installs FastAPI, HTTPX, and pytest alongside the core runtime.
If Repogent is already installed through pipx, run
`pipx install --force '.[demo]'` from this checkout to replace that installation.

### 2. Run the replay

```bash
repogent demo
```

The demo copies the bundled FastAPI fixture to a temporary directory, adds
`GET /health`, runs checks, and prints an evidence directory.

It uses recorded model outputs and automatically approves those recorded
artifacts. It makes no live model call and does not edit the bundled source.
The demo runs locally and reports **REDUCED ISOLATION**.

For live changes, you review and approve the requirements, plan, and final
patch yourself.

## Use with Codex Desktop

Install the runtime first. The plugin starts `repogent mcp --stdio`, so Codex
Desktop must find the `repogent` executable on its persistent PATH. A virtual
environment activated in one terminal does not give Desktop that PATH.

With the Codex CLI installed, run these commands from the Repogent checkout:

```bash
codex plugin marketplace add .
codex plugin add repogent@repogent-local
codex plugin list
```

This installs Repogent from its
[repository marketplace](https://developers.openai.com/plugins/build/plugins#how-local-marketplaces-work).
Enable the plugin in Codex Desktop and open a new chat. Restart Desktop if you
changed PATH after starting it.

Start with a readiness request:

```text
Use Repogent Repository Readiness for /path/to/my-project.
```

For a change, name the workflow explicitly:

```text
Use Repogent Verified Change for /path/to/my-project.
Reject inverted bounds in clamp and add a regression test.
Show me the requirements, plan, test results, and exact patch before applying it.
```

The default provider uses your Codex CLI sign-in. Before a live run, make sure
the CLI is signed in and the target repository is trusted in Codex. See
[Providers](#providers) for API-based alternatives.

## How a verified change works

The Codex workflow keeps content approval separate from permission to run tests:

1. **Approve the requirements.** Review the requested behavior and acceptance
   criteria before Repogent plans the change.
2. **Approve the plan.** Review the proposed approach and the source locations
   selected for the model's context.
3. **Inspect a preview and choose an executor.** Choose Docker or explicitly
   accept local execution. Repogent then tests the candidate in a disposable copy.
4. **Approve the exact validated patch.** Review the diff, required checks,
   optional findings, and trust label. This decision authorizes application.
5. **Read the final report.** Repogent applies the approved patch once, checks
   another disposable copy, and records the checkout state and final result.

There are three content approvals: requirements, plan, and patch. In the
plugin, each approval includes a fingerprint of the displayed artifact.
A changed artifact needs a new decision. The executor choice is a separate
decision bound to the current validation target.

Repogent can evaluate up to three candidates. Each new candidate preview needs
its own executor decision. If the evidence cannot select a candidate, the run
stops for human review.

Human review pauses the execution budget. Provider calls, repository inspection,
executor preparation, and validation still consume that budget.

## Use the CLI

The CLI uses the same engine as the plugin. It asks for content approvals in
the terminal. Select the executor with `--executor` when starting a run.

### Check readiness and inspect relevant code

```bash
repogent doctor /path/to/my-project

repogent analyze /path/to/my-project \
  --request "Reject inverted clamp bounds"
```

`doctor` checks readiness without editing files or running target code. Its
default executor is `deferred`: missing Docker can produce **READY (degraded)**
while the repository remains ready for a preview. This does not authorize
local execution or mean that tests passed.

`analyze` prints a bounded inventory, Python symbol graph, and relevant source
locations as JSON. It does not call a model.

### Start a live change

Build the validator image from the Repogent checkout before a Docker run:

```bash
docker build -t repogent-validator:py311 -f docker/validator.Dockerfile .
```

Then run a change against your target project:

```bash
repogent run \
  --repository /path/to/my-project \
  --request "Reject inverted bounds in clamp and add a regression test." \
  --provider codex-cli \
  --executor docker
```

For host execution, explicitly choose `--executor local`. Local tests can access
resources available to the host user. Use that mode only for repositories you
trust. Docker never silently falls back to local execution.

Use `repogent --help` or `repogent run --help` for the available options.

## Validation and isolation

Models propose code. They do not choose shell commands. Repogent runs these
fixed checks, with bounded output and execution time:

| Check | Command | Effect on patch application |
| --- | --- | --- |
| pytest | `python -m pytest -q` | The suite must pass and report at least one passing test. |
| Ruff | `python -m ruff check .` | Optional findings remain in the evidence. |
| mypy | `python -m mypy .` | Optional findings remain in the evidence. |
| Bandit | `python -m bandit -q -r .` | Optional findings remain in the evidence. |

A missing or failing required check blocks application. Collecting tests or
skipping every test does not count as a passing test suite. A project without
discoverable required tests cannot produce an applicable patch.

Optional checks can fail while required validation passes. The report retains
those failures and records unavailable tools as skipped. A successful run does
not mean that every optional check passed.

### Choose the execution boundary

| Mode | Where checks run | Boundary |
| --- | --- | --- |
| **Docker** | A disposable copy mounted inside the validator container. | No network, read-only checkout and container filesystem, plus CPU, memory, process, and time limits. |
| **Local** | A disposable copy on the host. | Fixed commands with reduced isolation. Repository code still has the host user's access. |

The bundled image uses Python 3.11 and includes pytest, Ruff, mypy, Bandit,
FastAPI, and HTTPX. It does not install target-project dependencies automatically.
Projects that need other dependencies require a reviewed runtime prepared by
the operator. Readiness checks tool availability, not complete dependency
compatibility or test success.

In Git repositories, Repogent selects tracked and non-ignored untracked files.
Inspection and patch policy enforce path and size limits. Provider requests use
bounded repository context. See the [security model](docs/security.md) for the
controls and their limits.

## Providers

| `--provider` | How proposals are produced | What you need |
| --- | --- | --- |
| `codex-cli` (default) | Local `codex exec` calls. | Codex CLI on PATH, a valid sign-in, and repository trust. |
| `openai` | OpenAI API calls. | `OPENAI_API_KEY` in the Repogent process environment. |
| `grok` | xAI API calls. | `XAI_API_KEY` in the Repogent process environment. |
| `scripted` | Recorded JSON outputs. | `--script /path/to/recorded-run.json`. |

Use `--model` to override a live provider's default model. Keep credentials
outside the target repository. Live providers receive repository context and
can incur usage charges. The local plugin does not make model inference offline.

The `grok` provider uses the xAI API. A Grok CLI provider is not implemented.

## Read the report

Every run prints its evidence directory. For normal runs, the default location
is `<target-parent>/.repogent/runs/run-<id>/`, outside the target checkout.
Use `--output-dir` to choose another location outside that checkout.

To reopen a report, use the directory printed after `Evidence:`:

```bash
repogent report /path/to/run-directory
```

The directory contains `run.json`, `events.jsonl`, `report.json`, `report.md`,
approval records, candidate evidence, and bounded check output.

The report separates observed check results from the model's claims about
acceptance criteria. It also shows whether the patch was applied, which paths
changed, and whether final validation passed.

| Trust label | Meaning |
| --- | --- |
| **ISOLATED VERIFIED** | Docker isolation was used and required validation passed. |
| **REDUCED ISOLATION** | The run used local execution, regardless of test outcome. |
| **UNVALIDATED** | The conditions for an isolated, verified result were not met. |

Inspect `checkout_state`, `applied_paths`, and `final_validation_status` after
an interruption. A failure after application can leave the approved patch in
place. The report gives the observed state and any manual recovery action.

## Current limits

- The supported change workflow targets Python repositories with pytest.
- Passing tests do not prove that a patch satisfies every requirement.
- The native Codex Desktop interface still needs a manual end-to-end check.
  The CLI, local MCP transport, and Docker workflow have live validation evidence.
- The composite GitHub Action is experimental. Known flag and report-path issues
  are recorded in the [product scope](docs/product-scope.md#release-work).
- Automatic dependency installation, hosted execution, deployments, automatic
  pull requests, and non-Python workflows are outside the current scope.

A [documented live case](docs/product-scope.md#live-validation-on-2026-11-13)
used more-itertools 10.8.0. Its regression test failed on the original code.
After exact-patch approval, Docker reported 696 passing tests and one skipped
test. Existing type and security findings remained visible in the report.

## Troubleshooting

| Symptom | Next action |
| --- | --- |
| `repogent` is missing in Desktop | Check `command -v repogent`, run `pipx ensurepath`, and restart Desktop. |
| `READY (degraded)` | Read the unavailable executor reason. Start Docker or build the validator image if you want isolated validation. |
| No required tests were found | Add a discoverable pytest suite before requesting patch application. |
| A dependency is missing during validation | Prepare a runtime with that dependency. Repogent does not install it from repository instructions. |
| Codex provider readiness fails | Check the CLI sign-in and target repository trust. The doctor report includes a remediation. |

## Development

From the Repogent checkout:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
make verify
```

`make verify` runs pytest with an 85% coverage minimum, Ruff lint and format
checks, strict mypy, Bandit, a package build, and package/plugin integration checks.
CI runs this gate on Python 3.11, 3.12, 3.13, and 3.14.

Docker integration tests require a running daemon and the validator image.
Provider tests marked `manual` require an authenticated external provider.

Read the [contribution guide](CONTRIBUTING.md), [architecture](docs/architecture.md),
and [changelog](CHANGELOG.md) for implementation and release details.
Report security issues through [SECURITY.md](SECURITY.md).

Repogent is released under the [MIT License](LICENSE).
