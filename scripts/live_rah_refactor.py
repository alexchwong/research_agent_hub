#!/usr/bin/env python3
"""P6: one owner-authorised live qualification campaign for the purpose-pipeline refactor.

Run with the installed hub's Python from a repository checkout:

    export OPENROUTER_API_KEY=sk-or-v1-...
    python scripts/live_rah_refactor.py --profile all --output reports/rah-live.json

The runner provisions a temporary, disposable RAH data root and never reads your real one. It prompts once
before any public billable call, then separately for each synthetic PaperQA2 corpus and provider route
(including a decline probe). Missing approval or credentials produce skipped outcomes with instructions.
Exit 0: requested mandatory managed checks passed. Exit 1: an automated check failed. Exit 2: none failed
but a mandatory managed check was skipped or lacked approval. Outstanding manual checks are always printed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
PROFILES = ("budget", "standard", "premium")
REDACT = [re.compile(r"sk-or-v1-[A-Za-z0-9]+"), re.compile(r"Bearer\s+\S+"), re.compile(r"[?&][^\s]*="),
          re.compile(r"(/|\\)(home|Users|users)(/|\\)[^\s]+")]
FACT = "The synthetic RAH trial enrolled 418 participants and reported a 12 percent reduction in the primary outcome."
SECOND = "A second synthetic RAH cohort used a seven day follow-up window."
QUESTION = "How many participants did the synthetic RAH trial enrol? Cite the source."
ABSENT = "What adverse events did the synthetic RAH trial report? Cite the source or say the papers do not say."


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(text, limit=300):
    text = str(text or "")
    for pattern in REDACT:
        text = pattern.sub("[redacted]", text)
    return text[:limit]


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def write_report(path, report):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def ask(prompt, assume=None):
    if assume is not None:
        print(prompt + (" [yes]" if assume else " [no]"))
        return assume
    try:
        return input(prompt + " Type yes to approve: ").strip().lower() == "yes"
    except EOFError:
        return False


class Campaign:
    def __init__(self, args):
        self.args = args
        self.checks, self.manual = [], []
        self.key = os.environ.get("OPENROUTER_API_KEY", "")
        self.report = {"schema_version": SCHEMA_VERSION, "baseline_sha": "b3b5df450a15f0b38aa3bffb4fdc9a42e31a3400",
                       "candidate_build_sha256": self.build_digest(), "host": self.host(), "started_at": now(),
                       "finished_at": None, "profiles_requested": list(args.profiles), "checks": self.checks,
                       "manual_checks": self.manual, "summary": {}}

    @staticmethod
    def build_digest():
        import rah
        package = Path(rah.__file__).parent
        hasher = hashlib.sha256()
        for path in sorted(package.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                hasher.update(path.relative_to(package).as_posix().encode() + b"\0" + path.read_bytes())
        return hasher.hexdigest()

    @staticmethod
    def host():
        from rah import installer
        identity = installer.host_identity()
        return identity | {"python": platform.python_version()}

    def check(self, check_id, status, *, profile=None, route=None, started=None, code=None, detail="", remediation="",
              http_status=None, model=None, provider=None, evidence=None, mandatory=False):
        row = {"id": check_id, "profile": profile, "route_id": route, "status": status,
               "duration_ms": int((time.monotonic() - started) * 1000) if started else 0,
               "configured_model": model, "observed_provider": provider, "code": code, "detail": redact(detail),
               "remediation": remediation, "mandatory": mandatory}
        if http_status:
            row["http_status"] = http_status
        if evidence:
            row["evidence"] = evidence
        self.checks.append(row)
        print(f"[{status}] {check_id}" + (f" · {profile}" if profile else "") + (f" · {redact(detail, 120)}" if detail else ""), flush=True)
        write_report(self.args.output, self.report)
        return row

    def manual_check(self, check_id, reason):
        self.manual.append({"id": check_id, "status": "pending", "reason": reason})

    # ---- setup ---------------------------------------------------------------------------------------

    def provision(self):
        started = time.monotonic()
        self.root = Path(tempfile.mkdtemp(prefix="rah-live-"))
        os.chmod(self.root, 0o700)
        uv = self.args.uv or shutil.which("uv")
        if not uv:
            self.check("setup", "skipped", started=started, code="uv_missing", remediation="Pass --uv with the approved uv path.", mandatory=True)
            return False
        result = subprocess.run([sys.executable, "-I", "-m", "rah.cli", "--data-dir", str(self.root), "setup", "--uv", str(uv),
                                 "--no-shortcut", "--no-browser"], stdin=subprocess.DEVNULL, capture_output=True, timeout=7200)
        if result.returncode:
            self.check("setup", "fail", started=started, code="setup_failed", detail=result.stderr.decode(errors="replace")[-300:],
                       remediation="Retry setup on this host; see DEVEL.md.", mandatory=True)
            return False
        subprocess.run([sys.executable, "-I", "-m", "rah.cli", "--data-dir", str(self.root), "remove-hub", "--no-shortcut"],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=600)
        lines = [f"RAH_OPENROUTER_API_KEY={json.dumps(self.key)}"]
        for env, key in (("RAH_LIVE_TAVILY_API_KEY", "RAH_NATIVE_GPTR__TAVILY_API_KEY"), ("RAH_LIVE_SEARX_URL", "RAH_NATIVE_GPTR__SEARX_URL")):
            if os.environ.get(env):
                lines.append(f"{key}={json.dumps(os.environ[env])}")
        if os.environ.get("RAH_LIVE_GPTR_RETRIEVER"):
            lines.append(f"RAH_NATIVE_GPTR__RETRIEVER={json.dumps(os.environ['RAH_LIVE_GPTR_RETRIEVER'])}")
        if os.environ.get("RAH_OA_PMC_CONTACT_EMAIL"):
            lines.append(f"RAH_OA_PMC_CONTACT_EMAIL={json.dumps(os.environ['RAH_OA_PMC_CONTACT_EMAIL'])}")
        config = self.root / "hub/config/.env"
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(config, 0o600)
        self.check("setup", "pass", started=started, detail="Disposable data root provisioned with the three pinned apps.", mandatory=True)
        return True

    def hub(self):
        from rah.installer import RouteBook
        from rah.web import create_app
        self.app = create_app(self.root, RouteBook.load(), None, "live", "live-token", lambda: None, lambda: "127.0.0.1:9")
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer live-token", "Origin": "http://127.0.0.1:9"}
        from rah import settings
        config = settings.load(self.root)
        self.call("post", "/settings/apply", {"revision": config.revision, "regenerate": True})

    def call(self, method, path, data=None):
        response = getattr(self.client, method)("/api" + path, base_url="http://127.0.0.1:9", headers=self.headers, json=data)
        return response.status_code, response.get_json()

    # ---- public guided flows ---------------------------------------------------------------------------

    def guided(self, profile, purpose, question, deep=False):
        started = time.monotonic()
        chat = self.call("post", "/chats", {})[1]["id"]
        base = f"/projects/{chat}/chats/{chat}/pipelines"
        status, preview = self.call("post", base + "/preview", {"purpose": purpose, "profile": profile, "question": question})
        if status != 200:
            return self.check(f"{purpose}_preview", "fail", profile=profile, started=started, code=preview.get("code"),
                              detail=preview.get("message"), http_status=status, mandatory=True)
        status, run = self.call("post", base, {"request_id": str(uuid4()), "purpose": purpose, "profile": profile, "question": question,
                                               "confirmation_digest": preview["digest"], "public_confirmed": True})
        wait = lambda states, limit: self.wait(base, run["id"], states, limit)
        data = wait({"awaiting_scope", "paused", "reconfirm_recipients", "failed"}, 600)
        survey = data["steps"][0]
        self.check("survey", "pass" if survey["state"] == "succeeded" else "fail", profile=profile, started=started,
                   code=survey.get("error_code"), detail=f"sources={survey['detail'].get('sources_count')}",
                   model="deepseek/deepseek-v4.1-flash-1017", mandatory=True)
        if data["state"] != "awaiting_scope":
            return None
        gate = data["questionnaires"][0]
        self.check("scope_gate", "pass" if gate["source"] == "model" else "fail", profile=profile, started=started,
                   code=(gate.get("error") or {}).get("code"), model="anthropic/claude-haiku-5.5",
                   provider=(gate.get("observed") or {}).get("provider"), detail=f"questions={len(gate['questions'])}", mandatory=True)
        first = gate["questions"][0]
        answers = {first["id"]: {"selected": [first["options"][0]["id"]], "custom": "Prefer peer-reviewed sources", "custom_mode": "add"}}
        status, scope = self.action(base, run["id"], "preview_scope", {"answers": answers, "instructions": "Keep the report short.", "broad": False})
        self.action(base, run["id"], "approve_scope", {"digest": scope["digest"], "constraints_reviewed": True})
        if purpose == "rapid_research" or deep:
            data = wait({"succeeded", "paused", "failed", "reconfirm_recipients"}, 1500 if deep else 900)
            final = data["steps"][-1]
            self.check("deep_report" if deep else "rapid_report", "pass" if data["state"] == "succeeded" else "fail", profile=profile,
                       started=started, code=final.get("error_code"), detail=f"sources={final['detail'].get('sources_count')}", mandatory=True)
        return base, run["id"]

    def action(self, base, run, action, data):
        current = self.call("get", f"{base}/{run}")[1]
        return self.call("post", f"{base}/{run}/actions", {"action": action, "action_id": str(uuid4()),
                                                            "expected_revision": current["revision"], "data": data})

    def wait(self, base, run, states, limit):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            data = self.call("get", f"{base}/{run}")[1]
            if data["state"] in states:
                return data
            time.sleep(2)
        return self.call("get", f"{base}/{run}")[1]

    # ---- OA, usage and errors ---------------------------------------------------------------------------

    def open_access(self):
        from rah import oa, installer
        started = time.monotonic()
        install = installer.install_location(self.root, "pqa")
        python = (install[1] / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")) if install else None
        target = os.environ.get("RAH_LIVE_OA_PMCID")
        with tempfile.TemporaryDirectory(dir=self.root) as temp:
            if target and python:
                result = oa.acquire(oa.Transport(), {"pmcid": target}, contact=os.environ.get("RAH_OA_PMC_CONTACT_EMAIL"), temp_dir=temp, python=python)
                self.check("oa_known_pmc", "pass" if result.outcome == "acquired" else "fail", started=started, code=result.error,
                           detail=f"{result.outcome} {result.licence or ''}", evidence={"sha256": result.sha256} if result.sha256 else None)
            else:
                self.check("oa_known_pmc", "skipped", started=started, code="needs_known_pmcid",
                           remediation="Set RAH_LIVE_OA_PMCID to a PMCID known to be in the PMC Open Access Subset with a PDF.")
            unresolved = oa.acquire(oa.Transport(), {"doi": "10.1000/rah-unresolved-probe"}, contact=os.environ.get("RAH_OA_PMC_CONTACT_EMAIL"),
                                    temp_dir=temp, python=python or sys.executable)
            self.check("oa_unresolved", "pass" if unresolved.outcome in ("unresolved", "manual_review") else "fail", started=started,
                       code=unresolved.error, detail=unresolved.outcome)

    def usage(self):
        started = time.monotonic()
        status, data = self.call("post", "/usage/refresh", {})
        ok = status == 200 and not data.get("error") and data["fields"].get("usage") is not None
        self.check("usage", "pass" if ok else "fail", started=started, code=data.get("error"), http_status=status)

    def error_handling(self):
        from rah import scope_gate
        from rah.common import Problem
        started = time.monotonic()
        try:
            scope_gate.call("sk-or-v1-invalid-rah-probe", scope_gate.request_body("Public probe", "Survey"))
            self.check("provider_error_handling", "fail", started=started, detail="An invalid key was accepted.")
        except Problem as error:
            self.check("provider_error_handling", "pass" if error.code in ("authorization_failed", "provider_error") else "fail",
                       started=started, code=error.code, detail=str(error))

    # ---- PaperQA2 candidate qualification -------------------------------------------------------------

    def endpoints(self, model):
        request = Request("https://openrouter.ai/api/v1/models/" + quote(model, safe="/") + "/endpoints",
                          headers={"Authorization": "Bearer " + self.key, "Accept": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
            data = json.loads(response.read(2_000_000))
        return [row["provider_name"] for row in (data.get("data") or {}).get("endpoints", []) if isinstance(row, dict) and row.get("provider_name")]

    def corpus(self, folder, partial=False):
        sys.path.insert(0, str(REPO / "tests"))
        from fixtures import text_pdf
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "synthetic-trial.pdf").write_bytes(text_pdf(FACT))
        if not partial:
            (folder / "synthetic-cohort.pdf").write_bytes(text_pdf(SECOND))
        return sorted(folder.iterdir())

    def qualify(self, profile):
        from rah import installer, pqa_profiles
        from rah.research_profiles import PQA_ROUTES
        key = PQA_ROUTES[profile]
        route = pqa_profiles.REGISTRY[key]
        started = time.monotonic()
        install = installer.install_location(self.root, "pqa")
        if not install:
            return self.check("pqa_route", "skipped", profile=profile, route=key, started=started, code="pqa_missing", mandatory=profile == "standard")
        proposed = {}
        for role, data in route["roles"].items():
            override = os.environ.get(f"RAH_LIVE_PQA_{profile.upper()}_{role.upper()}_PROVIDER")
            try:
                proposed[role] = override or (self.endpoints(data["model"]) or [None])[0]
            except (OSError, ValueError):
                proposed[role] = override
        if not all(proposed.values()):
            return self.check("pqa_route", "skipped", profile=profile, route=key, started=started, code="no_eligible_provider",
                              detail=json.dumps(proposed), remediation="Set RAH_LIVE_PQA_<PROFILE>_<ROLE>_PROVIDER for each role.",
                              mandatory=profile == "standard")
        print(f"\nPaperQA2 {profile} route {key}: synthetic corpus of two generated PDFs is sent for analysis.")
        for role, data in route["roles"].items():
            print(f"  {role}: {data['model']} via OpenRouter → {proposed[role]} only (no fallback) at {data['endpoint']}")
        if not ask(f"Approve this exact provider tuple and synthetic transfer for {profile}?", self.args.assume_yes):
            return self.check("pqa_route", "skipped", profile=profile, route=key, started=started, code="not_approved",
                              mandatory=profile == "standard")
        record, location = install
        identity = pqa_profiles.identity(key, proposed, upstream_version=record["upstream_version"], lock_sha256=record["lock_sha256"])
        profile_data = pqa_profiles.profile(key, proposed, upstream_version=record["upstream_version"], python=platform.python_version(),
                                            qualification=True)
        python = location / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        outcomes = {}
        for name, question, partial, providers in (("cited_answer", QUESTION, False, proposed), ("abstention", ABSENT, True, proposed),
                                                   ("refuses_fallback", QUESTION, True, proposed | {"answer": "rah-invalid-provider"})):
            run_started = time.monotonic()
            result = self.worker(python, profile_data | {"destinations": pqa_profiles.destinations(key, providers)}, question, partial)
            if name == "refuses_fallback":
                passed = not result.get("succeeded")
            elif name == "abstention":
                answer = result.get("answer", "").lower()
                passed = not result.get("succeeded") or any(phrase in answer for phrase in (
                    "cannot answer", "insufficient", "not report", "does not", "no information", "not mention", "unable"))
            else:
                passed = bool(result.get("succeeded")) and "418" in result.get("answer", "") and bool(result.get("bib"))
            outcomes[name] = passed
            self.check(f"pqa_{name}", "pass" if passed else "fail", profile=profile, route=key, started=run_started,
                       detail=result.get("error", "") if not passed else "", model=route["roles"]["answer"]["model"],
                       provider=proposed["answer"], mandatory=profile == "standard")
        print("Decline probe: the next prompt checks that declining sends nothing.")
        declined = not ask("Decline probe: approve another transfer? (answer no)", False if self.args.assume_yes else None)
        self.check("pqa_decline_sends_nothing", "pass" if declined else "fail", profile=profile, route=key, mandatory=profile == "standard")
        evidence = digest_bytes(json.dumps({"identity": identity, "outcomes": outcomes}, sort_keys=True).encode())
        status = "pass" if all(outcomes.values()) and declined else "fail"
        self.check("pqa_route", status, profile=profile, route=key, started=started, mandatory=profile == "standard",
                   evidence={"identity": identity, "evidence_sha256": evidence,
                             "enable_entry": {**{k: identity[k] for k in ("platform", "arch", "python", "upstream_version", "lock_sha256", "worker_sha256")},
                                              "providers": proposed, "evidence_sha256": evidence} if status == "pass" else None})

    def worker(self, python, profile, question, partial):
        from rah import pqa_profiles
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            folder = Path(folder)
            self.corpus(folder / "corpus", partial)
            (folder / "pqa_profile_worker.py").write_bytes(pqa_profiles.worker_bytes())
            home = folder / "home"
            home.mkdir()
            env = {key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG")}
            env |= {"OPENROUTER_API_KEY": self.key, "PQA_HOME": str(home), "HOME": str(home), "USERPROFILE": str(home),
                    "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PYTHONNOUSERSITE": "1", "PYTHON_DOTENV_DISABLED": "1", "NO_PROXY": "*"}
            try:
                subprocess.run([str(python), str(folder / "pqa_profile_worker.py")], cwd=folder, env=env, timeout=900,
                               input=json.dumps({"app": "pqa", "question": question, "profile": profile}).encode(),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return {"succeeded": False, "error": "worker_timeout"}
            completion = json.loads((folder / "completion.json").read_text()) if (folder / "completion.json").exists() else {}
            result = json.loads((folder / "result.json").read_text()) if (folder / "result.json").exists() else {}
            return {"succeeded": completion.get("succeeded", False), "answer": result.get("answer", ""), "bib": result.get("bib", "")}

    # ---- campaign -------------------------------------------------------------------------------------

    def run(self):
        self.manual_check("ldr_native_discovery", "Run Literature review on a desktop: Review and start in LDR, save Markdown and RIS "
                          "from the authenticated result, use Reopen LDR handoff after a hub restart, and confirm native history.")
        self.manual_check("desktop_matrix", "Repeat the guided walkthrough on Windows 11 x64, Apple silicon macOS (including 14) and Ubuntu x64.")
        if not self.key:
            for check in ("setup", "survey", "scope_gate", "rapid_report", "deep_report", "pqa_route"):
                self.check(check, "skipped", code="missing_credentials", remediation="export OPENROUTER_API_KEY before running.", mandatory=True)
            return
        print("RAH live qualification campaign")
        print(f"  Profiles: {', '.join(self.args.profiles)}")
        print("  Public calls: GPT Researcher survey (DeepSeek), Scope Gate (Haiku), Rapid report per profile and one Deep report,")
        print("  via OpenRouter with provider selection by the service; search through the configured GPT Researcher retriever;")
        print("  PMC ID Converter and PMC Cloud Service for open-access checks. Advertised OpenRouter prices apply; RAH sets no ceiling.")
        print("  Native paths: a temporary data root under the system temp directory. Your real RAH data root is never used.")
        if not ask("Approve these public billable calls?", self.args.assume_yes):
            for check in ("survey", "scope_gate", "rapid_report", "deep_report"):
                self.check(check, "skipped", code="not_approved", remediation="Rerun and approve the public calls.", mandatory=True)
        else:
            if not self.provision():
                return
            self.hub()
            for index, profile in enumerate(self.args.profiles):
                self.guided(profile, "rapid_research", "What is known about the effect of vitamin D supplementation on fracture risk in older adults?")
                if index == 0:
                    self.guided(profile, "deep_research", "Evidence for exercise interventions in knee osteoarthritis", deep=True)
            self.open_access()
            self.usage()
            self.error_handling()
            for profile in self.args.profiles:
                self.qualify(profile)

    def finish(self):
        self.report["finished_at"] = now()
        failed = [row for row in self.checks if row["status"] == "fail"]
        mandatory_gaps = [row for row in self.checks if row["mandatory"] and row["status"] == "skipped"]
        self.report["summary"] = {"passed": sum(row["status"] == "pass" for row in self.checks), "failed": len(failed),
                                  "skipped": sum(row["status"] == "skipped" for row in self.checks),
                                  "manual_pending": len(self.manual),
                                  "qualified_routes": [row["route_id"] for row in self.checks if row["id"] == "pqa_route" and row["status"] == "pass"]}
        write_report(self.args.output, self.report)
        for row in self.manual:
            print(f"[manual] {row['id']}: {row['reason']}")
        if self.root_kept():
            print(f"Disposable data root kept at {self.root} (--keep).")
        return 1 if failed else 2 if mandatory_gaps else 0

    def root_kept(self):
        root = getattr(self, "root", None)
        if root and not self.args.keep:
            shutil.rmtree(root, ignore_errors=True)
            return False
        return bool(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", choices=(*PROFILES, "all"), default="all")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv", type=Path, help="Approved uv executable for provisioning the disposable root")
    parser.add_argument("--keep", action="store_true", help="Keep the disposable data root for inspection")
    parser.add_argument("--assume-yes", action="store_true", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.profiles = list(PROFILES) if args.profile == "all" else [args.profile]
    campaign = Campaign(args)
    try:
        campaign.run()
    except KeyboardInterrupt:
        campaign.check("campaign", "fail", code="interrupted", detail="Interrupted by the operator.")
    except Exception as error:  # Partial reports are still written.
        campaign.check("campaign", "fail", code=type(error).__name__, detail=str(error))
    return campaign.finish()


if __name__ == "__main__":
    sys.exit(main())
