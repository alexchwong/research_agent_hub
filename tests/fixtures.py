import hashlib
import json
from pathlib import Path
import platform
import tarfile
import zipfile

from rah.installer import ENTRIES, PINS, host


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wheel(directory, name, files, entry=None):
    stem = name + "-1.0"
    path = directory / (stem + "-py3-none-any.whl")
    info = stem + ".dist-info/"
    files |= {
        info + "METADATA": "Metadata-Version: 2.1\nName: " + name.replace("_", "-") + "\nVersion: 1.0\n",
        info + "WHEEL": "Wheel-Version: 1.0\nGenerator: rah-ci-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if entry:
        files[info + "entry_points.txt"] = "[console_scripts]\n" + entry + "\n"
    files[info + "RECORD"] = "".join(filename + ",,\n" for filename in [*files, info + "RECORD"])
    with zipfile.ZipFile(path, "w") as out:
        for filename, body in files.items():
            out.writestr(filename, body)
    return path


LDR = """
import sys

def main():
    if "--help" in sys.argv:
        print("LDR fixture help")
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import os

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Native LDR fixture")

    HTTPServer((os.environ["LDR_WEB_HOST"], int(os.environ["LDR_WEB_PORT"])), Handler).serve_forever()
"""

GPT = """
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--report_type", required=True, choices=["research_report", "deep"])
p.add_argument("--no-pdf", action="store_true")
p.add_argument("--no-docx", action="store_true")
p.add_argument("question")
args = p.parse_args()
Path("outputs").mkdir(exist_ok=True)
Path("outputs/fixture.md").write_text("# Native result: " + args.question, encoding="utf-8")
"""

PQA = """
def main():
    print("PaperQA2 fixture help")
"""


def fixture_bundle(bundle):
    bundle.mkdir()
    wheels = bundle / "wheels"
    wheels.mkdir()
    rows = []
    platform_id, arch = host()
    for app in PINS:
        distribution = "fake_" + app
        files = {distribution + ".py": PQA}
        entry = None
        artifacts = []
        if app == "ldr":
            files = {"local_deep_research/__init__.py": "", "local_deep_research/web/__init__.py": "",
                     "local_deep_research/web/app.py": LDR}
            entry = "ldr-web = local_deep_research.web.app:main"
        if app == "pqa":
            entry = "pqa = fake_pqa:main"
        if app == "gptr":
            source = bundle / "cli.py"
            source.write_text(GPT, encoding="utf-8")
            archive = bundle / "fake-source.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(source, arcname="gptr-fixture/cli.py")
            source.unlink()
            artifacts.append({"file": archive.name, "sha256": checksum(archive), "type": "tar.gz"})
        payload = wheel(wheels, distribution, files, entry)
        lock = bundle / (app + ".lock")
        lock.write_text(distribution.replace("_", "-") + "==1.0 --hash=sha256:" + checksum(payload) + "\n", encoding="utf-8")
        rows.append({
            "app_id": app, "platform": platform_id, "arch": arch, "python_build": platform.python_version(),
            "upstream_version": PINS[app], "native_entry": ENTRIES[app], "artifacts": artifacts,
            "lock_path": lock.name, "lock_sha256": checksum(lock), "wheelhouse": wheels.name,
            "settings_capabilities": [], "essential": [distribution],
        })
    (bundle / "routes.json").write_text(json.dumps({"schema_version": 1, "routes": rows}), encoding="utf-8")
