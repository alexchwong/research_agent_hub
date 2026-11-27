"""Deterministic guided recipe controller.

RAH stores the question, approvals, step state, inputs and original outputs. The upstream apps do the
research. The recipe table below is fixed in code: no model decides which step or tool runs next.
"""
from datetime import datetime, timedelta, timezone
import hmac
import io
import json
from pathlib import Path
import re
import secrets
import shutil
from threading import Condition, Lock, Thread
from time import monotonic
from uuid import UUID, uuid4

from . import gptr_pipeline, installer, oa, pqa_profiles, research_profiles, scope_gate, search_sources, settings, upstreams
from .common import Problem, atomic, canonical, child, digest, file_digest, id_value, private_directory, read_json, venv_python
from .projects import citations, now, structured_citations


RECIPE_VERSION = 1
RECIPES = {
    "literature_review": ("survey", "scope_gate", "ldr_discovery", "papers", "paper_analysis"),
    "deep_research": ("survey", "scope_gate", "gptr_deep", "papers", "paper_analysis"),
    "rapid_research": ("survey", "scope_gate", "gptr_rapid"),
    "independent_corroboration": ("survey", "scope_gate", "ldr_comparison", "gptr_comparison"),
    "analyse_my_papers": ("survey", "scope_gate", "paper_selection", "paper_analysis"),
}
PURPOSES = {"literature_review": "Literature review", "deep_research": "Deep research", "rapid_research": "Rapid research",
            "independent_corroboration": "Independent corroboration", "analyse_my_papers": "Analyse my papers"}
STEPS = {
    "survey": ("gptr", "managed", "gptr"), "scope_gate": ("rah", "checkpoint", "scope_gate"),
    "gptr_deep": ("gptr", "managed", "gptr"), "gptr_rapid": ("gptr", "managed", "gptr"),
    "gptr_comparison": ("gptr", "managed", "gptr"), "ldr_discovery": ("ldr", "native", "ldr"),
    "ldr_comparison": ("ldr", "native", "ldr"), "papers": ("rah", "checkpoint", None),
    "paper_selection": ("rah", "checkpoint", None), "paper_analysis": ("pqa", "managed", "pqa"),
}
REPORT_MODES = {"gptr_deep": ("deep", "deep"), "gptr_rapid": ("research_report", "rapid"),
                "gptr_comparison": ("research_report", "comparison")}
PUBLIC_STEPS = {"gptr_deep", "gptr_rapid", "gptr_comparison", "ldr_discovery", "ldr_comparison"}
PARTIAL = {"literature_review": "discovery_only", "deep_research": "report_only"}
TERMINAL_STEP = {"succeeded", "failed", "cancelled", "skipped"}
TERMINAL_RUN = {"succeeded", "failed", "cancelled"}
DISPLAY = {"queued": "Queued", "surveying": "Surveying", "running": "Running", "awaiting_scope": "Awaiting scope",
           "brief_approved": "Brief approved", "waiting_app": "Waiting for app", "waiting_native": "Waiting for native step",
           "awaiting_papers": "Awaiting papers", "awaiting_disclosure": "Awaiting disclosure approval",
           "reconfirm_recipients": "Awaiting recipient re-confirmation", "paused": "Paused", "interrupted": "Interrupted",
           "succeeded": "Complete", "failed": "Failed", "cancelled": "Cancelled", "skipped": "Skipped"}
PREVIEW_TTL = 600
CONSENT_TTL = timedelta(hours=24)
GRANT_TTL = 8 * 3600
EXPORT_NAMES = {"report": ("ldr_report", ".md"), "citations": ("ldr_citations", ".ris")}
ACTIONS = {
    "ask_more": {"round_id", "answers"}, "preview_scope": {"answers", "instructions", "broad"},
    "approve_scope": {"digest", "constraints_reviewed"}, "use_survey_result": set(),
    "launch_native": {"route_digest", "attest", "reopen"}, "confirm_native_result": {"artifact_ids", "attribution", "public_provenance", "same_result"},
    "set_papers": {"source_artifact_id", "rows"}, "start_acquisition": {"paper_ids"},
    "approve_private_lookup": {"paper_id", "stage", "digest", "approve"},
    "select_corpus": {"pdf_ids", "analysis_question", "excerpt", "route_id"}, "finish_partial": set(),
    "reconfirm_route": {"digest"}, "pause": set(), "resume": set(), "cancel": set(),
    "reuse_output": {"digest"}, "rerun": {"confirm_charges"},
}


def utc():
    return datetime.now(timezone.utc)


class StaleRevision(Problem):
    def __init__(self, run):
        super().__init__("stale_revision", "This research changed. Review its current state and try again.", 409)
        self.run = run


