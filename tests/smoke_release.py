"""Install a release ZIP through its shipped launcher, then smoke-test the installed hub."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def smoke_release(archive_path, work, report, packaged, shortcut):
    checksum = archive_path.with_suffix(".zip.sha256").read_text().split()[0]
    with archive_path.open("rb") as archive:
        if hashlib.file_digest(archive, "sha256").hexdigest() != checksum:
            raise ValueError(f"Release ZIP checksum mismatch: {archive_path.name}")
        bundle = work / "bundle"
        archive.seek(0)
        with ZipFile(archive) as release:
            release.extractall(bundle)

    home = work / "home"
    home.mkdir()
    env = os.environ | {"UV_PYTHON_INSTALL_DIR": str(work / "python"),
                        "UV_PYTHON_BIN_DIR": str(work / "bin")}
    system = platform.system()
    if system == "Windows":
        env["LOCALAPPDATA"] = str(home)
        root = home / "Research Agent Hub"
        command = ["cmd.exe", "/d", "/c", "Install.cmd", "-BootstrapOnly"]
        python_path, uv_name = "Scripts/python.exe", "uv.exe"
    elif system == "Darwin":
        env["HOME"] = str(home)
        root = home / "Library/Application Support/Research Agent Hub"
        command = ["./Install.command", "--bootstrap-only"]
        python_path, uv_name = "bin/python", "uv"
    elif system == "Linux":
        env["XDG_DATA_HOME"] = str(home)
        root = home / "research-agent-hub"
        command = ["sh", "Install.sh", "--bootstrap-only"]
        python_path, uv_name = "bin/python", "uv"
    else:
        raise ValueError(f"Unsupported release smoke host: {system}")
    if system != "Windows":
        # ZipFile does not restore executable permissions when extracting.
        (bundle / uv_name).chmod(0o755)
        if system == "Darwin":
            (bundle / "Install.command").chmod(0o755)

    subprocess.run(command, input="yes\n", text=True, cwd=bundle, env=env, check=True, timeout=300)
    release_id = hashlib.sha256((bundle / "hub.lock").read_bytes()).hexdigest()
    runtime = root / "installs" / release_id
    if not (runtime / "ready").is_file() or (runtime / "installing").exists():
        raise RuntimeError("The release bootstrap did not finish installing the hub runtime.")
    uv = runtime / uv_name
    if uv.read_bytes() != (bundle / uv_name).read_bytes():
        raise RuntimeError("The retained uv does not match the release ZIP.")
    smoke = Path(__file__).with_name("smoke_setup.py").resolve()
    command = [runtime / "venv" / python_path, "-E", "-s", smoke, "--uv", uv, "--report", report]
    if packaged:
        command.append("--packaged")
    if shortcut:
        command.append("--shortcut")
    subprocess.run(command, cwd=work, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--packaged", action="store_true")
    parser.add_argument("--shortcut", action="store_true", help="Test shortcut creation in a disposable account.")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    archives = list(args.release_dir.glob("RAH-*.zip"))
    if len(archives) != 1:
        sys.exit(f"Expected exactly one release ZIP in {args.release_dir}")
    with TemporaryDirectory(prefix="rah-zip-", dir=os.environ.get("RUNNER_TEMP")) as temp:
        smoke_release(archives[0], Path(temp).resolve(), args.report.resolve(), args.packaged, args.shortcut)