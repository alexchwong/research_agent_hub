"""Run from an integrity-checked release bundle with its pinned Python."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from bundle_hub import python_in, sync_wheelhouse

bundle = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("root", type=Path)
parser.add_argument("uv", type=Path)
parser.add_argument("--bootstrap-only", action="store_true", help="Install the hub without starting setup.")
args = parser.parse_args()
root = args.root.resolve()
uv = args.uv.resolve()
if platform.python_version() != (bundle / "python-version.txt").read_text().strip():
    raise SystemExit("The bootstrap Python does not match the release.")
if subprocess.check_output([uv, "--version"], text=True).split()[1] != "0.13.9":
    raise SystemExit("The bootstrap uv does not match the release.")
root.mkdir(parents=True, exist_ok=True)
if shutil.disk_usage(root).free < 512 * 1024**2:
    raise SystemExit("Free at least 512 MB for the hub. Core apps need additional disk space.")
release = hashlib.sha256((bundle / "hub.lock").read_bytes()).hexdigest()
runtime = root / "installs" / release
runtime.mkdir(parents=True, exist_ok=True)
venv = runtime / "venv"
retained_uv = runtime / uv.name
lock = runtime / "installing"
try:
    lock.mkdir()
except FileExistsError:
    raise SystemExit("Another installer is active. If a previous install was interrupted, remove its empty installing directory and retry.")
try:
    if not retained_uv.exists() or retained_uv.read_bytes() != uv.read_bytes():
        candidate = runtime / (uv.name + ".new")
        shutil.copy2(uv, candidate)
        os.replace(candidate, retained_uv)
    if not (runtime / "ready").is_file() or not python_in(venv).is_file():
        subprocess.run([retained_uv, "venv", "--clear", "--python", sys.executable, venv], check=True)
        sync_wheelhouse(retained_uv, bundle, venv)
        (runtime / "ready").touch()
finally:
    lock.rmdir()

if args.bootstrap_only:
    raise SystemExit(0)

# The bootstrap lock guards the runtime files, not the foreground hub session.
command = [python_in(venv), "-I", "-m", "rah.cli", "--data-dir", root,
           "setup", "--uv", retained_uv, "--foreground"]
if os.name != "nt":
    os.execv(command[0], command)
try:
    raise SystemExit(subprocess.run(command).returncode)
except KeyboardInterrupt:
    raise SystemExit(130)
