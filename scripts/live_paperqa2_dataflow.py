#!/usr/bin/env python3
"""RAH devel: opt-in live PaperQA2/OpenRouter data-flow evidence, one JSON report.

Run from a research_agent_hub devel checkout:
    python scripts/live_paperqa2_dataflow.py --live --report paperqa2_live.json

Uses the installed pinned PaperQA2 interpreter, RAH's unmodified rah/worker.py,
synthetic PDFs only, and the RAH user config. Never unlocks a release gate.
Requires only stdlib in the launching interpreter.
"""
from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import zlib

SCHEMA = "rah-paperqa2-dataflow-v2"
EXPECTED_PQA = "2026.8.12"
MODELS = {"answer": "openai/gpt-4.1-mini", "summary": "openai/gpt-4.1-mini",
          "agent": "openai/gpt-4.1-mini", "embedding": "openai/text-embedding-3-small"}
ROLE_KEYS = {"answer": "RAH_PQA_ANSWER", "summary": "RAH_PQA_SUMMARY",
             "agent": "RAH_PQA_AGENT", "embedding": "RAH_PQA_EMBEDDING"}
INCLUDED = "RAH_INCLUDED_COVERAGE_ALPHA_72131"
EXCLUDED = "RAH_EXCLUDED_NEVER_SEND_98311"
METADATA = "RAH_METADATA_ONLY_BETA_61341"
QUESTION = "In the synthetic example, what sample size was reported? Answer with the sample size and a citation."


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    p = Path(path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as out:
        json.dump(value, out, indent=2, sort_keys=True)
        out.write("\n")
    if os.name != "nt":
        p.chmod(0o600)


def app_data_root():
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "Research Agent Hub"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Research Agent Hub"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "research-agent-hub"


def parse_env(path):
    # RAH's .env is written as dotenv literals, usually JSON-quoted strings.
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        hit = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)\s*$", line)
        if not hit:
            continue
        key, value = hit.groups()
        value = value.strip()
        if value.startswith(('"', "'")):
            try:
                value = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                continue
        else:
            value = value.split(" #", 1)[0].strip()
        if isinstance(value, str):
            values[key] = value
    return values


