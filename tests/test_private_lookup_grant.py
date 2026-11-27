"""T13: private identifier lookups run only under an immutable per-paper grant, enforced before every request."""
import json
from uuid import uuid4
from time import monotonic, sleep

import pytest

from rah import oa
from rah.common import Problem, venv_python, write_json
from rah.jobs import Job
from test_oa import FakeTransport, listing, metadata
from test_pipelines import hub, private_path, scope_to_brief, text_pdf  # noqa: F401  (hub is a fixture)


def converter(doi="10.1000/private", pmcid="PMC777"):
    return (200, "application/json", json.dumps({"records": [{"doi": doi, "pmcid": pmcid}]}).encode())


@pytest.fixture
def papers(hub, monkeypatch):
    hub.write_config("RAH_OPENROUTER_API_KEY=offline-key\nRAH_OA_PMC_CONTACT_EMAIL=maintainer@example.org\n")
    monkeypatch.setattr(oa, "parse_check", lambda path, python: None)
    pqa = private_path(hub.root, "apps/pqa/versions/" + str(uuid4()))
    venv_python(pqa / "venv").parent.mkdir(parents=True)
    venv_python(pqa / "venv").touch()
    write_json(hub.root / "apps/pqa/current.json", {"install_id": pqa.name, "lock_sha256": "c" * 64, "upstream_version": "x"})
    run, _ = hub.start("deep_research")
    scope_to_brief(hub, run)
    data = hub.settle(run, {"awaiting_papers"})
    rows = [{"doi": "10.1000/private", "accept": True}] + [{"doi": f"10.1000/p{index}", "accept": True} for index in range(20)]
    hub.act(run, "set_papers", {"source_artifact_id": None, "rows": rows})
    return run


def routes(**overrides):
    table = {oa.CONVERTER: converter(),
             oa.BUCKET + "?": lambda url: listing(1),
             oa.BUCKET + "PMC777.1/PMC777.1.json": (200, "application/json", json.dumps(metadata(doi="10.1000/private", pmid=None)).encode()),
             oa.BUCKET + "PMC777.1/PMC777.1.pdf": (200, "application/pdf", text_pdf())}
    return table | overrides


def private_id(hub, run, doi="10.1000/private"):
    return next(row["id"] for row in hub.get(run)["papers"]["rows"] if row["doi"] == doi)


def approvals(hub):
    with hub.projects.connect() as db:
        return [tuple(row) for row in db.execute("SELECT kind, decision FROM pipeline_approval WHERE kind='oa_private'")]


def wait_idle(hub):
    deadline = monotonic() + 10
    while hub.jobs.busy("oa") and monotonic() < deadline:
        sleep(0.05)


def test_batch_caps_at_20_and_one_click_looks_up_only_the_granted_identifiers(hub, papers):
    run = papers
    batch = hub.get(run)["lookup_batch"]
    assert len(batch["rows"]) == 20 and len(batch["excluded"]) == 1 and batch["digest"]
    transport = FakeTransport(routes())
    hub.pipes.transport = transport
    paper = private_id(hub, run)
    hub.act(run, "approve_private_lookups", {"paper_ids": [paper], "digest": batch["digest"]})
    wait_idle(hub)
    assert approvals(hub) == [("oa_private", "approved")]
    assert sum(url.startswith(oa.CONVERTER) for url in transport.calls) == 1 and "10.1000%2Fprivate" in transport.calls[0]
    assert all(url.startswith(oa.CONVERTER) or "PMC777" in url for url in transport.calls)
    row = next(item for item in hub.get(run)["papers"]["rows"] if item["id"] == paper)
    assert row["access_status"] == "oa_acquired"
    progress = next(step for step in hub.get(run)["steps"] if step["step_key"] == "papers")["detail"]["acquisition"]
    assert progress == {"selected": 1, "processed": 1, "pending": 0, "skipped": 0, "failed": 0, "running": False}
    with pytest.raises(Problem) as retry:  # The batch digest is single-use: retries need a new approval.
        hub.act(run, "approve_private_lookups", {"paper_ids": [paper], "digest": batch["digest"]})
    assert retry.value.code == "stale_preview"


