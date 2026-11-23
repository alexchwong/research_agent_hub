import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
from importlib import resources
from secrets import token_hex
import shlex
import subprocess
import sys
from threading import Event, Lock, Thread
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler
import webbrowser

from waitress import create_server

from . import settings, setup as provision
from .common import APPS, Problem, atomic, child, clean_env, data_root, digest, exclusive, prepare_root, read_json, write_json
from .installer import RouteBook
from .jobs import Job
from .openrouter import NoRedirect
from .web import create_app


def runtime(root, name):
    return child(root, "hub", "runtime", name)


def configuration(root):
    return read_json(runtime(root, "setup.json"), {})


def connected(root):
    try:
        record = read_json(runtime(root, "connection.json"))
        if not record or type(record["port"]) is not int or not 0 < record["port"] < 65536:
            return None
        nonce = token_hex(32)
        opener = build_opener(ProxyHandler({}), NoRedirect())
        url = f"http://127.0.0.1:{record['port']}/_identity?nonce={nonce}"
        with opener.open(url, timeout=1) as response:
            actual = json.loads(response.read(1024))["signature"]
        expected = hmac.new(record["identity"].encode(), nonce.encode(), hashlib.sha256).hexdigest()
        if isinstance(actual, str) and hmac.compare_digest(actual, expected):
            return record
    except (OSError, URLError, ValueError, KeyError, TypeError):
        pass
    return None


def build_identity():
    package = resources.files("rah")
    files = ("cli.py", "web.py", "setup.py", "install_assets/routes.json")
    return digest(b"".join(package.joinpath(name).read_bytes() for name in files))


def serve(root):
    setup = configuration(root)
    book = RouteBook.load(Path(setup["bundle"]) if setup.get("bundle") else None, setup.get("qualification", False))
    with exclusive(runtime(root, "instance.lock")):
        identity, token = token_hex(32), token_hex(32)
        quit_event = Event()
        address = [None]
        app = create_app(root, book, Path(setup["uv"]) if setup.get("uv") else None,
                         identity, token, quit_event.set, lambda: address[0])
        server = create_server(app, host="127.0.0.1", port=0, threads=6,
                               max_request_body_size=257 * 1024 * 1024, clear_untrusted_proxy_headers=True)
        address[0] = f"127.0.0.1:{server.effective_port}"
        connection = runtime(root, "connection.json")
        write_json(connection, {"port": int(server.effective_port), "pid": os.getpid(), "identity": identity, "token": token, "build": build_identity()})
        runner = Thread(target=server.run, daemon=True)
        runner.start()
        try:
            while runner.is_alive() and not quit_event.wait(0.5):
                pass
        finally:
            server.close()
            connection.unlink(missing_ok=True)


