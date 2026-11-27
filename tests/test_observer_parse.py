"""T15: the OpenRouter observer passes bytes through unchanged and records only usage metadata."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")

OBSERVER = Path(__file__).resolve().parents[1] / "rah" / "openrouter_observer.py"
CHAT = "https://openrouter.ai/api/v1/chat/completions"
EMBED = "https://openrouter.ai/api/v1/embeddings"


@pytest.fixture
def observer(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("openrouter_observer_under_test", OBSERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    monkeypatch.setattr(httpx.AsyncClient, "send", httpx.AsyncClient.send)
    path = tmp_path / "usage.jsonl"
    module.install(httpx, module.Log(str(path)))
    return lambda: [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def sse(*events):
    return b"".join(b"data: " + (event if isinstance(event, bytes) else json.dumps(event).encode()) + b"\n\n" for event in events)


USAGE = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17, "cost": 0.0042}
STREAM = sse({"id": "gen-1", "model": "m/x", "provider": "P", "choices": [{"delta": {"content": "hi"}}]},
             {"id": "gen-1", "choices": [], "usage": USAGE}, b"[DONE]")


def client(handler, **options):
    return httpx.Client(transport=httpx.MockTransport(handler), **options)


def test_non_streamed_json_usage_and_untouched_bytes(observer):
    body = json.dumps({"id": "gen-2", "model": "m/y", "provider": "Q", "usage": USAGE, "choices": [{"message": {"content": "secret"}}]}).encode()
    with client(lambda request: httpx.Response(200, content=body, headers={"content-type": "application/json"})) as http:
        response = http.post(CHAT, json={"model": "m/y"})
    assert response.content == body
    [line] = observer()
    assert line == {"endpoint": "/api/v1/chat/completions", "status": 200, "stream": False, "id": "gen-2", "model": "m/y",
                    "provider": "Q", "usage": line["usage"], "reason": None}
    assert line["usage"]["cost"] == 0.0042 and "secret" not in json.dumps(line)


def test_stream_byte_identical_and_usage_from_last_chunk(observer):
    pieces = [STREAM[i:i + 7] for i in range(0, len(STREAM), 7)]

    def handler(request):
        return httpx.Response(200, stream=httpx.ByteStream(b"".join(pieces)), headers={"content-type": "text/event-stream"})

    with client(handler) as http, http.stream("POST", CHAT, json={}) as response:
        received = b"".join(response.iter_raw())
    assert received == STREAM
    [line] = observer()
    assert line["stream"] is True and line["usage"]["completion_tokens"] == 5 and line["id"] == "gen-1" and line["reason"] is None


def test_async_stream_and_aclose(observer):
    async def run():
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                for index in range(0, len(STREAM), 5):
                    yield STREAM[index:index + 5]

        async def handler(request):
            return httpx.Response(200, stream=Chunks(), headers={"content-type": "text/event-stream"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with http.stream("POST", CHAT, json={}) as response:
                return b"".join([chunk async for chunk in response.aiter_raw()])
    assert asyncio.run(run()) == STREAM
    [line] = observer()
    assert line["usage"]["prompt_tokens"] == 12 and line["reason"] is None


def test_early_close_records_aborted(observer):
    first = sse({"id": "gen-3", "model": "m/z", "choices": [{"delta": {"content": "a"}}]})

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            yield first
            yield first

    with client(lambda request: httpx.Response(200, stream=Slow(), headers={"content-type": "text/event-stream"})) as http:
        with http.stream("POST", CHAT, json={}) as response:
            next(response.iter_raw())
    [line] = observer()
    assert line["reason"] == "aborted" and line["usage"] is None


def test_exception_from_stream_propagates_unchanged(observer):
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"data: {}\n\n"
            raise httpx.ReadError("cut")

    with client(lambda request: httpx.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"})) as http:
        with pytest.raises(httpx.ReadError):
            with http.stream("POST", CHAT, json={}) as response:
                response.read()
    assert observer()[0]["reason"] == "aborted"


def test_connect_error_is_no_response_and_reraised(observer):
    def handler(request):
        raise httpx.ConnectError("refused")
    with client(handler) as http, pytest.raises(httpx.ConnectError):
        http.post(CHAT, json={})
    assert observer() == [{"endpoint": "/api/v1/chat/completions", "status": None, "stream": None, "id": None,
                           "model": None, "provider": None, "usage": None, "reason": "no_response"}]


def test_line_overflow_and_malformed_sse_are_unparsed_but_pass_through(observer):
    huge = b"data: " + b"x" * (70 * 1024) + b"\n\n" + STREAM
    with client(lambda request: httpx.Response(200, stream=httpx.ByteStream(huge), headers={"content-type": "text/event-stream"})) as http:
        with http.stream("POST", CHAT, json={}) as response:
            assert b"".join(response.iter_raw()) == huge
    assert observer()[0]["reason"] == "unparsed"


def test_missing_usage_and_usage_before_done(observer):
    without = sse({"id": "g", "choices": []}, b"[DONE]")
    with client(lambda request: httpx.Response(200, content=without, headers={"content-type": "text/event-stream"})) as http:
        with http.stream("POST", CHAT, json={}) as response:
            response.read()
    assert observer()[0]["usage"] is None and observer()[0]["reason"] == "no_usage"


def test_oversize_json_body_is_too_large(observer):
    body = b'{"usage": {"cost": 1}, "pad": "' + b"x" * (16 * 1024 * 1024 + 10) + b'"}'
    with client(lambda request: httpx.Response(200, content=body, headers={"content-type": "application/json"})) as http:
        assert http.post(EMBED, json={}).content == body
    assert observer()[0]["reason"] == "too_large"


def test_other_traffic_is_not_observed(observer):
    with client(lambda request: httpx.Response(200, content=b"{}")) as http:
        http.get(CHAT)
        http.post("https://example.org/api/v1/chat/completions", json={})
        http.post("https://openrouter.ai/api/v1/credits", json={})
    assert observer() == []
