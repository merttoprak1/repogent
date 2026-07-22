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

Docker is the isolation boundary. If Docker or the configured executor is not
ready, stop and report the remediation from the readiness check. Never switch
to local or host execution, and never offer host execution as a fallback.

## Tool map

| Tool | Use |
| --- | --- |
| `repogent_doctor` | Check repository, provider, executor, and command readiness. |
| `start_run` | Begin one bounded change request. |
| `get_run` | Reconcile current state after uncertainty or before a risky retry. |
| `approve_requirements` | Submit the explicit requirements gate decision. |
| `approve_plan` | Submit the explicit plan gate decision. |
| `approve_patch` | Apply the explicitly approved exact patch. |
| `cancel_run` | Stop a rejected or cancelled run safely. |
| `get_report` | Retrieve the terminal evidence report for the final chat response. |

## Run the three gates

1. Resolve exactly one repository root. If the path or intended repository is
   ambiguous, ask the user and do not call a run tool until they answer.
2. Call `repogent_doctor` for that root with the intended provider, model, and
   Docker executor. Stop on any required failure; do not start a run or bypass
   the failed check.
3. Call `start_run` with the same root and the user's bounded request.
4. For the **requirements gate**, render the pending artifact answer-first:
   objective, acceptance criteria, risk, assumptions, open questions, and its
   digest. Ask for an explicit approve, revise, or reject decision bound to that
   displayed digest. Never infer approval. Plain "okay" or "continue" is
   ambiguous and does not approve the requirements digest. Call
   `approve_requirements` only with the matching run ID, kind, digest, and the
   user's explicit decision.
5. If the user requests a requirements revision, reject the current
   requirements gate with their feedback, then start a revised run. If they
   reject or cancel the work, call `cancel_run` and report checkout state.
6. For the **plan gate**, render the proposed steps, files/scope, validation,
   risk, assumptions, and digest. Ask for a fresh explicit approve, revise, or
   reject decision bound to that digest. Call `approve_plan` only after that
   decision; requirements approval never carries forward.
7. For the **patch gate**, render the candidate decision evidence, exact diff,
   changed files and changed lines, required checks, skipped checks with reasons,
   risk, and digest. Require wording equivalent to
   **"I approve this patch; apply it"** for this exact displayed digest before
   calling `approve_patch`.
   Plain "okay", "continue", prior requirements/plan approval, or approval of a
   different digest does not count.

Every gate is a separate, explicit, digest-bound user decision. Never auto-approve
requirements, plan, or patch, even when the user asks to approve every stage
automatically.

## Handle mutation and uncertainty

`approve_patch` is destructive and non-idempotent. Never retry `approve_patch` blindly
after a timeout, disconnect, or uncertain delivery. First call `get_run`;
apply only if the returned current state still shows the same pending patch
digest and no patch was applied. Otherwise report the observed state without
replaying the mutation.

Use `get_run` after uncertain delivery of any decision. Use `cancel_run` when
the user rejects or cancels. Do not continue from a stale snapshot or substitute
a digest from an earlier gate.

## Report in chat

When the run reaches a terminal state, call `get_run` and `get_report`. Lead the
final chat response with the run status and whether the selected patch was
applied. Then state checkout state, applied or candidate paths, final validation
status and checks, skipped checks, recovery guidance when incomplete, and the
local evidence path. Keep secrets and unbounded raw logs out of chat.
