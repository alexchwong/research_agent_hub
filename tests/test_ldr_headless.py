"""T3, T4, T5, T6: citation parser, status precedence, publication replay and the runner's progress callback."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from rah import gptr_pipeline, ldr_pipeline
from rah.common import Problem, private_directory, write_json
from rah.projects import structured_citations
from test_pipelines import RIS, hub, scope_to_brief  # noqa: F401  (hub is a fixture)

RUNNER = Path(__file__).resolve().parents[1] / "rah" / "ldr_guided.py"


def records(count, bad=None):
    rows = []
    for index in range(1, count + 1):
        title = "" if index == bad else f"TI  - Trial {index}\n"
        rows.append(("TY  - ELEC\n" + title + f"UR  - https://pubmed.ncbi.nlm.nih.gov/{index}/\nER  - \n") if index != bad
                    else "TY  - ELEC\nXX\nER  - \n")
    return "".join(rows)


def test_parser_keeps_first_250_and_counts_all(tmp_path):
    path = tmp_path / "a.ris"
    path.write_text(records(251))
    with pytest.raises(Problem):
        structured_citations(path)
    parsed = structured_citations(path, overflow="truncate")
    assert len(parsed.rows) == 250 and parsed.total == 251 and parsed.truncated
    path.write_text(records(10_000))
    parsed = structured_citations(path, overflow="truncate")
    assert parsed.total == 10_000 and len(parsed.rows) == 250
    path.write_text(records(251, bad=251))
    with pytest.raises(Problem):
        structured_citations(path, overflow="truncate")
    path.write_text(records(3))
    parsed = structured_citations(path)
    assert parsed.total == 3 and not parsed.truncated and parsed.rows[0]["pmid"] == "1"


def attempt(tmp_path, completion=None, outputs=("report.md", "citations.ris"), tamper=False):
    folder = private_directory(tmp_path / "attempt")
    out = private_directory(folder / "outputs")
    listed = []
    for name in outputs:
        body = b"# R\n\n## Sources\n" if name == "report.md" else RIS.encode()
        (out / name).write_bytes(body)
        listed.append({"name": name, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)})
    if tamper:
        (out / outputs[0]).write_bytes(b"changed")
    write_json(folder / "result.json", {"research_id": "r", "sources_count": 2, "outputs": listed, "ris_error": None})
    if completion == "succeeded":
        write_json(folder / "completion.json", {"status": "succeeded", "outputs": listed, "finished_at": "now"})
    elif completion:
        write_json(folder / "completion.json", {"status": "failed", "reason": completion})
    return folder


def process(status="exited", code=0):
    return gptr_pipeline.ProcessResult(status, code, 1.0, [])


@pytest.mark.parametrize("completion, result, expected", [
    ("succeeded", process(), ("succeeded", None)),
    ("ValueError", process(code=1), ("failed", "native_failed")),
    ("succeeded", process(code=1), ("failed", "native_failed")),        # non-zero exit after writing success
    ("succeeded", process("timeout", None), ("failed", "stage_timeout")),
    ("succeeded", process("cancelled", None), ("cancelled", "cancelled")),  # cancel after success was written
    (None, process("start_failed", None), ("failed", "worker_start")),
    (None, process(), ("failed", "native_output")),
    ("no_report", process(code=3), ("failed", "native_output")),
])
def test_status_precedence(tmp_path, completion, result, expected):
    folder = attempt(tmp_path, completion)
    collected = ldr_pipeline.collect(folder, result)
    assert (collected["status"], collected["code"]) == expected


def test_hash_mismatch_is_native_output(tmp_path):
    folder = attempt(tmp_path, "succeeded", tamper=True)
    assert ldr_pipeline.collect(folder, process())["code"] == "native_output"


def test_process_json_is_written_by_run_group_and_completion_never(tmp_path):
    folder = tmp_path / "group"
    result = gptr_pipeline.run_group(folder, [sys.executable, "-c", "print('x')"], {}, deadline=30, step="s", attempt=1,
                                     operation="o")
    assert result.status == "exited" and result.exit_code == 0
    assert json.loads((folder / "process.json").read_text())["status"] == "exited"
    assert not (folder / "completion.json").exists()


def fake_package(folder, quick_summary_body, exporter_body="return Result(b'')"):
    package = folder / "local_deep_research"
    for name in ("", "api", "exporters"):
        (package / name).mkdir(parents=True, exist_ok=True)
        (package / name / "__init__.py").write_text("")
    (package / "api/research_functions.py").write_text(
        "def quick_summary(query, progress_callback=None, **kwargs):\n" + "\n".join("    " + line for line in quick_summary_body.splitlines()) + "\n")
    (package / "exporters/ris_exporter.py").write_text(
        "class Result:\n    def __init__(self, content):\n        self.content = content\n\n"
        "class RISExporter:\n    def export(self, markdown):\n        " + exporter_body + "\n")


def run_runner(tmp_path, body, exporter="return Result(b'')"):
    folder = private_directory(tmp_path / "runner")
    fake_package(folder, body, exporter)
    target = folder / "ldr_guided.py"
    target.write_bytes(RUNNER.read_bytes())
    completed = subprocess.run([sys.executable, str(target)], cwd=folder, input=json.dumps({"prompt": "Public question"}).encode(),
                               capture_output=True, timeout=60)
    read = lambda name: json.loads((folder / name).read_text()) if (folder / name).exists() else None
    return completed.returncode, read("completion.json"), read("progress.json"), folder


REPORT = "return {'research_id': 'r', 'sources_count': 1, 'formatted_findings': '# A\\n\\n## Sources\\n\\n[1] T\\nURL: https://x.org/a\\n'}"


def test_callback_tolerates_any_input(tmp_path):
    calls = "\n".join([
        "for percent in (None, float('nan'), float('inf'), -5, 150, '40', True, 42.5):",
        "    progress_callback('Step \\x1b[31m \\u00e9' + 'x' * 300, percent, None)",
        "progress_callback(None, 10, 'not a dict')",
        "progress_callback(123, 20, {'phase': 7})",
        "progress_callback()",
        "progress_callback('late \\x07', 5, {'phase': 'p' * 100})",
        REPORT])
    code, completion, progress, _ = run_runner(tmp_path, calls)
    assert code == 0 and completion["status"] == "succeeded"
    assert progress["seq"] == 12 and progress["percent"] == 5 and len(progress["phase"]) == 40
    assert progress["message"] == "late "


def test_zero_callbacks_with_valid_outputs_succeeds(tmp_path):
    code, completion, progress, folder = run_runner(tmp_path, REPORT)
    assert code == 0 and completion["status"] == "succeeded" and (folder / "outputs/report.md").exists()
    assert not (folder / "outputs/citations.ris").exists()  # Empty exporter output writes no RIS.


def test_empty_report_and_exception(tmp_path):
    code, completion, _, _ = run_runner(tmp_path, "return {'formatted_findings': 'Error: x', 'sources_count': 0}")
    assert code == 3 and completion == {"status": "failed", "reason": "no_report"}
    code, completion, _, _ = run_runner(tmp_path / "b", "raise KeyError('secret')")
    assert code == 1 and completion == {"status": "failed", "reason": "KeyError"}


def test_exporter_error_is_recorded_not_fatal(tmp_path):
    code, completion, _, folder = run_runner(tmp_path, REPORT, "raise ValueError('x')")
    assert code == 0 and json.loads((folder / "result.json").read_text())["ris_error"] == "ValueError"


def test_restart_with_verified_ldr_output_reuses_without_duplicates(hub):
    run, _ = hub.start("literature_review")
    scope_to_brief(hub, run)
    data = hub.settle(run, {"awaiting_papers"})
    ldr = next(step for step in data["steps"] if step["step_key"] == "ldr_discovery")
    first = sorted(output["id"] for output in ldr["outputs"])
    # Simulate a hub crash after publication but before advance(): the step is still "running".
    with hub.projects.connect(write=True) as db:
        db.execute("DELETE FROM pipeline_step WHERE run_id=? AND position>?", (run["id"], ldr["position"]))
        db.execute("UPDATE pipeline_step SET state='running', finished_at=NULL WHERE id=?", (ldr["id"],))
        db.execute("UPDATE pipeline_run SET state='running' WHERE id=?", (run["id"],))
    from rah.pipelines import Pipelines
    restarted = Pipelines(hub.root, hub.book, hub.projects, hub.jobs, hub.runs)
    hub.pipes.close()
    hub.pipes = restarted
    data = hub.get(run)
    assert data["state"] == "interrupted" and "reuse_output" in data["actions"]
    hub.act(run, "reuse_output", {"digest": data["recovery"]["digest"]})
    data = hub.settle(run, {"awaiting_papers"})
    ldr = next(step for step in data["steps"] if step["step_key"] == "ldr_discovery")
    assert sorted(output["id"] for output in ldr["outputs"]) == first and ldr["state"] == "succeeded"
    assert ldr["usage"]["reason"] == "interrupted"


def test_restart_without_success_offers_rerun_only(hub):
    hub.ldr_mode = "crash"
    run, _ = hub.start("literature_review")
    scope_to_brief(hub, run)
    data = hub.settle(run, {"paused", "failed", "awaiting_papers"})
    ldr = next(step for step in data["steps"] if step["step_key"] == "ldr_discovery")
    with hub.projects.connect(write=True) as db:
        db.execute("UPDATE pipeline_step SET state='running', finished_at=NULL, error_code=NULL WHERE id=?", (ldr["id"],))
        db.execute("UPDATE pipeline_run SET state='running' WHERE id=?", (run["id"],))
    (hub.root / "hub/runs/guided" / ldr["id"] / "process.json").unlink()
    from rah.pipelines import Pipelines
    restarted = Pipelines(hub.root, hub.book, hub.projects, hub.jobs, hub.runs)
    hub.pipes.close()
    hub.pipes = restarted
    data = hub.get(run)
    assert data["state"] == "interrupted" and "reuse_output" not in data["actions"] and "rerun" in data["actions"]
