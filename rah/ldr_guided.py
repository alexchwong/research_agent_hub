"""Headless guided LDR runner. Runs inside the pinned LDR venv, in its own attempt folder.

It calls LDR's documented Python API (quick_summary) with the approved public prompt and converts the result
with LDR's own RIS exporter. It is the only writer of completion.json and result.json for an LDR attempt.
It never reads or writes the native LDR data directory, never writes LDR history, never prints content and
never retries. Stdin: {"prompt": "<approved public prompt>"}.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import unicodedata

PROMPT_LIMIT = 8000
MESSAGE_LIMIT = 200
PHASE_LIMIT = 40


def atomic_json(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def clean(value, limit):
    if not isinstance(value, str):
        return None
    text = "".join(char for char in value if unicodedata.category(char)[0] != "C" or char == " ")
    return text[:limit]


class Progress:
    """Tolerant LDR progress callback. Writes progress.json at most once a second plus a final write."""

    def __init__(self, path):
        self.path = path
        self.seq = 0
        self.written = 0.0
        self.state = {"percent": None, "message": None, "phase": None, "seq": 0, "at": None}

    def __call__(self, message=None, percent=None, metadata=None, *args, **kwargs):
        try:
            self.seq += 1
            value = None
            if isinstance(percent, (int, float)) and not isinstance(percent, bool) and math.isfinite(percent):
                value = max(0, min(100, int(percent)))
            phase = None
            if isinstance(metadata, dict):
                phase = clean(metadata.get("phase"), PHASE_LIMIT)
            text = clean(message, MESSAGE_LIMIT)
            self.state = {"percent": value, "message": text, "phase": phase, "seq": self.seq,
                          "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if time.monotonic() - self.written >= 1:
                self.flush()
        except Exception:
            pass

    def flush(self):
        try:
            atomic_json(self.path, self.state)
            self.written = time.monotonic()
        except Exception:
            pass


def output(path, data):
    path.write_bytes(data)
    return {"name": path.name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def run(cwd):
    request = json.loads(sys.stdin.read())
    prompt = request.get("prompt") if isinstance(request, dict) else None
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_LIMIT:
        raise ValueError("invalid prompt")
    from local_deep_research.api.research_functions import quick_summary
    from local_deep_research.exporters.ris_exporter import RISExporter

    progress = Progress(cwd / "progress.json")
    result = quick_summary(query=prompt, progress_callback=progress)
    progress.flush()
    formatted = result.get("formatted_findings") if isinstance(result, dict) else None
    if not isinstance(formatted, str) or not formatted.strip() or formatted.startswith("Error:"):
        atomic_json(cwd / "completion.json", {"status": "failed", "reason": "no_report"})
        return 3
    outputs_dir = cwd / "outputs"
    outputs_dir.mkdir(exist_ok=True)
    outputs = [output(outputs_dir / "report.md", formatted.encode("utf-8"))]
    ris_error = None
    try:
        content = RISExporter().export(formatted).content
        if isinstance(content, str):
            content = content.encode("utf-8")
        if content and content.strip():
            outputs.append(output(outputs_dir / "citations.ris", content))
    except Exception as error:
        ris_error = type(error).__name__
    sources = result.get("sources_count")
    atomic_json(cwd / "result.json", {"research_id": str(result.get("research_id") or ""),
                                      "sources_count": sources if isinstance(sources, int) and not isinstance(sources, bool) else None,
                                      "outputs": outputs, "ris_error": ris_error})
    atomic_json(cwd / "completion.json", {"status": "succeeded", "outputs": outputs,
                                          "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return 0


def main():
    cwd = Path.cwd()
    try:
        code = run(cwd)
    except BaseException as error:  # Record the class only; never content.
        try:
            atomic_json(cwd / "completion.json", {"status": "failed", "reason": type(error).__name__})
        except Exception:
            pass
        raise SystemExit(1)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
