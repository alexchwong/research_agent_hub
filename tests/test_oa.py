import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Thread
from time import monotonic
from urllib.parse import parse_qs, urlsplit

import pytest

from rah import oa
from rah.common import Problem
from fixtures import text_pdf


@pytest.mark.parametrize("value,expected", [
    ("10.1000/ABC", "10.1000/abc"), ("https://doi.org/10.1000/abc", "10.1000/abc"), ("doi:10.1000/a%2Fb", "10.1000/a/b"),
    ("10.1000/abc.", None), ("10.1000/ a", None), ("11.1000/x", None), ("10.1000/x 10.1000/y", None), ("10.1000/" + "x" * 300, None),
])
def test_doi_canonicalisation(value, expected):
    assert oa.doi(value) == expected


def test_identifier_rules():
    assert oa.pmid("12345") == "12345" and oa.pmid("0123") is None and oa.pmid("1" * 13) is None
    assert oa.pmcid("PMC123.2") == ("PMC123", 2) and oa.pmcid("pmc123") is None and oa.pmcid("PMC0") is None
    assert oa.from_url("https://pmc.ncbi.nlm.nih.gov/articles/PMC42/") == {"pmcid": "PMC42"}
    assert oa.from_url("https://pubmed.ncbi.nlm.nih.gov/77/") == {"pmid": "77"}
    assert oa.from_url("https://example.org/PMC42") == {} and oa.from_url("http://pubmed.ncbi.nlm.nih.gov/77/") == {}
    assert oa.identifiers({"pmcid": "PMC1", "citation_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC2/"})[1] == "conflicting_identifiers"
    assert oa.identifiers({"doi": "not a doi"})[1] == "invalid_doi"


def metadata(accession="PMC777", version=1, **extra):
    return {"pmcid": accession, "version": version, "doi": "10.1000/abc", "pmid": "12345", "is_pmc_openaccess": True,
            "is_manuscript": False, "is_retracted": False, "license_code": "CC BY",
            "pdf_url": f"s3://pmc-oa-opendata/{accession}.{version}/{accession}.{version}.pdf"} | extra


class FakeTransport:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, *, accept, limit, deadline, sink=None):
        oa.check_url(url)
        self.calls.append(url)
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                status, media, body = response(url) if callable(response) else response
                if sink is not None:
                    sink.write(body)
                    return status, media, b""
                return status, media, body
        return 404, None, b""


