from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler
from uuid import uuid4

from .common import APPS, Problem, child, clean_env, digest, executable, file_digest, private_directory, read_json, venv_python, write_json


PINS = {"ldr": "2.0.1", "gptr": "3.8.0", "pqa": "2026.8.12"}
ENTRIES = {"ldr": "ldr-web", "gptr": "cli.py", "pqa": "pqa"}
HOST = {"darwin": "macos", "win32": "windows", "linux": "ubuntu"}
SOURCE_HOSTS = {"github.com", "api.github.com", "codeload.github.com", "files.pythonhosted.org"}


def host():
    machine = platform.machine().lower()
    return HOST.get(sys.platform, sys.platform), {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)


def host_identity():
    os_id, arch = host()
    if sys.platform == "linux":
        release = platform.freedesktop_os_release()
        version = release.get("ID", "") + ":" + release.get("VERSION_ID", "")
    elif sys.platform == "darwin":
        version = platform.mac_ver()[0]
    else:
        version = platform.version()
    return {"platform": os_id, "arch": arch, "version": version}


@dataclass
class RouteBook:
    document: dict
    directory: Path | None = None
    qualification: bool = False
    custom: bool = False

    @classmethod
    def load(cls, directory=None, qualification=False):
        custom = directory is not None
        if directory is None:
            directory = Path(str(resources.files("rah").joinpath("install_assets")))
        directory = directory.resolve(strict=True)
        book = cls(read_json(child(directory, "routes.json")), directory, qualification, custom)
        if not isinstance(book.document, dict) or book.document.get("schema_version") != 1:
            raise Problem("invalid_manifest", "Expected a version 1 installation manifest.")
        return book

    def route(self, app):
        if app not in APPS:
            raise Problem("unknown_app", "Unknown application.", 404)
        os_id, arch = host()
        matches = [row for row in self.document["routes"] if (row["app_id"], row["platform"], row["arch"]) == (app, os_id, arch)]
        if len(matches) > 1:
            raise Problem("invalid_manifest", "The manifest has duplicate host routes.", 503)
        if not matches:
            return None
        row = matches[0]
        if row.get("qualification_only") and not self.qualification:
            return None
        if row["upstream_version"] != PINS[app] or row["native_entry"] != ENTRIES[app]:
            raise Problem("invalid_manifest", "The route does not match the upstream pin.", 503)
        return row

    def capabilities(self, app):
        return (self.route(app) or {}).get("settings_capabilities", [])


