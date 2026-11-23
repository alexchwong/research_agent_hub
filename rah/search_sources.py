"""Search preset metadata and a credential-free local SearXNG diagnostic."""
import json
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, ProxyHandler, Request

from .common import Problem
from .openrouter import NoRedirect


DEFAULTS = {"gptr": "pubmed_central", "ldr": "pubmed"}
FIELDS = {"gptr": "RETRIEVER", "ldr": "LDR_SEARCH_TOOL"}
URL_FIELDS = {"gptr": "SEARX_URL", "ldr": "LDR_SEARCH_ENGINE_WEB_SEARXNG_DEFAULT_PARAMS_INSTANCE_URL"}
LABELS = {
    "gptr": {"pubmed_central": "PubMed Central (NCBI)", "searx": "Local SearXNG and its enabled search engines", "tavily": "Tavily"},
    "ldr": {"pubmed": "PubMed (NCBI)", "wikipedia": "Wikipedia", "searxng": "Local SearXNG and its enabled search engines"},
}


def local_url(value):
    try:
        parts = urlsplit(value)
        if (parts.scheme in {"http", "https"} and parts.hostname in {"127.0.0.1", "::1"}
                and parts.port and not (parts.username or parts.password or parts.query or parts.fragment)
                and not any(c.isspace() or c == "\\" for c in value)):
            return value.rstrip("/")
    except ValueError:
        pass
    raise Problem("search_configuration", "Enter a local SearXNG base URL with a port, such as http://127.0.0.1:8888.", 409)


def test_searx(value):
    url = local_url(value)
    query = urlencode({"q": "Python pathlib documentation", "format": "json"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            Request(url + "/search?" + query, headers={"Accept": "application/json"}), timeout=8,
        ) as response:
            data = json.loads(response.read(2 * 1024 * 1024))
    except (OSError, ValueError, RecursionError) as error:
        raise Problem("search_unavailable", "Could not read SearXNG search results. Check the instance and enable JSON output.", 503) from error
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise Problem("search_unavailable", "SearXNG did not return a JSON results list.", 503)
    count = 0
    for item in results:
        url = item.get("url") if isinstance(item, dict) else None
        if isinstance(url, str):
            try:
                parts = urlsplit(url)
                count += bool(parts.scheme in {"http", "https"} and parts.hostname)
            except ValueError:
                continue
    if not count:
        engines = data.get("unresponsive_engines")
        detail = f" ({len(engines)} unresponsive engines)" if isinstance(engines, list) and engines else ""
        raise Problem("no_sources", f"SearXNG returned no usable results{detail}. Check its enabled engines.", 503)
    return count