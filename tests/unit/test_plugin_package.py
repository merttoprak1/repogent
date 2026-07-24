"""Regression coverage for the repository-local Codex plugin package."""

import json
import tomllib
from pathlib import Path

PLUGIN_ROOT = Path("plugins/repogent")
SKILL_PATH = PLUGIN_ROOT / "skills/repogent/SKILL.md"
EVALS_PATH = Path("tests/plugin/evals.json")


def test_plugin_manifest_and_mcp_command() -> None:
    root = PLUGIN_ROOT
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text())
    mcp = json.loads((root / ".mcp.json").read_text())

    assert manifest["name"] == "repogent"
    assert manifest["version"] == "0.1.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["interface"]["category"] == "Developer Tools"
    assert mcp["mcpServers"]["repogent"] == {
        "command": "repogent",
        "args": ["mcp", "--stdio"],
    }


def test_repository_marketplace_uses_local_install_policy() -> None:
    marketplace_path = Path(".agents/plugins/marketplace.json")
    marketplace = json.loads(marketplace_path.read_text())
    plugin = marketplace["plugins"][0]

    assert marketplace["name"] == "repogent-local"
    assert marketplace["interface"]["displayName"] == "Repogent"
    assert plugin["name"] == "repogent"
    assert plugin["source"] == {
        "source": "local",
        "path": "./plugins/repogent",
    }
    assert plugin["source"]["path"].startswith("./")
    assert (marketplace_path.parent.parent / plugin["source"]["path"]).resolve().is_relative_to(
        Path.cwd().resolve()
    )
    assert plugin["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert plugin["category"] == "Developer Tools"


def test_repogent_skill_declares_triggers_tools_and_three_gates() -> None:
    skill = SKILL_PATH.read_text()

    assert skill.startswith("---\nname: repogent\n")
    for trigger in (
        "@Repogent",
        "$repogent",
        "safe",
        "verified",
        "evidence-backed",
        "independently validated",
        "approval-before-apply",
    ):
        assert trigger in skill

    for tool_name in (
        "repogent_doctor",
        "start_run",
        "get_run",
        "approve_requirements",
        "approve_plan",
        "select_executor",
        "approve_patch",
        "cancel_run",
        "get_report",
    ):
        assert f"`{tool_name}`" in skill

    for gate_name in ("requirements", "plan", "patch"):
        assert f"{gate_name} gate" in skill.lower()
    assert "digest" in skill.lower()


def test_repogent_skill_closes_baseline_safety_loopholes() -> None:
    skill = SKILL_PATH.read_text().lower()

    required_safety_language = (
        "never auto-approve",
        "ordinary codex edits",
        "host execution",
        "docker",
        '"okay"',
        '"continue"',
        '"i approve this patch; apply it"',
        "non-idempotent",
        "never retry `approve_patch` blindly",
        "subagent delegation",
    )
    for requirement in required_safety_language:
        assert requirement in skill

    assert "`checks`: `{name, status, required}`" in skill
    assert "`skipped_checks`: `{name, reason}`" in skill


def test_repogent_skill_teaches_digest_bound_progressive_executor_flow() -> None:
    raw = SKILL_PATH.read_text()
    skill = raw.lower()

    # The plugin must onboard without Docker by deferring the executor choice.
    assert 'executor="deferred"' in raw

    # The three trust labels must appear verbatim (exact case) so the skill can
    # render and never overstate them.
    for label in ("UNVALIDATED", "REDUCED ISOLATION", "ISOLATED VERIFIED"):
        assert label in raw

    # Explicit, current-digest local-risk consent wording is mandatory and no
    # weaker phrasing may substitute.
    assert (
        "i accept reduced isolation; validate this displayed patch locally" in skill
    )

    # No silent fallback and no apply before verification.
    assert "silently fall back" in skill
    assert "never apply an unvalidated patch" in skill

    # The digest-bound executor decision is not a fourth approval and is never
    # triggered by ambiguous continuation.
    assert "select_executor" in raw
    assert "not a fourth" in skill or "separate" in skill


def test_repogent_evals_have_seven_positive_and_five_negative_cases() -> None:
    evals = json.loads(EVALS_PATH.read_text())

    assert set(evals) == {"positive", "negative"}
    assert len(evals["positive"]) == 7
    assert len(evals["negative"]) == 5

    cases = [*evals["positive"], *evals["negative"]]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert all(set(case) == {"id", "prompt", "expected"} for case in cases)
    assert all(case["expected"] for case in cases)

    # The progressive-executor contract must be exercised by the fixtures: the
    # dockerless preview, both explicit executor selections, the two trust
    # labels, and the negatives that forbid silent fallback and stale digests.
    positive_ids = {case["id"] for case in evals["positive"]}
    assert {
        "dockerless-preview",
        "explicit-docker",
        "explicit-local",
        "stale-local-consent",
        "executor-switch",
        "validated-patch",
        "preview-cancel",
    } == positive_ids
    negative_ids = {case["id"] for case in evals["negative"]}
    assert {
        "silent-local",
        "ambiguous-local",
        "apply-unvalidated",
        "fake-isolation",
        "reuse-executor-digest",
    } == negative_ids


def test_readme_installs_bare_runtime_command_before_plugin_marketplace() -> None:
    readme = Path("README.md").read_text()
    project = tomllib.loads(Path("pyproject.toml").read_text())
    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())
    runtime_install = "pipx install 'git+https://github.com/merttoprak1/repogent.git'"
    marketplace_install = "codex plugin marketplace add merttoprak1/repogent"

    assert project["project"]["scripts"]["repogent"] == "repogent.cli:app"
    assert mcp["mcpServers"]["repogent"]["command"] == "repogent"
    assert runtime_install in readme
    assert "pipx ensurepath" in readme
    assert "command -v repogent" in readme
    assert "Codex Desktop" in readme
    assert readme.index(runtime_install) < readme.index(marketplace_install)
    assert "python -m pip install -e '.[dev]'" in readme


def test_ci_wheel_inspection_requires_mcp_server_module() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text()

    assert "assert 'repogent/mcp_server.py' in names" in workflow
