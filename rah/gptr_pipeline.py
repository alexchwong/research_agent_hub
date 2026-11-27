"""Guided GPT Researcher CLI runner.

Runs the pinned native CLI as a direct argument list in its own process group, with a private working
directory and a per-operation settings projection. GPT Researcher owns planning, search and writing.
"""
from datetime import datetime, timezone
import os
import re
import signal
import subprocess
from threading import Thread
from time import monotonic, sleep

from . import research_profiles, search_sources, settings
from .common import Problem, canonical, child, clean_env, digest, file_digest, private_directory, write_json
from .projects import REPORT_LIMIT


SURVEY_SETTINGS = {
    "MAX_ITERATIONS": "1", "MAX_SEARCH_RESULTS_PER_QUERY": "3", "MAX_SCRAPER_WORKERS": "2",
    "BROWSE_CHUNK_MAX_LENGTH": "2048", "TOTAL_WORDS": "400", "CURATE_SOURCES": "False",
    "FAST_TOKEN_LIMIT": "1024", "SMART_TOKEN_LIMIT": "2048", "STRATEGIC_TOKEN_LIMIT": "1024", "SUMMARY_TOKEN_LIMIT": "512",
    "LLM_KWARGS": '{"max_tokens":2048,"max_retries":0}',
}
SEALED = {"IMAGE_GENERATION_ENABLED": "False", "MCP_STRATEGY": "disabled", "MCP_SERVERS": "[]", "PYTHONNOUSERSITE": "1",
          "PYTHON_DOTENV_DISABLED": "1", "NO_PROXY": "*"}
DEADLINES = {"survey": 180, "deep": 1200, "research_report": 600}
MODES = {"survey": "research_report", "rapid": "research_report", "comparison": "research_report", "deep": "deep"}
SEARCH_SECRETS = {"tavily": ("TAVILY_API_KEY",), "pubmed_central": (), "searx": ()}
KILL_GRACE = 3
STREAM_LIMIT = 64 * 1024
FAILURES = (
    (re.compile(rb"(?:401|Unauthorized|AuthenticationError|invalid api key)", re.I), "authorization_failed", 503,
     "The model provider rejected the configured key. Check Settings → OpenRouter."),
    (re.compile(rb"(?:402|insufficient (?:credits|funds|quota))", re.I), "insufficient_credit", 503,
     "The provider reported insufficient credit for this route."),
    (re.compile(rb"(?:429|RateLimit|rate limit)", re.I), "rate_limited", 503, "A provider rate limited the request. Retry later."),
    (re.compile(rb"(?:ModuleNotFoundError|ImportError)"), "native_install", 503, "Repair GPT Researcher in App details."),
    (re.compile(rb"(?:ConnectError|ConnectionError|Name or service not known|timed out)", re.I), "provider_unavailable", 503,
     "A provider or search service could not be reached."),
)


def now():
    return datetime.now(timezone.utc).isoformat()


def guided_values(stored_env, key, operation, quality):
    """Disk-free projection values for preview, fingerprinting and dispatch."""
    if stored_env.get("CONFIG_PATH") or settings.LEGACY_GPTR & stored_env.keys():
        raise Problem("native_configuration", "Guided research cannot use an external GPT Researcher config or legacy "
                      "base/model overrides. Use GPT Researcher Classic for those settings.", 409)
    retriever = stored_env.get("RETRIEVER")
    if retriever not in SEARCH_SECRETS or "," in (retriever or ""):
        raise Problem("unsupported_retriever", "Guided research needs one supported search source. Choose PubMed Central, "
                      "local SearXNG or Tavily in Settings → Search sources.", 409)
    if not key:
        raise Problem("needs_credential", "Add the shared OpenRouter key in Settings to run guided research.", 409)
    env = {"RETRIEVER": retriever, "OPENROUTER_API_KEY": key}
    for name in SEARCH_SECRETS[retriever]:
        if not stored_env.get(name):
            raise Problem("needs_search_credential", "Add the search key for the selected search source in Settings.", 409)
        env[name] = stored_env[name]
    if retriever == "searx":
        env["SEARX_URL"] = search_sources.local_url(stored_env.get("SEARX_URL", ""))
    env |= research_profiles.survey_projection() if operation == "survey" else research_profiles.downstream_projection(quality)
    if operation == "survey":
        env |= SURVEY_SETTINGS
    return env | SEALED


TOKENIZER = "Tokenizer data from openaipublic.blob.core.windows.net (GPT Researcher cost estimates; first use, then cached)"


def guided_projection(stored_env, key, operation, quality, workdir, cache=None):
    """Build the exact subprocess environment for one guided operation from the checked saved projection."""
    env = guided_values(stored_env, key, operation, quality)
    if cache is not None:
        # The pinned cost estimator loads a tokenizer file on first use. Keep one private cache across attempts.
        env["TIKTOKEN_CACHE_DIR"] = str(cache)
    private_directory(workdir)
    home = private_directory(child(workdir, "home"))
    docs = private_directory(child(workdir, "my-docs"))
    env |= {"DOC_PATH": str(docs), "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home),
            "LOCALAPPDATA": str(home), "XDG_CONFIG_HOME": str(home), "XDG_CACHE_HOME": str(home),
            "XDG_DATA_HOME": str(home), "TMPDIR": str(home), "TEMP": str(home), "TMP": str(home)}
    return clean_env() | env | SEALED


