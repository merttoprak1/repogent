# Repogent

**From issue to verified patch.**

> Repogent is an auditable multi-agent software engineering platform that transforms feature requests into tested, reviewed, and traceable repository changes.

Repogent is an open-source, synchronous, approval-gated CLI for narrowly scoped Python changes. It covers conventional Python libraries, command-line packages, data transforms, and the bundled FastAPI web-service MVP. Model roles propose typed requirements, plans, patches, repairs, and QA findings. Deterministic services alone inspect the repository, validate and apply diffs, select validation commands, execute checks, enforce limits, and update workflow state.

Repogent is open-source software released under the [MIT License](LICENSE).

## Setup

Repogent requires Python 3.11 or newer. From this repository, install the package and development tools:

```bash
python -m pip install -e '.[dev]'
```

Run the deterministic project gate with `make verify`. Docker is optional for development, but it is the default executor for Repogent runs.

## Analyze a repository

`analyze` is read-only. It prints a JSON inventory, deterministic Python symbol graph, and request-ranked localization. This command is covered by the local reliability integration fixtures:

```bash
repogent analyze ./tests/fixtures/python_library --request "Reject inverted clamp bounds"
```

The static graph understands conventional root packages and `src/` layouts (plus simple setuptools `package-dir` configuration), while preserving original filesystem paths in evidence. Dynamic imports, reflection, generated code, runtime framework wiring, and non-Python code can still reduce localization confidence. Repogent records ambiguity rather than pretending an uncertain location is decisive.

## v0.2 local workflow

Repogent describes five conceptual, user-facing phases — **Understand → Localize → Propose → Validate → Decide** — rather than claiming those labels are literal emitted `RunStage` event values:

1. **Understand** — preflight, bounded repository inventory, deterministic Python symbol graph construction, and typed requirements/specification generation with its approval gate.
2. **Localize** — consume the already-built graph to localize after requirements and acceptance criteria are known.
3. **Propose** — generate the implementation plan after localization, obtain plan approval, then generate a candidate patch.
4. **Validate** — policy-check and validate candidates only in disposable copies, retaining their evidence.
5. **Decide** — select evidence or surface ambiguity for human review; after patch approval, apply once to the real checkout, run isolated final validation and QA, then finalize the run.

Repogent starts with `candidate-1`. It can generate `candidate-2` and then `candidate-3` only when validation fails, localization remains ambiguous, the patch is high risk or broad, or acceptance coverage is incomplete. It never exceeds three candidates. Equal eligible evidence is an ambiguity, not an arbitrary selection; no patch is applied and the terminal status is `human_intervention_required`.

```text
[stage] analyzed
[approval] requirements approved
[approval] plan approved
[candidate] candidate-1 generated
[validation] candidate validation completed (4 passed, 0 failed, 0 skipped)
[approval] patch approved
[validation] final validation completed (4 passed, 0 failed, 0 skipped)
[terminal] workflow finished: completed
```

## Reproducible scripted demo

Repogent modifies the approved target checkout after the requirements, plan, and patch are each approved. Copy the bundled demo before running it so the tracked example remains unchanged:

```bash
REPOGENT_DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/repogent-demo.XXXXXX")"
cp -R examples/fastapi_demo/. "$REPOGENT_DEMO_DIR"/
repogent run --repository "$REPOGENT_DEMO_DIR" --request "Add a health endpoint" \
  --provider scripted --script ./examples/scripted_run.json \
  --executor local --output-dir ./.repogent/runs
```

Review each displayed artifact and answer `y` at the three approval prompts. A successful run adds `GET /health` and its test to `$REPOGENT_DEMO_DIR`, validates the checkout, performs independent scripted QA, and prints the external evidence directory. The local executor is selected explicitly here so the demo works without Docker; it is a weaker development fallback that runs allowlisted argument arrays on the host with a minimal environment and timeouts. It is not equivalent to container isolation.

## Docker validator image

Docker is the default executor. Preflight checks the repository, selected executor, and availability of every deterministic validation command before a provider is constructed; a missing required command writes terminal evidence and stops without spending model budget, while a missing optional command is a warning. Pytest becomes required when bounded discovery finds nested `test_*.py` or `*_test.py` files, test directories, or supported pytest configuration. Before using Docker, review and approve the pinned packages in `docker/validator.Dockerfile`, then build the fixed local image:

```bash
make validator-image
```

The runtime uses `repogent-validator:py311` with `--pull=never`, no network, a read-only checkout mount and container filesystem, bounded CPU, memory, PIDs, output, and time. The image supports the bundled MVP dependency set, not arbitrary project dependencies. Repogent does not install target-repository dependencies automatically. If Docker or the image is unavailable, Repogent does not silently downgrade: choose `--executor local` explicitly when its weaker host boundary is acceptable.

## Live OpenAI run

