from pathlib import Path
import sys
from threading import Thread
from time import monotonic, sleep
from unittest.mock import Mock

import pytest

from importlib import resources

from rah.common import Problem, clean_env
from rah.jobs import Job
from rah.runs import Runs


def test_cancel_reaps_owned_paper_worker(tmp_path, monkeypatch):
    observer = resources.files("rah").joinpath("openrouter_observer.py").read_bytes()

    class Script:
        def joinpath(self, name):
            assert name in ("worker.py", "pqa_profile_worker.py", "openrouter_observer.py")
            self.name = name
            return self

        def read_bytes(self):
            if self.name == "openrouter_observer.py":
                return observer
            return b"""import json, sys, time
from pathlib import Path
json.load(sys.stdin)
Path('started').touch()
time.sleep(30)
Path('unexpected-result').touch()
"""

    monkeypatch.setattr("rah.runs.resources.files", lambda name: Script())
    runs = object.__new__(Runs)
    runs.collect = Mock()
    job = Job("test", "pqa", state="Running")
    cancelled = []

    def request_cancel():
        deadline = monotonic() + 5
        while not (tmp_path / "started").exists() and monotonic() < deadline:
            sleep(0.02)
        if (tmp_path / "started").exists():
            cancelled.append(True)
        job.cancel()

    thread = Thread(target=request_cancel)
    thread.start()
    try:
        with pytest.raises(Problem) as error:
            runs.invoke(tmp_path, Path(sys.executable), clean_env(), {"app": "pqa"}, {"app": "pqa"}, job=job)
    finally:
        thread.join()
    assert cancelled
    assert error.value.code == "cancelled"
    assert not (tmp_path / "unexpected-result").exists()
    runs.collect.assert_not_called()
    Runs.clean_run(runs, tmp_path)
    assert not tmp_path.exists()