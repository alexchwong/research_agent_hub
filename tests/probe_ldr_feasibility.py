"""P0 probe: pinned LDR RIS emitter, open-access downloader contract and programmatic quick_summary feasibility.

Runs inside the LDR venv with a temporary data directory and blocked sockets. Records pass/fail per criterion.
No native database, cookies or user history are read.
"""
import argparse
import inspect
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from unittest.mock import patch


def blocked(*args, **kwargs):
    raise RuntimeError("Network access is disabled in this probe")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"ris": {}, "downloader": {}, "sdk": {}}
    with TemporaryDirectory(prefix="rah ldr probe ") as data, patch.object(socket.socket, "connect", blocked), \
            patch.object(socket, "getaddrinfo", blocked):
        os.environ.update(LDR_DATA_DIR=data, HOME=data, PYTHON_DOTENV_DISABLED="1", LITELLM_LOCAL_MODEL_COST_MAP="True")
        from local_deep_research.exporters.ris_exporter import RISExporter
        sample = "## Sources\n\n[1] Trial result\nURL: https://pubmed.ncbi.nlm.nih.gov/123456/\n\n[2] PMC article\nURL: https://pmc.ncbi.nlm.nih.gov/articles/PMC777/\n"
        content = RISExporter().export(sample).content
        text = content.decode() if isinstance(content, bytes) else content
        report["ris"] = {"records": text.count("ER  -"), "has_title": "TI  -" in text, "has_url": "UR  -" in text,
                         "pmid_url": "pubmed.ncbi.nlm.nih.gov/123456" in text, "pmc_url": "PMC777" in text}

        from local_deep_research.research_library.downloaders.open_access_doi import OpenAccessDOIDownloader
        from local_deep_research.research_library.downloaders.base import DownloadResult
        fields = list(DownloadResult._fields)
        source = inspect.getsource(OpenAccessDOIDownloader)
        report["downloader"] = {
            "result_fields": fields,
            "exposes_identifier_version": any(name in fields for name in ("identifier", "version", "pmcid")),
            "exposes_origin_and_final_url": any(name in fields for name in ("url", "final_url", "source_url")),
            "exposes_access_licence_evidence": any(name in fields for name in ("licence", "license", "access")),
            "explicit_error": "skip_reason" in fields,
            "landing_page_attempt": "landing" in source.lower(),
            "labels_missing_oa_as_paywall": "paywall" in source.lower(),
            "public_interface": [name for name in ("download", "download_with_result", "can_handle") if hasattr(OpenAccessDOIDownloader, name)],
        }
        report["downloader"]["passes"] = all(report["downloader"][key] for key in (
            "exposes_identifier_version", "exposes_origin_and_final_url", "exposes_access_licence_evidence", "explicit_error"))

        from local_deep_research.api import research_functions
        signature = inspect.signature(research_functions.quick_summary)
        returns = inspect.getsource(research_functions.quick_summary)
        report["sdk"] = {
            "parameters": list(signature.parameters),
            "accepts_settings_snapshot": "settings_snapshot" in returns,
            "returns_markdown_summary": '"summary"' in returns,
            "returns_typed_ris": "ris" in returns.lower(),
            "returns_sources_list": '"sources"' in returns,
            "writes_native_history": "ResearchHistory" in returns or "research_history" in returns,
        }
        try:
            result = research_functions.quick_summary("Synthetic public question", settings_override={"search.tool": "pubmed"})
            report["sdk"]["offline_run"] = "returned" if isinstance(result, dict) else "unexpected"
        except Exception as error:  # Class only: no payloads.
            report["sdk"]["offline_run"] = type(error).__name__
        report["sdk"]["passes"] = bool(report["sdk"]["returns_typed_ris"] and report["sdk"]["offline_run"] == "returned")
    args.report.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
