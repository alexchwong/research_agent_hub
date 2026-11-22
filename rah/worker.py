"""Small worker for native calls. This file also runs inside the PaperQA2 venv."""
from contextlib import contextmanager
from importlib.metadata import version
import json
import os
import platform
from pathlib import Path
import subprocess
import sys


def save(path, payload):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    temporary.replace(path)


def paper_options(profile):
    if profile["id"] != "openrouter-text-v1":
        raise ValueError("Unexpected managed PaperQA2 profile")
    destinations = {item["role"]: item for item in profile["destinations"]}
    if set(destinations) != {"answer", "summary", "agent", "embedding"}:
        raise ValueError("Incomplete managed PaperQA2 route")

    def route(role):
        item = destinations[role]
        url = "https://openrouter.ai/api/v1"
        endpoint = "/embeddings" if role == "embedding" else "/chat/completions"
        if item["endpoint"] != url + endpoint or item["service"] != "OpenRouter":
            raise ValueError("Unexpected managed PaperQA2 endpoint")
        model = "openrouter/" + item["model"]
        body = {"provider": {"only": [item["provider"]], "allow_fallbacks": False}}
        return model, body

    def chat(role):
        model, body = route(role)
        config = {
            "model_list": [{"model_name": model, "litellm_params": {"model": model, "extra_body": body}}],
            "router_kwargs": {"num_retries": 0},
        }
        return model, config

    answer, answer_config = chat("answer")
    summary, summary_config = chat("summary")
    agent, agent_config = chat("agent")
    # The pinned LiteLLM embedding adapter forwards these fields at the top level.
    embedding, body = route("embedding")
    return {
        "llm": answer, "llm_config": answer_config, "summary_llm": summary, "summary_llm_config": summary_config,
        "embedding": embedding, "embedding_config": {"kwargs": body},
        "agent": {"agent_llm": agent, "agent_llm_config": agent_config},
    }


@contextmanager
def paper_transport(profile):
    import httpx

    allowed = {(item["endpoint"], item["model"], item["provider"])
               for item in profile["destinations"]}
    send = httpx.Client.send
    send_async = httpx.AsyncClient.send

    def check(request):
        body = json.loads(request.content)
        provider = body.get("provider")
        if (request.method != "POST" or not isinstance(provider, dict)
                or provider.get("allow_fallbacks") is not False
                or not isinstance(provider.get("only"), list)
                or len(provider["only"]) != 1
                or (str(request.url), body.get("model"), provider["only"][0]) not in allowed
                or body.get("models")):
            raise ValueError("PaperQA2 attempted an unapproved request")

    def reject_redirect(response):
        if response.has_redirect_location:
            raise ValueError("PaperQA2 attempted a redirect")
        return response

    def guarded(client, request, **kwargs):
        check(request)
        return reject_redirect(send(client, request, **(kwargs | {"follow_redirects": False})))

    async def guarded_async(client, request, **kwargs):
        check(request)
        return reject_redirect(await send_async(client, request, **(kwargs | {"follow_redirects": False})))

    httpx.Client.send, httpx.AsyncClient.send = guarded, guarded_async
    try:
        yield
    finally:
        httpx.Client.send, httpx.AsyncClient.send = send, send_async


def paper(request, cwd):
    profile = request["profile"]
    if profile.get("policy") == "single-provider-no-fallback":
        pins = {"paper-qa": profile["upstream_version"], "fhlmi": "0.46.2",
                "fhaviary": "0.37.0", "litellm": "1.84.1",
                "paper-qa-pypdf": profile["upstream_version"], "pypdf": "7.1.1"}
        if profile["python"] != platform.python_version() or any(version(name) != pin for name, pin in pins.items()):
            raise ValueError("PaperQA2 runtime differs from the verified profile")
    from paperqa import Settings, ask
    from paperqa.settings import MultimodalOptions
    from paperqa_pypdf import parse_pdf_to_pages

    options = paper_options(request["profile"])
    options["agent"]["index"] = {"paper_directory": cwd / "corpus", "index_directory": cwd / "index",
                                 "recurse_subdirectories": False, "name": cwd.name}
    settings = Settings(**options,
                        parsing={"multimodal": MultimodalOptions.OFF, "use_doc_details": False,
                                 "parse_pdf": parse_pdf_to_pages})
    with paper_transport(request["profile"]):
        result = ask(request["question"], settings)
    if str(result.status) != "success" or not result.session.formatted_answer:
        raise ValueError("PaperQA2 did not return an answer")
    bib = result.bibtex or {}
    save(cwd / "result.json", {
        "answer": result.session.formatted_answer, "bib": "\n\n".join(bib.values()),
    })


def main():
    cwd = Path.cwd()
    succeeded = False
    try:
        request = json.load(sys.stdin)
        if request["app"] == "pqa":
            paper(request, cwd)
        else:
            subprocess.run(request["command"], cwd=cwd, env=request["env"], check=True,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        succeeded = True
    finally:
        save(cwd / "completion.json", {"succeeded": succeeded})


if __name__ == "__main__":
    main()
