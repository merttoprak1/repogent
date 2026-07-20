# Security model

Repository content and tests are untrusted. Repogent reduces authority; it does not make untrusted execution risk-free.

## Controls

- Repository instructions are delimited as untrusted data in every role prompt.
- Traversal does not follow repository symlinks, excludes common credential paths, and fails closed on fixed file-count, aggregate-byte, directory-entry, depth, and elapsed-time limits. Patch paths must remain below the resolved root.
- Binary, protected-path, malformed, oversized, absolute, and traversal patches are rejected.
- Models cannot choose commands. Validation uses fixed argument arrays from an allowlist without a shell.
- Docker is the default, disables network access, mounts the checkout read-only for validation, uses a read-only container filesystem, and applies CPU, memory, PID, and time limits.
- Preflight runs before provider construction. It fingerprints repository/configuration state, records readiness evidence, and stops without model spend on a failed required check.
- The local fallback is explicit, has a minimal environment, and is weaker than Docker; Docker never silently falls back to local execution.
- Host credentials are not forwarded. Configured and common secret forms are recursively redacted at the live-provider boundary and structurally sanitized before JSON persistence.
- One monotonic workflow deadline caps inspection, live-provider requests, and deterministic validation commands; approval waits are rechecked before any mutation.
- Patch application snapshots every touched path and restores it if application fails.
- Failed validation preserves the approved checkout state and reports that it is unvalidated.
- Candidate patches run in disposable copied workspaces and must restore their baseline before evidence selection. A real-checkout fingerprint is rechecked before application; the approved patch applies once, and final validation runs in another disposable workspace.
- Event logs are monotonic and JSON evidence is sanitized and versioned. Partial evidence is retained when provider, recovery, validation, or persistence failures require human intervention.

## Residual risks

Untrusted tests can consume resources, exploit container-runtime or kernel vulnerabilities, and inspect any data deliberately mounted into the container. Static Python analysis can be incomplete for dynamic imports, reflection, generated code, runtime framework wiring, and non-Python sources; ambiguity is surfaced for review instead of treated as confidence. The fixed validator image supports the MVP demo dependency set, not arbitrary project dependencies. Operators should run Repogent on disposable checkouts, keep Docker and the host patched, review every patch, and never mount credentials. Benchmarking, headless CI, and GitHub adapters are deferred; their future integrations must preserve these boundaries.
