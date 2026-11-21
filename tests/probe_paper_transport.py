"""Verify the pinned PaperQA2 client serializes recipient restrictions, without network."""
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


def probe(worker, runner):
    spec = importlib.util.spec_from_file_location("rah_worker", worker)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import httpx
    from aviary.core import Message, Tool
    from paperqa import Settings

    base = "https://openrouter.ai/api/v1/"
    expected = {}
    destinations = []
    for role in ("answer", "summary", "agent", "embedding"):
        provider = "fixture-" + role
        embedding = role == "embedding"
        endpoint = base + ("embeddings" if embedding else "chat/completions")
        model = "openai/text-embedding-3-small" if embedding else "openai/gpt-4.1-mini"
        destinations.append({"role": role, "service": "OpenRouter", "provider": provider, "model": model, "endpoint": endpoint})
        expected[provider] = (endpoint, model)
    settings = Settings(**module.paper_options({"id": "openrouter-text-v1", "destinations": destinations}))
    observed = set()

    def intercept(request):
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

    with patch.object(httpx.AsyncClient, "send", send_async), patch.object(httpx.Client, "send", send_sync):
        runner.run(exercise())
    assert observed == expected.keys(), observed
    print("PaperQA2 serialized OpenRouter provider restrictions for answer, summary, agent tools and embeddings.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--report", type=Path)
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
                    probe(args.worker.resolve(), runner)
        report["checks"].update(dict.fromkeys(("answer", "summary", "agent_tools", "embedding"), "passed"))
