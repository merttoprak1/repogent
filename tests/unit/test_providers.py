from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from openai import OpenAI

from repogent.domain import RequirementsSpec
from repogent.providers import (
    DEFAULT_MODELS,
    KNOWN_PROVIDERS,
    GrokProvider,
    ModelPricing,
    OpenAIProvider,
    ProviderError,
    ScriptedProvider,
    default_model_for,
    validate_provider_name,
)


def test_known_providers_are_the_closed_allowlist() -> None:
    assert frozenset({"openai", "grok", "codex-cli", "scripted"}) == KNOWN_PROVIDERS
    assert "grok-cli" not in KNOWN_PROVIDERS
    assert validate_provider_name("codex-cli") == "codex-cli"
    assert validate_provider_name("grok") == "grok"
    assert default_model_for("codex-cli") == "default"
    assert default_model_for("openai") == DEFAULT_MODELS["openai"] == "gpt-5.6-sol"
    assert default_model_for("grok") == DEFAULT_MODELS["grok"] == "grok-4.6"
    assert default_model_for("scripted") == "scripted"


def test_validate_provider_name_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="provider must be openai, grok, codex-cli, or scripted"):
        validate_provider_name("grok-cli")


def test_grok_ready_requires_xai_key_not_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-count")
    readiness = GrokProvider.check_ready()
    assert readiness.ready is False
    assert readiness.provider == "grok"
    assert readiness.model == "grok-4.6"
    assert "XAI_API_KEY" in (readiness.reason or "")

    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    ready = GrokProvider.check_ready(model="grok-4.6")
    assert ready.ready is True
    assert ready.provider == "grok"


def test_grok_provider_builds_xai_openai_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_openai(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    monkeypatch.setattr("repogent.providers.OpenAI", fake_openai)
    GrokProvider(model="grok-4.6")
    assert captured["api_key"] == "xai-test-key"
    assert captured["base_url"] == "https://api.x.ai/v1"


def test_grok_provider_uses_chat_parse_and_records_usage() -> None:
    parsed = RequirementsSpec(
        objective="Add route", functional_requirements=[], acceptance_criteria=[]
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
        _request_id="req-grok",
    )
    calls: list[dict[str, object]] = []

    def parse(**kwargs: object) -> object:
        calls.append(kwargs)
        return response

    chat = SimpleNamespace(completions=SimpleNamespace(parse=parse))
    client = SimpleNamespace(chat=chat)
    provider = GrokProvider(
        client=cast(OpenAI, client),
        model="grok-4.6",
        pricing=ModelPricing(),
    )
    result = provider.generate(
        system_prompt="system",
        payload={"request": "add route"},
        output_type=RequirementsSpec,
    )
    assert result.output == parsed
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 7
    assert result.usage.request_id == "req-grok"
    assert "model" in calls[0]
    assert calls[0]["model"] == "grok-4.6"
    assert "messages" in calls[0]


def test_grok_provider_redacts_secrets_in_chat_messages() -> None:
    parsed = RequirementsSpec(
        objective="Add route", functional_requirements=[], acceptance_criteria=[]
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
        usage=None,
        _request_id="req-redacted",
    )
    calls: list[dict[str, object]] = []

    def parse(**kwargs: object) -> object:
        calls.append(kwargs)
        return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse)))
    provider = GrokProvider(
        client=cast(OpenAI, client),
        secrets=["explicit-configured-secret"],
    )
    provider.generate(
        system_prompt="system",
        payload={
            "request": "keep this source visible",
            "credentials": {
                "xai": "xai-explicit-secret-value",
                "nested": ["explicit-configured-secret"],
            },
        },
        output_type=RequirementsSpec,
    )
    serialized = str(calls[0]["messages"])
    assert "keep this source visible" in serialized
    assert "explicit-configured-secret" not in serialized


def test_grok_provider_rejects_missing_parsed_output() -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=None))],
        usage=None,
        _request_id="req-1",
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=lambda **kwargs: response))
    )
    provider = GrokProvider(client=cast(OpenAI, client))
    with pytest.raises(ProviderError, match="no parsed output"):
        provider.generate(system_prompt="system", payload={}, output_type=RequirementsSpec)


def test_grok_provider_caps_request_with_remaining_timeout() -> None:
    parsed = RequirementsSpec(
        objective="Add route", functional_requirements=[], acceptance_criteria=[]
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
        usage=None,
        _request_id="req-timeout",
    )
    calls: list[dict[str, object]] = []
    options: list[dict[str, object]] = []

    def parse(**kwargs: object) -> object:
        calls.append(kwargs)
        return response

    class Client:
        chat = SimpleNamespace(completions=SimpleNamespace(parse=parse))

        def with_options(self, **kwargs: object) -> "Client":
            options.append(kwargs)
            return self

    provider = GrokProvider(client=cast(OpenAI, Client()))
    provider.generate(
        system_prompt="system",
        payload={},
        output_type=RequirementsSpec,
        timeout_seconds=2.5,
    )
    assert options == [{"timeout": 2.5, "max_retries": 0}]
    assert "timeout" not in calls[0]


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
    provider = OpenAIProvider(client=cast(OpenAI, client), secrets=["explicit-configured-secret"])
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
