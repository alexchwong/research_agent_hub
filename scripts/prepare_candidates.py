"""Resolve disposable, per-host upstream locks. This cannot qualify private PDF egress."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
from urllib.request import Request, urlopen


APPS = {
    "ldr": ("local-deep-research", "2.0.1", "2.0.1", "ldr-web", "MIT"),
    "gptr": ("gpt-researcher", "0.17.0", "3.8.0", "cli.py", "Apache-2.0 source; MIT PyPI metadata"),
    "pqa": ("paper-qa", "2026.8.12", "2026.8.12", "pqa", "Apache-2.0"),
}

LANGDETECT = ("langdetect", "1.0.9", "cbc1fef89f8d062739774bd51eda3da3274006b3661d199c2655f6b3f6d605a0")
SOURCE_WHEELS = {
    "ldr": [
        ("wikipedia", "1.4.0", "db0fad1829fdd441b1852306e9856398204dc0786d2996dd2e0c8bb8e26133b2"),
        ("google-search-results", "2.4.2", "603a30ecae2af8e600b22635757a6df275dad4b934f975e67878ccd640b78245"),
        ("jstyleson", "0.0.2", "680003f3b15a2959e4e6a351f3b858e3c07dd3e073a0d54954e34d8ea5e1308e"),
        LANGDETECT,
    ],
    "gptr": [
        ("docopt", "0.6.2", "49b3a825280bd66b3aa83585ef59c4a8c82f2c8a522dbe754a8bc8d08c85c491"),
        ("sgmllib3k", "1.0.0", "7868fb1c8bfa764c1ac563d3cf369c381d1325d36124933a726f29fcdaa812e9"),
        LANGDETECT,
    ],
}


def build_source_wheel(uv, destination, wheels, source_spec):
    name, version, expected = source_spec
    info = json.loads(fetch(f"https://pypi.org/pypi/{name}/{version}/json"))
    source = next(row for row in info["urls"] if row["digests"]["sha256"] == expected and row["packagetype"] == "sdist")
    archive = destination / source["filename"]
    archive.write_bytes(fetch(source["url"]))
    if sha(archive) != expected:
        raise ValueError(f"Source checksum failed for {name}")
    wheels.mkdir(exist_ok=True)
    subprocess.run([uv, "build", "--wheel", "--out-dir", wheels, archive], check=True)
    return {"distribution": f"{name}=={version}", "sha256": expected, "url": source["url"]}


def fetch(url):
    with urlopen(Request(url, headers={"User-Agent": "research-agent-hub-qualification"}), timeout=45) as response:
        return response.read()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(uv, destination, apps):
    destination.mkdir(parents=True, exist_ok=False)
    os_id = {"linux": "ubuntu", "win32": "windows", "darwin": "macos"}[sys.platform]
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    rows = []
    for app in apps:
        name, package, pin, entry, license_name = APPS[app]
        artifacts = []
        wheels = destination / (app + "-wheels")
        source_wheels = [build_source_wheel(uv, destination, wheels, spec) for spec in SOURCE_WHEELS.get(app, [])]
        if app == "gptr":
            repository = "assafelovic/gpt-researcher"
            commit = json.loads(fetch("https://api.github.com/repos/" + repository + "/commits/v" + pin))["sha"]
            filename = "gptr-source.tar.gz"
            url = "https://codeload.github.com/" + repository + "/tar.gz/" + commit
            archive = destination / filename
            archive.write_bytes(fetch(url))
            artifacts.append({"file": filename, "sha256": sha(archive), "source_ref": commit, "url": url, "type": "tar.gz"})
        requirement = destination / (app + ".in")
        requirement.write_text(name + "==" + package + "\n" +
                               ("fhlmi==0.46.2\n" if app == "pqa" else ""), encoding="utf-8")
        lock = destination / (app + ".lock")
        extra_options = []
        if source_wheels:
            extra_options += ["--find-links", wheels]
        if app == "ldr" and os_id != "macos":
            extra_options += ["--torch-backend", "cpu"]
        subprocess.run([uv, "pip", "compile", requirement, "--python", sys.executable, "--generate-hashes",
                        "--only-binary", ":all:", "--default-index", "https://pypi.org/simple", "-q", "-o", lock,
                        *extra_options], check=True)
        requirement.unlink()
        rows.append({
            "app_id": app, "platform": os_id, "arch": arch, "upstream_version": pin,
            "python_build": platform.python_version(), "artifacts": artifacts, "lock_path": lock.name,
            "lock_sha256": sha(lock), "licence": license_name, "notices": [name + " upstream distribution"],
            "prerequisites": ["Managed Python 3.12", "Platform native libraries listed in handover.md"],
            "essential": [name + "==" + package, "hash-locked Python runtime dependencies"],
            "extras": ["Optional upstream providers, services and extended document formats are configured separately."],
            "estimated_bytes": None, "adapter_id": app, "native_entry": entry,
            "launch_kind": "browser" if app == "ldr" else "cli", "settings_capabilities": [],
            "smoke_id": app + "-installed-entry", "qualification_only": True,
            **({"wheelhouse": wheels.name, "allow_registry": True, "built_sources": source_wheels} if source_wheels else {}),
            **({"torch_backend": "cpu"} if app == "ldr" and os_id != "macos" else {}),
        })
    (destination / "routes.json").write_text(json.dumps({"schema_version": 1, "routes": rows}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--app", choices=APPS, action="append")
    args = parser.parse_args()
    generate(args.uv, args.bundle, args.app or list(APPS))
