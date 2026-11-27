"""Own the disposable POSIX dev install, not the normal RAH workspace."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
from tempfile import mkdtemp


REPO = Path(__file__).resolve().parents[1]
PYTHON_PIN = "3.12.14"
UV_PIN = "0.13.9"
BUILD_NAME = re.compile(r"dev\.[a-zA-Z0-9_]{8}")


class SetupError(Exception):
    pass


def directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode) or path.stat().st_uid != os.getuid():
        raise SetupError(f"Expected a real directory owned by you: {path}")
    return path


@contextmanager
def lock(path, message, *, create=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise SetupError(f"Expected a private lock file: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupError(message) from error
        yield fd
    finally:
        os.close(fd)


def run(*args, cwd=REPO):
    subprocess.run(args, cwd=cwd, check=True)


def version(*args):
    return subprocess.check_output(args, text=True).strip()


def toolchain(repo):
    env = repo / ".venv"
    python, uv = env / "bin/python", env / "bin/uv"
    if not os.path.lexists(env):
        run(sys.executable, "-I", "-m", "venv", env, cwd=repo)
    if version(python, "-I", "-c", "import platform; print(platform.python_version())") != PYTHON_PIN:
        raise SetupError(f"Existing .venv must use Python {PYTHON_PIN}.")
    if subprocess.run([python, "-I", "-m", "pip", "--version"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        run(python, "-I", "-m", "ensurepip", cwd=repo)
    if not os.access(uv, os.X_OK) or version(uv, "--version").split()[:2] != ["uv", UV_PIN]:
        run(python, "-I", "-m", "pip", "install", "--only-binary=:all:", f"uv=={UV_PIN}", cwd=repo)
    run(uv, "pip", "sync", "--python", python, "--require-hashes", "--only-binary=:all:",
        "dev-deps.lock", cwd=repo)
    return python, uv


def current_build(release):
    current = release / "current"
    if not os.path.lexists(current):
        return None
    if not current.is_symlink() or not BUILD_NAME.fullmatch(target := os.readlink(current)):
        raise SetupError(f"Unexpected dev launcher target: {current}")
    return release / target


def cleanup_targets(release):
    current_build(release)
    targets = [path for path in release.iterdir()
               if path.name == "manual-data" or BUILD_NAME.fullmatch(path.name)]
    for path in targets:
        directory(path)
    return targets


def check_idle(release, targets):
    """Check pre-lock launchers and independently started native processes."""
    data = release / "manual-data"
    runtime = data / "hub/runtime"
    if data in targets and os.path.lexists(data / "hub"):
        directory(data / "hub")
        if os.path.lexists(runtime):
            directory(runtime)
            instance = runtime / "instance.lock"
            if os.path.lexists(instance):
                with lock(instance, "Quit the running dev hub before reinstalling."):
                    pass
    if not targets:
        return
    # Native entry points use absolute arguments under the dev data/build roots.
    # Also check cwd on Linux for children that replace their command line.
    roots = tuple(f"{path}/" for path in targets)
    for line in version("ps", "-axww", "-o", "pid=", "-o", "command=").splitlines():
        pid, _, command = line.strip().partition(" ")
        if not pid.isdecimal() or int(pid) == os.getpid():
            continue
        cwd = ""
        if sys.platform == "linux":
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd").rstrip("/") + "/"
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                pass
        if any(root in command or cwd.startswith(root) for root in roots):
            raise SetupError(f"Stop the running dev hub or native app (PID {pid}) before reinstalling.")


def prepare(repo, build):
    python, uv = toolchain(repo)
    run(python, "-I", repo / "scripts/bundle_hub.py", "--system-python", "--uv", uv,
        "--bundle", build / "bootstrap", "--venv", build / "installed-hub", cwd=repo)
    run(build / "installed-hub/bin/python", "-I", "-m", "rah.cli", "--help", cwd=build)
    launcher = build / "launcher"
    launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(str(python))} -I "
                        f"{shlex.quote(str(repo / 'scripts/setup_dev.py'))} launch \"$@\"\n")
    launcher.chmod(0o700)
    (build / "current-link").symlink_to(build.name)


def install(repo, release):
    targets = cleanup_targets(release)
    check_idle(release, targets)
    bin_dir = directory(release / "bin", create=True)
    build = Path(mkdtemp(prefix="dev.", dir=release))
    try:
        prepare(repo, build)
        # Recheck immediately before any deletion (legacy launchers do not take our lock).
        targets = [target for target in cleanup_targets(release) if target != build]
        check_idle(release, targets)
    except (OSError, subprocess.CalledProcessError) as error:
        shutil.rmtree(build)
        raise SetupError(f"Could not prepare the replacement; existing dev data was not changed: {error}") from error
    except BaseException:
        shutil.rmtree(build)
        raise

    try:
        (release / "current").unlink(missing_ok=True)
        (bin_dir / "rah").unlink(missing_ok=True)
        for target in targets:
            shutil.rmtree(target)
        (build / "launcher").replace(bin_dir / "rah")
        (build / "current-link").replace(release / "current")
    except OSError as error:
        raise SetupError(f"Dev reset did not complete; rerun setup after fixing: {error}") from error
    print("Installed a fresh dev wheel. Prior dev workspace data was not retained.")
    print("Run bash scripts/setup-dev.sh --verify, or start with ./.release-test/bin/rah.")


def installed_build(release):
    build = current_build(release)
    if build is None or not os.path.lexists(build):
        raise SetupError("Dev install missing. Run bash scripts/setup-dev.sh first.")
    directory(build)
    if not all(os.access(build / f"installed-hub/bin/{name}", os.X_OK) for name in ("python", "rah")):
        raise SetupError("Dev install incomplete. Run bash scripts/setup-dev.sh first.")
    return build


def verify(repo, build):
    python, uv = repo / ".venv/bin/python", repo / ".venv/bin/uv"
    installed = build / "installed-hub/bin/python"
    for executable in (python, installed):
        if version(executable, "-I", "-c", "import platform; print(platform.python_version())") != PYTHON_PIN:
            raise SetupError("The installed toolchain does not match the pins. Rerun setup.")
    if version(uv, "--version").split()[:2] != ["uv", UV_PIN]:
        raise SetupError("The installed toolchain does not match the pins. Rerun setup.")
    if not shutil.which("node"):
        raise SetupError("Node is required for JavaScript syntax checks.")
    run(python, "-m", "pytest", "-q", "-m", "not live", cwd=repo)
    for name in ("app", "workspace", "native_handoff", "guided"):
        run("node", "--check", f"rah/static/{name}.js", cwd=repo)
    run(installed, "-I", "-c", "from pathlib import Path; import sysconfig, rah; "
        "assert Path(rah.__file__).resolve().is_relative_to(sysconfig.get_path('purelib'))", cwd=build)
    for flags, report in (((), "setup"), (("--packaged",), "upstreams")):
        run(installed, "-s", repo / "tests/smoke_setup.py", "--uv", uv, *flags,
            "--report", repo / f"reports/{report}.json", cwd=build)


def main(mode, args):
    os.umask(0o077)
    if mode == "verify" and not (REPO / ".release-test").exists():
        raise SetupError("Dev install missing. Run bash scripts/setup-dev.sh first.")
    release = directory(REPO / ".release-test", create=mode == "install")
    with lock(release / ".setup-dev.lock",
              "Another dev setup, verification, or launcher is running. Quit it and retry.", create=True) as fd:
        if mode == "install":
            install(REPO, release)
        elif mode == "verify":
            verify(REPO, installed_build(release))
        elif mode == "launch":
            build = installed_build(release)
            python = build / "installed-hub/bin/python"
            os.chdir(build)
            # Hold the lifecycle lock for the entire foreground hub lifetime.
            os.set_inheritable(fd, True)
            os.execv(python, [python, "-I", "-m", "rah.cli", "--data-dir", release / "manual-data",
                              "setup", "--foreground", "--uv", REPO / ".venv/bin/uv", "--no-shortcut", *args])
        else:
            raise SetupError(f"Unknown command: {mode}")


if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2:])
    except (SetupError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"setup-dev: {error}")