class Pipelines:
    def __init__(self, root, book, projects, jobs, runs, provider=None, *, config_lock=None):
        self.root, self.book, self.projects, self.jobs, self.runs = root, book, projects, jobs, runs
        self.provider = provider
        self.config_lock = config_lock or Lock()
        self.generation = secrets.token_hex(16)
        self.previews = {}
        self.lock = Lock()
        self.wake = Condition()
        self.closing = False
        self.thread = None
        self.native = None
        self.folder = private_directory(child(root, "hub", "runs", "guided"))
        runs.guided = self
        self.key = self.route_key()
        # Guided PaperQA2 completions found by legacy recovery need this controller to register them.
        runs.recover()
        self.reconcile()

    # ---- infrastructure ---------------------------------------------------------------------------

    def route_key(self):
        path = child(self.root, "hub", "config", "route-identity.key")
        if not path.exists():
            atomic(path, secrets.token_bytes(32))
        data = path.read_bytes()
        if len(data) != 32:
            raise Problem("permissions", "The route identity key is damaged. Remove it to regenerate.", 503)
        return data

    def identity(self, value):
        return hmac.new(self.key, (value or "").encode(), "sha256").hexdigest() if value else None

    def start(self):
        if self.thread is None:
            self.thread = Thread(target=self.loop, name="rah-pipelines", daemon=True)
            self.thread.start()

    def close(self):
        self.closing = True
        self.notify()

    def notify(self):
        with self.wake:
            self.wake.notify_all()

    def loop(self):
        while not self.closing:
            with self.wake:
                self.wake.wait(timeout=1)
            if self.closing:
                return
            try:
                self.tick()
            except Exception:  # The next wake retries; state is persisted.
                pass

    def remember(self, payload):
        stamp = monotonic()
        with self.lock:
            self.previews = {key: value for key, value in self.previews.items() if value[0] > stamp}
            token = digest(canonical(payload) + secrets.token_bytes(16))
            self.previews[token] = (stamp + PREVIEW_TTL, payload)
            return token

    def recall(self, token, kind):
        with self.lock:
            item = self.previews.get(token) if isinstance(token, str) else None
            if not item or item[0] <= monotonic() or item[1].get("kind") != kind:
                raise Problem("stale_preview", "The preview expired or changed. Review it again.", 409)
            return item[1]

    def forget(self, token):
        with self.lock:
            self.previews.pop(token, None)

    # ---- routes and fingerprints --------------------------------------------------------------------

    def config(self):
        return settings.load(self.root)

    def gptr_route(self, operation, quality, prompt, mode="research_report"):
        config = self.config()
        route = self.book.route("gptr")
        if not route or route.get("qualification_only"):
            raise Problem("unverified_route", "GPT Researcher has no supported route on this host.", 503)
        install = upstreams.current(self.root, "gptr", self.book)
        caps = self.book.capabilities("gptr")
        stored = settings.checked_projection(self.root, "gptr", config, upgrade=lambda: settings.resolve(config, "gptr", caps))
        key = config.values.get("RAH_OPENROUTER_API_KEY")
        env = gptr_pipeline.guided_values(stored["env"], key, operation, quality)
        models = {name: env[name] for name in ("FAST_LLM", "SMART_LLM", "STRATEGIC_LLM", "EMBEDDING")}
        payload = {"app": "gptr", "mode": mode, "operation": operation, "install": install.name, "lock": route["lock_sha256"],
                   "endpoint": "https://openrouter.ai/api/v1", "routing": "openrouter-service-selected", "models": models,
                   "settings": gptr_pipeline.SURVEY_SETTINGS if operation == "survey" else {},
                   "retriever": env["RETRIEVER"], "search_url": env.get("SEARX_URL"),
                   "credentials": {name: self.identity(env[name]) for name in env if name.endswith("_KEY")},
                   "prompt": digest(prompt.encode())}
        deadline = gptr_pipeline.DEADLINES["survey" if operation == "survey" else mode]
        display = {"app": "GPT Researcher", "mode": mode, "version": route["upstream_version"],
                   "models": {name: value.partition(":")[2] for name, value in models.items()},
                   "search": search_sources.LABELS["gptr"][env["RETRIEVER"]], "deadline_seconds": deadline,
                   "destinations": ["OpenRouter (downstream provider selected by the service unless constrained)",
                                    search_sources.LABELS["gptr"][env["RETRIEVER"]],
                                    "Public web pages fetched from search results", gptr_pipeline.TOKENIZER],
                   "effort": research_profiles.EFFORT_LABEL}
        if operation == "survey":
            display["controls"] = {"query_target": "1 generated query (target, not a ceiling)", "results_per_query": 3,
                                   "per_request_output_tokens": 2048, "report_words": 400}
        return digest(canonical(payload)), display, (install, stored["env"], key)

    def gate_route(self, question):
        key = self.config().values.get("RAH_OPENROUTER_API_KEY")
        if not key:
            return None, {"available": False, "reason": "Add the shared OpenRouter key to use the Scope Gate. "
                          "Editable generic questions will be shown instead."}
        payload = {"endpoint": scope_gate.ENDPOINT, "model": scope_gate.MODEL, "max": scope_gate.MAX_COMPLETION_TOKENS,
                   "credential": self.identity(key), "prompt": digest(question.encode())}
        return digest(canonical(payload)), scope_gate.preview(question) | {"available": True}

    def ldr_route(self, prompt):
        config = self.config()
        route = self.book.route("ldr")
        if not route:
            raise Problem("unverified_route", "Local Deep Research has no supported route on this host.", 503)
        install = upstreams.current(self.root, "ldr", self.book)
        try:
            effective = settings.resolve(config, "ldr", self.book.capabilities("ldr"))
        except Problem:
            effective = {"env": {}, "models": {}}
        env = effective["env"]
        process = self.native.ldr_process() if self.native else None
        observed = {"provider": env.get("LDR_LLM_PROVIDER"), "model": env.get("LDR_LLM_MODEL"),
                    "search": env.get("LDR_SEARCH_TOOL"), "searx": env.get(search_sources.URL_FIELDS["ldr"])}
        payload = {"app": "ldr", "install": install.name, "lock": route["lock_sha256"], "settings": observed,
                   "credentials": {name: self.identity(env[name]) for name in env if name.endswith("_KEY")},
                   "process": (process or {}).get("generation"), "prompt": digest(prompt.encode())}
        display = {"app": "Local Deep Research", "version": route["upstream_version"],
                   "configured": {key: value for key, value in observed.items() if value},
                   "process": "Running" if process else "Not running", "relaunch_needed": bool(process and process.get("stale")),
                   "attestation": "Native UI overrides are unobserved by RAH. Review the model and search settings inside "
                                  "LDR before you press Start.", "mode": "Programmatic SDK not enabled; native workflow"}
        return digest(canonical(payload)), display

    def prices(self):
        try:
            return self.provider.cached_prices() if self.provider else {"models": {}, "refreshed_at": None}
        except Exception:
            return {"models": {}, "refreshed_at": None}

    # ---- database helpers --------------------------------------------------------------------------

    def require_run(self, db, scope, chat, run):
        self.projects.require_chat(db, scope, chat)
        row = db.execute("SELECT * FROM pipeline_run WHERE id=? AND chat_id=? AND project_id=?",
                         (id_value(run), chat, scope)).fetchone()
        if not row:
            raise Problem("run_missing", "Guided research not found in this chat.", 404)
        return dict(row)

    @staticmethod
    def current_step(db, run_id):
        row = db.execute("""SELECT * FROM pipeline_step WHERE run_id=? ORDER BY position DESC, attempt DESC LIMIT 1""",
                         (run_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def step(db, step_id):
        row = db.execute("SELECT * FROM pipeline_step WHERE id=?", (step_id,)).fetchone()
        if not row:
            raise Problem("step_missing", "Stage not found.", 404)
        return dict(row)

    @staticmethod
    def run_row(db, run_id):
        return dict(db.execute("SELECT * FROM pipeline_run WHERE id=?", (run_id,)).fetchone())

    def bump(self, db, run_id, state=None, *, outcome=None, brief=None):
        assignments, values = ["revision=revision+1", "updated_at=?"], [now()]
        if state:
            assignments.append("state=?")
            values.append(state)
        if outcome:
            assignments.append("outcome=?")
            values.append(outcome)
        if brief is not None:
            assignments.append("brief_json=?")
            values.append(json.dumps(brief, ensure_ascii=False))
        db.execute(f"UPDATE pipeline_run SET {','.join(assignments)} WHERE id=?", (*values, run_id))

    def set_step(self, db, step_id, state, **values):
        entry_label = values.pop("entry_label", None)
        values = {key: json.dumps(value, ensure_ascii=False) if key == "detail_json" else value for key, value in values.items()}
        if state in TERMINAL_STEP:
            values.setdefault("finished_at", now())
        columns = ["state=?", "updated_at=?"] + [f"{key}=?" for key in values]
        db.execute(f"UPDATE pipeline_step SET {','.join(columns)} WHERE id=?", (state, now(), *values.values(), step_id))
        row = self.step(db, step_id)
        label = DISPLAY[state]
        if row["step_key"] in ("ldr_discovery", "ldr_comparison") and state == "waiting_native" and entry_label:
            label = entry_label
        reason = values.get("error_detail") if state in ("failed", "paused", "interrupted", "reconfirm_recipients", "waiting_app") else None
        db.execute("UPDATE chat_entry SET state=?, reason=?, operation_id=COALESCE(?, operation_id), updated_at=? WHERE id=?",
                   (label, reason, values.get("operation_id"), now(), row["entry_id"]))
        return row

    def detail(self, db, step_id, **values):
        row = self.step(db, step_id)
        data = json.loads(row["detail_json"]) | values
        db.execute("UPDATE pipeline_step SET detail_json=?, updated_at=? WHERE id=?", (json.dumps(data, ensure_ascii=False), now(), step_id))
        return data

    def new_attempt(self, db, run, position, state, *, attempt=None, question="", quote="", inputs=(), round_number=None):
        key = RECIPES[run["purpose"]][position]
        app, action, _ = STEPS[key]
        if attempt is None:
            prior = db.execute("SELECT max(attempt) FROM pipeline_step WHERE run_id=? AND position=?", (run["id"], position)).fetchone()[0]
            attempt = (prior or 0) + 1
        step_id, entry = str(uuid4()), str(uuid4())
        stamp = now()
        db.execute("""INSERT INTO chat_entry (id,chat_id,app,action,mode,question,quote,intent,state,input_ids,created_at,updated_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (entry, run["chat_id"], app, action, key, question, quote, "guided:" + step_id, DISPLAY[state],
                    json.dumps(list(inputs)), stamp, stamp))
        db.execute("""INSERT INTO pipeline_step (id,run_id,position,step_key,attempt,entry_id,state,queued_at,round,created_at,updated_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (step_id, run["id"], position, key, attempt, entry, state,
                    stamp if state == "queued" else None, round_number, stamp, stamp))
        db.execute("UPDATE chat SET updated_at=? WHERE id=?", (stamp, run["chat_id"]))
        return step_id

    def approve(self, db, run_id, kind, payload, *, step=None, fingerprint=None, display=None, decision="approved", ttl=None):
        approval = str(uuid4())
        stamp = utc()
        db.execute("""INSERT INTO pipeline_approval (id,run_id,step_id,kind,payload_digest,route_fingerprint,display_json,
                      decision,decided_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                   (approval, run_id, step, kind, payload if isinstance(payload, str) else digest(canonical(payload)),
                    fingerprint, json.dumps((display or {}) | {"generation": self.generation}, ensure_ascii=False),
                    decision, stamp.isoformat(), (stamp + ttl).isoformat() if ttl else None))
        return approval

    def paused_requested(self, db, run_id):
        row = db.execute("""SELECT action FROM pipeline_action WHERE run_id=? AND action IN ('pause','resume')
                            ORDER BY rowid DESC LIMIT 1""", (run_id,)).fetchone()
        return bool(row and row["action"] == "pause")

    # ---- recipe progression ----------------------------------------------------------------------

    def advance(self, db, run_id, finished_position):
        """Open the next fixed recipe position, or finish the run. Pause takes effect here, between steps."""
        run = self.run_row(db, run_id)
        if run["state"] in TERMINAL_RUN:
            return
        recipe = RECIPES[run["purpose"]]
        position = finished_position + 1
        if position >= len(recipe):
            self.bump(db, run_id, "succeeded", outcome="complete")
            return
        if self.paused_requested(db, run_id):
            self.bump(db, run_id, "paused")
            return
        key = recipe[position]
        brief = json.loads(run["brief_json"] or "{}")
        prompt = brief.get("public_prompts", {}).get(key, "")
        if key in REPORT_MODES:
            self.new_attempt(db, run, position, "queued", question=prompt)
            self.bump(db, run_id, "queued")
            self.notify()
        elif key in ("ldr_discovery", "ldr_comparison"):
            self.new_attempt(db, run, position, "waiting_native", question=prompt)
            self.bump(db, run_id, "waiting_native")
        elif key in ("papers", "paper_selection"):
            self.new_attempt(db, run, position, "awaiting_papers")
            self.bump(db, run_id, "awaiting_papers")
        elif key == "paper_analysis":
            previous = self.latest_attempt(db, run_id, position - 1)
            corpus = json.loads(previous["detail_json"]).get("corpus") or {}
            step = self.new_attempt(db, run, position, "awaiting_disclosure", question=corpus.get("question", ""),
                                    quote=corpus.get("excerpt", ""), inputs=corpus.get("pdf_ids", []))
            self.detail(db, step, route_id=corpus.get("route_id"))
            available = pqa_profiles.availability(self.root, self.book).get(run["selected_profile"], {})
            if not available.get("available"):
                self.set_step(db, step, "paused", error_code=available.get("code", "profile_unavailable"),
                              error_detail=(available.get("reason") or "Managed PDF analysis is unavailable.")[:500])
                self.bump(db, run_id, "paused")
            else:
                self.bump(db, run_id, "awaiting_disclosure")

    @staticmethod
    def latest_attempt(db, run_id, position):
        row = db.execute("SELECT * FROM pipeline_step WHERE run_id=? AND position=? ORDER BY attempt DESC LIMIT 1",
                         (run_id, position)).fetchone()
        return dict(row) if row else None

    def fail_step(self, db, step_id, code, message, *, state="failed"):
        row = self.set_step(db, step_id, state, error_code=code, error_detail=message[:500])
        run = self.run_row(db, row["run_id"])
        if run["state"] not in TERMINAL_RUN:
            self.bump(db, row["run_id"], "paused" if state == "failed" else state)
        return row

    # ---- consent checks ---------------------------------------------------------------------------

    def consent(self, db, step):
        """Return None if public consent is current for this attempt, else a reason to re-confirm."""
        run = self.run_row(db, step["run_id"])
        key = step["step_key"]
        if key in ("survey", "scope_gate"):
            base = db.execute("SELECT * FROM pipeline_approval WHERE run_id=? AND kind='public_start' AND decision='approved' ORDER BY rowid DESC LIMIT 1",
                              (run["id"],)).fetchone()
            question = run["original_question"]
            current = (self.gptr_route("survey", run["selected_profile"], question)[0] if key == "survey"
                       else self.gate_route(question)[0])
            expected = json.loads(base["route_fingerprint"]).get(key) if base else None
        else:
            brief = json.loads(run["brief_json"] or "{}")
            base = db.execute("SELECT * FROM pipeline_approval WHERE run_id=? AND kind='scope' AND decision='approved' ORDER BY rowid DESC LIMIT 1",
                              (run["id"],)).fetchone()
            prompt = brief.get("public_prompts", {}).get(key, "")
            mode = REPORT_MODES[key]
            current = self.gptr_route(mode[1], run["selected_profile"], prompt, mode[0])[0]
            expected = brief.get("routes", {}).get(key, {}).get("fingerprint")
        override = db.execute("""SELECT * FROM pipeline_approval WHERE step_id=? AND kind='reconfirm' AND decision='approved'
                                 ORDER BY rowid DESC LIMIT 1""", (step["id"],)).fetchone()
        for approval, fingerprint in ((override, override["route_fingerprint"] if override else None), (base, expected)):
            if not approval:
                continue
            display = json.loads(approval["display_json"])
            fresh = datetime.fromisoformat(approval["decided_at"]) + CONSENT_TTL > utc()
            if fingerprint == current and display.get("generation") == self.generation and fresh:
                return None, current
        if not base:
            return "No public approval is recorded for this stage.", current
        if expected != current:
            return "The model, search service, credential or installed app changed since approval.", current
        if json.loads(base["display_json"]).get("generation") != self.generation:
            return "RAH restarted since approval. Review the recipients again before any new charges.", current
        return "The public approval is older than 24 hours. Review the same route again.", current

    # ---- public API: preview and create ----------------------------------------------------------------

    def preview(self, scope, chat, data):
        if set(data) - {"purpose", "profile", "question"}:
            raise Problem("invalid_request", "Unknown preview fields.", 422)
        purpose, profile = data.get("purpose"), research_profiles.quality(data.get("profile"))
        if purpose not in RECIPES:
            raise Problem("purpose", "Choose a research purpose.", 422)
        label = "Public topic for survey" if purpose == "analyse_my_papers" else "Public research question"
        question = scope_gate.public_text(data.get("question"), label=label)
        with self.projects.connect() as db:
            self.projects.require_chat(db, scope, chat)
        with self.config_lock:
            survey_fp, survey, _ = self.gptr_route("survey", profile, question)
            gate_fp, gate = self.gate_route(question)
        prices = self.prices()
        models = sorted(set(survey["models"].values()) | {scope_gate.MODEL})
        payload = {"kind": "public_start", "purpose": purpose, "profile": profile, "question": question,
                   "fingerprints": {"survey": survey_fp, "scope_gate": gate_fp}, "scope": scope, "chat": chat}
        token = self.remember(payload)
        return {"digest": token, "question": question, "purpose": purpose, "purpose_label": PURPOSES[purpose], "profile": profile,
                "survey": survey, "scope_gate": gate, "expires_in": PREVIEW_TTL,
                "pricing": {"models": {model: prices["models"].get(model) for model in models},
                            "refreshed_at": prices.get("refreshed_at"),
                            "note": "Advertised OpenRouter prices per million tokens where cached. RAH sets no spending ceiling."},
                "notice": "Confirm the question is public and safe to send. RAH does not detect private content.",
                "recipe": list(RECIPES[purpose])}

    def record_action(self, db, scope, chat, run, action, action_id, payload):
        action_id = id_value(action_id)
        payload_digest = digest(canonical({"action": action, "data": payload}))
        previous = db.execute("SELECT * FROM pipeline_action WHERE id=?", (action_id,)).fetchone()
        if previous:
            if previous["payload_digest"] != payload_digest or previous["chat_id"] != chat or previous["run_id"] != run:
                raise Problem("action_replay", "This action ID belongs to a different action.", 409)
            return json.loads(previous["result_json"]), True
        db.execute("INSERT INTO pipeline_action (id,project_id,chat_id,run_id,action,payload_digest,created_at) VALUES (?,?,?,?,?,?,?)",
                   (action_id, scope, chat, run, action, payload_digest, now()))
        return None, False

    def create(self, scope, chat, data):
        if set(data) - {"request_id", "purpose", "profile", "question", "confirmation_digest", "public_confirmed"}:
            raise Problem("invalid_request", "Unknown request fields.", 422)
        request_id = id_value(data.get("request_id"))
        if data.get("public_confirmed") is not True:
            raise Problem("public_confirmation", "Confirm that the question is public and safe to send.", 422)
        purpose, profile = data.get("purpose"), research_profiles.quality(data.get("profile"))
        question = data.get("question")
        payload = {"purpose": purpose, "profile": profile, "question": question}
        with self.projects.connect() as db:
            previous = db.execute("SELECT * FROM pipeline_action WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous["payload_digest"] != digest(canonical({"action": "create", "data": payload})) or previous["chat_id"] != chat:
                    raise Problem("action_replay", "This request ID belongs to a different request.", 409)
                run = self.require_run(db, scope, chat, previous["run_id"])
                return self.run_data(db, run) | {"replay": True}
        preview = self.recall(data.get("confirmation_digest"), "public_start")
        if any(preview[key] != value for key, value in payload.items()) or preview["scope"] != scope or preview["chat"] != chat:
            raise Problem("stale_preview", "The question or quality changed. Review the public start again.", 409)
        with self.config_lock:
            survey_fp = self.gptr_route("survey", profile, question)[0]
            gate_fp = self.gate_route(question)[0]
        if {"survey": survey_fp, "scope_gate": gate_fp} != preview["fingerprints"]:
            raise Problem("stale_preview", "The recipients changed. Review the public start again.", 409)
        with self.projects.connect(write=True) as db:
            self.projects.require_chat(db, scope, chat)
            if self.projects.active_run(db, chat):
                raise Problem("chat_busy", "Cancel or finish the active guided research in this chat first.", 409)
            if db.execute("""SELECT 1 FROM chat_entry WHERE chat_id=? AND action='managed'
                             AND state IN ('Preparing','Awaiting approval','Running')""", (chat,)).fetchone():
                raise Problem("chat_busy", "Finish or discard the active request in this chat first.", 409)
            self.record_action(db, scope, chat, None, "create", request_id, payload)
            revision = (db.execute("SELECT max(brief_revision) FROM pipeline_run WHERE chat_id=?", (chat,)).fetchone()[0] or 0) + 1
            run_id, stamp = str(uuid4()), now()
            db.execute("""INSERT INTO pipeline_run (id,project_id,chat_id,brief_revision,purpose,recipe_version,state,original_question,
                          selected_profile,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (run_id, scope, chat, revision, purpose, RECIPE_VERSION, "queued", question, profile, stamp, stamp))
            db.execute("UPDATE pipeline_action SET run_id=?, result_json=? WHERE id=?", (run_id, json.dumps({"run": run_id}), request_id))
            run = self.run_row(db, run_id)
            self.new_attempt(db, run, 0, "queued", question=question)
            self.approve(db, run_id, "public_start", {"question": question, "purpose": purpose, "profile": profile},
                         fingerprint=json.dumps(preview["fingerprints"]), display={"question_digest": digest(question.encode())},
                         ttl=CONSENT_TTL)
            if len(question.strip().splitlines()[0]) and db.execute("SELECT title FROM chat WHERE id=?", (chat,)).fetchone()["title"] == "New chat":
                db.execute("UPDATE chat SET title=? WHERE id=?", (question.strip().splitlines()[0][:160], chat))
            result = self.run_data(db, self.run_row(db, run_id))
        self.forget(data.get("confirmation_digest"))
        self.notify()
        return result | {"replay": False}

    # ---- scheduling -----------------------------------------------------------------------------------

    def tick(self):
        if self.jobs.closing:
            return
        with self.projects.connect() as db:
            rows = [dict(row) for row in db.execute("""SELECT s.* FROM pipeline_step s JOIN pipeline_run r ON r.id=s.run_id
                       WHERE s.state IN ('queued','waiting_app') AND r.state NOT IN ('succeeded','failed','cancelled','paused')
                       ORDER BY s.queued_at, s.id""")]
        claimed = set()
        for row in rows:
            resource = STEPS[row["step_key"]][2]
            if resource in claimed:
                continue
            claimed.add(resource)
            self.dispatch(row, resource)

    def dispatch(self, row, resource):
        app = {"gptr": "gptr", "scope_gate": None}.get(resource)
        reason = None
        if self.jobs.busy(resource) or self.jobs.busy("core") or (app and self.jobs.busy(app)):
            reason = "Waiting for the app's current operation to finish."
        elif app:
            try:
                upstreams.current(self.root, app, self.book)
            except Problem as error:
                reason = str(error) + " Use App details to set it up."
        if reason:
            with self.projects.connect(write=True) as db:
                current = self.step(db, row["id"])
                if current["state"] == "queued":
                    self.set_step(db, row["id"], "waiting_app", error_detail=reason[:500])
                    self.bump(db, row["run_id"], "waiting_app")
            return None
        with self.config_lock, self.projects.connect(write=True) as db:
            current = self.step(db, row["id"])
            if current["state"] not in ("queued", "waiting_app"):
                return None
            try:
                problem, fingerprint = self.consent(db, current)
            except Problem as error:
                self.set_step(db, row["id"], "waiting_app", error_detail=str(error)[:500])
                self.bump(db, row["run_id"], "waiting_app")
                return None
            if problem:
                self.set_step(db, row["id"], "reconfirm_recipients", error_detail=problem, route_fingerprint=fingerprint)
                self.bump(db, row["run_id"], "reconfirm_recipients")
                return None
        task = {"survey": self.survey_task, "scope_gate": self.gate_task}.get(row["step_key"], self.report_task)
        running = "surveying" if row["step_key"] == "survey" else "running"

        def reserve(job):
            with self.projects.connect(write=True) as db:
                changed = db.execute("""UPDATE pipeline_step SET state=?, operation_id=?, started_at=?, updated_at=?
                                        WHERE id=? AND state IN ('queued','waiting_app')""",
                                     (running, job.id, now(), now(), row["id"])).rowcount
                if not changed:
                    raise Problem("step_claimed", "This stage was already claimed.", 409)
                self.set_step(db, row["id"], running, operation_id=job.id)
                self.bump(db, row["run_id"], running)

        def finished(job):
            with self.projects.connect(write=True) as db:
                current = self.step(db, row["id"])
                if current["state"] == running and job.state != "Succeeded":
                    state = "cancelled" if job.state == "Cancelled" else "failed"
                    self.fail_step(db, row["id"], "cancelled" if state == "cancelled" else getattr(job, "code", "failed"),
                                   job.reason or "The stage failed.", state=state)
                    run = self.run_row(db, row["run_id"])
                    if state == "cancelled" and run["state"] not in TERMINAL_RUN:
                        self.bump(db, row["run_id"], "cancelled")
            self.notify()

        try:
            return self.jobs.start(resource, lambda job: task(job, row["id"]), reserve=reserve, finished=finished)
        except Problem:
            return None

    # ---- workers -------------------------------------------------------------------------------------

    def tokenizer_cache(self):
        path = self.root
        for part in ("apps", "gptr", "native", "tiktoken-cache"):
            path = private_directory(child(path, part))
        return path

    def attempt_dir(self, step_id):
        return child(self.folder, id_value(step_id))

    def survey_task(self, job, step_id):
        with self.projects.connect() as db:
            step = self.step(db, step_id)
            run = self.run_row(db, step["run_id"])
        with self.config_lock:
            _, display, (install, stored, key) = self.gptr_route("survey", run["selected_profile"], run["original_question"])
            folder = self.attempt_dir(step_id)
            env = gptr_pipeline.guided_projection(stored, key, "survey", run["selected_profile"], folder, self.tokenizer_cache())
        gptr_pipeline.validate_env(env)
        argv = gptr_pipeline.command(install, "research_report", run["original_question"])
        result = gptr_pipeline.run(folder, argv, env, deadline=gptr_pipeline.DEADLINES["survey"], job=job, step=step_id,
                                   attempt=step["attempt"], operation=job.id)
        job.checkpoint(final=True)
        return self.collect_report(step_id, result, role="survey", key="report", survey=True)

    def report_task(self, job, step_id):
        with self.projects.connect() as db:
            step = self.step(db, step_id)
            run = self.run_row(db, step["run_id"])
        brief = json.loads(run["brief_json"])
        prompt = brief["public_prompts"][step["step_key"]]
        mode, operation = REPORT_MODES[step["step_key"]]
        with self.config_lock:
            _, _, (install, stored, key) = self.gptr_route(operation, run["selected_profile"], prompt, mode)
            folder = self.attempt_dir(step_id)
            env = gptr_pipeline.guided_projection(stored, key, operation, run["selected_profile"], folder, self.tokenizer_cache())
        gptr_pipeline.validate_env(env)
        argv = gptr_pipeline.command(install, mode, prompt)
        result = gptr_pipeline.run(folder, argv, env, deadline=gptr_pipeline.DEADLINES[mode], job=job, step=step_id,
                                   attempt=step["attempt"], operation=job.id)
        job.checkpoint(final=True)
        return self.collect_report(step_id, result, role="research_report", key="report")

    def collect_report(self, step_id, result, *, role, key, survey=False):
        with self.projects.connect() as db:
            step = self.step(db, step_id)
            run = self.run_row(db, step["run_id"])
        count = result["sources_count"]
        empty = count == 0
        title = "No sources retrieved" if empty else (result.get("title") or None)
        staged = self.projects.stage(run["project_id"], run["chat_id"], Path(result["path"]), result["name"], "report")
        if staged["sha256"] != result["sha256"]:
            staged["temp"].unlink(missing_ok=True)
            raise Problem("changed_output", "The native report changed while it was collected.", 503)
        version = (self.book.route("gptr") or {}).get("upstream_version")

        def check(db):
            current = self.step(db, step_id)
            if current["state"] not in ("running", "surveying", "interrupted"):
                raise Problem("stale_attempt", "This attempt was superseded.", 409)

        def finalize(db, ids):
            self.detail(db, step_id, sources_count=count, total_cost_usd=result.get("total_cost_usd"),
                        output_name=result["name"])
            if empty:
                self.fail_step(db, step_id, "no_sources", "No sources retrieved. The original is kept as a failed-stage "
                               "artifact. Check the search source or retry.")
                return
            self.set_step(db, step_id, "succeeded", native_result_id=result["sha256"][:32])
            current = self.run_row(db, run["id"])
            if current["state"] in TERMINAL_RUN:
                return
            if survey:
                self.queue_gate(db, run["id"], 1)
            else:
                self.advance(db, run["id"], step["position"])

        ids = self.projects.publish(run["project_id"], run["chat_id"], [{
            "key": key, "role": role, "stage": staged, "app": "gptr", "version": version, "title": title,
            "origin": "public_native"}], step=step_id, run_id=step_id, entry=step["entry_id"], check=check, finalize=finalize,
            marker=child(private_directory(self.attempt_dir(step_id)), "publish.json"))
        self.notify()
        if empty:
            raise Problem("no_sources", "No sources retrieved. The original is saved as a failed-stage artifact.", 422)
        return {"artifact_ids": ids, "sources_count": count}

    def queue_gate(self, db, run_id, round_number, prior=None):
        run = self.run_row(db, run_id)
        step = self.new_attempt(db, run, 1, "queued", round_number=round_number)
        if prior is not None:
            self.detail(db, step, prior=prior)
        self.bump(db, run_id, "queued")
        self.notify()
        return step

    def survey_artifact(self, db, run_id):
        row = db.execute("""SELECT a.* FROM artifact a JOIN pipeline_step s ON s.id=a.step_id WHERE s.run_id=? AND s.position=0
                            AND s.state='succeeded' AND a.role='survey' ORDER BY s.attempt DESC LIMIT 1""", (run_id,)).fetchone()
        if not row:
            raise Problem("survey_missing", "The run has no completed survey.", 409)
        return dict(row)

    def gate_task(self, job, step_id):
        with self.projects.connect() as db:
            step = self.step(db, step_id)
            run = self.run_row(db, step["run_id"])
            survey = self.survey_artifact(db, run["id"])
            prior = json.loads(step["detail_json"]).get("prior")
        path = self.projects.artifact_path(survey)
        if file_digest(path) != survey["sha256"]:
            raise Problem("changed_survey", "The saved survey changed. Retry the survey.", 409)
        excerpt = scope_gate.survey_excerpt(path.read_text(encoding="utf-8"))
        round_number = step["round"] or 1
        observed, error = None, None
        key = self.config().values.get("RAH_OPENROUTER_API_KEY")
        try:
            if not key:
                raise Problem("scope_gate_unavailable", "The Scope Gate needs the shared OpenRouter key.", 409)
            job.checkpoint(final=True)
            content, observed = scope_gate.call(key, scope_gate.request_body(run["original_question"], excerpt, round_number, prior))
            questionnaire = scope_gate.parse(content, round_number)
        except Problem as problem:
            error = {"code": problem.code, "message": str(problem)}
            questionnaire = scope_gate.fallback(round_number) if round_number == 1 else {"round": 2, "source": "fallback", "questions": []}
        record = questionnaire | {"observed": observed, "error": error, "created_at": now()}
        staged = self.projects.stage(run["project_id"], run["chat_id"], io.BytesIO(canonical(record)),
                                     f"scope_round_{round_number}.json", "questionnaire")

        def check(db):
            if self.step(db, step_id)["state"] != "running":
                raise Problem("stale_attempt", "This attempt was superseded.", 409)

        def finalize(db, ids):
            self.set_step(db, step_id, "awaiting_scope", questionnaire_source=questionnaire["source"])
            self.detail(db, step_id, observed=observed, error=error)
            if self.run_row(db, run["id"])["state"] not in TERMINAL_RUN:
                self.bump(db, run["id"], "awaiting_scope")

        self.projects.publish(run["project_id"], run["chat_id"], [{
            "key": "questionnaire", "role": "scope_questionnaire", "stage": staged, "origin": "local_generated",
            "title": f"Scope questions · round {round_number}", "parents": [survey["id"]]}],
            step=step_id, run_id=step_id, entry=step["entry_id"], check=check, finalize=finalize)
        self.notify()
        return {"round": round_number, "source": questionnaire["source"]}

    def questionnaires(self, db, run_id):
        rows = db.execute("""SELECT a.*, s.round, s.attempt FROM artifact a JOIN pipeline_step s ON s.id=a.step_id
                             WHERE s.run_id=? AND s.position=1 AND a.role='scope_questionnaire'
                             AND s.state IN ('awaiting_scope','succeeded') ORDER BY s.attempt""", (run_id,)).fetchall()
        result = []
        for row in rows:
            data = json.loads(self.projects.artifact_path(dict(row)).read_text(encoding="utf-8"))
            result.append({key: data[key] for key in ("round", "source", "questions")} | {"artifact_id": row["id"],
                          "error": data.get("error"), "observed": data.get("observed")})
        return result

    # ---- run data ---------------------------------------------------------------------------------

    def run_data(self, db, run):
        steps = [dict(row) for row in db.execute("SELECT * FROM pipeline_step WHERE run_id=? ORDER BY position, attempt", (run["id"],))]
        outputs = {}
        for row in db.execute("""SELECT a.id,a.kind,a.role,a.original_name,a.media_type,a.output_key,a.title,a.origin_class,a.step_id,
                                 a.app_id,a.app_version,a.size_bytes,a.created_at FROM artifact a JOIN pipeline_step s ON s.id=a.step_id
                                 WHERE s.run_id=? ORDER BY a.rowid""", (run["id"],)):
            outputs.setdefault(row["step_id"], []).append(dict(row) | {"producer": {"ldr": "Local Deep Research", "gptr": "GPT Researcher",
                                                                                   "pqa": "PaperQA2"}.get(row["app_id"], "RAH")})
        current = steps[-1] if steps else None
        brief = json.loads(run["brief_json"]) if run["brief_json"] else None
        data = {key: run[key] for key in ("id", "chat_id", "brief_revision", "purpose", "recipe_version", "state", "outcome",
                                          "original_question", "selected_profile", "created_at", "updated_at", "revision")}
        data |= {"scope_id": run["project_id"], "purpose_label": PURPOSES[run["purpose"]], "display": DISPLAY.get(run["state"], run["state"]),
                 "recipe": list(RECIPES[run["purpose"]]), "brief": self.public_brief(brief),
                 "steps": [{key: step[key] for key in ("id", "position", "step_key", "attempt", "entry_id", "state", "round",
                                                       "questionnaire_source", "error_code", "error_detail", "native_result_id",
                                                       "operation_id", "started_at", "finished_at")}
                           | {"display": DISPLAY[step["state"]], "detail": self.public_detail(json.loads(step["detail_json"])),
                              "outputs": outputs.get(step["id"], []),
                              "app": STEPS[step["step_key"]][0], "current": step is current} for step in steps]}
        data["actions"] = self.available(db, run, current, brief)
        if current and current["step_key"] == "scope_gate" and current["state"] == "awaiting_scope":
            data["questionnaires"] = self.questionnaires(db, run["id"])
        recipe = RECIPES[run["purpose"]]
        if current and any(key in recipe for key in ("papers", "paper_selection")) and current["position"] >= 2:
            data["papers"] = self.papers(db, run)
        return data

    @staticmethod
    def public_brief(brief):
        if not brief:
            return None
        keep = ("schema", "purpose", "original_question", "questions_shown", "approved_constraints", "scope_mode", "approved_prompt",
                "brief_revision", "approved_at", "pipeline_recipe_version", "profile_snapshot", "public_prompts", "survey_artifact_ids")
        result = {key: brief.get(key) for key in keep if key in brief}
        result["routes"] = {key: value.get("display") for key, value in brief.get("routes", {}).items()}
        return result

    @staticmethod
    def public_detail(detail):
        hidden = {key: value for key, value in detail.items() if key not in ("prior", "recovery")}
        if isinstance(hidden.get("corpus"), dict):
            hidden["corpus"] = {"pdf_ids": hidden["corpus"]["pdf_ids"], "route_id": hidden["corpus"]["route_id"]}
        return hidden

    def available(self, db, run, current, brief):
        if run["state"] in TERMINAL_RUN or not current:
            return []
        actions = ["cancel"]
        state, key = current["state"], current["step_key"]
        if state not in ("running", "surveying") and run["state"] != "paused":
            actions.append("pause")
        if run["state"] == "paused" and state not in ("failed", "interrupted"):
            actions.append("resume")
        if key == "scope_gate" and state == "awaiting_scope":
            actions += ["preview_scope", "approve_scope"]
            if current["round"] == 1 and current["questionnaire_source"]:
                actions.append("ask_more")
            if run["purpose"] == "rapid_research":
                actions.append("use_survey_result")
        if key in ("ldr_discovery", "ldr_comparison") and state in ("waiting_native", "paused"):
            actions += ["launch_native", "confirm_native_result"]
        if key in ("papers", "paper_selection") and state == "awaiting_papers":
            actions += ["set_papers", "start_acquisition", "approve_private_lookup", "select_corpus"]
        if state == "reconfirm_recipients":
            actions.append("reconfirm_route")
        if state in ("failed", "interrupted", "cancelled"):
            actions.append("rerun")
        if state == "interrupted" and json.loads(current["detail_json"]).get("recovery"):
            actions.append("reuse_output")
        if run["purpose"] in PARTIAL and key in ("papers", "paper_analysis") and state not in ("running",):
            actions.append("finish_partial")
        return actions

    def get(self, scope, chat, run):
        with self.projects.connect() as db:
            row = self.require_run(db, scope, chat, run)
            data = self.run_data(db, row)
            current = self.current_step(db, row["id"])
        data |= self.previews_for(row, current)
        return data

    def list_for_chat(self, scope, chat):
        with self.projects.connect() as db:
            return [self.run_data(db, dict(row)) for row in db.execute(
                "SELECT * FROM pipeline_run WHERE chat_id=? AND project_id=? ORDER BY brief_revision", (chat, scope))]

    def previews_for(self, run, current):
        """Fresh server previews (with digests) for actions that need them."""
        result = {}
        if not current or run["state"] in TERMINAL_RUN:
            return result
        try:
            if current["state"] == "reconfirm_recipients":
                result["reconfirm"] = self.reconfirm_preview(run, current)
            if current["step_key"] in ("ldr_discovery", "ldr_comparison") and current["state"] in ("waiting_native", "paused"):
                brief = json.loads(run["brief_json"])
                fingerprint, display = self.ldr_route(brief["public_prompts"][current["step_key"]])
                result["native_route"] = display | {"digest": self.remember({"kind": "native_route", "step": current["id"],
                                                                             "fingerprint": fingerprint}),
                                                    "prompt": brief["public_prompts"][current["step_key"]]}
            if current["step_key"] == "papers" and current["state"] == "awaiting_papers":
                with self.projects.connect() as db:
                    rows = [dict(row) for row in db.execute("""SELECT * FROM pipeline_paper WHERE run_id=? AND origin_class<>'public_native'
                                                              AND access_status<>'oa_acquired' AND state='needed'""", (run["id"],))]
                result["lookup_previews"] = {row["id"]: preview for row in rows if (preview := self.private_lookup_preview(run, row))}
            if current["state"] == "interrupted":
                recovery = json.loads(current["detail_json"]).get("recovery")
                if recovery:
                    result["recovery"] = {"digest": self.remember({"kind": "recovery", "step": current["id"], "data": recovery}),
                                          "outputs": recovery["outputs"]}
        except Problem as error:
            result["route_problem"] = {"code": error.code, "message": str(error)}
        return result

    def reconfirm_preview(self, run, current):
        key = current["step_key"]
        if key == "survey":
            fingerprint, display, _ = self.gptr_route("survey", run["selected_profile"], run["original_question"])
        elif key == "scope_gate":
            fingerprint, display = self.gate_route(run["original_question"])
        else:
            brief = json.loads(run["brief_json"])
            mode, operation = REPORT_MODES[key]
            fingerprint, display, _ = self.gptr_route(operation, run["selected_profile"], brief["public_prompts"][key], mode)
        return {"step": current["id"], "reason": current["error_detail"], "route": display,
                "digest": self.remember({"kind": "reconfirm", "step": current["id"], "fingerprint": fingerprint})}

    # ---- actions ----------------------------------------------------------------------------------

    def act(self, scope, chat, run_id, data):
        if set(data) - {"action", "action_id", "expected_revision", "step_id", "data"}:
            raise Problem("invalid_request", "Unknown action fields.", 422)
        action, body = data.get("action"), data.get("data")
        if action not in ACTIONS or not isinstance(body, dict) or set(body) - ACTIONS[action]:
            raise Problem("invalid_action", "Unknown action or fields for this action.", 422)
        action_id = id_value(data.get("action_id"))
        with self.projects.connect() as db:
            run = self.require_run(db, scope, chat, run_id)
            previous = db.execute("SELECT * FROM pipeline_action WHERE id=?", (action_id,)).fetchone()
            if previous:
                if previous["payload_digest"] != digest(canonical({"action": action, "data": body})) or previous["run_id"] != run["id"]:
                    raise Problem("action_replay", "This action ID belongs to a different action.", 409)
                return json.loads(previous["result_json"]) | {"replay": True}, 200
            if data.get("expected_revision") != run["revision"]:
                raise StaleRevision(self.run_data(db, run))
            current = self.current_step(db, run["id"])
            if data.get("step_id") is not None and (not current or data["step_id"] != current["id"]):
                raise StaleRevision(self.run_data(db, run))
            if action not in self.available(db, run, current, json.loads(run["brief_json"] or "null")) and action not in ("preview_scope",):
                raise Problem("action_state", "This action is not available at the current stage.", 409)
        handler = getattr(self, "do_" + action)
        result, status = handler(scope, chat, run, current, body, action_id)
        return result, status

    def finish_action(self, db, scope, chat, run, action, action_id, body, result=None):
        replay, existed = self.record_action(db, scope, chat, run["id"], action, action_id, body)
        if existed:
            return replay
        self.bump(db, run["id"])
        data = self.run_data(db, self.run_row(db, run["id"])) | (result or {})
        db.execute("UPDATE pipeline_action SET result_json=? WHERE id=?", (json.dumps(data, ensure_ascii=False), action_id))
        return data

    def guard(self, db, run, current):
        """Compare-and-set protection within a write transaction."""
        latest = self.run_row(db, run["id"])
        if latest["revision"] != run["revision"]:
            raise StaleRevision(self.run_data(db, latest))
        step = self.current_step(db, run["id"])
        if current and step["id"] != current["id"]:
            raise StaleRevision(self.run_data(db, latest))
        return latest, step

    def do_pause(self, scope, chat, run, current, body, action_id):
        with self.projects.connect(write=True) as db:
            _, step = self.guard(db, run, current)
            if step["state"] not in ("running", "surveying"):
                self.bump(db, run["id"], "paused")
                if step["state"] in ("queued", "waiting_app"):
                    self.set_step(db, step["id"], "paused", error_detail="Paused by you.")
            return self.finish_action(db, scope, chat, run, "pause", action_id, body), 200

    def do_resume(self, scope, chat, run, current, body, action_id):
        with self.projects.connect(write=True) as db:
            _, step = self.guard(db, run, current)
            key = step["step_key"]
            if step["state"] == "paused" and key in ("survey", "scope_gate", *REPORT_MODES):
                self.set_step(db, step["id"], "queued", queued_at=now())
                self.bump(db, run["id"], "queued")
            elif step["state"] == "paused" and key == "paper_analysis":
                available = pqa_profiles.availability(self.root, self.book).get(run["selected_profile"], {})
                if not available.get("available"):
                    raise Problem("profile_unavailable", available.get("reason") or "Managed PDF analysis is unavailable.", 409)
                self.set_step(db, step["id"], "awaiting_disclosure")
                self.bump(db, run["id"], "awaiting_disclosure")
            elif step["state"] == "paused" and key in ("ldr_discovery", "ldr_comparison"):
                self.set_step(db, step["id"], "waiting_native")
                self.bump(db, run["id"], "waiting_native")
            elif step["state"] in TERMINAL_STEP:
                self.bump(db, run["id"], "running")
                result = self.finish_action(db, scope, chat, run, "resume", action_id, body)
                self.advance(db, run["id"], step["position"])
                data = self.run_data(db, self.run_row(db, run["id"]))
                db.execute("UPDATE pipeline_action SET result_json=? WHERE id=?", (json.dumps(data, ensure_ascii=False), action_id))
                self.notify()
                return data, 200
            else:
                self.bump(db, run["id"], step["state"])
            result = self.finish_action(db, scope, chat, run, "resume", action_id, body)
        self.notify()
        return result, 200

    def do_cancel(self, scope, chat, run, current, body, action_id):
        finishing = False
        with self.projects.connect(write=True) as db:
            _, step = self.guard(db, run, current)
            if step["state"] in ("running", "surveying") and step["operation_id"]:
                try:
                    self.jobs.get(step["operation_id"]).cancel()
                except Problem:
                    finishing = True
                    self.detail(db, step["id"], finishing=True)
            elif step["state"] not in TERMINAL_STEP:
                self.set_step(db, step["id"], "cancelled", error_detail="Cancelled by you.")
            self.bump(db, run["id"], "cancelled")
            result = self.finish_action(db, scope, chat, run, "cancel", action_id, body, {"finishing": finishing})
        if self.native:
            self.native.revoke(run["id"])
        self.discard_review(scope, step)
        return result, 200

    def discard_review(self, scope, step):
        if step and step["step_key"] == "paper_analysis":
            with self.projects.connect() as db:
                entry = db.execute("SELECT preview_id FROM chat_entry WHERE id=?", (step["entry_id"],)).fetchone()
            if entry and entry["preview_id"]:
                with self.runs.lock:
                    preview = self.runs.previews.pop(entry["preview_id"], None)
                if preview:
                    shutil.rmtree(preview.directory, ignore_errors=True)

    def do_ask_more(self, scope, chat, run, current, body, action_id):
        if body.get("round_id") != "r1" or current["round"] != 1:
            raise Problem("scope_round", "Another round is available once, after the first questions.", 409)
        with self.projects.connect() as db:
            rounds = self.questionnaires(db, run["id"])
        prior = scope_gate.answer_batch(rounds, body.get("answers") or {})
        with self.projects.connect(write=True) as db:
            self.guard(db, run, current)
            self.set_step(db, current["id"], "succeeded")
            step = self.queue_gate(db, run["id"], 2, prior)
            result = self.finish_action(db, scope, chat, run, "ask_more", action_id, body, {"queued_step": step})
        return result, 202

    def resolve_scope(self, run, body, rounds):
        broad = body.get("broad", False)
        resolved = scope_gate.resolve_answers([{"source": item["source"], "questions": item["questions"]} for item in rounds],
                                              body.get("answers") or {}, body.get("instructions") or "", broad)
        prompt = scope_gate.render(run["original_question"], resolved)
        return resolved, prompt

    def do_preview_scope(self, scope, chat, run, current, body, action_id):
        if current["step_key"] != "scope_gate" or current["state"] != "awaiting_scope":
            raise Problem("action_state", "Scope can be previewed once the questions are ready.", 409)
        with self.projects.connect() as db:
            rounds = self.questionnaires(db, run["id"])
        resolved, prompt = self.resolve_scope(run, body, rounds)
        routes = {}
        with self.config_lock:
            for key in RECIPES[run["purpose"]][2:]:
                if key in REPORT_MODES:
                    mode, operation = REPORT_MODES[key]
                    fingerprint, display, _ = self.gptr_route(operation, run["selected_profile"], prompt, mode)
                    routes[key] = {"fingerprint": fingerprint, "display": display}
                elif key in ("ldr_discovery", "ldr_comparison"):
                    fingerprint, display = self.ldr_route(prompt)
                    routes[key] = {"fingerprint": fingerprint, "display": display}
        constraints = [{"question": label, "answer": answer} for label, answer in resolved["lines"]]
        if resolved["instructions"]:
            constraints.append({"question": "Additional instructions", "answer": resolved["instructions"]})
        payload = {"kind": "scope", "run": run["id"], "step": current["id"], "prompt": prompt, "resolved": [list(line) for line in resolved["lines"]],
                   "instructions": resolved["instructions"], "broad": resolved["broad"], "answers": body.get("answers") or {},
                   "routes": routes, "rounds": rounds}
        token = self.remember(payload)
        return {"digest": token, "prompt": prompt, "constraints": constraints, "scope_mode": "broad" if resolved["broad"] else "scoped",
                "needs_conflict_review": not resolved["broad"] and bool(constraints), "length": len(prompt),
                "limit": scope_gate.PROMPT_LIMIT, "routes": {key: value["display"] for key, value in routes.items()},
                "private_stage": "PaperQA2 gets a fresh disclosure review later. Its PDFs, question and excerpt never enter this brief."
                if "paper_analysis" in RECIPES[run["purpose"]] else None}, 200

    def do_approve_scope(self, scope, chat, run, current, body, action_id):
        preview = self.recall(body.get("digest"), "scope")
        if preview["step"] != current["id"]:
            raise Problem("stale_preview", "The questions changed. Preview the scope again.", 409)
        if not preview["broad"] and (preview["resolved"] or preview["instructions"]) and body.get("constraints_reviewed") is not True:
            raise Problem("conflict_review", "Confirm “I reviewed these constraints for conflicts” before approving.", 422)
        with self.config_lock:
            for key, route in preview["routes"].items():
                current_fp = (self.ldr_route(preview["prompt"])[0] if key.startswith("ldr")
                              else self.gptr_route(REPORT_MODES[key][1], run["selected_profile"], preview["prompt"], REPORT_MODES[key][0])[0])
                if current_fp != route["fingerprint"]:
                    raise Problem("stale_preview", "The recipients changed. Preview the scope again.", 409)
        with self.projects.connect(write=True) as db:
            latest, step = self.guard(db, run, current)
            if latest["brief_json"]:
                raise Problem("brief_exists", "This revision already has an approved brief. Revise scope to change it.", 409)
            survey = self.survey_artifact(db, run["id"])
            customs = [entry.get("custom") for entry in preview["answers"].values() if isinstance(entry, dict) and entry.get("custom")]
            public_prompts = {key: preview["prompt"] for key in RECIPES[run["purpose"]][2:] if key in PUBLIC_STEPS}
            brief = {"schema": 1, "chat_id": chat, "purpose": run["purpose"], "original_question": run["original_question"],
                     "survey_artifact_ids": [survey["id"]],
                     "questions_shown": [{"round": item["round"], "source": item["source"], "questions": item["questions"]} for item in preview["rounds"]],
                     "user_answers": preview["answers"], "custom_requirements": customs,
                     "approved_constraints": [{"question": label, "answer": answer} for label, answer in preview["resolved"]]
                     + ([{"question": "Additional instructions", "answer": preview["instructions"]}] if preview["instructions"] else []),
                     "scope_mode": "broad" if preview["broad"] else "scoped", "brief_revision": run["brief_revision"],
                     "approved_at": now(), "pipeline_recipe_version": RECIPE_VERSION,
                     "profile_snapshot": research_profiles.snapshot(run["selected_profile"]),
                     "approved_prompt": preview["prompt"], "public_prompts": public_prompts, "routes": preview["routes"]}
            changed = db.execute("UPDATE pipeline_run SET brief_json=? WHERE id=? AND brief_json IS NULL",
                                 (json.dumps(brief, ensure_ascii=False), run["id"])).rowcount
            if not changed:
                raise Problem("brief_exists", "This revision already has an approved brief.", 409)
            self.approve(db, run["id"], "scope", brief, step=step["id"],
                         fingerprint=json.dumps({key: value["fingerprint"] for key, value in preview["routes"].items()}),
                         display={"prompt_digest": digest(preview["prompt"].encode())}, ttl=CONSENT_TTL)
            self.set_step(db, step["id"], "succeeded")
            self.bump(db, run["id"], "brief_approved")
            self.advance(db, run["id"], 1)
            result = self.finish_action(db, scope, chat, run, "approve_scope", action_id, body)
        self.forget(body.get("digest"))
        self.notify()
        return result, 202 if RECIPES[run["purpose"]][2] in REPORT_MODES else 200

    def do_use_survey_result(self, scope, chat, run, current, body, action_id):
        if run["purpose"] != "rapid_research":
            raise Problem("action_state", "Only Rapid research can finish with the survey.", 409)
        with self.projects.connect(write=True) as db:
            latest, step = self.guard(db, run, current)
            self.approve(db, run["id"], "early_finish", {"outcome": "survey_as_result", "at": now()}, step=step["id"])
            self.set_step(db, step["id"], "skipped")
            skipped = self.new_attempt(db, latest, 2, "skipped")
            self.set_step(db, skipped, "skipped")
            self.bump(db, run["id"], "succeeded", outcome="survey_as_result")
            result = self.finish_action(db, scope, chat, run, "use_survey_result", action_id, body)
        return result, 200

    def do_finish_partial(self, scope, chat, run, current, body, action_id):
        outcome = PARTIAL.get(run["purpose"])
        with self.projects.connect(write=True) as db:
            latest, step = self.guard(db, run, current)
            if run["purpose"] == "literature_review" and not db.execute(
                    "SELECT 1 FROM pipeline_action WHERE run_id=? AND action='set_papers'", (run["id"],)).fetchone():
                raise Problem("ris_intake", "Review the RIS citations in Papers Needed before finishing with discovery only.", 409)
            self.approve(db, run["id"], "partial_finish", {"outcome": outcome, "at": now()}, step=step["id"])
            if step["state"] not in TERMINAL_STEP:
                self.set_step(db, step["id"], "skipped")
            for position in range(step["position"] + 1, len(RECIPES[run["purpose"]])):
                skipped = self.new_attempt(db, latest, position, "skipped")
                self.set_step(db, skipped, "skipped")
            self.bump(db, run["id"], "succeeded", outcome=outcome)
            result = self.finish_action(db, scope, chat, run, "finish_partial", action_id, body)
        self.discard_review(scope, step)
        return result, 200

    def do_reconfirm_route(self, scope, chat, run, current, body, action_id):
        preview = self.recall(body.get("digest"), "reconfirm")
        if preview["step"] != current["id"]:
            raise Problem("stale_preview", "Review the current recipients again.", 409)
        with self.config_lock:
            fresh = self.reconfirm_preview(run, current)
        if self.recall(fresh["digest"], "reconfirm")["fingerprint"] != preview["fingerprint"]:
            raise Problem("stale_preview", "The recipients changed again. Review them once more.", 409)
        with self.projects.connect(write=True) as db:
            self.guard(db, run, current)
            self.approve(db, run["id"], "reconfirm", {"step": current["id"], "fingerprint": preview["fingerprint"]},
                         step=current["id"], fingerprint=preview["fingerprint"], ttl=CONSENT_TTL)
            self.set_step(db, current["id"], "queued", queued_at=now(), route_fingerprint=preview["fingerprint"])
            self.bump(db, run["id"], "queued")
            result = self.finish_action(db, scope, chat, run, "reconfirm_route", action_id, body)
        self.forget(body.get("digest"))
        self.notify()
        return result, 202

    def do_rerun(self, scope, chat, run, current, body, action_id):
        if body.get("confirm_charges") is not True:
            raise Problem("rerun_confirmation", "Confirm that an earlier attempt may already have incurred charges.", 422)
        with self.projects.connect(write=True) as db:
            latest, step = self.guard(db, run, current)
            key = step["step_key"]
            if key in ("ldr_discovery", "ldr_comparison"):
                state = "waiting_native"
            elif key in ("survey", "scope_gate", *REPORT_MODES):
                state = "reconfirm_recipients"
            else:
                raise Problem("action_state", "This stage cannot be rerun.", 409)
            entry = db.execute("SELECT question, quote FROM chat_entry WHERE id=?", (step["entry_id"],)).fetchone()
            new = self.new_attempt(db, latest, step["position"], state, question=entry["question"], round_number=step["round"])
            if step["round"] == 2:
                self.detail(db, new, prior=json.loads(step["detail_json"]).get("prior"))
            self.approve(db, run["id"], "rerun", {"step": step["id"], "charges": True}, step=new)
            if state == "reconfirm_recipients":
                self.set_step(db, new, state, error_detail="Review the recipients before this new attempt.")
            self.bump(db, run["id"], state)
            result = self.finish_action(db, scope, chat, run, "rerun", action_id, body, {"new_step": new})
        return result, 200

    def do_reuse_output(self, scope, chat, run, current, body, action_id):
        preview = self.recall(body.get("digest"), "recovery")
        if preview["step"] != current["id"]:
            raise Problem("stale_preview", "Review the recovered output again.", 409)
        recovery = preview["data"]
        path = child(self.attempt_dir(current["id"]), "outputs", recovery["outputs"][0]["name"])
        if not path.is_file() or file_digest(path) != recovery["outputs"][0]["sha256"]:
            raise Problem("recovery_changed", "The recovered output no longer matches this attempt. Rerun the stage instead.", 409)
        with self.projects.connect(write=True) as db:
            self.guard(db, run, current)
            self.set_step(db, current["id"], "running" if current["step_key"] != "survey" else "surveying")
            self.finish_action(db, scope, chat, run, "reuse_output", action_id, body)
        with path.open("rb") as handle:
            from .gptr_pipeline import frontmatter, source_count
            fields, _ = frontmatter(handle.read(128 * 1024))
        result = {"path": path, "name": path.name, "sha256": recovery["outputs"][0]["sha256"], "sources_count": source_count(fields),
                  "title": fields.get("title", "").strip('"') or None, "total_cost_usd": None}
        try:
            if current["step_key"] == "survey":
                self.collect_report(current["id"], result, role="survey", key="report", survey=True)
            else:
                self.collect_report(current["id"], result, role="research_report", key="report")
        except Problem as error:
            if error.code != "no_sources":
                raise
        self.forget(body.get("digest"))
        return self.get(scope, chat, run["id"]), 200

    # ---- native LDR -------------------------------------------------------------------------------

    def do_launch_native(self, scope, chat, run, current, body, action_id):
        if not self.native:
            raise Problem("native_unavailable", "Native handoff is unavailable in this process.", 503)
        preview = self.recall(body.get("route_digest"), "native_route")
        if preview["step"] != current["id"] or body.get("attest") is not True:
            raise Problem("native_affirmation", "Review the displayed LDR route and confirm you will check its settings in LDR.", 422)
        brief = json.loads(run["brief_json"])
        prompt = brief["public_prompts"][current["step_key"]]
        with self.config_lock:
            fingerprint, display = self.ldr_route(prompt)
        if fingerprint != preview["fingerprint"]:
            raise Problem("stale_preview", "The LDR route changed. Review it again.", 409)
        self.native.check_single(current["id"])
        reopen = body.get("reopen") is True
        formats = ["ris", "md"] if current["step_key"] == "ldr_discovery" else ["md", "ris"]
        with self.projects.connect(write=True) as db:
            self.guard(db, run, current)
            self.approve(db, run["id"], "native_launch", {"step": current["id"], "fingerprint": fingerprint, "reopen": reopen},
                         step=current["id"], fingerprint=fingerprint, display={"attested": True})
            self.finish_action(db, scope, chat, run, "launch_native", action_id, body)
        opened = self.native.open_ldr({"scope": scope, "chat": chat, "entry": current["entry_id"], "step": current["id"],
                                       "run": run["id"], "attempt": current["attempt"], "fingerprint": fingerprint,
                                       "formats": formats, "prompt": "" if reopen else prompt})
        with self.projects.connect(write=True) as db:
            self.set_step(db, current["id"], "waiting_native", route_fingerprint=fingerprint,
                          entry_label="Opened" if opened.get("opened") else "Waiting for native step")
            self.detail(db, current["id"], prefill_prepared=opened.get("prefill_prepared"), opened_at=now())
            self.bump(db, run["id"], "waiting_native")
            data = self.run_data(db, self.run_row(db, run["id"]))
        return data | {"native": opened}, 200

    def native_export(self, record, fmt, raw, result_id):
        """Register one authenticated native export for the bound step attempt as a pending output."""
        key = {"md": "report", "ris": "citations"}[fmt]
        if not isinstance(result_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", result_id):
            raise Problem("export_handoff", "The native result ID was not provided.", 403)
        result_id = str(UUID(result_id))
        with self.projects.connect() as db:
            step = self.step(db, record["step"])
            run = self.run_row(db, step["run_id"])
            latest = self.latest_attempt(db, run["id"], step["position"])
            chat = db.execute("SELECT trash_state FROM chat WHERE id=?", (run["chat_id"],)).fetchone()
        pending = step["state"] in ("waiting_native", "paused") and run["state"] not in TERMINAL_RUN
        sibling = step["state"] == "succeeded" and step["native_result_id"] == result_id and run["state"] not in ("cancelled", "failed")
        if (latest["id"] != step["id"] or chat["trash_state"] or not (pending or sibling)
                or (step["native_result_id"] and step["native_result_id"] != result_id)):
            raise Problem("export_handoff", "This export does not belong to the waiting LDR stage.", 409)
        filename = "LDR export." + fmt
        kind = "citations" if fmt == "ris" else "report"
        staged = self.projects.stage(run["project_id"], run["chat_id"], io.BytesIO(raw), filename, kind)
        if fmt == "ris":
            try:
                structured_citations(staged["temp"])
            except Problem:
                staged["temp"].unlink(missing_ok=True)
                raise
        origin = "public_native" if step["state"] == "succeeded" else "private_or_unknown"

        def check(db):
            latest = self.step(db, step["id"])
            if latest["native_result_id"] and latest["native_result_id"] != result_id:
                raise Problem("export_handoff", "This stage is bound to a different native result.", 409)

        def finalize(db, ids):
            db.execute("UPDATE pipeline_step SET native_result_id=COALESCE(native_result_id, ?), updated_at=? WHERE id=?",
                       (result_id, now(), step["id"]))
            received = self.detail(db, step["id"]).get("received", [])
            self.detail(db, step["id"], received=sorted(set(received) | {fmt}), native_verified=True)
            self.bump(db, run["id"])

        version = installer.PINS["ldr"]
        ids = self.projects.publish(run["project_id"], run["chat_id"], [{
            "key": key, "role": "discovery_report" if key == "report" else "citation_export", "stage": staged,
            "app": "ldr", "version": version, "origin": origin, "title": "LDR native export"}],
            step=step["id"], run_id=step["id"], entry=step["entry_id"], check=check, finalize=finalize)
        return ids[0]

    def manual_native(self, scope, chat, step_id, filename, stream):
        """A user-selected LDR export file. It stays labelled as user-imported with an unverified result ID."""
        suffix = Path(filename or "").suffix.lower()
        if suffix not in (".md", ".ris"):
            raise Problem("file_type", "Select an LDR Markdown (.md) or RIS (.ris) export.", 422)
        with self.projects.connect() as db:
            step = self.step(db, id_value(step_id))
            run = self.require_run(db, scope, chat, step["run_id"])
            if self.current_step(db, run["id"])["id"] != step["id"] or step["step_key"] not in ("ldr_discovery", "ldr_comparison") \
                    or step["state"] not in ("waiting_native", "paused"):
                raise Problem("attachment_state", "This stage is not waiting for an LDR export.", 409)
        kind = "citations" if suffix == ".ris" else "report"
        staged = self.projects.stage(scope, chat, stream, "User-imported LDR export" + suffix, kind)
        if kind == "citations":
            try:
                structured_citations(staged["temp"])
            except Problem:
                staged["temp"].unlink(missing_ok=True)
                raise

        def finalize(db, ids):
            received = self.detail(db, step["id"]).get("manual", [])
            self.detail(db, step["id"], manual=sorted(set(received) | {suffix[1:]}))
            self.bump(db, run["id"])

        return self.projects.publish(scope, chat, [{"key": "citations" if kind == "citations" else "report",
                                                     "role": "native_result", "stage": staged, "app": "ldr", "version": installer.PINS["ldr"],
                                                     "title": "User-imported LDR export", "origin": "private_or_unknown"}],
                                     step=step["id"], run_id=step["id"], entry=step["entry_id"], finalize=finalize)[0]

    def do_confirm_native_result(self, scope, chat, run, current, body, action_id):
        if body.get("attribution") is not True:
            raise Problem("native_confirmation", "Confirm this is the LDR result for the approved public question.", 422)
        ids = body.get("artifact_ids")
        with self.projects.connect() as db:
            rows = {row["id"]: dict(row) for row in db.execute("SELECT * FROM artifact WHERE step_id=?", (current["id"],))}
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or set(ids) - set(rows):
            raise Problem("native_confirmation", "Select the exports received for this stage.", 422)
        chosen = [rows[value] for value in ids]
        keys = {row["output_key"] for row in chosen}
        required = "citations" if current["step_key"] == "ldr_discovery" else "report"
        if required not in keys:
            label = "a valid LDR RIS export" if required == "citations" else "the original LDR Markdown report"
            raise Problem("native_required", f"This stage needs {label} before it can continue.", 409)
        manual = [row for row in chosen if row["role"] == "native_result"]
        if len(manual) == 2 and body.get("same_result") is not True:
            raise Problem("native_confirmation", "Confirm that both user-imported files belong to the same LDR result.", 422)
        public = body.get("public_provenance") is True
        with self.projects.connect(write=True) as db:
            latest, step = self.guard(db, run, current)
            for row in chosen:
                db.execute("UPDATE artifact SET origin_class=? WHERE project_id=? AND id=?",
                           ("public_native" if public else "private_or_unknown", row["project_id"], row["id"]))
            self.approve(db, run["id"], "native_confirmation", {"ids": ids, "public": public, "result": step["native_result_id"]},
                         step=step["id"], display={"verified": bool(step["native_result_id"]) and not manual})
            self.set_step(db, step["id"], "succeeded")
            self.detail(db, step["id"], confirmed=ids, verified=bool(step["native_result_id"]) and not manual)
            self.advance(db, run["id"], step["position"])
            result = self.finish_action(db, scope, chat, run, "confirm_native_result", action_id, body)
        if self.native:
            self.native.keep_for_sibling(current["id"])
        self.notify()
        return result, 200

    # ---- Papers Needed ------------------------------------------------------------------------------

    def suggestions(self, db, run):
        """Citation rows from the confirmed RIS (literature) or the explicit references section (deep)."""
        if run["purpose"] == "literature_review":
            row = db.execute("""SELECT a.* FROM artifact a JOIN pipeline_step s ON s.id=a.step_id WHERE s.run_id=? AND s.position=2
                                AND s.state='succeeded' AND a.output_key='citations' ORDER BY s.attempt DESC LIMIT 1""", (run["id"],)).fetchone()
            if not row:
                return None, []
            rows = structured_citations(self.projects.artifact_path(dict(row)))
            return dict(row), [item | oa.from_url(item.get("citation_url")) for item in rows]
        if run["purpose"] == "deep_research":
            row = db.execute("""SELECT a.* FROM artifact a JOIN pipeline_step s ON s.id=a.step_id WHERE s.run_id=? AND s.position=2
                                AND s.state='succeeded' AND a.role='research_report' ORDER BY s.attempt DESC LIMIT 1""", (run["id"],)).fetchone()
            if not row:
                return None, []
            text = self.projects.artifact_path(dict(row)).read_text(encoding="utf-8")
            section = references_section(text)
            if section is None:
                return dict(row), []
            return dict(row), [item | oa.from_url(item.get("citation_url")) for item in citations(section)]
        return None, []

    def papers(self, db, run):
        rows = [dict(row) for row in db.execute("SELECT * FROM pipeline_paper WHERE run_id=? ORDER BY position", (run["id"],))]
        attempts = {}
        for row in db.execute("SELECT * FROM acquisition WHERE project_id=? AND paper_id IN (SELECT id FROM pipeline_paper WHERE run_id=?) ORDER BY attempt",
                              (run["project_id"], run["id"])):
            attempts.setdefault(row["paper_id"], []).append({key: row[key] for key in ("attempt", "identifier", "version", "method",
                                                             "source_url", "metadata_url", "retrieved_at", "licence", "outcome",
                                                             "error_code", "review_status", "artifact_id")})
        try:
            source, suggested = self.suggestions(db, run)
        except Problem:
            source, suggested = None, []
        guided = settings.guided(self.config())
        for row in rows:
            row["acquisitions"] = attempts.get(row["id"], [])
            row["links"] = oa.manual_links(row, guided["openurl"])
        return {"rows": rows, "suggestions": suggested[:250], "source_artifact_id": source["id"] if source else None,
                "settings": {"oa_auto": guided["oa_auto"], "contact": bool(guided["contact"]), "openurl": bool(guided["openurl"])},
                "limits": {"pass": oa.PASS_PAPERS, "files": oa.RUN_FILES, "bytes": oa.RUN_BYTES}}

    def do_set_papers(self, scope, chat, run, current, body, action_id):
        rows = body.get("rows")
        if not isinstance(rows, list) or len(rows) > 250:
            raise Problem("papers_input", "Submit up to 250 citation rows.", 422)
        source_id = body.get("source_artifact_id")
        with self.projects.connect() as db:
            source, suggested = self.suggestions(db, run)
        if source_id is not None and (not source or source_id != source["id"]):
            raise Problem("papers_input", "The citation source is not this run's confirmed result.", 422)
        public_source = bool(source and source["origin_class"] == "public_native" and source_id)
        fields = ("title", "doi", "pmid", "pmcid", "citation_url", "note")
        clean = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) - set(fields) - {"accept", "id", "state", "pdf_artifact_id", "abstract_only", "suggestion"}:
                raise Problem("papers_input", "Invalid citation row.", 422)
            if row.get("accept") is not True:
                continue
            values = {}
            for name in fields:
                value = row.get(name)
                if value is not None and (not isinstance(value, str) or len(value) > 10_000):
                    raise Problem("papers_input", "Citation fields are too large or invalid.", 422)
                values[name] = (value or "").strip() or None
            if not any(values.values()):
                raise Problem("papers_input", "Each accepted row needs citation text or an identifier.", 422)
            if values["citation_url"] and not re.fullmatch(r"https?://\S+", values["citation_url"]):
                raise Problem("papers_input", "Citation URLs must be web addresses.", 422)
            for name, check in (("doi", oa.doi), ("pmid", oa.pmid), ("pmcid", oa.pmcid)):
                if values[name] and not check(values[name]):
                    values[name] = values[name] if name == "doi" else None
            found, problem = oa.identifiers(values)
            origin = "private_or_unknown"
            suggestion = row.get("suggestion")
            if public_source and isinstance(suggestion, int) and 0 <= suggestion < len(suggested) and not problem:
                original = suggested[suggestion]
                if all((values[name] or None) == ((original.get(name) or "").strip() or None) for name in ("doi", "pmid", "pmcid", "citation_url")):
                    origin = "public_native"
            state = row.get("state", "needed")
            if state not in ("needed", "supplied", "skipped"):
                raise Problem("papers_input", "Invalid paper state.", 422)
            clean.append((values, origin, state, row.get("pdf_artifact_id"), row.get("id"), bool(row.get("abstract_only")), problem))
        auto = []
        with self.projects.connect(write=True) as db:
            latest, step = self.guard(db, run, current)
            position = (db.execute("SELECT max(position) FROM pipeline_paper WHERE run_id=?", (run["id"],)).fetchone()[0] or -1) + 1
            for values, origin, state, pdf, existing, abstract, problem in clean:
                if state == "supplied":
                    if not pdf:
                        raise Problem("pdf_association", "Choose the PDF for a supplied paper.", 422)
                    self.projects.select_pdf_rows(db, scope, [pdf])
                else:
                    pdf = None
                if existing:
                    prior = db.execute("SELECT * FROM pipeline_paper WHERE id=? AND run_id=?", (id_value(existing), run["id"])).fetchone()
                    if not prior:
                        raise Problem("papers_input", "A paper row belongs to another run.", 422)
                    changed = any((prior[name] or None) != values[name] for name in ("doi", "pmid", "pmcid", "citation_url"))
                    merged_origin = "private_or_unknown" if (changed or prior["origin_class"] != "public_native") else prior["origin_class"]
                    db.execute("""UPDATE pipeline_paper SET title=?,doi=?,pmid=?,pmcid=?,citation_url=?,note=?,state=?,pdf_artifact_id=?,
                                  abstract_only=?,origin_class=?,access_status=CASE WHEN ? IS NOT NULL THEN 'local_available' ELSE access_status END,
                                  updated_at=? WHERE id=?""",
                               (*(values[name] for name in ("title", "doi", "pmid", "pmcid", "citation_url", "note")), state, pdf,
                                int(abstract), merged_origin, pdf, now(), prior["id"]))
                    paper = prior["id"]
                    origin = merged_origin
                else:
                    paper = str(uuid4())
                    db.execute("""INSERT INTO pipeline_paper (id,run_id,project_id,position,title,doi,pmid,pmcid,citation_url,note,
                                  source_artifact_id,pdf_artifact_id,origin_class,state,access_status,abstract_only,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (paper, run["id"], scope, position, *(values[name] for name in ("title", "doi", "pmid", "pmcid", "citation_url", "note")),
                                source_id, pdf, origin, state, "local_available" if pdf else "unresolved", int(abstract), now(), now()))
                    position += 1
                self.match_existing(db, scope, step["id"], paper, values)
                if origin == "public_native" and state == "needed" and not problem:
                    auto.append(paper)
            result = self.finish_action(db, scope, chat, run, "set_papers", action_id, body)
        guided = settings.guided(self.config())
        if guided["oa_auto"] and auto:
            self.queue_acquisition(scope, chat, run["id"], step["id"], auto[:oa.PASS_PAPERS], private=False)
            result["acquisition"] = "queued"
        return result, 200

    def match_existing(self, db, scope, step_id, paper, values):
        """Explicit prior association by exact canonical identifier with a valid checksum. Suggestion only."""
        found, _ = oa.identifiers(values)
        if not found:
            return
        candidates = []
        for field in ("doi", "pmid"):
            if found.get(field):
                candidates += db.execute(f"SELECT pdf_artifact_id FROM paper WHERE project_id=? AND pdf_artifact_id IS NOT NULL AND lower({field})=?",
                                         (scope, found[field])).fetchall()
        for (artifact,) in {tuple(row) for row in candidates}:
            row = db.execute("SELECT * FROM artifact WHERE project_id=? AND id=? AND kind='pdf'", (scope, artifact)).fetchone()
            if not row:
                continue
            try:
                valid = file_digest(self.projects.artifact_path(dict(row))) == row["sha256"]
            except (OSError, Problem):
                valid = False
            if valid and not db.execute("SELECT 1 FROM acquisition WHERE paper_id=? AND artifact_id=?", (paper, artifact)).fetchone():
                attempt = (db.execute("SELECT max(attempt) FROM acquisition WHERE paper_id=?", (paper,)).fetchone()[0] or 0) + 1
                db.execute("""INSERT INTO acquisition (id,project_id,step_id,paper_id,attempt,identifier,method,retrieved_at,outcome,
                              review_status,artifact_id,sha256) VALUES (?,?,?,?,?,?,?,?,'existing_match','pending',?,?)""",
                           (str(uuid4()), scope, step_id, paper, attempt, found.get("doi") or found.get("pmid"), "existing-association",
                            now(), artifact, row["sha256"]))
                db.execute("UPDATE pipeline_paper SET access_status='local_available' WHERE id=?", (paper,))

    def do_start_acquisition(self, scope, chat, run, current, body, action_id):
        ids = body.get("paper_ids")
        with self.projects.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM pipeline_paper WHERE run_id=? ORDER BY position", (run["id"],))]
        eligible = [row["id"] for row in rows if row["origin_class"] == "public_native" and row["state"] == "needed"
                    and row["access_status"] not in ("oa_acquired",)]
        if ids is None:
            ids = eligible[:oa.PASS_PAPERS]
        if not isinstance(ids, list) or not ids or len(ids) > oa.PASS_PAPERS or len(set(ids)) != len(ids) or set(ids) - set(eligible):
            raise Problem("acquisition_input", "Choose up to 20 public paper rows. Private or unknown identifiers need a separate lookup review.", 422)
        with self.projects.connect(write=True) as db:
            self.guard(db, run, current)
            self.approve(db, run["id"], "oa_public", {"papers": ids}, step=current["id"])
            result = self.finish_action(db, scope, chat, run, "start_acquisition", action_id, body)
        self.queue_acquisition(scope, chat, run["id"], current["id"], ids, private=False)
        return result | {"acquisition": "queued"}, 202

    def private_lookup_preview(self, run, row):
        guided = settings.guided(self.config())
        found, problem = oa.identifiers(row)
        if problem or not found:
            return None
        if found.get("pmcid"):
            stage, sends, destinations = "cloud", [found["pmcid"][0]], [oa.BUCKET]
        elif found.get("doi"):
            if not guided["contact"]:
                return {"stage": "converter", "blocked": "Contact needed for DOI lookup"}
            stage, sends, destinations = "converter", [found["doi"], "tool=" + oa.TOOL, "maintainer contact"], [oa.CONVERTER]
        else:
            return None
        payload = {"kind": "private_lookup", "paper": row["id"], "run": run["id"], "stage": stage, "identifiers": sends}
        return {"stage": stage, "sends": sends, "destinations": destinations, "digest": self.remember(payload),
                "notice": "Only these identifiers are sent. Titles, notes, report text and PDFs are never sent."}

    def do_approve_private_lookup(self, scope, chat, run, current, body, action_id):
        preview = self.recall(body.get("digest"), "private_lookup")
        if preview["paper"] != body.get("paper_id") or preview["stage"] != body.get("stage") or preview["run"] != run["id"]:
            raise Problem("stale_preview", "Review the lookup again.", 409)
        approved = body.get("approve") is True
        with self.projects.connect(write=True) as db:
            self.guard(db, run, current)
            self.approve(db, run["id"], "oa_private", {"paper": preview["paper"], "stage": preview["stage"], "ids": preview["identifiers"]},
                         step=current["id"], decision="approved" if approved else "declined", display={"stage": preview["stage"]})
            result = self.finish_action(db, scope, chat, run, "approve_private_lookup", action_id, body)
        self.forget(body.get("digest"))
        if approved:
            self.queue_acquisition(scope, chat, run["id"], current["id"], [preview["paper"]], private=preview["stage"])
            return result | {"acquisition": "queued"}, 202
        return result, 200

    def queue_acquisition(self, scope, chat, run_id, step_id, paper_ids, *, private):
        def task(job):
            return self.acquire(job, scope, chat, run_id, step_id, paper_ids, private)

        try:
            return self.jobs.start("oa", task)
        except Problem:
            raise Problem("busy", "Another acquisition pass is running. Try again when it finishes.", 409)

    def acquire(self, job, scope, chat, run_id, step_id, paper_ids, private, transport=None):
        guided = settings.guided(self.config())
        install = installer.install_location(self.root, "pqa")
        python = venv_python(install[1] / "venv") if install else None
        transport = transport or getattr(self, "transport", None) or oa.Transport()
        started = monotonic()
        results = []
        directory = self.projects.files_directory(scope, chat)
        for paper in paper_ids:
            job.checkpoint()
            if monotonic() - started > oa.PASS_DEADLINE:
                break
            with self.projects.connect() as db:
                row = dict(db.execute("SELECT * FROM pipeline_paper WHERE id=? AND run_id=?", (paper, run_id)).fetchone())
                run = self.run_row(db, run_id)
                totals = db.execute("""SELECT count(*), COALESCE(sum(a.size_bytes),0) FROM artifact a JOIN pipeline_step s ON s.id=a.step_id
                                       WHERE s.run_id=? AND a.role='source_pdf' AND a.origin_class='public_acquired'""", (run_id,)).fetchone()
            if run["state"] in TERMINAL_RUN:
                break
            if row["origin_class"] != "public_native" and not private:
                continue
            used = {"files": totals[0], "bytes": totals[1]}

            def budget(size):
                if size is None:
                    return used["files"] < oa.RUN_FILES
                return used["files"] + 1 <= oa.RUN_FILES and used["bytes"] + size <= oa.RUN_BYTES

            lookup = dict(row)
            if private == "converter":
                # The private converter stage resolves only the identifier; the Cloud Service step needs its own approval.
                result = self.convert_only(transport, lookup, guided["contact"])
            elif not python:
                result = oa.Result("manual_review", lookup.get("doi") or "", "pmc", error="parser_unavailable")
            else:
                result = oa.acquire(transport, lookup, contact=guided["contact"], temp_dir=directory, python=python,
                                    version=row["confirmed_version"], budget=budget)
            results.append(self.record_acquisition(scope, chat, run, step_id, row, result, private))
        self.notify()
        return {"papers": results}

    def convert_only(self, transport, row, contact):
        found, problem = oa.identifiers(row)
        if problem or not found or not found.get("doi"):
            return oa.Result("rejected", "", "pmc-idconv", error=problem or "needs_doi")
        if not contact:
            return oa.Result("unresolved", found["doi"], "pmc-idconv", error="contact_needed")
        try:
            accession, url = oa.convert_doi(transport, found["doi"], contact, monotonic() + oa.PAPER_DEADLINE, found.get("pmid"))
        except Problem as error:
            return oa.Result("failed_retryable" if error.status >= 503 else "manual_review", found["doi"], "pmc-idconv", error=error.code)
        if not accession:
            return oa.Result("unresolved", found["doi"], "pmc-idconv", error="no_pmc_mapping")
        return oa.Result("unresolved", found["doi"], "pmc-idconv", pmcid=accession, error="pmcid_resolved_review_cloud_lookup",
                         metadata_url=oa.CONVERTER)

    def record_acquisition(self, scope, chat, run, step_id, row, result, private):
        artifact = None
        if result.outcome == "acquired":
            staged = self.projects.stage(scope, chat, Path(result.temp), (row["title"] or result.pmcid or "Open-access PDF")[:120].replace("/", "-") + ".pdf", "pdf")
            Path(result.temp).unlink(missing_ok=True)
            if staged["sha256"] != result.sha256:
                staged["temp"].unlink(missing_ok=True)
                result = oa.Result("failed_retryable", result.identifier, result.method, error="changed_download")
            else:
                artifact = self.projects.publish(scope, chat, [{"key": "pdf:" + row["id"], "role": "source_pdf", "stage": staged,
                                                                 "origin": "public_acquired" if not private else "private_or_unknown",
                                                                 "title": f"{result.pmcid}.{result.version} · {result.licence}"}],
                                                 step=step_id, run_id=step_id)[0]
        with self.projects.connect(write=True) as db:
            attempt = (db.execute("SELECT max(attempt) FROM acquisition WHERE paper_id=?", (row["id"],)).fetchone()[0] or 0) + 1
            db.execute("""INSERT INTO acquisition (id,project_id,step_id,paper_id,attempt,identifier,version,method,source_url,metadata_url,
                          retrieved_at,access_evidence,licence,outcome,error_code,artifact_id,sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (str(uuid4()), scope, step_id, row["id"], min(attempt, 100), result.identifier or "-", result.version, result.method,
                        result.source_url, result.metadata_url, now(), json.dumps(result.evidence or {}) if result.evidence else None,
                        result.licence, result.outcome, result.error, artifact, result.sha256))
            status = {"acquired": "oa_acquired", "failed_retryable": "failed_retryable"}.get(result.outcome)
            updates = {"access_status": status} if status else {}
            if result.pmcid and not row["pmcid"] and private != "converter":
                updates["pmcid"] = result.pmcid
            if private == "converter" and result.pmcid:
                updates["pmcid"] = result.pmcid
            if updates:
                db.execute(f"UPDATE pipeline_paper SET {','.join(f'{key}=?' for key in updates)}, updated_at=? WHERE id=?",
                           (*updates.values(), now(), row["id"]))
            self.bump(db, run["id"])
        return {"paper": row["id"], "outcome": result.outcome, "error": result.error, "artifact_id": artifact,
                "versions": result.versions}

    def do_select_corpus(self, scope, chat, run, current, body, action_id):
        ids = body.get("pdf_ids")
        question = body.get("analysis_question")
        excerpt = body.get("excerpt") or ""
        if not isinstance(question, str) or not question.strip() or len(question) > 8000 or "\0" in question:
            raise Problem("analysis_question", "Enter a PaperQA2 analysis question up to 8,000 characters.", 422)
        if not isinstance(excerpt, str) or len(excerpt) > 8000 or "\0" in excerpt:
            raise Problem("analysis_excerpt", "The optional excerpt can have at most 8,000 characters.", 422)
        route = research_profiles.PQA_ROUTES[run["selected_profile"]]
        if body.get("route_id") != route:
            raise Problem("route_selection", "Confirm the PaperQA2 route for this research quality.", 422)
        rows = self.projects.selected_pdfs(scope, ids)
        for row in rows:
            if file_digest(self.projects.artifact_path(row)) != row["sha256"]:
                raise Problem("changed_pdf", f"{row['original_name']} changed. Attach the current version.", 409)
        corpus = {"pdf_ids": [row["id"] for row in rows], "question": question, "excerpt": excerpt, "route_id": route}
        with self.projects.connect(write=True) as db:
            latest, step = self.guard(db, run, current)
            for paper in db.execute("SELECT * FROM pipeline_paper WHERE run_id=? AND state='needed'", (run["id"],)).fetchall():
                acquired = db.execute("""SELECT artifact_id FROM acquisition WHERE paper_id=? AND artifact_id IS NOT NULL
                                         ORDER BY attempt DESC LIMIT 1""", (paper["id"],)).fetchone()
                if acquired and acquired["artifact_id"] in corpus["pdf_ids"]:
                    db.execute("UPDATE pipeline_paper SET state='supplied', pdf_artifact_id=?, updated_at=? WHERE id=?",
                               (acquired["artifact_id"], now(), paper["id"]))
                    db.execute("UPDATE acquisition SET review_status='accepted' WHERE paper_id=? AND artifact_id=?", (paper["id"], acquired["artifact_id"]))
            self.detail(db, step["id"], corpus=corpus, corpus_size=len(rows))
            self.set_step(db, step["id"], "succeeded")
            result = self.finish_action(db, scope, chat, run, "select_corpus", action_id, body)
            self.advance(db, run["id"], step["position"])
            result = self.run_data(db, self.run_row(db, run["id"]))
            db.execute("UPDATE pipeline_action SET result_json=? WHERE id=?", (json.dumps(result, ensure_ascii=False), action_id))
        return result, 200

    # ---- PaperQA2 hooks (called by Runs) ----------------------------------------------------------------

    def guided_review(self, scope, entry_id):
        """Return (step, run, quality) for a guided PaperQA2 entry awaiting disclosure, else None."""
        with self.projects.connect() as db:
            row = db.execute("SELECT * FROM pipeline_step WHERE entry_id=?", (entry_id,)).fetchone()
            if not row:
                return None
            step = dict(row)
            run = self.run_row(db, step["run_id"])
            if run["project_id"] != scope:
                raise Problem("entry_missing", "Request not found in this project.", 404)
            current = self.current_step(db, run["id"])
        if step["step_key"] != "paper_analysis" or current["id"] != step["id"] or step["state"] != "awaiting_disclosure" \
                or run["state"] in TERMINAL_RUN:
            raise Problem("request_state", "This PaperQA2 stage is not waiting for a disclosure review.", 409)
        return step, run

    def review_opened(self, step_id, preview_id):
        with self.projects.connect(write=True) as db:
            step = self.step(db, step_id)
            if step["state"] != "awaiting_disclosure":
                raise Problem("request_state", "This stage is not waiting for a review.", 409)
            self.detail(db, step_id, review_opened_at=now())

    def review_decided(self, step_id, preview, approve):
        with self.projects.connect(write=True) as db:
            step = self.step(db, step_id)
            run = self.run_row(db, step["run_id"])
            if step["state"] != "awaiting_disclosure" or run["state"] in TERMINAL_RUN:
                raise Problem("request_state", "This stage is not waiting for a review.", 409)
            self.approve(db, run["id"], "pqa_review", preview.digest, step=step_id, decision="approved" if approve else "declined")
            if not approve:
                self.detail(db, step_id, declined_at=now())
                db.execute("UPDATE chat_entry SET preview_id=NULL WHERE id=?", (step["entry_id"],))
                self.bump(db, run["id"])
                shutil.rmtree(preview.directory, ignore_errors=True)
                return None

        def reserve(job):
            with self.projects.connect(write=True) as db:
                if not db.execute("UPDATE pipeline_step SET state='running', operation_id=?, started_at=? WHERE id=? AND state='awaiting_disclosure'",
                                  (job.id, now(), step_id)).rowcount:
                    raise Problem("step_claimed", "This stage already started.", 409)
                self.set_step(db, step_id, "running", operation_id=job.id)
                self.bump(db, run["id"], "running")

        def finished(job):
            self.runs.clean_run(preview.directory)
            with self.projects.connect(write=True) as db:
                current = self.step(db, step_id)
                if current["state"] == "running" and job.state != "Succeeded":
                    self.fail_step(db, step_id, "cancelled" if job.state == "Cancelled" else "worker_failed",
                                   job.reason or "PaperQA2 did not finish.", state="cancelled" if job.state == "Cancelled" else "failed")
                    if job.state != "Cancelled":
                        # A failed private run can be reviewed again without discarding the selection.
                        self.set_step(db, step_id, "awaiting_disclosure", error_code="worker_failed",
                                      error_detail=(job.reason or "PaperQA2 did not finish.")[:500])
                        self.bump(db, run["id"], "awaiting_disclosure")
            self.notify()

        try:
            return self.jobs.start("pqa", lambda job: self.runs.run_paper(preview, job), reserve=reserve, finished=finished)
        except Problem as error:
            shutil.rmtree(preview.directory, ignore_errors=True)
            if error.code == "busy":
                raise Problem("busy", "PaperQA2 is busy. Review the PDF transfer again when it is free; no consent was stored.", 409)
            raise

    def paper_result(self, record, result, run_folder):
        step_id = record["guided"]
        with self.projects.connect() as db:
            step = self.step(db, step_id)
            run = self.run_row(db, step["run_id"])
        answer = self.projects.stage(run["project_id"], run["chat_id"], io.BytesIO(result["answer"].encode()), "PaperQA2 answer.md", "answer")
        outputs = [{"key": "answer", "role": "answer", "stage": answer, "app": "pqa", "version": record["version"],
                    "origin": "local_generated", "title": "PaperQA2 answer", "parents": [item["id"] for item in record["files"]]}]
        if result.get("bib"):
            bib = self.projects.stage(run["project_id"], run["chat_id"], io.BytesIO(result["bib"].encode()), "PaperQA2 references.bib", "citations")
            outputs.append({"key": "references", "role": "references", "stage": bib, "app": "pqa", "version": record["version"],
                            "origin": "local_generated", "title": "PaperQA2 native references", "parents": [item["id"] for item in record["files"]]})
        names = {item["storage"]: item["name"] for item in record["files"]}
        coverage = result.get("coverage") or {}

        def label(values):
            return None if values is None else [names.get(value, value) for value in values]

        def finalize(db, ids):
            current = self.step(db, step_id)
            if current["state"] not in ("running", "interrupted"):
                return
            papers = [dict(row) for row in db.execute("SELECT state, abstract_only FROM pipeline_paper WHERE run_id=?", (run["id"],))]
            self.detail(db, step_id, coverage={"submitted": label(coverage.get("submitted")), "cited": label(coverage.get("cited")),
                                               "reviewed": label(coverage.get("reviewed")),
                                               "abstract_only": sum(1 for row in papers if row["abstract_only"]),
                                               "not_obtained": sum(1 for row in papers if row["state"] != "supplied")})
            self.set_step(db, step_id, "succeeded")
            self.advance(db, run["id"], step["position"])

        ids = self.projects.publish(run["project_id"], run["chat_id"], outputs, step=step_id, run_id=step_id, entry=step["entry_id"], finalize=finalize)
        self.notify()
        return {"artifact_id": ids[0], "submitted_files": record["files"]}

    # ---- restart reconciliation ------------------------------------------------------------------------

    def reconcile(self):
        with self.projects.connect() as db:
            steps = [dict(row) for row in db.execute("""SELECT s.* FROM pipeline_step s JOIN pipeline_run r ON r.id=s.run_id
                       WHERE s.state IN ('running','surveying','waiting_native','awaiting_disclosure')
                       AND r.state NOT IN ('succeeded','failed','cancelled')""")]
        for step in steps:
            folder = self.attempt_dir(step["id"])
            marker = child(folder, "publish.json")
            with self.projects.connect() as db:
                run = self.run_row(db, step["run_id"])
            if marker.exists():
                try:
                    self.projects.recover_publication(run["project_id"], run["chat_id"], marker)
                except (Problem, OSError):
                    pass
            with self.projects.connect(write=True) as db:
                current = self.step(db, step["id"])
                if current["state"] in ("running", "surveying"):
                    completion = read_json(child(folder, "completion.json"), None) if folder.exists() else None
                    recovery = None
                    if completion and completion.get("status") == "succeeded" and completion.get("outputs"):
                        output = completion["outputs"][0]
                        path = child(folder, "outputs", output["name"])
                        if path.is_file() and file_digest(path) == output["sha256"]:
                            recovery = {"outputs": [output]}
                    self.set_step(db, step["id"], "interrupted", error_code="interrupted",
                                  error_detail="RAH stopped while this stage was running. Inspect it, reuse its verified output, or rerun it.")
                    if recovery:
                        self.detail(db, step["id"], recovery=recovery)
                    self.bump(db, step["run_id"], "interrupted")
                elif current["state"] == "waiting_native":
                    self.set_step(db, step["id"], "paused", error_code="grant_expired",
                                  error_detail="RAH restarted. Use Reopen LDR handoff to save the result from LDR history; research is not resubmitted.")
                    self.bump(db, step["run_id"], "paused")
                elif current["state"] == "awaiting_disclosure":
                    db.execute("UPDATE chat_entry SET preview_id=NULL WHERE id=?", (current["entry_id"],))


def references_section(text):
    """Return the explicit references section of a Markdown report, or None."""
    match = re.search(r"^(#{1,6})\s*(References|Sources|Bibliography|Works Cited)\s*$", text, re.M | re.I)
    if not match:
        return None
    level = len(match.group(1))
    rest = text[match.end():]
    end = re.search(r"^#{1,%d}\s+\S" % level, rest, re.M)
    return rest[:end.start()] if end else rest
