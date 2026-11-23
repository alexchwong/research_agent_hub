import asyncio
import hashlib
import hmac
from importlib import resources
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from rah import settings, upstreams
from rah.common import Problem, canonical, digest, prepare_root, write_json
from rah.gptr_browser import LocalBrowser
from rah.installer import RouteBook
from rah.native_handoff import NativeHandoff
from rah.projects import Projects, UNFILED
from rah.web import create_app


@pytest.fixture
def root(tmp_path):
    return prepare_root(tmp_path.resolve() / "RAH data")


def test_folders_scope_trash_restore_and_move(root):
    store = Projects(root)
    project = store.create("Project")
    first = store.create_chat(project)["id"]
    second = store.create_chat(project)["id"]
    solo = store.create_chat(None)["id"]
    assert store.folder(project, first) == root / "projects" / project / "chats" / first
    assert (store.folder(solo) / "files").is_dir()
    assert store.get_chat(solo, solo)["project_id"] is None
    shared = store.register(project, io.BytesIO(b"shared"), "shared.md", "report")
    private = store.register(project, io.BytesIO(b"private"), "private.md", "report", chat=first)
    result = store.register(solo, io.BytesIO(b"solo"), "solo.md", "report")
    assert [a["id"] for a in store.get(project, first)["artifact"]] == [private]
    assert store.get(project, second)["artifact"] == []
    assert store.artifact_path(store.artifact(solo, result)).read_bytes() == b"solo"
    store.trash_chat(first)
    assert store.trash_list()[0]["id"] == first
    assert [a["id"] for a in store.get(project)["artifact"]] == [shared]
    assert (root / "trash" / first / "files").is_dir()
    custom = root.parent / "Research résumé"
    backup = Path(store.move_workspace(str(custom))["backup"])
    assert (backup / "trash" / first).is_dir()
    assert (root / "hub/state.sqlite3").is_file()
    store = Projects(root)
    store.restore_chat(first)
    assert store.artifact_path(store.artifact(project, private)).read_bytes() == b"private"
    assert store.folder(project, first).is_relative_to(custom)
    store.move_workspace(str(root))
    assert Projects(root).folder(solo) == root / "chats" / solo


def test_recover_interrupted_trash_after_directory_move(root):
    store = Projects(root)
    chat = store.create_chat(None)["id"]
    with store.connect(write=True) as db:
        db.execute("UPDATE chat SET trash_state='trashing' WHERE id=?", (chat,))
    store.chat_directory({"id": chat, "project_id": None}).rename(root / "trash" / chat)
    assert Projects(root).trash_list()[0]["id"] == chat


def test_upgrade_legacy_unfiled_preserves_artifacts_and_shared_papers(root):
    chat, entry, report, pdf = (str(uuid4()) for _ in range(4))
    with sqlite3.connect(root / "hub/state.sqlite3") as db:
        for name in ("schema.sql", "workspace.sql"):
            db.executescript(resources.files("rah").joinpath(name).read_text())
        db.execute("INSERT INTO project VALUES (?, 'Unfiled', 4,'old','old')", (UNFILED,))
        db.execute("INSERT INTO chat VALUES (?,?,'Legacy','gptr','old','old')", (chat, UNFILED))
        db.execute("""INSERT INTO chat_entry (id,chat_id,app,action,question,quote,intent,state,result_ids,created_at,updated_at)
                      VALUES (?,?,'gptr','managed','question','','[]','Complete',?,'old','old')""", (entry, chat, json.dumps([report])))
        for ident, owner, kind, ext, content in ((report, chat, "report", "md", b"report"), (pdf, None, "pdf", "pdf", b"%PDF-1.7")):
            directory = root / "projects" / UNFILED
            if owner:
                directory = directory / "chats" / owner
            directory = directory / "files"
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            name = f"{ident}.{ext}"
            (directory / name).write_bytes(content)
            db.execute("""INSERT INTO artifact (project_id,id,kind,storage_name,sha256,size_bytes,created_at,chat_id)
                          VALUES (?,?,?,?,?,?,'old',?)""", (UNFILED, ident, kind, name, digest(content), len(content), owner))
        db.execute("INSERT INTO paper (project_id,id,title,pdf_artifact_id,state) VALUES (?,?,'Paper',?,'supplied')",
                   (UNFILED, str(uuid4()), pdf))
    store = Projects(root)
    assert store.get_chat(UNFILED, chat)["entries"][0]["id"] == entry
    assert store.get_chat(chat, chat)["project_id"] is None
    assert store.get(UNFILED)["revision"] == 4
    assert store.artifact_path(store.artifact(chat, report)).parent == root / "chats" / chat / "files"
    assert store.selected_pdfs(chat, [pdf])[0]["id"] == pdf
    assert Projects(root).get_chat(chat, chat)["id"] == chat


