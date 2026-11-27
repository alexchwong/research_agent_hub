"""Deterministic guided recipe controller.

RAH stores the question, approvals, step state, inputs and original outputs. The upstream apps do the
research. The recipe table below is fixed in code: no model decides which step or tool runs next.
"""
from datetime import datetime, timezone
import hmac
import io
import json
from pathlib import Path
import re
import secrets
import shutil
from threading import Condition, Lock, Thread
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from . import gptr_pipeline, installer, ldr_pipeline, oa, pqa_profiles, research_profiles, scope_gate, search_sources, settings, upstreams
from . import usage
from .common import Problem, atomic, canonical, child, digest, file_digest, id_value, private_directory, read_json, venv_python
from .projects import citations, now, structured_citations, usable_citation


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
    "gptr_comparison": ("gptr", "managed", "gptr"), "ldr_discovery": ("ldr", "managed", "ldr"),
    "ldr_comparison": ("ldr", "managed", "ldr"), "papers": ("rah", "checkpoint", None),
    "paper_selection": ("rah", "checkpoint", None), "paper_analysis": ("pqa", "managed", "pqa"),
}
REPORT_MODES = {"gptr_deep": ("deep", "deep"), "gptr_rapid": ("research_report", "rapid"),
                "gptr_comparison": ("research_report", "comparison")}
PUBLIC_STEPS = {"gptr_deep", "gptr_rapid", "gptr_comparison", "ldr_discovery", "ldr_comparison"}
LDR_STEPS = ("ldr_discovery", "ldr_comparison")
MANAGED_STEPS = ("survey", "scope_gate", *REPORT_MODES, *LDR_STEPS)
DEADLINES = {"survey": gptr_pipeline.DEADLINES["survey"], "scope_gate": scope_gate.DEADLINE,
             "gptr_rapid": gptr_pipeline.DEADLINES["research_report"], "gptr_comparison": gptr_pipeline.DEADLINES["research_report"],
             "gptr_deep": gptr_pipeline.DEADLINES["deep"], "ldr_discovery": ldr_pipeline.DEADLINE,
             "ldr_comparison": ldr_pipeline.DEADLINE, "paper_analysis": 600}
HEARTBEAT = 5
COMPONENT_LABELS = {"install": "installed app", "lock": "installed app", "models": "model", "model": "model",
                    "settings": "LDR model or search settings", "retriever": "search service", "search_url": "search service",
                    "credentials": "saved key", "credential": "saved key", "prompt": "approved prompt", "runner": "RAH runner",
                    "endpoint": "endpoint", "routing": "endpoint", "mode": "report mode", "operation": "report mode",
                    "max": "request limit", "app": "app"}
PARTIAL = {"literature_review": "discovery_only", "deep_research": "report_only"}
TERMINAL_STEP = {"succeeded", "failed", "cancelled", "skipped"}
TERMINAL_RUN = {"succeeded", "failed", "cancelled"}
DISPLAY = {"queued": "Queued", "surveying": "Surveying", "running": "Running", "awaiting_scope": "Awaiting scope",
           "brief_approved": "Brief approved", "waiting_app": "Waiting for app", "waiting_native": "Waiting for native step",
           "awaiting_papers": "Awaiting papers", "awaiting_disclosure": "Awaiting disclosure approval",
           "reconfirm_recipients": "Awaiting recipient re-confirmation", "paused": "Paused", "interrupted": "Interrupted",
           "succeeded": "Complete", "failed": "Failed", "cancelled": "Cancelled", "skipped": "Skipped"}
