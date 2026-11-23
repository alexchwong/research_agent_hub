from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock, RLock
from uuid import uuid4

from .common import Problem


@dataclass
class Job:
    id: str
    resource: str
    state: str = "Queued"
    result: dict | None = None
    reason: str | None = None
    detail: dict = field(default_factory=dict)
    cancellable: bool = True
    requested: bool = False
    lock: Lock = field(default_factory=Lock)

    def checkpoint(self, final=False):
        with self.lock:
            if self.requested:
                raise Problem("cancelled", "Operation cancelled.", 409)
            if final:
                self.cancellable = False

    def cancel(self):
        with self.lock:
            if not self.cancellable or self.state not in {"Queued", "Running"}:
                raise Problem("not_cancellable", "This operation has crossed its cancellation boundary.", 409)
            self.requested = True

    def active(self):
        return self.state in {"Queued", "Running"}

    def report(self, name, state, message):
        with self.lock:
            self.detail[name] = {"state": state, "message": message}

    def status(self):
        with self.lock:
            return {"id": self.id, "state": self.state, "result": self.result, "reason": self.reason,
                    "cancellable": self.cancellable and self.active(), "apps": dict(self.detail)}


class Jobs:
    def __init__(self):
        self.lock = RLock()
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.records = {}
        self.closing = False

    def start(self, resource, task, *, reserve=None, finished=None):
        with self.lock:
            if self.closing or self.busy(resource):
                raise Problem("busy", "An operation is already active.", 409)
            job = Job(str(uuid4()), resource)
            if reserve:
                reserve(job)
            self.records[job.id] = job
            self.executor.submit(self.run, job, task, finished)
            return job.id

    def run(self, job, task, finished=None):
        with job.lock:
            job.state = "Running"
        try:
            job.checkpoint()
            result = task(job)
        except Problem as error:
            with job.lock:
                job.reason = str(error)
                job.state = "Cancelled" if error.code == "cancelled" else "Failed"
        except Exception:
            with job.lock:
                job.reason = "The operation failed. Check the native environment and documentation."
                job.state = "Failed"
        else:
            with job.lock:
                job.result = result
                job.state = "Succeeded"
        finally:
            if finished:
                finished(job)

    def busy(self, resource=None):
        with self.lock:
            return any(job.active() and (resource is None or job.resource == resource) for job in self.records.values())

    def close_if_idle(self):
        with self.lock:
            if self.busy():
                raise Problem("busy", "Wait for the current operations before quitting.", 409)
            self.closing = True

    def get(self, job):
        with self.lock:
            try:
                return self.records[job]
            except KeyError as error:
                raise Problem("missing_operation", "Operation not found.", 404) from error
