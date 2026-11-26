import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from rah import settings, upstreams
from rah.common import Problem, prepare_root, write_json
from rah.gptr_browser import LocalBrowser
from rah.installer import RouteBook
from rah.web import create_app, gptr_recipients


@pytest.mark.parametrize("listener", ["owned", "standalone", "unrelated"])
@pytest.mark.parametrize("name, port_attr", [("gptr", "GPTR_PORT"), ("ldr", "LDR_PORT")])
def test_restart_only_recovers_and_stops_owned_listener(tmp_path, monkeypatch, listener, name, port_attr):
    root = prepare_root(tmp_path.resolve() / "data")
    identity = "a" * 64
    generation = "b" * 32
    stopped = Event()
    stop = Mock(side_effect=stopped.set)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            async def send(message):
                if message["type"] == "http.response.start":
                    self.send_response(message["status"])
                    self.end_headers()
                else:
                    self.wfile.write(message.get("body", b""))

            asyncio.run(native({"type": "http", "method": self.command, "path": self.path,
                                "headers": [(key.lower().encode(), value.encode()) for key, value in self.headers.items()]},
                               None, send))

        do_POST = do_GET

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_port
        url = f"http://127.0.0.1:{port}"
        owned = listener == "owned"
        native = LocalBrowser(None, port, {"owned": identity, "standalone": "", "unrelated": "f" * 64}[listener], stop)
        monkeypatch.setattr(upstreams, port_attr, port)
        monkeypatch.setattr("rah.web.webbrowser.open", Mock())
        record = root / "hub/runtime" / f"{name}-browser.json"
        write_json(record, {"identity": identity, "generation": generation, "revision": "c" * 64})
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            book = RouteBook.load()
            headers = {"Authorization": "Bearer test", "Origin": "http://localhost"}

            def start_hub():
                return create_app(root, book, None, "hub", "test", Mock(), lambda: "localhost").test_client()

            initial = start_hub()
            assert initial.get(f"/api/apps/{name}/browser", headers=headers).json["state"] == ("Ready" if owned else "Stopped")
            hub = start_hub()
            if owned:
                result = hub.post(f"/api/apps/{name}/launch", headers=headers, json={"ready_generation": generation})
                assert result.json == {"state": "Ready", "url": url + "/", "generation": generation,
                                       "restart_needed": True, "opened": True}
                assert identity not in result.get_data(as_text=True)
                stop.assert_not_called()
            with pytest.raises(HTTPError) as denied:
                urlopen(Request(url + "/_rah_stop", headers={"Origin": url, "X-RAH-Nonce": "d" * 64,
                                                            "X-RAH-Proof": "e" * 64}, method="POST"), timeout=2)
            assert denied.value.code == 403
            denied.value.close()
            stop.assert_not_called()
            result = hub.post(f"/api/apps/{name}/stop", headers=headers, json={})
            assert result.status_code == (200 if owned else 409)
            if owned:
                assert stopped.wait(2)
                stop.assert_called_once()
            else:
                stop.assert_not_called()
        finally:
            server.shutdown()
            thread.join()


def test_gptr_chat_distinguishes_missing_models_and_search_credentials():
    with pytest.raises(Problem) as missing:
        gptr_recipients({"FAST_LLM": "openrouter:fixture/chat"}, "shared-key")
    assert missing.value.code == "missing_models"
    assert "Smart, Strategic, Embedding" in str(missing.value)
    assert "OpenAI" not in str(missing.value)

    env = {role: "openrouter:fixture/model" for role in settings.ROLES["gptr"].values()}
    env["OPENROUTER_API_KEY"] = "shared-key"
    env["RETRIEVER"] = "pubmed_central"
    assert gptr_recipients(env, "shared-key") == ["OpenRouter", "PubMed Central (NCBI)"]
    env["RETRIEVER"] = "tavily"
    with pytest.raises(Problem) as search:
        gptr_recipients(env, "shared-key")
    assert search.value.code == "needs_search_credential"
    env["TAVILY_API_KEY"] = "search-key"
    assert gptr_recipients(env, "shared-key") == ["OpenRouter", "Tavily"]

    env["FAST_LLM"] = "openai:fixture/model"
    with pytest.raises(Problem, match="Fast role uses OpenAI"):
        gptr_recipients(env, "shared-key")

@pytest.mark.parametrize("refusal", [ConnectionRefusedError(111, "Connection refused"), TimeoutError("timed out")],
                         ids=["posix_refusal", "windows_refusal"])
@pytest.mark.parametrize("name", ["ldr", "gptr"])
def test_launch_treats_loopback_probe_timeout_as_free_port(tmp_path, monkeypatch, refusal, name):
    # Windows reports a refused loopback connect only after SYN retries, so the 0.5 s probe times out.
    root = prepare_root(tmp_path.resolve() / "data")
    install = tmp_path / "install"
    install.mkdir()
    port = upstreams.LDR_PORT if name == "ldr" else upstreams.GPTR_PORT
    spec = upstreams.LaunchSpec(["python", f"{name}_browser.py"], install, {}, f"http://127.0.0.1:{port}/")
    popen = Mock()
    popen.return_value.poll.return_value = None
    monkeypatch.setattr("rah.web.upstreams.current", Mock(return_value=install))
    monkeypatch.setattr("rah.web.upstreams.launch_spec", Mock(return_value=spec))
    monkeypatch.setattr("rah.web.upstreams.refresh_browser", Mock())
    monkeypatch.setattr("rah.web.socket.create_connection", Mock(side_effect=refusal))
    monkeypatch.setattr("rah.web.subprocess.Popen", popen)
    hub = create_app(root, RouteBook.load(), None, "hub", "test", Mock(), lambda: "localhost").test_client()
    result = hub.post(f"/api/apps/{name}/launch", json={},
                      headers={"Authorization": "Bearer test", "Origin": "http://localhost"})
    assert result.status_code == 200, result.get_json()
    assert result.json["state"] == "Starting"
    popen.assert_called_once()
