"""Managed PaperQA2 route registry and eligibility checks.

Each route binds one guided quality to exact OpenRouter model IDs, one explicit provider per role and the
single-provider-no-fallback policy. Routes ship as disabled candidates. A maintainer enables a route for a
platform only by adding the P6 evidence identity below after owner approval. A passing report alone does not
change runtime availability.
"""
from importlib import resources
import platform
from types import MappingProxyType

from . import installer
from .common import Problem, canonical, child, digest, file_digest
from .research_profiles import MODELS, PQA_ROUTES


CHAT = "https://openrouter.ai/api/v1/chat/completions"
EMBEDDINGS = "https://openrouter.ai/api/v1/embeddings"
POLICY = "single-provider-no-fallback"
WORKER = "pqa_profile_worker.py"
RETIRED = ("openrouter-text-v1",)
PAPER_ROLES = MappingProxyType({
    "answer": "Question, cited excerpts and answer context",
    "summary": "Selected paper passages and citation metadata",
    "agent": "Question, tool context and evidence excerpts",
    "embedding": "Text chunks and query text",
})


def _route(quality, summary, agent, answer):
    return {"profile": quality, "status": "candidate", "policy": POLICY,
            "roles": {"summary": {"model": MODELS[summary], "endpoint": CHAT},
                      "agent": {"model": MODELS[agent], "endpoint": CHAT},
                      "answer": {"model": MODELS[answer], "endpoint": CHAT},
                      "embedding": {"model": MODELS["embedding_small"], "endpoint": EMBEDDINGS}},
            # Each enabled identity: platform, arch, python, upstream_version, lock_sha256, worker_sha256,
            # providers {role: provider}, evidence_sha256. Added only after P6 qualification and owner approval.
            "enabled": ()}


REGISTRY = MappingProxyType({
    PQA_ROUTES["budget"]: _route("budget", "haiku", "haiku", "haiku"),
    PQA_ROUTES["standard"]: _route("standard", "haiku", "terra", "terra"),
    PQA_ROUTES["premium"]: _route("premium", "sonnet", "sonnet", "sonnet"),
})


def worker_bytes():
    return resources.files("rah").joinpath(WORKER).read_bytes()


def worker_digest():
    return digest(worker_bytes())


def host_identity():
    os_id, arch = installer.host()
    return {"platform": os_id, "arch": arch, "python": platform.python_version()}


def identity(key, providers, *, upstream_version, lock_sha256):
    """The complete tuple that P6 evidence and an enabled registry entry must bind."""
    route = REGISTRY[key]
    host = host_identity()
    return {"route": key, "profile": route["profile"], "policy": route["policy"], **host,
            "upstream_version": upstream_version, "lock_sha256": lock_sha256, "worker_sha256": worker_digest(),
            "roles": {role: {"model": data["model"], "endpoint": data["endpoint"], "provider": providers.get(role)}
                      for role, data in route["roles"].items()}}


def destinations(key, providers):
    route = REGISTRY[key]
    return [{"role": role, "service": "OpenRouter", "model": data["model"], "endpoint": data["endpoint"],
             "provider": providers[role], "data": PAPER_ROLES[role]} for role, data in route["roles"].items()]


def profile(key, providers, *, upstream_version, python, qualification=False):
    rows = destinations(key, providers)
    return {"id": key, "quality": REGISTRY[key]["profile"], "policy": POLICY, "parser": "Local text-only PDF parsing",
            "destinations": rows, "python": python, "upstream_version": upstream_version,
            "qualification": bool(qualification), "identity": digest(canonical(rows))}


def enabled_entry(key, install_record, route_row):
    route = REGISTRY.get(key)
    if not route:
        return None
    host = host_identity()
    for entry in route["enabled"]:
        if (entry.get("platform") == host["platform"] and entry.get("arch") == host["arch"]
                and entry.get("python") == host["python"] and entry.get("worker_sha256") == worker_digest()
                and entry.get("lock_sha256") == route_row["lock_sha256"] == install_record.get("lock_sha256")
                and entry.get("upstream_version") == route_row["upstream_version"] == install_record.get("upstream_version")
                and set(entry.get("providers", {})) == set(route["roles"]) and all(entry["providers"].values())
                and entry.get("evidence_sha256")):
            return entry
    return None


def resolve(root, book, quality_or_key):
    """Return (route_row, installation, profile) for an enabled managed route, or raise unavailable."""
    key = PQA_ROUTES.get(quality_or_key, quality_or_key)
    if key in RETIRED:
        raise Problem("retired_profile", "This managed PaperQA2 route was retired. Start a fresh review on an available quality.", 409)
    if key not in REGISTRY:
        raise Problem("unknown_profile", "Unknown managed PaperQA2 route.", 404)
    route_row = book.route("pqa")
    install = installer.install_location(root, "pqa")
    if not route_row or book.custom or route_row.get("qualification_only") or not install:
        raise Problem("unverified_profile", "Install PaperQA2 at its manifest pin, or open native PaperQA2.", 503)
    record, location = install
    if (record.get("lock_sha256") != route_row["lock_sha256"] or record.get("upstream_version") != route_row["upstream_version"]
            or file_digest(child(book.directory, route_row["lock_path"])) != route_row["lock_sha256"]):
        raise Problem("unverified_profile", "Repair PaperQA2 at the reviewed pin or open native PaperQA2.", 503)
    entry = enabled_entry(key, record, route_row)
    if not entry:
        label = REGISTRY[key]["profile"].title()
        raise Problem("profile_unavailable", f"Managed PDF analysis is unavailable for {label} on this platform: its route "
                      "has no qualification evidence yet. Choose another available quality, or open native PaperQA2 "
                      "without an automatic file transfer.", 503)
    return route_row, location, profile(key, entry["providers"], upstream_version=route_row["upstream_version"],
                                        python=platform.python_version()) | {"evidence": entry["evidence_sha256"]}


def availability(root, book):
    result = {}
    for quality, key in PQA_ROUTES.items():
        try:
            resolve(root, book, key)
            result[quality] = {"available": True, "route": key}
        except Problem as error:
            result[quality] = {"available": False, "route": key, "code": error.code, "reason": str(error)}
        except OSError:
            result[quality] = {"available": False, "route": key, "code": "unverified_profile",
                               "reason": "Repair the PaperQA2 install and try again."}
    return result
