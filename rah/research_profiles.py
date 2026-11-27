"""Immutable guided research quality catalogue (version 1).

The catalogue maps each guided quality to exact OpenRouter model IDs per functional role. It does not
choose research steps, tools or answers; those remain with the upstream applications.
"""
from types import MappingProxyType


CATALOGUE_VERSION = 1
MODELS = MappingProxyType({
    "deepseek": "deepseek/deepseek-v4.1-flash-1017",
    "haiku": "anthropic/claude-haiku-5.5",
    "terra": "openai/gpt-6.1-terra",
    "sol": "openai/gpt-6.2-sol",
    "sonnet": "anthropic/claude-sonnet-5.5",
    "embedding_small": "openai/text-embedding-3-small",
})
LABELS = MappingProxyType({
    "deepseek": "DeepSeek", "haiku": "Haiku", "terra": "Terra", "sol": "Sol", "sonnet": "Sonnet",
    "embedding_small": "Embedding Small",
})
QUALITIES = ("budget", "standard", "premium")
QUALITY_LABELS = MappingProxyType({"budget": "Budget", "standard": "Standard", "premium": "Premium"})
DEFAULT_QUALITY = "standard"
EFFORT_LABEL = "Provider/native default (RAH does not set effort)"


def _profile(smart, strategic, summary, agent, answer):
    return MappingProxyType({
        "survey": MODELS["deepseek"],
        "gptr_fast": MODELS["deepseek"],
        "scope_gate": MODELS["haiku"],
        "gptr_smart": MODELS[smart],
        "gptr_strategic": MODELS[strategic],
        "pqa_summary": MODELS[summary],
        "pqa_agent": MODELS[agent],
        "pqa_answer": MODELS[answer],
        "embedding": MODELS["embedding_small"],
    })


PROFILES = MappingProxyType({
    "budget": _profile("haiku", "haiku", "haiku", "haiku", "haiku"),
    "standard": _profile("terra", "terra", "haiku", "terra", "terra"),
    "premium": _profile("sol", "sol", "sonnet", "sonnet", "sonnet"),
})
PQA_ROUTES = MappingProxyType({"budget": "pqa-budget-v1", "standard": "pqa-standard-v1", "premium": "pqa-premium-v1"})
ROLE_LABELS = MappingProxyType({
    "survey": "Survey and GPT Researcher FAST", "gptr_fast": "GPT Researcher FAST", "scope_gate": "Scope Gate",
    "gptr_smart": "Research synthesis / GPT Researcher SMART",
    "gptr_strategic": "Strategic reasoning / GPT Researcher STRATEGIC",
    "pqa_summary": "Managed PaperQA2 summary", "pqa_agent": "Managed PaperQA2 agent",
    "pqa_answer": "Managed PaperQA2 answer", "embedding": "Embeddings",
})


def quality(value):
    if value not in QUALITIES:
        from .common import Problem
        raise Problem("research_quality", "Choose Budget, Standard or Premium research quality.")
    return value


def snapshot(value):
    """The per-run profile copy stored with an approved brief."""
    return {"catalogue_version": CATALOGUE_VERSION, "quality": quality(value), "models": dict(PROFILES[value]),
            "pqa_route": PQA_ROUTES[value]}


def survey_projection():
    model = "openrouter:" + MODELS["deepseek"]
    return {"FAST_LLM": model, "SMART_LLM": model, "STRATEGIC_LLM": model,
            "EMBEDDING": "openrouter:" + MODELS["embedding_small"]}


def downstream_projection(value):
    models = PROFILES[quality(value)]
    return {"FAST_LLM": "openrouter:" + models["gptr_fast"], "SMART_LLM": "openrouter:" + models["gptr_smart"],
            "STRATEGIC_LLM": "openrouter:" + models["gptr_strategic"], "EMBEDDING": "openrouter:" + models["embedding"]}


def public_catalogue():
    return {"version": CATALOGUE_VERSION, "default": DEFAULT_QUALITY, "effort": EFFORT_LABEL,
            "qualities": [{"id": key, "label": QUALITY_LABELS[key], "roles": dict(PROFILES[key]),
                           "pqa_route": PQA_ROUTES[key]} for key in QUALITIES],
            "role_labels": dict(ROLE_LABELS), "models": {LABELS[key]: value for key, value in MODELS.items()}}
