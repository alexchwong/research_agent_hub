"""T18: schema 4 adds usage_json and closes everything that was in flight, in one transaction."""
import io
import json
import sqlite3
from uuid import uuid4

import pytest

from rah.common import Problem, prepare_root
from rah.projects import Projects, stored_version


@pytest.fixture
def root(tmp_path):
    return prepare_root(tmp_path / "RAH data")


def build_v3(root, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(Projects, "upgrade_v4", lambda self: None)
        store = Projects(root)
    assert stored_version(root / "hub/state.sqlite3") == 3
    chat = store.create_chat(None)["id"]
    artifact = store.register(chat, io.BytesIO(b"# kept"), "kept.md", "report", chat=chat)
    ids = {name: str(uuid4()) for name in ("run", "done_run", "gptr_step", "ldr_step", "done_step", "gptr_entry",
                                          "ldr_entry", "done_entry", "awaiting", "running", "complete")}
    chats = {name: store.create_chat(None)["id"] for name in ("awaiting", "running")}
    with sqlite3.connect(root / "hub/state.sqlite3") as db:
        entry = """INSERT INTO chat_entry (id,chat_id,app,action,question,quote,intent,state,created_at,updated_at)
                   VALUES (?,?,?,?, 'q','',?,?,'now','now')"""
        db.execute(entry, (ids["gptr_entry"], chat, "gptr", "managed", "guided", "Running"))
        db.execute(entry, (ids["ldr_entry"], chat, "ldr", "native", "guided", "Waiting for native step"))
        db.execute(entry, (ids["done_entry"], chat, "gptr", "managed", "guided", "Complete"))
        db.execute(entry, (ids["awaiting"], chats["awaiting"], "pqa", "managed", "classic", "Awaiting approval"))
        db.execute(entry, (ids["running"], chats["running"], "gptr", "managed", "classic", "Running"))
        db.execute(entry, (ids["complete"], chat, "gptr", "managed", "classic", "Complete"))
        run = """INSERT INTO pipeline_run (id,project_id,chat_id,brief_revision,purpose,recipe_version,state,outcome,
                 original_question,selected_profile,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?,'q','standard','now','now')"""
        db.execute(run, (ids["run"], chat, chat, 1, "literature_review", "running", None))
        db.execute(run, (ids["done_run"], chat, chat, 2, "rapid_research", "succeeded", "complete"))
        step = """INSERT INTO pipeline_step (id,run_id,position,step_key,attempt,entry_id,state,created_at,updated_at)
                  VALUES (?,?,?,?,1,?,?,'now','now')"""
        db.execute(step, (ids["gptr_step"], ids["run"], 0, "survey", ids["gptr_entry"], "succeeded"))
        db.execute(step, (ids["ldr_step"], ids["run"], 2, "ldr_discovery", ids["ldr_entry"], "waiting_native"))
        db.execute(step, (ids["done_step"], ids["done_run"], 0, "survey", ids["done_entry"], "succeeded"))
        db.execute("UPDATE pipeline_step SET state='running' WHERE id=?", (ids["gptr_step"],))
    return ids, artifact, chat


def test_schema3_upgrade_closes_in_flight_state_consistently(root, monkeypatch):
    ids, artifact, chat = build_v3(root, monkeypatch)
    store = Projects(root)
    assert stored_version(root / "hub/state.sqlite3") == 4
    with store.connect() as db:
        steps = {row["id"]: dict(row) for row in db.execute("SELECT * FROM pipeline_step")}
        entries = {row["id"]: dict(row) for row in db.execute("SELECT * FROM chat_entry")}
        runs = {row["id"]: row["state"] for row in db.execute("SELECT id, state FROM pipeline_run")}
        assert db.execute("SELECT count(*) FROM artifact WHERE id=?", (artifact,)).fetchone()[0] == 1
    for key in ("gptr_step", "ldr_step"):
        assert steps[ids[key]]["state"] == "cancelled" and steps[ids[key]]["error_code"] == "upgrade"
        assert steps[ids[key]]["finished_at"]
    assert steps[ids["done_step"]]["state"] == "succeeded" and steps[ids["done_step"]]["error_code"] is None
    assert entries[ids["gptr_entry"]]["state"] == entries[ids["ldr_entry"]]["state"] == "Cancelled"
    assert entries[ids["awaiting"]]["state"] == "Interrupted" and entries[ids["awaiting"]]["preview_id"] is None
    assert entries[ids["running"]]["state"] == "Interrupted"
    assert entries[ids["complete"]]["state"] == entries[ids["done_entry"]]["state"] == "Complete"
    assert runs == {ids["run"]: "cancelled", ids["done_run"]: "succeeded"}
    polling = {"Preparing", "Awaiting approval", "Running", "Waiting for native step"}
    assert not polling & {entry["state"] for entry in entries.values()}
    assert all(entry["usage_json"] is None for entry in entries.values())
    assert (root / "chats" / chat / "files").exists()


def test_fresh_chain_reaches_4_and_usage_json_is_checked(root):
    store = Projects(root)
    assert stored_version(root / "hub/state.sqlite3") == 4
    chat = store.create_chat(None)["id"]
    entry = str(uuid4())
    with store.connect(write=True) as db:
        db.execute("""INSERT INTO chat_entry (id,chat_id,app,action,question,quote,intent,state,created_at,updated_at)
                      VALUES (?,?,'gptr','managed','q','','classic','Complete','now','now')""", (entry, chat))
    store.record_usage(entry, {"basis": "not_observed"})
    assert store.get_chat(chat, chat)["entries"][0]["usage"] == {"basis": "not_observed"}
    for bad in ("not json", json.dumps({"x": "y" * 9000})):
        with pytest.raises(sqlite3.IntegrityError), store.connect(write=True) as db:
            db.execute("UPDATE chat_entry SET usage_json=? WHERE id=?", (bad, entry))


def test_newer_schema_is_refused(root):
    Projects(root)
    with sqlite3.connect(root / "hub/state.sqlite3") as db:
        db.execute("PRAGMA user_version=5")
    with pytest.raises(Problem) as error:
        Projects(root)
    assert error.value.code == "workspace_version"
