"""Lawful open-access PDF acquisition from the official PMC dataset for exact identifiers.

Only exact public DOIs or PMCIDs are sent. DOI-to-PMCID conversion uses the PMC ID Converter; versions and
licences come from the PMC Cloud Service metadata. No landing pages, publisher logins, cookies, mirrors or
alternate providers are used. Missing copies stay in manual review: absence does not imply a paywall.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
from tempfile import NamedTemporaryFile
from time import monotonic
from urllib.parse import unquote, urlencode, urlsplit
from xml.etree import ElementTree

from .common import Problem, clean_env


CONVERTER = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
BUCKET_HOST = "pmc-oa-opendata.s3.amazonaws.com"
BUCKET = "https://" + BUCKET_HOST + "/"
HOSTS = {"pmc.ncbi.nlm.nih.gov", BUCKET_HOST}
TOOL = "research-agent-hub"
CONNECT_TIMEOUT, READ_TIMEOUT = 5, 15
PAPER_DEADLINE, PASS_DEADLINE = 45, 600
PASS_PAPERS, RUN_FILES, RUN_BYTES = 20, 40, 512 * 1024 * 1024
PDF_LIMIT = 256 * 1024 * 1024
METADATA_LIMIT = 256 * 1024
OPEN_LICENCES = re.compile(r"^(?:CC0|CC BY(?:-(?:NC|ND|SA|NC-ND|NC-SA))?)(?: \d\.\d)?$|^cc(?:0|-by(?:-nc|-nd|-sa|-nc-nd|-nc-sa)?)$", re.I)
DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:")


def now():
    return datetime.now(timezone.utc).isoformat()


# ---- Identifier canonicalisation ---------------------------------------------------------------

def doi(value):
    """Canonical DOI for automatic use, or None when it is absent, ambiguous or malformed."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    lowered = text.lower()
    for prefix in DOI_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    if "%" in text:
        text = unquote(text)
    text = text.lower()
    if (not re.fullmatch(r"10\.[0-9]{4,9}/[\x21-\x7e]+", text) or len(text.encode()) > 255
            or text[-1] in ".,;:)]}'\"" or text[text.index("/") + 1] in ".,;:" or " " in text or "\\" in text
            or re.search(r"10\.[0-9]{4,9}/.*10\.[0-9]{4,9}/", text)):
        return None
    return text


def pmid(value):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,11}", value.strip()):
        return None
    return value.strip()


