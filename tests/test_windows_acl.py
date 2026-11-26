from base64 import b64decode, b64encode
import os
import subprocess
from time import monotonic
from types import SimpleNamespace

import pytest

from rah import common


@pytest.fixture
def windows_acl_call(monkeypatch, tmp_path):
    root = tmp_path / "RAH data"
    monkeypatch.setattr(common, "os", SimpleNamespace(name="nt", environ=os.environ))

    def check(outcome):
        def run(args, **kwargs):
            assert args[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
            assert b64decode(args[4]).decode("utf-16le") == common._WINDOWS_ACL_SCRIPT
            assert kwargs["env"]["RAH_ACL_PATH"] == str(root)
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert kwargs["timeout"] == 15
            if isinstance(outcome, Exception):
                raise outcome
            return subprocess.CompletedProcess(args, *outcome)

        monkeypatch.setattr(common.subprocess, "run", run)
        common.verify_windows_root(root)

    return check


def test_acl_verifier_requires_explicit_success(windows_acl_call):
    windows_acl_call((0, b"RAH_ACL_OK\r\n"))


@pytest.mark.parametrize(("outcome", "message"), [
    ((0, b""), "could not verify"),
    ((0, b"unexpected output"), "could not verify"),
    ((1, b"RAH_ACL_OK\r\n"), "could not verify"),
    ((3, b""), "broad ACL"),
    (OSError("launch failed"), "could not verify"),
    (subprocess.TimeoutExpired("powershell.exe", 15), "timed out after 15 seconds"),
])
def test_acl_verifier_fails_closed(windows_acl_call, outcome, message):
    with pytest.raises(common.Problem, match=message) as caught:
        windows_acl_call(outcome)
    assert caught.value.code == "permissions"


def _powershell_diagnostics(root):
    env = common.clean_env() | {"RAH_ACL_PATH": str(root)}
    dotnet_acl = (
        "$sections = [System.Security.AccessControl.AccessControlSections]::Owner -bor "
        "[System.Security.AccessControl.AccessControlSections]::Access\n"
        "$a = [System.IO.Directory]::GetAccessControl($env:RAH_ACL_PATH, $sections)"
    )
    probes = [
        ("startup", ""),
        ("Get-Acl", "$null = Get-Acl -LiteralPath $env:RAH_ACL_PATH"),
        ("current user SID", "$null = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"),
        (".NET ACL read", dotnet_acl),
        ("owner SID", dotnet_acl + "\n$null = $a.GetOwner([System.Security.Principal.SecurityIdentifier])"),
        ("access-rule enumeration", dotnet_acl + "\n$null = $a.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])"),
        ("full verifier with .NET ACL read", common._WINDOWS_ACL_SCRIPT.replace(
            "$a = Get-Acl -LiteralPath $env:RAH_ACL_PATH", dotnet_acl,
        )),
    ]
    details = []
    for label, script in probes:
        start = monotonic()
        try:
            command = b64encode(("$ErrorActionPreference = 'Stop'\n" + script).encode("utf-16le")).decode("ascii")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
            )
            outcome = f"exit {result.returncode}, stdout={result.stdout[:500]!r}, stderr={result.stderr[:500]!r}"
        except (OSError, subprocess.TimeoutExpired) as error:
            outcome = type(error).__name__
        details.append(f"{label} ({monotonic() - start:.2f}s): {outcome}")
    return "\n".join(details)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ACLs")
def test_private_windows_directory_passes_and_broad_acl_is_rejected(tmp_path):
    root = common.private_directory(tmp_path / "RAH data")
    try:
        common.verify_windows_root(root)
    except common.Problem as error:
        pytest.fail(f"{error}\n{_powershell_diagnostics(root)}")

    # Everyone gains directory read access only in this empty pytest fixture.
    inherited = root / "inherited"
    inherited.mkdir()
    common.verify_windows_root(inherited)
    subprocess.run(["icacls.exe", str(root), "/grant", "*S-1-1-0:(OI)(CI)(R)"], check=True, timeout=15)
    with pytest.raises(common.Problem, match="broad ACL"):
        common.verify_windows_root(root)
    with pytest.raises(common.Problem, match="broad ACL"):
        common.verify_windows_root(inherited)