def test_busy_queue_writes_no_approvals(hub, papers):
    run = papers
    batch = hub.get(run)["lookup_batch"]
    blocker = hub.jobs.start("oa", lambda job: sleep(1))
    with pytest.raises(Problem) as busy:
        hub.act(run, "approve_private_lookups", {"paper_ids": [private_id(hub, run)], "digest": batch["digest"]})
    assert busy.value.status == 409 and approvals(hub) == []
    while hub.jobs.get(blocker).active():
        sleep(0.05)


def grant_for(hub, run, paper):
    batch = hub.get(run)["lookup_batch"]
    stored = hub.pipes.recall(batch["digest"], "lookup_batch")
    return {paper: stored["grants"][paper]}


def acquire(hub, run, paper, grants, transport):
    step = next(step for step in hub.get(run)["steps"] if step["step_key"] == "papers")
    return hub.pipes.acquire(Job("t", "oa", state="Running"), hub.scope, hub.chat, run["id"], step["id"], [paper], "both",
                             transport=transport, grants=grants)


@pytest.mark.parametrize("change", ["doi", "pmcid", "origin", "contact"])
def test_any_change_after_approval_sends_nothing(hub, papers, change):
    run = papers
    paper = private_id(hub, run)
    grants = grant_for(hub, run, paper)
    with hub.projects.connect(write=True) as db:
        if change == "doi":
            db.execute("UPDATE pipeline_paper SET doi='10.1000/other' WHERE id=?", (paper,))
        elif change == "pmcid":
            db.execute("UPDATE pipeline_paper SET pmcid='PMC999' WHERE id=?", (paper,))
        elif change == "origin":
            db.execute("UPDATE pipeline_paper SET origin_class='public_native' WHERE id=?", (paper,))
    if change == "contact":
        hub.write_config("RAH_OPENROUTER_API_KEY=offline-key\n")
    transport = FakeTransport(routes())
    result = acquire(hub, run, paper, grants, transport)
    assert transport.calls == []
    assert result["papers"][0]["outcome"] == "rejected" and result["papers"][0]["error"] == "changed_after_approval"


def test_change_between_requests_stops_the_next_request(hub, papers):
    run = papers
    paper = private_id(hub, run)
    grants = grant_for(hub, run, paper)

    def convert_then_change(url):
        with hub.projects.connect(write=True) as db:
            db.execute("UPDATE pipeline_paper SET doi='10.1000/other' WHERE id=?", (paper,))
        return converter()

    transport = FakeTransport(routes(**{oa.CONVERTER: convert_then_change}))
    result = acquire(hub, run, paper, grants, transport)
    assert len(transport.calls) == 1 and transport.calls[0].startswith(oa.CONVERTER)
    assert result["papers"][0]["outcome"] == "rejected"


def test_cloud_follow_up_only_for_the_pmcid_returned_for_the_approved_doi(hub, papers):
    run = papers
    paper = private_id(hub, run)
    grants = grant_for(hub, run, paper)
    transport = FakeTransport(routes(**{oa.CONVERTER: converter(doi="10.1000/someone-else")}))
    acquire(hub, run, paper, grants, transport)
    assert all(url.startswith(oa.CONVERTER) for url in transport.calls)


def test_conversion_failure_and_partial_counts(hub, papers):
    run = papers
    batch = hub.get(run)["lookup_batch"]
    ids = [row["paper_id"] for row in batch["rows"][:3]]
    hub.pipes.transport = FakeTransport(routes(**{oa.CONVERTER: (500, "text/plain", b"")}))
    hub.act(run, "approve_private_lookups", {"paper_ids": ids, "digest": batch["digest"]})
    wait_idle(hub)
    progress = next(step for step in hub.get(run)["steps"] if step["step_key"] == "papers")["detail"]["acquisition"]
    assert progress["selected"] == 3 and progress["pending"] == 0 and not progress["running"]
    assert progress["processed"] + progress["failed"] + progress["skipped"] == 3
