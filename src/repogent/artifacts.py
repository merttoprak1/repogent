from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel

from repogent.sanitization import redact_text, sanitize_data


class ArtifactStoreError(ValueError):
    pass


SAFE_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SAFE_SUFFIX = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9._-]*")


def redact(text: str, explicit_secrets: list[str]) -> str:
    return redact_text(text, explicit_secrets)


class ArtifactStore:
    def __init__(self, root: Path, secrets: list[str] | None = None) -> None:
        self.root = root
        self.secrets = secrets or []

    @classmethod
    def create(
        cls,
        base_dir: Path,
        target_root: Path,
        request: str,
        *,
        run_id: str | None = None,
        secrets: list[str] | None = None,
    ) -> ArtifactStore:
        del request
        target = target_root.resolve(strict=True)
        base = base_dir.resolve()
        if base == target or target in base.parents:
            raise ArtifactStoreError("evidence directory must be outside target repository")
        if run_id is not None and not _is_plain_component(run_id, SAFE_STEM):
            raise ArtifactStoreError("run ID must be a plain path component")
        identifier = run_id or f"run-{uuid.uuid4().hex[:12]}"
        root = base / identifier
        root.mkdir(parents=True, exist_ok=False)
        return cls(root, secrets)

    def write_model(self, name: str, model: BaseModel) -> Path:
        return self.write_text(
            name,
            json.dumps(sanitize_data(model.model_dump(mode="json"), self.secrets), indent=2),
            suffix=".json",
        )

    def write_text(self, name: str, text: str, *, suffix: str = ".txt") -> Path:
        if not _is_plain_component(name, SAFE_STEM):
            raise ArtifactStoreError("artifact name must be a plain safe stem")
        if not _is_plain_component(suffix, SAFE_SUFFIX):
            raise ArtifactStoreError("suffix must be a plain suffix beginning with one dot")
        index = len(list(self.root.glob(f"{name}-*{suffix}"))) + 1
        path = self._path_in_root(f"{name}-{index:03d}{suffix}")
        self._atomic_write(path, self._sanitize_text(text))
        return path

    def update_manifest(self, manifest: BaseModel) -> Path:
        path = self._path_in_root("run.json")
        content = json.dumps(
            sanitize_data(manifest.model_dump(mode="json"), self.secrets), indent=2
        )
        self._atomic_write(path, content)
        return path

    def write_final(self, filename: str, content: str) -> Path:
        if Path(filename).name != filename or not filename.endswith((".md", ".json")):
            raise ArtifactStoreError("final artifact must be a plain Markdown or JSON filename")
        path = self._path_in_root(filename)
        self._atomic_write(path, self._sanitize_text(content))
        return path

    def _sanitize_text(self, content: str) -> str:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return redact(content, self.secrets)
        return json.dumps(sanitize_data(payload, self.secrets), indent=2)

    def _path_in_root(self, filename: str) -> Path:
        path = self.root / filename
        resolved_root = self.root.resolve(strict=True)
        resolved_path = path.resolve()
        if resolved_root not in resolved_path.parents:
            raise ArtifactStoreError("artifact path must remain inside evidence directory")
        return path

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _is_plain_component(value: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.fullmatch(value)) and "/" not in value and "\\" not in value
