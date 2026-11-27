"""Headless guided LDR route: the documented LDR Python API (quick_summary) plus LDR's own RIS exporter.

RAH only invokes the upstream functions with the approved public prompt in a private attempt folder, observes
the process and imports its outputs. LDR owns research, strategy, search and report writing.
"""
from importlib import resources
from urllib.parse import urlsplit

from . import gptr_pipeline, search_sources, settings, upstreams
from .common import Problem, atomic, canonical, child, clean_env, digest, file_digest, private_directory, read_json, venv_python

DEADLINE = 3600
RUNNER = "ldr_guided.py"
OPENROUTER = "https://openrouter.ai/api/v1"
STRATEGY = "langgraph-agent (LDR default; may run several searches and sub-research steps)"
# Fixed hosts recorded by the installed pinned-upstream probe (tests/probe_ldr_headless.py) for each supported
# search tool, besides the model endpoint. A fixed host the probe observes outside this list fails the probe.
FIXED_HOSTS = {
    # Recorded by probe_ldr_headless.py on LDR 2.0.1: the PubMed E-utilities API, plus OpenAlex enrichment and
    # LDR's journal-quality reference data, which LDR downloads into each run's private data directory.
    "pubmed": ("eutils.ncbi.nlm.nih.gov", "api.openalex.org", "openalex.s3.amazonaws.com", "doaj.org",
               "raw.githubusercontent.com"),
}
PUBLIC_WEB = "LDR may fetch public web pages linked from search results; these can be on any host."
SETTINGS_BASIS = "LDR defaults plus your RAH settings for LDR. Settings saved inside LDR's own interface don't apply."


def runner_bytes():
    return resources.files("rah").joinpath(RUNNER).read_bytes()


def runner_digest():
    from .usage import observer_bytes
    return digest(runner_bytes() + observer_bytes())


def unavailable(message):
    return Problem("ldr_headless_unavailable", message, 409)


def route(root, book, config, prompt, identity):
    """Return (fingerprint, display, (install, env), components) or raise ldr_headless_unavailable."""
    row = book.route("ldr")
    if not row:
        raise unavailable("Local Deep Research has no supported route on this host. Use App details.")
    try:
        install = upstreams.current(root, "ldr", book)
    except Problem as error:
        raise unavailable("Install Local Deep Research in App details to run it in RAH.") from error
    caps = book.capabilities("ldr")
    try:
        stored = settings.checked_projection(root, "ldr", config, upgrade=lambda: settings.resolve(config, "ldr", caps))
    except Problem as error:
        raise unavailable(str(error) + " Apply your saved settings to run LDR in RAH.") from error
    env = dict(stored["env"])
    provider, model = env.get("LDR_LLM_PROVIDER"), env.get("LDR_LLM_MODEL")
    search = env.get(search_sources.FIELDS["ldr"])
    if not provider or not model:
        raise unavailable("Running LDR in RAH needs an LDR model in Settings (it is set to LDR's native settings). "
                          "To use LDR with its own settings, open it from Classic apps.")
    if not search:
        raise unavailable("Running LDR in RAH needs a search source in Settings → Search sources (it is set to LDR's "
                          "native settings). To use LDR with its own settings, open it from Classic apps.")
    searx = env.get(search_sources.URL_FIELDS["ldr"])
    components = {"app": "ldr", "install": install.name, "lock": row["lock_sha256"],
                  "settings": {"provider": provider, "model": model, "search": search, "searx_url": searx},
                  "credentials": {name: identity(value) for name, value in sorted(env.items()) if name.endswith("_KEY")},
                  "prompt": digest(prompt.encode()), "runner": runner_digest()}
    endpoint = OPENROUTER if provider == "openrouter" else None
    hosts = []
    if endpoint:
        hosts.append(urlsplit(endpoint).hostname)
    if searx:
        hosts.append(urlsplit(searx).hostname or searx)
    hosts += list(FIXED_HOSTS.get(search, ()))
    label = search_sources.LABELS["ldr"].get(search, search)
    display = {"app": "Local Deep Research", "version": row["upstream_version"],
               "mode": "Headless in RAH (LDR Python API, quick summary)",
               "model": {"service": "OpenRouter" if provider == "openrouter" else provider, "endpoint": endpoint,
                         "model": model, "provider_policy": "selected by OpenRouter" if provider == "openrouter" else None},
               "models": {"LLM": f"{provider} · {model}"},
               "search": label, "strategy": STRATEGY, "fixed_hosts": hosts, "public_web": PUBLIC_WEB,
               "destinations": [("OpenRouter (" + endpoint + "); provider selected by OpenRouter") if endpoint else provider,
                                label, PUBLIC_WEB],
               "settings_basis": SETTINGS_BASIS, "history": "Not saved to LDR's history", "deadline_seconds": DEADLINE}
    return digest(canonical(components)), display, (install, env), components


