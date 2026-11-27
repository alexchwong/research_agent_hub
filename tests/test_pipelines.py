import io
import json
from pathlib import Path
import sys
from time import monotonic, sleep
from unittest.mock import Mock
from uuid import uuid4

import pytest

from rah import pqa_profiles, scope_gate, settings, upstreams
from rah.common import Problem, atomic, digest, prepare_root, private_directory, venv_python, write_json
from rah.installer import RouteBook
from rah.jobs import Jobs
from rah.pipelines import Pipelines, StaleRevision
from rah.projects import Projects
from rah.runs import Runs
from fixtures import text_pdf


FAKE_CLI = r'''
import json, os, sys, time
from pathlib import Path
Path("argv.json").write_text(json.dumps(sys.argv[1:]))
Path("env.json").write_text(json.dumps({k: os.environ.get(k) for k in ("SMART_LLM", "MAX_ITERATIONS", "OPENAI_API_KEY")}))
behaviour = Path(os.environ["DOC_PATH"]).parent.parent.parent / "behaviour.txt"
mode = behaviour.read_text().strip() if behaviour.exists() else "ok"
if mode == "hang":
    time.sleep(60)
count = "0" if mode == "zero" else "3"
query = sys.argv[-1]
Path("outputs").mkdir(exist_ok=True)
body = "# Report\n\nFindings about " + query.splitlines()[0] + "\n\n## References\n\n1. Trial A. doi:10.1000/abc PMID: 12345\n2. Review B https://pmc.ncbi.nlm.nih.gov/articles/PMC777/\n"
Path("outputs/out.md").write_text('---\ntitle: "T"\nsources_count: ' + count + '\n---\n' + body, encoding="utf-8")
'''
QUESTIONS = json.dumps({"questions": [
    {"question": "Which population?", "selection": "single", "options": ["Adults", "Children"]},
    {"question": "Which outcomes?", "selection": "multiple", "options": ["Survival", "Toxicity", "Cost"]}]})
RIS = ("TY  - ELEC\nTI  - Public trial\nDO  - 10.1000/xyz\nUR  - https://pubmed.ncbi.nlm.nih.gov/555/\nER  - \n"
       "TY  - ELEC\nTI  - PMC paper\nUR  - https://pmc.ncbi.nlm.nih.gov/articles/PMC888/\nER  - \n")


def private_path(root, relative):
    path = root
    for part in Path(relative).parts:
        path = private_directory(path / part)
    return path


class Hub:
    def __init__(self, tmp_path, monkeypatch):
        self.root = prepare_root(tmp_path.resolve() / "data")
        self.install = private_path(self.root, "apps/gptr/versions/fake")
        python = venv_python(self.install / "venv")
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        (self.install / "source").mkdir()
        (self.install / "source/cli.py").write_text(FAKE_CLI)
        ldr_install = private_path(self.root, "apps/ldr/versions/fake")
        real = RouteBook.load()
        rows = {"gptr": {"upstream_version": "3.8.0", "lock_sha256": "a" * 64, "lock_path": "x"},
                "ldr": {"upstream_version": "2.0.1", "lock_sha256": "b" * 64, "lock_path": "y", "structured_citations": "ldr-ris-v1"},
                "pqa": None}
        self.book = Mock()
        self.book.route.side_effect = lambda app: rows[app]
        self.book.capabilities.side_effect = real.capabilities
        self.book.custom = False
        monkeypatch.setattr(upstreams, "current", lambda root, app, book: self.install if app == "gptr" else ldr_install)
        self.write_config("RAH_OPENROUTER_API_KEY=offline-key\n")
        self.projects = Projects(self.root)
        self.jobs = Jobs()
        self.runs = Runs(self.root, self.book, self.projects, self.jobs)
        self.gate = Mock(return_value=(QUESTIONS, {"model": scope_gate.MODEL, "provider": None, "prompt_tokens": 1,
                                                   "completion_tokens": 1, "cost": None}))
        monkeypatch.setattr(scope_gate, "call", self.gate)
        self.pipes = Pipelines(self.root, self.book, self.projects, self.jobs, self.runs)
        self.native = Mock()
        self.native.ldr_process.return_value = {"generation": "g1", "stale": False}
        self.native.open_ldr.return_value = {"opened": True, "prefill_prepared": True}
        self.pipes.native = self.native
        self.chat = self.projects.create_chat(None)["id"]
        self.scope = self.chat

    def write_config(self, text):
        atomic(self.root / "hub/config/.env", text.encode())
        config = settings.load(self.root)
        settings.install_projection(self.root, "gptr", settings.resolve(config, "gptr", RouteBook.load().capabilities("gptr")), True)

    def start(self, purpose, question="Effects of drug X on outcome Y?", profile="standard"):
        preview = self.pipes.preview(self.scope, self.chat, {"purpose": purpose, "profile": profile, "question": question})
        run = self.pipes.create(self.scope, self.chat, {"request_id": str(uuid4()), "purpose": purpose, "profile": profile,
                                                         "question": question, "confirmation_digest": preview["digest"],
                                                         "public_confirmed": True})
        return run, preview

    def get(self, run):
        return self.pipes.get(self.scope, self.chat, run["id"])

    def settle(self, run, until, timeout=20):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            self.pipes.tick()
            data = self.get(run)
            if data["state"] in until:
                return data
            sleep(0.05)
        raise AssertionError(f"run stuck in {self.get(run)['state']}")

    def act(self, run, action, data=None, step=None):
        current = self.get(run)
        result, status = self.pipes.act(self.scope, self.chat, run["id"], {
            "action": action, "action_id": str(uuid4()), "expected_revision": current["revision"],
            "step_id": step, "data": data or {}})
        return result, status

    def attempt_file(self, run, key, name):
        step = [item for item in self.get(run)["steps"] if item["step_key"] == key][-1]
        return json.loads((self.root / "hub/runs/guided" / step["id"] / name).read_text())


