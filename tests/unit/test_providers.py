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


def test_openai_provider_recursively_redacts_secrets_at_request_boundary() -> None:
    parsed = RequirementsSpec(
        objective="Add route", functional_requirements=[], acceptance_criteria=[]
    )
    response = SimpleNamespace(output_parsed=parsed, usage=None, _request_id="req-redacted")
    calls: list[dict[str, object]] = []

    def parse(**kwargs: object) -> object:
        calls.append(kwargs)
        return response

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    provider = OpenAIProvider(
        client=cast(OpenAI, client), secrets=["explicit-configured-secret"]
    )
    secrets = {
        "openai": "sk-proj-abcdefghijklmnop",
        "nested": [
            "token=ghp_abcdefghijklmnopqrstuvwxyz123456",
            {"aws": "AKIAIOSFODNN7EXAMPLE"},
            "aws_session_token=aws-session-secret",
            "postgresql://alice:s3cr3t@db.example/app",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signatureABCDE",
            "password=hunter2",
            "explicit-configured-secret",
        ],
        "structured": {
            "password": "correct horse battery staple",
            "token": "opaque-token-value",
            "api_key": "opaque-api-key-value",
            "note": 'password="another secret with spaces"',
        },
    }

    provider.generate(
        system_prompt="system",
        payload={"request": "keep this source visible", "credentials": secrets},
        output_type=RequirementsSpec,
    )

    serialized_request = str(calls[0]["input"])
    assert "keep this source visible" in serialized_request
    for secret in (
        "sk-proj-abcdefghijklmnop",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "AKIAIOSFODNN7EXAMPLE",
        "aws-session-secret",
        "postgresql://alice:s3cr3t@db.example/app",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signatureABCDE",
        "hunter2",
        "explicit-configured-secret",
        "correct horse battery staple",
        "opaque-token-value",
        "opaque-api-key-value",
        "another secret with spaces",
    ):
        assert secret not in serialized_request


def test_openai_provider_rejects_missing_parsed_output() -> None:
    response = SimpleNamespace(output_parsed=None, usage=None, _request_id="req-1")
    client = SimpleNamespace(responses=SimpleNamespace(parse=lambda **kwargs: response))
    provider = OpenAIProvider(client=cast(OpenAI, client))
    with pytest.raises(ProviderError, match="no parsed output"):
        provider.generate(system_prompt="system", payload={}, output_type=RequirementsSpec)


def test_openai_provider_caps_request_with_remaining_timeout() -> None:
    parsed = RequirementsSpec(
        objective="Add route", functional_requirements=[], acceptance_criteria=[]
    )
    response = SimpleNamespace(output_parsed=parsed, usage=None, _request_id="req-timeout")
    calls: list[dict[str, object]] = []

    def parse(**kwargs: object) -> object:
        calls.append(kwargs)
        return response

    options: list[dict[str, object]] = []

    class Client:
        responses = SimpleNamespace(parse=parse)

        def with_options(self, **kwargs: object) -> "Client":
            options.append(kwargs)
            return self

    provider = OpenAIProvider(client=cast(OpenAI, Client()))

    provider.generate(
        system_prompt="system",
        payload={},
        output_type=RequirementsSpec,
        timeout_seconds=2.5,
    )

    assert options == [{"timeout": 2.5, "max_retries": 0}]
    assert "timeout" not in calls[0]
