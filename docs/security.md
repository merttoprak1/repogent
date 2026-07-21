# Security model

Repository content and tests are untrusted. Repogent reduces authority; it does not make untrusted execution risk-free.

## Controls

- Repository instructions are delimited as untrusted data in every role prompt.
- Traversal does not follow repository symlinks, excludes common credential paths, and fails closed on fixed file-count, aggregate-byte, directory-entry, depth, and elapsed-time limits. Patch paths must remain below the resolved root.
- Binary, protected-path, malformed, oversized, absolute, and traversal patches are rejected.
- Models cannot choose commands. Validation uses fixed argument arrays from an allowlist without a shell.
- Docker is the default, disables network access, mounts the checkout read-only for validation, uses a read-only container filesystem, and applies CPU, memory, PID, and time limits. Preflight probes each module or executable inside the fixed image without mounting the repository and caches readiness per image and tool.
- Preflight runs before provider construction. It fingerprints repository/configuration state, checks every fixed validation command, and stops without model spend when a required command is unavailable; optional tools produce warnings. Bounded pytest discovery fails closed on traversal limits, malformed or oversized configuration, races, and filesystem access uncertainty. Recognized root configuration is read with fd-relative metadata and no-follow checks where available; only a stable, bounded regular file is accepted, so symlinks, FIFOs, devices, sockets, and other special files are not read.
- The local fallback is explicit, has a minimal environment, and is weaker than Docker; Docker never silently falls back to local execution.
- Host credentials are not forwarded. Provider context has a deterministic global serialized-size ceiling: inventory bodies are excluded, localization is top-ranked and complete-line bounded, and a structured allocator progressively compacts low-priority bulk data while retaining critical identifiers, statuses, reasons, and explicit truncation counts. Configured and common secret forms are recursively redacted at the live-provider boundary and structurally sanitized before JSON persistence.
- One monotonic workflow deadline caps inspection, live-provider requests, and deterministic validation commands; approval waits are rechecked before any mutation.
- Before real-checkout mutation, a `recovery_unknown` write-ahead intent and touched paths must be durable. Patch application then snapshots every touched path and attempts restoration after ordinary failures, `KeyboardInterrupt`, or `SystemExit`. Until `applied` is durably recorded, any failure triggers a detect-only baseline comparison: only a proven match becomes `not_applied`; drift or uncertainty retains the write-ahead state and surfaces every path for manual inspection.
- Failed or interrupted post-apply work preserves the approved checkout state and reports the applied paths, final-validation state, and exact manual next action; disposable candidate restoration is never presented as real-checkout rollback.
- Candidate patches run in disposable copied workspaces and must restore their baseline before evidence selection. A real-checkout fingerprint is rechecked before application; the approved patch applies once, and final validation runs in another disposable workspace.
- Event logs are monotonic and JSON evidence is sanitized and versioned. One outer terminalization boundary retains partial evidence for provider, budget, interruption, recovery, validation, event, or persistence failures. Generated typed output and usage are written before budget enforcement and marked when they were not consumed.

## Residual risks

Untrusted tests can consume resources, exploit container-runtime or kernel vulnerabilities, and inspect any data deliberately mounted into the container. Static Python analysis can be incomplete for dynamic imports, reflection, generated code, runtime framework wiring, and non-Python sources; ambiguity is surfaced for review instead of treated as confidence. The fixed validator image supports the MVP demo dependency set, not arbitrary project dependencies. Operators should run Repogent on disposable checkouts, keep Docker and the host patched, review every patch, and never mount credentials. Benchmarking, headless CI, and GitHub adapters are deferred; their future integrations must preserve these boundaries.
