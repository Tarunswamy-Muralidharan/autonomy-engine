"""PII redaction for audit records.

Audit logs are themselves sensitive assets: parameters may carry emails,
phone numbers, or card numbers. We redact BEFORE persisting, so raw PII
never reaches storage. (Redaction here is regex-based and deliberately
conservative; swap in an NER model behind the same function if needed.)
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"\b(?:\+?\d{1,3}[ -]?)?\d{10}\b")),
    ("AADHAAR", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
]


def redact_text(text: str) -> str:
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def redact_params(params: dict) -> dict:
    """Redact every string value in a (possibly nested) params dict."""
    def _walk(value):
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value
    return _walk(params)
