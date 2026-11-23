import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock

import pytest

from rah import search_sources, settings
from rah.common import Problem
from rah.gptr_browser import report_source_status
from rah.runs import Runs


@pytest.mark.parametrize("app,field,default,override", [
    ("gptr", "RETRIEVER", "pubmed_central", "tavily"),
    ("ldr", "LDR_SEARCH_TOOL", "pubmed", "wikipedia"),
])
def test_defaults_and_explicit_search_settings(app, field, default, override):
    assert settings.resolve(settings.parse(""), app, [])["env"][field] == default
    key = f"RAH_NATIVE_{app.upper()}__{field}"
    assert settings.resolve(settings.parse(f"{key}={override}"), app, [])["env"][field] == override
    assert field not in settings.resolve(settings.parse(f"{key}=native"), app, [])["env"]


def test_searx_connection_requires_results():
    payload = {"results": [{"url": "https://docs.python.org/3/library/pathlib.html"}]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path.startswith("/search?q=Python+pathlib+documentation&format=json")
            assert not self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            assert search_sources.test_searx(url) == 1
            payload.update(results=[{"url": "not a result"}], unresponsive_engines=[["fixture", "timeout"]])
            with pytest.raises(Problem, match="1 unresponsive engines"):
                search_sources.test_searx(url)
        finally:
            server.shutdown()
            thread.join()
    with pytest.raises(Problem, match="local SearXNG"):
        search_sources.test_searx("https://example.invalid:8888")


def test_managed_empty_sources_do_not_publish_report(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "report.md").write_bytes(b"---\nquery: sample\nsources_count: 0\n---\n\nUnsourced report\n")
    run = object.__new__(Runs)
    run.projects = Mock()
    with pytest.raises(Problem, match="No sources retrieved"):
        run.collect(tmp_path, {"app": "gptr", "project": "fixture", "version": "fixture"})
    run.projects.register.assert_not_called()


@pytest.mark.parametrize("source,found,warn", [("web", False, True), ("web", True, False), ("local", False, False)])
def test_classic_reports_empty_web_retrieval(source, found, warn):
    async def receive():
        return {"type": "websocket.receive", "text": "start " + json.dumps({"report_source": source})}

    messages = []

    async def send(message):
        messages.append(json.loads(message["text"]))

    async def upstream(scope, receive, send):
        await receive()
        for msg in [{"type": "logs", "content": "subquery_context_not_found"},
                    *([{"type": "logs", "content": "context_combined"}] if found else []), {"type": "path"}]:
            await send({"type": "websocket.send", "text": json.dumps(msg)})

    asyncio.run(report_source_status(upstream, {}, receive, send))
    assert any(msg.get("content") == "rah_no_sources" for msg in messages) is warn
    assert messages[-1]["type"] == "path"