def validate_env(env):
    """Validate the projected subprocess env itself (not the saved profile)."""
    banned = settings.LEGACY_GPTR | {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OLLAMA_BASE_URL"}
    if banned & env.keys():
        raise Problem("native_configuration", "The guided projection contains an unsupported override.", 409)
    for role in ("FAST_LLM", "SMART_LLM", "STRATEGIC_LLM", "EMBEDDING"):
        provider, _, model = env.get(role, "").partition(":")
        if provider != "openrouter" or model not in research_profiles.MODELS.values():
            raise Problem("unsupported_model", "The guided projection must use the curated OpenRouter models.", 409)
    secrets = {name for name in env if name.endswith("_KEY")}
    allowed = {"OPENROUTER_API_KEY"} | set(SEARCH_SECRETS.get(env.get("RETRIEVER"), ()))
    if secrets - allowed:
        raise Problem("native_configuration", "The guided projection would forward an unrelated credential.", 409)
    return True


def recipients(env):
    return {"models": "OpenRouter (downstream providers selected by the service unless constrained)",
            "search": search_sources.LABELS["gptr"][env["RETRIEVER"]],
            "fetches": "External search results can lead GPT Researcher to fetch public source pages."}


def command(install, mode, question):
    from .common import venv_python
    return [str(venv_python(install / "venv")), str(install / "source" / "cli.py"), "--report_type", mode,
            "--report_source", "web", "--no-pdf", "--no-docx", "--", question]


def frontmatter(head):
    """Return (fields, body_offset) for a leading YAML block, or ({}, 0)."""
    if not head.startswith(b"---\n"):
        return {}, 0
    end = head.find(b"\n---\n", 4)
    if end == -1:
        return {}, 0
    fields = {}
    for line in head[4:end].split(b"\n"):
        match = re.fullmatch(rb"([a-z_]+):\s*(.*)", line.strip())
        if match:
            fields[match.group(1).decode()] = match.group(2).decode("utf-8", "replace")
    return fields, end + 5


def source_count(fields):
    value = fields.get("sources_count")
    if value is None or not re.fullmatch(r"[0-9]{1,9}", value):
        return None
    return int(value)


def terminate(process):
    """Stop the whole child tree and wait for exit."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        process.wait(timeout=30)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=30)
    # Reap any survivors that kept the group alive after the leader exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Counter:
    def __init__(self, stream):
        self.total = 0
        self.tail = b""
        self.thread = Thread(target=self.drain, args=(stream,), daemon=True)
        self.thread.start()

    def drain(self, stream):
        while block := stream.read(8192):
            self.total += len(block)
            self.tail = (self.tail + block)[-STREAM_LIMIT:]
        stream.close()


def classify(counters, returncode):
    tail = b"".join(counter.tail for counter in counters)
    for pattern, code, status, message in FAILURES:
        if pattern.search(tail):
            return Problem(code, message, status)
    return Problem("native_failed", f"GPT Researcher exited with status {returncode}. Check its search source and model settings.", 503)


def run(attempt_dir, argv, env, *, deadline, job=None, step=None, attempt=None, operation=None):
    """Run one CLI attempt. Returns the single new Markdown output and native metadata."""
    workdir = private_directory(attempt_dir)
    outputs = private_directory(child(workdir, "outputs"))
    before = {path.name for path in outputs.iterdir()}
    write_json(child(workdir, "operation.json"), {"step": step, "attempt": attempt, "operation": operation,
                                                  "started_at": now(), "argv_digest": digest(canonical(argv[2:]))})
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    started = monotonic()
    status = "failed"
    returncode = None
    try:
        process = subprocess.Popen(argv, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False, **options)
    except OSError as error:
        write_json(child(workdir, "completion.json"), {"status": "start_failed", "finished_at": now()})
        raise Problem("worker_start", "GPT Researcher could not start. Repair it in App details.", 503) from error
    counters = [Counter(process.stdout), Counter(process.stderr)]
    try:
        while process.poll() is None:
            if job is not None and job.requested:
                status = "cancelled"
                terminate(process)
                raise Problem("cancelled", "Operation cancelled.", 409)
            if monotonic() - started >= deadline:
                status = "timeout"
                terminate(process)
                raise Problem("stage_timeout", f"GPT Researcher did not finish within {deadline // 60} minutes. "
                              "The stage is incomplete; retry when ready.", 504)
            sleep(0.1)
        returncode = process.returncode
        for counter in counters:
            counter.thread.join(timeout=5)
        if returncode:
            status = "failed"
            raise classify(counters, returncode)
        created = sorted(path for path in outputs.iterdir() if path.name not in before and path.suffix.lower() == ".md")
        if len(created) != 1 or created[0].is_symlink() or not created[0].is_file():
            status = "failed"
            raise Problem("native_output", "Expected exactly one new GPT Researcher Markdown report.", 503)
        path = created[0]
        size = path.stat().st_size
        if not size or size > REPORT_LIMIT:
            status = "failed"
            raise Problem("native_output", "The native report is empty or larger than 32 MiB.", 503)
        with path.open("rb") as handle:
            fields, _ = frontmatter(handle.read(128 * 1024))
        count = source_count(fields)
        checksum = file_digest(path)
        status = "succeeded"
        return {"path": path, "name": path.name, "sha256": checksum, "size": size, "sources_count": count,
                "title": fields.get("title", "").strip('"')[:300] or None,
                "total_cost_usd": fields.get("total_cost_usd") if re.fullmatch(r"[0-9.eE-]{1,32}", fields.get("total_cost_usd", "")) else None,
                "stdout_bytes": counters[0].total, "stderr_bytes": counters[1].total}
    finally:
        if process.poll() is None:
            terminate(process)
        record = {"status": status, "exit_code": returncode, "finished_at": now(), "elapsed": round(monotonic() - started, 3)}
        if status == "succeeded":
            record["outputs"] = [{"name": path.name, "sha256": checksum, "size": size}]
        write_json(child(workdir, "completion.json"), record)
