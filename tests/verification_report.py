"""Small, credential-free reports for standalone verification commands."""
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
from tempfile import NamedTemporaryFile
from time import monotonic


@contextmanager
def verification(path, name, **context):
    start = monotonic()
    report = {"schema_version": 1, "check": name, "status": "passed",
              "platform": platform.system(), "arch": platform.machine(),
              "python": platform.python_version(), "checks": {}, **context}
    for package in ("research-agent-hub", "paper-qa"):
        try:
            report[package] = version(package)
        except PackageNotFoundError:
            pass
    try:
        yield report
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        if path is not None:
            report["duration_seconds"] = round(monotonic() - start, 3)
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
                json.dump(report, output, indent=2, allow_nan=False)
                output.write("\n")
            Path(output.name).replace(path)