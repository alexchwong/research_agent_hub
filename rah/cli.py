import argparse
from base64 import b64encode
import hashlib
import hmac
import json
import os
from pathlib import Path
from importlib import resources
from secrets import token_hex
import shlex
import signal
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


def launch_workspace(record):
    try:
        opened = webbrowser.open(f"http://127.0.0.1:{record['port']}/#{record['token']}")
    except (webbrowser.Error, OSError):
        opened = False
    if not opened:
        raise Problem("browser", "The hub started, but a default browser could not be opened.")


def serve(root, *, launch_browser=False, foreground=False):
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
        record = {"port": int(server.effective_port), "pid": os.getpid(), "identity": identity,
                  "token": token, "build": build_identity()}
        runner = Thread(target=server.run, daemon=True)
        handlers = {}
        interrupted = None

        def stop_from_console(signum, frame):
            nonlocal interrupted
            interrupted = signum
            raise KeyboardInterrupt

        try:
            if foreground:
                for name in ("SIGINT", "SIGHUP", "SIGTERM", "SIGBREAK"):
                    if sig := getattr(signal, name, None):
                        handlers[sig] = signal.signal(sig, stop_from_console)
            write_json(connection, record)
            runner.start()
            if foreground:
                print("Research Agent Hub is running. To quit, press Ctrl+C or close this console window.", flush=True)
            if launch_browser:
                try:
                    launch_workspace(record)
                except Problem as error:
                    print(str(error) + " Run rah open to retry.", file=sys.stderr, flush=True)
            while runner.is_alive() and not quit_event.wait(0.5):
                pass
        except KeyboardInterrupt:
            if interrupted is None:
                raise
        finally:
            try:
                server.close()
            finally:
                connection.unlink(missing_ok=True)
                for sig, previous in handlers.items():
                    signal.signal(sig, previous)
                if interrupted is not None:
                    # Console shutdown must not wait for active executor jobs or the UI's idle check.
                    os._exit(128 + interrupted)


def open_hub(root, launch_browser=True, *, foreground=False):
    record = connected(root)
    if not record and foreground:
        return serve(root, launch_browser=launch_browser, foreground=True)
    if not record:
        options = {"creationflags": subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
        transport = {key: value for key, value in os.environ.items()
                     if key in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        desktop = {key: value for key, value in os.environ.items() if key in {
            "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
            "XDG_CURRENT_DESKTOP", "DESKTOP_SESSION",
        }}
        subprocess.Popen([sys.executable, "-m", "rah.cli", "--data-dir", root, "serve"],
                         env=clean_env() | transport | desktop, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **options)
        deadline = monotonic() + 20
        while monotonic() < deadline and not (record := connected(root)):
            sleep(0.2)
    if not record:
        raise Problem("startup", "The hub did not start. Run rah serve in a terminal for local diagnostics.")
    if launch_browser:
        launch_workspace(record)
    if foreground:
        print("Research Agent Hub is already running. To quit it, press Ctrl+C or close its original console.", flush=True)
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
$ErrorActionPreference = 'Stop'
$shell = [Activator]::CreateInstance([Type]::GetTypeFromProgID('WScript.Shell', $true))
$link = $shell.CreateShortcut($env:RAH_SHORTCUT)
$link.TargetPath = $env:RAH_TARGET
$link.Arguments = $env:RAH_ARGUMENTS
$link.Description = 'Open Research Agent Hub'
$link.Save()
"""
        env = clean_env() | {"RAH_SHORTCUT": str(path), "RAH_TARGET": sys.executable,
                             "RAH_ARGUMENTS": subprocess.list2cmdline(
                                 ["-I", "-m", "rah.cli", "--data-dir", str(root), "open", "--foreground"])}
        command = b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                           env=env, stdin=subprocess.DEVNULL, check=True, timeout=15)
        except subprocess.TimeoutExpired as error:
            raise Problem("shortcut", "Windows shortcut creation timed out after 15 seconds.") from error
        except (OSError, subprocess.CalledProcessError) as error:
            raise Problem("shortcut", "Windows could not create the hub shortcut.") from error
        if not path.is_file():
            raise Problem("shortcut", "Windows did not save the hub shortcut.")
    elif sys.platform == "darwin":
        import plistlib
        bundle = path / "Contents"
        executable = bundle / "MacOS/rah-open"
        executable.parent.mkdir(parents=True, exist_ok=True)
        atomic(bundle / "Info.plist", plistlib.dumps({"CFBundleName": "Research Agent Hub",
               "CFBundleIdentifier": "org.researchagenthub.desktop", "CFBundleExecutable": "rah-open",
               "CFBundlePackageType": "APPL"}))
        command = bundle / "Resources/Research Agent Hub.command"
        command.parent.mkdir(parents=True, exist_ok=True)
        line = shlex.join([str(target), "--data-dir", str(root), "open", "--foreground"])
        atomic(command, ("#!/bin/sh\nexec " + line + "\n").encode())
        command.chmod(0o700)
        atomic(executable, ("#!/bin/sh\nexec " + shlex.join(["/usr/bin/open", "-a", "Terminal", str(command)]) + "\n").encode())
        executable.chmod(0o700)
    else:
        arguments = " ".join(desktop_argument(value) for value in (target, "--data-dir", root, "open", "--foreground"))
        atomic(path, ("[Desktop Entry]\nType=Application\nName=Research Agent Hub\nExec=" + arguments + "\nTerminal=true\n").encode())
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
            raise Problem("setup_running", "Stop the running hub before applying setup. Press Ctrl+C or close its console, "
                          "or use Quit hub in App details for an older launch.")
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
    if not complete:
        print("Retry the affected app in App details or stop the hub and rerun rah setup.", file=sys.stderr, flush=True)
    try:
        open_hub(root, not args.no_browser, foreground=args.foreground)
    except Problem as error:
        if error.code != "browser":
            if not complete:
                print("Run rah setup again to retry the affected apps.", file=sys.stderr)
            raise
        print("Use the Research Agent Hub shortcut or run rah open to open the workspace.", file=sys.stderr)
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
    setup.add_argument("--foreground", action="store_true")
    commands.add_parser("serve")
    opened = commands.add_parser("open")
    opened.add_argument("--no-browser", action="store_true")
    opened.add_argument("--foreground", action="store_true")
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
            open_hub(root, not args.no_browser, foreground=args.foreground)
        elif args.action == "serve":
            serve(root, foreground=True)
        else:
            remove_hub(root, not args.no_shortcut)
    except KeyboardInterrupt:
        return 130
    except (Problem, OSError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, Problem) else "The local command could not complete. Check setup and permissions.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
