"""Small worker for the legacy managed GPT Researcher CLI report. Managed PaperQA2 uses pqa_profile_worker.py."""
import json
import os
from pathlib import Path
import subprocess
import sys


def save(path, payload):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    temporary.replace(path)


def main():
    cwd = Path.cwd()
    succeeded = False
    try:
        request = json.load(sys.stdin)
        if request["app"] != "gptr":
            raise ValueError("The retired managed PaperQA2 route is no longer available")
        subprocess.run(request["command"], cwd=cwd, env=request["env"], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        succeeded = True
    finally:
        save(cwd / "completion.json", {"succeeded": succeeded})


if __name__ == "__main__":
    main()
