from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from repogent.domain import VersionedModel, WorkflowKind


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNKNOWN_RUN = "unknown_run"
    OPERATION_NOT_ALLOWED = "operation_not_allowed"
    STALE_DIGEST = "stale_digest"
    LIMIT_EXCEEDED = "limit_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    VALIDATION_FAILED = "validation_failed"
    POLICY = "policy_error"
    INTERNAL = "internal_error"


class RetryClass(StrEnum):
    READ_ONLY = "read_only"
    RECONCILE_FIRST = "reconcile_first"
    NON_RETRYABLE = "non_retryable"


class ErrorDetail(VersionedModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=512)
    remediation: str | None = Field(default=None, max_length=512)
    retry: RetryClass
    run_id: str | None = Field(default=None, max_length=256)
    run_kind: WorkflowKind | None = None


class RepogentError(RuntimeError):
    def __init__(self, detail: ErrorDetail) -> None:
        super().__init__(detail.message)
        self.detail = detail
