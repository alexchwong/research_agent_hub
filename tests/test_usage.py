"""T14: per-entry usage summaries and their completeness labels."""
import json

import pytest

from rah import usage

KEY = ("gptr", "research_report", "a" * 64, usage.observer_digest())


def log(path, *records):
    path.write_text("".join((record if isinstance(record, str) else json.dumps(record)) + "\n" for record in records))
    return path


def ok(cost=0.01, prompt=10, completion=2, model="m/a", **extra):
    usage_row = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cost is not None:
        usage_row["cost"] = cost
    return {"endpoint": "/api/v1/chat/completions", "status": 200, "stream": False, "id": "g", "model": model,
            "provider": "P", "usage": usage_row | extra, "reason": None}


@pytest.fixture
def qualified(monkeypatch):
    monkeypatch.setattr(usage, "COVERAGE", frozenset({KEY}))


def test_qualified_complete(tmp_path, qualified):
    summary = usage.summarize(log(tmp_path / "u", ok(), ok(cost=0.02)), route_key=KEY, openrouter_expected=True)
    assert summary["complete"] and summary["reason"] is None and summary["cost_usd"] == "0.03"
    assert summary["prompt_tokens"] == 20 and summary["requests"] == 2 and summary["models"] == {"m/a": 2}


def test_unverified_coverage_is_never_complete(tmp_path):
    summary = usage.summarize(log(tmp_path / "u", ok()), route_key=KEY, openrouter_expected=True)
    assert not summary["complete"] and summary["coverage"] == "unverified" and summary["reason"] == "coverage_unverified"


def test_missing_cost_errors_no_response_and_byok(tmp_path, qualified):
    error = {"endpoint": "/api/v1/chat/completions", "status": 429, "usage": None, "reason": "no_usage"}
    gone = {"endpoint": "/api/v1/embeddings", "status": None, "usage": None, "reason": "no_response"}
    summary = usage.summarize(log(tmp_path / "u", ok(cost=None), error, gone, ok(is_byok=True)), route_key=KEY,
                              openrouter_expected=True)
    assert summary["cost_missing"] == 1 and summary["errors"] == 1 and summary["no_response"] == 1 and summary["byok"] == 1
    assert not summary["complete"] and summary["cost_usd"] == "0.01"


def test_aborted_interrupted_malformed_and_truncated(tmp_path, qualified, monkeypatch):
    aborted = ok() | {"reason": "aborted"}
    assert not usage.summarize(log(tmp_path / "a", aborted), route_key=KEY, openrouter_expected=True)["complete"]
    interrupted = usage.summarize(log(tmp_path / "i", ok()), route_key=KEY, openrouter_expected=True, interrupted=True)
    assert interrupted["reason"] == "interrupted" and not interrupted["complete"]
    malformed = usage.summarize(log(tmp_path / "m", ok(), "{not json"), route_key=KEY, openrouter_expected=True)
    assert malformed["reason"] == "unparsed" and not malformed["complete"]
    monkeypatch.setattr(usage, "LINE_CAP", 1)
    truncated = usage.summarize(log(tmp_path / "t", ok(), ok()), route_key=KEY, openrouter_expected=True)
    assert truncated["reason"] == "truncated" and truncated["requests"] == 1 and not truncated["complete"]


def test_zero_lines(tmp_path, qualified):
    expected = usage.summarize(tmp_path / "missing", route_key=KEY, openrouter_expected=True)
    assert expected["requests"] == 0 and expected["reason"] == "no_usage_observed" and not expected["complete"]
    other = usage.summarize(tmp_path / "missing", route_key=KEY, openrouter_expected=False)
    assert other["reason"] is None and other["openrouter_expected"] is False


def test_scope_gate_mapping_and_encode_limit():
    sent = usage.scope_gate({"model": "m", "prompt_tokens": 3, "completion_tokens": 4, "cost": "0.001"}, sent=True)
    assert sent["complete"] and sent["requests"] == 1 and sent["cost_usd"] == "0.001"
    assert usage.scope_gate(None, sent=False)["requests"] == 0
    assert usage.scope_gate(None, sent=True, error="x")["errors"] == 1
    big = dict(sent, models={("m" * 190) + str(index): 1 for index in range(60)})
    assert len(usage.encode(big)) <= 8192