Use a disposable checkout, review every approval artifact, keep the evidence directory outside the target repository, and do not expose credentials to repository code:

```bash
OPENAI_API_KEY=... repogent run --repository /path/to/fastapi-repo \
  --request "Add a health endpoint" --provider openai --executor docker \
  --output-dir ./.repogent/runs
```

The OpenAI provider uses structured outputs. Provider-facing context is centralized and bounded: requirements receive inventory metadata without file bodies; later roles receive the highest-ranked locations and bounded snippets; repair and QA receive capped command summaries with explicit truncation. Docker remains the default if `--executor` is omitted. If Docker or the fixed validator image is unavailable, `DockerExecutor` reports unavailable execution as skipped rather than silently switching to local execution; select `--executor local` only when you accept the weaker boundary. `ValidationPipeline` treats an unavailable required check as failed, while optional checks are skipped.

## Approvals and mutation

A normal successful run pauses for approval of:

1. extracted requirements;
2. the implementation plan;
3. the exact policy-checked unified diff.

A rejection or normal user interruption ends the run as `cancelled`. The checkout is unchanged before patch approval. Candidate patches are policy-checked and evaluated in disposable copies, then fully restored before evidence selection. After approval, the selected patch applies once to the real checkout; an application failure restores every touched path. Final validation is isolated from that checkout. If any later validation, QA, event, or artifact step fails, the manifest and report explicitly say that the real patch remains applied, list its paths and final-validation state, and give the next recovery action.

## Evidence

`--output-dir` names the external base directory. If it is omitted, Repogent uses a safe `.repogent/runs` evidence root beside the target repository. Repogent creates a unique `run-<id>/` beneath the selected root and refuses an evidence directory inside the target repository. Each run contains:

- atomic `run.json` state, a monotonic `events.jsonl`, and a final `report.md`;
- numbered `inventory-*.json`, `symbol-graph-*.json`, `localization-*.json`, and role inputs and outputs;
- `candidate-*.json`, `candidate-evidence-*.json`, `candidate-selection-*.json`, approvals, proposed/applied diffs, and repair history;
- raw validation status, fixed argument arrays, output, exit codes, durations, and reasons for skipped checks;
- provider usage and the independent QA result.

Versioned domain/model artifacts currently declare `schema_version: "1"` (for example, manifests, events, localization, candidates, evidence, selection, and validation models). Raw role-input JSON/text payload artifacts, such as `requirements-input`, `planning-input`, `candidate-input`, and `qa-input`, preserve bounded provider context but are not versioned model envelopes. Stage artifacts are append-only, while `run.json` is atomically replaced as the state changes. It records fingerprints, candidate IDs, selected-candidate and real-checkout apply state, applied paths, final-validation state, recovery guidance, generated-but-not-consumed outputs, event linkage, and terminal reason. A provider output and its usage are persisted before its budget is enforced, so an output that crosses a limit remains inspectable but is not approved, evaluated, or used to start another stage. Common credential forms and configured secrets are redacted before persistence, but evidence still deserves careful handling.

## Terminal statuses

- `completed`: deterministic validation passed and QA approved.
- `completed_with_findings`: validation passed and QA reported non-blocking findings.
- `changes_requested`: validation passed but QA found blocking issues.
- `cancelled`: a human rejected an approval gate or interrupted the workflow.
- `human_intervention_required`: policy, provider, timeout, budget, ambiguous evidence, validation integrity, or candidate limits stopped the run.

Only `completed` and `completed_with_findings` produce a successful CLI exit. A skipped optional validation tool is visibly recorded with its reason; it is never represented as having passed. Candidate evaluation restoration and the selected patch's real-checkout state are tracked separately. If recovery cannot be proved, a required check fails, the real checkout drifts before application, or final isolated validation differs from candidate evidence, Repogent stops for human intervention and retains the partial evidence and exact recovery state in the report.

## Security and scope

Repository content and tests are untrusted. Docker reduces their authority but cannot make execution risk-free. Use disposable checkouts, keep Docker and the host patched, inspect the validator image and every patch, never mount credentials, and read the [security model](docs/security.md). See [architecture](docs/architecture.md) for component boundaries and workflow states.

The CLI intentionally uses conservative fixed workflow budgets and patch limits. Applications that need custom `Budget` or `PatchLimits` values can configure them through the Python API; the MVP does not expose limit flags.

The MVP deliberately defers:

- a real-repository benchmark harness and published metrics;
- headless CI policy, stable exit codes, and reusable workflows;
- GitHub issue, status-check, notification, and pull-request integration;
- semantic embeddings, Qdrant, or another vector database;
- LangGraph;
- PostgreSQL or resumable background workers;
- a web dashboard or FastAPI control API;
- automated dependency installation;
- arbitrary model-authored commands;
- autonomous deployment;
- support for languages other than Python;
- the 20–30-task benchmark suite.

These are future milestones, not capabilities of this release.
