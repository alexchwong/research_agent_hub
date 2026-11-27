"""Verify the pinned PaperQA2 client serializes recipient restrictions, without network.

With --observer, RAH's OpenRouter usage observer hooks Client.send (as when Runs.invoke wraps the worker), the
worker's tuple guard wraps the observer, and requests are answered at the httpx transport below both, where they are
counted independently. The observer's line count must equal the transport count, and an unapproved request must
still be refused before it reaches the observer or the transport.
"""
import argparse
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from unittest.mock import patch
from verification_report import verification


def blocked(*args, **kwargs):
    raise RuntimeError("Network access is disabled in this probe")


MODELS = {"pqa-budget-v1": ("anthropic/claude-haiku-5.5",) * 3, "pqa-standard-v1": ("anthropic/claude-haiku-5.5", "openai/gpt-6.1-terra", "openai/gpt-6.1-terra"),
          "pqa-premium-v1": ("anthropic/claude-sonnet-5.5",) * 3}


def probe(worker, runner, route="pqa-standard-v1", observer_log=None):
    spec = importlib.util.spec_from_file_location("rah_worker", worker)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import httpx
    from aviary.core import Message, Tool
    from paperqa import Settings

    base = "https://openrouter.ai/api/v1/"
    expected = {}
    destinations = []
    summary, agent, answer = MODELS[route]
    chosen = {"summary": summary, "agent": agent, "answer": answer, "embedding": "openai/text-embedding-3-small"}
    for role in ("answer", "summary", "agent", "embedding"):
        provider = "fixture-" + role
        embedding = role == "embedding"
        endpoint = base + ("embeddings" if embedding else "chat/completions")
        model = chosen[role]
        destinations.append({"role": role, "service": "OpenRouter", "provider": provider, "model": model, "endpoint": endpoint})
        expected[provider] = (endpoint, model)
    profile = {"id": route, "policy": "single-provider-no-fallback", "destinations": destinations}
    settings = Settings(**module.paper_options(profile))
    observed = set()
    transport_count = []

    def intercept(request):
        transport_count.append(str(request.url))
        body = json.loads(request.content)
        provider = body["provider"]["only"][0]
        endpoint, model = expected[provider]
        assert request.headers["Authorization"] == "Bearer synthetic-router-key"
        assert str(request.url) == endpoint and body["model"] == model
        assert body["provider"] == {"only": [provider], "allow_fallbacks": False}
        assert all(key not in body for key in ("extra_body", "models", "route"))
        if provider == "fixture-embedding":
            assert body["input"] == ["synthetic PDF excerpt"]
            response = {"object": "list", "model": model, "usage": {"prompt_tokens": 1, "total_tokens": 1},
                        "data": [{"object": "embedding", "embedding": [.3, .4], "index": 0}]}
        else:
            assert body["messages"][0]["content"] == "synthetic question"
            message = {"role": "assistant", "content": "synthetic answer"}
            reason = "stop"
            if provider == "fixture-agent":
                assert body["tools"][0]["function"]["name"] == "fixture_search"
                message = {"role": "assistant", "tool_calls": [{"id": "fixture-call", "type": "function",
                           "function": {"name": "fixture_search", "arguments": '{"query":"synthetic question"}'}}]}
                reason = "tool_calls"
            response = {"id": "fixture", "object": "chat.completion", "model": model, "created": 1,
                        "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}}
        observed.add(provider)
        return httpx.Response(200, request=request, json=response)

    async def send_async(_client, request, **kwargs):
        return intercept(request)

    def send_sync(_client, request, **kwargs):
        return intercept(request)

    def fixture_search(query: str) -> str:
        """Return a synthetic search result.

        Args:
            query: A synthetic query.
        """
        return query

    async def exercise():
        prompt = [Message(role="user", content="synthetic question")]
        for model in (settings.get_llm(), settings.get_summary_llm()):
            assert (await model.acompletion(prompt))[0].text == "synthetic answer"
        tool = Tool.from_function(fixture_search)
        response = await settings.get_agent_llm().select_tool(prompt, tools=[tool])
        assert response.tool_calls[0].function.name == "fixture_search"
        assert await settings.get_embedding_model().embed_documents(["synthetic PDF excerpt"]) == [[.3, .4]]

    if observer_log is None:
        with patch.object(httpx.AsyncClient, "send", send_async), patch.object(httpx.Client, "send", send_sync):
            with module.paper_transport(profile):
                runner.run(exercise())
        assert observed == expected.keys(), observed
        print(f"PaperQA2 serialized OpenRouter provider restrictions for {route}: answer, summary, agent tools and embeddings.")
        return {}

    async def transport_async(_transport, request):
        await request.aread()
        return intercept(request)

    def transport_sync(_transport, request):
        request.read()
        return intercept(request)

    path = Path(__file__).resolve().parents[1] / "rah" / "openrouter_observer.py"
    spec = importlib.util.spec_from_file_location("openrouter_observer", path)
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    transports = [patch.object(httpx.AsyncHTTPTransport, "handle_async_request", transport_async),
                  patch.object(httpx.HTTPTransport, "handle_request", transport_sync)]
    try:  # litellm's async clients send through its own aiohttp-backed httpx transports.
        from litellm.llms.custom_httpx import aiohttp_transport
        transports += [patch.object(getattr(aiohttp_transport, name), "handle_async_request", transport_async)
                       for name in ("AiohttpTransport", "LiteLLMAiohttpTransport") if hasattr(aiohttp_transport, name)]
    except ImportError:
        pass
    for item in transports:
        item.start()
    with patch.object(httpx.AsyncClient, "send", httpx.AsyncClient.send), patch.object(httpx.Client, "send", httpx.Client.send):
        observer.install(httpx, observer.Log(str(observer_log)))
        with module.paper_transport(profile):
            runner.run(exercise())
            before = len(transport_count)
            refused = False
            try:  # An unapproved tuple: the guard refuses before the observer or the transport sees it.
                with httpx.Client() as client:
                    client.post(base + "chat/completions", json={"model": "unapproved/model",
                                                                 "provider": {"only": ["other"], "allow_fallbacks": False}})
            except ValueError:
                refused = True
    for item in transports:
        item.stop()
    lines = [json.loads(line) for line in Path(observer_log).read_text().splitlines()] if Path(observer_log).exists() else []
    assert observed == expected.keys(), observed
    result = {"transport_requests": len(transport_count), "observer_lines": len(lines), "unapproved_refused": refused,
              "unapproved_reached_transport": len(transport_count) != before,
              "observer_reasons": sorted({str(line.get("reason")) for line in lines})}
    assert refused and not result["unapproved_reached_transport"], result
    assert result["observer_lines"] == result["transport_requests"] > 0, result
    print(f"PaperQA2 {route} under the observer: {len(lines)} observed lines for {len(transport_count)} transport requests.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--route", default="pqa-standard-v1", choices=sorted(MODELS))
    parser.add_argument("--observer", action="store_true", help="Run under rah/openrouter_observer.py and compare counts")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    with verification(args.report, "paperqa_transport") as report:
        with TemporaryDirectory(prefix="rah paper wire ") as home:
            os.environ.update(PQA_HOME=home, OPENROUTER_API_KEY="synthetic-router-key",
                              LITELLM_LOCAL_MODEL_COST_MAP="True", PYTHON_DOTENV_DISABLED="1")
            with asyncio.Runner() as runner:
                runner.get_loop()
                with patch.object(socket.socket, "connect", blocked), patch.object(socket.socket, "connect_ex", blocked), \
                        patch.object(socket, "getaddrinfo", blocked):
                    result = probe(args.worker.resolve(), runner, args.route,
                                   Path(home) / "usage.jsonl" if args.observer else None)
        report["checks"].update(dict.fromkeys(("answer", "summary", "agent_tools", "embedding"), "passed"))
        if args.observer:
            report["checks"].update({"observer_counts_equal": "passed", "guard_before_observer": "passed"})
            report["observer"] = result
