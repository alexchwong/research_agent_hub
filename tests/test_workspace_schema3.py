import hashlib
from importlib import resources
import io
import json
import sqlite3
from threading import Thread
from uuid import uuid4

import pytest

from rah.common import Problem, prepare_root
from rah.projects import Projects, output_id, stored_version


@pytest.fixture
def root(tmp_path):
    return prepare_root(tmp_path.resolve() / "RAH data")


def build_v2(root):
    """Create a schema 2 database the way the 0.1.0 migration chain does."""
    database = root / "hub/state.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(resources.files("rah").joinpath("schema.sql").read_text())
        db.executescript(resources.files("rah").joinpath("workspace.sql").read_text())
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for statement in resources.files("rah").joinpath("workspace_v3.sql").read_text().split(";"):
            if statement.strip():
                db.execute(statement)
    return database


def seed_v2(root):
    database = build_v2(root)
    project, chat, solo, trashed = (str(uuid4()) for _ in range(4))
    report, pdf, answer, ris = (str(uuid4()) for _ in range(4))
    entry, archived = str(uuid4()), str(uuid4())
    files = {}
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO scope VALUES (?)", (project,))
        db.execute("INSERT INTO project (id,name,revision,created_at,updated_at) VALUES (?, 'Résumé · 研究', 3, 'old', 'old')", (project,))
        db.execute("INSERT INTO chat (id,project_id,title,app,created_at,updated_at) VALUES (?,?,'Named chat','gptr','old','old')", (chat, project))
        for item in (solo, trashed):
            db.execute("INSERT INTO scope VALUES (?)", (item,))
            db.execute("INSERT INTO chat (id,project_id,title,app,created_at,updated_at,trash_state,trashed_at) VALUES (?,NULL,'Solo','pqa','old','old',?,?)",
                       (item, "trashed" if item == trashed else None, "old" if item == trashed else None))
        rows = [(project, report, "report", "md", b"# report", chat, "run-1"), (project, pdf, "pdf", "pdf", b"%PDF-1.7\nfixture", None, None),
                (solo, answer, "answer", "md", "Answer · ünïcode".encode(), solo, "run-2"), (project, ris, "report", "ris", b"TY  - ELEC\nER  - \n", chat, None)]
        for scope, ident, kind, ext, content, owner, run in rows:
            directory = root / ("projects/" + scope if scope == project else "chats/" + scope)
            if owner and scope == project:
                directory = directory / "chats" / owner
            directory = directory / "files"
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            (directory / f"{ident}.{ext}").write_bytes(content)
            files[ident] = directory / f"{ident}.{ext}"
            db.execute("""INSERT INTO artifact (project_id,id,kind,app_id,storage_name,original_name,sha256,size_bytes,run_id,created_at,chat_id)
                          VALUES (?,?,?,?,?,?,?,?,?,'old',?)""", (scope, ident, kind, "gptr" if run else None, f"{ident}.{ext}",
                                                                  f"original.{ext}", hashlib.sha256(content).hexdigest(), len(content), run, owner))
        (files[answer].parent / f"{answer}.bib").write_text("@article{x}")
        db.execute("INSERT INTO paper (project_id,id,title,pdf_artifact_id,state) VALUES (?,?,'Paper',?,'supplied')", (project, str(uuid4()), pdf))
        db.execute("INSERT INTO paper (project_id,id,title,state) VALUES (?,?,'Missing','needed')", (project, str(uuid4())))
        for ident, state in ((entry, "Complete"), (archived, "Interrupted")):
            db.execute("""INSERT INTO chat_entry (id,chat_id,app,action,question,quote,intent,state,result_ids,created_at,updated_at)
                          VALUES (?,?,'gptr','managed','q','','[]',?,?,'old','old')""", (ident, chat, state, json.dumps([report])))
        db.execute("INSERT INTO workspace_setting VALUES (1, ?)", (str(root),))
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
    return {"project": project, "chat": chat, "solo": solo, "trashed": trashed, "files": files,
            "report": report, "pdf": pdf, "answer": answer, "ris": ris, "entry": entry}


def table_rows(database, name):
    with sqlite3.connect(database) as db:
        return db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]


