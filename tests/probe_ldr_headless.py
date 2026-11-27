"""Installed pinned-upstream probe 1 (spec v2 §8.2): headless LDR through the RAH runner and observer.

Run inside the pinned LDR venv:

    <ldr venv python> tests/probe_ldr_headless.py --report probe-ldr-headless.json

The probe runs ``openrouter_observer.py -- ldr_guided.py`` exactly as RAH does, in a private attempt folder with the
RAH projection. Inside that child, sockets are blocked and fake OpenRouter and PubMed responses are served at the
transport layer (httpx/httpx2 transports and ``requests.Session.send``), below the observer's ``Client.send`` hook, so
the request counts are independent of the observer. It writes one machine-readable report and exits non-zero on
failure. On a pass it prints the ``usage.COVERAGE`` entry for owner review; it never edits RAH.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
RAH = HERE.parent / "rah"
OPENROUTER_HOST = "openrouter.ai"
PUBLIC_IP = "130.14.29.110"
QUESTION = "Effects of a synthetic drug X on synthetic outcome Y in adults"


# ---- child side: fakes below the observer -------------------------------------------------------------------------

def chat_reply(body):
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    used_tool = any(isinstance(item, dict) and item.get("role") == "tool" for item in messages)
    usage = {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52, "cost": 0.000123}
    message = {"role": "assistant", "content": "Synthetic finding: drug X improved outcome Y in one trial [1]."}
    finish = "stop"
    if tools and not used_tool:
        names = [tool.get("function", {}).get("name") for tool in tools]
        name = next((item for item in names if item and "search" in item), names[0])
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps({"query": "drug X outcome Y"} if "subtopic" not in name
                                                                else {"subtopics": ["drug X outcome Y"]})}}]}
        finish = "tool_calls"
    return {"id": "gen-probe", "object": "chat.completion", "created": 0, "model": body.get("model"), "provider": "ProbeProvider",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}


def sse(reply):
    first = dict(reply, object="chat.completion.chunk", choices=[{"index": 0, "delta": reply["choices"][0]["message"],
                                                                  "finish_reason": reply["choices"][0]["finish_reason"]}])
    first.pop("usage")
    last = {"id": reply["id"], "object": "chat.completion.chunk", "model": reply["model"], "choices": [], "usage": reply["usage"]}
    return ("data: " + json.dumps(first) + "\n\ndata: " + json.dumps(last) + "\n\ndata: [DONE]\n\n").encode()


def pubmed(url, params):
    path = urlsplit(url).path
    if path.endswith("esearch.fcgi"):
        return "application/json", json.dumps({"esearchresult": {"idlist": ["555"], "count": "1", "querytranslation": "x"}})
    if path.endswith("esummary.fcgi"):
        return "application/json", json.dumps({"result": {"uids": ["555"], "555": {
            "uid": "555", "title": "Public trial of drug X", "authors": [{"name": "Doe J"}], "fulljournalname": "Synthetic J",
            "pubdate": "2020", "source": "Synth J", "articleids": [{"idtype": "doi", "value": "10.1000/xyz"}]}}})
    if path.endswith("efetch.fcgi"):
        return "application/xml", ("<?xml version='1.0'?><PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>555</PMID>"
                                   "<Article><ArticleTitle>Public trial of drug X</ArticleTitle><Abstract><AbstractText>"
                                   "Drug X improved outcome Y.</AbstractText></Abstract></Article></MedlineCitation>"
                                   "</PubmedArticle></PubmedArticleSet>")
    if path.endswith("elink.fcgi"):
        return "application/json", json.dumps({"linksets": []})
    return None, None


def shim(attempt, record_path):
    """Runs inside the observed child: install fakes, then run the RAH runner unchanged."""
    record = {"transport": [], "hosts": [], "refused": [], "outside_writes": [], "callbacks": [], "effective": {}, "errors": []}
    attempt = Path(attempt).resolve()
    allowed = (str(attempt), "/dev/null", "/dev/")

    def audit(event, args):
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            path, mode = os.fsdecode(args[0]), args[1] if len(args) > 1 else "r"
            flags = args[2] if len(args) > 2 else 0
            writing = (isinstance(mode, str) and any(ch in mode for ch in "wax+")) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR))
            if writing and not os.path.realpath(path).startswith(allowed):
                record["outside_writes"].append(path)
    sys.addaudithook(audit)

    def refuse(*args, **kwargs):
        record["refused"].append(repr(args[1] if len(args) > 1 else args)[:200])
        raise OSError("Network access is disabled in this probe")
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host in ("localhost", "127.0.0.1", "::1"):
            return real_getaddrinfo(host, port, *args, **kwargs)
        record["hosts"].append({"host": host, "kind": "dns"})
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 443))]
    socket.getaddrinfo = resolve

    def caller():
        """The innermost LDR module that made the request: evidence for classifying the host."""
        for frame in reversed(traceback.extract_stack(limit=80)):
            if "local_deep_research" in frame.filename and "security" not in frame.filename:
                return frame.filename.split("local_deep_research", 1)[1].lstrip("/\\")
        return None

    def note(method, url, transport):
        host = urlsplit(str(url)).hostname or ""
        origin = caller()
        # Fixed hosts are those LDR's own code contacts for this configuration (model, search API, reference data);
        # fetched pages are links taken from search results (LDR's content fetchers).
        page = bool(origin) and any(part in origin for part in ("content_fetcher", "full_search", "fetch_tool", "web_fetch"))
        kind = "model" if host == OPENROUTER_HOST else ("page_fetch" if page else "fixed")
        record["transport"].append({"method": method, "host": host, "path": urlsplit(str(url)).path, "via": transport,
                                    "kind": kind, "caller": origin})
        return host

    stream_once = {"done": False}

    def fake_httpx(module, name):
        def respond(request):
            host = note(request.method, request.url, name)
            if host == OPENROUTER_HOST and request.method == "POST":
                body = json.loads(request.content or b"{}")
                if urlsplit(str(request.url)).path.endswith("/embeddings"):
                    data = {"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.0] * 8}],
                            "model": body.get("model"), "usage": {"prompt_tokens": 3, "total_tokens": 3, "cost": 0.000001}}
                    return module.Response(200, json=data, request=request)
                reply = chat_reply(body)
                if body.get("stream"):
                    stream_once["done"] = True
                    return module.Response(200, content=sse(reply), headers={"content-type": "text/event-stream"}, request=request)
                return module.Response(200, json=reply, request=request)
            return module.Response(404, content=b"", request=request)

        def handle(self, request):
            return respond(request)

        async def handle_async(self, request):
            return respond(request)
        module.HTTPTransport.handle_request = handle
        module.AsyncHTTPTransport.handle_async_request = handle_async

    for name in ("httpx", "httpx2"):
        try:
            fake_httpx(__import__(name), name)
        except ImportError:
            pass

    import requests

    def send(session, prepared, **kwargs):
        host = note(prepared.method, prepared.url, "requests")
        response = requests.Response()
        response.url = prepared.url
        response.request = prepared
        media, text = pubmed(prepared.url, None) if host == "eutils.ncbi.nlm.nih.gov" else (None, None)
        response.status_code = 200 if text is not None else 404
        response._content = (text or "").encode()
        response.headers["Content-Type"] = media or "text/plain"
        response.encoding = "utf-8"
        return response
    requests.Session.send = send

    from local_deep_research.api import research_functions
    original = research_functions.quick_summary

    def observed(query, progress_callback=None, **kwargs):
        def shape(*args, **kw):
            record["callbacks"].append([type(value).__name__ for value in args] + sorted(kw))
            return progress_callback(*args, **kw)
        return original(query=query, progress_callback=shape, **kwargs)
    research_functions.quick_summary = observed

    try:
        from local_deep_research.api.settings_utils import create_settings_snapshot, extract_setting_value
        snapshot = create_settings_snapshot()
        record["effective"] = {key: extract_setting_value(snapshot, key) for key in
                               ("search.search_strategy", "llm.provider", "llm.model", "search.tool")}
    except Exception as error:  # Class only.
        record["errors"].append("settings:" + type(error).__name__)
    code = 0
    try:
        sys.argv = [str(attempt / "ldr_guided.py")]
        runpy.run_path(str(attempt / "ldr_guided.py"), run_name="__main__")
    except SystemExit as stop:
        code = stop.code or 0
    finally:
        record["streamed"] = stream_once["done"]
        Path(record_path).write_text(json.dumps(record))
    raise SystemExit(code)


# ---- parent side -------------------------------------------------------------------------------------------------

RAH_SIDE = r"""
import json, sys
from pathlib import Path
from rah import ldr_pipeline, usage
from rah.installer import RouteBook
from rah.projects import structured_citations, usable_citation
route = RouteBook.load().route("ldr") or {}
result = {"observer_sha256": usage.observer_digest(), "fixed_hosts": list(ldr_pipeline.FIXED_HOSTS.get("pubmed", ())),
          "route_lock_sha256": route.get("lock_sha256"), "route_version": route.get("upstream_version"),
          "ris_records": 0, "ris_usable_rows": 0}
