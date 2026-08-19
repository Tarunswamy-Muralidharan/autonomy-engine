"""Amazon Comprehend PII detection for the audit redactor.

Regex catches PII that has *shape* - an email has an @, a card has 13-19
digits. It cannot catch PII that only has *meaning*: a person's name, a
street address, a passport number sitting in a free-text field. Those are
exactly what leaks into an agent's tool parameters ("email the Q3 numbers to
Priya Raman at 14 Peters Road, Chennai"), and they are exactly what a
regulator asks about.

So detection is layered:

  regex        - deterministic, offline, zero-latency, always runs
  Comprehend   - contextual NER, catches names/addresses/national IDs

Comprehend AUGMENTS the regex layer, it never replaces it. If the service is
slow, throttled, unreachable, or unauthorized, redaction silently falls back
to regex alone and the request still completes. A PII detector that can take
the governance gate down with it is worse than no PII detector.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("autonomygate.comprehend")

# Comprehend's synchronous API accepts 100KB, but audit redaction sits in the
# request path - we bound the call far below the hard limit so a large payload
# costs latency we control rather than latency AWS controls.
MAX_ANALYZED_CHARS = 4_500
MIN_ANALYZED_CHARS = 8
MIN_CONFIDENCE = 0.85

# Entity types worth destroying in an audit record. Deliberately NOT the full
# Comprehend set: DATE_TIME, AGE, URL and USERNAME are frequently the very
# fields an auditor needs to read, and redacting them would make the trail
# useless without making it meaningfully safer.
REDACTED_TYPES = frozenset({
    "NAME", "ADDRESS", "EMAIL", "PHONE", "PASSWORD",
    "SSN", "PASSPORT_NUMBER", "DRIVER_ID", "LICENSE_PLATE",
    "CREDIT_DEBIT_NUMBER", "CREDIT_DEBIT_CVV", "CREDIT_DEBIT_EXPIRY", "PIN",
    "BANK_ACCOUNT_NUMBER", "BANK_ROUTING", "SWIFT_CODE",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "IP_ADDRESS", "MAC_ADDRESS",
    # India-specific national identifiers
    "IN_AADHAAR", "IN_PAN", "IN_NREGA", "IN_VOTER_NUMBER",
    "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
})

# Circuit breaker: after this many consecutive failures we stop calling out on
# the request path entirely. Without it, a Comprehend outage would add its full
# timeout to every single evaluate() call.
FAILURE_THRESHOLD = 3

_lock = threading.Lock()
_client = None
_consecutive_failures = 0
_tripped = False


def enabled() -> bool:
    return os.environ.get("AUTONOMYGATE_PII", "regex").lower() == "comprehend"


def _get_client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        # Short timeouts and no retries: this is a best-effort enrichment on a
        # latency-sensitive path. Failing fast to regex beats blocking.
        _client = boto3.client(
            "comprehend",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=Config(connect_timeout=1, read_timeout=2,
                          retries={"max_attempts": 1}),
        )
    return _client


def _record_failure(exc: Exception) -> None:
    global _consecutive_failures, _tripped
    with _lock:
        _consecutive_failures += 1
        if _consecutive_failures >= FAILURE_THRESHOLD and not _tripped:
            _tripped = True
            log.error('{"event": "comprehend_circuit_open", "reason": "%s", '
                      '"effect": "falling back to regex-only redaction"}', exc)


def _record_success() -> None:
    global _consecutive_failures, _tripped
    if _consecutive_failures or _tripped:
        with _lock:
            _consecutive_failures = 0
            _tripped = False


def reset_circuit() -> None:
    """Test hook: clear breaker state between cases."""
    global _client, _consecutive_failures, _tripped
    with _lock:
        _client = None
        _consecutive_failures = 0
        _tripped = False


def detect_spans(text: str) -> list[tuple[int, int, str]]:
    """Return (begin, end, ENTITY_TYPE) spans, or [] if unavailable.

    Never raises. Callers treat an empty result as "regex only".
    """
    if _tripped or not enabled():
        return []
    if not (MIN_ANALYZED_CHARS <= len(text) <= MAX_ANALYZED_CHARS):
        return []
    try:
        resp = _get_client().detect_pii_entities(Text=text, LanguageCode="en")
    except Exception as exc:  # noqa: BLE001 - any failure means "use regex"
        _record_failure(exc)
        return []
    _record_success()
    return [
        (e["BeginOffset"], e["EndOffset"], e["Type"])
        for e in resp.get("Entities", [])
        if e.get("Type") in REDACTED_TYPES
        and e.get("Score", 0.0) >= MIN_CONFIDENCE
    ]
