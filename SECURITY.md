# Security policy

## Supported versions

Repogent is pre-1.0. Only the latest released version receives security fixes.

| Version | Supported |
| ------- | --------- |
| 0.3.1   | Yes       |
| < 0.3.1 | No        |

## Reporting a vulnerability

Report suspected vulnerabilities privately through
[GitHub Security Advisories](https://github.com/merttoprak1/repogent/security/advisories/new).
Please do not open a public issue for an unfixed vulnerability.

Include the affected version, your environment, the impact you observed, and
reproduction steps. A proof-of-concept repository or transcript is helpful.

Expect an initial acknowledgement within seven days. Repogent is maintained by a
single author as an unfunded open-source project, so remediation timelines are
best effort and depend on severity.

## Scope

Repogent's threat model is documented in [docs/security.md](docs/security.md).
Read it before reporting: several properties that look like weaknesses are
documented, intentional boundaries.

In scope:

- bypassing an approval gate, or advancing a run without the digest-bound
  approval of the exact displayed artifact;
- applying a patch to the real checkout without a matching approved patch
  digest, or escaping the Git-bounded repository scope;
- escaping the validation executor boundary, or obtaining local execution
  without the explicit reduced-isolation consent;
- executing a capability that the session's `WorkflowKind` policy denies;
- leaking provider credentials or unsanitized secrets into evidence artifacts,
  logs, or MCP responses.

Out of scope:

- risks inherent to running untrusted repository code inside an executor the
  operator explicitly selected — Repogent reduces authority, it does not make
  untrusted execution safe;
- attacks that require the operator to approve a patch whose exact diff was
  displayed to them;
- vulnerabilities in Docker, Codex, model providers, or other dependencies,
  unless Repogent's use of them is what creates the exposure;
- prompt-injection content that changes what a model *proposes*, without
  crossing a deterministic gate — proposals are untrusted by design.
