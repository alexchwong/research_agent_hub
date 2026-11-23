import io
from importlib import resources
import platform
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from rah import settings
from rah.common import Problem, atomic, digest, prepare_root, private_directory, venv_python, write_json
from rah.installer import RouteBook
from rah.jobs import Jobs
from rah.openrouter import OpenRouter
from rah.projects import Projects, citations, structured_citations
from rah.runs import PAPER_ROLES, Runs
from rah.web import create_app


class HubTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = prepare_root(Path(self.directory.name).resolve() / "rah")

    def test_settings_and_projection(self):
        roles = ["LDR_CHAT:openrouter"]
        config = settings.parse("RAH_OPENROUTER_API_KEY=example\nRAH_OPENROUTER_CHAT_MODEL=vendor/model:variant\n")
        resolved = settings.resolve(config, "ldr", roles)
        self.assertEqual(resolved["env"]["LDR_LLM_MODEL"], "vendor/model:variant")
        self.assertEqual(resolved["env"]["LDR_LLM_OPENROUTER_API_KEY"], "example")
        explicit_native = settings.parse(config.text + "RAH_LDR_CHAT=native\n")
        self.assertEqual(settings.resolve(explicit_native, "ldr", roles)["env"], {"LDR_SEARCH_TOOL": "pubmed"})
        self.assertEqual(settings.resolve(config, "pqa", [])["env"], {})
        with self.assertRaises(Problem):
            settings.resolve(settings.parse(config.text + "RAH_LDR_CHAT=openrouter\n"), "ldr", roles)

    def test_project_revisions_and_associations(self):
        store = Projects(self.root)
        first, second = store.create("first"), store.create("second")
        pdf = store.register(first, io.BytesIO(b"%PDF-1.7\nfirst"), "sample.pdf", "pdf", 0)
        with self.assertRaises(Problem):
            store.save_papers(second, 0, [{"title": "paper", "state": "supplied", "pdf_artifact_id": pdf}])
        store.save_papers(first, 0, [{"title": "paper", "state": "supplied", "pdf_artifact_id": pdf}])
        self.assertEqual(store.selected_pdfs(first, [pdf])[0]["id"], pdf)
        with self.assertRaises(Problem):
            store.save_papers(first, 0, [{"title": "stale edit"}])
        self.assertEqual(citations("1. A DOI 10.1000/test\n   PMID: 1234\n2. B")[0]["pmid"], "1234")

    def test_protected_requests_and_unverified_pdf(self):
        client = create_app(self.root, RouteBook({"schema_version": 1, "routes": []}), None,
                            "identity", "test-token", lambda: None, lambda: "127.0.0.1:8500").test_client()
        origin = "http://127.0.0.1:8500"
        self.assertEqual(client.get("/api/settings", base_url=origin).status_code, 401)
        headers = {"Authorization": "Bearer test-token", "Origin": "http://elsewhere.test"}
        self.assertEqual(client.post("/api/projects", base_url=origin, headers=headers, json={"name": "x"}).status_code, 403)
        headers["Origin"] = origin
        project = client.post("/api/projects", base_url=origin, headers=headers, json={"name": "x"}).get_json()["id"]
        response = client.post(f"/api/projects/{project}/pqa-preview", base_url=origin, headers=headers,
                               json={"question": "question", "pdfs": [], "revision": 0})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "unverified_profile")

    def test_usage_without_provider_network(self):
        key = ["first"]
        provider = OpenRouter(lambda: key[0])
        with patch("rah.openrouter.request_json", return_value={"usage": 0, "byok_usage": 2}):
            self.assertEqual(provider.refresh()["fields"]["usage"], "0")
        key[0] = "second"
        self.assertIsNone(provider.usage()["fields"]["usage"])


    def test_native_ris_intake(self):
        report = self.root / "fixture.ris"
        report.write_text(
            "TY  - ELEC\nID  - ref1\nTI  - Synthetic trial\nUR  - https://pubmed.ncbi.nlm.nih.gov/123456/\nER  - \n"
            "TY  - ELEC\nID  - ref2\nTI  - Replication\nDO  - 10.1000/test\nER  - \n", encoding="utf-8")
        rows = structured_citations(report)
        self.assertEqual(rows[0]["pmid"], "123456")
        self.assertEqual(rows[1]["doi"], "10.1000/test")
        report.write_text('{"sources":[]}', encoding="utf-8")
        with self.assertRaises(Problem):
            structured_citations(report)

    def test_verified_route_binds_roles_and_selected_bytes(self):
        config_path = self.root / "hub/config/.env"
        config = ("RAH_OPENROUTER_API_KEY=synthetic\nRAH_OPENROUTER_CHAT_MODEL=fixture/chat\n"
                  "RAH_OPENROUTER_EMBEDDING_MODEL=fixture/embed\nRAH_PQA_OPENAI_API_KEY=ignored\n")
        atomic(config_path, config.encode())
        install_id = str(uuid4())
        app = private_directory(self.root / "apps/pqa")
        python = venv_python(app / "versions" / install_id / "venv")
        private_directory(python.parent)
        python.touch()
        lock = self.root / "fixture.lock"
        atomic(lock, b"fixture lock")
        lock_hash = digest(lock.read_bytes())
        write_json(app / "current.json", {"install_id": install_id, "lock_sha256": lock_hash, "upstream_version": "fixture"})
        route = {"lock_sha256": lock_hash, "lock_path": lock.name, "upstream_version": "fixture",
                 "python_build": platform.python_version(), "paper_profile": settings.PAPER_PROFILE}
        evidence = {"status": "verified", "lock_sha256": [lock_hash], "reference": "test fixture",
                    "worker_sha256": digest(resources.files("rah").joinpath("worker.py").read_bytes()),
                    "python": platform.python_version(), "upstream_version": "fixture",
                    "policy": "single-provider-no-fallback",
                    "roles": {role: {"provider": "Azure",
                                     "model": "fixture/embed" if role == "embedding" else "fixture/chat",
                                     "endpoint": "https://openrouter.ai/api/v1/" +
                                                 ("embeddings" if role == "embedding" else "chat/completions")}
                              for role in PAPER_ROLES}}
        book = Mock()
        book.route.return_value = route
        book.document = {"paper_profiles": {settings.PAPER_PROFILE: evidence}}
        book.directory = self.root
        book.custom = False
        book.capabilities.return_value = [f"PQA_{role.upper()}:openrouter" for role in PAPER_ROLES]
        projects = Projects(self.root)
        project = projects.create("Transfers")
        chosen = projects.register(project, io.BytesIO(b"%PDF-1.7\nselected"), "chosen.pdf", "pdf", 0)
        excluded = projects.register(project, io.BytesIO(b"%PDF-1.7\nexcluded"), "excluded.pdf", "pdf")
        projects.save_papers(project, 0, [{"title": item, "state": "supplied", "pdf_artifact_id": item}
                                          for item in (chosen, excluded)])
        runs = Runs(self.root, book, projects, Jobs())

        settings.install_projection(self.root, "pqa", settings.resolve(settings.load(self.root), "pqa", book.capabilities("pqa")))
        declined = runs.preview(project, [chosen], "Question")
        self.assertFalse((runs.folder(declined["preview_id"]) / "corpus").exists())
        self.assertIsNone(runs.approve(project, declined["preview_id"], declined["binding"], False))
        self.assertFalse(runs.folder(declined["preview_id"]).exists())
        with self.assertRaises(Problem):
            runs.approve(project, declined["preview_id"], declined["binding"], True)

        stale = runs.preview(project, [chosen], "Question")
        pending = runs.previews[stale["preview_id"]]
        atomic(config_path, config.replace("KEY=synthetic", "KEY=replaced").encode())
        with self.assertRaises(Problem) as failure:
            runs.run_paper(pending, Mock())
        self.assertEqual(failure.exception.code, "settings_stale")
        self.assertFalse(pending.directory.exists())

        atomic(config_path, (config + "# unrelated edit\n").encode())
        preview = runs.preview(project, [chosen], "Question")
        self.assertEqual(preview["files"], ["chosen.pdf"])
        self.assertEqual({item["provider"] for item in preview["profile"]["destinations"]},
                         {"Azure"})

        def verify(folder, interpreter, env, payload, record, *, job):
            self.assertEqual(interpreter, python)
            self.assertEqual(env["OPENROUTER_API_KEY"], "synthetic")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(payload["profile"], preview["profile"])
            self.assertEqual([path.read_bytes() for path in (folder / "corpus").iterdir()], [b"%PDF-1.7\nselected"])
            return {"verified": True}

        with patch.object(runs, "invoke", side_effect=verify), patch.object(runs.jobs, "start") as start:
            runs.approve(project, preview["preview_id"], preview["binding"], True)
            self.assertEqual(start.call_args.args[1](Mock()), {"verified": True})
            with self.assertRaises(Problem):
                runs.approve(project, preview["preview_id"], preview["binding"], True)

    def test_embedding_catalogue_is_cached_separately(self):
        provider = OpenRouter(lambda: None)

        def fetch(call, _headers):
            if call.url == "https://openrouter.ai/api/v1/embeddings/models":
                return {"data": [{"id": "fixture/embed"}]}
            self.assertEqual(call.url, "https://openrouter.ai/api/v1/models")
            return {"data": [{"id": "fixture/chat", "architecture": {"output_modalities": ["text"]}},
                             {"id": "fixture/image", "architecture": {"output_modalities": ["image"]}}]}

        with patch("modelspell._http.get_json", side_effect=fetch) as request:
            self.assertEqual(provider.models("chat"), ["fixture/chat"])
            self.assertEqual(provider.models("embedding"), ["fixture/embed"])
            self.assertEqual(provider.models("chat"), ["fixture/chat"])
            self.assertEqual(request.call_count, 2)
