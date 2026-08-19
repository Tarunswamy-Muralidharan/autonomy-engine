"""Tests for the Comprehend-backed PII detection layer.

The contract under test is not "Comprehend finds names" - that is AWS's job.
It is: the audit redactor gets STRICTLY stronger when Comprehend is on, and
never weaker, slower, or more fragile when Comprehend misbehaves. A PII
detector that can fail the governance gate is a liability, so most of these
tests are about failure.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.audit import comprehend
from app.audit.redact import redact_params, redact_text


class StubComprehend:
    """Stands in for the boto3 Comprehend client."""

    def __init__(self, entities=None, raises=None):
        self.entities = entities or []
        self.raises = raises
        self.calls = 0

    def detect_pii_entities(self, Text, LanguageCode):  # noqa: N803 - boto3 casing
        self.calls += 1
        if self.raises:
            raise self.raises
        return {"Entities": self.entities}


@pytest.fixture()
def comprehend_on(monkeypatch):
    """Enable the layer and hand back a factory for installing a stub."""
    monkeypatch.setenv("AUTONOMYGATE_PII", "comprehend")
    comprehend.reset_circuit()

    def install(stub):
        monkeypatch.setattr(comprehend, "_get_client", lambda: stub)
        return stub

    yield install
    comprehend.reset_circuit()


def entity(begin, end, type_, score=0.99):
    return {"BeginOffset": begin, "EndOffset": end, "Type": type_, "Score": score}


# ---------- the layer is off unless explicitly enabled ----------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AUTONOMYGATE_PII", raising=False)
    comprehend.reset_circuit()
    assert comprehend.detect_spans("Priya Raman lives in Chennai") == []


def test_disabled_layer_never_builds_a_client(monkeypatch):
    """No boto3 client, no credentials lookup, no latency when off."""
    monkeypatch.setenv("AUTONOMYGATE_PII", "regex")
    comprehend.reset_circuit()

    def explode():
        raise AssertionError("client must not be constructed while disabled")

    monkeypatch.setattr(comprehend, "_get_client", explode)
    assert comprehend.detect_spans("Priya Raman lives in Chennai") == []


# ---------- it catches what regex structurally cannot ----------

def test_redacts_a_name_regex_would_miss(comprehend_on):
    text = "Escalate the refund to Priya Raman today"
    assert "Priya Raman" in redact_text(text)  # regex alone is blind to it

    comprehend_on(StubComprehend([entity(23, 34, "NAME")]))
    out = redact_text(text)
    assert "Priya Raman" not in out
    assert "[REDACTED:NAME]" in out
    assert out.startswith("Escalate the refund to ")


def test_redacts_an_address(comprehend_on):
    text = "Ship it to 14 Peters Road, Chennai"
    comprehend_on(StubComprehend([entity(11, len(text), "ADDRESS")]))
    assert redact_text(text) == "Ship it to [REDACTED:ADDRESS]"


def test_low_confidence_entities_are_ignored(comprehend_on):
    """A weak guess must not shred an audit record."""
    text = "Escalate the refund to Priya Raman today"
    comprehend_on(StubComprehend([entity(23, 34, "NAME", score=0.40)]))
    assert redact_text(text) == text


def test_non_identifying_types_are_kept(comprehend_on):
    """Auditors need timestamps; redacting them adds no safety."""
    text = "Ran the job at 2026-08-19 09:30 IST"
    comprehend_on(StubComprehend([entity(15, 35, "DATE_TIME")]))
    assert redact_text(text) == text


# ---------- the two layers compose without corrupting each other ----------

def test_overlapping_detections_produce_one_clean_marker(comprehend_on):
    """Both layers flag the same email; it must be redacted exactly once."""
    text = "mail priya.raman@ourcorp.com now"
    comprehend_on(StubComprehend([entity(5, 28, "EMAIL")]))
    out = redact_text(text)
    assert out == "mail [REDACTED:EMAIL] now"
    assert "REDACTED:EMAIL]" in out and out.count("[REDACTED") == 1


def test_regex_and_comprehend_findings_both_survive(comprehend_on):
    text = "Priya Raman paid with 4111 1111 1111 1111"
    comprehend_on(StubComprehend([entity(0, 11, "NAME")]))
    out = redact_text(text)
    assert "[REDACTED:NAME]" in out
    assert "[REDACTED:CARD]" in out
    assert "4111" not in out and "Priya" not in out


def test_multiple_spans_do_not_shift_each_other(comprehend_on):
    """Right-to-left application keeps later offsets valid."""
    text = "Priya Raman and Arjun Mehta approved it"
    comprehend_on(StubComprehend([entity(0, 11, "NAME"), entity(16, 27, "NAME")]))
    assert redact_text(text) == "[REDACTED:NAME] and [REDACTED:NAME] approved it"


def test_nested_params_get_the_comprehend_layer_too(comprehend_on):
    comprehend_on(StubComprehend([entity(0, 11, "NAME")]))
    out = redact_params({"note": "Priya Raman", "nested": [{"who": "Priya Raman"}]})
    assert out["note"] == "[REDACTED:NAME]"
    assert out["nested"][0]["who"] == "[REDACTED:NAME]"


# ---------- failure must degrade to regex, never break the gate ----------

def test_service_error_falls_back_to_regex(comprehend_on):
    comprehend_on(StubComprehend(raises=RuntimeError("ThrottlingException")))
    out = redact_text("card 4111 1111 1111 1111 for Priya Raman")
    assert "[REDACTED:CARD]" in out      # regex layer still did its job
    assert "Priya Raman" in out          # NER layer degraded, did not crash


def test_circuit_opens_after_repeated_failures(comprehend_on):
    stub = comprehend_on(StubComprehend(raises=RuntimeError("timeout")))
    for _ in range(comprehend.FAILURE_THRESHOLD):
        redact_text("Priya Raman was here")
    calls_when_tripped = stub.calls
    for _ in range(5):
        redact_text("Priya Raman was here")
    assert stub.calls == calls_when_tripped, \
        "breaker must stop calling out once open"


def test_circuit_stays_closed_while_healthy(comprehend_on):
    stub = comprehend_on(StubComprehend([entity(0, 11, "NAME")]))
    for _ in range(comprehend.FAILURE_THRESHOLD + 3):
        redact_text("Priya Raman was here")
    assert stub.calls == comprehend.FAILURE_THRESHOLD + 3


def test_oversized_text_is_not_sent(comprehend_on):
    stub = comprehend_on(StubComprehend([]))
    redact_text("x" * (comprehend.MAX_ANALYZED_CHARS + 1))
    assert stub.calls == 0, "bound the call ourselves rather than let AWS reject it"


def test_trivial_text_is_not_sent(comprehend_on):
    stub = comprehend_on(StubComprehend([]))
    redact_text("hi")
    assert stub.calls == 0
