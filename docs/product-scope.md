# Repogent: product scope and restart plan

Status: proposal, 2026-11-12. Baseline: `feat/sentinel-lessons`, commit `e5dfcae`.
Work continues on `feat/verified-change-foundation`.

## Product promise

Repogent lets a Python developer review a small change with independent test results before applying the exact diff. The model proposes requirements, a plan, and candidate diffs. Repogent controls which files it reads, which commands run, and when the checkout changes. The user makes the final decision.

The first user already works in Codex Desktop and maintains a Python package with pytest. Codex Desktop is the first interface. The command line uses the same engine. The first use case is a bug fix with a regression test.

## Where work stopped

- The engine supports repository readiness, bounded Python analysis, patch proposals, disposable validation, exact-diff approval, and a report. The Codex plugin uses local Model Context Protocol (MCP) tools. Codex CLI, OpenAI, the xAI API, and a scripted provider supply proposals.
- The baseline commit adds the replay demo, degraded readiness, and a read-only GitHub Action above the local `origin/main`. Other workflow kinds in the [registry](../src/repogent/capabilities.py) are policy definitions. The MCP server starts only Verified Change.
- The test suite passed 701 tests, skipped 4, and reached 90.63% coverage. Ruff, mypy, and Bandit passed. The existing wheel passed 32 package and plugin checks, including local MCP integration. The replay added `GET /health` to a disposable copy. All four final checks passed, and the report showed `REDUCED ISOLATION`.
- The local Codex CLI passed readiness. Docker had no running daemon. This review did not run a live model change, test the actual Desktop plugin, or run isolated validation. The package and plugin report version 0.4.0 locally. Public distribution of that version remains unconfirmed.

## Release work

The new wheel completed the replay in a clean environment with the `demo` extra. A separate base install named the missing dependencies and stopped. Both installs used a local wheel cache.

