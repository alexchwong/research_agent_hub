import os
import signal
import subprocess
import sys
from time import monotonic, sleep

import pytest

from rah.cli import connected, register_shortcut
from rah.common import exclusive


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal signals")
@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGHUP", "SIGTERM"])
def test_console_signal_stops_foreground_hub(tmp_path, signal_name):
    stop_signal = getattr(signal, signal_name)
    root = tmp_path.resolve() / "data"
    command = [sys.executable, "-m", "rah.cli", "--data-dir", str(root), "open", "--foreground", "--no-browser"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            deadline = monotonic() + 10
            while process.poll() is None and monotonic() < deadline:
                if record := connected(root):
                    assert record["pid"] == process.pid
                    break
                sleep(0.05)
            else:
                pytest.fail("Foreground hub did not become ready")
            process.send_signal(stop_signal)
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 128 + stop_signal, stderr
            assert "press Ctrl+C or close this console window" in stdout
            runtime = root / "hub/runtime"
            assert not (runtime / "connection.json").exists()
            with exclusive(runtime / "instance.lock"):
                assert not connected(root)
        finally:
            if process.poll() is None:
                process.kill()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop entry format")
def test_desktop_shortcut_launches_in_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # The shortcut requires a wheel-installed console script; use a fixture target.
    target = tmp_path / "rah"
    target.touch()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    content = register_shortcut(tmp_path / "hub data").read_text()
    assert '"open" "--foreground"' in content
    assert "Terminal=true" in content