def open_hub(root, launch_browser=True):
    record = connected(root)
    if not record:
        options = {"creationflags": subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
        transport = {key: value for key, value in os.environ.items()
                     if key in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        subprocess.Popen([sys.executable, "-m", "rah.cli", "--data-dir", root, "serve"],
                         env=clean_env() | transport, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **options)
        deadline = monotonic() + 20
        while monotonic() < deadline and not (record := connected(root)):
            sleep(0.2)
    if not record:
        raise Problem("startup", "The hub did not start. Run rah serve in a terminal for local diagnostics.")
    if launch_browser:
        try:
            opened = webbrowser.open(f"http://127.0.0.1:{record['port']}/#{record['token']}")
        except (webbrowser.Error, OSError):
            opened = False
        if not opened:
            raise Problem("browser", "The hub started, but a default browser could not be opened.")
    return record


def shortcut_path():
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Research Agent Hub.lnk"
    if sys.platform == "darwin":
        return Path.home() / "Applications/Research Agent Hub.app"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "applications/research-agent-hub.desktop"


def desktop_argument(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`").replace("%", "%%") + '"'


def register_shortcut(root):
    path = shortcut_path()
    target = Path(sys.executable).parent / ("rah.exe" if os.name == "nt" else "rah")
    if not target.is_file():
        raise Problem("shortcut", "Install the RAH wheel and run its entrypoint before registering a shortcut.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        script = """
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:RAH_SHORTCUT)
$link.TargetPath = $env:RAH_TARGET
$link.Arguments = $env:RAH_ARGUMENTS
$link.Description = 'Open Research Agent Hub'
$link.Save()
"""
        env = clean_env() | {"RAH_SHORTCUT": str(path), "RAH_TARGET": str(target),
                             "RAH_ARGUMENTS": subprocess.list2cmdline(["--data-dir", str(root), "open"])}
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, timeout=15)
    elif sys.platform == "darwin":
        import plistlib
        bundle = path / "Contents"
        executable = bundle / "MacOS/rah-open"
        executable.parent.mkdir(parents=True, exist_ok=True)
        atomic(bundle / "Info.plist", plistlib.dumps({"CFBundleName": "Research Agent Hub",
               "CFBundleIdentifier": "org.researchagenthub.desktop", "CFBundleExecutable": "rah-open",
               "CFBundlePackageType": "APPL"}))
        line = shlex.join([str(target), "--data-dir", str(root), "open"])
        atomic(executable, ("#!/bin/sh\nexec " + line + "\n").encode())
        executable.chmod(0o700)
    else:
        arguments = " ".join(desktop_argument(value) for value in (target, "--data-dir", root, "open"))
        atomic(path, ("[Desktop Entry]\nType=Application\nName=Research Agent Hub\nExec=" + arguments + "\nTerminal=false\n").encode())
        path.chmod(0o700)
    return path


def remove_hub(root, shortcut=True):
    if record := connected(root):
        from urllib.request import Request
        origin = f"http://127.0.0.1:{record['port']}"
        request = Request(origin + "/api/quit", data=b"{}", headers={
            "Authorization": "Bearer " + record["token"], "Origin": origin, "Content-Type": "application/json",
        })
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=3):
                pass
        except URLError as error:
            raise Problem("busy", "Quit the idle hub before removing its registration.", 409) from error
        deadline = monotonic() + 5
        while connected(root) and monotonic() < deadline:
            sleep(0.2)
        if connected(root):
            raise Problem("busy", "The hub is still shutting down.", 409)
    with exclusive(runtime(root, "instance.lock")):
        if shortcut:
            path = shortcut_path()
            if sys.platform == "darwin":
                import shutil
                plist = path / "Contents/Info.plist"
                if plist.is_file():
                    import plistlib
                    if plistlib.loads(plist.read_bytes()).get("CFBundleIdentifier") == "org.researchagenthub.desktop":
                        shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        for name in ("setup.json", "connection.json"):
            runtime(root, name).unlink(missing_ok=True)
    print("Retained apps, native profiles and credentials, project files, settings, and installed Python.")
    print(root)


class SetupProgress(Job):
    def report(self, name, state, message):
        super().report(name, state, message)
        if state != "Queued":
            print(f"{APPS[name]} · {state}: {message}", flush=True)


def setup_core(root, args):
    previous = configuration(root)
    uv = args.uv or (Path(previous["uv"]) if previous.get("uv") else None)
    if not uv or not uv.is_absolute() or not uv.is_file() or not os.access(uv, os.X_OK):
        raise Problem("uv_missing", "Pass the absolute path of the approved uv executable.")
    bundle = args.bundle.resolve() if args.bundle else previous.get("bundle")
    qualification = args.qualification if args.qualification is not None else previous.get("qualification", False)
    requested = {"uv": str(uv), "bundle": str(bundle) if bundle else None, "qualification": qualification}
    book = RouteBook.load(Path(bundle) if bundle else None, qualification)
    print(f"Set up Research Agent Hub, Local Deep Research, GPT Researcher and PaperQA2 in {root}.", flush=True)
    if running := connected(root):
        if requested != previous or running.get("build") != build_identity():
            raise Problem("setup_running", "Quit the running hub before applying the requested setup configuration.")
        if not all(provision.current(root, book, name) for name in APPS):
            raise Problem("setup_running", "Open App details in the running hub to retry the missing installation, or quit and rerun rah setup.")
        complete = True
        if not args.no_shortcut:
            register_shortcut(root)
    else:
        with exclusive(runtime(root, "instance.lock")):
            settings.seed(root)
            write_json(runtime(root, "setup.json"), requested)
            complete = provision.install_core(root, book, uv, Lock(), SetupProgress("setup", "core"))["complete"]
            if not args.no_shortcut:
                register_shortcut(root)
    try:
        open_hub(root, not args.no_browser)
    except Problem as error:
        if error.code != "browser":
            if not complete:
                print("Run rah setup again to retry the affected apps.", file=sys.stderr)
            raise
        print("Use the Research Agent Hub shortcut or run rah open to open the workspace.", file=sys.stderr)
    if not complete:
        print("Retry the affected app in App details or quit the hub and rerun rah setup.", file=sys.stderr)
    return 0 if complete else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rah")
    parser.add_argument("--data-dir", type=Path, default=None)
    commands = parser.add_subparsers(dest="action", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--uv", type=Path)
    setup.add_argument("--bundle", type=Path)
    setup.add_argument("--qualification", action="store_true", default=None)
    setup.add_argument("--no-shortcut", action="store_true")
    setup.add_argument("--no-browser", action="store_true")
    commands.add_parser("serve")
    opened = commands.add_parser("open")
    opened.add_argument("--no-browser", action="store_true")
    removed = commands.add_parser("remove-hub")
    removed.add_argument("--no-shortcut", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "nt":
        os.umask(0o077)
    try:
        root = prepare_root(args.data_dir or data_root())
        if args.action == "setup":
            return setup_core(root, args)
        elif args.action == "open":
            open_hub(root, not args.no_browser)
        elif args.action == "serve":
            serve(root)
        else:
            remove_hub(root, not args.no_shortcut)
    except (Problem, OSError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, Problem) else "The local command could not complete. Check setup and permissions.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