| Finding | Evidence | Release criterion |
|---|---|---|
| The replay failed in a clean runtime environment. | The baseline wheel and only its runtime dependencies reproduced a preflight exit of 2. | Use the new `demo` install extra. The replay names missing dependencies. Prove the install path from the release source before publication. |
| A report could pass with no required checks. | At baseline, a report with one skipped optional check returned `passed=True`. See [domain.py](../src/repogent/domain.py#L257). | The branch requires a passing required check. Pytest must report at least one passing test. Verify the behavior in Docker before release. |
| Acceptance coverage can look stronger than the evidence. | The candidate score uses the proposal's claimed criteria and the overall check result. | The report labels the model's claims. The clamp example now checks that its regression test fails on the original code and passes after the patch. Repeat that proof on an external repository. |
| The draft Action has gaps. | A blocked doctor run with `fail-on-blocked: false` exited 2 and produced no step outputs. The script also expands inputs into shell source and writes its report inside the checkout. See [action.yml](../action.yml#L40). | Keep the Action outside the first milestone. Before release, test both flag values, pass inputs as data, and put the report outside the checkout. See [GitHub's guidance](https://docs.github.com/en/actions/reference/security/secure-use#good-practices-for-mitigating-script-injection-attacks). |

## Related projects

| Project | Pattern from the source | What Repogent can use |
|---|---|---|
| [Agentless](https://github.com/OpenAutoCoder/Agentless) | Localize the fault, generate candidates, and rank them with reproduction and regression tests. | Show the test that reproduces the bug. Compare candidates using observed results. |
| [Aider](https://aider.chat/docs/repomap.html) | A small repository map supplies relevant code context. | Show why Repogent selected specific files and symbols before it proposes a change. |
| [OpenHands](https://docs.openhands.dev/sdk/arch/security) | An action confirmation policy records decisions in the event history. | Show the pending action and its effect. Keep evidence of the user's decision. |
| [mini-SWE-agent](https://mini-swe-agent.com/latest/usage/swebench/) | A test harness runs benchmark tasks in defined environments. | Publish the base commit, test setup, result, and time for each example. |
| [reviewdog](https://github.com/reviewdog/reviewdog) | Filter tool findings to the changed lines of a diff. | Start the report with the exact diff, relevant checks, and the checkout state. |

This branch takes two concrete ideas. [Agentless runs reproduction tests against the original code and candidate patches](https://github.com/OpenAutoCoder/Agentless/blob/main/agentless/test/run_reproduction_tests.py). The clamp example uses that pattern. [Aider's repository map ranks code symbols within a size budget](https://github.com/Aider-AI/aider/blob/main/aider/repomap.py). Repogent keeps its Python graph and now shows why it ranked each top location.

## First finished milestone

1. Prove the clean install and replay. Run the package from a fresh environment. Check that the Codex plugin starts the intended CLI and opens the same report.
2. Require test evidence before application. Check that missing tests, failed tests, a rejected diff, and a stale approval leave the checkout unchanged. Keep local runs labeled `REDUCED ISOLATION`.
3. Check the Desktop plugin interface. The live MCP/Docker case below passed. Confirm the plugin loads the chosen package and presents its pending decisions.
4. Repeat on a second Python package. Record the supported project shape, validator dependencies, and any limits. Align the README, package, and plugin versions with the chosen distribution source.

The first milestone covers local Python projects with an existing pytest suite and dependencies that the reviewed validator can run. Keep the scope to Readiness, Verified Change, and the report. Defer new providers, hosted execution, automatic pull requests, new languages, and an evidence web interface.

After the milestone, test a second use case: validate an existing Git diff without calling a model or changing the checkout. This would test whether developers want the evidence step on its own.

## Live validation on 2026-11-13

The external case used [more-itertools 10.8.0](https://github.com/more-itertools/more-itertools/tree/8c1a6ef241b51ff055e89219f050ccf4f15f37f6). Its base commit was 8c1a6ef241b51ff055e89219f050ccf4f15f37f6. The regression test checks negative counts, a False result, and preservation of the iterator. Before the patch, Docker reported one failure, 695 passing tests, and one skipped test.

The first live attempt omitted the target function from the model context. The source selector now gives explicit function names priority and limits each snippet to 4,000 characters. The next attempt produced a valid patch but stopped on equal candidate evidence. The proposal schema now asks the model to copy the approved criteria unchanged.

Run run-58c20651ae18 completed_with_findings from the installed wheel with Codex CLI and Docker. Its selected patch added two lines to exactly_n. Docker reported 696 passing tests and one skipped test after application. Final validation passed. The test file stayed unchanged during the run.

The optional Ruff, mypy, and Bandit checks reported findings. The original source also failed those checks. Model QA reported limited access for independent source review.

The test client supplied the digest-bound decisions for this disposable checkout. A separate Codex CLI session called the installed MCP server's readiness tool. The Desktop plugin interface remains untested.

The Repogent suite passed 708 tests, skipped three, and reached 90.14% coverage. The new wheel passed 32 package and plugin checks. Ruff, mypy, and Bandit passed on Repogent.

Manual approval exposed a timeout after an overnight operator break. Human
approval waits now pause the execution budget. Executor inspection and
preparation retain their deadlines. Regression tests simulate a nine-hour wait.

The manual run run-d2276149562c expired while it waited for executor selection overnight. The local test harness recovered the recorded proposals in run-75b4dec4d33d. It checked current source hashes and matched each artifact digest before reusing the user's explicit decisions. The saved proposals retain their original usage metadata. Docker ran the checks again. The operator approved the exact patch. Repogent applied it to more_itertools/more.py. Final Docker validation passed, and the run finished as completed with ISOLATED VERIFIED. Optional Ruff, mypy, and Bandit checks failed.

After the wait-budget fix, the full suite passed 712 tests, skipped three, and reached 90.77% coverage. Package and plugin checks passed 32 tests.

## Optional checks on 2026-11-13

The follow-up compared the original and patched more-itertools sources with the same regression test. Ruff previously exited with code 2 because its cache required a writable checkout. Docker now sets `RUFF_NO_CACHE=true` and stores the mypy cache under `/tmp/repogent-mypy-cache`. The fixed commands, network restriction, and read-only checkout remain in effect.

Ruff passed on both sources after the fix. The original source still failed the regression test. The patched source passed 696 tests and skipped one test.

Mypy reported the same 297 errors in both cases. All errors came from documentation or test files. Bandit reported the same 42 findings, with 41 low-severity findings and one medium-severity finding. The medium finding refers to a test that deserializes its own pickle output. The comparison retained these findings and found no new findings from the patch.

The evidence includes separate check outputs and a comparison of diagnostic messages and source context. This comparison ignores shifted line numbers. The original approval report remains unchanged.

The full Repogent suite passed 718 tests, skipped three, and reached 90.77% coverage. Package and MCP checks passed 32 tests. The local CLI uses the updated wheel.
