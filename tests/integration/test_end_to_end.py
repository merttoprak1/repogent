import json
import shutil
from pathlib import Path

from repogent.agents import RoleSet
from repogent.approvals import FakeApprover
from repogent.artifacts import ArtifactStore
from repogent.domain import Budget, CheckStatus, Decision, RunManifest, RunStatus
from repogent.execution import LocalExecutor, ValidationPolicy
from repogent.patching import PatchApplier, PatchPolicy
from repogent.providers import ScriptedProvider
from repogent.repository import LexicalRetriever, RepositoryInspector
from repogent.validation import ValidationPipeline
from repogent.workflow import Workflow


def test_scripted_fastapi_change_reaches_verified_report(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(Path("examples/fastapi_demo"), target)
    outputs = json.loads(Path("examples/scripted_run.json").read_text())
    policy = ValidationPolicy()
    executor = LocalExecutor(
        allowed={command.name: command.argv for command in policy.commands(target)}
    )
    store = ArtifactStore.create(tmp_path / "runs", target, "add health", run_id="demo-run")
    workflow = Workflow(
        root=target,
        request='Add a health endpoint that returns {"status": "ok"}',
        manifest=RunManifest(run_id="demo-run", request="add health"),
        roles=RoleSet.from_provider(ScriptedProvider(outputs)),
        approver=FakeApprover([Decision.APPROVED] * 3),
        patch_policy=PatchPolicy(),
        patch_applier=PatchApplier(),
        validator=ValidationPipeline(executor, policy),
        artifacts=store,
        inspector=RepositoryInspector(),
        retriever=LexicalRetriever(),
        budget=Budget(),
    )
    result = workflow.run()
    assert result.status is RunStatus.COMPLETED
    assert '@app.get("/health")' in (target / "app.py").read_text()
    assert (store.root / "report.md").exists()
    manifest = json.loads((store.root / "run.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["execution_mode"] == "local"
    assert manifest["verification_status"] == "passed"
    assert manifest["evaluated_target"]["kind"] == "patch"
    assert manifest["evaluated_target"]["digest"]
    report = (store.root / "report.md").read_text()
    assert "Verification: REDUCED ISOLATION" in report
    assert "Execution mode: local" in report
    assert f"Evaluated target: patch:{manifest['evaluated_target']['digest']}" in report


def test_scripted_clamp_change_reaches_verified_report(tmp_path: Path) -> None:
    target = tmp_path / "target"
    fixture = Path("tests/fixtures/python_library")
    shutil.copytree(fixture, target)
    outputs = json.loads(Path("examples/scripted_clamp.json").read_text())
    policy = ValidationPolicy()
    executor = LocalExecutor(
        allowed={command.name: command.argv for command in policy.commands(target)}
    )
    store = ArtifactStore.create(
        tmp_path / "runs", target, "reject inverted clamp bounds", run_id="clamp-run"
    )
    workflow = Workflow(
        root=target,
        request="Reject inverted clamp bounds",
        manifest=RunManifest(run_id="clamp-run", request="reject inverted clamp bounds"),
        roles=RoleSet.from_provider(ScriptedProvider(outputs)),
        approver=FakeApprover([Decision.APPROVED] * 3),
        patch_policy=PatchPolicy(),
        patch_applier=PatchApplier(),
        validator=ValidationPipeline(executor, policy),
        artifacts=store,
        inspector=RepositoryInspector(),
        retriever=LexicalRetriever(),
        budget=Budget(),
    )
    result = workflow.run()
    assert result.status is RunStatus.COMPLETED
    assert "raise ValueError" in (target / "src/example_math/__init__.py").read_text()
    assert (store.root / "report.md").exists()
    manifest = json.loads((store.root / "run.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["execution_mode"] == "local"
    assert manifest["verification_status"] == "passed"
    assert manifest["evaluated_target"]["kind"] == "patch"
    assert manifest["evaluated_target"]["digest"]
    report = (store.root / "report.md").read_text()
    assert "Verification: REDUCED ISOLATION" in report
    assert "Execution mode: local" in report
    assert f"Evaluated target: patch:{manifest['evaluated_target']['digest']}" in report

    baseline = tmp_path / "baseline"
    shutil.copytree(fixture, baseline)
    shutil.copyfile(target / "tests/test_math.py", baseline / "tests/test_math.py")
    reproduction = executor.run(policy.commands(baseline)[0], baseline)
    assert reproduction.status is CheckStatus.FAILED
    assert reproduction.exit_code == 1
    assert "test_rejects_inverted_bounds" in reproduction.stdout
    assert "DID NOT RAISE" in reproduction.stdout