def listing(*versions, truncated=False):
    prefixes = "".join(f"<CommonPrefixes><Prefix>PMC777.{v}/</Prefix></CommonPrefixes>" for v in versions)
    return (200, "application/xml", f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Prefix>PMC777.</Prefix><IsTruncated>{str(truncated).lower()}</IsTruncated>{prefixes}</ListBucketResult>".encode())


def run(tmp_path, transport, row, monkeypatch, parse=None, **options):
    monkeypatch.setattr(oa, "parse_check", lambda path, python: parse)
    return oa.acquire(transport, row, contact=options.pop("contact", "maintainer@example.org"), temp_dir=tmp_path,
                      python="python", **options)


def test_doi_to_pmcid_to_pdf(tmp_path, monkeypatch):
    pdf = text_pdf()
    md5 = hashlib.md5(pdf).hexdigest()
    transport = FakeTransport({
        oa.CONVERTER: (200, "application/json", json.dumps({"records": [{"doi": "10.1000/abc", "pmcid": "PMC777", "pmid": "12345"}]}).encode()),
        oa.BUCKET + "?": listing(1),
        oa.BUCKET + "PMC777.1/PMC777.1.json": (200, "application/json", json.dumps(metadata(pdf_url=f"s3://pmc-oa-opendata/PMC777.1/PMC777.1.pdf?md5={md5}")).encode()),
        oa.BUCKET + "PMC777.1/PMC777.1.pdf": (200, "application/octet-stream", pdf)})
    result = run(tmp_path, transport, {"doi": "https://doi.org/10.1000/ABC", "pmid": "12345"}, monkeypatch)
    assert result.outcome == "acquired" and result.version == 1 and result.licence == "CC BY"
    assert open(result.temp, "rb").read() == pdf and result.sha256 == hashlib.sha256(pdf).hexdigest()
    query = parse_qs(urlsplit(transport.calls[0]).query)
    assert query == {"ids": ["10.1000/abc"], "idtype": ["doi"], "format": ["json"], "tool": ["research-agent-hub"], "email": ["maintainer@example.org"]}
    assert parse_qs(urlsplit(transport.calls[1]).query) == {"list-type": ["2"], "prefix": ["PMC777."], "delimiter": ["/"], "max-keys": ["10"]}
    assert transport.calls[-1] == oa.BUCKET + "PMC777.1/PMC777.1.pdf"


@pytest.mark.parametrize("change,outcome,error", [
    ({"license_code": "TDM"}, "unresolved", "tdm_only"), ({"is_retracted": True}, "unresolved", "retracted"),
    ({"is_pmc_openaccess": False}, "unresolved", "not_open_access_subset"), ({"license_code": None}, "manual_review", "unknown_licence"),
    ({"doi": "10.1000/other"}, "manual_review", "identity_mismatch"),
    ({"pdf_url": "https://evil.test/PMC777.1/x.pdf"}, "unresolved", "pdf_outside_bucket"),
])
def test_ineligible_records_stay_manual(tmp_path, monkeypatch, change, outcome, error):
    transport = FakeTransport({oa.BUCKET + "?": listing(1),
                               oa.BUCKET + "PMC777.1/PMC777.1.json": (200, "application/json", json.dumps(metadata(**change)).encode())})
    result = run(tmp_path, transport, {"pmcid": "PMC777", "doi": "10.1000/abc"}, monkeypatch)
    assert (result.outcome, result.error) == (outcome, error)
    assert not any(call.endswith(".pdf") for call in transport.calls)


def test_versions_choose_and_contact(tmp_path, monkeypatch):
    meta = {1: metadata(version=1, is_manuscript=True), 2: metadata(version=2), 3: metadata(version=3)}
    routes = {oa.BUCKET + "?": listing(1, 2, 3)} | {
        oa.BUCKET + f"PMC777.{v}/PMC777.{v}.json": (200, "application/json", json.dumps(item).encode()) for v, item in meta.items()}
    result = run(tmp_path, FakeTransport(routes), {"pmcid": "PMC777"}, monkeypatch)
    assert result.outcome == "choose_version" and [item["version"] for item in result.versions] == [1, 2, 3]
    assert run(tmp_path, FakeTransport({oa.BUCKET + "?": listing(truncated=True)}), {"pmcid": "PMC777"}, monkeypatch).outcome == "manual_review"
    missing = run(tmp_path, FakeTransport({}), {"doi": "10.1000/abc"}, monkeypatch, contact=None)
    assert (missing.outcome, missing.error) == ("unresolved", "contact_needed")
    assert run(tmp_path, FakeTransport({}), {"pmid": "123"}, monkeypatch).error == "needs_public_doi_or_pmcid"


def test_invalid_pdf_is_not_usable(tmp_path, monkeypatch):
    routes = {oa.BUCKET + "?": listing(1), oa.BUCKET + "PMC777.1/PMC777.1.json": (200, "application/json", json.dumps(metadata()).encode())}
    bad = run(tmp_path, FakeTransport(routes | {oa.BUCKET + "PMC777.1/PMC777.1.pdf": (200, "application/pdf", b"<html>")}), {"pmcid": "PMC777"}, monkeypatch)
    assert (bad.outcome, bad.error) == ("manual_review", "not_pdf")
    image = run(tmp_path, FakeTransport(routes | {oa.BUCKET + "PMC777.1/PMC777.1.pdf": (200, "application/pdf", text_pdf(""))}),
                {"pmcid": "PMC777"}, monkeypatch, parse="not_text_parseable")
    assert (image.outcome, image.error) == ("manual_review", "not_text_parseable")
    assert list(tmp_path.iterdir()) == []


def test_egress_boundaries():
    for url in ("http://pmc-oa-opendata.s3.amazonaws.com/x", "https://user@pmc-oa-opendata.s3.amazonaws.com/x",
                "https://pmc-oa-opendata.s3.amazonaws.com:8443/x", "https://evil.test/x", "https://pmc-oa-opendata.s3.amazonaws.com/x#f"):
        with pytest.raises(Problem):
            oa.check_url(url)
    for address in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "100.64.0.1", "::1", "::ffff:192.168.0.1", "fc00::1"):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        with pytest.raises(Problem):
            oa.public_addresses("pmc.ncbi.nlm.nih.gov", resolver=lambda *a, **k: [(family, 1, 6, "", (address, 443))])
    assert oa.public_addresses("pmc.ncbi.nlm.nih.gov", resolver=lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))])


class Handler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        status, headers, body = self.routes[self.path]
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def local_transport(server):
    def connector(host, family, sockaddr, timeout):
        assert host == oa.BUCKET_HOST and sockaddr[0] == "93.184.216.34"
        return socket.create_connection(server.server_address, timeout=timeout)

    return oa.Transport(resolver=lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))], connector=connector)


def test_transport_refuses_redirects_encodings_and_oversize(local_server):
    Handler.routes = {"/r": (302, {"Location": "https://evil.test/"}, b""), "/gz": (200, {"Content-Encoding": "gzip"}, b"x"),
                      "/big": (200, {"Content-Length": "999999"}, b"x"), "/ok": (200, {"Content-Type": "application/json", "Content-Length": "2"}, b"{}"),
                      "/short": (200, {"Content-Length": "10"}, b"abc")}
    transport = local_transport(local_server)
    deadline = monotonic() + 5
    for path, code in (("/r", "redirect_refused"), ("/gz", "unexpected_encoding"), ("/big", "too_large"), ("/short", "network_error")):
        with pytest.raises(Problem) as error:
            transport.get(oa.BUCKET + path.lstrip("/"), accept="*/*", limit=1000, deadline=deadline)
        assert error.value.code in (code, "truncated")
    assert transport.get(oa.BUCKET + "ok", accept="application/json", limit=1000, deadline=deadline) == (200, "application/json", b"{}")


def test_request_exports_are_formula_safe():
    exported = oa.export_requests([{"title": "=HYPERLINK(\"x\")", "doi": "10.1000/x", "pmid": "1", "state": "needed"}])
    assert exported["csv"].splitlines()[1].startswith('"\'=HYPERLINK(""x"")"')
    assert "DO  - 10.1000/x" in exported["ris"] and exported["ids"] == "10.1000/x\nPMID:1\n"
    links = oa.manual_links({"doi": "10.1000/x", "pmid": "1"}, "https://library.example.edu/openurl")
    assert [item["label"] for item in links] == ["DOI", "PubMed", "Library"]
    with pytest.raises(Problem):
        oa.openurl_base("http://library.example.edu/openurl")
