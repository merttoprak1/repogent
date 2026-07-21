from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from repogent.domain import ProviderCallEvidence, ProviderUsage
from repogent.sanitization import redact_text, sanitize_data

T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        evidence: ProviderCallEvidence | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.evidence = evidence


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: Decimal = Decimal("5.00")
    output_per_million: Decimal = Decimal("30.00")


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    output: T
    usage: ProviderUsage
    evidence: ProviderCallEvidence | None = None


class ModelProvider(Protocol):
    def generate(
        self,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        output_type: type[T],
        role: str = "unknown",
    ) -> ProviderResult[T]: ...


class ScriptedProvider:
    def __init__(self, outputs: Sequence[Mapping[str, Any]]) -> None:
        self._outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        output_type: type[T],
        role: str = "unknown",
        timeout_seconds: float | None = None,
    ) -> ProviderResult[T]:
        del timeout_seconds
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "payload": dict(payload),
                "output_type": output_type.__name__,
                "role": role,
            }
        )
        if not self._outputs:
            raise ProviderError("scripted provider has no output remaining")
        raw = self._outputs.pop(0)
        try:
            output = output_type.model_validate(raw)
        except ValidationError as error:
            raise ProviderError(f"scripted output failed validation: {error}") from error
        return ProviderResult(output=output, usage=ProviderUsage(model="scripted"))

    @classmethod
    def from_json(cls, path: str) -> ScriptedProvider:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ProviderError("script file must contain a JSON array of objects")
        return cls(data)


class OpenAIProvider:
    def __init__(
        self,
        *,
        client: OpenAI | None = None,
        model: str = "gpt-5.6-sol",
        pricing: ModelPricing | None = None,
        secrets: Sequence[str] = (),
    ) -> None:
        self.client = client or OpenAI()
        self.model = model
        self.pricing = pricing or ModelPricing()
        self.secrets = tuple(secrets)

    def generate(
        self,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        output_type: type[T],
        role: str = "unknown",
        timeout_seconds: float | None = None,
    ) -> ProviderResult[T]:
        del role
        started = time.monotonic()
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ProviderError("provider timeout exhausted")
        request_client = self.client
        if timeout_seconds is not None:
            request_client = self.client.with_options(
                timeout=timeout_seconds, max_retries=0
            )
        try:
            response = request_client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": redact_text(system_prompt, self.secrets)},
                    {
                        "role": "user",
                        "content": json.dumps(
                            sanitize_data(payload, self.secrets), sort_keys=True
                        ),
                    },
                ],
                text_format=output_type,
            )
        except OpenAIError as error:
            raise ProviderError(f"OpenAI request failed: {error}") from error
        output = response.output_parsed
        if output is None:
            raise ProviderError("OpenAI response contained no parsed output")
        usage = response.usage
        input_tokens = usage.input_tokens if usage else 0
        output_tokens = usage.output_tokens if usage else 0
        estimated_cost = (
            Decimal(input_tokens) * self.pricing.input_per_million
            + Decimal(output_tokens) * self.pricing.output_per_million
        ) / Decimal(1_000_000)
        return ProviderResult(
            output=output,
            usage=ProviderUsage(
                model=self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=estimated_cost,
                request_id=response._request_id,
                latency_seconds=time.monotonic() - started,
            ),
        )
