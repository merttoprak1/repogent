# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `grok` proposal provider using the xAI API (`XAI_API_KEY`, default model
  `grok-4.6`) on the existing typed-proposal contract.
- Explicit doctor **degraded** mode when required checks pass but Docker
  isolation is unavailable (`READY (degraded)`). This is not a silent local
  fallback.
- `repogent demo`: labeled scripted replay against a disposable copy of the
  bundled FastAPI fixture. Not a live model call.
- The `demo` install extra adds the dependencies for the replay.
- Composite GitHub Action for read-only `repogent doctor`. Default provider is
  `scripted`; no secrets and no SARIF upload.

### Changed

- Verified Change requires a passing pytest test before it can apply a patch.
- Reports show why Repogent ranked the top locations. They label the model's
  criteria claims separately from check results.
- The source selector gives explicit function names priority over general
  matches. Each snippet has a size limit and retains the definition.
- Patch proposals copy the acceptance criteria from the requirements. This
  keeps the model's claims matched to the approved criteria.
- Human approval waits pause the execution budget. Executor inspection and
  preparation retain their deadlines.
- Docker validation runs Ruff without a cache and keeps the mypy cache in
  temporary storage. This prevents cache writes to the read-only checkout.

## [0.4.0] - 2026-08-23

### Added

- PyPI packaging metadata, classifiers, and a GitHub Release workflow that
  publishes with trusted publishing (OIDC). No long-lived PyPI token is stored
  in the repository.
- `repogent --version` / `-V`.
- `repogent report <run-directory>` reprints `report.md` without following
  symlinks.
- A single closed allowlist of proposal providers (`openai`, `codex-cli`,
  `scripted`) so a later local CLI provider can register in one place.

### Changed

- `DoctorRequest.executor` now defaults to `deferred`, matching the CLI doctor
  command and Codex skills. A missing Docker daemon is an unavailable isolation
  option, not a base-readiness failure, unless `executor=docker` is explicit.
- Human doctor output ends with one `next:` action.
- `report.md` leads with status, trust label, checkout state, applied paths,
  final validation, and recovery guidance when the checkout changed.
- README and plugin copy are Codex-first and share one tagline.

### Security

- CLI report reads use `O_NOFOLLOW` and reject non-regular files, matching the
  MCP report path.

## [0.3.1] - 2026-07-31

Previous public release. See GitHub tag `v0.3.1`.

[0.4.0]: https://github.com/merttoprak1/repogent/releases/tag/v0.4.0
[0.3.1]: https://github.com/merttoprak1/repogent/releases/tag/v0.3.1
