# Task 5 Report: Canonical Portable Verification Gate

## Status

Implemented in two commits:

- `5de7e1c` (`style: normalize Ruff formatting`)
- `c5b625b` (`build: unify local and CI verification`)

The first commit contains only Ruff's mechanical formatting of 45 tracked
Python files. The second makes `make verify` the single local and CI gate,
supports `PYTHON` with a `python3` default, moves package policy into executable
artifact tests, and updates operator documentation.

## RED evidence

The Make behavior tests were written before changing the gate and execute the
real Makefile with `make -n`; they never inspect Makefile or CI source text.

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest \
  tests/integration/test_quality_gate.py -q

FAILED test_make_verify_dry_run_uses_overridden_interpreter
  expected "python3 -m pytest"; observed hard-coded "python -m pytest"
FAILED test_package_check_executes_plugin_stdio_contract
  make: No rule to make target `package-check`
```

A second RED cycle protected the distribution policy. Before Make enabled the
package-test context and before a wheel was built, the dry-run contract lacked
`REPOGENT_PACKAGE_CHECK=1` and both real wheel tests failed with:

```text
expected exactly one built wheel, found []
```

The production changes that make those tests fail are a hard-coded
interpreter, an omitted verification stage, failure to activate artifact tests,
or removal of a required package/plugin/stdio test path.

## Canonical gate

`Makefile` now provides:

- `PYTHON ?= python3` and uses it for every Python-backed target;
- `test`, `lint`, `format-check`, `typecheck`, and `security` targets;
- `package-check`, which builds the distribution and runs wheel, plugin-package,
  and real stdio tests; and
- `verify`, which depends on the complete gate.

CI installs the development environment and invokes only
`make verify PYTHON=python`. The previous duplicated plugin, archive inspection,
and isolated-install shell implementations were removed.

## Executable package policy

`tests/unit/test_package_data.py` now opens the actual wheel and checks for the
MCP server, `py.typed`, license metadata, and exclusion of `.superpowers`. It
also creates a fresh virtual environment, installs that wheel with
`--no-deps --no-index`, and verifies the installed typed package resource.

These build-artifact tests skip during an ordinary `pytest` run and are
mandatory under `make package-check` through `REPOGENT_PACKAGE_CHECK=1`. This
keeps the ordinary suite runnable from a clean checkout while ensuring the
canonical gate cannot silently omit distribution validation.

The prior CI source-text assertion in `test_plugin_package.py` was removed; the
package behavior itself is now the contract.

## GREEN evidence

The complete canonical command was run from the isolated worktree:

```text
PYTHONPATH=src make verify \
  PYTHON=/Users/mert/Documents/Repogent/.venv/bin/python
```

Result:

```text
646 passed, 4 skipped
coverage: 90.75% (required: 85%)
ruff check: All checks passed
ruff format --check: 82 files already formatted
mypy src: Success, 33 source files
bandit: exit 0
build: repogent-0.3.0.tar.gz and repogent-0.3.0-py3-none-any.whl
package/plugin/stdio tests: 27 passed
```

The first sandboxed build attempt could not resolve PyPI while creating
`python -m build`'s isolated environment. The identical command was rerun with
approved network access. A subsequent package run without `PYTHONPATH=src`
correctly exposed that the shared root `.venv` points its editable install at
`main`; child stdio processes loaded that old package. Supplying
`PYTHONPATH=src` made the worktree source authoritative and the complete gate
passed. CI does not need this worktree-specific override because it installs
the checked-out source directly.

Focused final verification also passed:

```text
pytest tests/integration/test_quality_gate.py tests/unit/test_package_data.py \
  -q --no-cov
# 4 passed, 2 skipped

ruff check .
# All checks passed

ruff format --check .
# 82 files already formatted
```

## Documentation

README and architecture documentation now describe `make verify`, the
`python3` default, `make verify PYTHON=.venv/bin/python`, and the exact classes
of checks owned by the gate.

## Worktree hygiene

The pre-existing unstaged roadmap-plan change remains untouched and is not
included in either Task 5 implementation commit.
