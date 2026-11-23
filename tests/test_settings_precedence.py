import json

import pytest

from rah import settings
from rah.common import Problem
from rah.installer import RouteBook


DEFAULTS = {
    "RAH_OPENROUTER_API_KEY": "synthetic-shared-key",
    "RAH_OPENROUTER_CHAT_MODEL": "test/chat",
    "RAH_OPENROUTER_EMBEDDING_MODEL": "test/embed",
}


def resolve(app, values):
    config = settings.parse("\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()))
    return settings.resolve(config, app, RouteBook.load().capabilities(app))


def test_role_defaults_override_and_release_coexist():
    profile = resolve("gptr", DEFAULTS | {
        "RAH_GPTR_SMART": "openrouter:test/override",
        "RAH_GPTR_STRATEGIC": "native",
        "RAH_NATIVE_GPTR__STRATEGIC_LLM": "native-provider:test/strategy",
        "RAH_NATIVE_GPTR__TAVILY_API_KEY": "synthetic-search-key",
        "RAH_NATIVE_PQA__OPENAI_API_KEY": "synthetic-other-app-key",
    })
    assert profile["env"] == {
        "FAST_LLM": "openrouter:test/chat",
        "SMART_LLM": "openrouter:test/override",
        "STRATEGIC_LLM": "native-provider:test/strategy",
        "EMBEDDING": "openrouter:test/embed",
        "TAVILY_API_KEY": "synthetic-search-key",
        "RETRIEVER": "pubmed_central",
        "OPENROUTER_API_KEY": "synthetic-shared-key",
    }
    assert profile["states"]["GPTR_FAST"] == "Shared OpenRouter default"
    assert profile["states"]["GPTR_SMART"] == "Saved for next launch"
    assert profile["states"]["GPTR_STRATEGIC"] == "Native settings"


def test_key_only_selects_openrouter_for_unconfigured_text_and_vector_roles():
    key = {"RAH_OPENROUTER_API_KEY": "synthetic-shared-key"}
    gptr = resolve("gptr", key | {"RAH_NATIVE_GPTR__OPENAI_API_KEY": "old-openai-key"})
    assert gptr["env"] | {} == {
        "OPENAI_API_KEY": "old-openai-key",
        "RETRIEVER": "pubmed_central",
        "OPENROUTER_API_KEY": "synthetic-shared-key",
        **dict.fromkeys(("FAST_LLM", "SMART_LLM", "STRATEGIC_LLM"), "openrouter:deepseek/deepseek-v4.1-flash-1017"),
        "EMBEDDING": "openrouter:openai/text-embedding-3-small",
    }
    assert resolve("ldr", key)["env"]["LDR_LLM_MODEL"] == "deepseek/deepseek-v4.1-flash-1017"
    assert not resolve("pqa", key)["models"]
    with pytest.raises(Problem, match="chat model"):
        resolve("gptr", key | {"RAH_OPENROUTER_EMBEDDING_MODEL": "deepseek/deepseek-v4.1-flash-1017"})


@pytest.mark.parametrize("app,extra,code", [
    ("ldr", {"RAH_NATIVE_LDR__LDR_LLM_MODEL": "test/native"}, "settings_conflict"),
    ("gptr", {"RAH_NATIVE_GPTR__OPENAI_BASE_URL": "https://example.invalid"}, "settings_conflict"),
    ("gptr", {"RAH_NATIVE_GPTR__FAST_LLM_MODEL": "test/legacy"}, "settings_conflict"),
    ("pqa", {"RAH_PQA_SETTINGS_FILE": "native.toml"}, "settings_conflict"),
    ("ldr", {"RAH_LDR_CHAT": "unmapped:test/model"}, "unsupported_model"),
    ("ldr", {"RAH_OPENROUTER_API_KEY": ""}, "needs_credential"),
])
def test_unsafe_or_unverified_combinations_do_not_project(app, extra, code):
    with pytest.raises(Problem) as error:
        resolve(app, DEFAULTS | extra)
    assert error.value.code == code


def test_native_pqa_settings_can_be_used_without_managed_credentials():
    profile = resolve("pqa", DEFAULTS | {"RAH_PQA_SETTINGS_FILE": "native.toml"} | {
        f"RAH_PQA_{role}": "native" for role in ("ANSWER", "SUMMARY", "AGENT", "EMBEDDING")
    })
    assert profile["native_file"] == "native.toml"
    assert profile["env"] == profile["models"] == {}
    assert profile["args"] == []