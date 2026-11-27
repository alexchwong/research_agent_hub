"""Per-entry OpenRouter usage, summarised from the observer's usage.jsonl.

Figures are OpenRouter's own per-response records for requests made by one RAH-launched process. Nothing here is
derived from shared-key totals. "complete" requires a coverage qualification for the exact pinned route.
"""
from decimal import Decimal, InvalidOperation
from importlib import resources
import json

from .common import digest

LINE_CAP = 100_000
BYTE_CAP = 16 * 1024 * 1024
MODEL_KEYS = 20
OBSERVER = "openrouter_observer.py"

# (app, mode, lock_sha256, observer_sha256) entries proven by a passing installed pinned-upstream probe report
# (DEVEL.md, "Installed probes") and added only after owner approval. Routes without an entry are "unverified".
COVERAGE = frozenset()


def observer_bytes():
    return resources.files("rah").joinpath(OBSERVER).read_bytes()


def observer_digest():
    return digest(observer_bytes())


def route_key(app, mode, lock_sha256):
    return (app, mode, lock_sha256, observer_digest())


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def summarize(path, *, route_key, openrouter_expected, interrupted=False):
    summary = {"basis": "observed", "coverage": "qualified" if tuple(route_key) in COVERAGE else "unverified",
               "requests": 0, "with_usage": 0, "errors": 0, "no_response": 0, "prompt_tokens": 0,
               "completion_tokens": 0, "cost_usd": None, "cost_missing": 0, "byok": 0, "models": {},
               "complete": False, "reason": None, "openrouter_expected": bool(openrouter_expected)}
    cost, aborted, unparsed, truncated = None, 0, 0, False
    try:
        with open(path, "rb") as handle:
            raw = handle.read(BYTE_CAP + 1)
    except FileNotFoundError:
        raw = b""
    except OSError:
        raw, unparsed = b"", 1
    if len(raw) > BYTE_CAP:
        raw, truncated = raw[:BYTE_CAP], True
    lines = raw.splitlines()
    if len(lines) > LINE_CAP:
        lines, truncated = lines[:LINE_CAP], True
    for line in lines:
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            unparsed += 1
            continue
        summary["requests"] += 1
        reason = record.get("reason")
        status = record.get("status")
        if reason == "no_response":
            summary["no_response"] += 1
            continue
        if reason == "aborted":
            aborted += 1
        elif reason in ("unparsed", "too_large"):
            unparsed += 1
        ok = isinstance(status, int) and 200 <= status < 300
        if not ok:
            summary["errors"] += 1
        model = record.get("model")
        if isinstance(model, str) and model:
            if model in summary["models"] or len(summary["models"]) < MODEL_KEYS:
                summary["models"][model[:200]] = summary["models"].get(model[:200], 0) + 1
        usage = record.get("usage")
        if isinstance(usage, dict):
            summary["with_usage"] += 1
            for key in ("prompt_tokens", "completion_tokens"):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    summary[key] += value
            value = _decimal(usage.get("cost"))
            if value is None:
                if ok:
                    summary["cost_missing"] += 1
            else:
                cost = (cost or Decimal(0)) + value
            if usage.get("is_byok") is True:
                summary["byok"] += 1
        elif ok:
            summary["cost_missing"] += 1
    summary["cost_usd"] = format(cost, "f") if cost is not None else None
    if interrupted:
        summary["reason"] = "interrupted"
    elif truncated:
        summary["reason"] = "truncated"
    elif unparsed:
        summary["reason"] = "unparsed"
    elif openrouter_expected and summary["requests"] == 0:
        summary["reason"] = "no_usage_observed"
    elif summary["coverage"] != "qualified":
        summary["reason"] = "coverage_unverified"
    summary["aborted"] = aborted
    summary["complete"] = (summary["coverage"] == "qualified" and not interrupted and not truncated and not unparsed
                           and not aborted and summary["cost_missing"] == 0
                           and (summary["requests"] > 0 or not openrouter_expected))
    return summary


def scope_gate(observed, *, sent, error=None):
    """Map the Scope Gate's single directly observed response into the common summary shape."""
    summary = {"basis": "observed", "coverage": "qualified", "requests": 1 if sent else 0, "with_usage": 0,
               "errors": 1 if (sent and error) else 0, "no_response": 0, "prompt_tokens": 0, "completion_tokens": 0,
               "cost_usd": None, "cost_missing": 0, "byok": 0, "models": {}, "complete": False, "reason": None,
               "aborted": 0, "openrouter_expected": True}
    if observed:
        tokens = [observed.get("prompt_tokens"), observed.get("completion_tokens")]
        if any(isinstance(value, int) for value in tokens):
            summary["with_usage"] = 1
            summary["prompt_tokens"] = tokens[0] if isinstance(tokens[0], int) else 0
            summary["completion_tokens"] = tokens[1] if isinstance(tokens[1], int) else 0
        try:
            value = Decimal(observed["cost"]) if observed.get("cost") is not None else None
        except (InvalidOperation, TypeError):
            value = None
        if value is not None and value.is_finite() and value >= 0:
            summary["cost_usd"] = format(value, "f")
        elif not error:
            summary["cost_missing"] = 1
        if isinstance(observed.get("model"), str):
            summary["models"] = {observed["model"][:200]: 1}
    elif sent and not error:
        summary["cost_missing"] = 1
    summary["complete"] = bool(sent) and not error and summary["cost_missing"] == 0
    return summary


NOT_OBSERVED = {"basis": "not_observed"}


def encode(summary):
    """JSON for chat_entry.usage_json, kept under its 8 KiB limit."""
    text = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    if len(text) > 8192:
        text = json.dumps(dict(summary, models={}), ensure_ascii=False, separators=(",", ":"))
    return text
