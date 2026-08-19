"""PII redaction for audit records.

Audit logs are themselves sensitive assets: parameters may carry emails,
phone numbers, or card numbers, and we redact BEFORE persisting so raw PII
never reaches the audit table.

Two detection layers feed one redaction pass:

  regex        always on, deterministic, offline, catches shaped PII
  Comprehend   optional (AUTONOMYGATE_PII=comprehend), catches meaningful
               PII regex cannot see - names, addresses, national IDs

Both layers produce character spans over the SAME original text; the spans
are merged and applied right-to-left in one pass. Applying them sequentially
would corrupt offsets and let one layer redact inside another's marker.
"""
from __future__ import annotations

import re

from . import comprehend

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


def _regex_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    digitless = not any(ch.isdigit() for ch in text)
    for label, pattern, needle in _PATTERNS:
        if needle is not None and needle not in text:
            continue  # cheap pre-check: no '@' means no email to find
        if needle is None and digitless:
            continue  # number patterns can't match digit-free text
        spans.extend((m.start(), m.end(), label) for m in pattern.finditer(text))
    return spans


def _merge(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Drop overlaps, preferring the longest span at each position.

    Two detectors legitimately flag the same characters (Comprehend's EMAIL
    and our EMAIL regex). Redacting the wider match once is correct; redacting
    both would splice a marker into the middle of another marker.
    """
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    merged: list[tuple[int, int, str]] = []
    last_end = -1
    for begin, end, label in spans:
        if begin >= last_end:
            merged.append((begin, end, label))
            last_end = end
    return merged


def redact_text(text: str) -> str:
    if len(text) > MAX_REDACT_LEN:
        text = text[:MAX_REDACT_LEN] + f"...[TRUNCATED {len(text)} chars]"
    spans = _regex_spans(text)
    # Comprehend spans are computed over the same (already truncated) string,
    # so the two offset spaces agree.
    spans.extend(comprehend.detect_spans(text))
    for begin, end, label in reversed(_merge(spans)):
        text = f"{text[:begin]}[REDACTED:{label}]{text[end:]}"
    return text


def redact_params(params: dict) -> dict:
    """Redact PII anywhere in a (possibly nested) params structure.

    Covers dict KEYS and NUMERIC values too: a 16-digit card sent as a JSON
    number, or an email used as a key, would otherwise reach storage in the
    clear (both found in adversarial testing).
    """
    def _walk(value):
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and len(str(abs(value))) >= 10:
            # long bare integers are card/phone/id shaped - scan them as text
            redacted = redact_text(str(value))
            return redacted if "[REDACTED" in redacted else value
        if isinstance(value, dict):
            return {redact_text(str(k)): _walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_walk(v) for v in value]
        return value
    return _walk(params)
