import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts import bundle_hub


@pytest.fixture
def wheel_inputs(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    package = repo / "rah"
    package.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        (repo / name).touch()
    (package / "cli.py").write_bytes(b"# current working-tree CLI\n")
    (package / "new_module.py").touch()
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "cli.pyc").touch()
    (package / ".gitignore").touch()
    stale = repo / "build/lib/rah/cli.py"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"# stale CLI\n")
    newer = (package / "cli.py").stat().st_mtime + 3600
    os.utime(stale, (newer, newer))
    (repo / "research_agent_hub.egg-info").mkdir()
    (repo / ".env").touch()
    wheelhouse = tmp_path / "bootstrap/wheelhouse"
    wheelhouse.mkdir(parents=True)
    monkeypatch.setattr(bundle_hub, "REPO", repo)
    return repo, wheelhouse, stale


def write_wheel(out, files):
    with ZipFile(out / "research_agent_hub-0.1.0-py3-none-any.whl", "w") as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)


def test_build_wheel_ignores_newer_checkout_cache(wheel_inputs, monkeypatch):
    repo, wheelhouse, stale = wheel_inputs

    def fake_build(uv, build, wheel, out_dir, out, *, cwd):
        assert (uv, build, wheel, out_dir, out) == (Path("uv"), "build", "--wheel", "--out-dir", wheelhouse)
        assert cwd != repo
        assert {entry.name for entry in cwd.iterdir()} == {"rah", "pyproject.toml", "README.md", "LICENSE"}
        assert {entry.name for entry in (cwd / "rah").iterdir()} == {"cli.py", "new_module.py"}
        cached = cwd / "build/lib/rah/cli.py"
        cli = cached if cached.exists() else cwd / "rah/cli.py"
        write_wheel(out, {"rah/cli.py": cli.read_bytes(), "rah/new_module.py": b""})

    monkeypatch.setattr(bundle_hub, "run", fake_build)
    with ZipFile(bundle_hub.build_wheel(Path("uv"), wheelhouse)) as wheel:
        assert wheel.read("rah/cli.py") == (repo / "rah/cli.py").read_bytes()
        assert "rah/new_module.py" in wheel.namelist()
    assert stale.read_bytes() == b"# stale CLI\n"
    assert list(wheelhouse.parent.iterdir()) == [wheelhouse]


def test_build_wheel_rejects_source_mismatch(wheel_inputs, monkeypatch):
    _, wheelhouse, stale = wheel_inputs

    def fake_build(*args, cwd):
        write_wheel(wheelhouse, {"rah/cli.py": stale.read_bytes(), "rah/unexpected.py": b""})

    monkeypatch.setattr(bundle_hub, "run", fake_build)
    with pytest.raises(ValueError, match="Wheel differs from staged RAH source") as error:
        bundle_hub.build_wheel(Path("uv"), wheelhouse)
    assert "missing=['rah/new_module.py']" in str(error.value)
    assert "extra=['rah/unexpected.py']" in str(error.value)
    assert "changed=['rah/cli.py']" in str(error.value)