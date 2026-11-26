"""Build a host bootstrap wheelhouse and install its exact wheel in a clean venv."""
import argparse
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
from tempfile import TemporaryDirectory
import zipfile
import sys

REPO = Path(__file__).resolve().parents[1]


def run(*args, cwd=None):
    subprocess.run(args, cwd=REPO if cwd is None else cwd, check=True)


def python_in(venv):
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def build_wheel(uv, wheelhouse):
    # Setuptools can reuse newer files under build/lib, even after a checkout changes.
    # Copy only the build inputs and keep working-tree edits (including new package files).
    with TemporaryDirectory(prefix="source-", dir=wheelhouse.parent) as directory:
        source = Path(directory)
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copyfile(REPO / name, source / name)
        package = source / "rah"
        shutil.copytree(REPO / "rah", package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.py[co]", ".DS_Store"))
        expected = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).digest()
                    for path in package.rglob("*") if path.is_file()}

        run(uv, "build", "--wheel", "--out-dir", wheelhouse, cwd=source)
        wheels = list(wheelhouse.glob("research_agent_hub-*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected exactly one RAH release wheel")
        with zipfile.ZipFile(wheels[0]) as wheel:
            files = [entry for entry in wheel.infolist() if entry.filename.startswith("rah/") and not entry.is_dir()]
            actual = {entry.filename: hashlib.sha256(wheel.read(entry)).digest() for entry in files}
        missing = expected.keys() - actual.keys()
        extra = actual.keys() - expected.keys()
        changed = {name for name in expected.keys() & actual.keys() if expected[name] != actual[name]}
        if missing or extra or changed or len(files) != len(actual):
            raise ValueError(f"Wheel differs from staged RAH source: missing={sorted(missing)}, "
                             f"extra={sorted(extra)}, changed={sorted(changed)}, "
                             f"duplicate entries={len(files) - len(actual)}")
        return wheels[0]


def bundle(uv, target):
    target.mkdir(parents=True, exist_ok=False)
    wheelhouse = target / "wheelhouse"
    wheelhouse.mkdir()
    wheel = build_wheel(uv, wheelhouse)
    lock = REPO / "hub-deps.lock"
    run(sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
        "--only-binary=:all:", "--require-hashes", "--dest", wheelhouse, "-r", lock)
    hub_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lines = lock.read_text(encoding="utf-8").splitlines()
    # The source lock downloads the pinned release from GitHub; the installed
    # lock must use the verified copy in the wheelhouse without network access.
    for i, line in enumerate(lines):
        if line.startswith("modelspell @ "):
            artifact, checksum = line.removeprefix("modelspell @ ").split()
            version = artifact.rsplit("/", 1)[1].split("-")[1]
            lines[i] = f"modelspell=={version} {checksum}"
    lines.append("research-agent-hub==0.1.0 --hash=sha256:" + hub_hash)
    (target / "hub.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (target / "python-version.txt").write_text(platform.python_version() + "\n", encoding="utf-8")
    (target / "sha256.txt").write_text("".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(target).as_posix() + "\n"
        for path in [target / "hub.lock", target / "python-version.txt", *sorted(wheelhouse.glob("*.whl"))]
    ), encoding="utf-8")


def release_zip(uv, target, output):
    hosts = {("Linux", "x86_64"): "ubuntu-x64", ("Darwin", "arm64"): "macos-arm64",
             ("Windows", "AMD64"): "windows-x64"}
    host = hosts.get((platform.system(), platform.machine()))
    if not host or platform.python_version() != "3.12.14":
        raise ValueError("Build this release ZIP on a supported host with Python 3.12.14.")
    if subprocess.check_output([uv, "--version"], text=True).split()[1] != "0.13.9":
        raise ValueError("Use the pinned uv 0.13.9 executable.")
    scripts = REPO / "scripts"
    for name in ("bundle_hub.py", "install_release.py"):
        shutil.copy2(scripts / name, target / name)
    shutil.copy2(REPO / "LICENSE", target / "LICENSE")
    if host == "windows-x64":
        for name in ("Install.cmd", "Install.ps1"):
            shutil.copy2(scripts / name, target / name)
        shutil.copy2(uv, target / "uv.exe")
        entry = "Install.cmd"
    else:
        entry = "Install.command" if host == "macos-arm64" else "Install.sh"
        shutil.copy2(scripts / "Install.sh", target / entry).chmod(0o755)
        shutil.copy2(uv, target / "uv").chmod(0o755)
    (target / "host.txt").write_text(host + "\n", encoding="utf-8")
    instructions = {
        "Install.sh": "Extract the ZIP and run sh Install.sh in a terminal from the extracted folder.",
        "Install.command": "Extract the ZIP and open Install.command on macOS 14 or later. If macOS blocks it, use the documented Gatekeeper steps.",
        "Install.cmd": "Unblock the downloaded ZIP before extraction, then open Install.cmd.",
    }
    (target / "START.txt").write_text(
        instructions[entry] + "\nConfirm once to install the hub and all three core apps.\n"
        "See the official release page for the ZIP checksum and accepted operating systems.\n",
        encoding="utf-8")
    files = sorted(path for path in target.rglob("*") if path.is_file() and path.name != "sha256.txt")
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                        f"{path.relative_to(target).as_posix()}\n" for path in files)
    (target / "sha256.txt").write_text(checksums, encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"RAH-0.1.0-{host}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for path in [*files, target / "sha256.txt"]:
            zip_file.write(path, path.relative_to(target).as_posix())
    with archive.open("rb") as handle:
        checksum = hashlib.file_digest(handle, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")


def sync_wheelhouse(uv, target, venv):
    run(uv, "pip", "sync", "--python", python_in(venv), "--require-hashes", "--only-binary", ":all:",
        "--no-index", "--find-links", target / "wheelhouse", target / "hub.lock", cwd=target)


def install(uv, target, venv, system_python):
    version = (target / "python-version.txt").read_text(encoding="utf-8").strip()
    if system_python:
        selector = [sys._base_executable]
    else:
        run(uv, "python", "install", version)
        selector = ["--managed-python", "--python", version]
    run(uv, "venv", *(["--python", *selector] if system_python else selector), venv)
    sync_wheelhouse(uv, target, venv)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--venv", type=Path)
    parser.add_argument("--release-dir", type=Path)
    parser.add_argument("--system-python", action="store_true", help="Use the builder Python for a local test.")
    args = parser.parse_args()
    bundle(args.uv.resolve(), args.bundle.resolve())
    if args.venv:
        install(args.uv.resolve(), args.bundle.resolve(), args.venv.resolve(), args.system_python)

    if args.release_dir:
        release_zip(args.uv.resolve(), args.bundle.resolve(), args.release_dir.resolve())
