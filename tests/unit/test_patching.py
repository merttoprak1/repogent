from pathlib import Path

import pytest

from repogent.domain import PatchProposal
from repogent.patching import PatchApplier, PatchPolicy, PatchPolicyError

GOOD_DIFF = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""


@pytest.mark.parametrize(
    ("diff", "message"),
    [
        ("--- a/app.py\n+++ b/../../escape.py\n@@ -1 +1 @@\n-x\n+y\n", "unsafe path"),
        ("--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-x\n+y\n", "protected path"),
        ("GIT binary patch\n--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+x\n", "binary"),
        ("--- a/.git/config\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n", "protected path"),
        ("--- a/app.py\n+++ b/other.py\n@@ -1 +1 @@\n-x\n+y\n", "renames"),
        ("hello\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n", "malformed"),
        ('--- "x/app.py"\n+++ "x/app.py"\n@@ -1 +1 @@\n-x\n+y\n', "unsafe path"),
        ("--- x/app.py\n+++ x/app.py\n@@ -1 +1 @@\n-x\n+y\n", "unsafe path"),
    ],
)
def test_policy_rejects_unsafe_diffs(tmp_path: Path, diff: str, message: str) -> None:
    with pytest.raises(PatchPolicyError, match=message):
        PatchPolicy().validate(tmp_path, PatchProposal(summary="unsafe", diff=diff))


def test_policy_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    diff = "--- /dev/null\n+++ b/linked/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
    with pytest.raises(PatchPolicyError, match="outside repository"):
        PatchPolicy().validate(tmp_path, PatchProposal(summary="escape", diff=diff))


def test_policy_rejects_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("value = 1\n")
    (tmp_path / "app.py").symlink_to(outside)
    with pytest.raises(PatchPolicyError, match="symlink"):
        PatchPolicy().validate(tmp_path, PatchProposal(summary="escape", diff=GOOD_DIFF))


def test_applier_changes_file_after_validation(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n")
    patch = PatchPolicy().validate(tmp_path, PatchProposal(summary="change", diff=GOOD_DIFF))
    PatchApplier().apply(tmp_path, patch)
    assert target.read_text() == "value = 2\n"


def test_applier_creates_file_in_missing_directory_after_validation(tmp_path: Path) -> None:
    diff = "--- /dev/null\n+++ b/generated/app.py\n@@ -0,0 +1 @@\n+value = 1\n"
    patch = PatchPolicy().validate(tmp_path, PatchProposal(summary="add", diff=diff))
    PatchApplier().apply(tmp_path, patch)
    assert (tmp_path / "generated" / "app.py").read_text() == "value = 1\n"


def test_applier_restores_snapshot_when_apply_fails(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("different = 1\n")
    patch = PatchPolicy().validate(tmp_path, PatchProposal(summary="change", diff=GOOD_DIFF))
    with pytest.raises(RuntimeError, match="git apply"):
        PatchApplier().apply(tmp_path, patch)
    assert target.read_text() == "different = 1\n"


def test_applier_restores_all_touched_files_after_post_apply_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first = 1\n")
    second.write_text("second = 1\n")
    diff = """--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-first = 1
+first = 2
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-second = 1
+second = 2
"""
    patch = PatchPolicy().validate(tmp_path, PatchProposal(summary="change", diff=diff))
    original_apply = PatchApplier._git_apply

    def fail_after_apply(root: Path, content: str, *, check: bool) -> None:
        original_apply(root, content, check=check)
        if not check:
            raise RuntimeError("git apply failed after modifying files")

    monkeypatch.setattr(PatchApplier, "_git_apply", staticmethod(fail_after_apply))
    with pytest.raises(RuntimeError, match="after modifying"):
        PatchApplier().apply(tmp_path, patch)
    assert first.read_text() == "first = 1\n"
    assert second.read_text() == "second = 1\n"


def test_applier_removes_new_parent_directories_after_post_apply_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diff = """--- /dev/null
+++ b/generated/nested/app.py
@@ -0,0 +1 @@
+value = 1
"""
    patch = PatchPolicy().validate(tmp_path, PatchProposal(summary="add", diff=diff))
    original_apply = PatchApplier._git_apply

    def fail_after_apply(root: Path, content: str, *, check: bool) -> None:
        original_apply(root, content, check=check)
        if not check:
            raise RuntimeError("git apply failed after creating directories")

    monkeypatch.setattr(PatchApplier, "_git_apply", staticmethod(fail_after_apply))
    with pytest.raises(RuntimeError, match="after creating directories"):
        PatchApplier().apply(tmp_path, patch)
    assert not (tmp_path / "generated").exists()


def test_applier_preserves_existing_parent_directories_after_post_apply_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing_parent = tmp_path / "generated"
    existing_parent.mkdir()
    diff = """--- /dev/null
+++ b/generated/nested/app.py
@@ -0,0 +1 @@
+value = 1
"""
    patch = PatchPolicy().validate(tmp_path, PatchProposal(summary="add", diff=diff))
    original_apply = PatchApplier._git_apply

    def fail_after_apply(root: Path, content: str, *, check: bool) -> None:
        original_apply(root, content, check=check)
        if not check:
            raise RuntimeError("git apply failed after creating directories")

    monkeypatch.setattr(PatchApplier, "_git_apply", staticmethod(fail_after_apply))
    with pytest.raises(RuntimeError, match="after creating directories"):
        PatchApplier().apply(tmp_path, patch)
    assert existing_parent.is_dir()
    assert not (existing_parent / "nested").exists()