@pytest.fixture
def hub(tmp_path, monkeypatch):
    instance = Hub(tmp_path, monkeypatch)
    yield instance
    instance.pipes.close()


def scope_to_brief(hub, run, broad=False):
    data = hub.settle(run, {"awaiting_scope"})
    questions = data["questionnaires"][0]["questions"]
    answers = {} if broad else {questions[0]["id"]: {"selected": [questions[0]["options"][0]["id"]]},
                                questions[1]["id"]: {"selected": [questions[1]["options"][0]["id"], questions[1]["options"][1]["id"]]}}
    preview, _ = hub.act(run, "preview_scope", {"answers": answers, "instructions": "", "broad": broad})
    if not broad:
        with pytest.raises(Problem):
            hub.act(run, "approve_scope", {"digest": preview["digest"]})
    result, _ = hub.act(run, "approve_scope", {"digest": preview["digest"], "constraints_reviewed": True})
    return preview, result


def test_rapid_research_end_to_end(hub):
    run, preview = hub.start("rapid_research")
    assert preview["survey"]["models"]["SMART_LLM"] == "deepseek/deepseek-v4.1-flash-1017"
    assert preview["scope_gate"]["model"] == "anthropic/claude-haiku-5.5"
    assert run["brief_revision"] == 1 and run["state"] == "queued"
    data = hub.settle(run, {"awaiting_scope"})
    assert hub.attempt_file(run, "survey", "env.json")["MAX_ITERATIONS"] == "1"
    assert hub.attempt_file(run, "survey", "env.json")["OPENAI_API_KEY"] is None
    body = hub.gate.call_args.args[1]
    sent = json.loads(body["messages"][1]["content"])
    assert sent["topic"] == "Effects of drug X on outcome Y?" and "sources_count" not in sent["survey"] and "https" not in sent["survey"]
    preview, result = scope_to_brief(hub, run)
    assert preview["prompt"] == ("Effects of drug X on outcome Y?\n\nApproved scope constraints:\n"
                                 "- Which population? — Adults\n- Which outcomes? — Survival; Toxicity")
    data = hub.settle(run, {"succeeded"})
    assert data["outcome"] == "complete"
    argv = hub.attempt_file(run, "gptr_rapid", "argv.json")
    assert argv[:2] == ["--report_type", "research_report"] and argv[-1] == preview["prompt"]
    assert hub.attempt_file(run, "gptr_rapid", "env.json")["SMART_LLM"] == "openrouter:openai/gpt-6.1-terra"
    roles = [output["role"] for step in data["steps"] for output in step["outputs"]]
    assert roles == ["survey", "scope_questionnaire", "research_report"]
    assert data["brief"]["profile_snapshot"]["models"]["gptr_smart"] == "openai/gpt-6.1-terra"
    chat = hub.projects.get_chat(hub.scope, hub.chat)
    assert {entry["state"] for entry in chat["entries"]} == {"Complete", "Awaiting scope"} or all(
        entry["state"] in ("Complete",) for entry in chat["entries"][:1])


