# This script is copied into an app's launch directory. It requires only Python's stdlib.
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    launch = Path(__file__).resolve().parent
    base = launch.parent
    app = base.name
    install = json.loads((base / "current.json").read_text(encoding="utf-8"))
    profile = json.loads((launch / "profile.json").read_text(encoding="utf-8"))
    checksum = profile.pop("checksum")
    canonical = json.dumps(profile, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()
    if hashlib.sha256(canonical).hexdigest() != checksum:
        raise SystemExit("Generated launch profile has changed. Use the native command or reapply settings in RAH.")
    venv = base / "versions" / install["install_id"] / "venv"
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    allowed = {
        "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "SYSTEMDRIVE",
        "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "PATHEXT", "COMSPEC", "USER", "USERNAME",
        "HOMEDRIVE", "HOMEPATH",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(profile["env"], PYTHON_DOTENV_DISABLED="1", PYTHONNOUSERSITE="1", NO_PROXY="*")
    native = base / "native"
    arguments = sys.argv[1:]
    browser = app == "gptr" and arguments == ["--browser"]
    if app == "ldr":
        cmd = [scripts / ("ldr-web" + suffix)]
        env.update(LDR_WEB_HOST="127.0.0.1", LDR_WEB_PORT="58721", LDR_WEB_USE_HTTPS="false", LDR_DATA_DIR=str(native))
    elif app == "gptr":
        if browser:
            cmd = [scripts / ("python" + suffix), venv.parent / "gptr_browser.py", venv.parent / "source", "58722"]
        else:
            cmd = [scripts / ("python" + suffix), venv.parent / "source/cli.py"]
    else:
        cmd = [scripts / ("pqa" + suffix), *profile["args"]]
        env["PQA_HOME"] = str(native)
        if source := profile.get("native_file"):
            config = Path(source).expanduser()
            if config.suffix.lower() != ".json" or not config.is_absolute() or not config.is_file():
                raise SystemExit("Select an existing absolute PaperQA2 JSON settings path.")
            # PaperQA2's pinned native loader appends .json to its config name.
            cmd[1:1] = ["--settings", str(config.with_suffix(""))]
    print("RAH standalone settings revision", profile["revision"][:12], flush=True)
    arguments = [] if browser else arguments or ([] if app == "ldr" else ["--help"])
    return subprocess.call([*cmd, *arguments], cwd=native, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