def projection(env, attempt_dir):
    home = private_directory(child(attempt_dir, "home"))
    data = private_directory(child(home, "ldr-data"))
    private = {name: str(home) for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                                             "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR", "TEMP", "TMP")}
    return clean_env() | env | private | {"LDR_DATA_DIR": str(data), "PYTHONNOUSERSITE": "1",
                                          "PYTHON_DOTENV_DISABLED": "1", "NO_PROXY": "*"}


def command(install, attempt_dir):
    runner = child(attempt_dir, RUNNER)
    atomic(runner, runner_bytes())
    return gptr_pipeline.observer_command(attempt_dir, venv_python(install / "venv"), runner, [])


def valid_outputs(attempt_dir, listed):
    """Outputs listed by the runner whose bytes still match their SHA-256."""
    found = {}
    for item in listed or []:
        name = item.get("name") if isinstance(item, dict) else None
        if name not in ("report.md", "citations.ris"):
            continue
        path = child(attempt_dir, "outputs", name)
        if path.is_file() and not path.is_symlink() and file_digest(path) == item.get("sha256"):
            found["report" if name == "report.md" else "citations"] = {"path": path, "sha256": item["sha256"]}
    return found


def _json(path):
    try:
        data = read_json(path, None)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def collect(attempt_dir, process):
    """Apply the status precedence of spec v2 §2.4.2 to a finished attempt."""
    completion = _json(child(attempt_dir, "completion.json"))
    result = _json(child(attempt_dir, "result.json")) or {}
    listed = (completion or {}).get("outputs") or result.get("outputs")
    outputs = valid_outputs(attempt_dir, listed)
    base = {"outputs": outputs, "research_id": result.get("research_id") or None,
            "sources_count": result.get("sources_count"), "ris_error": result.get("ris_error")}
    status = process.status if process else None
    if status == "cancelled":
        return base | {"status": "cancelled", "code": "cancelled", "message": "Operation cancelled."}
    if status == "timeout":
        return base | {"status": "failed", "code": "stage_timeout",
                       "message": "LDR didn't finish within 60 minutes. Charges may already apply."}
    if status == "start_failed":
        return base | {"status": "failed", "code": "worker_start", "message": "LDR could not start. Repair it in App details."}
    if process is not None and process.exit_code:
        reason = (completion or {}).get("reason")
        if reason == "no_report":
            return base | {"status": "failed", "code": "native_output", "message": "LDR returned no report."}
        problem = gptr_pipeline.classify(process.counters, process.exit_code)
        if problem.code != "native_failed":
            return base | {"status": "failed", "code": problem.code, "message": str(problem)}
        detail = f" ({reason})" if isinstance(reason, str) and reason.isidentifier() else ""
        return base | {"status": "failed", "code": "native_failed",
                       "message": f"LDR failed{detail}. Check its model and search settings."}
    listed_names = {item.get("name") for item in (completion or {}).get("outputs") or [] if isinstance(item, dict)}
    verified = {path["path"].name for path in outputs.values()}
    if completion and completion.get("status") == "succeeded" and "report.md" in listed_names and listed_names <= verified:
        return base | {"status": "succeeded", "code": None, "message": None}
    return base | {"status": "failed", "code": "native_output", "message": "LDR finished without a verifiable result."}