def test_broad_scope_sends_original_question_unchanged(hub):
    question = "  Public topic with spacing\nand a second line  "
    run, _ = hub.start("rapid_research", question)
    preview, _ = scope_to_brief(hub, run, broad=True)
    assert preview["prompt"] == question
    hub.settle(run, {"succeeded"})
    assert hub.attempt_file(run, "gptr_rapid", "argv.json")[-1] == question


def test_survey_as_result_and_no_sources(hub):
    run, _ = hub.start("rapid_research")
    hub.settle(run, {"awaiting_scope"})
    result, _ = hub.act(run, "use_survey_result")
    assert result["state"] == "succeeded" and result["outcome"] == "survey_as_result"
    other = hub.projects.create_chat(None)["id"]
    hub.chat = hub.scope = other
    (hub.root / "hub/runs/behaviour.txt").write_text("zero")
    run, _ = hub.start("deep_research")
    data = hub.settle(run, {"paused"})
    survey = data["steps"][0]
    assert survey["state"] == "failed" and survey["error_code"] == "no_sources"
    assert survey["outputs"][0]["title"] == "No sources retrieved" and len(data["steps"]) == 1
    assert "rerun" in data["actions"]


def test_scope_gate_failure_uses_editable_fallback(hub):
    hub.gate.side_effect = Problem("invalid_response", "bad", 503)
    run, _ = hub.start("rapid_research")
    data = hub.settle(run, {"awaiting_scope"})
    round_one = data["questionnaires"][0]
    assert round_one["source"] == "fallback" and round_one["error"]["code"] == "invalid_response"
    assert [item["question"] for item in round_one["questions"]] == ["Which evidence should we focus on?", "Which aspect matters most?"]
    assert hub.gate.call_count == 1


def test_second_round_is_explicit_and_separate(hub):
    run, _ = hub.start("rapid_research")
    data = hub.settle(run, {"awaiting_scope"})
    first = data["questionnaires"][0]["questions"][0]
    hub.act(run, "ask_more", {"round_id": "r1", "answers": {first["id"]: {"selected": [first["options"][0]["id"]]}}})
    data = hub.settle(run, {"awaiting_scope"})
    assert [item["round"] for item in data["questionnaires"]] == [1, 2]
    second_body = json.loads(hub.gate.call_args.args[1]["messages"][1]["content"])
    assert second_body["answers"] == [{"question": "Which population?", "selected": ["Adults"], "custom": ""}]
    assert "ask_more" not in data["actions"]
    scope_steps = [step for step in data["steps"] if step["step_key"] == "scope_gate"]
    assert [step["attempt"] for step in scope_steps] == [1, 2] and scope_steps[0]["state"] == "succeeded"


def test_route_change_requires_reconfirmation(hub):
    run, _ = hub.start("rapid_research")
    hub.write_config("RAH_OPENROUTER_API_KEY=rotated-key\n")
    data = hub.settle(run, {"reconfirm_recipients"})
    step = data["steps"][-1]
    assert step["step_key"] == "survey" and "changed" in step["error_detail"]
    current = hub.get(run)
    assert current["reconfirm"]["route"]["models"]["FAST_LLM"] == "deepseek/deepseek-v4.1-flash-1017"
    hub.act(run, "reconfirm_route", {"digest": current["reconfirm"]["digest"]})
    # The Scope Gate uses the same rotated credential, so it needs its own re-confirmation before its request.
    data = hub.settle(run, {"reconfirm_recipients"})
    assert data["steps"][-1]["step_key"] == "scope_gate" and hub.gate.call_count == 0
    hub.act(run, "reconfirm_route", {"digest": hub.get(run)["reconfirm"]["digest"]})
    hub.settle(run, {"awaiting_scope"})
    assert hub.gate.call_count == 1


