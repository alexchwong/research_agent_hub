"""T12, T17: the PDF send is approved against a read-only server preflight that the bound preview must reproduce."""
import io

import pytest

from rah import pqa_profiles, usage
from rah.common import Problem, atomic
from fixtures import text_pdf
from test_pipelines import enable_route, hub  # noqa: F401  (hub is a fixture)


@pytest.fixture
def pdfs(hub, monkeypatch):
    enable_route(hub, monkeypatch)
    return [hub.projects.register(hub.scope, io.BytesIO(text_pdf(name)), name + ".pdf", "pdf", chat=hub.chat) for name in ("a", "b")]


def snapshot(hub):
    runs = sorted(path.name for path in (hub.root / "hub/runs").iterdir())
    with hub.projects.connect() as db:
        rows = db.execute("SELECT count(*) FROM chat_entry").fetchone()[0], db.execute("SELECT count(*) FROM artifact").fetchone()[0]
    return runs, rows


def test_preflight_writes_nothing_and_preview_reproduces_it(hub, pdfs):
    before = snapshot(hub)
    shown = hub.runs.preflight(hub.scope, pdfs, "Question?", quote="excerpt")
    assert snapshot(hub) == before
    assert [row["name"] for row in shown["files"]] == ["a.pdf", "b.pdf"] and shown["file_ids"] == pdfs
    assert {row["role"] for row in shown["destinations"]} == set(pqa_profiles.PAPER_ROLES)
    preview = hub.runs.preview(hub.scope, pdfs, "Question?", quote="excerpt")
    assert preview["disclosure_digest"] == shown["disclosure_digest"]
    assert preview["destinations_digest"] == shown["destinations_digest"]


@pytest.mark.parametrize("change", ["order", "question", "excerpt", "quality", "credential", "content", "provider"])
def test_any_change_between_preflight_and_click_changes_the_digest(hub, pdfs, monkeypatch, change):
    shown = hub.runs.preflight(hub.scope, pdfs, "Question?", quote="excerpt")
    ids, question, quote, quality = list(pdfs), "Question?", "excerpt", "standard"
    if change == "order":
        ids.reverse()
    elif change == "question":
        question = "Other?"
    elif change == "excerpt":
        quote = "other"
    elif change == "quality":
        quality = "budget"
    elif change == "credential":
        hub.write_config("RAH_OPENROUTER_API_KEY=another-key\n")
    elif change == "content":
        row = hub.projects.selected_pdfs(hub.scope, [pdfs[0]])[0]
        atomic(hub.projects.artifact_path(row), text_pdf("changed"))
    elif change == "provider":
        entry = dict(pqa_profiles.REGISTRY["pqa-standard-v1"]["enabled"][0])
        entry["providers"] = dict(entry["providers"], answer="Other")
        monkeypatch.setattr(pqa_profiles, "REGISTRY", dict(pqa_profiles.REGISTRY) | {
            "pqa-standard-v1": dict(pqa_profiles.REGISTRY["pqa-standard-v1"]) | {"enabled": (entry,)}})
    try:
        preview = hub.runs.preview(hub.scope, ids, question, quote=quote, quality=quality)
    except Problem as error:  # A changed PDF or an unavailable quality is refused before any transfer.
        assert error.code in ("changed_pdf", "profile_unavailable")
        return
    assert preview["disclosure_digest"] != shown["disclosure_digest"]


def test_changed_observer_invalidates_preflight_preview_and_run(hub, pdfs, monkeypatch):
    preview = hub.runs.preview(hub.scope, pdfs, "Question?")
    stored = hub.runs.previews[preview["preview_id"]]
    monkeypatch.setattr(usage, "observer_digest", lambda: "0" * 64)
    with pytest.raises(Problem) as unavailable:
        hub.runs.preflight(hub.scope, pdfs, "Question?")
    assert unavailable.value.code == "profile_unavailable"  # The enabled identity binds the observer hash.
    monkeypatch.setattr(hub.runs, "paper_route", lambda config, quality="standard": (
        {"lock_sha256": stored.binding["lock"], "upstream_version": "fixture"},
        type("L", (), {"name": stored.binding["install"]})(), stored.binding["profile"]))
    with pytest.raises(Problem) as stale:
        hub.runs.run_paper(stored, type("J", (), {"checkpoint": lambda self, final=False: None})())
    assert stale.value.code == "stale_preview"
