import json
import os
from pathlib import Path
import sys
from threading import Thread
from time import monotonic, sleep

import pytest

from rah import gptr_pipeline
from rah.common import Problem
from rah.jobs import Job


FAKE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
mode = os.environ["FAKE_MODE"]
Path("argv.json").write_text(json.dumps(sys.argv[1:]))
Path("env.json").write_text(json.dumps({k: os.environ.get(k) for k in ("FAST_LLM", "SMART_LLM", "STRATEGIC_LLM", "EMBEDDING",
    "MAX_ITERATIONS", "LLM_KWARGS", "DOC_PATH", "IMAGE_GENERATION_ENABLED", "MCP_STRATEGY", "OPENAI_API_KEY", "PYTHON_DOTENV_DISABLED")}))
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time, pathlib; pathlib.Path('grandchild.pid').write_text(str(__import__('os').getpid())); time.sleep(60)"])
    time.sleep(60)
if mode == "fail":
    print("openai.AuthenticationError: 401 invalid api key sk-or-secret", file=sys.stderr)
    sys.exit(3)
Path("outputs").mkdir(exist_ok=True)
count = {"ok": "4", "zero": "0", "bad": "lots"}.get(mode, "4")
Path("outputs/report.md").write_text('---\ntitle: "Survey"\nsources_count: ' + count + '\ntotal_cost_usd: 0.0012\n---\n# Body\n', encoding="utf-8")
if mode == "two":
    Path("outputs/second.md").write_text("x")
'''


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "cli.py"
    script.write_text(FAKE)
    return script


def stored(**extra):
    return {"RETRIEVER": "pubmed_central", "OPENAI_API_KEY": "native-openai", "TAVILY_API_KEY": "unused"} | extra


def run(tmp_path, fake, mode, *, deadline=20, job=None):
    env = gptr_pipeline.guided_projection(stored(), "or-key", "survey", "standard", tmp_path / "attempt")
    gptr_pipeline.validate_env(env)
    env["FAKE_MODE"] = mode
    argv = [sys.executable, str(fake), "--report_type", "research_report", "--report_source", "web", "--no-pdf", "--no-docx", "--", "Q -- x"]
    return gptr_pipeline.run(tmp_path / "attempt", argv, env, deadline=deadline, job=job)


def test_projection_is_sealed_and_survey_capped(tmp_path, fake):
    result = run(tmp_path, fake, "ok")
    attempt = tmp_path / "attempt"
    env = json.loads((attempt / "env.json").read_text())
    assert env["FAST_LLM"] == env["SMART_LLM"] == env["STRATEGIC_LLM"] == "openrouter:deepseek/deepseek-v4.1-flash-1017"
    assert env["EMBEDDING"] == "openrouter:openai/text-embedding-3-small"
    assert env["MAX_ITERATIONS"] == "1" and json.loads(env["LLM_KWARGS"]) == {"max_tokens": 2048, "max_retries": 0}
    assert env["OPENAI_API_KEY"] is None and env["IMAGE_GENERATION_ENABLED"] == "False" and env["MCP_STRATEGY"] == "disabled"
    assert env["PYTHON_DOTENV_DISABLED"] == "1" and Path(env["DOC_PATH"]).is_dir() and not any(Path(env["DOC_PATH"]).iterdir())
    assert json.loads((attempt / "argv.json").read_text())[-2:] == ["--", "Q -- x"]
    assert result["sources_count"] == 4 and result["title"] == "Survey" and result["total_cost_usd"] == "0.0012"
    assert json.loads((attempt / "completion.json").read_text())["outputs"][0]["sha256"] == result["sha256"]
    downstream = gptr_pipeline.guided_projection(stored(), "k", "deep", "premium", tmp_path / "deep")
    assert downstream["SMART_LLM"] == "openrouter:openai/gpt-6.2-sol" and "MAX_ITERATIONS" not in downstream


@pytest.mark.parametrize("extra,code", [({"CONFIG_PATH": "x.json"}, "native_configuration"),
                                        ({"OPENAI_BASE_URL": "https://x"}, "native_configuration"),
                                        ({"RETRIEVER": "tavily,searx"}, "unsupported_retriever"),
                                        ({"RETRIEVER": "bing"}, "unsupported_retriever"),
                                        ({"RETRIEVER": "searx", "SEARX_URL": "https://remote.test"}, "search_configuration")])
def test_projection_rejects_unsupported_configuration(tmp_path, extra, code):
    with pytest.raises(Problem) as error:
        gptr_pipeline.guided_projection(stored(**extra), "k", "survey", "standard", tmp_path)
    assert error.value.code == code


def test_validate_env_rejects_extra_secret(tmp_path):
    env = gptr_pipeline.guided_projection(stored(), "k", "survey", "standard", tmp_path)
    with pytest.raises(Problem):
        gptr_pipeline.validate_env(env | {"ANTHROPIC_API_KEY": "x"})
    with pytest.raises(Problem):
        gptr_pipeline.validate_env(env | {"SMART_LLM": "openrouter:other/model"})


@pytest.mark.parametrize("mode,count", [("zero", 0), ("bad", None)])
def test_source_counts_are_never_assumed(tmp_path, fake, mode, count):
    assert run(tmp_path, fake, mode)["sources_count"] == count


def test_failure_maps_to_safe_message(tmp_path, fake):
    with pytest.raises(Problem) as error:
        run(tmp_path, fake, "fail")
    assert error.value.code == "authorization_failed" and "sk-or" not in str(error.value)
    assert json.loads((tmp_path / "attempt/completion.json").read_text())["status"] == "failed"


def test_two_outputs_are_rejected(tmp_path, fake):
    with pytest.raises(Problem) as error:
        run(tmp_path, fake, "two")
    assert error.value.code == "native_output"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split()[2] != "Z"
    except FileNotFoundError:
        return True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group check")
def test_timeout_kills_whole_process_group(tmp_path, fake):
    started = monotonic()
    with pytest.raises(Problem) as error:
        run(tmp_path, fake, "hang", deadline=2)
    assert error.value.code == "stage_timeout" and monotonic() - started < 10
    pid = int((tmp_path / "attempt/grandchild.pid").read_text())
    deadline = monotonic() + 5
    while alive(pid) and monotonic() < deadline:
        sleep(0.05)
    assert not alive(pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group check")
def test_cancel_stops_child_tree(tmp_path, fake):
    job = Job("op", "gptr", state="Running")

    def cancel():
        while not (tmp_path / "attempt/grandchild.pid").exists():
            sleep(0.05)
        job.cancel()

    thread = Thread(target=cancel)
    thread.start()
    with pytest.raises(Problem) as error:
        run(tmp_path, fake, "hang", job=job)
    thread.join()
    assert error.value.code == "cancelled"
    pid = int((tmp_path / "attempt/grandchild.pid").read_text())
    deadline = monotonic() + 5
    while alive(pid) and monotonic() < deadline:
        sleep(0.05)
    assert not alive(pid)
    assert json.loads((tmp_path / "attempt/completion.json").read_text())["status"] == "cancelled"
