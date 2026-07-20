from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_NAME_PATTERN = (
    r"password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|client[_-]?secret|"
    r"private[_-]?key|aws[_-]?secret[_-]?access[_-]?key|aws[_-]?session[_-]?token"
)
_ASSIGNMENT_PREFIX = (
    rf"(?P<prefix>(?<![A-Za-z0-9_])(?P<keyquote>['\"]?)"
    rf"(?:{_SENSITIVE_NAME_PATTERN})(?P=keyquote)\s*[=:]\s*)"
)
_QUOTED_ASSIGNMENT = re.compile(
    rf"(?i){_ASSIGNMENT_PREFIX}"
    r"(?P<quote>['\"])(?P<value>[^\r\n]*?)(?P=quote)"
)
_UNQUOTED_ASSIGNMENT = re.compile(
    rf"(?i){_ASSIGNMENT_PREFIX}"
    r"(?P<value>[^\s,;)}\]'\"]+)"
)
_SENSITIVE_FIELDS = {
    "accesskey",
    "apikey",
    "authorization",
    "awssecretaccesskey",
    "awssessiontoken",
    "clientsecret",
    "credential",
    "credentials",
    "passwd",
    "password",
    "privatekey",
    "pwd",
    "secret",
    "token",
}
_SECRET_VALUES = (
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://"
        r"[^\s,;)}\]'\"<>]+"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
)


def redact_text(text: str, explicit_secrets: Sequence[str] = ()) -> str:
    """Redact known secret values while preserving surrounding text delimiters."""
    result = text
    result = _QUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}"
        f"{REDACTED}{match.group('quote')}",
        result,
    )
    result = _UNQUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        result,
    )
    for pattern in _SECRET_VALUES:
        result = pattern.sub(REDACTED, result)
    for secret in sorted((item for item in explicit_secrets if item), key=len, reverse=True):
        result = result.replace(secret, REDACTED)
    return result


def sanitize_data(value: Any, explicit_secrets: Sequence[str] = ()) -> Any:
    """Recursively sanitize string values in JSON-like provider or artifact data."""
    if isinstance(value, str):
        return redact_text(value, explicit_secrets)
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if isinstance(key, str)
                and isinstance(item, str)
                and _is_sensitive_field(key)
                else sanitize_data(item, explicit_secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(sanitize_data(item, explicit_secrets) for item in value)
    if isinstance(value, list):
        return [sanitize_data(item, explicit_secrets) for item in value]
    return value


def _is_sensitive_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SENSITIVE_FIELDS
