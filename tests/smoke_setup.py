"""Test the installed entry point, isolated setup, and retained native launchers."""
import argparse
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request

from rah.cli import connected, shortcut_path
from rah.common import APPS, clean_env, venv_python
from rah.installer import RouteBook
from rah.projects import structured_citations
from fixtures import fixture_bundle
from verification_report import verification


def wait(operation, api):
    deadline = monotonic() + 1100
    while monotonic() < deadline:
        info = api("/operations/" + operation)
        if info["state"] not in ("Running", "Queued"):
            return info
        sleep(0.3)
    raise TimeoutError("Installation did not finish")


def websocket_status(url, origin):
    address = urlsplit(url)
    with socket.create_connection((address.hostname, address.port), timeout=8) as connection:
        connection.sendall((
            "GET /ws HTTP/1.1\r\n"
            f"Host: {address.netloc}\r\nOrigin: {origin}\r\n"
            "Connection: Upgrade\r\nUpgrade: websocket\r\n"
            "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: YWJjZGVmZ2hpamtsbW5vcA==\r\n\r\n"
        ).encode())
        with connection.makefile("rb") as response:
            return int(response.readline().split()[1])


def smoke(root, uv, bundle, shortcut, real, packaged=False, *, report):
    cli = Path(sys.executable).parent / ("rah.exe" if os.name == "nt" else "rah")
    if not bundle and not packaged:
        bundle = root.parent / "fixtures"
        fixture_bundle(bundle)
    book = RouteBook.load(bundle, real and not packaged)
    names = [name for name in APPS if book.route(name)]
    report["apps"] = {name: {field: book.route(name)[field] for field in ("upstream_version", "lock_sha256")}
                      for name in names}
    if packaged:
        assert set(names) == set(APPS), f"Missing packaged install routes: {set(APPS) - set(names)}"
    setup = ["setup", "--uv", uv, "--no-browser"]
    if bundle:
        setup += ["--bundle", bundle]
    if not shortcut:
        setup.append("--no-shortcut")
    if real and not packaged:
        setup.append("--qualification")
    remover = ["remove-hub"] + ([] if shortcut else ["--no-shortcut"])

    def cli_run(*args, check=True):
        return subprocess.run([cli, "--data-dir", root, *args], check=check, timeout=60)

    try:
        cli_run(*setup)
        if shortcut:
            assert shortcut_path().exists(), "Desktop shortcut was not created"
        record = connected(root)
        assert record
        cli_run("open", "--no-browser")
        assert connected(root)["pid"] == record["pid"], "Second launch replaced the hub"
        origin = f"http://127.0.0.1:{record['port']}"
        opener = build_opener(ProxyHandler({}))

        def api(path, data=None):
            headers = {"Authorization": "Bearer " + record["token"], "Origin": origin}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = Request(origin + "/api" + path, headers=headers,
                          data=json.dumps(data).encode() if data is not None else None)
            with opener.open(req, timeout=20) as response:
                return json.load(response)

        with opener.open(origin, timeout=10) as response:
            assert b"Research Agent Hub" in response.read()
        installed = {}
        plan = api("/apps/core/install-plan")
        assert [item["id"] for item in plan["apps"]] == list(APPS)
        if not real:
            lock = bundle / "pqa.lock"
            original = lock.read_bytes()
            lock.write_bytes(original + b"\n# changed")
            try:
                partial = wait(api("/apps/core/install", {"digest": plan["digest"]})["operation_id"], api)
                assert partial["result"] == {"complete": False}, partial
                assert partial["apps"]["pqa"]["state"] == "Failed", partial
                unchanged = {name: (root / "apps" / name / "current.json").read_bytes() for name in ("ldr", "gptr")}
            finally:
                lock.write_bytes(original)
        op = api("/apps/core/install", {"digest": plan["digest"]})
        info = wait(op["operation_id"], api)
        if not real:
            assert info["result"] == {"complete": True}, info
            for name, pointer in unchanged.items():
                assert (root / "apps" / name / "current.json").read_bytes() == pointer
            print("A failed app can be retried without replacing successful siblings.", flush=True)
        for name in names:
            assert info["apps"][name]["state"] == "Installed", (name, info)
            pointer = root / "apps" / name / "current.json"
            installed[name] = json.loads(pointer.read_bytes())
            print(f"Installed {APPS[name]} through native route", flush=True)
        if packaged:
            for name, expected in (("ldr", b"local deep research"), ("gptr", b"gpt researcher")):
                try:
                    launched = api(f"/apps/{name}/launch", {})
                    deadline = monotonic() + 135
                    while launched["state"] == "Starting" and monotonic() < deadline:
                        assert "url" not in launched
                        sleep(0.5)
                        launched = api(f"/apps/{name}/browser")
                    assert launched["state"] == "Ready", (name, launched)
                    with opener.open(launched["url"], timeout=10) as response:
                        assert expected in response.read().lower()
                    if name == "gptr":
                        assert websocket_status(launched["url"], launched["url"].rstrip("/")) == 101
                        assert websocket_status(launched["url"], "https://foreign.invalid") == 403
                        foreign = Request(launched["url"], headers={"Origin": "https://foreign.invalid"})
                        try:
                            opener.open(foreign, timeout=10)
                        except HTTPError as error:
                            assert error.code == 403
                        else:
                            raise AssertionError("Foreign origin reached the native app")
                    print(f"Verified native browser readiness: {APPS[name]}", flush=True)
                finally:
                    if next(row for row in api("/apps") if row["id"] == name)["can_stop"]:
                        api(f"/apps/{name}/stop", {})
        if not real:
            first = api("/projects", {"name": "Cross-platform setup smoke"})["id"]
            op = api(f"/projects/{first}/gptr-reports", {"question": "Public fixture", "public": True, "mode": "deep"})
            result = wait(op["operation_id"], api)
            assert result["state"] == "Succeeded", result
            assert api("/projects/" + first)["artifact"][0]["original_name"] == "fixture.md"
            plan = api("/apps/pqa/install-plan")
            lock = bundle / "pqa.lock"
            original = lock.read_bytes()
            try:
                lock.write_bytes(original + b"\n# changed")
                op = api("/apps/pqa/install", {"digest": plan["digest"]})
                assert wait(op["operation_id"], api)["state"] == "Failed"
                assert json.loads((root / "apps/pqa/current.json").read_bytes()) == installed["pqa"]
            finally:
                lock.write_bytes(original)
        if packaged:
            pqa = venv_python(root / "apps/pqa/versions" / installed["pqa"]["install_id"] / "venv")
            project_root = Path(__file__).resolve().parents[1]
            subprocess.run([pqa, project_root / "tests/probe_paper_transport.py",
                            "--worker", project_root / "rah/worker.py"], env=clean_env(), timeout=120, check=True)
            ldr = venv_python(root / "apps/ldr/versions" / installed["ldr"]["install_id"] / "venv")
            export = subprocess.check_output([ldr, "-c",
                "from local_deep_research.exporters.ris_exporter import RISExporter; "
                "import sys; sys.stdout.buffer.write(RISExporter().export("
                "'## Sources\\n\\n[1] Trial result\\nURL: https://pubmed.ncbi.nlm.nih.gov/123456/').content)"],
                env=clean_env() | {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}, timeout=120)
            fixture = root / "upstream-sample.ris"
            fixture.write_bytes(export)
            assert structured_citations(fixture)[0]["pmid"] == "123456"
            print("Imported RIS emitted by pinned LDR.", flush=True)
        assert {row["id"] for row in api("/apps") if row["version"]} == set(names)
        cli_run(*remover)
        assert connected(root) is None
        if shortcut:
            assert not shortcut_path().exists()
        for name, item in installed.items():
            base = root / "apps" / name
            python = venv_python(base / "versions" / item["install_id"] / "venv")
            if real and name == "ldr":
                subprocess.run([python, "-c", "from local_deep_research.web.app import main; assert callable(main)"],
                               env=clean_env() | {"LDR_DATA_DIR": str(base / "native")}, cwd=base / "native", timeout=90, check=True)
            else:
                result = subprocess.run([python, base / "launch/native.py", "--help"], check=True,
                                        text=True, capture_output=True, timeout=90)
                assert "standalone settings revision" in result.stdout
            print(f"Verified standalone entry after hub removal: {APPS[name]}", flush=True)
    finally:
        if connected(root):
            cli_run(*remover, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--shortcut", action="store_true")
    parser.add_argument("--packaged", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    mode = "packaged" if args.packaged else "candidate" if args.bundle else "fixture"
    with verification(args.report, "installed_setup", mode=mode) as report:
        uv_version = subprocess.check_output([args.uv.resolve(), "--version"], text=True, timeout=10).split()
        if len(uv_version) >= 2 and uv_version[0] == "uv" and all(part.isdecimal() for part in uv_version[1].split(".")):
            report["uv"] = uv_version[1]
        with TemporaryDirectory(prefix="rah setup smoke ") as temp:
            root = Path(temp).resolve() / "data"
            smoke(root, args.uv.resolve(), args.bundle and args.bundle.resolve(), args.shortcut,
                  args.packaged or args.bundle is not None, args.packaged, report=report)
        report["checks"].update(setup="passed", retained_entries="passed",
                                shortcut="passed" if args.shortcut else "skipped",
                                native_transport_and_ris="passed" if args.packaged else "skipped")
