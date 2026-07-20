# Repogent

**From issue to verified patch.**

> Repogent is an auditable multi-agent software engineering platform that transforms feature requests into tested, reviewed, and traceable repository changes.

Repogent's MVP is a synchronous, approval-gated CLI for narrowly scoped changes to Python FastAPI repositories. Model roles propose typed requirements, plans, patches, repairs, and QA findings. Deterministic services alone inspect the repository, validate and apply diffs, select validation commands, execute checks, enforce limits, and update workflow state.

## Setup

Repogent requires Python 3.11 or newer. From this repository, install the package and development tools:

```bash
python -m pip install -e '.[dev]'
```

Run the deterministic project gate with `make verify`. Docker is optional for development, but it is the default executor for Repogent runs.

## Analyze a repository

`analyze` is read-only. It prints a JSON inventory and request-ranked lexical context:

```bash
repogent analyze ./examples/fastapi_demo --request "Add a health endpoint"
```

## Reproducible scripted demo

Repogent modifies the approved target checkout after the requirements, plan, and patch are each approved. Copy the bundled demo before running it so the tracked example remains unchanged:

```bash
rm -rf /tmp/repogent-demo
mkdir -p /tmp/repogent-demo
cp -R examples/fastapi_demo/. /tmp/repogent-demo/
repogent run --repository /tmp/repogent-demo --request "Add a health endpoint" \
  --provider scripted --script ./examples/scripted_run.json \
  --executor local --output-dir ./.repogent/runs
```

Review each displayed artifact and answer `y` at the three approval prompts. A successful run adds `GET /health` and its test to `/tmp/repogent-demo`, validates the checkout, performs independent scripted QA, and prints the external evidence directory. The local executor is selected explicitly here so the demo works without Docker; it is a weaker development fallback that runs allowlisted argument arrays on the host with a minimal environment and timeouts. It is not equivalent to container isolation.

## Docker validator image

Docker is the default executor. Before using it, review and approve the pinned packages in `docker/validator.Dockerfile`, then build the fixed local image:

```bash
make validator-image
```

The runtime uses `repogent-validator:py311` with `--pull=never`, no network, a read-only checkout mount and container filesystem, bounded CPU, memory, PIDs, output, and time. The image supports the bundled MVP dependency set, not arbitrary project dependencies. Repogent does not install target-repository dependencies automatically.

## Live OpenAI run

Use a disposable checkout, review every approval artifact, keep the evidence directory outside the target repository, and do not expose credentials to repository code:

```bash
OPENAI_API_KEY=... repogent run --repository /path/to/fastapi-repo \
  --request "Add a health endpoint" --provider openai --executor docker \
  --output-dir ./.repogent/runs
```

The OpenAI provider uses structured outputs. Docker remains the default if `--executor` is omitted. If Docker or the fixed validator image is unavailable, checks are recorded as skipped rather than silently switching to local execution; select `--executor local` only when you accept the weaker boundary.

## Approvals and mutation

A normal successful run pauses for approval of:

1. extracted requirements;
2. the implementation plan;
3. the exact policy-checked unified diff.

A rejection ends the run as `cancelled`. The checkout is unchanged before patch approval. After approval, patch application is transactional: an application failure restores every touched path. A validation failure deliberately leaves the approved patch in place and records it as unvalidated so it can be inspected. If validation fails and a repair is proposed, each repair diff passes the same policy and requires another explicit approval; at most two repair attempts are permitted.

## Evidence

`--output-dir` names the external base directory. Repogent creates a unique `run-<id>/` beneath it and refuses an evidence directory inside the target repository. Each run contains:

- atomic `run.json` state and a final `report.md`;
- numbered inventories, retrieved context, and role inputs and outputs;
- provider usage, approvals, proposed/approved/applied diffs, and any repair history;
- raw validation status, fixed argument arrays, output, exit codes, durations, and reasons for skipped checks;
- the independent QA result.

Stage artifacts are append-only, while `run.json` is atomically replaced as the state changes. Common credential forms and configured secrets are redacted before persistence, but evidence still deserves careful handling.

## Terminal statuses

- `completed`: deterministic validation passed and QA approved.
- `completed_with_findings`: validation passed and QA reported non-blocking findings.
- `changes_requested`: validation passed but QA found blocking issues.
- `cancelled`: a human rejected an approval gate.
- `human_intervention_required`: policy, provider, timeout, budget, or repair limits stopped the run.

Only `completed` and `completed_with_findings` produce a successful CLI exit. A skipped optional validation tool is visibly recorded with its reason; it is never represented as having passed.

## Security and scope

Repository content and tests are untrusted. Docker reduces their authority but cannot make execution risk-free. Use disposable checkouts, keep Docker and the host patched, inspect the validator image and every patch, never mount credentials, and read the [security model](docs/security.md). See [architecture](docs/architecture.md) for component boundaries and workflow states.

The MVP deliberately defers:

- semantic embeddings, Qdrant, or another vector database;
- LangGraph;
- PostgreSQL or resumable background workers;
- a web dashboard or FastAPI control API;
- GitHub issue ingestion or pull-request creation;
- automated dependency installation;
- arbitrary model-authored commands;
- autonomous deployment;
- support for languages other than Python;
- the 20–30-task benchmark suite.

These are future milestones, not capabilities of this release.
