from time import monotonic, sleep
from unittest.mock import Mock
from uuid import uuid4

import pytest

from rah.web import create_app
from test_pipelines import Hub


@pytest.fixture
def api(tmp_path, monkeypatch):
    hub = Hub(tmp_path, monkeypatch)
    hub.pipes.close()
    monkeypatch.setattr("rah.web.webbrowser.open", Mock())
    app = create_app(hub.root, hub.book, None, "identity", "token", lambda: None, lambda: "127.0.0.1:8500")
    client = app.test_client()
    headers = {"Authorization": "Bearer token", "Origin": "http://127.0.0.1:8500"}

    def call(method, path, data=None, **extra):
        response = getattr(client, method)("/api" + path, base_url="http://127.0.0.1:8500", headers=headers, json=data, **extra)
        return response.status_code, response.get_json()

    chat = call("post", "/chats", {})[1]["id"]
    return call, chat, hub, client


def test_guided_api_surface(api):
    call, chat, hub, client = api
    base = f"/projects/{chat}/chats/{chat}/pipelines"
    assert client.post("/api" + base + "/preview", base_url="http://127.0.0.1:8500", json={}).status_code == 401
    status, workspace = call("get", "/workspace")
    assert workspace["guided"]["default"] == {"composer": "guided", "purpose": "literature_review", "profile": "standard"}
    assert [item["id"] for item in workspace["guided"]["profiles"]["qualities"]] == ["budget", "standard", "premium"]
    assert workspace["guided"]["pdf_analysis"]["standard"]["available"] is False
    status, error = call("post", base + "/preview", {"purpose": "rapid_research", "profile": "standard", "question": "bad​"})
    assert status == 422 and error["code"] == "public_text"
    status, preview = call("post", base + "/preview", {"purpose": "rapid_research", "profile": "standard", "question": "Public Q?"})
    assert status == 200 and preview["survey"]["deadline_seconds"] == 180
    request = {"request_id": str(uuid4()), "purpose": "rapid_research", "profile": "standard", "question": "Public Q?",
               "confirmation_digest": preview["digest"], "public_confirmed": True}
    status, run = call("post", base, request)
    assert status == 202 and run["state"] == "queued"
    assert call("post", base, request)[0] == 200
    status, chat_data = call("get", f"/projects/{chat}/chats/{chat}")
    assert chat_data["pipeline_runs"][0]["id"] == run["id"] and chat_data["entries"][0]["mode"] == "survey"
    entry = chat_data["entries"][0]["id"]
    assert call("post", f"/projects/{chat}/gptr-reports", {"entry_id": entry})[1]["code"] == "guided_entry"
    assert call("post", "/apps/gptr/launch", {"entry_id": entry, "project_id": chat})[1]["code"] in ("guided_entry", "busy")
    status, stale = call("post", base + f"/{run['id']}/actions", {"action": "pause", "action_id": str(uuid4()), "expected_revision": 99, "data": {}})
    assert status == 409 and stale["code"] == "stale_revision" and stale["run"]["id"] == run["id"]
    assert call("post", base + f"/{run['id']}/actions", {"action": "nope", "action_id": str(uuid4()), "expected_revision": 0, "data": {}})[0] == 422
    assert call("post", f"/chats/{chat}/trash", {})[0] == 409
    deadline = monotonic() + 20
    while call("get", base + f"/{run['id']}")[1]["state"] != "awaiting_scope" and monotonic() < deadline:
        sleep(0.1)
    data = call("get", base + f"/{run['id']}")[1]
    survey = data["steps"][0]["outputs"][0]
    response = client.get(f"/api/projects/{chat}/artifacts/{survey['id']}?named=1", base_url="http://127.0.0.1:8500", headers={"Authorization": "Bearer token"})
    assert response.status_code == 200 and f"01_gptr_survey_{survey['id'][:8]}.md" in response.headers["Content-Disposition"]
    status, cancelled = call("post", base + f"/{run['id']}/actions", {"action": "cancel", "action_id": str(uuid4()),
                                                                     "expected_revision": data["revision"], "data": {}})
    assert status == 200 and cancelled["state"] == "cancelled"
    assert call("post", f"/chats/{chat}/trash", {})[0] == 200


def test_guided_export_endpoint_requires_live_grant(api):
    call, chat, hub, client = api
    response = client.post("/_rah_native_export", base_url="http://127.0.0.1:8500", data=b"TY  - ELEC\nER  - \n",
                           headers={"X-RAH-Export": "a" * 64, "X-RAH-Format": "ris", "X-RAH-Result": str(uuid4()),
                                    "Content-Type": "application/octet-stream"})
    assert response.status_code == 403


def test_workspace_public_start_and_read_only_pqa_preflight(api):
    call, chat, hub, client = api
    status, workspace = call("get", "/workspace")
    start = workspace["guided"]["public_start"]
    assert status == 200 and start["available"] and start["display_digest"]
    base = f"/projects/{chat}/chats/{chat}/pipelines"
    status, preview = call("post", base + "/preview", {"purpose": "rapid_research", "profile": "standard", "question": "Public Q?"})
    assert preview["display_digest"] == start["display_digest"]
    runs = sorted(path.name for path in (hub.root / "hub/runs").iterdir())
    status, error = call("post", f"/projects/{chat}/pqa-preflight", {"pdfs": [], "question": "Q", "quality": "standard"})
    assert status >= 400 and error["code"] in ("profile_unavailable", "unverified_profile", "pdf_selection")
    assert call("post", f"/projects/{chat}/pqa-preflight", {"pdfs": [], "question": "Q", "extra": 1})[0] == 422
    assert sorted(path.name for path in (hub.root / "hub/runs").iterdir()) == runs
    assert client.post(f"/api/projects/{chat}/pqa-preflight", base_url="http://127.0.0.1:8500", json={}).status_code == 401