def pmcid(value):
    """Return (accession, version or None) for 'PMC123' or 'PMC123.2'; reject anything else."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"PMC([1-9][0-9]{0,11})(?:\.([1-9][0-9]{0,11}))?", value.strip())
    if not match:
        return None
    return "PMC" + match.group(1), int(match.group(2)) if match.group(2) else None


def from_url(url):
    """Infer PMID/PMCID only from canonical PubMed and PMC article URLs."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return {}
    if parts.scheme != "https" or parts.username or parts.password or parts.port:
        return {}
    if parts.hostname == "pubmed.ncbi.nlm.nih.gov":
        match = re.fullmatch(r"/([1-9][0-9]{0,11})/?", parts.path)
        return {"pmid": match.group(1)} if match else {}
    if parts.hostname in {"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
        match = re.fullmatch(r"(?:/pmc)?/articles/(PMC[1-9][0-9]{0,11})/?", parts.path)
        return {"pmcid": match.group(1)} if match else {}
    return {}


def identifiers(row):
    """Exact identifiers for automatic processing, or a reason they cannot be used."""
    found = {"doi": doi(row.get("doi")) if row.get("doi") else None,
             "pmid": pmid(row.get("pmid")) if row.get("pmid") else None,
             "pmcid": pmcid(row.get("pmcid")) if row.get("pmcid") else None}
    for field in ("doi", "pmid", "pmcid"):
        if row.get(field) and not found[field]:
            return None, f"invalid_{field}"
    inferred = from_url(row.get("citation_url"))
    for field, value in inferred.items():
        current = found[field][0] if field == "pmcid" and found[field] else found[field]
        if current and current != value:
            return None, "conflicting_identifiers"
        if not current:
            found[field] = pmcid(value) if field == "pmcid" else value
    return found, None


# ---- Transport -----------------------------------------------------------------------------------

def public_addresses(host, port=443, resolver=socket.getaddrinfo):
    results = resolver(host, port, type=socket.SOCK_STREAM)
    addresses = []
    for family, _, _, _, sockaddr in results:
        address = ipaddress.ip_address(sockaddr[0])
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped:
            address = mapped
        if (not address.is_global or address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified
                or address in ipaddress.ip_network("100.64.0.0/10")):
            raise Problem("egress_blocked", "An acquisition host resolved to a non-public address.", 502)
        addresses.append((family, sockaddr))
    if not addresses:
        raise Problem("egress_blocked", "An acquisition host did not resolve.", 502)
    return addresses


def check_url(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.username or parts.password or parts.port
            or parts.fragment or "\\" in url or any(ord(char) < 33 for char in url)):
        raise Problem("egress_blocked", "The acquisition URL is outside the approved PMC endpoints.", 502)
    return parts


class Transport:
    """HTTPS GET to a validated public address while keeping the hostname for SNI, certificates and Host."""

    def __init__(self, resolver=socket.getaddrinfo, context=None, connector=None):
        self.resolver = resolver
        self.context = context or ssl.create_default_context()
        self.connector = connector

    def connect(self, host, family, sockaddr, timeout):
        if self.connector:
            return self.connector(host, family, sockaddr, timeout)
        raw = socket.socket(family, socket.SOCK_STREAM)
        raw.settimeout(min(CONNECT_TIMEOUT, timeout))
        raw.connect(sockaddr)
        raw.settimeout(min(READ_TIMEOUT, timeout))
        return self.context.wrap_socket(raw, server_hostname=host)

    def get(self, url, *, accept, limit, deadline, sink=None):
        parts = check_url(url)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise Problem("acquisition_timeout", "The per-paper acquisition deadline passed.", 504)
        family, sockaddr = public_addresses(parts.hostname, 443, self.resolver)[0]
        target = parts.path + ("?" + parts.query if parts.query else "")
        try:
            secure = self.connect(parts.hostname, family, sockaddr, remaining)
            connection = http.client.HTTPSConnection(parts.hostname, timeout=min(READ_TIMEOUT, remaining))
            connection.sock = secure
            connection.request("GET", target, headers={"Host": parts.hostname, "Accept": accept,
                                                       "Accept-Encoding": "identity", "User-Agent": TOOL})
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            raise Problem("network_error", "Retry available: the PMC service could not be reached.", 503) from error
        try:
            if 300 <= response.status < 400:
                raise Problem("redirect_refused", "The PMC service redirected the request; RAH does not follow redirects.", 502)
            if response.status == 429 or response.status >= 500:
                raise Problem("retryable_status", f"Retry available: the PMC service returned HTTP {response.status}.", 503)
            if response.status == 404:
                return 404, None, b""
            if response.status != 200:
                raise Problem("unexpected_status", f"The PMC service returned HTTP {response.status}.", 502)
            encoding = (response.getheader("Content-Encoding") or "identity").lower()
            if encoding not in ("identity", ""):
                raise Problem("unexpected_encoding", "The PMC service returned an unexpected content encoding.", 502)
            declared = response.getheader("Content-Length")
            if declared and (not declared.isdigit() or int(declared) > limit):
                raise Problem("too_large", "The response exceeds its size limit.", 413)
            media = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            total, body = 0, bytearray()
            while True:
                if monotonic() > deadline:
                    raise Problem("acquisition_timeout", "The per-paper acquisition deadline passed.", 504)
                block = response.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise Problem("too_large", "The response exceeds its size limit.", 413)
                if sink:
                    sink.write(block)
                else:
                    body.extend(block)
            if declared and int(declared) != total:
                raise Problem("truncated", "Retry available: the response was truncated.", 503)
            return 200, media, bytes(body)
        except (OSError, http.client.HTTPException) as error:
            raise Problem("network_error", "Retry available: the PMC transfer failed.", 503) from error
        finally:
            connection.close()


def strict_json(raw):
    if b"<!DOCTYPE" in raw[:512].upper():
        raise Problem("invalid_metadata", "Unexpected metadata format.", 502)
    try:
        return json.loads(raw)
    except ValueError as error:
        raise Problem("invalid_metadata", "The PMC service returned invalid JSON.", 502) from error


def strict_xml(raw):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise Problem("invalid_metadata", "The listing contained a DTD or entity.", 502)
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as error:
        raise Problem("invalid_metadata", "The PMC listing was not valid XML.", 502) from error


# ---- PMC adapter -----------------------------------------------------------------------------------

@dataclass
class Result:
    outcome: str
    identifier: str
    method: str
    version: int | None = None
    source_url: str | None = None
    metadata_url: str | None = None
    licence: str | None = None
    evidence: dict | None = None
    error: str | None = None
    temp: str | None = None
    sha256: str | None = None
    size: int | None = None
    pmcid: str | None = None
    versions: list | None = None


def convert_doi(transport, value, contact, deadline, known_pmid=None):
    query = urlencode({"ids": value, "idtype": "doi", "format": "json", "tool": TOOL, "email": contact})
    url = CONVERTER + "?" + query
    status, media, raw = transport.get(url, accept="application/json", limit=METADATA_LIMIT, deadline=deadline)
    if status != 200 or media not in ("application/json", "text/json"):
        raise Problem("invalid_metadata", "The PMC ID Converter returned an unexpected response.", 502)
    data = strict_json(raw)
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        return None, url
    record = records[0]
    if record.get("status") == "error" or (record.get("doi") or "").lower() != value:
        return None, url
    found = pmcid(record.get("pmcid") or "")
    if not found:
        return None, url
    if known_pmid and record.get("pmid") and str(record["pmid"]) != known_pmid:
        raise Problem("conflicting_identifiers", "The PMC ID Converter disagreed with a known PMID.", 409)
    return found[0], url


def list_versions(transport, accession, deadline):
    query = urlencode({"list-type": "2", "prefix": accession + ".", "delimiter": "/", "max-keys": "10"})
    status, media, raw = transport.get(BUCKET + "?" + query, accept="application/xml", limit=METADATA_LIMIT, deadline=deadline)
    if status != 200 or media not in ("application/xml", "text/xml"):
        raise Problem("invalid_metadata", "The PMC listing returned an unexpected response.", 502)
    root = strict_xml(raw)
    namespace = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    if (root.findtext(namespace + "IsTruncated") or "false").lower() == "true":
        return None
    versions = []
    for prefix in root.iter(namespace + "Prefix"):
        if prefix.text is None or prefix is root.find(namespace + "Prefix"):
            continue
        match = re.fullmatch(re.escape(accession) + r"\.([1-9][0-9]{0,11})/", prefix.text)
        if not match:
            return None
        versions.append(int(match.group(1)))
    if len(versions) > 10:
        return None
    return sorted(versions)


def metadata(transport, accession, version, deadline):
    name = f"{accession}.{version}"
    url = f"{BUCKET}{name}/{name}.json"
    status, media, raw = transport.get(url, accept="application/json", limit=METADATA_LIMIT, deadline=deadline)
    if status == 404:
        return None, url
    if status != 200 or media not in ("application/json", "binary/octet-stream", "application/octet-stream"):
        raise Problem("invalid_metadata", "The PMC metadata had an unexpected content type.", 502)
    data = strict_json(raw)
    if not isinstance(data, dict):
        raise Problem("invalid_metadata", "The PMC metadata was not an object.", 502)
    return data, url


def eligible(data, accession, version, known):
    """Evidence for one exact version, or the reason it needs manual review."""
    if data.get("pmcid") not in (accession, f"{accession}.{version}") or data.get("version") not in (version, str(version), None):
        return None, "identity_mismatch"
    if known.get("doi") and data.get("doi") and str(data["doi"]).lower() != known["doi"]:
        return None, "identity_mismatch"
    if known.get("pmid") and data.get("pmid") and str(data["pmid"]) != known["pmid"]:
        return None, "identity_mismatch"
    if data.get("is_retracted") is True or str(data.get("retracted", "")).lower() in ("true", "yes"):
        return None, "retracted"
    if data.get("is_pmc_openaccess") is not True:
        return None, "not_open_access_subset"
    licence = data.get("license_code")
    if isinstance(licence, str) and licence.strip().upper() == "TDM":
        return None, "tdm_only"
    if not isinstance(licence, str) or not OPEN_LICENCES.match(licence.strip()):
        return None, "unknown_licence"
    url = data.get("pdf_url")
    if not isinstance(url, str) or not url:
        return None, "no_pdf"
    expected_path = f"/{accession}.{version}/"
    if url.startswith("s3://pmc-oa-opendata/"):
        url = BUCKET + url[len("s3://pmc-oa-opendata/"):]
    try:
        parts = check_url(url)
    except Problem:
        return None, "pdf_outside_bucket"
    md5 = None
    if parts.query:
        match = re.fullmatch(r"md5=([0-9a-f]{32})", parts.query)
        if not match:
            return None, "pdf_outside_bucket"
        md5 = match.group(1)
    if parts.hostname != BUCKET_HOST or not parts.path.startswith(expected_path) or not parts.path.endswith(".pdf") or "//" in parts.path:
        return None, "pdf_outside_bucket"
    url = BUCKET + parts.path.lstrip("/")
    return {"pdf_url": url, "licence": licence.strip(), "md5": md5, "manuscript": data.get("is_manuscript") is True,
            "doi": data.get("doi"), "pmid": data.get("pmid"), "title": data.get("title") if isinstance(data.get("title"), str) else None}, None


def parse_check(path, python, timeout=30):
    """Verify text parseability with the pinned PaperQA2 parser in a separate, non-network process."""
    script = ("import sys, socket\n"
              "def blocked(*a, **k): raise OSError('network disabled')\n"
              "socket.socket.connect = blocked; socket.getaddrinfo = blocked\n"
              "from paperqa_pypdf import parse_pdf_to_pages\n"
              "pages = parse_pdf_to_pages(sys.argv[1], parse_media=False)\n"
              "content = getattr(pages, 'content', pages)\n"
              "texts = [v if isinstance(v, str) else (v[0] if isinstance(v, tuple) else str(v)) for v in (content.values() if isinstance(content, dict) else content)]\n"
              "sys.exit(0 if any(t.strip() for t in texts) else 4)\n")
    try:
        result = subprocess.run([str(python), "-I", "-c", script, str(path)], env=clean_env(), timeout=timeout,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "parse_timeout"
    return None if result.returncode == 0 else "not_text_parseable"


def acquire(transport, row, *, contact, temp_dir, python, deadline_seconds=PAPER_DEADLINE, version=None, budget=None):
    """Resolve one paper through the fixed PMC route. Never raises for expected outcomes."""
    deadline = monotonic() + deadline_seconds
    found, reason = identifiers(row)
    if reason:
        return Result("rejected", row.get("doi") or row.get("pmcid") or "", "pmc", error=reason)
    method = "pmc-direct"
    accession = found["pmcid"][0] if found["pmcid"] else None
    version = version or (found["pmcid"][1] if found["pmcid"] else None)
    try:
        if not accession:
            if not found["doi"]:
                return Result("unresolved", found["pmid"] or "", "pmc", error="needs_public_doi_or_pmcid")
            if not contact:
                return Result("unresolved", found["doi"], "pmc-idconv", error="contact_needed")
            accession, converter_url = convert_doi(transport, found["doi"], contact, deadline, found["pmid"])
            method = "pmc-idconv"
            if not accession:
                return Result("unresolved", found["doi"], method, metadata_url=CONVERTER, error="no_pmc_mapping")
        identifier = accession
        if version:
            candidates = [version]
        else:
            candidates = list_versions(transport, accession, deadline)
            if candidates is None:
                return Result("manual_review", identifier, method, pmcid=accession, error="listing_needs_review")
            if not candidates:
                return Result("unresolved", identifier, method, pmcid=accession, error="not_in_dataset")
        evidence = []
        for number in candidates:
            data, url = metadata(transport, accession, number, deadline)
            if data is None:
                continue
            item, problem = eligible(data, accession, number, {"doi": found["doi"], "pmid": found["pmid"]})
            evidence.append({"version": number, "metadata_url": url, "eligible": bool(item), "reason": problem,
                             **({"licence": item["licence"], "manuscript": item["manuscript"]} if item else {})})
            if item:
                evidence[-1]["item"] = item
        usable = [entry for entry in evidence if entry["eligible"]]
        if not usable:
            reasons = {entry["reason"] for entry in evidence} or {"no_metadata"}
            outcome = "manual_review" if reasons & {"unknown_licence", "identity_mismatch", "invalid_metadata"} else "unresolved"
            return Result(outcome, identifier, method, pmcid=accession, error=sorted(reasons)[0],
                          evidence={"versions": [{k: v for k, v in entry.items() if k != "item"} for entry in evidence]})
        published = [entry for entry in usable if not entry["manuscript"]]
        choice = published if len(published) == 1 else usable if len(usable) == 1 else None
        if choice is None:
            return Result("choose_version", identifier, method, pmcid=accession, error="choose_version",
                          versions=[{"version": entry["version"], "licence": entry["licence"], "manuscript": entry["manuscript"]} for entry in usable],
                          evidence={"versions": [{k: v for k, v in entry.items() if k != "item"} for entry in evidence]})
        selected = choice[0]
        item = selected["item"]
        if budget and not budget(None):
            return Result("manual_review", identifier, method, pmcid=accession, error="run_limit")
        handle = NamedTemporaryFile(dir=temp_dir, prefix=".oa-", suffix=".pdf", delete=False)
        try:
            with handle:
                status, media, _ = transport.get(item["pdf_url"], accept="application/pdf", limit=PDF_LIMIT, deadline=deadline, sink=handle)
            if status != 200:
                raise Problem("not_in_dataset", "The PMC object was not found.", 404)
            if media not in ("application/pdf", "application/octet-stream", "binary/octet-stream"):
                raise Problem("not_pdf", "The PMC object was not a PDF.", 502)
            with open(handle.name, "rb") as source:
                head = source.read(5)
                source.seek(0)
                sha = hashlib.file_digest(source, "sha256").hexdigest()
                source.seek(0)
                md5 = hashlib.file_digest(source, "md5").hexdigest()
            size = os.path.getsize(handle.name)
            if head != b"%PDF-":
                raise Problem("not_pdf", "The PMC object has no PDF header.", 502)
            if item["md5"] and md5 != item["md5"]:
                raise Problem("checksum_mismatch", "The PMC object failed its MD5 integrity check.", 502)
            if budget and not budget(size):
                raise Problem("run_limit", "The run reached its automatic download limit.", 413)
            problem = parse_check(handle.name, python)
            if problem:
                raise Problem(problem, "The PDF is not text-parseable (image-only, encrypted or invalid).", 422)
        except Problem as error:
            os.unlink(handle.name)
            code = error.code
            outcome = "failed_retryable" if error.status == 503 else "manual_review"
            return Result(outcome, identifier, method, version=selected["version"], pmcid=accession, error=code,
                          source_url=item["pdf_url"], metadata_url=selected["metadata_url"], licence=item["licence"])
        except BaseException:
            os.unlink(handle.name)
            raise
        return Result("acquired", identifier, method, version=selected["version"], source_url=item["pdf_url"],
                      metadata_url=selected["metadata_url"], licence=item["licence"], pmcid=accession, temp=handle.name,
                      sha256=sha, size=size, evidence={"versions": [{k: v for k, v in entry.items() if k != "item"} for entry in evidence],
                                                       "subset": "PMC Open Access Subset", "manuscript": item["manuscript"],
                                                       "retrieved_at": now()})
    except Problem as error:
        return Result("failed_retryable" if error.status in (503, 504) else "manual_review",
                      accession or found.get("doi") or "", method, pmcid=accession, error=error.code)


def manual_links(row, openurl_base=None):
    found = {"doi": doi(row.get("doi")), "pmid": pmid(row.get("pmid")), "pmcid": pmcid(row.get("pmcid"))}
    links = []
    if found.get("doi"):
        links.append({"label": "DOI", "url": "https://doi.org/" + found["doi"]})
    if found.get("pmid"):
        links.append({"label": "PubMed", "url": f"https://pubmed.ncbi.nlm.nih.gov/{found['pmid']}/"})
    if found.get("pmcid"):
        links.append({"label": "PMC", "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{found['pmcid'][0]}/"})
    if openurl_base and (found.get("doi") or found.get("pmid")):
        query = {"rft_id": "info:doi/" + found["doi"]} if found.get("doi") else {"rft_id": "info:pmid/" + found["pmid"]}
        links.append({"label": "Library", "url": openurl_base + ("&" if "?" in openurl_base else "?") + urlencode(query)})
    return links


def openurl_base(value):
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment or len(value) > 500:
        raise Problem("openurl", "Enter an HTTPS institutional OpenURL base, such as https://library.example.edu/openurl.", 422)
    return value


def formula_safe(value):
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def export_requests(rows):
    header = ["title", "doi", "pmid", "pmcid", "citation_url", "note", "state", "access_status"]
    csv = "\r\n".join([",".join(map(formula_safe, header))] +
                      [",".join(formula_safe(row.get(field)) for field in header) for row in rows]) + "\r\n"
    ris = []
    for row in rows:
        ris.append("TY  - JOUR")
        for tag, field in (("TI", "title"), ("DO", "doi"), ("UR", "citation_url"), ("N1", "note")):
            if row.get(field):
                ris.append(f"{tag}  - " + " ".join(str(row[field]).split()))
        if row.get("pmid"):
            ris.append("AN  - PMID:" + str(row["pmid"]))
        ris.append("ER  - ")
    ids = [value for row in rows for value in (row.get("doi"), row.get("pmid") and "PMID:" + row["pmid"], row.get("pmcid")) if value]
    return {"csv": csv, "ris": "\n".join(ris) + "\n", "ids": "\n".join(ids) + ("\n" if ids else "")}


def python_for(path):
    return path if path else sys.executable