PREVIEW_TTL = 600
ACTIONS = {
    "ask_more": {"round_id", "answers"}, "preview_scope": {"answers", "instructions", "broad"},
    "approve_scope": {"digest"}, "use_survey_result": set(),
    "set_papers": {"source_artifact_id", "rows"}, "start_acquisition": {"paper_ids"},
    "approve_private_lookups": {"paper_ids", "digest"},
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
        self.previews = {}
        self.lock = Lock()
        self.wake = Condition()
        self.closing = False
        self.thread = None
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
        return digest(canonical(payload)), display, (install, stored["env"], key), payload

    def gate_route(self, question):
        key = self.config().values.get("RAH_OPENROUTER_API_KEY")
        if not key:
            return None, {"available": False, "reason": "Add the shared OpenRouter key to use the Scope Gate. "
                          "Editable generic questions will be shown instead."}, {}
        payload = {"endpoint": scope_gate.ENDPOINT, "model": scope_gate.MODEL, "max": scope_gate.MAX_COMPLETION_TOKENS,
                   "credential": self.identity(key), "prompt": digest(question.encode())}
        return digest(canonical(payload)), scope_gate.preview(question) | {"available": True}, payload

    def ldr_route(self, prompt):
        """Headless LDR route: (fingerprint, display, (install, env), components). Raises ldr_headless_unavailable."""
        return ldr_pipeline.route(self.root, self.book, self.config(), prompt, self.identity)

    def step_route(self, run, key, prompt):
        """(fingerprint, display, components) for one downstream public step."""
        if key in LDR_STEPS:
            fingerprint, display, _, components = self.ldr_route(prompt)
        else:
            mode, operation = REPORT_MODES[key]
            fingerprint, display, _, components = self.gptr_route(operation, run["selected_profile"], prompt, mode)
        return fingerprint, display, components

    def public_start(self, profile="standard"):
        """Question-independent display of the public start recipients, for the one-click composer."""
        try:
            with self.config_lock:
                _, survey, _, _ = self.gptr_route("survey", profile, "")
                _, gate, _ = self.gate_route("")
        except Problem as error:
            return {"available": False, "code": error.code, "reason": str(error)}
        return self.start_display(survey, gate) | {"available": True}

    @staticmethod
    def start_display(survey, gate):
        gate = {key: value for key, value in gate.items() if key != "question_digest"}
        return {"survey": survey, "scope_gate": gate, "display_digest": digest(canonical({"survey": survey, "scope_gate": gate}))}

    @staticmethod
    def changed(old, new):
        """Name the material route dimensions that differ, without exposing values."""
        if not isinstance(old, dict) or not isinstance(new, dict):
            return None
        labels = []
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                label = COMPONENT_LABELS.get(key, key.replace("_", " "))
                if label not in labels:
                    labels.append(label)
        return labels

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
        values = {key: json.dumps(value, ensure_ascii=False) if key == "detail_json" else value for key, value in values.items()}
        if state in TERMINAL_STEP:
            values.setdefault("finished_at", now())
        columns = ["state=?", "updated_at=?"] + [f"{key}=?" for key in values]
        db.execute(f"UPDATE pipeline_step SET {','.join(columns)} WHERE id=?", (state, now(), *values.values(), step_id))
        row = self.step(db, step_id)
        label = DISPLAY[state]
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

    def approve(self, db, run_id, kind, payload, *, step=None, fingerprint=None, display=None, decision="approved"):
        approval = str(uuid4())
        db.execute("""INSERT INTO pipeline_approval (id,run_id,step_id,kind,payload_digest,route_fingerprint,display_json,
                      decision,decided_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
                   (approval, run_id, step, kind, payload if isinstance(payload, str) else digest(canonical(payload)),
                    fingerprint, json.dumps(display or {}, ensure_ascii=False), decision, utc().isoformat()))
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
        elif key in LDR_STEPS:
            self.new_attempt(db, run, position, "queued", question=prompt)
            self.bump(db, run_id, "queued")
            self.notify()
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
        """Return (None, fingerprint) if public consent is current for this attempt, else (reason, fingerprint).

        There is no expiry by elapsed time or hub restart: consent stays current while the full route fingerprint is
        unchanged. A material route or payload change requires re-confirmation, naming what changed.
        """
        run = self.run_row(db, step["run_id"])
        key = step["step_key"]
        if key in ("survey", "scope_gate"):
            base = db.execute("SELECT * FROM pipeline_approval WHERE run_id=? AND kind='public_start' AND decision='approved' ORDER BY rowid DESC LIMIT 1",
                              (run["id"],)).fetchone()
            question = run["original_question"]
            if key == "survey":
                current, _, _, components = self.gptr_route("survey", run["selected_profile"], question)
            else:
                current, _, components = self.gate_route(question)
            expected = json.loads(base["route_fingerprint"]).get(key) if base else None
            approved = json.loads(base["display_json"]).get("components", {}).get(key) if base else None
        else:
            brief = json.loads(run["brief_json"] or "{}")
            base = db.execute("SELECT * FROM pipeline_approval WHERE run_id=? AND kind='scope' AND decision='approved' ORDER BY rowid DESC LIMIT 1",
                              (run["id"],)).fetchone()
            prompt = brief.get("public_prompts", {}).get(key, "")
            current, _, components = self.step_route(run, key, prompt)
            route = brief.get("routes", {}).get(key, {})
            expected, approved = route.get("fingerprint"), route.get("components")
        override = db.execute("""SELECT * FROM pipeline_approval WHERE step_id=? AND kind='reconfirm' AND decision='approved'
                                 ORDER BY rowid DESC LIMIT 1""", (step["id"],)).fetchone()
        if override and override["route_fingerprint"] == current:
            return None, current
        if base and expected == current:
            return None, current
        if not base:
            return "No public approval is recorded for this stage.", current
        if override:
            approved = json.loads(override["display_json"]).get("components") or approved
        labels = self.changed(approved, components)
        if labels:
            what = ", ".join(labels)
            return f"The {what} changed since approval. Review the recipients again.", current
        return "The approved route changed since approval. Review the recipients again.", current

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
            survey_fp, survey, _, survey_parts = self.gptr_route("survey", profile, question)
            gate_fp, gate, gate_parts = self.gate_route(question)
        prices = self.prices()
        models = sorted(set(survey["models"].values()) | {scope_gate.MODEL})
        payload = {"kind": "public_start", "purpose": purpose, "profile": profile, "question": question,
                   "fingerprints": {"survey": survey_fp, "scope_gate": gate_fp}, "scope": scope, "chat": chat,
                   "components": {"survey": survey_parts, "scope_gate": gate_parts}}
        token = self.remember(payload)
        return {"digest": token, "question": question, "purpose": purpose, "purpose_label": PURPOSES[purpose], "profile": profile,
                "survey": survey, "scope_gate": gate, "expires_in": PREVIEW_TTL,
                "display_digest": self.start_display(survey, gate)["display_digest"],
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
                         fingerprint=json.dumps(preview["fingerprints"]),
                         display={"question_digest": digest(question.encode()), "components": preview["components"]})
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
        app = {"gptr": "gptr", "ldr": "ldr", "scope_gate": None}.get(resource)
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
        task = {"survey": self.survey_task, "scope_gate": self.gate_task, "ldr_discovery": self.ldr_task,
                "ldr_comparison": self.ldr_task}.get(row["step_key"], self.report_task)
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

    def heartbeat(self, step_id, progress=None):
        """A tick callback for a running child: liveness every HEARTBEAT seconds, plus copied LDR progress.

        Both are written with detail() and never bump the run revision, so Pause and Cancel keep working.
        """
        state = {"beat": 0.0, "seq": None}

        def tick():
            stamp = monotonic()
            values = {}
            if stamp - state["beat"] >= HEARTBEAT:
                state["beat"] = stamp
                values["heartbeat"] = utc().isoformat()
            if progress is not None:
                try:
                    data = json.loads(progress.read_bytes())
                except (OSError, ValueError):
                    data = None
                if isinstance(data, dict) and data.get("seq") != state["seq"]:
                    state["seq"] = data.get("seq")
                    percent = data.get("percent")
                    values["progress"] = {
                        "percent": percent if isinstance(percent, int) and not isinstance(percent, bool) and 0 <= percent <= 100 else None,
                        "message": data["message"][:200] if isinstance(data.get("message"), str) else None,
                        "phase": data["phase"][:40] if isinstance(data.get("phase"), str) else None,
                        "seq": data.get("seq") if isinstance(data.get("seq"), int) else None,
                        "received_at": utc().isoformat()}
            if values:
                with self.projects.connect(write=True) as db:
                    if self.step(db, step_id)["state"] in ("running", "surveying"):
                        self.detail(db, step_id, **values)
        return tick

    def record_step_usage(self, step_id, path, key, *, expected=True, interrupted=False):
        with self.projects.connect() as db:
            entry = self.step(db, step_id)["entry_id"]
        self.projects.record_usage(entry, usage.summarize(path, route_key=key, openrouter_expected=expected,
                                                          interrupted=interrupted))

    def gptr_key(self, mode):
        return usage.route_key("gptr", mode, (self.book.route("gptr") or {}).get("lock_sha256"))

    def survey_task(self, job, step_id):
        with self.projects.connect() as db:
            step = self.step(db, step_id)
            run = self.run_row(db, step["run_id"])
        with self.config_lock:
            _, display, (install, stored, key), _ = self.gptr_route("survey", run["selected_profile"], run["original_question"])
            folder = self.attempt_dir(step_id)
            env = gptr_pipeline.guided_projection(stored, key, "survey", run["selected_profile"], folder, self.tokenizer_cache())
        gptr_pipeline.validate_env(env)
        argv = gptr_pipeline.command(install, "research_report", run["original_question"], folder)
        try:
            result = gptr_pipeline.run(folder, argv, env, deadline=gptr_pipeline.DEADLINES["survey"], job=job, step=step_id,
                                       attempt=step["attempt"], operation=job.id, tick=self.heartbeat(step_id))
        finally:
            self.record_step_usage(step_id, child(folder, "usage.jsonl"), self.gptr_key("research_report"))
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
            _, _, (install, stored, key), _ = self.gptr_route(operation, run["selected_profile"], prompt, mode)
            folder = self.attempt_dir(step_id)
            env = gptr_pipeline.guided_projection(stored, key, operation, run["selected_profile"], folder, self.tokenizer_cache())
        gptr_pipeline.validate_env(env)
        argv = gptr_pipeline.command(install, mode, prompt, folder)
        try:
            result = gptr_pipeline.run(folder, argv, env, deadline=gptr_pipeline.DEADLINES[mode], job=job, step=step_id,
                                       attempt=step["attempt"], operation=job.id, tick=self.heartbeat(step_id))
        finally:
            self.record_step_usage(step_id, child(folder, "usage.jsonl"), self.gptr_key(mode))
        job.checkpoint(final=True)
        return self.collect_report(step_id, result, role="research_report", key="report")

    def ldr_key(self):
        return usage.route_key("ldr", "quick_summary", (self.book.route("ldr") or {}).get("lock_sha256"))

    def ldr_task(self, job, step_id):
        """Run LDR's documented Python API headlessly for one approved public prompt."""
        with self.projects.connect() as db:
            step = self.step(db, step_id)
            run = self.run_row(db, step["run_id"])
        prompt = json.loads(run["brief_json"])["public_prompts"][step["step_key"]]
        with self.config_lock:
            _, _, (install, env), components = self.ldr_route(prompt)
        folder = private_directory(self.attempt_dir(step_id))
        argv = ldr_pipeline.command(install, folder)
        expected = components["settings"]["provider"] == "openrouter"
        process = None
        try:
            process = gptr_pipeline.run_group(folder, argv, ldr_pipeline.projection(env, folder), deadline=ldr_pipeline.DEADLINE,
                                              job=job, stdin=canonical({"prompt": prompt}), step=step_id,
                                              attempt=step["attempt"], operation=job.id,
                                              tick=self.heartbeat(step_id, child(folder, "progress.json")))
        finally:
            self.record_step_usage(step_id, child(folder, "usage.jsonl"), self.ldr_key(), expected=expected)
        result = ldr_pipeline.collect(folder, process)
        if result["status"] == "cancelled":
            raise Problem("cancelled", "Operation cancelled.", 409)
        job.checkpoint(final=True)
        return self.collect_ldr(step_id, result)

    def collect_ldr(self, step_id, result):
        """Publish the LDR outputs that verified, then apply the completion rule (spec v2 §2.5)."""
        with self.projects.connect() as db:
            step = self.step(db, step_id)
            run = self.run_row(db, step["run_id"])
        key = step["step_key"]
        outputs = result["outputs"]
        code, message = result.get("code"), result.get("message")
        parsed = None
        if result["status"] == "succeeded":
            if not isinstance(result.get("sources_count"), int) or result["sources_count"] <= 0:
                code, message = "no_sources", "LDR retrieved no sources. Its report is kept."
            elif key == "ldr_discovery":
                try:
                    parsed = structured_citations(outputs["citations"]["path"], overflow="truncate") if "citations" in outputs else None
                except Problem:
                    parsed = None
                if not parsed or not any(usable_citation(row) for row in parsed.rows):
                    code, message = "no_usable_citations", "LDR finished but returned no usable citations. Its report is kept."
        if key == "ldr_comparison" and "citations" in outputs:
            try:
                structured_citations(outputs["citations"]["path"], overflow="truncate")
            except Problem:
                outputs = {name: value for name, value in outputs.items() if name != "citations"}
        names = {"report": ("LDR report.md", "report", "discovery_report", "LDR report"),
                 "citations": ("LDR citations.ris", "citations", "citation_export", "LDR citations")}
        staged = []
        try:
            for output_key, item in outputs.items():
                filename, kind, role, title = names[output_key]
                entry = self.projects.stage(run["project_id"], run["chat_id"], item["path"], filename, kind)
                if entry["sha256"] != item["sha256"]:
                    entry["temp"].unlink(missing_ok=True)
                    raise Problem("changed_output", "The LDR output changed while it was collected.", 503)
                staged.append({"key": output_key, "role": role, "stage": entry, "app": "ldr", "version": installer.PINS["ldr"],
                               "title": title, "origin": "public_native"})
        except BaseException:
            for item in staged:
                item["stage"]["temp"].unlink(missing_ok=True)
            raise
        succeeded = result["status"] == "succeeded" and code is None
        if not succeeded and code is None:
            code, message = "native_output", "LDR finished without a verifiable result."

        def check(db):
            if self.step(db, step_id)["state"] not in ("running", "interrupted"):
                raise Problem("stale_attempt", "This attempt was superseded.", 409)

        def finalize(db, ids):
            detail = {"research_id": result.get("research_id"), "sources_count": result.get("sources_count")}
            if parsed is not None:
                detail["citations"] = {"records": parsed.total, "usable": sum(1 for row in parsed.rows if usable_citation(row)),
                                       "truncated": parsed.truncated}
            self.detail(db, step_id, headless=detail, sources_count=result.get("sources_count"))
            if not succeeded:
                self.fail_step(db, step_id, code, message)
                return
            self.set_step(db, step_id, "succeeded", native_result_id=result.get("research_id") or None)

        ids = self.projects.publish(run["project_id"], run["chat_id"], staged, step=step_id, run_id=step_id,
                                    entry=step["entry_id"], check=check, finalize=finalize,
                                    marker=child(private_directory(self.attempt_dir(step_id)), "publish.json")) if staged else []
        if not staged:
            with self.projects.connect(write=True) as db:
                check(db)
                finalize(db, [])
        if succeeded:
            self.verify_and_advance(step_id, outputs, ids)
        self.notify()
        if not succeeded:
            raise Problem(code, message, 422)
        return {"artifact_ids": ids, "sources_count": result.get("sources_count")}

    def verify_and_advance(self, step_id, outputs, ids):
        """Advance only once both published artifacts verifiably belong to this attempt's outputs."""
        with self.projects.connect(write=True) as db:
            step = self.step(db, step_id)
            rows = {row["output_key"]: dict(row) for row in db.execute("SELECT * FROM artifact WHERE step_id=?", (step_id,))}
            if step["state"] != "succeeded" or any(rows.get(key, {}).get("sha256") != item["sha256"] for key, item in outputs.items()):
                self.fail_step(db, step_id, "native_output", "The published LDR outputs could not be verified. Rerun this stage.")
                return
            run = self.run_row(db, step["run_id"])
            if run["state"] not in TERMINAL_RUN:
                self.advance(db, run["id"], step["position"])

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
        self.projects.record_usage(step["entry_id"], usage.scope_gate(observed, sent=bool(key),
                                                                      error=error if observed is None else None))
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
        entries = {row["id"]: row["usage_json"] for row in db.execute(
            "SELECT id, usage_json FROM chat_entry WHERE id IN (SELECT entry_id FROM pipeline_step WHERE run_id=?)", (run["id"],))}
        stamp = utc()
        data = {key: run[key] for key in ("id", "chat_id", "brief_revision", "purpose", "recipe_version", "state", "outcome",
                                          "original_question", "selected_profile", "created_at", "updated_at", "revision")}
        data |= {"scope_id": run["project_id"], "purpose_label": PURPOSES[run["purpose"]], "display": DISPLAY.get(run["state"], run["state"]),
                 "recipe": list(RECIPES[run["purpose"]]), "brief": self.public_brief(brief),
                 "steps": [{key: step[key] for key in ("id", "position", "step_key", "attempt", "entry_id", "state", "round",
                                                       "questionnaire_source", "error_code", "error_detail", "native_result_id",
                                                       "operation_id", "started_at", "finished_at")}
                           | {"display": DISPLAY[step["state"]], "detail": self.public_detail(json.loads(step["detail_json"])),
                              "outputs": outputs.get(step["id"], []),
                              "app": STEPS[step["step_key"]][0], "current": step is current}
                           | self.status_fields(step, entries.get(step["entry_id"]), stamp) for step in steps],
                 "server_time": stamp.isoformat()}
        data["actions"] = self.available(db, run, current, brief)
        if current and current["step_key"] == "scope_gate" and current["state"] == "awaiting_scope":
            data["questionnaires"] = self.questionnaires(db, run["id"])
        recipe = RECIPES[run["purpose"]]
        if current and any(key in recipe for key in ("papers", "paper_selection")) and current["position"] >= 2:
            data["papers"] = self.papers(db, run)
        return data

    @staticmethod
    def status_fields(step, usage_json, stamp):
        """Polled status: elapsed time, deadline, liveness heartbeat, LDR progress and recorded usage."""
        detail = json.loads(step["detail_json"])
        since = step["started_at"] if step["state"] in ("running", "surveying") else (
            step["queued_at"] if step["state"] in ("queued", "waiting_app") else None)
        elapsed = None
        if since:
            try:
                start = datetime.fromisoformat(since)
                start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
                elapsed = max(0, int((stamp - start).total_seconds()))
            except ValueError:
                elapsed = None
        return {"elapsed_seconds": elapsed, "deadline_seconds": DEADLINES.get(step["step_key"]),
                "progress": detail.get("progress"), "heartbeat": detail.get("heartbeat"), "server_time": stamp.isoformat(),
                "usage": json.loads(usage_json) if usage_json else None,
                "mode": "headless" if step["step_key"] in LDR_STEPS else None}

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
        hidden = {key: value for key, value in detail.items() if key not in ("prior", "recovery", "progress", "heartbeat")}
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
        if key in ("papers", "paper_selection") and state == "awaiting_papers":
            actions += ["set_papers", "start_acquisition", "approve_private_lookups", "select_corpus"]
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
            if current["step_key"] == "papers" and current["state"] == "awaiting_papers":
                result["lookup_batch"] = self.lookup_batch(run, current)
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
            fingerprint, display, _, components = self.gptr_route("survey", run["selected_profile"], run["original_question"])
        elif key == "scope_gate":
            fingerprint, display, components = self.gate_route(run["original_question"])
        else:
            brief = json.loads(run["brief_json"])
            fingerprint, display, components = self.step_route(run, key, brief["public_prompts"][key])
        return {"step": current["id"], "reason": current["error_detail"], "route": display,
                "digest": self.remember({"kind": "reconfirm", "step": current["id"], "fingerprint": fingerprint,
                                         "components": components})}

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
            if step["state"] == "paused" and key in MANAGED_STEPS:
                self.set_step(db, step["id"], "queued", queued_at=now())
                self.bump(db, run["id"], "queued")
            elif step["state"] == "paused" and key == "paper_analysis":
                available = pqa_profiles.availability(self.root, self.book).get(run["selected_profile"], {})
                if not available.get("available"):
                    raise Problem("profile_unavailable", available.get("reason") or "Managed PDF analysis is unavailable.", 409)
                self.set_step(db, step["id"], "awaiting_disclosure")
                self.bump(db, run["id"], "awaiting_disclosure")
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
        problem = None
        with self.config_lock:
            for key in RECIPES[run["purpose"]][2:]:
                if key in REPORT_MODES or key in LDR_STEPS:
                    try:
                        fingerprint, display, components = self.step_route(run, key, prompt)
                    except Problem as error:
                        if error.code != "ldr_headless_unavailable":
                            raise
                        problem = {"code": error.code, "message": str(error)}
                        continue
                    routes[key] = {"fingerprint": fingerprint, "display": display, "components": components}
        constraints = [{"question": label, "answer": answer} for label, answer in resolved["lines"]]
        if resolved["instructions"]:
            constraints.append({"question": "Additional instructions", "answer": resolved["instructions"]})
        payload = {"kind": "scope", "run": run["id"], "step": current["id"], "prompt": prompt, "resolved": [list(line) for line in resolved["lines"]],
                   "instructions": resolved["instructions"], "broad": resolved["broad"], "answers": body.get("answers") or {},
                   "routes": routes, "rounds": rounds}
        token = None if problem else self.remember(payload)
        return {"digest": token, "route_problem": problem, "prompt": prompt, "constraints": constraints, "scope_mode": "broad" if resolved["broad"] else "scoped",
                "needs_conflict_review": not resolved["broad"] and bool(constraints), "length": len(prompt),
                "limit": scope_gate.PROMPT_LIMIT, "routes": {key: value["display"] for key, value in routes.items()},
                "private_stage": "PaperQA2 gets a fresh disclosure review later. Its PDFs, question and excerpt never enter this brief."
                if "paper_analysis" in RECIPES[run["purpose"]] else None}, 200

    def do_approve_scope(self, scope, chat, run, current, body, action_id):
        preview = self.recall(body.get("digest"), "scope")
        if preview["step"] != current["id"]:
            raise Problem("stale_preview", "The questions changed. Preview the scope again.", 409)
        with self.config_lock:
            for key, route in preview["routes"].items():
                current_fp = self.step_route(run, key, preview["prompt"])[0]
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
                         display={"prompt_digest": digest(preview["prompt"].encode())})
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
                         step=current["id"], fingerprint=preview["fingerprint"], display={"components": preview.get("components")})
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
            if key not in MANAGED_STEPS:
                raise Problem("action_state", "This stage cannot be rerun.", 409)
            entry = db.execute("SELECT question, quote FROM chat_entry WHERE id=?", (step["entry_id"],)).fetchone()
            # consent() still stops the new attempt at dispatch if any material route dimension changed.
            new = self.new_attempt(db, latest, step["position"], "queued", question=entry["question"], round_number=step["round"])
            if step["round"] == 2:
                self.detail(db, new, prior=json.loads(step["detail_json"]).get("prior"))
            self.approve(db, run["id"], "rerun", {"step": step["id"], "charges": True}, step=new)
            self.bump(db, run["id"], "queued")
            result = self.finish_action(db, scope, chat, run, "rerun", action_id, body, {"new_step": new})
        self.notify()
        return result, 200

    def do_reuse_output(self, scope, chat, run, current, body, action_id):
        preview = self.recall(body.get("digest"), "recovery")
        if preview["step"] != current["id"]:
            raise Problem("stale_preview", "Review the recovered output again.", 409)
        recovery = preview["data"]
        if current["step_key"] in LDR_STEPS:
            folder = self.attempt_dir(current["id"])
            result = ldr_pipeline.collect(folder, gptr_pipeline.ProcessResult("exited", 0, 0.0, []))
            if result["status"] != "succeeded":
                raise Problem("recovery_changed", "The recovered output no longer matches this attempt. Rerun the stage instead.", 409)
            with self.projects.connect(write=True) as db:
                self.guard(db, run, current)
                self.set_step(db, current["id"], "running")
                self.finish_action(db, scope, chat, run, "reuse_output", action_id, body)
            try:
                self.collect_ldr(current["id"], result)
            except Problem as error:
                if error.code not in ("no_sources", "no_usable_citations", "native_output"):
                    raise
            self.forget(body.get("digest"))
            return self.get(scope, chat, run["id"]), 200
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

    # ---- Papers Needed ------------------------------------------------------------------------------

    def suggestions(self, db, run):
        """Citation rows from the confirmed RIS (literature) or the explicit references section (deep)."""
        if run["purpose"] == "literature_review":
            row = db.execute("""SELECT a.* FROM artifact a JOIN pipeline_step s ON s.id=a.step_id WHERE s.run_id=? AND s.position=2
                                AND s.state='succeeded' AND a.output_key='citations' ORDER BY s.attempt DESC LIMIT 1""", (run["id"],)).fetchone()
            if not row:
                return None, []
            rows = structured_citations(self.projects.artifact_path(dict(row)), overflow="truncate").rows
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

    GRANT_FIELDS = ("origin_class", "doi", "pmcid", "pmid", "citation_url", "confirmed_version")

    def lookup_batch(self, run, current):
        """One immutable grant per private row: exact identifiers, sequence and endpoints, capped at oa.PASS_PAPERS."""
        guided = settings.guided(self.config())
        with self.projects.connect() as db:
            rows = [dict(row) for row in db.execute("""SELECT * FROM pipeline_paper WHERE run_id=? AND origin_class<>'public_native'
                                                      AND access_status<>'oa_acquired' AND state='needed' ORDER BY position""",
                                                   (run["id"],))]
        grants, listed, blocked, excluded = {}, [], [], []
        for row in rows:
            found, problem = oa.identifiers(row)
            if problem or not found or not (found.get("doi") or found.get("pmcid")):
                continue
            if found.get("pmcid"):
                sequence, endpoints = ["cloud"], [oa.BUCKET]
                sends = [{"identifier": found["pmcid"][0], "destination": "PMC Cloud Service (" + oa.BUCKET_HOST + ")"}]
            else:
                if not guided["contact"]:
                    blocked.append({"paper_id": row["id"], "identifier": found["doi"],
                                    "reason": "A maintainer contact is needed for DOI lookups. Add one in Settings → Guided research."})
                    continue
                sequence, endpoints = ["converter", "cloud"], [oa.CONVERTER, oa.BUCKET]
                sends = [{"identifier": found["doi"] + " · tool=" + oa.TOOL + " · maintainer contact",
                          "destination": "PMC ID Converter (NCBI)"},
                         {"identifier": "the PMCID returned for this DOI", "destination": "PMC Cloud Service (" + oa.BUCKET_HOST + ")"}]
            if len(grants) >= oa.PASS_PAPERS:
                excluded.append({"paper_id": row["id"], "reason": "Not included in this batch"})
                continue
            grants[row["id"]] = {"run_id": run["id"], "step_id": current["id"], "project_id": run["project_id"],
                                 "paper_id": row["id"], **{field: row[field] for field in self.GRANT_FIELDS},
                                 "lookup": {"doi": found.get("doi"), "pmcid": found["pmcid"][0] if found.get("pmcid") else None},
                                 "sequence": sequence, "endpoints": endpoints, "contact_required": "converter" in sequence}
            listed.append({"paper_id": row["id"], "title": row["title"], "sends": sends, "sequence": sequence})
        if not grants and not blocked:
            return None
        token = self.remember({"kind": "lookup_batch", "run": run["id"], "step": current["id"], "grants": grants}) if grants else None
        return {"rows": listed, "blocked": blocked, "excluded": excluded, "digest": token,
                "notice": "Only the identifiers listed are sent. Titles, notes, report text and PDFs are never sent."}

    def do_approve_private_lookups(self, scope, chat, run, current, body, action_id):
        preview = self.recall(body.get("digest"), "lookup_batch")
        ids = body.get("paper_ids")
        if (preview["run"] != run["id"] or preview["step"] != current["id"] or not isinstance(ids, list) or not ids
                or len(set(ids)) != len(ids) or set(ids) - set(preview["grants"])):
            raise Problem("stale_preview", "The lookup batch changed. Review it again.", 409)
        if self.jobs.busy("oa"):
            raise Problem("busy", "Another lookup is running. Try again when it finishes.", 409)
        grants = {paper: preview["grants"][paper] for paper in ids}
        approvals = []
        with self.projects.connect(write=True) as db:
            self.guard(db, run, current)
            for paper, grant in grants.items():
                approvals.append(self.approve(db, run["id"], "oa_private", digest(canonical(grant)), step=current["id"],
                                              display={"stage": "both", "grant": grant}))
            result = self.finish_action(db, scope, chat, run, "approve_private_lookups", action_id, body)
        try:
            self.queue_acquisition(scope, chat, run["id"], current["id"], list(grants), private="both", grants=grants)
        except Problem:
            with self.projects.connect(write=True) as db:
                db.executemany("UPDATE pipeline_approval SET decision='declined', display_json=json_set(display_json,'$.reason','not_started') WHERE id=?",
                               [(approval,) for approval in approvals])
                db.execute("DELETE FROM pipeline_action WHERE id=?", (id_value(action_id),))
            raise Problem("busy", "Another lookup is running. Try again when it finishes.", 409)
        self.forget(body.get("digest"))
        return result | {"acquisition": "queued"}, 202

    def queue_acquisition(self, scope, chat, run_id, step_id, paper_ids, *, private, grants=None):
        def task(job):
            return self.acquire(job, scope, chat, run_id, step_id, paper_ids, private, grants=grants)

        try:
            return self.jobs.start("oa", task)
        except Problem:
            raise Problem("busy", "Another acquisition pass is running. Try again when it finishes.", 409)

    def grant_matches(self, paper, grant):
        with self.projects.connect() as db:
            row = db.execute("SELECT * FROM pipeline_paper WHERE id=? AND run_id=?", (paper, grant["run_id"])).fetchone()
        if not row or any(row[field] != grant[field] for field in self.GRANT_FIELDS):
            return False
        if [endpoint for endpoint in (oa.CONVERTER, oa.BUCKET) if endpoint in grant["endpoints"]] != grant["endpoints"]:
            return False
        guided = settings.guided(self.config())
        return not grant["contact_required"] or bool(guided["contact"])

    def grant_transport(self, transport, paper, grant):
        """Re-check the grant before every request and allow only its disclosed endpoints and identifiers."""
        pipes = self
        lookup = grant["lookup"]
        bound = {"pmcid": lookup["pmcid"], "converted": False}

        class Granted:
            violation = None

            def get(self, url, **kwargs):
                if not pipes.grant_matches(paper, grant):
                    self.violation = "changed_after_approval"
                    raise Problem("changed_after_approval", "This paper changed after approval. Review the lookup again.", 409)
                converter = False
                if url.startswith(oa.CONVERTER):
                    query = parse_qs(urlsplit(url).query)
                    converter = allowed = ("converter" in grant["sequence"] and oa.CONVERTER in grant["endpoints"]
                                           and bool(lookup["doi"]) and query.get("ids") == [lookup["doi"]] and not bound["converted"])
                elif url.startswith(oa.BUCKET):
                    found = set(re.findall(r"PMC[1-9][0-9]*", url))
                    allowed = ("cloud" in grant["sequence"] and oa.BUCKET in grant["endpoints"] and bound["pmcid"] is not None
                               and found == {bound["pmcid"]})
                else:
                    allowed = False
                if not allowed:
                    self.violation = "outside_grant"
                    raise Problem("outside_grant", "This request is outside the approved lookup.", 403)
                response = transport.get(url, **kwargs)
                if converter:
                    bound["converted"] = True
                    bound["pmcid"] = pipes.converted_pmcid(response, lookup["doi"])
                return response
        return Granted()

    @staticmethod
    def converted_pmcid(response, doi):
        """The single PMCID the converter returned for the approved DOI; only that one may follow at the Cloud Service."""
        try:
            status, _, raw = response
            records = json.loads(raw).get("records") if status == 200 else None
            record = records[0] if isinstance(records, list) and len(records) == 1 and isinstance(records[0], dict) else {}
            if (record.get("doi") or "").lower() != doi:
                return None
            return (oa.pmcid(record.get("pmcid") or "") or (None,))[0]
        except (TypeError, ValueError, AttributeError):
            return None

    def acquire(self, job, scope, chat, run_id, step_id, paper_ids, private, transport=None, grants=None):
        guided = settings.guided(self.config())
        install = installer.install_location(self.root, "pqa")
        python = venv_python(install[1] / "venv") if install else None
        transport = transport or getattr(self, "transport", None) or oa.Transport()
        started = monotonic()
        results = []
        directory = self.projects.files_directory(scope, chat)
        counts = {"selected": len(paper_ids), "processed": 0, "pending": len(paper_ids), "skipped": 0, "failed": 0, "running": True}

        def progress(final=False):
            counts["running"] = not final
            with self.projects.connect(write=True) as db:
                self.detail(db, step_id, acquisition=dict(counts))
                self.bump(db, run_id)

        progress()
        try:
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
                counts["pending"] -= 1
                if row["origin_class"] != "public_native" and not grants:
                    counts["skipped"] += 1
                    continue
                if grants and not self.grant_matches(paper, grants[paper]):
                    result = oa.Result("rejected", row.get("doi") or row.get("pmcid") or "", "grant", error="changed_after_approval")
                    results.append(self.record_acquisition(scope, chat, run, step_id, row, result, private))
                    counts["skipped"] += 1
                    progress()
                    continue
                used = {"files": totals[0], "bytes": totals[1]}

                def budget(size):
                    if size is None:
                        return used["files"] < oa.RUN_FILES
                    return used["files"] + 1 <= oa.RUN_FILES and used["bytes"] + size <= oa.RUN_BYTES

                if not python:
                    result = oa.Result("manual_review", row.get("doi") or "", "pmc", error="parser_unavailable")
                else:
                    channel = self.grant_transport(transport, paper, grants[paper]) if grants else transport
                    try:
                        result = oa.acquire(channel, dict(row), contact=guided["contact"], temp_dir=directory, python=python,
                                            version=row["confirmed_version"], budget=budget)
                    except Problem as error:
                        result = oa.Result("rejected", row.get("doi") or row.get("pmcid") or "", "grant", error=error.code)
                    if getattr(channel, "violation", None):
                        # A grant check refused a request; no request outside the grant was sent.
                        if result.temp:
                            Path(result.temp).unlink(missing_ok=True)
                        result = oa.Result("rejected", row.get("doi") or row.get("pmcid") or "", "grant", error=channel.violation)
                results.append(self.record_acquisition(scope, chat, run, step_id, row, result, private))
                if result.outcome in ("acquired", "existing_match"):
                    counts["processed"] += 1
                elif result.outcome == "rejected" and result.error in ("changed_after_approval", "outside_grant"):
                    counts["skipped"] += 1
                else:
                    counts["failed" if result.outcome in ("failed_retryable", "rejected") else "processed"] += 1
                progress()
        finally:
            progress(final=True)
            self.notify()
        return {"papers": results}

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
            if result.pmcid and not row["pmcid"]:
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
                       WHERE s.state IN ('running','surveying','awaiting_disclosure')
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
            if step["state"] in ("running", "surveying") and folder.exists():
                gptr_pipeline.terminate_orphan(folder)
                if step["step_key"] in LDR_STEPS:
                    self.record_step_usage(step["id"], child(folder, "usage.jsonl"), self.ldr_key(), interrupted=True)
                elif step["step_key"] in ("survey", *REPORT_MODES):
                    mode = "research_report" if step["step_key"] == "survey" else REPORT_MODES[step["step_key"]][0]
                    self.record_step_usage(step["id"], child(folder, "usage.jsonl"), self.gptr_key(mode), interrupted=True)
            with self.projects.connect(write=True) as db:
                current = self.step(db, step["id"])
                if current["state"] in ("running", "surveying"):
                    try:
                        completion = read_json(child(folder, "completion.json"), None) if folder.exists() else None
                    except ValueError:
                        completion = None
                    recovery = None
                    if current["step_key"] in LDR_STEPS:
                        if folder.exists() and ldr_pipeline.collect(folder, gptr_pipeline.ProcessResult("exited", 0, 0.0, []))["status"] == "succeeded":
                            recovery = {"outputs": completion["outputs"]}
                    elif completion and completion.get("status") == "succeeded" and completion.get("outputs"):
                        output = completion["outputs"][0]
                        path = child(folder, "outputs", output["name"])
                        if path.is_file() and file_digest(path) == output["sha256"]:
                            recovery = {"outputs": [output]}
                    self.set_step(db, step["id"], "interrupted", error_code="interrupted",
                                  error_detail="RAH stopped while this stage was running. Inspect it, reuse its verified output, or rerun it.")
                    if recovery:
                        self.detail(db, step["id"], recovery=recovery)
                    self.bump(db, step["run_id"], "interrupted")
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