def test_process_restart_requires_route_review(hub):
    run, _ = hub.start("rapid_research")
    hub.pipes.generation = "restarted"
    data = hub.settle(run, {"reconfirm_recipients"})
    assert "restarted" in data["steps"][-1]["error_detail"]


def test_request_replay_stale_revision_and_action_replay(hub):
    preview = hub.pipes.preview(hub.scope, hub.chat, {"purpose": "rapid_research", "profile": "budget", "question": "Q?"})
    request = {"request_id": str(uuid4()), "purpose": "rapid_research", "profile": "budget", "question": "Q?",
               "confirmation_digest": preview["digest"], "public_confirmed": True}
    first = hub.pipes.create(hub.scope, hub.chat, request)
    again = hub.pipes.create(hub.scope, hub.chat, request)
    assert again["replay"] and again["id"] == first["id"]
    with pytest.raises(Problem) as changed:
        hub.pipes.create(hub.scope, hub.chat, request | {"question": "Other?"})
    assert changed.value.status == 409
    with pytest.raises(Problem) as busy:
        hub.start("rapid_research")
    assert busy.value.code in ("chat_busy", "stale_preview")
    hub.settle(first, {"awaiting_scope"})
    with pytest.raises(StaleRevision):
        hub.pipes.act(hub.scope, hub.chat, first["id"], {"action": "pause", "action_id": str(uuid4()), "expected_revision": 0, "data": {}})
    action = {"action": "pause", "action_id": str(uuid4()), "expected_revision": hub.get(first)["revision"], "data": {}}
    paused, _ = hub.pipes.act(hub.scope, hub.chat, first["id"], action)
    replay, _ = hub.pipes.act(hub.scope, hub.chat, first["id"], action)
    assert replay["replay"] and replay["state"] == paused["state"] == "paused"


def test_legacy_and_guided_share_one_active_managed_request(hub):
    run, _ = hub.start("rapid_research")
    with pytest.raises(Problem) as legacy:
        hub.projects.accept(None, hub.chat, str(uuid4()), "gptr", "managed", "research_report", "Q", "", "t", [], lambda: None)
    assert legacy.value.code == "chat_busy"
    with pytest.raises(Problem) as trash:
        hub.projects.trash_chat(hub.chat)
    assert trash.value.code == "chat_busy"
    hub.settle(run, {"awaiting_scope"})
    hub.act(run, "cancel")
    assert hub.get(run)["state"] == "cancelled"
    hub.projects.trash_chat(hub.chat)


def test_cancel_running_survey_kills_child(hub):
    (hub.root / "hub/runs/behaviour.txt").write_text("hang")
    run, _ = hub.start("rapid_research")
    hub.settle(run, {"surveying"})
    hub.act(run, "cancel")
    data = hub.settle(run, {"cancelled"})
    deadline = monotonic() + 10
    while data["steps"][0]["state"] != "cancelled" and monotonic() < deadline:
        sleep(0.1)
        data = hub.get(run)
    assert data["steps"][0]["state"] == "cancelled"
    assert json.loads((hub.root / "hub/runs/guided" / data["steps"][0]["id"] / "completion.json").read_text())["status"] == "cancelled"


def test_restart_interrupts_and_reuses_verified_output(hub):
    run, _ = hub.start("rapid_research")
    data = hub.settle(run, {"awaiting_scope"})
    survey = data["steps"][0]
    # Simulate a crash after a verified native completion but before registration of a later attempt.
    with hub.projects.connect(write=True) as db:
        db.execute("DELETE FROM artifact_parent")
        db.execute("DELETE FROM artifact WHERE step_id IN (SELECT id FROM pipeline_step WHERE run_id=?)", (run["id"],))
        entries = [row[0] for row in db.execute("SELECT entry_id FROM pipeline_step WHERE run_id=? AND position=1", (run["id"],))]
        db.execute("DELETE FROM pipeline_step WHERE run_id=? AND position=1", (run["id"],))
        db.executemany("DELETE FROM chat_entry WHERE id=?", [(entry,) for entry in entries])
        db.execute("UPDATE pipeline_step SET state='surveying' WHERE id=?", (survey["id"],))
        db.execute("UPDATE pipeline_run SET state='surveying' WHERE id=?", (run["id"],))
    restarted = Pipelines(hub.root, hub.book, hub.projects, hub.jobs, hub.runs)
    hub.pipes.close()
    hub.pipes = restarted
    data = hub.get(run)
    assert data["state"] == "interrupted" and "reuse_output" in data["actions"] and data["recovery"]["outputs"]
    hub.pipes.generation = restarted.generation
    hub.act(run, "reuse_output", {"digest": data["recovery"]["digest"]})
    assert hub.get(run)["steps"][0]["state"] == "succeeded"