def test_v2_upgrade_preserves_rows_bytes_and_backs_up(root):
    data = seed_v2(root)
    database = root / "hub/state.sqlite3"
    before = {name: table_rows(database, name) for name in ("project", "chat", "chat_entry", "artifact", "paper", "scope")}
    digests = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in data["files"].items()}
    store = Projects(root)
    assert stored_version(database) == 4
    assert {name: table_rows(database, name) for name in before} == before
    assert {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in data["files"].items()} == digests
    with store.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        legacy = db.execute("SELECT output_key, origin_class, role FROM artifact WHERE id=?", (data["report"],)).fetchone()
        assert tuple(legacy) == ("primary", "private_or_unknown", None)
        assert {row[0] for row in db.execute("SELECT access_status FROM paper")} == {"local_available", "unresolved"}
        assert tuple(db.execute("SELECT composer,purpose,profile FROM guided_default").fetchone()) == ("guided", "literature_review", "standard")
    backups = list((root / "hub/backups").glob("state-pre-v3-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as copy:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 2
        assert copy.execute("SELECT count(*) FROM artifact").fetchone()[0] == before["artifact"]
    assert json.loads(backups[0].with_suffix(".json").read_text())["integrity_check"] == "ok"
    assert store.get_chat(data["project"], data["chat"])["entries"][0]["id"] == data["entry"]
    assert store.artifact_path(store.artifact(data["solo"], data["answer"])).with_suffix(".bib").is_file()
    assert store.trash_list()[0]["id"] == data["trashed"]
    # The legacy single-output replay keeps the original ID; changed bytes are refused.
    assert store.register(data["solo"], io.BytesIO("Answer · ünïcode".encode()), "original.md", "answer", run_id="run-2") == data["answer"]
    with pytest.raises(Problem) as conflict:
        store.register(data["solo"], io.BytesIO(b"other"), "original.md", "answer", run_id="run-2")
    assert conflict.value.status == 409
    assert Projects(root).get(data["project"])["revision"] == 3


def test_fresh_install_matches_upgrade_default(root, tmp_path):
    fresh = Projects(root)
    with fresh.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        fresh_default = tuple(db.execute("SELECT * FROM guided_default").fetchone())
    other = prepare_root(tmp_path.resolve() / "upgraded")
    seed_v2(other)
    with Projects(other).connect() as db:
        assert tuple(db.execute("SELECT * FROM guided_default").fetchone()) == fresh_default


def test_future_version_is_refused_before_workspace_changes(root):
    database = build_v2(root)
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version=5")
    with pytest.raises(Problem) as error:
        Projects(root)
    assert error.value.code == "workspace_version"
    assert not (root / "chats").exists() and not (root / "hub/backups").exists()


def test_injected_failure_rolls_back_and_redo_succeeds(root):
    data = seed_v2(root)

    def fail(db):
        raise RuntimeError("injected")

    with pytest.raises(Problem) as error:
        Projects(root, migration_hook=fail)
    assert error.value.code == "workspace_migration" and "backup" in str(error.value)
    assert stored_version(root / "hub/state.sqlite3") == 2
    with sqlite3.connect(root / "hub/state.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='pipeline_run'").fetchone()[0] == 0
    store = Projects(root)
    assert store.get_chat(data["project"], data["chat"])["entries"]
    assert len(list((root / "hub/backups").glob("*.sqlite3"))) == 2


def test_simultaneous_launch_upgrades_once(root):
    seed_v2(root)
    errors = []

    def open_store():
        try:
            Projects(root)
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [Thread(target=open_store) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert stored_version(root / "hub/state.sqlite3") == 4
    assert len(list((root / "hub/backups").glob("*.sqlite3"))) == 1


def pipeline_fixture(store, project=None):
    chat = store.create_chat(project)["id"]
    scope = project or chat
    run, step, entry = str(uuid4()), str(uuid4()), str(uuid4())
    with store.connect(write=True) as db:
        db.execute("""INSERT INTO chat_entry (id,chat_id,app,action,question,quote,intent,state,created_at,updated_at)
                      VALUES (?,?,'gptr','managed','q','','guided','Running','now','now')""", (entry, chat))
        db.execute("""INSERT INTO pipeline_run (id,project_id,chat_id,brief_revision,purpose,recipe_version,state,original_question,
                      selected_profile,created_at,updated_at) VALUES (?,?,?,1,'rapid_research',1,'running','q','standard','now','now')""",
                   (run, scope, chat))
        db.execute("""INSERT INTO pipeline_step (id,run_id,position,step_key,attempt,entry_id,state,created_at,updated_at)
                      VALUES (?,?,0,'survey',1,?,'running','now','now')""", (step, run, entry))
    return scope, chat, run, step, entry


def outputs(store, scope, chat, items):
    return [{"key": key, "role": role, "stage": store.stage(scope, chat, io.BytesIO(body), name, kind)}
            for key, role, body, name, kind in items]


def test_multi_output_publication_replay_and_conflicts(root):
    store = Projects(root)
    scope, chat, run, step, entry = pipeline_fixture(store)
    items = [("report", "discovery_report", b"# LDR", "01_ldr_report.md", "report"),
             ("citations", "citation_export", b"TY  - ELEC\nER  - \n", "02_ldr_citations.ris", "citations")]
    first = store.publish(scope, chat, outputs(store, scope, chat, items), step=step, run_id=step, entry=entry)
    assert first == [output_id(step, "report"), output_id(step, "citations")]
    assert store.publish(scope, chat, outputs(store, scope, chat, items), step=step, run_id=step, entry=entry) == first
    assert store.get_chat(scope, chat)["entries"][0]["result_ids"] == first
    for change in ((b"# changed", "01_ldr_report.md"), (b"# LDR", "renamed.md")):
        with pytest.raises(Problem) as conflict:
            store.publish(scope, chat, outputs(store, scope, chat, [("report", "discovery_report", change[0], change[1], "report")]),
                          step=step, run_id=step, entry=entry)
        assert conflict.value.status == 409
    files = sorted(path.name for path in (root / "chats" / chat / "files").iterdir())
    assert files == sorted(value + ext for value, ext in zip(first, (".md", ".ris")))
    with store.connect() as db:
        kinds = {row["id"]: (row["kind"], row["media_type"], row["step_id"]) for row in db.execute("SELECT * FROM artifact")}
    assert kinds[first[1]] == ("citations", "application/x-research-info-systems", step)


def test_partial_commit_failure_and_orphan_recovery(root, tmp_path):
    store = Projects(root)
    scope, chat, run, step, entry = pipeline_fixture(store)
    marker = tmp_path / "publish.json"

    def explode(db, ids):
        raise RuntimeError("injected finalize failure")

    with pytest.raises(RuntimeError):
        store.publish(scope, chat, outputs(store, scope, chat, [("report", "research_report", b"# A", "a.md", "report"),
                                                                  ("citations", "citation_export", b"@x{}", "a.bib", "citations")]),
                      step=step, run_id=step, entry=entry, finalize=explode, marker=marker)
    directory = root / "chats" / chat / "files"
    assert list(directory.iterdir()) == []
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM artifact").fetchone()[0] == 0
    # A crash after placement leaves a file matching the uncommitted marker; recovery removes only that file.
    orphan = directory / (output_id(step, "report") + ".md")
    orphan.write_bytes(b"# A")
    keep = directory / "user-file.md"
    keep.write_bytes(b"# A")
    store.recover_publication(scope, chat, marker)
    assert not orphan.exists() and keep.exists()


def test_lineage_rejects_other_scopes_and_cycles(root):
    store = Projects(root)
    project = store.create("Shared project")
    scope, chat, run, step, entry = pipeline_fixture(store, project)
    other_chat = store.create_chat(project)["id"]
    sibling = store.register(project, io.BytesIO(b"# sibling"), "sibling.md", "report", chat=other_chat)
    solo = store.create_chat(None)["id"]
    foreign = store.register(solo, io.BytesIO(b"# foreign"), "foreign.md", "report")
    staged = outputs(store, scope, chat, [("report", "research_report", b"# child", "child.md", "report")])
    staged[0]["parents"] = [sibling]
    (child,) = store.publish(scope, chat, staged, step=step, run_id=step)
    staged = outputs(store, scope, chat, [("answer", "answer", b"# other", "other.md", "answer")])
    staged[0]["parents"] = [foreign]
    with pytest.raises(Problem):
        store.publish(scope, chat, staged, step=step, run_id=step)
    with store.connect(write=True) as db, pytest.raises(Problem):
        store.add_parent(db, project, child, sibling)
    listed = store.artifacts_for(project, chat, include_project=True)
    assert {row["id"] for row in listed} >= {child, sibling}
    assert {row["id"] for row in store.artifacts_for(project, chat)} == {child}