def test_gptr_credential_reaches_launch_children_and_upgrades_old_profile(root):
    config = settings.parse("RAH_OPENROUTER_API_KEY=offline-fixture\nRAH_NATIVE_GPTR__SMART_LLM=openrouter:test/chat\n")
    capabilities = RouteBook.load().capabilities("gptr")
    current = settings.resolve(config, "gptr", capabilities)
    previous = settings.install_projection(root, "gptr", current)
    previous.pop("checksum")
    previous["version"] = 1
    previous["env"].pop("OPENROUTER_API_KEY")
    write_json(root / "apps/gptr/launch/profile.json", previous | {"checksum": digest(canonical(previous))})
    profile = settings.checked_projection(root, "gptr", config, upgrade=lambda: settings.resolve(config, "gptr", capabilities))
    assert settings.applied(root, "gptr")["version"] == settings.GPTR_PROFILE_VERSION
    store = Projects(root)
    chat = store.create_chat(None)["id"]
    for browser in (False, True):
        args = {"browser": True, "workspace": store.folder(chat)} if browser else {}
        spec = upstreams.launch_spec(root, "gptr", root / "test-install", profile, **args)
        subprocess.run([sys.executable, "-c", "import os; assert os.environ['OPENROUTER_API_KEY']=='offline-fixture'"],
                       env=spec.env, cwd=spec.directory, check=True)
        assert "offline-fixture" not in repr(spec.command)
        if browser:
            assert spec.directory == store.folder(chat) / "gptr"
            assert Path(spec.env["DOC_PATH"]).name == "my-docs"


def test_signed_prefill_is_local_single_use_and_does_not_run_upstream():
    calls = []

    async def upstream(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/html")]})
        await send({"type": "http.response.body", "body": b"<body><textarea id='task'></textarea></body>"})

    identity = "a" * 64
    origin = b"http://127.0.0.1:58722"
    app = LocalBrowser(NativeHandoff(upstream, "gptr", 58722, identity, "http://127.0.0.1:8900"), 58722, identity)

    def invoke(path, body=b"", headers=(), method="POST", source=origin):
        messages = []
        async def send(message):
            messages.append(message)
        async def receive():
            return {"type": "http.request", "body": body}
        asyncio.run(app({"type": "http", "path": path, "method": method, "headers": [
            (b"host", b"127.0.0.1:58722"), (b"origin", source), *headers]}, receive, send))
        return messages[0]["status"], messages[-1].get("body", b"")

    question = "  A multiline question?\nUntrusted <script>text</script> — café.  "
    ticket = "b" * 64
    payload = canonical({"ticket": ticket, "question": question})
    proof = hmac.new(identity.encode(), b"handoff:" + hashlib.sha256(payload).hexdigest().encode(), "sha256").hexdigest().encode()
    assert invoke("/_rah_handoff_stage", payload)[0] == 403
    assert invoke("/_rah_handoff_stage", payload, [(b"x-rah-proof", proof)])[0] == 204
    assert b"/_rah_handoff.js" in invoke("/", method="GET")[1]
    headers = [(b"x-rah-ticket", ticket.encode())]
    assert invoke("/_rah_handoff_take", headers=headers, source=b"http://foreign")[0] == 403
    status, result = invoke("/_rah_handoff_take", headers=headers)
    assert status == 200 and json.loads(result)["question"] == question
    assert invoke("/_rah_handoff_take", headers=headers)[0] == 404
    assert calls == ["/"]


def test_standalone_chat_http_and_scoped_folder(root):
    app = create_app(root, RouteBook.load(), None, "hub", "test", lambda: None, lambda: "localhost")
    client = app.test_client()
    headers = {"Origin": "http://localhost", "Authorization": "Bearer test"}
    chat = client.post("/api/chats", headers=headers, json={}).json["id"]
    assert client.get("/api/projects/" + chat, headers=headers).json["folder"] == str(root / "chats" / chat)
    assert client.post(f"/api/chats/{chat}/trash", headers=headers, json={}).status_code == 200
    assert client.get("/api/projects/" + chat, headers=headers).status_code == 404
    assert client.post(f"/api/chats/{chat}/restore", headers=headers, json={}).status_code == 200
    assert client.get("/api/projects/" + chat, headers=headers).status_code == 200