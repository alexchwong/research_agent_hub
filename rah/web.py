import hashlib
import hmac
import io
import re
from secrets import token_hex
import shlex
import socket
import subprocess
import sys
from threading import Lock, Thread
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler, Request
import webbrowser

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from . import installer, search_sources, settings, setup, upstreams
from .common import APPS, Problem, canonical, child, clean_env, digest, executable, id_value, read_json, venv_python, write_json
from .folder_picker import choose_folder
from .jobs import Jobs
from .openrouter import NoRedirect, OpenRouter
from .projects import PDF_LIMIT, REPORT_LIMIT, Projects, citations, structured_citations
from .runs import Runs


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise Problem("invalid_json", "Expected a JSON object.")
    return data


def revision(data):
    value = data.get("revision")
    if type(value) is not int or value < 0:
        raise Problem("revision", "A project revision is required.")
    return value


def question(data, *, preserve=False):
    value = data.get("question")
    if not isinstance(value, str) or not value.strip() or len(value) > 8000 or "\0" in value:
        raise Problem("question", "Enter a question up to 8,000 characters.")
    return value if preserve else value.strip()


def visible_plan(row):
    return {key: row.get(key) for key in ("upstream_version", "prerequisites", "essential", "extras", "estimated_bytes", "evidence_status")}


def gptr_recipients(env, shared_key):
    if env.get("CONFIG_PATH") or settings.LEGACY_GPTR & env.keys():
        raise Problem("native_configuration", "Open GPT Researcher for external or legacy configuration.", 409)
    missing = [role.title() for role, variable in settings.ROLES["gptr"].items() if not env.get(variable)]
    if shared_key and missing and not env.get("OPENAI_API_KEY"):
        raise Problem("missing_models", f"Choose OpenRouter models in Settings for GPT Researcher: {', '.join(missing)}. "
                      "Set shared chat and embedding defaults or configure the missing roles individually.", 409)
    providers = set()
    keys = {"openai": ("OPENAI_API_KEY", "OpenAI"), "openrouter": ("OPENROUTER_API_KEY", "OpenRouter")}
    for label, role in settings.ROLES["gptr"].items():
        provider, separator, model = env.get(role, "openai:default").partition(":")
        if not separator or not model or provider not in keys:
            raise Problem("native_configuration", "Open GPT Researcher to use this model configuration.", 409)
        key, title = keys[provider]
        if not env.get(key):
            if role not in env:
                raise Problem("missing_models", "To use OpenRouter for GPT Researcher, set the shared key and chat and "
                              "embedding models in Settings. Unset roles currently use native OpenAI defaults.", 409)
            raise Problem("needs_credential", f"The GPT Researcher {label.title()} role uses {title}. "
                          f"Add its {title} key or change the model in Settings.", 409)
        providers.add(title)
    source = env.get("RETRIEVER")
    if source not in search_sources.LABELS["gptr"]:
        raise Problem("native_configuration", "Open GPT Researcher to use the configured native search service.", 409)
    if source == "tavily" and not env.get("TAVILY_API_KEY"):
        raise Problem("needs_search_credential", "Add a Tavily key or select keyless PubMed Central in Settings → Search sources.", 409)
    if source == "searx":
        search_sources.local_url(env.get("SEARX_URL", ""))
    return sorted(providers) + [search_sources.LABELS["gptr"][source]]


def browser_control(url, identity, *, stop=False):
    nonce = token_hex(32)
    purpose = "stop" if stop else "identity"
    proof = hmac.new(identity.encode(), f"{purpose}:{nonce}".encode(), "sha256").hexdigest()
    headers = {"X-RAH-Nonce": nonce}
    if stop:
        headers.update({"X-RAH-Proof": proof, "Origin": url.rstrip("/")})
    target = Request(url + "_rah_" + purpose, headers=headers, method="POST" if stop else "GET")
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(target, timeout=0.7) as response:
            return response.status == 204 if stop else (
                response.status == 200 and hmac.compare_digest(response.read(65), proof.encode()))
    except (URLError, OSError, ValueError):
        return False


