"""PII redaction for audit records.

Audit logs are themselves sensitive assets: parameters may carry emails,
phone numbers, or card numbers. We redact BEFORE persisting, so raw PII
never reaches storage. (Redaction here is regex-based and deliberately
conservative; swap in an NER model behind the same function if needed.)
"""
from __future__ import annotations

import re

# Bounded quantifiers: unbounded [\w.+-]+ backtracks quadratically on long
# non-matching strings (found via adversarial testing - a 600KB digit-free
# param hung the process for minutes). Real emails/numbers fit these bounds.
_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("EMAIL", re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}\.[\w.]{2,24}"), "@"),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b"), None),
    ("PHONE", re.compile(r"\b(?:\+?\d{1,3}[ -]?)?\d{10}\b"), None),
    ("AADHAAR", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), None),
]

MAX_REDACT_LEN = 20_000  # audit stores a bounded excerpt; DynamoDB caps items at 400KB


def redact_text(text: str) -> str:
    if len(text) > MAX_REDACT_LEN:
        text = text[:MAX_REDACT_LEN] + f"...[TRUNCATED {len(text)} chars]"
    digitless = not any(ch.isdigit() for ch in text)
    for label, pattern, needle in _PATTERNS:
        if needle is not None and needle not in text:
            continue  # cheap pre-check: no '@' means no email to find
        if needle is None and digitless:
            continue  # number patterns can't match digit-free text
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
