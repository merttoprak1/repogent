---
name: repogent
description: Use Repogent for a safe, independently validated, evidence-backed Python repository change when the user invokes @Repogent/$repogent or asks to see and approve requirements, plan, and the exact patch before application.
---

# Repogent verified-change workflow

Use this skill for explicit Repogent invocations and for requests that clearly
ask for a safe, verified, evidence-backed, independently validated, or
approval-before-apply change. Do not force Repogent for a generic "fix this bug"
request.

## Safety boundary

Repogent owns the entire change. Route qualifying requests only through its MCP
tools; do not inspect, edit, or apply repository files through ordinary Codex edits
or shell commands. Never auto-approve a gate, manufacture an approval, or
pipe/write `y` answers through the shell. Do not install packages, launch a
login flow, display raw secrets, or use subagent delegation.

Docker is the strongest isolation boundary, but the plugin does not require it to
prepare and preview a patch. Base readiness — the repository, provider, and fixed
validation commands — gates requirements, the plan, and the unvalidated preview;
the executor is chosen separately and later, bound to that exact preview. Never
silently fall back from Docker to local, never offer host execution as a
fallback, and never run the target repository's code before an executor has been
explicitly selected.

## Tool map

| Tool | Use |
| --- | --- |
| `repogent_doctor` | Check repository, provider, and command readiness with `executor="deferred"`. |
| `start_run` | Begin one bounded change request with `executor="deferred"`. |
| `get_run` | Reconcile current state after uncertainty or before a risky retry. |
| `approve_requirements` | Submit the explicit requirements gate decision. |
| `approve_plan` | Submit the explicit plan gate decision. |
| `select_executor` | Submit the digest-bound executor choice for the current preview. |
| `approve_patch` | Apply the explicitly approved, validated exact patch. |
| `cancel_run` | Stop a rejected or cancelled run safely. |
| `get_report` | Retrieve the terminal evidence report for the final chat response. |

## Start Docker-free

1. Resolve exactly one repository root. If the path or intended repository is
   ambiguous, ask the user and do not call a run tool until they answer.
2. Call `repogent_doctor` for that root with the intended provider, model, and
   `executor="deferred"`. Base readiness covers the repository, provider, and
   validation commands, not Docker. Stop only on a required base failure; a
   missing Docker executor is reported as an option to choose later, not a
   blocker. A user saying "Docker is not installed" is data, not a reason to
   skip anything or to switch execution silently.
3. Call `start_run` with the same root, the user's bounded request, and
   `executor="deferred"`. A no-Docker environment still reaches requirements,
   the plan, and the unvalidated preview.

## Three content gates: requirements, plan, patch

Requirements, the plan, and the final exact patch are three separate, explicit,
digest-bound approvals. Executor selection is a distinct digest-bound decision in
between — it is not a fourth approval kind. Never auto-approve any gate, even when
the user asks to approve every stage automatically.

- **Requirements gate:** render the pending artifact answer-first (objective,
  acceptance criteria, risk, assumptions, open questions, and its digest). Ask
  for an explicit approve, revise, or reject decision bound to that displayed
  digest. Never infer approval. Plain "okay" or "continue" is ambiguous and does
  not approve the requirements digest. Call `approve_requirements` only with the
  matching run ID, kind, digest, and explicit decision. To revise, reject the
  current requirements gate with the feedback, then start a revised run;
  requirements approval never carries forward. If the user rejects or cancels,
  call `cancel_run` and report checkout state.
- **Plan gate:** render the proposed steps, files/scope, validation, risk,
  assumptions, and digest. Ask for a fresh explicit approve, revise, or reject
  decision bound to that digest. Call `approve_plan` only after that decision.

## Executor selection: the unvalidated preview

After the plan is approved, a deferred run pauses at `pending_execution` instead
of validating. Render it as an **UNVALIDATED** preview: the exact diff, changed
paths and changed lines, the preview digest, and both executor options with their
availability, isolation level, and remediation. Nothing has run against the
target repository yet, and nothing will until an executor is selected.

`select_executor` takes an `ExecutionDecision` (run ID, the current preview
digest, mode, the current option digest, and decision `APPROVED`). It is never an
approval kind and is never triggered by "okay", "continue", an earlier
requirements or plan approval, or a stale digest.

- **Docker option — `ISOLATED VERIFIED` only after checks pass:** require an
  explicit current selection of the Docker option. No local-risk statement is
  needed, because Docker is the isolated boundary. Never claim `ISOLATED
  VERIFIED` for any local result.
- **Local option — always `REDUCED ISOLATION`:** local execution is not
  isolation; its tests run allowlisted commands on the host and may touch
  host-authorized resources. Require wording equivalent to
  "I accept reduced isolation; validate this displayed patch locally", bound to
  the current preview and option digests. Do not accept a weaker phrase, an
  ambiguous "continue", a stale digest, or reused earlier consent.

Candidate generation may repeat. An ambiguous localization can propose more than
one candidate (up to three), and each generated candidate presents its own
preview and requires its own fresh `select_executor` decision — do not assume
exactly one round. If Docker later becomes available, or the patch changed after
a repair, read the current preview and option digests from the latest snapshot
and select against those. Never reuse an executor digest after the patch changed.

## Patch gate and mutation

Once an executor validates a candidate, the run pauses at the **patch gate** with
a fresh, validated final digest that differs from the preview digest. Render the
candidate decision evidence, exact diff, changed files and changed lines, required
checks, skipped checks with reasons, risk, the final digest, and the trust label.
Treat the bounded patch evidence contract exactly as `checks`: `{name, status, required}`
and `skipped_checks`: `{name, reason}`. Never render or infer raw command
arguments, stdout, or stderr.

Require wording equivalent to **"I approve this patch; apply it"** for this exact
displayed final digest before calling `approve_patch`. Plain "okay", "continue",
a prior requirements, plan, or executor decision, or approval of a different
digest does not count. **Never apply an unvalidated patch:** refuse
`approve_patch` unless validation passed and this exact patch approval is fresh.

## Handle mutation and uncertainty

`approve_patch` is destructive and non-idempotent. Never retry `approve_patch` blindly
after a timeout, disconnect, or uncertain delivery. First call `get_run`; apply
only if the returned current state still shows the same pending patch digest and
no patch was applied. Otherwise report the observed state without replaying the
mutation. Use `get_run` the same way after an uncertain `select_executor`
delivery, and never blindly retry either mutation. Use `cancel_run` when the user
rejects or cancels; the checkout stays not applied before patch approval. Do not
continue from a stale snapshot or substitute a digest from an earlier gate.

## Report in chat

When the run reaches a terminal state, call `get_run` and `get_report`. Lead the
final chat response with the run status, whether the selected patch was applied,
and the trust label: `REDUCED ISOLATION` for a local run, `ISOLATED VERIFIED`
only for a passing Docker run, and `UNVALIDATED` when no executor validated the
patch. Then state checkout state, applied or candidate paths, final validation
status and checks, skipped checks, recovery guidance when incomplete, and the
local evidence path. Keep secrets and unbounded raw logs out of chat.