def literature_to_papers(hub, run):
    scope_to_brief(hub, run)
    data = hub.settle(run, {"waiting_native"})
    current = data["steps"][-1]
    assert data["native_route"]["attestation"].startswith("Native UI overrides are unobserved")
    hub.act(run, "launch_native", {"route_digest": data["native_route"]["digest"], "attest": True})
    info = hub.native.open_ldr.call_args.args[0]
    assert info["prompt"] == data["native_route"]["prompt"] and info["step"] == current["id"]
    result_id = str(uuid4())
    report = hub.pipes.native_export(info, "md", b"# LDR report", result_id)
    with pytest.raises(Problem):
        hub.pipes.native_export(info, "ris", RIS.encode(), str(uuid4()))
    with pytest.raises(Problem):
        hub.act(run, "confirm_native_result", {"artifact_ids": [report], "attribution": True, "public_provenance": True})
    ris = hub.pipes.native_export(info, "ris", RIS.encode(), result_id)
    assert hub.pipes.native_export(info, "ris", RIS.encode(), result_id) == ris
    with pytest.raises(Problem):
        hub.pipes.native_export(info, "ris", RIS.encode() + b"TY  - ELEC\nTI  - X\nER  - \n", result_id)
    hub.act(run, "confirm_native_result", {"artifact_ids": [report, ris], "attribution": True, "public_provenance": True})
    return hub.get(run), ris


def test_literature_review_native_handoff_papers_and_unavailable_pdf_route(hub):
    run, _ = hub.start("literature_review")
    data, ris = literature_to_papers(hub, run)
    assert data["state"] == "awaiting_papers"
    papers = data["papers"]
    assert papers["source_artifact_id"] == ris
    assert [row.get("pmcid") for row in papers["suggestions"]] == [None, "PMC888"] and papers["suggestions"][0]["pmid"] == "555"
    with pytest.raises(Problem):
        hub.act(run, "finish_partial")
    rows = [dict(item, accept=True, suggestion=index) for index, item in enumerate(papers["suggestions"])]
    rows[1]["doi"] = "10.9999/typed"  # A typed identifier never becomes public.
    hub.act(run, "set_papers", {"source_artifact_id": ris, "rows": rows})
    saved = hub.get(run)["papers"]["rows"]
    assert [row["origin_class"] for row in saved] == ["public_native", "private_or_unknown"]
    pdf = hub.projects.register(hub.scope, io.BytesIO(text_pdf()), "trial.pdf", "pdf", chat=hub.chat)
    with pytest.raises(Problem):
        hub.act(run, "select_corpus", {"pdf_ids": [pdf], "analysis_question": "What?", "route_id": "pqa-budget-v1"})
    hub.act(run, "select_corpus", {"pdf_ids": [pdf], "analysis_question": "What did the trial find?", "excerpt": "",
                                   "route_id": "pqa-standard-v1"})
    data = hub.get(run)
    assert data["state"] == "paused" and data["steps"][-1]["error_code"] in ("unverified_profile", "profile_unavailable")
    assert "finish_partial" in data["actions"]
    result, _ = hub.act(run, "finish_partial")
    assert result["outcome"] == "discovery_only" and result["state"] == "succeeded"


def test_corroboration_keeps_two_originals(hub):
    run, _ = hub.start("independent_corroboration")
    scope_to_brief(hub, run)
    data = hub.settle(run, {"waiting_native"})
    hub.act(run, "launch_native", {"route_digest": data["native_route"]["digest"], "attest": True})
    info = hub.native.open_ldr.call_args.args[0]
    report = hub.pipes.native_export(info, "md", b"# LDR comparison", str(uuid4()))
    hub.act(run, "confirm_native_result", {"artifact_ids": [report], "attribution": True, "public_provenance": True})
    data = hub.settle(run, {"succeeded"})
    outputs = {step["step_key"]: step["outputs"] for step in data["steps"]}
    assert outputs["ldr_comparison"][0]["producer"] == "Local Deep Research"
    assert outputs["gptr_comparison"][0]["role"] == "research_report"
    assert data["brief"]["public_prompts"]["ldr_comparison"] == data["brief"]["public_prompts"]["gptr_comparison"]