def create_app(root, book, uv, identity, token, stop, address):
    app = Flask(__name__, static_folder="static", static_url_path="/assets")
    app.config.update(MAX_CONTENT_LENGTH=PDF_LIMIT + 1024 * 1024, MAX_FORM_PARTS=5)
    projects = Projects(root)
    jobs = Jobs()
    runs = Runs(root, book, projects, jobs)
    config_lock = Lock()
    lifecycle_lock = Lock()
    picker_lock = Lock()
    browsers = {"ldr": upstreams.LDR_PORT, "gptr": upstreams.GPTR_PORT}
    services = {}
    export_grants = {}
    provider = OpenRouter(lambda: settings.load(root).values.get("RAH_OPENROUTER_API_KEY"))

    def cap(name):
        return book.capabilities(name)

    def commit_profiles(config, force=False):
        failures = {}
        for name in APPS:
            try:
                settings.install_projection(root, name, settings.resolve(config, name, cap(name)), force)
            except (Problem, OSError) as error:
                failures[name] = str(error) if isinstance(error, Problem) else "Could not write generated settings."
        return failures

    def native(name, **options):
        config = settings.load(root)
        location = upstreams.current(root, name, book)
        stored = settings.applied(root, name)
        if not stored and not config.text:
            stored = settings.install_projection(root, name, settings.resolve(config, name, cap(name)))
        stored = settings.checked_projection(root, name, config, upgrade=lambda: settings.resolve(config, name, cap(name)))
        return location, config, upstreams.launch_spec(root, name, location, stored, **options)

    def public_route():
        if book.route("gptr") and book.route("gptr").get("qualification_only"):
            raise Problem("unverified_route", "Open the standalone candidate app.", 503)
        location, config, spec = native("gptr")
        return location, spec, gptr_recipients(spec.env, config.values.get("RAH_OPENROUTER_API_KEY"))

    def available(name, action):
        if jobs.busy("core") or jobs.busy(name) or jobs.closing:
            raise Problem("busy", "Wait for the current hub operation.", 409)
        upstreams.current(root, name, book)
        if action == "native":
            return []
        if name == "gptr":
            return public_route()[2]
        runs.paper_route(settings.load(root))
        return ["OpenRouter", "Azure"]

    @app.get("/api/workspace")
    def workspace():
        readiness = {}
        with lifecycle_lock, config_lock:
            for name in APPS:
                try:
                    recipients = available(name, "native" if name == "ldr" else "managed")
                    readiness[name] = {"ready": True, "recipients": recipients}
                except (Problem, OSError) as error:
                    readiness[name] = {"ready": False, "reason": str(error) if isinstance(error, Problem) else "Repair this installation in App details.",
                                       "code": error.code if isinstance(error, Problem) else "app_not_installed"}
        return jsonify(projects=projects.list(), chats=projects.list_chats(), readiness=readiness)

    @app.post("/api/search/searx/test")
    def test_searx():
        value = body().get("url")
        if not isinstance(value, str) or len(value) > 4000:
            raise Problem("search_configuration", "Enter a local SearXNG base URL.")
        return jsonify(results=search_sources.test_searx(value))

    @app.post("/api/chats")
    def create_chat():
        data = body()
        with lifecycle_lock:
            return jsonify(projects.create_chat(data.get("project_id"), data.get("title", "New chat"))), 201

    @app.get("/api/chats/trash")
    def trash_list():
        return jsonify(projects.trash_list())

    @app.post("/api/chats/<chat>/trash")
    def trash_chat(chat):
        with lifecycle_lock:
            if jobs.busy() or any(browser_alive(name) and (services[name].get("workspace") or {}).get("chat") == chat for name in browsers):
                raise Problem("chat_busy", "Finish active work and stop the app using this chat folder before deleting it.", 409)
            projects.trash_chat(chat)
        return jsonify(trashed=True)

    @app.post("/api/chats/<chat>/restore")
    def restore_chat(chat):
        with lifecycle_lock:
            projects.restore_chat(chat)
        return jsonify(restored=True)

    @app.route("/api/workspace/storage", methods=["GET", "POST"])
    def workspace_storage():
        result = {}
        if request.method == "POST":
            data = body()
            if data.get("confirm") is not True:
                raise Problem("workspace_confirmation", "Confirm copying existing workspace files to the selected folder.")
            with lifecycle_lock:
                if jobs.busy() or any(browser_alive(name) for name in browsers):
                    raise Problem("workspace_busy", "Finish active work and stop the native apps before changing the workspace folder.", 409)
                if runs.has_reviews() or projects.has_active():
                    raise Problem("workspace_busy", "Finish or discard active requests before changing the workspace folder.", 409)
                result = projects.move_workspace(data.get("path")) or {}
                export_grants.clear()
        return jsonify(path=str(projects.workspace_root), default=str(root), **result)

    @app.post("/api/workspace/storage/choose")
    def workspace_choose():
        if not picker_lock.acquire(blocking=False):
            raise Problem("folder_picker", "A folder picker is already open.", 409)
        try:
            return jsonify(path=choose_folder(projects.workspace_root))
        finally:
            picker_lock.release()

    @app.get("/api/projects/<project>/chats/<chat>")
    def read_chat(project, chat):
        result = projects.get_chat(project, chat)
        for item in result["entries"]:
            if item["state"] == "Awaiting approval":
                item["review_expired"] = not runs.reviewing(item["preview_id"])
        return jsonify(result)

    @app.post("/api/projects/<project>/chats/<chat>")
    def rename_chat(project, chat):
        data = body()
        projects.update_chat(project, chat, title=data.get("title"), app=data.get("app"))
        return jsonify(saved=True)

    @app.post("/api/workspace/requests")
    def accept_request():
        data = body()
        name, action = data.get("app"), data.get("action")
        if not isinstance(name, str) or name not in APPS or action not in ("managed", "native") or (name == "ldr" and action == "managed"):
            raise Problem("request_app", "Select an available app and action.")
        query = question(data, preserve=action == "native") if action == "managed" or data.get("question") else ""
        mode = data.get("mode")
        pdfs = data.get("pdfs", [])
        if not isinstance(pdfs, list) or (pdfs and (name != "pqa" or action != "managed")):
            raise Problem("pdf_selection", "Only PaperQA2 accepts PDF attachments.")
        quote = data.get("quote", "")
        if not isinstance(quote, str) or len(quote) > 8000 or "\0" in quote or (quote and (name != "pqa" or action != "managed")):
            raise Problem("request_quote", "Only PaperQA2 accepts an explicit excerpt up to 8,000 characters.")
        if action == "managed" and name == "gptr":
            if mode not in ("deep", "research_report") or data.get("public") is not True:
                raise Problem("public_confirmation", "Confirm the current question can be sent to the disclosed providers.")
        elif mode is not None:
            raise Problem("request_mode", "This action has no report mode.")
        title = query.strip().splitlines()[0] if query.strip() else APPS[name]
        with lifecycle_lock, config_lock:
            entry = projects.accept(data.get("project_id"), data.get("chat_id"), data.get("entry_id"),
                                    name, action, mode, query, quote, title, pdfs, lambda: available(name, action))
        return jsonify(entry), 200 if entry["replay"] else 201

    @app.post("/api/projects/<project>/requests/<entry>/discard")
    def discard_request(project, entry):
        with lifecycle_lock:
            return jsonify(projects.transition(project, entry, ("Preparing", "Awaiting approval"), "Cancelled", preview_id=None))

    @app.before_request
    def protect():
        expected = address()
        if request.host != expected:
            raise Problem("host", "Unexpected host.", 403)
        if request.method == "OPTIONS":
            raise Problem("origin", "Cross-origin requests are disabled.", 403)
        if request.path.startswith("/api/"):
            if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                raise Problem("unauthorized", "Reopen RAH from the desktop launcher.", 401)
            if request.method == "POST":
                if request.headers.get("Origin") != "http://" + expected:
                    raise Problem("origin", "Unexpected request origin.", 403)
                upload = request.endpoint == "upload"
                required = "multipart/form-data" if upload else "application/json"
                if request.mimetype != required:
                    raise Problem("media_type", "Unexpected request format.", 415)
                if not upload and request.content_length and request.content_length > 512 * 1024:
                    raise Problem("too_large", "Request is too large.", 413)
        runs.expire()

    @app.after_request
    def headers(response):
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        })
        return response

    @app.errorhandler(Problem)
    def issue(error):
        return jsonify(code=error.code, message=str(error)), error.status

    @app.errorhandler(HTTPException)
    def http_issue(error):
        return jsonify(code="http_error", message=error.name), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.error("Local request could not complete (%s)", type(error).__name__)
        return jsonify(code="failed", message="The local operation could not complete."), 500

    @app.get("/")
    def shell():
        return render_template("index.html")

    @app.get("/_identity")
    def identify():
        nonce = request.args.get("nonce", "")
        if not re.fullmatch("[0-9a-f]{64}", nonce):
            raise Problem("nonce", "Invalid challenge.")
        return jsonify(signature=hmac.new(identity.encode(), nonce.encode(), hashlib.sha256).hexdigest())

    def up_to_date(name):
        return setup.current(root, book, name)

    def browser_record(name):
        return child(root, "hub", "runtime", f"{name}-browser.json")

    def recover_browser(name):
        if name not in browsers:
            return None
        try:
            record = read_json(browser_record(name))
        except (OSError, ValueError, Problem):
            return None
        if not isinstance(record, dict) or not all(
            isinstance(record.get(key), str) and re.fullmatch(pattern, record[key])
            for key, pattern in {"identity": r"[0-9a-f]{64}", "generation": r"[0-9a-f]{32}",
                                 "revision": r"[0-9a-f]{64}"}.items()
        ):
            return None
        url = f"http://127.0.0.1:{browsers[name]}/"
        if browser_control(url, record["identity"]):
            service = {**record, "process": None, "url": url, "started": monotonic(), "ready": True}
            services[name] = service
            return service
        return None

    def browser_alive(name):
        service = services.get(name)
        if service:
            process = service["process"]
            return (process.poll() is None if process is not None else
                    browser_control(service["url"], service["identity"]))
        return recover_browser(name) is not None

    def browser_status(name, config):
        service = services.get(name) or recover_browser(name)
        if not service:
            return {"state": "Stopped"}
        if not browser_alive(name):
            return {"state": "Stopped" if service.get("stopping") else "Exited"}
        prior = service.get("ready", False)
        url = service["url"]
        ready = service["process"] is None or browser_control(url, service["identity"])
        service["ready"] = ready
        if service["process"] is not None and service["process"].poll() is not None:
            return {"state": "Exited"}
        if service.get("stopping"):
            return {"state": "Stopping"}
        if ready:
            return {"state": "Ready", "url": url, "generation": service["generation"],
                    "restart_needed": service["revision"] != config.revision or service.get("profile_version") != settings.PROFILE_VERSIONS[name]}
        if prior or monotonic() - service["started"] >= 120:
            return {"state": "Waiting for the native app to become ready"}
        return {"state": "Starting"}

    @app.get("/api/apps")
    def apps():
        rows = []
        config = settings.load(root)
        with lifecycle_lock:
            for name, title in APPS.items():
                route = book.route(name)
                installed = installer.install_location(root, name)
                state = "Needs setup" if route else "No install route for this host"
                if up_to_date(name):
                    state = "Available to run"
                elif installed and route:
                    state = "Reinstall at the current pin"
                try:
                    profile = settings.resolve(config, name, cap(name))
                    config_state = "Model settings saved" if profile["models"] else "Uses native model settings"
                    stored = settings.applied(root, name)
                    if installed and (not stored or stored.get("revision") != config.revision):
                        config_state = "Apply settings before launch"
                except (Problem, ValueError, AttributeError) as error:
                    config_state = str(error) if isinstance(error, Problem) else "Apply settings before launch"
                metadata = browser_status(name, config) if name in services or browser_alive(name) else {}
                rows.append({"id": name, "name": title, "state": metadata.get("state", state),
                             "configuration": config_state, "installable": bool(route), "current": up_to_date(name),
                             "version": installed[0]["upstream_version"] if installed else None,
                             "candidate": installer.PINS[name], "service": metadata,
                             "can_stop": bool(browser_alive(name)),
                             "qualification_only": bool(route and route.get("qualification_only")),
                             "paper_analysis": runs.availability() if name == "pqa" else None})
        return jsonify(rows)

    def core_plan():
        rows = {name: book.route(name) for name in APPS}
        return rows, digest(canonical(rows))

    @app.get("/api/apps/core/install-plan")
    def install_core_plan():
        rows, checksum = core_plan()
        return jsonify(digest=checksum, apps=[
            {"id": name, "name": title, "plan": visible_plan(rows[name]) if rows[name] else None,
             "installed": up_to_date(name)} for name, title in APPS.items()
        ])

    def ensure_uv():
        if not uv or not uv.is_file():
            raise Problem("uv_missing", "Run rah setup with the absolute path to the approved uv executable.", 503)

    @app.post("/api/apps/core/install")
    def install_core():
        rows, checksum = core_plan()
        if body().get("digest") != checksum:
            raise Problem("install_plan", "Review the current core installation plan first.", 409)
        ensure_uv()

        with lifecycle_lock:
            if jobs.busy():
                raise Problem("busy", "Wait for the current operation before installing core apps.", 409)
            return jsonify(operation_id=jobs.start("core", lambda job: setup.install_core(root, book, uv, config_lock, job, browser_alive))), 202

    @app.get("/api/apps/<name>/install-plan")
    def install_plan(name):
        route = book.route(name)
        if not route:
            raise Problem("route_unavailable", "No install bundle for this host. See docs/releases.md.", 503)
        return jsonify(plan=visible_plan(route), digest=installer.plan_digest(route))

    @app.post("/api/apps/<name>/install")
    def install(name):
        route = book.route(name)
        if not route or body().get("digest") != installer.plan_digest(route):
            raise Problem("install_plan", "Review the current installation plan first.", 409)
        ensure_uv()
        with lifecycle_lock:
            if jobs.busy("core") or browser_alive(name):
                raise Problem("busy", "Stop this app or wait for core setup before reinstalling.", 409)
            return jsonify(operation_id=jobs.start(name, lambda job: {"location": setup.install_one(root, book, uv, config_lock, name, job)})), 202

    def native_request(data, name):
        if not data.get("entry_id"):
            return None
        row = projects.entry(data.get("project_id"), data["entry_id"])
        if row["app"] != name or row["action"] != "native" or row["state"] != "Preparing":
            raise Problem("request_state", "This native request has already advanced.", 409)
        return row

    def finish_native(entry, state, reason=None):
        if entry:
            projects.transition(entry["project_id"], entry["id"], ("Preparing",), state, reason=reason)

    def native_ready(name, entry, data, service, state, workspace_target):
        if state["state"] != "Ready":
            return jsonify(state | {"opened": False})
        open_requested = entry is not None or data.get("open") is True or "ready_generation" in data
        if open_requested and not service.get("opened"):
            url = state["url"]
            prefilled = False
            if entry and (entry["question"] or name == "ldr" and workspace_target):
                ticket = token_hex(32)
                grant = token_hex(32) if name == "ldr" and workspace_target else None
                raw = canonical({"ticket": ticket, "question": entry["question"], "grant": grant})
                proof = hmac.new(service["identity"].encode(), b"handoff:" + hashlib.sha256(raw).hexdigest().encode(), "sha256").hexdigest()
                if service.get("handoff_hub") == "http://" + address():
                    try:
                        target = Request(url + "_rah_handoff_stage", data=raw, headers={"X-RAH-Proof": proof,
                                         "Origin": url.rstrip("/"), "Content-Type": "application/json"})
                        with build_opener(ProxyHandler({}), NoRedirect()).open(target, timeout=1.5) as response:
                            prefilled = response.status == 204
                    except (URLError, OSError, ValueError):
                        pass
                if prefilled:
                    if grant:
                        tick = monotonic()
                        for key in list(export_grants):
                            if export_grants[key][0] <= tick:
                                del export_grants[key]
                        if len(export_grants) >= 128:
                            del export_grants[next(iter(export_grants))]
                        export_grants[grant] = (tick + 8 * 3600, workspace_target, entry["id"])
                    url += "#rah-handoff=" + ticket
            finish_native(entry, "Opened")
            service["opened"] = True
            Thread(target=webbrowser.open, args=(url,), daemon=True).start()
            return jsonify(state | {"opened": True} | ({"prefill_prepared": prefilled} if entry else {}))
        finish_native(entry, "Opened")
        return jsonify(state | {"opened": True})

    @app.post("/_rah_native_export")
    def native_export():
        grant = request.headers.get("X-RAH-Export", "")
        fmt = request.headers.get("X-RAH-Format")
        if (not re.fullmatch(r"[0-9a-f]{64}", grant) or fmt not in ("md", "ris") or request.headers.get("Origin")
                or request.mimetype != "application/octet-stream" or not request.content_length or request.content_length > REPORT_LIMIT):
            raise Problem("export_handoff", "Invalid native export handoff.", 403)
        with lifecycle_lock:
            record = export_grants.get(grant)
            if not record or record[0] <= monotonic():
                raise Problem("export_handoff", "The export handoff expired. Download and attach the export in RAH.", 403)
            _, workspace_target, entry = record
            artifact = projects.register(workspace_target["scope"], io.BytesIO(request.get_data()),
                                         "LDR export." + fmt, "report", app="ldr", version=installer.PINS["ldr"], entry=entry)
        return jsonify(artifact_id=artifact), 201

    @app.post("/api/apps/<name>/launch")
    def launch(name):
        data = body()
        with lifecycle_lock:
            if jobs.busy("core") or jobs.busy(name) or jobs.closing:
                raise Problem("busy", "Wait for the app's current operation.", 409)
            entry = native_request(data, name)
            workspace_target = ({"scope": entry["project_id"], "chat": entry["chat_id"]} if entry else
                                {"scope": id_value(data["project_id"]), "chat": None} if data.get("project_id") and name == "gptr" else None)
            folder = projects.folder(workspace_target["scope"], workspace_target["chat"]) if workspace_target else None
            if name in browsers and browser_alive(name):
                service = services[name]
                confirming = "ready_generation" in data
                if confirming and data["ready_generation"] != service["generation"]:
                    raise Problem("native_launch_changed", "The native app changed. Open it again.", 409)
                state = browser_status(name, settings.load(root))
                if name == "gptr" and (workspace_target is not None and service.get("workspace") != workspace_target
                                       or state.get("restart_needed") and entry):
                    raise Problem("native_workspace_restart", "Stop and reopen GPT Researcher Classic to apply this folder or its saved settings.", 409)
                if not confirming:
                    service["opened"] = False
                return native_ready(name, entry, data, service, state, workspace_target)
            if name == "pqa":
                location = upstreams.current(root, name, book)
                config = settings.load(root)
                script = child(root, "apps", name, "launch", "native.py")
                note = "Run this standalone command in a terminal. Add native subcommands and options as needed."
                try:
                    settings.checked_projection(root, name, config, effective=settings.resolve(config, name, cap(name)))
                    command = [str(venv_python(location / "venv")), str(script)]
                except Problem:
                    command = [str(executable(location / "venv", "pqa")), "--help"]
                    note = "Run this native command in a terminal. Replace --help with a native command and options. To select a native settings file, pass --settings with its absolute path without the .json extension."
                if sys.platform == "win32":
                    command_line = "& " + " ".join("'" + item.replace("'", "''") + "'" for item in command)
                else:
                    command_line = shlex.join(command)
                finish_native(entry, "Command shown")
                return jsonify(command=command, command_line=command_line, note=note)
            with config_lock:
                identity_token = token_hex(32)
                hub_origin = "http://" + address()
                if name == "ldr":
                    location = upstreams.current(root, name, book)
                    config = settings.load(root)
                    try:
                        effective = settings.resolve(config, name, cap(name))
                        profile = settings.checked_projection(root, name, config, effective=effective, upgrade=lambda: effective)
                    except Problem:
                        native_values = {key: value for key, value in config.values.items() if key.startswith("RAH_NATIVE_LDR__")}
                        native_config = settings.Config(config.text, native_values, config.revision)
                        profile = settings.resolve(native_config, name, cap(name))
                    spec = upstreams.launch_spec(root, name, location, profile, browser=True, identity=identity_token, hub_origin=hub_origin)
                else:
                    location, config, spec = native(name, browser=True, identity=identity_token, workspace=folder, hub_origin=hub_origin)
            if "ready_generation" in data:
                if not browser_alive(name) or data["ready_generation"] != services[name]["generation"]:
                    raise Problem("native_launch_changed", "The native app changed. Open it again.", 409)
                service = services[name]
                return native_ready(name, entry, data, service, browser_status(name, config), workspace_target)
            if browser_alive(name):
                raise Problem("native_launch_changed", "The native app changed. Open it again.", 409)
            if not browser_alive(name):
                try:
                    with socket.create_connection(("127.0.0.1", browsers[name]), timeout=0.5):
                        raise Problem("port_in_use", "The native port is in use by a listener RAH could not verify. "
                                      "Close it separately or reopen its existing app.", 409)
                except (ConnectionRefusedError, TimeoutError):
                    # Windows retries a refused loopback SYN for ~2 s, so the probe times out instead of
                    # being refused. A live loopback listener completes the handshake immediately.
                    pass
                record = {"revision": config.revision, "identity": identity_token, "generation": token_hex(16),
                          "handoff_hub": hub_origin, "profile_version": settings.PROFILE_VERSIONS[name],
                          "workspace": workspace_target if name == "gptr" else None}
                options = {"creationflags": subprocess.DETACHED_PROCESS} if sys.platform == "win32" else {"start_new_session": True}
                try:
                    upstreams.refresh_browser(name, location)
                    write_json(browser_record(name), record)
                    proc = subprocess.Popen(spec.command, cwd=spec.directory, env=spec.env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
                except OSError as error:
                    raise Problem("native_launch_failed", "The native app could not start. Review its setup.", 503) from error
                services[name] = {**record, "process": proc, "started": monotonic(), "url": spec.url}
            services[name]["opened"] = False
            return native_ready(name, entry, data, services[name], browser_status(name, config), workspace_target)

    @app.get("/api/apps/<name>/browser")
    def observe_browser(name):
        if name not in browsers:
            raise Problem("unknown_app", "Unknown browser application.", 404)
        with lifecycle_lock:
            return jsonify(browser_status(name, settings.load(root)))

    @app.post("/api/apps/<name>/stop")
    def stop_browser(name):
        with lifecycle_lock:
            if name not in browsers or not browser_alive(name):
                raise Problem("not_running", "There is no native process owned by this hub to stop.", 409)
            service = services[name]
            if service["process"] is None:
                if not browser_control(service["url"], service["identity"], stop=True):
                    raise Problem("native_launch_changed", "The native app could not be verified for shutdown.", 409)
            else:
                service["process"].terminate()
            service["stopping"] = True
        return jsonify(state="Stopping")

    @app.get("/api/operations/<operation>")
    def observe(operation):
        return jsonify(jobs.get(id_value(operation)).status())

    @app.post("/api/operations/<operation>/cancel")
    def cancel(operation):
        jobs.get(id_value(operation)).cancel()
        return jsonify(cancel_requested=True)

    @app.route("/api/settings", methods=["GET", "POST"])
    def setting():
        with lifecycle_lock, config_lock:
            if request.method == "POST":
                data = body()
                config = settings.update(root, data.get("revision"), data.get("changes", {}), data.get("clear", []))
                failures = commit_profiles(config)
            else:
                config = settings.load(root)
                failures = {}
            result = settings.public_config(root, config, cap)
            for name in browsers:
                if browser_alive(name) and (services[name]["revision"] != config.revision
                                          or services[name].get("profile_version") != settings.PROFILE_VERSIONS[name]):
                    result["states"][name]["running"] = "Restart needed"
            return jsonify(result | {"failures": failures})

    @app.post("/api/settings/apply")
    def apply_settings():
        with config_lock:
            data = body()
            config = settings.load(root)
            if data.get("revision") != config.revision:
                raise Problem("stale_settings", "Reload the current settings.", 409)
            return jsonify(failures=commit_profiles(config, data.get("regenerate") is True))

    @app.get("/api/models/<family>")
    def models(family):
        if family not in {"chat", "embedding"}:
            raise Problem("model_family", "Unknown model family.", 404)
        return jsonify(provider.choices(family) if request.args.get("details") == "1" else provider.models(family))

    @app.get("/api/usage")
    def usage():
        return jsonify(provider.usage())

    @app.post("/api/usage/refresh")
    def refresh():
        return jsonify(provider.refresh())

    @app.route("/api/projects", methods=["GET", "POST"])
    def project_list():
        if request.method == "POST":
            with lifecycle_lock:
                return jsonify(id=projects.create(body().get("name"))), 201
        return jsonify(projects.list())

    @app.get("/api/projects/<project>")
    def project_get(project):
        chat = request.args.get("chat")
        data = projects.get(project, chat)
        data["folder"] = str(projects.folder(project, chat))
        data["native_outputs"] = projects.native_outputs(project, chat)
        for artifact in data["artifact"]:
            if artifact["kind"] == "answer":
                path = projects.artifact_path(artifact)
                artifact["has_bib"] = child(path.parent, path.stem + ".bib").is_file()
        availability = runs.availability()
        data["capabilities"] = {
            "structured_citations": (book.route("ldr") or {}).get("structured_citations") == "ldr-ris-v1",
            "paper_route": availability["available"], "paper_reason": availability.get("reason"),
        }
        return jsonify(data)

    @app.post("/api/projects/<project>/open-folder")
    def open_folder(project):
        folder = projects.folder(project, body().get("chat_id"))
        command = ("explorer.exe" if sys.platform == "win32" else "open" if sys.platform == "darwin" else "xdg-open")
        try:
            subprocess.Popen([command, folder], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            raise Problem("file_manager", "No file manager is available. Use Copy path instead.", 503) from error
        return jsonify(opened=True, path=str(folder))

    @app.get("/api/projects/<project>/native-output/<filename>")
    def download_native_output(project, filename):
        path = projects.native_output(project, request.args.get("chat"), filename)
        return send_file(path, as_attachment=True, download_name=path.name, mimetype="application/octet-stream")

    @app.post("/api/projects/<project>/native-output/import")
    def import_native_output(project):
        data = body()
        with lifecycle_lock:
            chat = data.get("chat_id")
            path = projects.native_output(project, chat, data.get("name"))
            with path.open("rb") as handle:
                artifact = projects.register(project, handle, path.name, "pdf" if path.suffix.lower() == ".pdf" else "report",
                                             app="gptr", version=installer.PINS["gptr"], chat=chat)
        return jsonify(artifact_id=artifact), 201

    @app.post("/api/projects/<project>/files")
    def upload(project):
        files = request.files.getlist("file")
        if len(files) != 1 or len(request.files) != 1:
            raise Problem("file_input", "Select a single file.")
        entry = request.form.get("entry_id")
        attachment = request.form.get("attachment_id")
        if attachment and not entry:
            raise Problem("attachment_owner", "An attachment ID needs a request.")
        native_entry = projects.entry(project, entry) if entry else None
        name = native_entry["app"] if native_entry and native_entry["action"] == "native" else None
        with lifecycle_lock:
            artifact = projects.register(project, files[0].stream, files[0].filename, request.form.get("kind"),
                                         app=name, version=installer.PINS[name] if name else None,
                                         entry=entry, attachment_id=attachment, chat=request.form.get("chat_id"))
        return jsonify(artifact_id=artifact), 201

    @app.get("/api/projects/<project>/artifacts/<artifact>")
    def download(project, artifact):
        row = projects.artifact(project, artifact)
        if row["kind"] == "native_link":
            return jsonify(url=row["native_url"])
        path = projects.artifact_path(row)
        if request.args.get("format") == "bib":
            path = child(path.parent, path.stem + ".bib")
            filename = "native-citations.bib"
        else:
            filename = row["original_name"]
        if not path.is_file():
            raise Problem("file_missing", "The original file was moved or removed.", 404)
        return send_file(path, as_attachment=True, download_name=filename, mimetype="application/octet-stream")

    @app.post("/api/projects/<project>/papers/parse")
    def parse_papers(project):
        projects.get(project)
        data = body()
        artifact = data.get("artifact_id")
        if artifact:
            if (book.route("ldr") or {}).get("structured_citations") != "ldr-ris-v1":
                raise Problem("unverified_export", "LDR export import has not been verified. Use Paste citations.", 503)
            row = projects.artifact(project, artifact)
            if row["kind"] != "report" or not row["storage_name"].endswith(".ris"):
                raise Problem("export_type", "Select an LDR RIS export.")
            return jsonify([item | {"source_artifact_id": artifact} for item in structured_citations(projects.artifact_path(row))])
        return jsonify(citations(data.get("text")))

    @app.post("/api/projects/<project>/papers")
    def save_papers(project):
        data = body()
        projects.save_papers(project, revision(data), data.get("rows"))
        return jsonify(saved=True)

    @app.post("/api/projects/<project>/gptr-reports")
    def gptr(project):
        projects.get(project)
        data = body()
        entry_id = data.get("entry_id")
        entry = projects.entry(project, entry_id) if entry_id else None
        with lifecycle_lock, config_lock:
            if entry:
                if entry["app"] != "gptr" or entry["action"] != "managed":
                    raise Problem("request_app", "This is not a public report request.", 409)
                if set(data) - {"entry_id", "recipients"}:
                    raise Problem("request_replay", "Only the saved public question may be dispatched.", 409)
                if entry["operation_id"]:
                    return jsonify(operation_id=entry["operation_id"]), 202
                query, mode = entry["question"], entry["mode"]
            else:
                query, mode = question(data), data.get("mode")
                if data.get("public") is not True or mode not in ("deep", "research_report"):
                    raise Problem("public_confirmation", "Confirm a public question and select a documented report mode.")
            if jobs.busy("core"):
                raise Problem("busy", "Wait for core setup to finish.", 409)
            _, spec, recipients = public_route()
            if (entry or "recipients" in data) and data.get("recipients") != recipients:
                raise Problem("public_confirmation", "The model or search recipients changed. Review them before running again.", 409)
            version = book.route("gptr")["upstream_version"]
            operation = jobs.start("gptr", lambda job: runs.public_report(project, query, mode, spec, version, job, entry=entry_id),
                                   **runs.observe_request(project, entry_id))
            return jsonify(operation_id=operation), 202

    @app.post("/api/projects/<project>/pqa-preview")
    def pqa_preview(project):
        data = body()
        entry_id = data.get("entry_id")
        with lifecycle_lock, config_lock:
            if jobs.closing or jobs.busy("core"):
                raise Problem("busy", "Wait for the current hub operation.", 409)
            ids = data.get("pdfs")
            if not entry_id:
                return jsonify(runs.preview(project, ids, question(data)))
            entry = projects.entry(project, entry_id)
            if set(data) - {"entry_id", "pdfs"} or entry["app"] != "pqa" or entry["action"] != "managed":
                raise Problem("request_app", "Only the saved PaperQA2 request can be reviewed.", 409)
            if entry["state"] == "Awaiting approval" and entry["input_ids"] != ids:
                raise Problem("request_replay", "Start a new attempt to change the reviewed selection.", 409)
            if entry["state"] not in ("Preparing", "Awaiting approval"):
                raise Problem("request_state", "Start a new attempt to review this request.", 409)
            return jsonify(runs.preview(project, ids, entry["question"], entry=entry_id, quote=entry["quote"]))

    @app.post("/api/projects/<project>/pqa-approve")
    def pqa_approve(project):
        data = body()
        key = id_value(data.get("preview_id"))
        if data.get("approve") is True and data.get("account_exports_checked") is not True:
            raise Problem("approval", "Confirm that the key excludes this run from additional exports.")
        if type(data.get("approve")) is not bool:
            raise Problem("approval", "Choose Approve or Decline.")
        with lifecycle_lock:
            if data["approve"] and jobs.busy("core"):
                raise Problem("busy", "Wait for core setup to finish.", 409)
            return jsonify(operation_id=runs.approve(project, key, data.get("binding"), data["approve"]))

    @app.post("/api/quit")
    def quit_hub():
        with lifecycle_lock:
            jobs.close_if_idle()
        def finish():
            sleep(0.15)
            stop()
        Thread(target=finish, daemon=True).start()
        return jsonify(quitting=True)

    return app
