from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from openai import OpenAI

from repogent.domain import RequirementsSpec
from repogent.providers import OpenAIProvider, ProviderError, ScriptedProvider


def test_scripted_provider_validates_against_requested_schema() -> None:
    provider = ScriptedProvider(
        [
            {
                "objective": "Add health route",
                "functional_requirements": [],
                "acceptance_criteria": [],
            }
        ]
    )
    result = provider.generate(
        system_prompt="requirements", payload={}, output_type=RequirementsSpec
    )
    assert result.output.objective == "Add health route"


def test_openai_provider_uses_responses_parse_and_records_usage() -> None:
    parsed = RequirementsSpec(
        objective="Add route", functional_requirements=[], acceptance_criteria=[]
    )
    response = SimpleNamespace(
        output_parsed=parsed,
        usage=SimpleNamespace(input_tokens=12, output_tokens=7),
        _request_id="req-123",
    )
    client = SimpleNamespace(responses=SimpleNamespace(parse=lambda **kwargs: response))
    provider = OpenAIProvider(client=cast(OpenAI, client), model="gpt-5.6-sol")
    result = provider.generate(
        system_prompt="system", payload={"request": "add route"}, output_type=RequirementsSpec
    )
    assert result.output == parsed
    assert result.usage.input_tokens == 12
    assert result.usage.request_id == "req-123"
    assert result.usage.estimated_cost_usd == Decimal("0.00027")


def test_openai_provider_rejects_missing_parsed_output() -> None:
    response = SimpleNamespace(output_parsed=None, usage=None, _request_id="req-1")
    client = SimpleNamespace(responses=SimpleNamespace(parse=lambda **kwargs: response))
    provider = OpenAIProvider(client=cast(OpenAI, client))
    with pytest.raises(ProviderError, match="no parsed output"):
        provider.generate(system_prompt="system", payload={}, output_type=RequirementsSpec)
