# Security model

Repository content and tests are untrusted. Repogent reduces authority; it does not make untrusted execution risk-free.

## Controls

- The Codex plugin is a local stdio adapter, not a second policy engine. It
  delegates readiness, workflow state, patch policy, validation, and evidence
  to Repogent's existing deterministic services. The local Codex process,
  installed `repogent` executable, target checkout, and evidence directory are
  inside the operator's local trust boundary; no remote Repogent control plane
  is introduced.
- MCP requirements, plan, and patch approvals are independent and
  digest-bound. A stale digest or wrong gate kind cannot advance a run, and the
  patch digest binds directly to the exact displayed diff. The patch approval
  tool is non-idempotent and must not be retried blindly after uncertain
  delivery.
- A canonical repository root has at most one active MCP run. Aliases resolve
  to the same lock, and the reservation is released only when terminal state is
  safely publishable. This prevents two chat requests from racing mutations in
  one checkout.
- MCP disconnect invokes bounded, cooperative session shutdown. Pending gates
  are closed and active runs are asked to cancel, while durable `checkout_state`
  remains authoritative: `not_applied` is reported only when non-mutation is
  known, `applied` when the approved patch is durable, and `recovery_unknown`
  when manual inspection is required.
- Repository instructions are delimited as untrusted data in every role prompt.
- Traversal does not follow repository symlinks, excludes common credential paths, and fails closed on fixed file-count, aggregate-byte, directory-entry, depth, and elapsed-time limits. Patch paths must remain below the resolved root.
- Binary, protected-path, malformed, oversized, absolute, and traversal patches are rejected.
- Models cannot choose commands. Validation uses fixed argument arrays from an allowlist without a shell.
- Docker is the default, disables network access, mounts the checkout read-only for validation, uses a read-only container filesystem, and applies CPU, memory, PID, and time limits. Preflight probes each module or executable inside the fixed image without mounting the repository and caches readiness per image and tool.
- Preflight runs before provider construction. It fingerprints repository/configuration state, checks every fixed validation command, and stops without model spend when a required command is unavailable; optional tools produce warnings. Bounded pytest discovery fails closed on traversal limits, malformed or oversized configuration, races, and filesystem access uncertainty. Recognized root configuration is read with fd-relative metadata and no-follow checks where available; only a stable, bounded regular file is accepted, so symlinks, FIFOs, devices, sockets, and other special files are not read.
- The local executor is chosen explicitly through the digest-bound execution gate, has a minimal environment, and is weaker than Docker; Docker never silently falls back to local execution, and no target-repository code runs before an executor is selected.
- A local executor's command allowlist is not isolation. The allowlist only bounds which commands run; it does not sandbox them. Local tests execute on the host and may read, write, or reach any resource the host user is already authorized to access — the network, the filesystem outside the checkout, and ambient credentials. Local runs are therefore always labelled `REDUCED ISOLATION`; only a Docker run whose required checks pass may be labelled `ISOLATED VERIFIED`, and a local result is never presented as isolated.
- Host credentials are not forwarded. Provider context has a deterministic global serialized-size ceiling: inventory bodies are excluded, localization is top-ranked and complete-line bounded, and a structured allocator progressively compacts low-priority bulk data while retaining critical identifiers, statuses, reasons, and explicit truncation counts. Configured and common secret forms are recursively redacted at the live-provider boundary and structurally sanitized before JSON persistence.
- The optional Codex CLI provider runs `codex exec` from a provider-owned temporary directory with ephemeral, read-only-sandbox, and ignore-user-config/rules flags. It keeps its prompt, schema, output, and diagnostics in owner-only temporary files; refuses a Codex executable or temporary directory under the target repository; filters target-root paths from the child environment and provider payload; and records readiness and call evidence. This complements Codex's own controls, but does not provide strict OS-enforced read isolation: a local subprocess still has whatever host access the operating system and the Codex runtime permit.
- One monotonic workflow deadline caps inspection, live-provider requests, and deterministic validation commands; approval waits are rechecked before any mutation.
- Before real-checkout mutation, a `recovery_unknown` write-ahead intent and touched paths must be durable. Patch application then snapshots every touched path and attempts restoration after ordinary failures, `KeyboardInterrupt`, or `SystemExit`. Until `applied` is durably recorded, any failure triggers a detect-only baseline comparison: only a proven match becomes `not_applied`; drift or uncertainty retains the write-ahead state and surfaces every path for manual inspection.
- Failed or interrupted post-apply work preserves the approved checkout state and reports the applied paths, final-validation state, and exact manual next action; disposable candidate restoration is never presented as real-checkout rollback.
- Candidate patches run in disposable copied workspaces and must restore their baseline before evidence selection. A real-checkout fingerprint is rechecked before application; the approved patch applies once, and final validation runs in another disposable workspace.
- Event logs are monotonic and JSON evidence is sanitized and versioned. One outer terminalization boundary retains partial evidence for provider, budget, interruption, recovery, validation, event, or persistence failures. Generated typed output and usage are written before budget enforcement and marked when they were not consumed.

## Residual risks

Untrusted tests can consume resources, exploit container-runtime or kernel vulnerabilities, and inspect any data deliberately mounted into the container. Static Python analysis can be incomplete for dynamic imports, reflection, generated code, runtime framework wiring, and non-Python sources; ambiguity is surfaced for review instead of treated as confidence. The fixed validator image supports the MVP demo dependency set, not arbitrary project dependencies. Operators should run Repogent on disposable checkouts, keep Docker and the host patched, review every patch, and never mount credentials. A Codex CLI login is user-initiated; treat the local CLI and its account/session as part of the operator's trusted environment, not as an isolation boundary. Benchmarking, headless CI, and GitHub adapters are deferred; their future integrations must preserve these boundaries.