def enable_route(hub, monkeypatch):
    lock = hub.root / "pqa.lock"
    atomic(lock, b"lock")
    lock_hash = digest(b"lock")
    install_id = str(uuid4())
    python = venv_python(private_path(hub.root, "apps/pqa/versions/" + install_id) / "venv")
    python.parent.mkdir(parents=True)
    python.touch()
    write_json(hub.root / "apps/pqa/current.json", {"install_id": install_id, "lock_sha256": lock_hash, "upstream_version": "fixture"})
    row = {"upstream_version": "fixture", "lock_sha256": lock_hash, "lock_path": lock.name}
    original = hub.book.route.side_effect
    hub.book.route.side_effect = lambda app: row if app == "pqa" else original(app)
    hub.book.directory = hub.root
    entry = {**pqa_profiles.host_identity(), "upstream_version": "fixture", "lock_sha256": lock_hash,
             "worker_sha256": pqa_profiles.worker_digest(), "providers": {role: "P-" + role for role in pqa_profiles.PAPER_ROLES},
             "evidence_sha256": "f" * 64}
    monkeypatch.setattr(pqa_profiles, "REGISTRY", dict(pqa_profiles.REGISTRY) | {
        "pqa-standard-v1": dict(pqa_profiles.REGISTRY["pqa-standard-v1"]) | {"enabled": (entry,)}})


def test_analyse_my_papers_private_question_only_at_paperqa_stage(hub, monkeypatch):
    enable_route(hub, monkeypatch)
    run, _ = hub.start("analyse_my_papers", "Public topic: anticoagulants")
    scope_to_brief(hub, run, broad=True)
    data = hub.get(run)
    assert data["state"] == "awaiting_papers" and data["brief"]["public_prompts"] == {}
    pdf = hub.projects.register(hub.scope, io.BytesIO(text_pdf()), "private notes.pdf", "pdf", chat=hub.chat)
    hub.act(run, "select_corpus", {"pdf_ids": [pdf], "analysis_question": "PRIVATE: compare doses in my notes",
                                   "excerpt": "secret excerpt", "route_id": "pqa-standard-v1"})
    data = hub.get(run)
    step = data["steps"][-1]
    assert data["state"] == "awaiting_disclosure"
    survey_argv = hub.attempt_file(run, "survey", "argv.json")
    assert "PRIVATE" not in json.dumps(survey_argv) and "private notes" not in json.dumps(hub.gate.call_args.args[1])
    entry = hub.projects.entry(hub.scope, step["entry_id"])
    assert entry["question"].startswith("PRIVATE") and entry["input_ids"] == [pdf]
    review = hub.runs.preview(hub.scope, [pdf], entry["question"], entry=entry["id"], quote=entry["quote"], quality="standard", guided=step["id"])
    assert {row["model"] for row in review["profile"]["destinations"]} == {"anthropic/claude-haiku-5.5", "openai/gpt-6.1-terra",
                                                                           "openai/text-embedding-3-small"}

    def worker(folder, interpreter, env, payload, record, *, job):
        assert payload["question"] == "PRIVATE: compare doses in my notes\n\nUser-selected excerpt:\nsecret excerpt"
        assert [path.name for path in (folder / "corpus").iterdir()] == [pdf + ".pdf"]
        return hub.runs.guided.paper_result(record, {"answer": "Cited answer (Doe 2020).", "bib": "@article{doe}",
                                                     "coverage": {"submitted": [pdf + ".pdf"], "cited": [pdf + ".pdf"], "reviewed": None}}, folder.name)

    monkeypatch.setattr(hub.runs, "invoke", worker)
    operation = hub.runs.approve(hub.scope, review["preview_id"], review["binding"], True)
    assert operation
    data = hub.settle(run, {"succeeded"})
    final = data["steps"][-1]
    assert [output["role"] for output in final["outputs"]] == ["answer", "references"]
    assert final["detail"]["coverage"]["cited"] == ["private notes.pdf"] and final["detail"]["coverage"]["reviewed"] is None
    with hub.projects.connect() as db:
        assert [row[0] for row in db.execute("SELECT parent_id FROM artifact_parent WHERE child_id=?", (final["outputs"][0]["id"],))] == [pdf]
        assert db.execute("SELECT decision FROM pipeline_approval WHERE kind='pqa_review'").fetchone()[0] == "approved"


