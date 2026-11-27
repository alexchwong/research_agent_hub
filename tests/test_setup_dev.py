import os
import subprocess
import sys

import pytest

if os.name != "posix":
    pytest.skip("shell dev setup is POSIX-only", allow_module_level=True)

from scripts import setup_dev


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    release = tmp_path / ".release-test"
    release.mkdir()
    monkeypatch.setattr(setup_dev, "REPO", tmp_path)

    def prepare(repo, build):
        launcher = build / "launcher"
        launcher.touch(mode=0o700)
        python = build / "installed-hub/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        (python.parent / "rah").touch(mode=0o700)
        (build / "current-link").symlink_to(build.name)

    monkeypatch.setattr(setup_dev, "prepare", prepare)
    return release


def seed_chat(release):
    chat = release / "manual-data/chats/history.sqlite"
    chat.parent.mkdir(parents=True)
    chat.write_text("prior chat")
    return chat


def test_reinstall_removes_chats_and_old_builds(sandbox, tmp_path):
    setup_dev.main("install", [])
    first = setup_dev.current_build(sandbox)
    chat = seed_chat(sandbox)
    stale = sandbox / "dev.stale123"
    stale.mkdir()
    outside = tmp_path / "normal-workspace"
    outside.mkdir()
    (chat.parent / "external").symlink_to(outside, target_is_directory=True)
    unrelated = sandbox / "installed-hub"
    unrelated.mkdir()

    setup_dev.main("install", [])

    assert not (sandbox / "manual-data").exists()
    assert not first.exists()
    assert not stale.exists()
    assert list(sandbox.glob("dev.*")) == [setup_dev.installed_build(sandbox)]
    assert os.access(sandbox / "bin/rah", os.X_OK)
    assert unrelated.is_dir() and outside.is_dir()


def test_failed_preparation_preserves_current_install(sandbox, monkeypatch):
    setup_dev.main("install", [])
    first = setup_dev.current_build(sandbox)
    chat = seed_chat(sandbox)

    def fail(*args):
        raise subprocess.CalledProcessError(1, "build")

    monkeypatch.setattr(setup_dev, "prepare", fail)
    with pytest.raises(setup_dev.SetupError, match="existing dev data was not changed"):
        setup_dev.main("install", [])
    assert chat.read_text() == "prior chat"
    assert list(sandbox.glob("dev.*")) == [first]
    assert setup_dev.current_build(sandbox) == first


def test_refuses_active_launcher_or_legacy_hub(sandbox):
    chat = seed_chat(sandbox)
    with setup_dev.lock(sandbox / ".setup-dev.lock", "busy", create=True):
        with pytest.raises(setup_dev.SetupError, match="launcher is running"):
            setup_dev.main("install", [])
    instance = sandbox / "manual-data/hub/runtime/instance.lock"
    instance.parent.mkdir(parents=True)
    with setup_dev.lock(instance, "busy", create=True):
        with pytest.raises(setup_dev.SetupError, match="running dev hub"):
            setup_dev.main("install", [])
    assert chat.exists()


def test_refuses_active_native_app(sandbox):
    chat = seed_chat(sandbox)
    script = chat.parent / "native.py"
    script.write_text("import time; time.sleep(60)")
    with subprocess.Popen([sys.executable, script]) as process:
        try:
            with pytest.raises(setup_dev.SetupError, match=f"PID {process.pid}"):
                setup_dev.main("install", [])
        finally:
            process.terminate()
    assert chat.exists()


def test_refuses_symlink_at_cleanup_root(sandbox, tmp_path):
    outside = tmp_path / "normal-workspace"
    outside.mkdir()
    (sandbox / "manual-data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(setup_dev.SetupError, match="Expected a real directory"):
        setup_dev.main("install", [])
    assert outside.is_dir()


def test_verify_uses_current_build_without_resetting(sandbox, monkeypatch):
    setup_dev.main("install", [])
    first = setup_dev.current_build(sandbox)
    chat = seed_chat(sandbox)
    calls = []
    monkeypatch.setattr(setup_dev, "verify", lambda repo, build: calls.append(build))
    setup_dev.main("verify", [])
    assert calls == [first]
    assert setup_dev.current_build(sandbox) == first
    assert chat.exists()