# Contributing to Repogent

Thanks for your interest. Repogent is a small, opinionated project: it trades
breadth for the ability to explain and verify every change it makes. Contributions
that preserve that property are very welcome.

## Before you start

For anything beyond a typo or a small bug fix, please open an issue first and
describe the change. Repogent's authority boundaries — approval gates, patch
policy, repository scope, executor isolation — are deliberate, and a design
conversation before the code saves work on both sides.

## Development setup

Repogent requires Python 3.11 or newer.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

This `.venv` is for repository development. It does not replace the persistent
`pipx` installation that Codex Desktop needs; see the README.

## The verification gate

One command has to pass before a pull request is ready:

```bash
make verify
```

That runs, in order: `pytest` (with coverage held at a minimum of 85%),
`ruff check`, `ruff format --check`, `mypy` in strict mode, `bandit`, and a
package check that builds the wheel and asserts its contents. CI runs the same
gate on Python 3.11, 3.12, and 3.13 — nothing extra, so a green local run
should mean a green CI run.

Some tests are marked and skipped by default:

- `docker` requires a running Docker daemon and the validator image
  (`make validator-image`);
- `manual` consumes an authenticated external provider.

## Expectations for changes

- **Tests come with the change.** New behavior needs a test that fails without it.
- **Types are not optional.** `mypy --strict` covers `src`; keep it clean rather
  than adding ignores.
- **Determinism belongs in services, not prompts.** Repository inspection, patch
  validation, command selection, limits, and state transitions are deterministic
  code. If a change makes one of them depend on model judgment, it will be
  declined.
- **Never weaken a gate for convenience.** Approvals are digest-bound to the exact
  artifact displayed. Local execution requires explicit reduced-isolation consent
  and must never become a silent fallback.
- **Keep commits focused**, with a conventional-commit subject
  (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).

## Reporting security issues

Do not open a public issue. Follow [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