def test_analyse_my_papers_decline_cannot_complete(hub, monkeypatch):
    enable_route(hub, monkeypatch)
    run, _ = hub.start("analyse_my_papers", "Public topic")
    scope_to_brief(hub, run, broad=True)
    pdf = hub.projects.register(hub.scope, io.BytesIO(text_pdf()), "a.pdf", "pdf", chat=hub.chat)
    hub.act(run, "select_corpus", {"pdf_ids": [pdf], "analysis_question": "Q", "route_id": "pqa-standard-v1"})
    step = hub.get(run)["steps"][-1]
    entry = hub.projects.entry(hub.scope, step["entry_id"])
    review = hub.runs.preview(hub.scope, [pdf], "Q", entry=entry["id"], quality="standard", guided=step["id"])
    assert hub.runs.approve(hub.scope, review["preview_id"], review["binding"], False) is None
    data = hub.get(run)
    assert data["state"] == "awaiting_disclosure" and "finish_partial" not in data["actions"]


def test_trashed_chat_rejects_native_export(hub):
    run, _ = hub.start("literature_review")
    scope_to_brief(hub, run)
    data = hub.settle(run, {"waiting_native"})
    hub.act(run, "launch_native", {"route_digest": data["native_route"]["digest"], "attest": True})
    info = hub.native.open_ldr.call_args.args[0]
    hub.act(run, "cancel")
    with pytest.raises(Problem):
        hub.pipes.native_export(info, "ris", RIS.encode(), str(uuid4()))
    hub.native.revoke.assert_called_with(run["id"])


def test_deep_research_papers_automatic_oa_and_private_lookup(hub, monkeypatch):
    from rah import oa
    from test_oa import FakeTransport, listing, metadata
    hub.write_config("RAH_OPENROUTER_API_KEY=offline-key\nRAH_OA_AUTO=true\nRAH_OA_PMC_CONTACT_EMAIL=maintainer@example.org\n")
    pdf = text_pdf()
    transport = FakeTransport({
        oa.BUCKET + "?": lambda url: listing(1),
        oa.BUCKET + "PMC777.1/PMC777.1.json": (200, "application/json", json.dumps(metadata(doi=None, pmid=None)).encode()),
        oa.BUCKET + "PMC777.1/PMC777.1.pdf": (200, "application/pdf", pdf),
        oa.CONVERTER: (200, "application/json", json.dumps({"records": [{"doi": "10.1000/abc", "pmcid": "PMC777"}]}).encode())})
    hub.pipes.transport = transport
    monkeypatch.setattr(oa, "parse_check", lambda path, python: None)
    pqa = private_path(hub.root, "apps/pqa/versions/" + str(uuid4()))
    (venv_python(pqa / "venv")).parent.mkdir(parents=True)
    venv_python(pqa / "venv").touch()
    write_json(hub.root / "apps/pqa/current.json", {"install_id": pqa.name, "lock_sha256": "c" * 64, "upstream_version": "x"})
    run, _ = hub.start("deep_research")
    scope_to_brief(hub, run)
    data = hub.settle(run, {"awaiting_papers"})
    suggestions = data["papers"]["suggestions"]
    assert suggestions[0]["doi"] == "10.1000/abc" and suggestions[0]["pmid"] == "12345" and suggestions[1]["pmcid"] == "PMC777"
    rows = [dict(item, accept=True, suggestion=index) for index, item in enumerate(suggestions)]
    hub.act(run, "set_papers", {"source_artifact_id": data["papers"]["source_artifact_id"], "rows": rows})
    deadline = monotonic() + 10
    while monotonic() < deadline and sum(row["access_status"] == "oa_acquired" for row in hub.get(run)["papers"]["rows"]) < 2:
        sleep(0.05)
    papers = hub.get(run)["papers"]["rows"]
    acquired = [row for row in papers if row["access_status"] == "oa_acquired"]
    assert len(acquired) == 2 and {row["state"] for row in acquired} == {"needed"}
    assert {row["acquisitions"][-1]["method"] for row in acquired} == {"pmc-idconv", "pmc-direct"}
    assert acquired[0]["acquisitions"][-1]["licence"] == "CC BY"
    pdf_id = acquired[0]["acquisitions"][-1]["artifact_id"]
    assert sum(call.startswith(oa.CONVERTER) for call in transport.calls) == 1
    assert not any("Trial" in call or "Review" in call for call in transport.calls)
    # A typed private identifier never joins the automatic subset and needs its own review.
    manual = [{"doi": "10.1000/private", "accept": True}]
    hub.act(run, "set_papers", {"source_artifact_id": None, "rows": manual})
    private = next(row for row in hub.get(run)["papers"]["rows"] if row["doi"] == "10.1000/private")
    assert private["origin_class"] == "private_or_unknown" and not private["acquisitions"]
    with pytest.raises(Problem):
        hub.act(run, "start_acquisition", {"paper_ids": [private["id"]]})
    preview = hub.pipes.private_lookup_preview(hub.get(run), private)
    assert preview["stage"] == "converter" and "10.1000/private" in preview["sends"]
    hub.act(run, "approve_private_lookup", {"paper_id": private["id"], "stage": "converter", "digest": preview["digest"], "approve": False})
    hub.act(run, "select_corpus", {"pdf_ids": [pdf_id], "analysis_question": "Summarise", "route_id": "pqa-standard-v1"})
    rows = {row["id"]: row for row in hub.get(run)["papers"]["rows"]}
    assert rows[acquired[0]["id"]]["state"] == "supplied" and rows[acquired[0]["id"]]["pdf_artifact_id"] == pdf_id
    with hub.projects.connect() as db:
        origin = db.execute("SELECT origin_class, role FROM artifact WHERE id=?", (pdf_id,)).fetchone()
        assert tuple(origin) == ("public_acquired", "source_pdf")
        kinds = [row[0] for row in db.execute("SELECT kind || ':' || decision FROM pipeline_approval WHERE kind LIKE 'oa_%'")]
        assert kinds == ["oa_private:declined"]


