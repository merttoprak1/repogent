# Plugin Version and Cache Identity Design

## Objective

Publish the progressive-executor plugin work under version `0.2.0` so Codex
Desktop installs it into a new cache identity instead of reusing the existing
`0.1.0` package copied from `main`.

## Scope

- Change the Python package version in `pyproject.toml` from `0.1.0` to `0.2.0`.
- Change the Codex plugin manifest version in
  `plugins/repogent/.codex-plugin/plugin.json` from `0.1.0` to `0.2.0`.
- Update plugin-package regression coverage to require the plugin manifest and
  Python package versions to match and to assert the expected `0.2.0` release.
- Leave fixture project versions unchanged because they describe independent
  test repositories rather than the Repogent release.
- Do not edit `main`, marketplace identity, or the existing Codex cache.

## Rationale

The installed cache copy has the same content digest as the `main` branch's
`0.1.0` plugin and a different digest from the feature worktree. Both source
trees currently advertise version `0.1.0`, so reinstalling the feature package
does not establish a distinct versioned cache target. The progressive executor,
executor-selection gate, and trust-label reporting are feature-level changes;
`0.2.0` accurately communicates that scope while creating a new cache identity.

Changing the marketplace name would produce a test-only identity without
solving release upgrades. Requiring users to delete cache directories would be
fragile and would leave the package version ambiguous.

## Validation

1. Run the focused plugin-package regression test.
2. Run the repository's plugin/package validators used by CI.
3. Confirm the built plugin metadata and Python project metadata both report
   `0.2.0`.
4. Reinstall the plugin from the feature worktree and verify that Codex Desktop
   exposes all nine MCP tools, including `select_executor`.
5. Continue the manual Docker-free flow and verify it reaches an `UNVALIDATED`
   preview before any executor is selected.

## Failure Handling

If automated validation fails, do not reinstall the plugin; correct only the
version-alignment defect identified by the failing check. If Codex still loads
`0.1.0` after validated `0.2.0` metadata is installed, inspect the resolved
marketplace source and installed cache path before changing additional files.
