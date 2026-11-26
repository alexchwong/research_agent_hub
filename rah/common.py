from base64 import b64encode
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from uuid import UUID


class Problem(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


APPS = {"ldr": "Local Deep Research", "gptr": "GPT Researcher", "pqa": "PaperQA2"}
ENV_NAMES = {
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT",
    "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
    "PATHEXT", "COMSPEC", "USER", "USERNAME", "HOMEDRIVE", "HOMEPATH",
}
NO_PROXY_ENV = {"NO_PROXY": "*", "PYTHON_DOTENV_DISABLED": "1", "PYTHONNOUSERSITE": "1"}


def clean_env():
    return {key: value for key, value in os.environ.items() if key.upper() in ENV_NAMES} | NO_PROXY_ENV


def data_root():
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "Research Agent Hub"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Research Agent Hub"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "research-agent-hub"


def id_value(value):
    try:
        checked = str(UUID(value))
        if checked == value:
            return checked
    except (ValueError, TypeError, AttributeError):
        pass
    raise Problem("unknown_id", "Unknown resource.", 404)


def is_link(path):
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def child(root, *parts):
    root = Path(root)
    path = root
    for part in parts:
        if Path(part).name != part or part in {"", ".", ".."} or ":" in part or "\\" in part:
            raise Problem("bad_path", "Invalid managed path.")
        path = path / part
        if (path.exists() or path.is_symlink()) and is_link(path):
            raise Problem("bad_path", "Managed files must not be links.")
    return path


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if is_link(path) or not path.is_dir():
        raise Problem("permissions", "Select a private directory without filesystem redirects.")
    if os.name != "nt":
        metadata = path.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise Problem("permissions", "Managed directories must be accessible only to your account.")
    return path


_WINDOWS_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$sections = [System.Security.AccessControl.AccessControlSections]::Owner -bor `
    [System.Security.AccessControl.AccessControlSections]::Access
$a = [System.IO.Directory]::GetAccessControl($env:RAH_ACL_PATH, $sections)
$u = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($u, 'S-1-5-18', 'S-1-5-32-544')
if ($a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -notin $allowed) { exit 3 }
# CPython mkdir(0700) grants Owner Rights. This SID applies only to the verified owner.
$allowed += 'S-1-3-4'
foreach ($r in $a.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
    if ($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Value -notin $allowed) { exit 3 }
}
[Console]::WriteLine('RAH_ACL_OK')
"""


def verify_windows_root(path):
    if os.name != "nt":
        return
    # Avoid passing multiline PowerShell source through Windows command-line quoting.
    command = b64encode(_WINDOWS_ACL_SCRIPT.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
            env=clean_env() | {"RAH_ACL_PATH": str(path)}, timeout=15,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise Problem("permissions", "Windows directory ACL verification timed out after 15 seconds.") from error
    except OSError as error:
        raise Problem("permissions", "Windows could not verify the managed directory ACL.") from error
    if result.returncode == 3:
        raise Problem("permissions", "The managed Windows directory has an unverified or broad ACL.")
    if result.returncode or result.stdout.strip() != b"RAH_ACL_OK":
        raise Problem("permissions", "Windows could not verify the managed directory ACL.")


def prepare_root(root):
    root = root.absolute()
    for parent in reversed([root, *root.parents]):
        if (parent.exists() or parent.is_symlink()) and is_link(parent):
            raise Problem("permissions", "Use a data path without filesystem redirects.")
    private_directory(root)
    verify_windows_root(root)
    for parts in [("hub",), ("hub", "runtime"), ("hub", "config"), ("hub", "runs"), ("apps",), ("projects",)]:
        path = private_directory(child(root, *parts))
        verify_windows_root(path)
    return root


def atomic(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def canonical(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def write_json(path, value):
    atomic(path, canonical(value))


def read_json(path, default=None):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_digest(path):
    if is_link(path) or not path.is_file():
        raise Problem("bad_path", "Expected a regular managed file.")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def venv_python(path):
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def executable(path, name):
    return path / ("Scripts" if os.name == "nt" else "bin") / (name + ".exe" if os.name == "nt" else name)


@contextmanager
def exclusive(path):
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise Problem("busy", "Another operation owns this resource.", 409) from error
        yield