def test_registry_ships_disabled_and_guided_runs_never_write_native_settings(hub):
    import hashlib
    from importlib import resources
    for key, route in pqa_profiles.REGISTRY.items():
        assert route["enabled"] == () and route["status"] == "candidate" and route["policy"] == "single-provider-no-fallback"
    assert pqa_profiles.worker_digest() == hashlib.sha256(resources.files("rah").joinpath("pqa_profile_worker.py").read_bytes()).hexdigest()
    manifest = json.loads(resources.files("rah").joinpath("install_assets/routes.json").read_text())
    assert "paper_profiles" not in manifest and "openrouter-text-v1" not in json.dumps(manifest)
    before = {path: path.read_bytes() for path in (hub.root / "hub/config/.env", hub.root / "apps/gptr/launch/profile.json")}
    run, _ = hub.start("rapid_research", profile="premium")
    scope_to_brief(hub, run, broad=True)
    hub.settle(run, {"succeeded"})
    assert {path: path.read_bytes() for path in before} == before
    assert hub.attempt_file(run, "gptr_rapid", "env.json")["SMART_LLM"] == "openrouter:openai/gpt-6.2-sol"


def test_missing_app_waits_without_dispatch_and_busy_resource_is_fifo(hub, monkeypatch):
    from rah.common import Problem as P
    run, _ = hub.start("rapid_research")
    original = upstreams.current

    def missing(root, app, book):
        raise P("app_not_installed", "Install this application at its manifest pin first.", 503)

    monkeypatch.setattr(upstreams, "current", missing)
    hub.pipes.tick()
    data = hub.get(run)
    assert data["state"] == "waiting_app" and "App details" in data["steps"][0]["error_detail"]
    monkeypatch.setattr(upstreams, "current", original)
    busy = hub.jobs.start("gptr", lambda job: sleep(1))
    hub.pipes.tick()
    assert hub.get(run)["state"] == "waiting_app"
    while hub.jobs.get(busy).active():
        sleep(0.05)
    hub.settle(run, {"awaiting_scope"})
