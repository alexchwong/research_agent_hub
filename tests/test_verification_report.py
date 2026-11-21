import json
import socket

import pytest

from verification_report import verification


def test_offline_network_guard():
    assert socket.getaddrinfo("127.0.0.1", 80)
    with pytest.raises(AssertionError, match="External network"):
        socket.getaddrinfo("example.invalid", 443)


def test_report_pass_and_skip(tmp_path):
    path = tmp_path / "reports" / "check.json"
    with verification(path, "example") as report:
        report["checks"].update(required="passed", optional="skipped")
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["schema_version"] == 1 and result["status"] == "passed"
    assert result["checks"]["optional"] == "skipped"
    assert result["duration_seconds"] >= 0


def test_report_failure_omits_exception_message(tmp_path):
    path = tmp_path / "check.json"
    with pytest.raises(ValueError, match="private detail"):
        with verification(path, "example"):
            raise ValueError("private detail")
    text = path.read_text(encoding="utf-8")
    result = json.loads(text)
    assert result["status"] == "failed" and result["error_type"] == "ValueError"
    assert "private detail" not in text