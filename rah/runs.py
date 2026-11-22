from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import io
import platform
import shutil
import subprocess
import sys
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4

from . import installer, settings
from .common import Problem, atomic, canonical, child, clean_env, digest, file_digest, private_directory, read_json, venv_python, write_json


PAPER_ROLES = {
    "answer": "Question, cited excerpts and answer context",
    "summary": "Selected paper passages and citation metadata",
    "agent": "Question, tool context and evidence excerpts",
    "embedding": "Text chunks and query text",
}
OPENROUTER_BASE = "https://openrouter.ai/api/v1/"


@dataclass
class Preview:
    directory: Path
    binding: dict
    rows: list
    expires: float
    digest: str


class Runs:
    def __init__(self, root, book, projects, jobs):
        self.root = root
        self.book = book
        self.projects = projects
        self.jobs = jobs
        self.previews = {}
        self.lock = Lock()
        self.recover()

    def folder(self, run_id):
        return child(self.root, "hub", "runs", run_id)

    def recover(self):
        for path in child(self.root, "hub", "runs").iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                if str(UUID(path.name)) != path.name:
                    continue
                folder = self.folder(path.name)
                record = read_json(child(folder, "run.json"))
                marker = read_json(child(folder, "completion.json"))
                if not record:
                    shutil.rmtree(folder)
                elif marker:
                    if marker["succeeded"]:
                        self.collect(folder, record)
                    shutil.rmtree(folder)
            except (Problem, OSError, ValueError, KeyError):
                continue

    def paper_route(self, config):
        route = self.book.route("pqa")
        evidence = (route or {}).get("paper_evidence") or {}
        if (not route or self.book.custom or route.get("qualification_only") or evidence.get("status") != "verified"
                or evidence.get("lock_sha256") != route["lock_sha256"] or evidence.get("profile") != "openrouter-text-v1"
                or not evidence.get("reference")):
            raise Problem("unverified_profile", "The managed OpenRouter PDF route still needs data-flow verification. Open native PaperQA2 separately.", 503)
        worker = resources.files("rah").joinpath("worker.py").read_bytes()
        install = installer.install_location(self.root, "pqa")
        if (evidence.get("host") != installer.host_identity()
                or evidence.get("worker_sha256") != digest(worker)
                or evidence.get("python") != platform.python_version()
                or evidence.get("upstream_version") != route["upstream_version"]
                or evidence.get("policy") != "single-provider-no-fallback"
                or not install or install[0].get("upstream_version") != route["upstream_version"]
                or install[0].get("lock_sha256") != route["lock_sha256"]
                or (self.book.directory is not None and
                    file_digest(child(self.book.directory, route["lock_path"])) != route["lock_sha256"])):
            raise Problem("unverified_profile", "This installed PaperQA2 runtime has no matching data-flow verification.", 503)
        location = install[1]
        projection = settings.resolve(config, "pqa", self.book.capabilities("pqa"))
        stored = settings.checked_projection(self.root, "pqa", config)
        if any(stored.get(key) != projection[key] for key in ("models", "env", "args", "native_file")):
            raise Problem("settings_stale", "Reapply the saved PaperQA2 settings.", 409)
        models = projection["models"]
        destinations = []
        for role, data in PAPER_ROLES.items():
            model = models.get(role)
            if not model or model["provider"] != "openrouter":
                raise Problem("unverified_role", f"Select a verified OpenRouter model for PaperQA2 {role}.", 409)
            providers = evidence.get("routes", {}).get(role, {})
            provider = providers.get(model["model"])
            if (not isinstance(provider, str) or not provider
                    or any(c.isspace() for c in provider)):
                raise Problem("unverified_role", f"PaperQA2 {role} has no verified provider for the selected model.", 409)
            endpoint = "embeddings" if role == "embedding" else "chat/completions"
            destinations.append({"role": role, "service": "OpenRouter", "provider": provider,
                                 "model": model["model"], "endpoint": OPENROUTER_BASE + endpoint, "data": data})
        profile = {"id": "openrouter-text-v1", "parser": "Local text-only PDF parsing",
                   "evidence": evidence["reference"], "policy": evidence["policy"],
                   "identity": digest(canonical(evidence)), "destinations": destinations,
                   "python": evidence["python"], "upstream_version": evidence["upstream_version"]}
        return route, location, profile

    def expire(self):
        with self.lock:
            stale = [key for key, preview in self.previews.items() if preview.expires < monotonic()]
            for key in stale:
                shutil.rmtree(self.previews.pop(key).directory, ignore_errors=True)

    def availability(self):
        try:
            self.paper_route(settings.load(self.root))
        except Problem as error:
            return {"available": False, "reason": str(error)}
        except OSError:
            return {"available": False, "reason": "Repair the PaperQA2 install and try again."}
        return {"available": True}

    def preview(self, project, revision, ids, question):
        config = settings.load(self.root)
        route, location, profile = self.paper_route(config)
        rows = self.projects.selected_pdfs(project, ids, revision)
        preview_id = str(uuid4())
        folder = private_directory(self.folder(preview_id))
        try:
            for row in rows:
                if file_digest(self.projects.artifact_path(row)) != row["sha256"]:
                    raise Problem("changed_pdf", "A PDF changed. Attach the current version before continuing.", 409)
            binding = {"project": project, "project_revision": revision, "settings_revision": config.revision,
                       "question": question, "install": location.name, "lock": route["lock_sha256"],
                       "files": [[row["id"], row["sha256"]] for row in rows], "profile": profile}
            preview = Preview(folder, binding, rows, monotonic() + 900, digest(canonical(binding)))
            self.expire()
            with self.lock:
                self.previews[preview_id] = preview
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return {"preview_id": preview_id, "binding": preview.digest, "profile": profile, "question": question,
                "files": [row["original_name"] for row in rows], "reindex": len(rows)}

    def approve(self, project, preview_id, binding, approve):
        self.expire()
        with self.lock:
            preview = self.previews.get(preview_id)
            if not preview or project != preview.binding["project"] or binding != preview.digest:
                raise Problem("stale_preview", "The preview expired or changed. Review it again.", 409)
            self.previews.pop(preview_id)
        if not approve:
            shutil.rmtree(preview.directory, ignore_errors=True)
            return None
        try:
            return self.jobs.start("pqa", lambda job: self.run_paper(preview, job))
        except BaseException:
            shutil.rmtree(preview.directory, ignore_errors=True)
            raise

    def run_paper(self, preview, job):
        info = preview.binding
        folder = preview.directory
        try:
            config = settings.load(self.root)
            route, installation, profile = self.paper_route(config)
            self.projects.selected_pdfs(info["project"], [row["id"] for row in preview.rows], info["project_revision"])
            if (config.revision != info["settings_revision"] or route["lock_sha256"] != info["lock"]
                    or installation.name != info["install"] or info["profile"] != profile
                    or digest(canonical(info)) != preview.digest):
                raise Problem("stale_preview", "The PDF route or settings changed. Review the selection again.", 409)
            corpus = private_directory(child(folder, "corpus"))
            for row in preview.rows:
                staged = child(corpus, row["storage_name"])
                shutil.copyfile(self.projects.artifact_path(row), staged)
                if file_digest(staged) != row["sha256"]:
                    raise Problem("changed_pdf", "A selected PDF changed. Review it again.", 409)
            job.checkpoint(final=True)
            home = private_directory(child(folder, "pqa-home"))
            env = clean_env() | {
                "OPENROUTER_API_KEY": config.values["RAH_OPENROUTER_API_KEY"], "PQA_HOME": str(home),
                "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home), "LOCALAPPDATA": str(home),
                "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PYTHONNOUSERSITE": "1",
                "PYTHON_DOTENV_DISABLED": "1", "NO_PROXY": "*",
            }
            record = {"app": "pqa", "project": info["project"], "version": route["upstream_version"],
                      "files": [{"id": row["id"], "name": row["original_name"], "sha256": row["sha256"]} for row in preview.rows]}
            return self.invoke(folder, venv_python(installation / "venv"), env, {"app": "pqa", "question": info["question"], "profile": profile}, record)
        finally:
            self.clean_run(folder)

    def public_report(self, project, question, mode, spec, version, job):
        job.checkpoint(final=True)
        folder = private_directory(self.folder(job.id))
        args = [str(value) for value in spec.command] + ["--report_type", mode, "--no-pdf", "--no-docx", "--", question]
        record = {"app": "gptr", "project": project, "version": version}
        try:
            return self.invoke(folder, sys.executable, clean_env(), {"app": "gptr", "command": args, "env": spec.env}, record)
        finally:
            self.clean_run(folder)

    def clean_run(self, folder):
        if not (folder / "run.json").exists() or (
            (folder / "completion.json").exists() and (folder / "collected.json").exists()
        ):
            shutil.rmtree(folder, ignore_errors=True)

    def invoke(self, folder, python, env, payload, record):
        script = child(folder, "worker.py")
        atomic(script, resources.files("rah").joinpath("worker.py").read_bytes())
        write_json(child(folder, "run.json"), record)
        try:
            result = subprocess.run([python, script], cwd=folder, env=env, input=canonical(payload),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=600 if record["app"] == "pqa" else None)
        except subprocess.TimeoutExpired as error:
            write_json(child(folder, "collected.json"), {"failed": True})
            write_json(child(folder, "completion.json"), {"succeeded": False})
            raise Problem("worker_timeout", "PaperQA2 timed out. Review the selection before retrying.", 503) from error
        except OSError as error:
            write_json(child(folder, "collected.json"), {"failed": True})
            write_json(child(folder, "completion.json"), {"succeeded": False})
            raise Problem("worker_start", "The native worker could not start.", 503) from error
        if result.returncode:
            if (folder / "completion.json").exists():
                write_json(child(folder, "collected.json"), {"failed": True})
            raise Problem("worker_failed", "The native worker failed. Verify provider and native setup.", 503)
        output = self.collect(folder, record)
        write_json(child(folder, "collected.json"), output)
        return output

    def collect(self, folder, record):
        if record["app"] == "pqa":
            result = read_json(child(folder, "result.json"))
            artifact = self.projects.register(record["project"], io.BytesIO(result["answer"].encode()),
                                              "PaperQA2 answer.md", "answer", app="pqa", version=record["version"],
                                              run_id=folder.name, bib=result.get("bib"))
        else:
            outputs = child(folder, "outputs")
            files = list(outputs.glob("*.md"))
            if len(files) != 1:
                raise Problem("native_output", "Expected one native Markdown report.", 503)
            path = child(outputs, files[0].name)
            with path.open("rb") as report:
                artifact = self.projects.register(record["project"], report, path.name, "report",
                                                  app="gptr", version=record["version"], run_id=folder.name)
        return {"artifact_id": artifact, "submitted_files": record.get("files", [])}
