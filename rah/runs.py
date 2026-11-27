from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import io
import re
import shutil
import subprocess
import sys
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4

from . import pqa_profiles, settings
from .common import Problem, atomic, canonical, child, clean_env, digest, file_digest, private_directory, read_json, venv_python, write_json


PAPER_ROLES = pqa_profiles.PAPER_ROLES


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
        self.guided = None
        self.lock = Lock()
        self.recover()
        self.projects.interrupt()

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
                        try:
                            self.collect(folder, record)
                        except Problem as error:
                            if error.code != "no_sources":
                                raise
                            if record.get("entry"):
                                self.projects.transition(record["project"], record["entry"], ("Running",), "Failed", reason=str(error))
                    shutil.rmtree(folder)
            except (Problem, OSError, ValueError, KeyError, TypeError):
                continue

    def paper_route(self, config, quality="standard"):
        """Resolve an enabled managed PaperQA2 route. Native model overrides never select a managed route."""
        route, location, profile = pqa_profiles.resolve(self.root, self.book, quality)
        if not config.values.get("RAH_OPENROUTER_API_KEY"):
            raise Problem("needs_credential", "Add the shared OpenRouter key before a managed PDF transfer.", 409)
        return route, location, profile

    def expire(self):
        with self.lock:
            stale = [key for key, preview in self.previews.items() if preview.expires < monotonic()]
            for key in stale:
                shutil.rmtree(self.previews.pop(key).directory, ignore_errors=True)

    def availability(self, quality="standard"):
        try:
            self.paper_route(settings.load(self.root), quality)
        except Problem as error:
            return {"available": False, "reason": str(error), "code": error.code}
        except OSError:
            return {"available": False, "reason": "Repair the PaperQA2 install and try again."}
        return {"available": True}

    def reviewing(self, preview_id):
        self.expire()
        with self.lock:
            return preview_id in self.previews

    def has_reviews(self):
        self.expire()
        with self.lock:
            return bool(self.previews)

    def preview(self, project, ids, question, *, entry=None, quote="", quality="standard", guided=None):
        config = settings.load(self.root)
        route, location, profile = self.paper_route(config, quality)
        rows = self.projects.selected_pdfs(project, ids)
        preview_id = str(uuid4())
        folder = private_directory(self.folder(preview_id))
        try:
            for row in rows:
                if file_digest(self.projects.artifact_path(row)) != row["sha256"]:
                    raise Problem("changed_pdf", "A PDF changed. Attach the current version before continuing.", 409)
            binding = {"project": project, "entry": entry, "credential": digest(config.values["RAH_OPENROUTER_API_KEY"].encode()),
                       "question": question, "quote": quote, "install": location.name, "lock": route["lock_sha256"],
                       "files": [[row["id"], row["original_name"], row["sha256"]] for row in rows], "profile": profile,
                       "quality": quality, "guided": guided, "worker": pqa_profiles.worker_digest()}
            preview = Preview(folder, binding, rows, monotonic() + 900, digest(canonical(binding)))
            self.expire()
            with self.lock:
                if entry and guided:
                    row = self.projects.entry(project, entry)
                    if self.guided:
                        self.guided.review_opened(guided, preview_id)
                    if prior := self.previews.pop(row["preview_id"] or "", None):
                        shutil.rmtree(prior.directory, ignore_errors=True)
                    self.projects.set_preview(project, entry, preview_id)
                elif entry:
                    row = self.projects.entry(project, entry)
                    self.projects.transition(project, entry, ("Preparing", "Awaiting approval"), "Awaiting approval",
                                             input_ids=ids, preview_id=preview_id, reason=None)
                    if prior := self.previews.pop(row["preview_id"], None):
                        shutil.rmtree(prior.directory, ignore_errors=True)
                self.previews[preview_id] = preview
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return {"preview_id": preview_id, "binding": preview.digest, "profile": profile, "question": question,
                "quote": quote, "files": [row["original_name"] for row in rows], "reindex": len(rows),
                "sizes": [row["size_bytes"] for row in rows], "quality": quality}

    def observe_request(self, project, entry, *, run_id=None, allowed=("Preparing",)):
        if not entry:
            return {}
        def reserve(job):
            self.projects.transition(project, entry, allowed, "Running", operation_id=job.id,
                                     run_id=run_id or job.id, reason=None)
        def finished(job):
            if job.state != "Succeeded":
                state = "Cancelled" if job.state == "Cancelled" else "Failed"
                self.projects.transition(project, entry, ("Running",), state, reason=job.reason)
        return {"reserve": reserve, "finished": finished}

    def approve(self, project, preview_id, binding, approve):
        self.expire()
        with self.lock:
            preview = self.previews.get(preview_id)
            if not preview or project != preview.binding["project"] or binding != preview.digest:
                raise Problem("stale_preview", "The preview expired or changed. Review it again.", 409)
            entry = preview.binding["entry"]
            if entry and self.projects.entry(project, entry)["preview_id"] != preview_id:
                raise Problem("stale_preview", "The preview changed. Review it again.", 409)
            guided = preview.binding.get("guided")
            if guided and self.guided:
                operation = self.guided.review_decided(guided, preview, approve)
                self.previews.pop(preview_id)
                return operation
            if approve:
                callbacks = self.observe_request(project, entry, run_id=preview_id, allowed=("Awaiting approval",))
                mark_done = callbacks.get("finished")
                def finished(job):
                    self.clean_run(preview.directory)
                    if mark_done:
                        mark_done(job)
                operation = self.jobs.start("pqa", lambda job: self.run_paper(preview, job),
                                            **(callbacks | {"finished": finished}))
            else:
                if entry:
                    self.projects.transition(project, entry, ("Awaiting approval",), "Cancelled", preview_id=None)
                shutil.rmtree(preview.directory, ignore_errors=True)
                operation = None
            self.previews.pop(preview_id)
            return operation

    def run_paper(self, preview, job):
        info = preview.binding
        folder = preview.directory
        try:
            config = settings.load(self.root)
            route, installation, profile = self.paper_route(config, info.get("quality", "standard"))
            selected = self.projects.selected_pdfs(info["project"], [row[0] for row in info["files"]])
            if ([[row["id"], row["original_name"], row["sha256"]] for row in selected] != info["files"]
                    or digest(config.values["RAH_OPENROUTER_API_KEY"].encode()) != info["credential"]
                    or route["lock_sha256"] != info["lock"]
                    or installation.name != info["install"] or info["profile"] != profile
                    or info.get("worker") != pqa_profiles.worker_digest()
                    or digest(canonical(info)) != preview.digest):
                raise Problem("stale_preview", "The PDF route or settings changed. Review the selection again.", 409)
            corpus = private_directory(child(folder, "corpus"))
            for row in selected:
                staged = child(corpus, row["storage_name"])
                shutil.copyfile(self.projects.artifact_path(row), staged)
                if file_digest(staged) != row["sha256"]:
                    raise Problem("changed_pdf", "A selected PDF changed. Review it again.", 409)
            job.checkpoint()
            home = private_directory(child(folder, "pqa-home"))
            env = clean_env() | {
                "OPENROUTER_API_KEY": config.values["RAH_OPENROUTER_API_KEY"], "PQA_HOME": str(home),
                "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home), "LOCALAPPDATA": str(home),
                "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PYTHONNOUSERSITE": "1",
                "PYTHON_DOTENV_DISABLED": "1", "NO_PROXY": "*",
            }
            record = {"app": "pqa", "project": info["project"], "version": route["upstream_version"],
                      "entry": info["entry"], "guided": info.get("guided"),
                      "files": [{"id": row["id"], "name": row["original_name"], "sha256": row["sha256"],
                                 "storage": row["storage_name"]} for row in preview.rows]}
            question = info["question"]
            if info["quote"]:
                question += "\n\nUser-selected excerpt:\n" + info["quote"]
            return self.invoke(folder, venv_python(installation / "venv"), env,
                               {"app": "pqa", "question": question, "profile": profile}, record, job=job)
        finally:
            self.clean_run(folder)

    def public_report(self, project, question, mode, spec, version, job, *, entry=None):
        job.checkpoint(final=True)
        folder = private_directory(self.folder(job.id))
        args = [str(value) for value in spec.command] + ["--report_type", mode, "--no-pdf", "--no-docx", "--", question]
        record = {"app": "gptr", "project": project, "version": version, "entry": entry}
        try:
            return self.invoke(folder, sys.executable, clean_env(), {"app": "gptr", "command": args, "env": spec.env}, record)
        finally:
            self.clean_run(folder)

    def clean_run(self, folder):
        if not (folder / "run.json").exists() or (
            (folder / "completion.json").exists() and (folder / "collected.json").exists()
        ):
            shutil.rmtree(folder, ignore_errors=True)

    def invoke(self, folder, python, env, payload, record, *, job=None):
        name = "pqa_profile_worker.py" if payload.get("app") == "pqa" else "worker.py"
        script = child(folder, name)
        atomic(script, resources.files("rah").joinpath(name).read_bytes())
        write_json(child(folder, "run.json"), record)
        try:
            command = [python, script]
            options = {"cwd": folder, "env": env, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if job is None:
                returncode = subprocess.run(command, input=canonical(payload), **options).returncode
            else:
                job.checkpoint()
                with subprocess.Popen(command, stdin=subprocess.PIPE, **options) as worker:
                    try:
                        pending = canonical(payload)
                        deadline = monotonic() + 600
                        while True:
                            job.checkpoint()
                            try:
                                worker.communicate(input=pending, timeout=min(0.25, max(0, deadline - monotonic())))
                                break
                            except subprocess.TimeoutExpired:
                                if monotonic() >= deadline:
                                    raise
                                pending = None
                    finally:
                        if worker.poll() is None:
                            worker.kill()
                    returncode = worker.returncode
                job.checkpoint(final=True)
        except subprocess.TimeoutExpired as error:
            write_json(child(folder, "collected.json"), {"failed": True})
            write_json(child(folder, "completion.json"), {"succeeded": False})
            raise Problem("worker_timeout", "PaperQA2 timed out. Review the selection before retrying.", 503) from error
        except OSError as error:
            write_json(child(folder, "collected.json"), {"failed": True})
            write_json(child(folder, "completion.json"), {"succeeded": False})
            raise Problem("worker_start", "The native worker could not start.", 503) from error
        except Problem as error:
            if error.code == "cancelled":
                write_json(child(folder, "completion.json"), {"succeeded": False})
                write_json(child(folder, "collected.json"), {"cancelled": True})
            raise
        if returncode:
            if (folder / "completion.json").exists():
                write_json(child(folder, "collected.json"), {"failed": True})
            raise Problem("worker_failed", "The native worker failed. Verify provider and native setup.", 503)
        try:
            output = self.collect(folder, record)
        except Problem as error:
            if error.code == "no_sources":
                write_json(child(folder, "collected.json"), {"failed": True})
            raise
        write_json(child(folder, "collected.json"), output)
        return output

    def collect(self, folder, record):
        if record["app"] == "pqa":
            result = read_json(child(folder, "result.json"))
            if record.get("guided"):
                if not self.guided:
                    raise Problem("guided_unavailable", "The guided controller is not running.", 503)
                return self.guided.paper_result(record, result, folder.name)
            artifact = self.projects.register(record["project"], io.BytesIO(result["answer"].encode()),
                                              "PaperQA2 answer.md", "answer", app="pqa", version=record["version"],
                                              run_id=folder.name, bib=result.get("bib"), entry=record.get("entry"))
        else:
            outputs = child(folder, "outputs")
            files = list(outputs.glob("*.md"))
            if len(files) != 1:
                raise Problem("native_output", "Expected one native Markdown report.", 503)
            path = child(outputs, files[0].name)
            with path.open("rb") as report:
                header = report.read(128 * 1024).split(b"\n---\n", 1)
                if len(header) == 2 and header[0].startswith(b"---\n") and re.search(rb"^sources_count: 0\s*$", header[0], re.M):
                    raise Problem("no_sources", "No sources retrieved. No sourced report was saved. Check the search source or try a different query.", 422)
                report.seek(0)
                artifact = self.projects.register(record["project"], report, path.name, "report",
                                                  app="gptr", version=record["version"], run_id=folder.name, entry=record.get("entry"))
        return {"artifact_id": artifact, "submitted_files": record.get("files", [])}
