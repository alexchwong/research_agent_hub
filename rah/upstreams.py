from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .common import Problem, atomic, child, clean_env, executable, private_directory, venv_python
from .installer import install_location


LDR_PORT = 58721
GPTR_PORT = 58722


@dataclass
class LaunchSpec:
    command: list
    directory: Path
    env: dict
    url: str | None = None


def current(root, app, book):
    location = install_location(root, app)
    route = book.route(app)
    if not route or not location or route["lock_sha256"] != location[0]["lock_sha256"]:
        raise Problem("app_not_installed", "Install this application at its manifest pin first.", 503)
    return location[1]


def launch_spec(root, app, install, profile, *, browser=False, identity=""):
    native = private_directory(child(root, "apps", app, "native"))
    env = clean_env() | profile["env"]
    venv = install / "venv"
    if app == "ldr":
        env.update(LDR_WEB_HOST="127.0.0.1", LDR_WEB_PORT=str(LDR_PORT), LDR_WEB_USE_HTTPS="false",
                   LDR_DATA_DIR=str(native))
        return LaunchSpec([executable(venv, "ldr-web")], native, env, f"http://127.0.0.1:{LDR_PORT}/")
    if app == "gptr":
        if browser:
            env["RAH_GPTR_STARTUP_TOKEN"] = identity
            return LaunchSpec([venv_python(venv), install / "gptr_browser.py", install / "source", str(GPTR_PORT)],
                              native, env, f"http://127.0.0.1:{GPTR_PORT}/")
        return LaunchSpec([venv_python(venv), install / "source/cli.py"], native, env)
    env["PQA_HOME"] = str(native)
    return LaunchSpec([executable(venv, "pqa"), *profile["args"]], native, env)


def standalone_path(root, app):
    launch = private_directory(child(root, "apps", app, "launch"))
    return child(launch, "native.py")


def write_standalone(root, app):
    path = standalone_path(root, app)
    atomic(path, resources.files("rah").joinpath("standalone.py").read_bytes())
    return path



def prepare_launch(root, app, install):
    if app == "gptr":
        atomic(install / "gptr_browser.py", resources.files("rah").joinpath("gptr_browser.py").read_bytes())
    return write_standalone(root, app)
