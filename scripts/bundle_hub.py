"""Build a host bootstrap wheelhouse and install its exact wheel in a clean venv."""
import argparse
import hashlib
from pathlib import Path
import platform
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]


def run(*args):
    subprocess.run(args, cwd=REPO, check=True)


def python_in(venv):
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def bundle(uv, target):
    target.mkdir(parents=True, exist_ok=False)
    wheelhouse = target / "wheelhouse"
    wheelhouse.mkdir()
    run(uv, "build", "--wheel", "--out-dir", wheelhouse)
    wheels = list(wheelhouse.glob("research_agent_hub-*.whl"))
    if len(wheels) != 1:
        raise ValueError("Expected exactly one RAH release wheel")
    lock = REPO / "hub-deps.lock"
    run(sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
        "--only-binary=:all:", "--require-hashes", "--dest", wheelhouse, "-r", lock)
    hub_hash = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    lines = lock.read_text(encoding="utf-8").splitlines()
    lines.append("research-agent-hub==0.1.0 --hash=sha256:" + hub_hash)
    (target / "hub.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (target / "python-version.txt").write_text(platform.python_version() + "\n", encoding="utf-8")
    (target / "sha256.txt").write_text("".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(target).as_posix() + "\n"
        for path in [target / "hub.lock", target / "python-version.txt", *sorted(wheelhouse.glob("*.whl"))]
    ), encoding="utf-8")


def install(uv, target, venv, system_python):
    version = (target / "python-version.txt").read_text(encoding="utf-8").strip()
    if system_python:
        selector = [sys._base_executable]
    else:
        run(uv, "python", "install", version)
        selector = ["--managed-python", "--python", version]
    run(uv, "venv", *(["--python", *selector] if system_python else selector), venv)
    run(uv, "pip", "sync", "--python", python_in(venv), "--require-hashes", "--only-binary", ":all:",
        "--no-index", "--find-links", target / "wheelhouse", target / "hub.lock")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--venv", type=Path)
    parser.add_argument("--system-python", action="store_true", help="Use the builder Python for a local test.")
    args = parser.parse_args()
    bundle(args.uv.resolve(), args.bundle.resolve())
    if args.venv:
        install(args.uv.resolve(), args.bundle.resolve(), args.venv.resolve(), args.system_python)
