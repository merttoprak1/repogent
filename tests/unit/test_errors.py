import pytest
from pydantic import ValidationError

from repogent.domain import WorkflowKind
from repogent.errors import ErrorCode, ErrorDetail, RepogentError, RetryClass


def test_error_detail_serializes_stable_safe_contract() -> None:
    unsafe_exception_text = "secret-value at /private/secret/path"
    detail = ErrorDetail(
        code=ErrorCode.POLICY,
        message="safe message",
        remediation="inspect the run report",
        retry=RetryClass.NON_RETRYABLE,
        run_id="run-1",
        run_kind=WorkflowKind.PATCH_REVIEW,
    )

    assert detail.model_dump(mode="json") == {
        "schema_version": "1",
        "code": "policy_error",
        "message": "safe message",
        "remediation": "inspect the run report",
        "retry": "non_retryable",
        "run_id": "run-1",
        "run_kind": "patch_review",
    }
    assert unsafe_exception_text not in detail.model_dump_json()


def test_error_detail_enforces_public_message_bound() -> None:
    with pytest.raises(ValidationError):
        ErrorDetail(
            code=ErrorCode.INTERNAL,
            message="x" * 513,
            retry=RetryClass.NON_RETRYABLE,
        )


def test_repogent_error_exposes_only_typed_detail_as_its_message() -> None:
    detail = ErrorDetail(
        code=ErrorCode.UNKNOWN_RUN,
        message="No run exists for the supplied run ID.",
        retry=RetryClass.NON_RETRYABLE,
        run_id="missing",
    )

    error = RepogentError(detail)

    assert error.detail is detail
    assert str(error) == detail.message
