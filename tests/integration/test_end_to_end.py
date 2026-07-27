import json
import shutil
from pathlib import Path

from repogent.agents import RoleSet
from repogent.approvals import FakeApprover
from repogent.artifacts import ArtifactStore
from repogent.domain import Budget, Decision, RunManifest, RunStatus
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
        root=target, request='Add a health endpoint that returns {"status": "ok"}',
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
    assert manifest["preview_digest"]
    report = (store.root / "report.md").read_text()
    assert "Verification: REDUCED ISOLATION" in report
    assert "Execution mode: local" in report
    assert f"Preview digest: {manifest['preview_digest']}" in report
