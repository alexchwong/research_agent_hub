import json
import os
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from rah.openrouter import NoRedirect, OpenRouter


pytestmark = pytest.mark.live
DEFAULT_MODELS = {"CHAT": "openai/gpt-4.1-nano", "EMBEDDING": "openai/text-embedding-3-small"}


@pytest.fixture
def key():
    value = os.environ.get("OPENROUTER_API_KEY")
    if not value:
        pytest.fail("Set OPENROUTER_API_KEY for the requested live run", pytrace=False)
    return value


def test_current_key_usage(key):
    result = OpenRouter(lambda: key).refresh()
    if result["error"] or result["fields"]["usage"] is None:
        pytest.fail("Current-key usage was unavailable: " + (result["error"] or "missing usage"), pytrace=False)
    assert result["currency"] == "USD" and result["refreshed_at"] and not result["stale"]


@pytest.mark.parametrize("role,endpoint,inputs", [
    ("CHAT", "chat/completions", {"messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 16}),
    ("EMBEDDING", "embeddings", {"input": "Synthetic test text."}),
])
def test_direct_inference(request, role, endpoint, inputs):
    if not request.config.getoption("--live-inference"):
        pytest.skip("requires --live-inference (may incur charges)")
    key = request.getfixturevalue("key")
    variable = f"RAH_LIVE_{role}_MODEL"
    model = os.environ.get(variable) or DEFAULT_MODELS[role]
    try:
        call = Request("https://openrouter.ai/api/v1/" + endpoint,
                       data=json.dumps({"model": model, **inputs}).encode(),
                       headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with build_opener(ProxyHandler({}), NoRedirect()).open(call, timeout=30) as response:
            result = json.loads(response.read(1024 * 1024))
        payload = result["choices"][0]["message"] if role == "CHAT" else result["data"][0]["embedding"]
        if not payload:
            raise ValueError
    except (URLError, OSError, ValueError, KeyError, IndexError, TypeError):
        pytest.fail(f"OpenRouter {role.lower()} request failed or returned an invalid result", pytrace=False)