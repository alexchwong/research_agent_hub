"""P0 probe: run the pinned GPT Researcher CLI in-process with the guided survey projection.

Model, embedding and retriever traffic is intercepted at the installed package transports (httpx for the
OpenAI-compatible clients, requests for the PubMed Central retriever). No production endpoint is overridden and
real sockets are blocked. Writes a JSON report of effective settings and observed request bodies.

With --observer the CLI runs under RAH's OpenRouter usage observer, which hooks Client.send above these transports.
--compare takes the report of a run without the observer: outputs, request bodies and effective settings must be
byte-identical, and the observer's line count must equal the chat plus embeddings requests counted here. The probe
exits non-zero on failure.
"""
import hashlib
import importlib.util
import re
import argparse
import json
import os
from pathlib import Path
import runpy
import socket
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch


VOLATILE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?"
                      r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def stable(text):
    """Digest with the upstream's own clock values and run IDs masked: they differ between any two runs."""
    return hashlib.sha256(VOLATILE.sub("<volatile>", text).encode()).hexdigest()


def blocked(*args, **kwargs):
    raise RuntimeError("Network access is disabled in this probe")


ARTICLE = ("<pmc-articleset><article><front><article-meta><title-group><article-title>Synthetic trial</article-title>"
           "</title-group><abstract><p>Synthetic abstract.</p></abstract></article-meta></front><body><p>Synthetic body text "
           "about the topic with enough words to embed.</p></body></article></pmc-articleset>")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--env", type=Path, required=True, help="JSON object of the guided projection to apply")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--mode", default="research_report")
    parser.add_argument("--observer", action="store_true", help="Run the CLI under rah/openrouter_observer.py")
    parser.add_argument("--compare", type=Path, help="Report of the same run without the observer")
    args = parser.parse_args()
    bodies = []
    env = json.loads(args.env.read_text())
    observed = {"chat": [], "embeddings": [], "retriever": [], "other": []}
    import httpx
    import requests
    try:
        import httpx2  # The pinned openai SDK sends through its vendored-name httpx2 package.
    except ImportError:
        httpx2 = None

    def respond(request):
        module = httpx2 if httpx2 and isinstance(request, httpx2.Request) else httpx
        url = str(request.url)
        body = json.loads(request.content or b"{}")
        if url.startswith("https://openrouter.ai/"):
            bodies.append(stable((request.content or b"").decode(errors="replace")))
        if url == "https://openrouter.ai/api/v1/chat/completions":
            observed["chat"].append({"model": body.get("model"), "max_tokens": body.get("max_tokens"),
                                     "max_completion_tokens": body.get("max_completion_tokens"),
                                     "temperature": body.get("temperature"), "reasoning_effort": body.get("reasoning_effort"),
                                     "stream": body.get("stream"), "keys": sorted(body)})
            text = '["synthetic query"]' if "queries" in json.dumps(body.get("messages", [])).lower() else "# Synthetic report\n\nBody."
            if body.get("stream"):
                chunk = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": body.get("model"),
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
                done = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": body.get("model"),
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                payload = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode()
                return module.Response(200, request=request, content=payload, headers={"content-type": "text/event-stream"})
            return module.Response(200, request=request, json={"id": "x", "object": "chat.completion", "created": 1, "model": body.get("model"),
                                  "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}})
        if url == "https://openrouter.ai/api/v1/embeddings":
            inputs = body.get("input") if isinstance(body.get("input"), list) else [body.get("input")]
            observed["embeddings"].append({"model": body.get("model"), "inputs": len(inputs)})
            return module.Response(200, request=request, json={"object": "list", "model": body.get("model"),
                                  "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]} for i in range(len(inputs))],
                                  "usage": {"prompt_tokens": 1, "total_tokens": 1}})
        observed["other"].append(url.split("?")[0])
        return module.Response(404, request=request)

    async def send_async(_transport, request):
        await request.aread()
        return respond(request)

    def send_sync(_transport, request):
        request.read()
        return respond(request)

    def requests_send(_session, prepared, **kwargs):
        response = requests.Response()
        response.request = prepared
        response.url = prepared.url
        base = prepared.url.split("?")[0]
        if "openaipublic.blob.core.windows.net" in base:
            observed["other"].append(base)
        observed["retriever"].append({"url": base, "query": prepared.url.split("?", 1)[1] if "?" in prepared.url else ""})
        if base.endswith("esearch.fcgi"):
            response.status_code, response._content = 200, json.dumps({"esearchresult": {"idlist": ["1", "2", "3"]}}).encode()
        elif base.endswith("efetch.fcgi"):
            response.status_code, response._content = 200, ARTICLE.encode()
        else:
            response.status_code, response._content = 404, b""
        response.encoding = "utf-8"
        return response

    with TemporaryDirectory(prefix="rah gptr probe ") as work:
        os.chdir(work)
        os.environ.clear()
        os.environ.update(env | {"DOC_PATH": str(Path(work) / "my-docs"), "HOME": work})
        sys.argv = [str(args.source / "cli.py"), "--report_type", args.mode, "--report_source", "web", "--no-pdf", "--no-docx",
                    "--", "Synthetic public question"]
        sys.path.insert(0, str(args.source))
        status = "passed"
        error = None
        patches = [patch.object(httpx.AsyncHTTPTransport, "handle_async_request", send_async),
                   patch.object(httpx.HTTPTransport, "handle_request", send_sync)]
        if httpx2:
            patches += [patch.object(httpx2.AsyncHTTPTransport, "handle_async_request", send_async),
                        patch.object(httpx2.HTTPTransport, "handle_request", send_sync)]
        import tiktoken

        class Tokens:
            """Byte-level stand-in: the pinned tokenizer otherwise downloads its BPE file on first use."""
            name = "probe-bytes"

            def encode(self, text, **kwargs):
                return list(text.encode())

            def decode(self, tokens):
                return bytes(tokens).decode(errors="replace")

        patches += [patch.object(tiktoken, "get_encoding", lambda name: Tokens()),
                    patch.object(tiktoken, "encoding_for_model", lambda name: Tokens())]
        for item in patches:
            item.start()
        usage_log = Path(work) / "usage.jsonl"
        if args.observer:
            path = Path(__file__).resolve().parents[1] / "rah" / "openrouter_observer.py"
            spec = importlib.util.spec_from_file_location("openrouter_observer", path)
            observer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(observer)
            log = observer.Log(str(usage_log))
            for module in (httpx, httpx2):
                if module:
                    observer.install(module, log)
        with \
                patch.object(requests.Session, "send", requests_send), patch.object(socket.socket, "connect", blocked), \
                patch.object(socket, "getaddrinfo", blocked):
            try:
                runpy.run_path(str(args.source / "cli.py"), run_name="__main__")
            except SystemExit as exit_status:
                status = "passed" if not exit_status.code else "failed"
            except Exception as exception:  # Record the class only; no payloads.
                status, error = "failed", type(exception).__name__
        outputs = sorted(path.name for path in (Path(work) / "outputs").glob("*.md")) if (Path(work) / "outputs").exists() else []
        frontmatter = (Path(work) / "outputs" / outputs[0]).read_text().split("---")[1] if outputs else ""
        output_digests = sorted(stable((Path(work) / "outputs" / name).read_text(errors="replace")) for name in outputs)
        lines = [json.loads(line) for line in usage_log.read_text().splitlines()] if usage_log.exists() else []
    from gpt_researcher.config.config import Config
    os.environ.update(env)
    config = Config()
    effective = {name: getattr(config, name.lower(), None) for name in (
        "MAX_ITERATIONS", "MAX_SEARCH_RESULTS_PER_QUERY", "MAX_SCRAPER_WORKERS", "BROWSE_CHUNK_MAX_LENGTH", "TOTAL_WORDS",
        "CURATE_SOURCES", "FAST_TOKEN_LIMIT", "SMART_TOKEN_LIMIT", "STRATEGIC_TOKEN_LIMIT", "SUMMARY_TOKEN_LIMIT", "LLM_KWARGS",
        "IMAGE_GENERATION_ENABLED", "MCP_STRATEGY")}
    report = {"status": status, "error": error, "outputs": outputs, "frontmatter_has_sources_count": "sources_count" in frontmatter,
              "effective": effective, "fast": config.fast_llm, "smart": config.smart_llm, "strategic": config.strategic_llm,
              "embedding": config.embedding, "retrievers": config.retrievers, "observed": observed,
              "observer": args.observer, "request_body_sha256": sorted(bodies), "output_sha256": output_digests,
              "observer_lines": len(lines), "observer_reasons": sorted({str(line.get("reason")) for line in lines})}
    checks = {"run_passed": status == "passed"}
    if args.observer:
        checks["counts_equal"] = len(lines) == len(observed["chat"]) + len(observed["embeddings"]) and len(lines) > 0
    if args.compare:
        baseline = json.loads(args.compare.read_text())
        checks["request_bodies_identical"] = baseline["request_body_sha256"] == report["request_body_sha256"]
        checks["outputs_identical"] = baseline["output_sha256"] == report["output_sha256"]
        checks["effective_identical"] = json.dumps(baseline["effective"], sort_keys=True, default=str) == \
            json.dumps(json.loads(json.dumps(effective, default=str)), sort_keys=True)
    report["checks"] = checks
    report["passed"] = all(checks.values())
    args.report.write_text(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
