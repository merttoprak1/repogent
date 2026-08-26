# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `grok` proposal provider using the xAI API (`XAI_API_KEY`, default model
  `grok-4.6`) on the existing typed-proposal contract.

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
