"""Small worker for native calls. This file also runs inside the PaperQA2 venv."""
import json
import os
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


def paper(request, cwd):
    from paperqa import Settings, ask
    from paperqa.settings import MultimodalOptions
    from paperqa_pypdf import parse_pdf_to_pages

    options = paper_options(request["profile"])
    options["agent"]["index"] = {"paper_directory": cwd / "corpus", "index_directory": cwd / "index",
                                 "recurse_subdirectories": False, "name": cwd.name}
    settings = Settings(**options,
                        parsing={"multimodal": MultimodalOptions.OFF, "use_doc_details": False,
                                 "parse_pdf": parse_pdf_to_pages})
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
