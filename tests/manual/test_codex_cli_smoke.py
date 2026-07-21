import os

import pytest

from repogent.codex_cli import CodexCliProvider
from repogent.domain import RequirementsSpec

pytestmark = pytest.mark.manual


@pytest.mark.skipif(os.getenv("REPOGENT_CODEX_SMOKE") != "1", reason="opt in")
def test_authenticated_codex_cli_smoke() -> None:
    result = CodexCliProvider().generate(
        role="requirements",
        system_prompt="Return the requested schema.",
        payload={"request": "Describe a no-op change", "repository_context": []},
        output_type=RequirementsSpec,
        timeout_seconds=120,
    )

    assert result.output.objective