def determine_models(settings):
    models, sources = {}, {}
    for role, key in ROLE_KEYS.items():
        value = settings.get(key) or ""
        if not value:
            base = (settings.get("RAH_OPENROUTER_EMBEDDING_MODEL") if role == "embedding" else
                    settings.get("RAH_OPENROUTER_CHAT_MODEL"))
            if base:
                value = "openrouter:" + base
                sources[role] = "shared_configuration"
        if value:
            if not value.startswith("openrouter:"):
                raise ValueError("Non-OpenRouter role: " + role + " (set a verified OpenRouter role before testing)")
            models[role] = value.removeprefix("openrouter:")
            sources.setdefault(role, "per_role_configuration")
        else:
            models[role] = MODELS[role]
            sources[role] = "test_only_fallback_NOT_INSTALLED_CONFIGURATION"
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]*/[a-zA-Z0-9_.:/+-]+", models[role]):
            raise ValueError("Invalid model ID for " + role)
    return models, sources


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def openrouter_get(key, path, timeout=20):
    url = "https://openrouter.ai/api/v1/" + path
    req = Request(url, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Response oversized")
    return json.loads(data)


def choose_providers(key, models, override):
    selected, reasons = {}, {}
    for role, model in models.items():
        if role in override:
            selected[role] = override[role]
            reasons[role] = "explicit_cli_override"
            continue
        try:
            result = openrouter_get(key, "models/" + quote(model, safe="/") + "/endpoints")
            entries = (result.get("data") or {}).get("endpoints", [])
            entries = [r for r in entries if isinstance(r, dict) and isinstance(r.get("provider_name"), str)
                       and r["provider_name"]]
            # The endpoint tag is diagnostic; routing accepts provider names.
            found = next((x for x in entries if x.get("status") in (0, "online", "healthy", None)), None)
            found = found or (entries[0] if entries else None)
            if found:
                selected[role] = found["provider_name"]
                reasons[role] = "openrouter_endpoint_catalogue"
                continue
            reasons[role] = "no_provider_endpoint_returned"
        except (HTTPError, URLError, ValueError, KeyError, OSError, TypeError) as exc:
            reasons[role] = "endpoint_discovery_" + type(exc).__name__
        if model.startswith("openai/"):
            selected[role] = "OpenAI"
            reasons[role] += "_conservative_OpenAI_fallback"
    return selected, reasons


def make_pdf(path, text, subject):
    """Write a small but valid one-page PDF with searchable text and one embedded raster."""
    def obj_string(txt):
        return txt.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    lines = ["BT /F1 11 Tf 48 760 Td"]
    for i, line in enumerate(text.splitlines()):
        if i:
            lines.append("0 -18 Td")
        lines.append("(" + obj_string(line) + ") Tj")
    lines.append("ET")
    lines.append("q 18 0 0 18 48 690 cm /Im1 Do Q")
    content = ("\n".join(lines) + "\n").encode("ascii")
    pixel = zlib.compress(b"\x0a\x1f\x4d")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> /XObject << /Im1 6 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ("<< /Length %d >>\nstream\n" % len(content)).encode() + content + b"endstream",
        ("<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n" % len(pixel)).encode() + pixel + b"\nendstream",
        ("<< /Title (RAH synthetic study) /Subject (%s) /Producer (RAH live test) >>" % obj_string(subject)).encode(),
    ]
    parts = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    for i, data in enumerate(objs, 1):
        offsets.append(sum(map(len, parts)))
        parts.extend([f"{i} 0 obj\n".encode(), data, b"\nendobj\n"])
    start = sum(map(len, parts))
    parts.append(("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode())
    for offset in offsets[1:]:
        parts.append(f"{offset:010d} 00000 n \n".encode())
    parts.append((f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R /Info 7 0 R >>\nstartxref\n{start}\n%%EOF\n").encode())
    path.write_bytes(b"".join(parts))


class NetworkMonitor:
    """Default-deny non-OpenRouter DNS destinations during the PaperQA2 child.

    Monitors Python socket DNS/connect and httpx requests in THIS process only;
    it does not claim OS-wide egress capture or OpenRouter-internal visibility.
    """
    def __init__(self):
        self.requests = []
        self.dns = []
        self.blocked = []
        self.allowed_ips = set()
        self.phase = "initial"
        self.ids = []
        self.pdf_b64 = []

    def install(self):
        import httpx
        self._old_dns = socket.getaddrinfo
        self._old_connect = socket.socket.connect
        self._old_connect_ex = socket.socket.connect_ex
        self._old_async = httpx.AsyncClient.send
        self._old_sync = httpx.Client.send
        self._old_response_json = httpx.Response.json
        monitor = self

        def dns(host, port, *args, **kwargs):
            name = (host.decode() if isinstance(host, bytes) else str(host)).lower().rstrip(".")
            if name not in {"openrouter.ai", "localhost", "127.0.0.1", "::1"}:
                monitor.blocked.append({"kind": "dns", "target": "unapproved_host", "host": name[:120]})
                raise OSError("External hostname blocked by RAH live test")
            answer = monitor._old_dns(host, port, *args, **kwargs)
            if name == "openrouter.ai":
                monitor.allowed_ips.update(row[4][0] for row in answer)
            monitor.dns.append({"host": name})
            return answer

        def check_peer(addr):
            if not isinstance(addr, tuple):
                return  # local socket used by runtime; not an Internet destination
            ip = str(addr[0]).split("%", 1)[0]
            if ip not in monitor.allowed_ips and ip not in {"127.0.0.1", "::1"}:
                monitor.blocked.append({"kind": "socket", "target": "unrecognized_ip"})
                raise OSError("External socket blocked by RAH live test")

        def connect(sock, addr):
            check_peer(addr)
            return monitor._old_connect(sock, addr)

        def connect_ex(sock, addr):
            check_peer(addr)
            return monitor._old_connect_ex(sock, addr)

        def capture(req):
            url = req.url
            blob = b""
            try:
                blob = req.content
            except (httpx.RequestNotRead, RuntimeError):
                pass
            data = None
            if len(blob) < 2_000_000:
                try:
                    data = json.loads(blob) if blob else None
                except (ValueError, UnicodeDecodeError):
                    pass
            provider = data.get("provider") if isinstance(data, dict) else None
            choice = provider.get("only") if isinstance(provider, dict) else None
            entry = {"phase": monitor.phase, "method": req.method, "host": str(url.host),
                     "path": str(url.path), "body_bytes": len(blob),
                     "model": data.get("model") if isinstance(data, dict) else None,
                     "provider_only": choice if isinstance(choice, list) else None,
                     "allow_fallbacks": provider.get("allow_fallbacks") if isinstance(provider, dict) else None,
                     "included_marker": INCLUDED.encode() in blob,
                     "excluded_marker": EXCLUDED.encode() in blob,
                     "metadata_marker": METADATA.encode() in blob,
                     "raw_pdf_header": b"%PDF-" in blob,
                     "pdf_base64_transfer": any(x in blob for x in monitor.pdf_b64),
                     "image_payload_indicator": any(x in blob for x in
                         (b'"image_url"', b'"input_image"', b'data:image/', b'data:application/pdf')),
                     "tool_schema": isinstance(data, dict) and bool(data.get("tools")),
                     "streamed_unknown": not blob}
            monitor.requests.append(entry)
            if str(url.host) != "openrouter.ai" or str(url.scheme) != "https" or str(url.path) not in {
                    "/api/v1/chat/completions", "/api/v1/embeddings"}:
                monitor.blocked.append({"kind": "httpx", "target": "unapproved_endpoint"})
                raise OSError("Non-OpenRouter HTTP request refused")
            return entry

        def observe(response, entry):
            entry["status_code"] = int(response.status_code)
            try:
                data = response.json()
                if (isinstance(data, dict) and isinstance(data.get("id"), str)
                    and entry["path"].endswith("chat/completions")
                    and not any(x["id"] == data["id"] for x in monitor.ids)):
                    monitor.ids.append({"id": data["id"], "phase": entry["phase"], "model": entry["model"]})
            except (ValueError, UnicodeDecodeError, RuntimeError):
                pass

        async def a_send(client, request, **kwargs):
            entry = capture(request)
            result = await monitor._old_async(client, request, **kwargs)
            observe(result, entry)
            return result

        def s_send(client, request, **kwargs):
            entry = capture(request)
            result = monitor._old_sync(client, request, **kwargs)
            observe(result, entry)
            return result

        def response_json(response, *args, **kwargs):
            data = monitor._old_response_json(response, *args, **kwargs)
            try:
                if (isinstance(data, dict) and isinstance(data.get("id"), str)
                    and response.request.url.host == "openrouter.ai"
                    and response.request.url.path == "/api/v1/chat/completions"
                    and not any(x["id"] == data["id"] for x in monitor.ids)):
                    monitor.ids.append({"id": data["id"], "phase": monitor.phase,
                                        "model": data.get("model")})
            except (RuntimeError, KeyError, ValueError):
                pass
            return data

        socket.getaddrinfo = dns
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        httpx.AsyncClient.send = a_send
        httpx.Client.send = s_send
        httpx.Response.json = response_json


def safe_error(exc, *, secret="", work=""):
    """Bounded diagnostic, with credentials, canaries and temp paths removed."""
    message = str(exc)
    for sensitive in (secret, work, INCLUDED, EXCLUDED, METADATA):
        if sensitive:
            message = message.replace(sensitive, "[redacted]")
    message = re.sub(r"(?i)(authorization|api[_ -]?key|bearer)[= :]+[^\s,;]+", r"\1=[redacted]", message)
    message = re.sub(r"(?:sk-or-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{12,})", "[redacted]", message)
    message = re.sub(r"[A-Za-z0-9_-]{100,}", "[redacted-long-token]", message)
    return {"exception_type": type(exc).__name__, "safe_message": message[:280]}


def child_main(input_file, result_file):
    config = json.loads(Path(input_file).read_text(encoding="utf-8"))
    report = {"tests": {}, "requests": [], "blocked_connections": [], "dns": [], "generation_ids_internal": [],
              "environment": {}, "limitations": ["Python-process network interception, not OS-wide packet capture",
              "OpenRouter-to-provider internal transfers are not directly observable"]}
    monitor = NetworkMonitor()
    try:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(k, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        os.environ["OPENROUTER_API_KEY"] = config["key"]
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        os.environ["PYTHONNOUSERSITE"] = "1"
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        work = Path(config["work"])
        os.environ["PQA_HOME"] = str(work / "pqa-home")
        (work / "pqa-home").mkdir()
        (work / "corpus").mkdir()
        (work / "excluded").mkdir()
        make_pdf(work / "corpus" / "rah_selected.pdf",
                 "Synthetic journal report for program evaluation.\n" + INCLUDED +
                 "\nThe synthetic sample size is 17 participants.\nAll numbers are fabricated for a software test.", METADATA)
        make_pdf(work / "excluded" / "rah_unselected.pdf",
                 "Synthetic excluded study.\n" + EXCLUDED +
                 "\nThe excluded synthetic sample size is 999 participants.", "EXCLUDED_METADATA_DO_NOT_SEND")
        monitor.pdf_b64 = [base64.b64encode((work / "corpus" / "rah_selected.pdf").read_bytes()),
                           base64.b64encode((work / "excluded" / "rah_unselected.pdf").read_bytes())]
        # Record exact PDF hashes, not their content, in the uploadable report.
        report["fixtures"] = {"included_pdf_sha256": sha(work / "corpus" / "rah_selected.pdf"),
                              "excluded_pdf_sha256": sha(work / "excluded" / "rah_unselected.pdf"),
                              "selected_file_count": 1, "excluded_file_count": 1, "contains_raster_image": True,
                              "metadata_subject_present": True}
        for name in ("paper-qa", "fhlmi", "fhaviary", "litellm", "paper-qa-pypdf", "pypdf"):
            try:
                report["environment"][name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                report["environment"][name] = "not_installed"
        worker_path = Path(config["worker"])
        spec = importlib.util.spec_from_file_location("rah_live_worker", worker_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("RAH worker missing")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        import asyncio
        from aviary.core import Message, Tool
        from paperqa import Settings
        from paperqa_pypdf import parse_pdf_to_pages
        # Check the locally generated PDF is actually parseable by the pinned reader.
        from pypdf import PdfReader
        read = PdfReader(str(work / "corpus" / "rah_selected.pdf"))
        parsed = "\n".join(page.extract_text() or "" for page in read.pages)
        report["tests"]["local_pdf_parsing"] = {
            "status": "PASS" if INCLUDED in parsed and "17 participants" in parsed else "FAIL",
            "parser": "pypdf", "included_marker_found": INCLUDED in parsed}
        monitor.install()
        profile = config["profile"]
        options = worker.paper_options(profile)  # exact RAH managed profile builder
        settings = Settings(**options)

        def test_tool(query: str) -> str:
            """A harmless synthetic research query tool.

            Args:
                query: Synthetic test query.
            """
            return "synthetic fixture"

        async def probes():
            messages = [Message(role="user", content="This is a synthetic test. Reply with OK only.")]
            for role, run in [("answer", lambda: settings.get_llm().acompletion(messages)),
                              ("summary", lambda: settings.get_summary_llm().acompletion(messages)),
                              ("agent", lambda: settings.get_agent_llm().select_tool(messages, tools=[Tool.from_function(test_tool)])),
                              ("embedding", lambda: settings.get_embedding_model().embed_documents(["synthetic PDF excerpt"]))]:
                monitor.phase = "probe_" + role
                try:
                    result = await run()
                    report["tests"]["probe_" + role] = {"status": "PASS" if result is not None else "FAIL"}
                except Exception as exc:  # deliberately do not emit provider exception text
                    report["tests"]["probe_" + role] = {"status": "FAIL", **safe_error(exc, secret=config["key"], work=str(work))}
        with asyncio.Runner() as runner:
            runner.run(probes())
        # A deliberately invalid provider must not fall back to any other provider.
        monitor.phase = "negative_invalid_provider"
        invalid_provider = "RAH_NO_SUCH_PROVIDER_DO_NOT_FALLBACK_76239"
        bad = {"id": "openrouter-text-v1", "destinations": [
            dict(entry, provider=invalid_provider) if entry["role"] == "answer" else entry
            for entry in profile["destinations"]]}
        try:
            bad_settings = Settings(**worker.paper_options(bad))
            async def rejected():
                return await bad_settings.get_llm().acompletion(
                    [Message(role="user", content="Synthetic invalid provider test: respond OK")])
            with asyncio.Runner() as runner:
                runner.run(rejected())
            report["tests"]["negative_invalid_provider"] = {"status": "FAIL", "reason": "unavailable_provider_accepted"}
        except Exception as exc:
            report["tests"]["negative_invalid_provider"] = {
                "status": "PASS", "failure_type": type(exc).__name__}
        monitor.phase = "native_pdf_index_and_answer"
        try:
            worker.paper({"profile": profile, "question": QUESTION}, work)
            answer = json.loads((work / "result.json").read_text(encoding="utf-8"))
            actual_answer = answer.get("answer", "")
            bib = answer.get("bib", "")
            # The native worker persists session.formatted_answer, which can include
            # a 'References' section independently of AnswerResponse.bibtex.
            correct = bool(re.search(r"\b17\b", actual_answer)) and not bool(re.search(r"\b999\b", actual_answer))
            references_heading = bool(re.search(r"(?im)^\s*References\s*$", actual_answer))
            source_mention = any(x in actual_answer.lower() for x in
                                 ("rah_selected", "rah selected", "rah synthetic study"))
            citation_present = bool(bib.strip()) or (references_heading and source_mention)
            sanitized_excerpt = actual_answer[:2000]
            for value, marker in ((config["key"], "[secret]"), (INCLUDED, "[selected marker]"),
                                  (EXCLUDED, "[excluded marker]"), (METADATA, "[metadata marker]")):
                if value:
                    sanitized_excerpt = sanitized_excerpt.replace(value, marker)
            report["tests"]["native_pdf_index_and_answer"] = {
                "status": "PASS" if correct and citation_present else
                          "FAIL" if not correct else "INCONCLUSIVE",
                "sample_size_17": bool(re.search(r"\b17\b", actual_answer)),
                "forbidden_sample_size_999": bool(re.search(r"\b999\b", actual_answer)),
                "citation_in_formatted_answer": references_heading and source_mention,
                "reference_heading_present": references_heading,
                "selected_source_mentioned": source_mention,
                "native_bibliography_present": bool(bib.strip()),
                "answer_excerpt_synthetic_only": sanitized_excerpt,
                "answer_bytes": len(actual_answer.encode()),
                "bibliography_bytes": len(bib.encode())}
        except Exception as exc:
            report["tests"]["native_pdf_index_and_answer"] = {"status": "FAIL", **safe_error(exc, secret=config["key"], work=str(work))}
    except BaseException as exc:
        report["tests"]["setup_or_execution"] = {"status": "FAIL", **safe_error(exc, secret=config.get("key", ""), work=config.get("work", ""))}
    finally:
        report["requests"] = monitor.requests
        report["blocked_connections"] = monitor.blocked
        report["dns"] = monitor.dns
        report["generation_ids_internal"] = monitor.ids
        write_json(result_file, report)
    return 0


def metadata_review(key, ids):
    results = []
    for item in ids[:120]:
        out = {"phase": item.get("phase"), "model": item.get("model"),
               "generation_id_sha12": hashlib.sha256(item["id"].encode()).hexdigest()[:12]}
        for attempt in range(5):
            try:
                data = openrouter_get(key, "generation?id=" + quote(item["id"], safe=""), timeout=15)
                obj = data.get("data", {})
                out.update(provider_name=obj.get("provider_name"), charged_cost=obj.get("total_cost"),
                           delivered_model=obj.get("model"), generation_found=bool(obj))
                break
            except HTTPError as exc:
                out["metadata_http_status"] = int(exc.code)
            except (URLError, ValueError, KeyError, OSError, TypeError) as exc:
                out["metadata_error_type"] = type(exc).__name__
            if attempt < 4:
                time.sleep((1, 2, 4, 7)[attempt])
        results.append(out)
    return results


def main():
    ap = argparse.ArgumentParser(description="One synthetic live PaperQA2/OpenRouter data-flow report (v2)")
    ap.add_argument("--live", action="store_true", help="Authorize paid synthetic provider requests")
    ap.add_argument("--report", default="paperqa2_live.json")
    ap.add_argument("--data-dir", type=Path, help="RAH data directory; default is installed RAH location")
    ap.add_argument("--timeout", type=int, default=480, help="Whole child-process deadline (seconds)")
    ap.add_argument("--provider", action="append", default=[], metavar="ROLE=PROVIDER",
                    help="Use provider named by OpenRouter, e.g. answer=OpenAI; repeat for each role if needed")
    ap.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--_input", help=argparse.SUPPRESS)
    ap.add_argument("--_output", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args._child:
        return child_main(args._input, args._output)
    root = (args.data_dir or app_data_root()).expanduser().resolve()
    repo = Path(__file__).resolve().parent.parent
    worker = repo / "rah" / "worker.py"
    report = {"schema": SCHEMA, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "status": "INCONCLUSIVE", "checks": {}, "environment": {
                  "host_os": platform.system(), "host_arch": platform.machine(),
                  "launcher_python": platform.python_version()},
              "notes": ["Only autogenerated synthetic test PDFs are used; private project files are not read.",
                        "PASS means this sampled live route passed checks, not general proof of future routes.",
                        "This script does not modify RAH manifests or approve managed PDF handoffs.",
                        "Missing OpenRouter generation metadata and missing native citation evidence are INCONCLUSIVE, not proven privacy failures."]}
    try:
        report["environment"]["worker_sha256"] = sha(worker)
        current = root / "apps" / "pqa" / "current.json"
        record = json.loads(current.read_text(encoding="utf-8"))
        install_id = record.get("install_id")
        if not isinstance(install_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", install_id):
            raise ValueError("Invalid installed PaperQA2 pointer")
        pqa_dir = root / "apps" / "pqa" / "versions" / install_id
        python_exe = pqa_dir / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python_exe.is_file():
            raise FileNotFoundError("PaperQA2 venv not found; install PaperQA2 using RAH Apps first")
        report["environment"]["install_record_fields"] = sorted(k for k in record if k not in {"path", "root"})
        report["environment"]["installed_lock_sha256"] = record.get("lock_sha256")
        report["environment"]["installed_record_upstream_version"] = record.get("upstream_version")
        manifest = repo / "rah" / "install_assets" / "routes.json"
        if manifest.exists():
            manifest_rows = json.loads(manifest.read_text(encoding="utf-8")).get("routes", [])
            matching_rows = [r for r in manifest_rows if r.get("app_id") == "pqa" and
                             r.get("lock_sha256") == record.get("lock_sha256") and
                             r.get("upstream_version") == record.get("upstream_version")]
            report["environment"]["matching_pqa_manifest_rows"] = len(matching_rows)
            report["checks"]["installed_lock_matches_repo_manifest"] = bool(matching_rows)
        else:
            report["checks"]["installed_lock_matches_repo_manifest"] = "INCONCLUSIVE"
        repo_git = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"],
                                  capture_output=True, text=True, timeout=5, check=False)
        if repo_git.returncode == 0:
            report["environment"]["rah_commit"] = repo_git.stdout.strip()
        raw = parse_env(root / "hub" / "config" / ".env")
        raw.update({k: v for k, v in os.environ.items() if k in set(ROLE_KEYS.values()) | {
            "RAH_OPENROUTER_CHAT_MODEL", "RAH_OPENROUTER_EMBEDDING_MODEL", "RAH_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"}})
        key = raw.get("OPENROUTER_API_KEY") or raw.get("RAH_OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("No OpenRouter key found in RAH user .env or process environment")
        models, sources = determine_models(raw)
        report["environment"]["roles"] = {r: {"model": models[r], "source": sources[r]} for r in MODELS}
        overrides = {}
        for x in args.provider:
            if "=" not in x:
                raise ValueError("Expected --provider ROLE=PROVIDER")
            role, value = x.split("=", 1)
            if role not in MODELS or not value or not re.fullmatch(r"[A-Za-z0-9 _.-]{2,70}", value):
                raise ValueError("Invalid provider override (role or provider name)")
            overrides[role] = value
        if not args.live:
            report["checks"]["network_authorization"] = "NOT_RUN: pass --live to authorize billable provider calls"
            write_json(args.report, report)
            print("INCONCLUSIVE: live permission missing; report saved to", args.report)
            return 2
        providers, reasons = choose_providers(key, models, overrides)
        report["environment"]["providers"] = {role: {"chosen": providers.get(role), "selection": reasons[role]} for role in MODELS}
        missing = set(MODELS) - set(providers)
        if missing:
            raise RuntimeError("Provider discovery unavailable for: " + ", ".join(sorted(missing)) +
                               "; use --provider ROLE=PROVIDER for those roles")
        destinations = []
        for role in MODELS:
            destinations.append({"role": role, "service": "OpenRouter", "provider": providers[role],
                "model": models[role], "endpoint": "https://openrouter.ai/api/v1/" +
                ("embeddings" if role == "embedding" else "chat/completions")})
        profile = {"id": "openrouter-text-v1", "destinations": destinations}
        with tempfile.TemporaryDirectory(prefix="rah-pqa-live-") as tmp:
            home = Path(tmp)
            request = home / "request.json"
            result = home / "child_result.json"
            work = home / "work"
            work.mkdir()
            write_json(request, {"key": key, "profile": profile, "worker": str(worker), "work": str(work)})
            child_env = {k: v for k, v in os.environ.items() if k.upper() in {
                "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR",
                "LANG", "LC_ALL", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH", "PATHEXT"}}
            # Explicit TLS cert bundle, if needed, may be preserved; proxy routing stays disabled.
            for cert in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
                if cert in os.environ:
                    child_env[cert] = os.environ[cert]
            child_env.update(PYTHONNOUSERSITE="1", PYTHON_DOTENV_DISABLED="1", NO_PROXY="*", no_proxy="*")
            cmd = [str(python_exe), str(Path(__file__).resolve()), "--_child", "--_input", str(request), "--_output", str(result)]
            try:
                proc = subprocess.run(cmd, env=child_env, cwd=work, capture_output=True, timeout=args.timeout,
                                      stdin=subprocess.DEVNULL, check=False)
                report["checks"]["child_process_exit_zero"] = proc.returncode == 0
            except subprocess.TimeoutExpired:
                report["checks"]["timeout"] = "FAIL: PaperQA2 live child exceeded requested deadline"
            if result.exists():
                inner = json.loads(result.read_text(encoding="utf-8"))
                ids = inner.pop("generation_ids_internal", [])
                report["live"] = inner
                report["generation_metadata"] = metadata_review(key, ids)
                check = {}
                requests = inner.get("requests", [])
                check["all_httpx_requests_openrouter"] = all(r.get("host") == "openrouter.ai" and
                    r.get("path") in {"/api/v1/chat/completions", "/api/v1/embeddings"} for r in requests) and bool(requests)
                check["no_blocked_non_openrouter_destinations"] = not bool(inner.get("blocked_connections"))
                check["no_excluded_canary_egress"] = not any(r.get("excluded_marker") for r in requests)
                check["no_raw_pdf_egress"] = not any(r.get("raw_pdf_header") or r.get("pdf_base64_transfer") for r in requests)
                check["no_image_payload_egress"] = not any(r.get("image_payload_indicator") for r in requests)
                negative = [r for r in requests if r.get("phase") == "negative_invalid_provider"]
                check["invalid_provider_rejected_without_fallback"] = (
                    inner.get("tests", {}).get("negative_invalid_provider", {}).get("status") == "PASS" and
                    bool(negative) and all(r.get("provider_only") == ["RAH_NO_SUCH_PROVIDER_DO_NOT_FALLBACK_76239"]
                    and r.get("allow_fallbacks") is False and r.get("status_code") != 200 for r in negative))
                check["index_sent_included_text"] = any(r.get("included_marker") for r in requests
                    if r.get("phase") == "native_pdf_index_and_answer")
                for role in MODELS:
                    relevant = [r for r in requests if r.get("phase") == "probe_" + role]
                    check[role + "_request_provider_locked"] = bool(relevant) and all(
                        r.get("model") == models[role] and r.get("provider_only") == [providers[role]] and
                        r.get("allow_fallbacks") is False and r.get("status_code") == 200 for r in relevant)
                check["installed_paperqa_version_matches_pin"] = inner.get("environment", {}).get("paper-qa") == EXPECTED_PQA
                check["current_record_pin_matches_expected"] = record.get("upstream_version") == EXPECTED_PQA
                check["local_pdf_parsing"] = inner.get("tests", {}).get("local_pdf_parsing", {}).get("status") == "PASS"
                native_test = inner.get("tests", {}).get("native_pdf_index_and_answer", {})
                check["native_pdf_answer_correct"] = (
                    native_test.get("sample_size_17") is True and
                    native_test.get("forbidden_sample_size_999") is False)
                check["native_pdf_citations_present"] = (
                    True if native_test.get("citation_in_formatted_answer") or
                    native_test.get("native_bibliography_present") else "INCONCLUSIVE")
                # Using fallback models is valuable evidence of the transport, but
                # cannot qualify whatever models the user later configures in RAH.
                check["roles_match_installed_configuration"] = (
                    True if all(not v.startswith("test_only_fallback") for v in sources.values())
                    else "INCONCLUSIVE")
                for role in MODELS:
                    check[role + "_probe_succeeded"] = inner.get("tests", {}).get("probe_" + role, {}).get("status") == "PASS"
                metadata = report["generation_metadata"]
                matching = []
                missing = []
                mismatches = []
                for item in metadata:
                    role = item.get("phase", "").removeprefix("probe_")
                    expected_provider = providers.get(role, providers["answer"])
                    observed_provider = item.get("provider_name")
                    if not observed_provider:
                        missing.append(item.get("generation_id_sha12"))
                    elif observed_provider.lower() != expected_provider.lower():
                        mismatches.append(item.get("generation_id_sha12"))
                    else:
                        matching.append(item.get("generation_id_sha12"))
                required_phases = {"probe_answer", "probe_summary", "probe_agent", "native_pdf_index_and_answer"}
                confirmed_phases = {item.get("phase") for item in metadata
                                    if item.get("provider_name") and
                                    item.get("generation_id_sha12") in matching}
                report["provider_confirmation_coverage"] = {
                    "confirmed_count": len(matching), "unavailable_count": len(missing),
                    "mismatch_count": len(mismatches),
                    "required_phases_confirmed": sorted(required_phases & confirmed_phases),
                    "incomplete_generation_ids_sha12": missing,
                    "mismatched_generation_ids_sha12": mismatches}
                # Some successful OpenRouter responses cannot subsequently be found
                # through the generation-metadata endpoint. They do not override
                # observed provider pins, a negative fallback test, and confirmation
                # of each required chat phase; preserve as incomplete coverage.
                check["chat_generation_provider_confirmed"] = (
                    False if mismatches else True if required_phases <= confirmed_phases
                    else "INCONCLUSIVE")
                report["checks"].update(check)
                all_checks = report["checks"]
                report["status"] = ("FAIL" if any(value is False for value in all_checks.values())
                    else "INCONCLUSIVE" if any(value == "INCONCLUSIVE" for value in all_checks.values())
                    else "PASS")
            else:
                report["checks"]["child_report_generated"] = False
                report["status"] = "INCONCLUSIVE"
        if any(v["source"].startswith("test_only_fallback") for v in report["environment"]["roles"].values()):
            report["notes"].append("One or more roles were not configured in RAH; defaults used for this TEST ONLY. This cannot qualify a different installed assignment.")
        report["notes"].append("Embedding response metadata may not expose actual downstream provider; strict outbound provider pin is checked but downstream embedding delivery remains partly unobservable.")
    except BaseException as exc:
        report["checks"]["setup"] = {"status": "INCONCLUSIVE", **safe_error(exc, secret=locals().get("key") or "", work=str(root))}
        report["notes"].append("Setup did not complete. See the exception type and model/provider selection fields; no secrets are included.")
        report["status"] = "INCONCLUSIVE"
    finally:
        write_json(args.report, report)
    print(report["status"] + ": saved " + str(Path(args.report).resolve()))
    if report["status"] != "PASS":
        print("Unresolved or failing checks:")
        for name, result in sorted(report["checks"].items()):
            if result is False or result == "INCONCLUSIVE":
                print(" - " + name + ": " + str(result))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
