"""Managed PaperQA2 profile worker. Runs inside the PaperQA2 venv with the approved subset only.

Every outgoing request must match an approved (endpoint, model, provider) tuple with fallbacks disabled.
Redirects and model lists are refused. The worker writes the native answer, its BibTeX and observable
coverage; it never generates citations itself.
"""
from contextlib import contextmanager
import hashlib
from importlib.metadata import version
import json
import os
import platform
from pathlib import Path
import sys


ROUTES = {"pqa-budget-v1", "pqa-standard-v1", "pqa-premium-v1"}
BASE = "https://openrouter.ai/api/v1"
PINS = {"fhlmi": "0.46.2", "fhaviary": "0.37.0", "litellm": "1.84.1", "pypdf": "7.1.1"}


def save(path, payload):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    temporary.replace(path)


def paper_options(profile):
    if profile["id"] not in ROUTES or profile.get("policy") != "single-provider-no-fallback":
        raise ValueError("Unexpected managed PaperQA2 profile")
    destinations = {item["role"]: item for item in profile["destinations"]}
    if set(destinations) != {"answer", "summary", "agent", "embedding"} or len(profile["destinations"]) != 4:
        raise ValueError("Incomplete managed PaperQA2 route")

    def route(role):
        item = destinations[role]
        endpoint = "/embeddings" if role == "embedding" else "/chat/completions"
        if item["endpoint"] != BASE + endpoint or item["service"] != "OpenRouter" or not item.get("provider"):
            raise ValueError("Unexpected managed PaperQA2 endpoint")
        return "openrouter/" + item["model"], {"provider": {"only": [item["provider"]], "allow_fallbacks": False}}

    def chat(role):
        model, body = route(role)
        return model, {"model_list": [{"model_name": model, "litellm_params": {"model": model, "extra_body": body}}],
                       "router_kwargs": {"num_retries": 0}}

    answer, answer_config = chat("answer")
    summary, summary_config = chat("summary")
    agent, agent_config = chat("agent")
    # The pinned LiteLLM embedding adapter forwards these fields at the top level.
    embedding, body = route("embedding")
    return {"llm": answer, "llm_config": answer_config, "summary_llm": summary, "summary_llm_config": summary_config,
            "embedding": embedding, "embedding_config": {"kwargs": body},
            "agent": {"agent_llm": agent, "agent_llm_config": agent_config}}


@contextmanager
def paper_transport(profile, observed=None):
    import httpx

    allowed = {(item["endpoint"], item["model"], item["provider"]) for item in profile["destinations"]}
    send, send_async = httpx.Client.send, httpx.AsyncClient.send

    def check(request):
        body = json.loads(request.content)
        provider = body.get("provider")
        if (request.method != "POST" or not isinstance(provider, dict) or provider.get("allow_fallbacks") is not False
                or not isinstance(provider.get("only"), list) or len(provider["only"]) != 1
                or (str(request.url), body.get("model"), provider["only"][0]) not in allowed
                or body.get("models") or body.get("route")):
            raise ValueError("PaperQA2 attempted an unapproved request")

    def note(response):
        if response.has_redirect_location:
            raise ValueError("PaperQA2 attempted a redirect")
        if observed is not None:
            observed.append(str(response.request.url))
        return response

    def guarded(client, request, **kwargs):
        check(request)
        return note(send(client, request, **(kwargs | {"follow_redirects": False})))

    async def guarded_async(client, request, **kwargs):
        check(request)
        return note(await send_async(client, request, **(kwargs | {"follow_redirects": False})))

    httpx.Client.send, httpx.AsyncClient.send = guarded, guarded_async
    try:
        yield
    finally:
        httpx.Client.send, httpx.AsyncClient.send = send, send_async


def coverage(result, corpus):
    """Observable per-file coverage. Unobservable fields stay null rather than guessed."""
    by_md5 = {}
    for path in corpus.iterdir():
        with path.open("rb") as handle:
            by_md5[hashlib.file_digest(handle, "md5").hexdigest()] = path.name
    used = set(result.session.used_contexts)
    cited, reviewed, observable = set(), set(), True
    for context in result.session.contexts:
        doc = getattr(context.text, "doc", None)
        name = by_md5.get(getattr(doc, "content_hash", None) or "")
        if name is None:
            location = getattr(doc, "file_location", None)
            name = Path(str(location)).name if location else None
        if name is None:
            observable = False
            continue
        reviewed.add(name)
        if context.id in used:
            cited.add(name)
    files = sorted(path.name for path in corpus.iterdir())
    return {"submitted": files, "cited": sorted(cited) if observable else None,
            "reviewed": sorted(reviewed) if observable else None}


def paper(request, cwd):
    profile = request["profile"]
    pins = PINS | {"paper-qa": profile["upstream_version"], "paper-qa-pypdf": profile["upstream_version"]}
    if profile["python"] != platform.python_version() or any(version(name) != pin for name, pin in pins.items()):
        raise ValueError("PaperQA2 runtime differs from the reviewed profile")
    from paperqa import Settings, ask
    from paperqa.settings import MultimodalOptions
    from paperqa_pypdf import parse_pdf_to_pages

    options = paper_options(profile)
    corpus = cwd / "corpus"
    options["agent"]["index"] = {"paper_directory": corpus, "index_directory": cwd / "index",
                                 "recurse_subdirectories": False, "name": cwd.name}
    settings = Settings(**options, parsing={"multimodal": MultimodalOptions.OFF, "use_doc_details": False,
                                            "parse_pdf": parse_pdf_to_pages})
    observed = []
    with paper_transport(profile, observed):
        result = ask(request["question"], settings)
    if str(result.status) != "success" or not result.session.formatted_answer:
        raise ValueError("PaperQA2 did not return an answer")
    bib = result.bibtex or {}
    save(cwd / "result.json", {"answer": result.session.formatted_answer, "bib": "\n\n".join(bib.values()),
                               "coverage": coverage(result, corpus), "requests": len(observed)})


def main():
    cwd = Path.cwd()
    succeeded = False
    try:
        request = json.load(sys.stdin)
        if request.get("app") != "pqa":
            raise ValueError("The profile worker only runs PaperQA2")
        paper(request, cwd)
        succeeded = True
    finally:
        save(cwd / "completion.json", {"succeeded": succeeded})


if __name__ == "__main__":
    main()
