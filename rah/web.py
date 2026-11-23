import hashlib
import hmac
import re
from secrets import token_hex
import shlex
import socket
import subprocess
import sys
from threading import Lock, Thread
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, Request
from urllib.parse import urlsplit
import webbrowser

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from . import installer, settings, setup, upstreams
from .common import APPS, Problem, canonical, child, clean_env, digest, executable, id_value, venv_python
from .jobs import Jobs
from .openrouter import NoRedirect, OpenRouter
from .projects import PDF_LIMIT, Projects, UNFILED, citations, structured_citations
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


def question(data):
    value = data.get("question")
    if not isinstance(value, str) or not value.strip() or len(value) > 8000 or "\0" in value:
        raise Problem("question", "Enter a question up to 8,000 characters.")
    return value.strip()


def visible_plan(row):
    return {key: row.get(key) for key in ("upstream_version", "prerequisites", "essential", "extras", "estimated_bytes", "evidence_status")}


def create_app(root, book, uv, identity, token, stop, address):
    app = Flask(__name__, static_folder="static", static_url_path="/assets")
    app.config.update(MAX_CONTENT_LENGTH=PDF_LIMIT + 1024 * 1024, MAX_FORM_PARTS=5)
    projects = Projects(root)
    jobs = Jobs()
    runs = Runs(root, book, projects, jobs)
    config_lock = Lock()
    lifecycle_lock = Lock()
    browsers = {"ldr": upstreams.LDR_PORT, "gptr": upstreams.GPTR_PORT}
    services = {}
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
        stored = settings.checked_projection(root, name, config)
        return location, config, upstreams.launch_spec(root, name, location, stored, **options)

    def public_route():
        if book.route("gptr") and book.route("gptr").get("qualification_only"):
            raise Problem("unverified_route", "Open the standalone candidate app.", 503)
        location, _, spec = native("gptr")
        env = spec.env
        if env.get("CONFIG_PATH") or settings.LEGACY_GPTR & env.keys():
            raise Problem("native_configuration", "Open GPT Researcher for external or legacy configuration.", 409)
        providers = set()
        for role in settings.ROLES["gptr"].values():
            provider, separator, model = env.get(role, "openai:default").partition(":")
            keys = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
            if not separator or not model or provider not in keys:
                raise Problem("native_configuration", "Open GPT Researcher to use this model configuration.", 409)
            if not env.get(keys[provider]):
                raise Problem("needs_credential", f"Add the {provider} credential for GPT Researcher in Settings.", 409)
            providers.add({"openai": "OpenAI", "openrouter": "OpenRouter"}[provider])
        if env.get("RETRIEVER", "tavily").strip() != "tavily":
            raise Problem("native_configuration", "Open GPT Researcher to use the configured native search service.", 409)
        if not env.get("TAVILY_API_KEY"):
            raise Problem("needs_credential", "Add a Tavily key in Settings to run GPT Researcher here.", 409)
        return location, spec, sorted(providers) + ["Tavily"]

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
        return jsonify(projects=projects.list(), chats=projects.list_chats(), readiness=readiness, unfiled=UNFILED)

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
        query = question(data) if action == "managed" or data.get("question") else ""
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
        title = query.splitlines()[0] if query else APPS[name]
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

    def browser_alive(name):
        service = services.get(name)
        return service and service["process"].poll() is None

    def browser_status(name, config):
        service = services.get(name)
        if not service:
            return {"state": "Stopped"}
        if not browser_alive(name):
            return {"state": "Stopped" if service.get("stopping") else "Exited"}
        prior = service.get("ready", False)
        opener = build_opener(ProxyHandler({}), NoRedirect())
        url = service["url"]
        ready = False
        try:
            target = Request(url + "_rah_ready", headers={"X-RAH-Native": service["identity"]}) if name == "gptr" else url
            with opener.open(target, timeout=0.7) as response:
                if name == "gptr":
                    ready = response.status == 204
                else:
                    page = response.read(256 * 1024).lower()
                    ready = b"local deep research" in page or b"native ldr fixture" in page
        except HTTPError as error:
            if name == "ldr" and error.code in (301, 302, 303, 307, 308):
                redirect = urlsplit(error.headers.get("Location", ""))
                ready = (not redirect.netloc or redirect.netloc == urlsplit(url).netloc) and redirect.path in {
                    "/auth/login", "/login", "/auth/register", "/register",
                }
        except (URLError, OSError, ValueError):
            pass
        service["ready"] = ready
        if service["process"].poll() is not None:
            return {"state": "Exited"}
        if ready:
            return {"state": "Ready", "url": url, "generation": service["generation"], "restart_needed": service["revision"] != config.revision}
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
                metadata = browser_status(name, config) if name in services else {}
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

    @app.post("/api/apps/<name>/launch")
    def launch(name):
        data = body()
        with lifecycle_lock:
            if jobs.busy("core") or jobs.busy(name) or jobs.closing:
                raise Problem("busy", "Wait for the app's current operation.", 409)
            entry = native_request(data, name)
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
                if name == "ldr":
                    location = upstreams.current(root, name, book)
                    config = settings.load(root)
                    try:
                        effective = settings.resolve(config, name, cap(name))
                        profile = settings.checked_projection(root, name, config, effective=effective)
                    except Problem:
                        native_values = {key: value for key, value in config.values.items() if key.startswith("RAH_NATIVE_LDR__")}
                        native_config = settings.Config(config.text, native_values, config.revision)
                        profile = settings.resolve(native_config, name, cap(name))
                    spec = upstreams.launch_spec(root, name, location, profile, browser=True, identity=identity_token)
                else:
                    location, config, spec = native(name, browser=True, identity=identity_token)
            if "ready_generation" in data:
                service = services.get(name)
                if not service or not browser_alive(name) or data["ready_generation"] != service["generation"]:
                    raise Problem("native_launch_changed", "The native app changed. Open it again.", 409)
                state = browser_status(name, config)
                if state["state"] == "Ready":
                    finish_native(entry, "Opened")
                    if not service.get("opened"):
                        service["opened"] = True
                        Thread(target=webbrowser.open, args=(state["url"],), daemon=True).start()
                return jsonify(state | {"opened": state["state"] == "Ready"})
            if not browser_alive(name):
                try:
                    with socket.create_connection(("127.0.0.1", browsers[name]), timeout=0.5):
                        raise Problem("port_in_use", "The native port is in use. Close the other listener or use its existing window.", 409)
                except ConnectionRefusedError:
                    pass
                if name == "gptr" and not (location / "gptr_browser.py").is_file():
                    raise Problem("launcher_missing", "Reinstall GPT Researcher to prepare its native browser launcher.", 409)
                options = {"creationflags": subprocess.DETACHED_PROCESS} if sys.platform == "win32" else {"start_new_session": True}
                try:
                    proc = subprocess.Popen(spec.command, cwd=spec.directory, env=spec.env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
                except OSError as error:
                    raise Problem("native_launch_failed", "The native app could not start. Review its setup.", 503) from error
                services[name] = {"process": proc, "revision": config.revision, "started": monotonic(),
                                  "url": spec.url, "identity": identity_token, "generation": token_hex(16)}
            services[name]["opened"] = False
            state = browser_status(name, config)
            if state["state"] == "Ready":
                finish_native(entry, "Opened")
            if (entry is not None or data.get("open") is True) and state["state"] == "Ready":
                services[name]["opened"] = True
                Thread(target=webbrowser.open, args=(state["url"],), daemon=True).start()
            return jsonify(state | {"opened": state["state"] == "Ready"})

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
            services[name]["process"].terminate()
            services[name]["stopping"] = True
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
        with config_lock:
            if request.method == "POST":
                data = body()
                config = settings.update(root, data.get("revision"), data.get("changes", {}), data.get("clear", []))
                failures = commit_profiles(config)
            else:
                config = settings.load(root)
                failures = {}
            result = settings.public_config(root, config, cap)
            for name, service in tuple(services.items()):
                if service["process"].poll() is None and service["revision"] != config.revision:
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
            return jsonify(id=projects.create(body().get("name"))), 201
        return jsonify(projects.list())

    @app.get("/api/projects/<project>")
    def project_get(project):
        data = projects.get(project)
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
        projects.get(project)
        command = ("explorer.exe" if sys.platform == "win32" else "open" if sys.platform == "darwin" else "xdg-open")
        subprocess.Popen([command, projects.directory(project)], env=clean_env(), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify(opened=True)

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
        artifact = projects.register(project, files[0].stream, files[0].filename, request.form.get("kind"),
                                     app=name, version=installer.PINS[name] if name else None,
                                     entry=entry, attachment_id=attachment)
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
                if set(data) != {"entry_id"}:
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
            _, spec, _ = public_route()
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