if sys.argv[1] != "-":
    try:
        parsed = structured_citations(Path(sys.argv[1]), overflow="truncate")
        result.update(ris_records=parsed.total, ris_usable_rows=sum(1 for row in parsed.rows if usable_citation(row)))
    except Exception as error:
        result["ris_error"] = type(error).__name__
print(json.dumps(result))
"""


def rah_side(python, ris):
    """RAH's own parser and constants run in the hub environment (the LDR venv does not contain RAH's dependencies)."""
    completed = subprocess.run([python, "-c", RAH_SIDE, str(ris) if ris.exists() else "-"], cwd=RAH.parent,
                               capture_output=True, timeout=120)
    if completed.returncode:
        raise RuntimeError("RAH-side evaluation failed: " + completed.stderr.decode(errors="replace")[-300:])
    return json.loads(completed.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--shim", nargs=2, metavar=("ATTEMPT", "RECORD"))
    parser.add_argument("--rah-python", default=sys.executable, help="Python of the RAH development environment")
    parser.add_argument("--keep", type=Path, help="Copy the attempt's outputs and usage log here as evidence")
    args, _ = parser.parse_known_args()
    if args.shim:
        return shim(*args.shim)
    report = {"probe": "ldr_headless", "python": platform.python_version(), "host": platform.platform(), "checks": {}}
    try:
        import local_deep_research
        report["ldr_version"] = getattr(local_deep_research, "__version__", None)
        wheel = [path for path in Path(local_deep_research.__file__).parents[1].glob("local_deep_research-*.dist-info")]
        report["dist_info"] = [path.name for path in wheel]
    except ImportError as error:
        report["error"] = "ldr_not_importable:" + type(error).__name__
    work = Path(tempfile.mkdtemp(prefix="rah-ldr-probe-"))
    try:
        attempt = work / "attempt"
        home = attempt / "home"
        (home / "ldr-data").mkdir(parents=True)
        for name in ("ldr_guided.py", "openrouter_observer.py"):
            shutil.copyfile(RAH / name, attempt / name)
        shutil.copyfile(Path(__file__), attempt / "probe_shim.py")
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if key in os.environ}
        env.update({name: str(home) for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                                                  "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR", "TEMP", "TMP")})
        env.update({"LDR_DATA_DIR": str(home / "ldr-data"), "PYTHONNOUSERSITE": "1", "PYTHON_DOTENV_DISABLED": "1", "NO_PROXY": "*",
                    "PYTHONDONTWRITEBYTECODE": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                    "LDR_LLM_PROVIDER": "openrouter", "LDR_LLM_MODEL": "deepseek/deepseek-v4.1-flash-1017",
                    "LDR_LLM_OPENROUTER_API_KEY": "probe-key-not-real", "LDR_SEARCH_TOOL": "pubmed"})
        record_path = work / "record.json"
        argv = [sys.executable, str(attempt / "openrouter_observer.py"), "--out", str(attempt / "usage.jsonl"), "--",
                str(attempt / "probe_shim.py"), "--report", "-", "--shim", str(attempt), str(record_path)]
        completed = subprocess.run(argv, cwd=attempt, env=env, input=json.dumps({"prompt": QUESTION}).encode(),
                                   capture_output=True, timeout=900)
        report["exit_code"] = completed.returncode
        report["stderr_tail"] = completed.stderr.decode(errors="replace")[-1500:]
        record = json.loads(record_path.read_text()) if record_path.exists() else {}
        completion = json.loads((attempt / "completion.json").read_text()) if (attempt / "completion.json").exists() else None
        report["completion"] = completion
        report_md = attempt / "outputs" / "report.md"
        ris = attempt / "outputs" / "citations.ris"
        hub = rah_side(args.rah_python, ris)
        total, usable = hub["ris_records"], hub["ris_usable_rows"]
        report["ris_error"] = hub.get("ris_error")
        lines = [json.loads(line) for line in (attempt / "usage.jsonl").read_text().splitlines()] if (attempt / "usage.jsonl").exists() else []
        model_posts = [item for item in record.get("transport", []) if item["kind"] == "model" and item["method"] == "POST"
                       and item["path"] in ("/api/v1/chat/completions", "/api/v1/completions", "/api/v1/embeddings", "/api/v1/responses")]
        fixed = sorted({item["host"] for item in record.get("transport", []) if item["kind"] == "fixed"})
        report["requests"] = sorted({json.dumps([item["method"], item["host"], item["path"], item["caller"]])
                                     for item in record.get("transport", [])})
        fetched = sorted({item["host"] for item in record.get("transport", []) if item["kind"] == "page_fetch"})
        allowed_fixed = set(hub["fixed_hosts"])
        report.update({
            "callback_shapes": sorted({json.dumps(shape) for shape in record.get("callbacks", [])}),
            "callback_count": len(record.get("callbacks", [])),
            "sources_section": report_md.exists() and "## Sources" in report_md.read_text(errors="replace"),
            "ris_records": total, "ris_usable_rows": usable,
            "hosts": {"model": sorted({item["host"] for item in record.get("transport", []) if item["kind"] == "model"}),
                      "fixed": fixed, "fetched_pages": fetched},
            "transport_openrouter_posts": len(model_posts), "observer_lines": len(lines),
            "streamed_response_served": record.get("streamed", False),
            "socket_refusals": record.get("refused", []), "outside_writes": sorted(set(record.get("outside_writes", [])))[:50],
            "effective": record.get("effective", {}), "shim_errors": record.get("errors", []),
            "observer_sha256": hub["observer_sha256"], "runner_sha256": hashlib.sha256((RAH / "ldr_guided.py").read_bytes()).hexdigest(),
        })
        checks = report["checks"]
        checks["completed"] = bool(completion and completion.get("status") == "succeeded" and completed.returncode == 0)
        checks["ris_usable"] = isinstance(usable, int) and usable > 0
        checks["counts_equal"] = len(model_posts) == len(lines) and len(lines) > 0
        checks["no_refusals"] = not record.get("refused")
        checks["no_outside_writes"] = not record.get("outside_writes")
        checks["fixed_hosts_listed"] = set(fixed) <= allowed_fixed
        report["passed"] = all(checks.values())
        if report["passed"]:
            # Valid only when the venv was installed from exactly this route lock; the owner checks that before adding it.
            report["route_lock_sha256"] = hub["route_lock_sha256"]
            report["coverage_entry"] = ["ldr", "quick_summary", hub["route_lock_sha256"], hub["observer_sha256"]]
    except Exception as error:
        report["passed"] = False
        report["error"] = type(error).__name__ + ": " + str(error)[:300]
    finally:
        if args.keep and (work / "attempt").exists():
            args.keep.mkdir(parents=True, exist_ok=True)
            for name in ("outputs/report.md", "outputs/citations.ris", "usage.jsonl", "progress.json", "completion.json", "result.json"):
                if (work / "attempt" / name).exists():
                    shutil.copyfile(work / "attempt" / name, args.keep / Path(name).name)
        shutil.rmtree(work, ignore_errors=True)
    text = json.dumps(report, indent=2)
    if str(args.report) == "-":
        print(text)
    else:
        args.report.write_text(text)
    print("PASS" if report.get("passed") else "FAIL", flush=True)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
