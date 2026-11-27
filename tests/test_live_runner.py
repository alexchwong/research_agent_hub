import json

from scripts import live_rah_refactor


def test_runner_without_credentials_writes_skipped_report(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    output = tmp_path / "reports/rah-live.json"
    code = live_rah_refactor.main(["--profile", "standard", "--output", str(output)])
    report = json.loads(output.read_text())
    assert code == 2
    assert set(report) == {"schema_version", "baseline_sha", "candidate_build_sha256", "host", "started_at", "finished_at",
                           "profiles_requested", "checks", "manual_checks", "summary"}
    assert report["profiles_requested"] == ["standard"] and report["summary"]["failed"] == 0
    assert all(row["status"] == "skipped" and row["code"] == "missing_credentials" for row in report["checks"])
    assert {"id", "profile", "route_id", "status", "duration_ms", "code", "detail", "remediation"} <= set(report["checks"][0])


def test_redaction_removes_keys_queries_and_paths():
    text = live_rah_refactor.redact("Bearer sk-or-v1-abc https://x.test/?email=a@b.c /home/alex/private.pdf sk-or-v1-zzz")
    assert "sk-or" not in text and "email=" not in text and "/home/alex" not in text