def filtered_run(args, cwd, env=None, timeout=900, stdin=None):
    try:
        result = subprocess.run(args, cwd=cwd, env=env if env is not None else clean_env(), input=stdin,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Problem("native_launch_failed", "The native command could not start or did not complete.", 503) from error
    if result.returncode:
        raise Problem("native_command_failed", "The native command failed. Check the installed route and native configuration.", 503)
    return result.stdout


def fetch_to(item, target, bundle):
    if "file" in item:
        source = child(bundle, item["file"])
        if file_digest(source) != item["sha256"]:
            raise Problem("checksum", "Bundled artifact checksum did not match.", 503)
        shutil.copyfile(source, target)
        return
    url = item["url"]
    if urlparse(url).scheme != "https" or urlparse(url).hostname not in SOURCE_HOSTS:
        raise Problem("invalid_manifest", "Unrecognized upstream artifact origin.", 503)
    with build_opener(ProxyHandler({})).open(url, timeout=45) as response:
        actual = urlparse(response.geturl())
        if actual.scheme != "https" or actual.hostname not in SOURCE_HOSTS:
            raise Problem("invalid_manifest", "Unrecognized upstream redirect.", 503)
        with target.open("wb") as output:
            shutil.copyfileobj(response, output)
    if file_digest(target) != item["sha256"]:
        raise Problem("checksum", "Downloaded artifact checksum did not match.", 503)


def untar(archive, target):
    roots = set()
    with tarfile.open(archive, mode="r:*") as source:
        for entry in source.getmembers():
            path = PurePosixPath(entry.name)
            if (path.is_absolute() or not path.parts or ".." in path.parts or
                    "\\" in entry.name or ":" in entry.name or not (entry.isfile() or entry.isdir())):
                raise Problem("bad_archive", "Source archive contains an unsafe entry.", 503)
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise Problem("bad_archive", "Source archive needs exactly one root.", 503)
        source.extractall(target, filter="data")
    return child(target, roots.pop())


def install_location(root, app):
    record = read_json(child(root, "apps", app, "current.json"))
    if not record:
        return None
    from .common import id_value
    directory = child(root, "apps", app, "versions", id_value(record["install_id"]))
    if not venv_python(directory / "venv").is_file():
        return None
    return record, directory


def install(root, book, app, uv, checkpoint, prepare=None, step=lambda _message: None):
    route = book.route(app)
    if not route or book.directory is None:
        raise Problem("route_unavailable", "No qualified application route is installed for this host.", 503)
    bundle = book.directory
    lock = child(bundle, route["lock_path"])
    if file_digest(lock) != route["lock_sha256"]:
        raise Problem("checksum", "Dependency lock checksum did not match.", 503)
    base = private_directory(child(root, "apps", app))
    versions = private_directory(child(base, "versions"))
    private_directory(child(base, "native"))
    install_id = str(uuid4())
    directory = private_directory(child(versions, install_id))
    promoted = False
    try:
        venv = directory / "venv"
        python = venv_python(venv)
        # Use the already verified hub interpreter. The native environments outlive the hub venv.
        base_python = Path(sys._base_executable)
        if platform.python_version() != route["python_build"]:
            raise Problem("python_pin", "The manifest requires a different Python patch version.", 503)
        step("Creating the independent environment")
        checkpoint()
        filtered_run([uv, "venv", "--python", base_python, venv], directory)
        packages = [uv, "pip", "sync", "--python", python, "--require-hashes", "--only-binary", ":all:"]
        if route.get("wheelhouse"):
            packages += ["--find-links", child(bundle, route["wheelhouse"])]
            if not route.get("allow_registry"):
                packages += ["--no-index"]
        if route.get("torch_backend") == "cpu":
            packages += ["--torch-backend", "cpu"]
        packages += ["--default-index", "https://pypi.org/simple", "--no-config"]
        # TLS/proxy configuration is available only to the package installer.
        transport = {key: value for key, value in os.environ.items()
                     if key in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        package_env = clean_env() | {"NO_PROXY": ""} | transport
        step("Installing locked dependencies")
        checkpoint()
        try:
            filtered_run(packages + [lock], directory, package_env)
        except Problem as error:
            raise Problem("dependency_install", "Could not install the locked dependencies. See docs/releases.md.", 503) from error
        step("Verifying native assets")
        checkpoint()
        for item in route["artifacts"]:
            archive = child(directory, "upstream.tar.gz")
            fetch_to(item, archive, bundle)
            source = untar(archive, directory)
            source.rename(directory / "source")
            archive.unlink()
        checkpoint()
        smoke_cmd = [python, directory / "source/cli.py"] if app == "gptr" else [executable(venv, ENTRIES[app])]
        smoke_env = clean_env()
        if app == "ldr":
            # Import smoke keeps the real server from opening a listener during installation.
            smoke_cmd = [python, "-c", "from local_deep_research.web.app import main; assert callable(main)"]
            smoke_env["LDR_DATA_DIR"] = str(directory / "smoke-ldr")
        if app == "pqa":
            smoke_env.update(PQA_HOME=str(directory / "smoke-pqa"), LITELLM_LOCAL_MODEL_COST_MAP="True")
        step("Checking the installed native entry")
        try:
            filtered_run(smoke_cmd + ([] if app == "ldr" else ["--help"]), directory, smoke_env, timeout=120)
        except Problem as error:
            raise Problem("entry_smoke", "Native entry point failed its installed smoke check. See docs/releases.md.", 503) from error
        step("Preparing the standalone launcher and settings")

        def publish():
            checkpoint(final=True)
            write_json(child(base, "current.json"), {
                "install_id": install_id, "upstream_version": route["upstream_version"],
                "lock_sha256": route["lock_sha256"],
            })

        if prepare:
            prepare(directory, publish)
        else:
            publish()
        promoted = True
        return str(directory)
    finally:
        if not promoted:
            shutil.rmtree(directory, ignore_errors=True)


def plan_digest(route):
    from .common import canonical
    return digest(canonical(route))
