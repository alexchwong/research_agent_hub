from dataclasses import dataclass
import io
import json
import os
import re

from dotenv.parser import parse_stream

from . import search_sources
from .common import APPS, Problem, atomic, canonical, child, digest, private_directory, read_json, write_json


ROLES = {
    "ldr": {"CHAT": "LDR_LLM_MODEL"},
    "gptr": {"FAST": "FAST_LLM", "SMART": "SMART_LLM", "STRATEGIC": "STRATEGIC_LLM", "EMBEDDING": "EMBEDDING"},
    "pqa": {"ANSWER": "llm", "SUMMARY": "summary_llm", "AGENT": "agent.agent_llm", "EMBEDDING": "embedding"},
}
DEFAULT_CHAT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"
GPTR_PROFILE_VERSION = 4
PROFILE_VERSIONS = {"gptr": GPTR_PROFILE_VERSION, "ldr": 3}
COMMON = {"RAH_OPENROUTER_API_KEY", "RAH_OPENROUTER_CHAT_MODEL", "RAH_OPENROUTER_EMBEDDING_MODEL", "RAH_PQA_SETTINGS_FILE"}
GUIDED = {"RAH_OA_AUTO", "RAH_OA_PMC_CONTACT_EMAIL", "RAH_OPENURL_BASE"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")
NATIVE = {
    "ldr": {
        "LDR_SEARCH_TOOL", "LDR_SEARCH_ENGINE_WEB_OPENALEX_API_KEY", "LDR_SEARCH_ENGINE_WEB_BRAVE_API_KEY",
        "LDR_SEARCH_ENGINE_WEB_PUBMED_API_KEY", search_sources.URL_FIELDS["ldr"], "LDR_LLM_PROVIDER", "LDR_LLM_MODEL",
        "LDR_LLM_OPENAI_API_KEY", "LDR_LLM_ANTHROPIC_API_KEY", "LDR_LLM_LMSTUDIO_URL",
    },
    "gptr": {"TAVILY_API_KEY", "RETRIEVER", "SEARX_URL", "CONTEXT_FILTER", "EMBEDDING", "OPENAI_API_KEY", "CONFIG_PATH",
             "FAST_LLM", "SMART_LLM", "STRATEGIC_LLM", "LLM_PROVIDER", "FAST_LLM_MODEL", "SMART_LLM_MODEL",
             "OPENAI_BASE_URL", "OLLAMA_BASE_URL"},
    "pqa": {"OPENAI_API_KEY", "CROSSREF_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "ANTHROPIC_API_KEY"},
}
LEGACY_GPTR = {"LLM_PROVIDER", "FAST_LLM_MODEL", "SMART_LLM_MODEL", "OPENAI_BASE_URL", "CONFIG_PATH"}
KEYS = COMMON | GUIDED | {f"RAH_{app.upper()}_{role}" for app, roles in ROLES.items() for role in roles} | {
    f"RAH_NATIVE_{app.upper()}__{item}" for app, options in NATIVE.items() for item in options
}


def secret(name):
    return name.endswith("_KEY")


@dataclass
class Config:
    text: str
    values: dict
    revision: str


def parse(text):
    values = {}
    for entry in parse_stream(io.StringIO(text)):
        if entry.error:
            raise Problem("invalid_env", f"Invalid .env entry at line {entry.original.line}.")
        if entry.key in KEYS:
            if entry.key in values or entry.value is None or any(c in entry.value for c in "\r\n\0"):
                raise Problem("invalid_env", f"Invalid or repeated setting at line {entry.original.line}.")
            values[entry.key] = entry.value
    return Config(text, values, digest(text.encode()))


def load(root):
    path = child(root, "hub", "config", ".env")
    if not path.exists():
        return parse("")
    if os.name != "nt":
        stat = path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise Problem("permissions", "Set the hub .env file's permissions to 0600.")
    return parse(path.read_text(encoding="utf-8"))


def seed(root):
    """Create a private, empty hub .env. RAH no longer seeds managed PaperQA2 model values."""
    path = child(root, "hub", "config", ".env")
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        pass


def guided(config):
    """Validated guided-research settings: OA opt-in (default off), PMC contact and OpenURL base."""
    values = config.values
    auto = values.get("RAH_OA_AUTO", "")
    contact = values.get("RAH_OA_PMC_CONTACT_EMAIL", "")
    if auto not in ("", "true", "false"):
        raise Problem("invalid_settings", "RAH_OA_AUTO must be true or false.", 409)
    if contact and not EMAIL.fullmatch(contact):
        raise Problem("invalid_settings", "Enter a valid PMC contact email address.", 409)
    base = values.get("RAH_OPENURL_BASE", "")
    if base:
        from .oa import openurl_base
        openurl_base(base)
    return {"oa_auto": auto == "true", "contact": contact or None, "openurl": base or None}


def checked_projection(root, app, config, *, effective=None, upgrade=None):
    try:
        stored = applied(root, app)
        if isinstance(stored, dict):
            checksum = stored.pop("checksum", None)
            if checksum == digest(canonical(stored)):
                same_revision = stored.get("revision") == config.revision
                if same_revision and upgrade and stored.get("version") in range(1, PROFILE_VERSIONS.get(app, 1)):
                    return install_projection(root, app, upgrade())
                if effective is None and same_revision:
                    return stored
                if effective is not None and all(stored.get(key) == effective[key] for key in ("models", "env", "args", "native_file")):
                    return stored
    except (ValueError, OSError):
        pass
    raise Problem("settings_stale", "Apply saved settings before continuing.", 409)


def resolve(config, app, capabilities):
    values = config.values
    environment = {
        name: values[key] for name in NATIVE[app]
        if values.get(key := f"RAH_NATIVE_{app.upper()}__{name}")
    }
    options = {}
    if app == "gptr" and values.get("RAH_OPENROUTER_API_KEY"):
        options["OPENROUTER_API_KEY"] = values["RAH_OPENROUTER_API_KEY"]
    states = {}
    if field := search_sources.FIELDS.get(app):
        selection = environment.get(field, "").strip()
        if selection == "native":
            environment.pop(field)
        elif not selection and not (app == "gptr" and environment.get("CONFIG_PATH")):
            selection = options[field] = search_sources.DEFAULTS[app]
        elif selection:
            environment[field] = selection
        states["search"] = search_sources.LABELS[app].get(selection, "Native settings" if selection in {"", "native"} else selection)
    managed = set()
    models = {}
    for role, native_name in ROLES[app].items():
        label = f"{app.upper()}_{role}"
        value = values.get(f"RAH_{label}", "")
        default_key = "RAH_OPENROUTER_EMBEDDING_MODEL" if role == "EMBEDDING" else "RAH_OPENROUTER_CHAT_MODEL"
        inherited = not value and f"{label}:openrouter" in capabilities and values.get(default_key)
        automatic = not value and not inherited and f"{label}:openrouter" in capabilities and values.get("RAH_OPENROUTER_API_KEY") and (
            app == "gptr" and not ({native_name} | LEGACY_GPTR) & environment.keys()
            or app == "ldr" and not {"LDR_LLM_PROVIDER", "LDR_LLM_MODEL", "LDR_LLM_OPENAI_API_KEY", "LDR_LLM_ANTHROPIC_API_KEY"} & environment.keys()
        )
        if inherited or automatic:
            value = "openrouter:" + (values[default_key] if inherited else DEFAULT_EMBEDDING_MODEL if role == "EMBEDDING" else DEFAULT_CHAT_MODEL)
        if not value or value == "native":
            states[label] = "Native settings"
            continue
        provider, sep, model = value.partition(":")
        if not sep or not model or any(char.isspace() for char in model) or f"{label}:{provider}" not in capabilities:
            raise Problem("unsupported_model", f"{label} has an unsupported model mapping.")
        if role == "EMBEDDING" and provider == "openrouter" and model.split(":", 1)[0] == DEFAULT_CHAT_MODEL:
            raise Problem("unsupported_model", "DeepSeek Flash is a chat model. Choose a separate OpenRouter embedding model.", 409)
        if provider == "openrouter" and not values.get("RAH_OPENROUTER_API_KEY"):
            raise Problem("needs_credential", f"{label} needs an OpenRouter key.", 409)
        if provider == "openai" and not environment.get("OPENAI_API_KEY"):
            raise Problem("needs_credential", f"{label} needs a standalone OpenAI key.", 409)
        managed.add(native_name)
        models[role.lower()] = {"provider": provider, "model": model}
        states[label] = (f"Built-in OpenRouter default: {model}" if automatic else
                         "Shared OpenRouter default" if inherited else "Saved for next launch")
        if app == "ldr" and provider == "openrouter":
            managed.update({"LDR_LLM_PROVIDER", "LDR_LLM_OPENROUTER_API_KEY"})
            options.update(LDR_LLM_PROVIDER="openrouter", LDR_LLM_MODEL=model,
                           LDR_LLM_OPENROUTER_API_KEY=values["RAH_OPENROUTER_API_KEY"])
        elif app == "gptr" and provider == "openrouter":
            options[native_name] = value
        elif app == "pqa":
            options[native_name] = f"{provider}/{model}"
            if provider == "openrouter":
                options["OPENROUTER_API_KEY"] = values["RAH_OPENROUTER_API_KEY"]
        else:
            raise Problem("unsupported_model", f"{label} has an unsupported provider.")
    if managed & environment.keys() or (app == "gptr" and managed and LEGACY_GPTR & environment.keys()):
        raise Problem("settings_conflict", f"{APPS[app]} has overlapping guided and native settings.", 409)
    arguments = []
    native_file = None
    if app == "pqa":
        native = values.get("RAH_PQA_SETTINGS_FILE")
        if native and managed:
            raise Problem("settings_conflict", "Choose standalone PaperQA2 roles or its native settings file.", 409)
        if native:
            native_file = native
        arguments.extend(f"--{name}={options.pop(name)}" for name in sorted(managed))
    return {"env": environment | options, "args": arguments, "native_file": native_file, "states": states, "models": models, "revision": config.revision}


def update(root, revision, changes, clear):
    current = load(root)
    if current.revision != revision:
        raise Problem("stale_settings", "Settings changed. Reload and try again.", 409)
    if not isinstance(changes, dict) or not isinstance(clear, list) or any(not isinstance(key, str) for key in clear):
        raise Problem("invalid_settings", "Invalid settings input.")
    if not (changes.keys() | set(clear)) <= KEYS or any(not secret(key) for key in clear):
        raise Problem("invalid_settings", "Unknown setting or clear action.")
    replacements = {}
    for key, value in changes.items():
        if not isinstance(value, str) or len(value) > 4000 or any(char in value for char in "\r\n\0"):
            raise Problem("invalid_settings", "Invalid value for " + key + ".")
        if value or not secret(key):
            replacements[key] = value
    replacements.update(dict.fromkeys(clear, ""))
    parts = []
    for entry in parse_stream(io.StringIO(current.text)):
        if entry.key in replacements:
            parts.append(f"{entry.key}={json.dumps(replacements.pop(entry.key), ensure_ascii=False)}\n")
        else:
            parts.append(entry.original.string)
    if parts and not parts[-1].endswith("\n"):
        parts.append("\n")
    parts.extend(f"{key}={json.dumps(value, ensure_ascii=False)}\n" for key, value in replacements.items())
    candidate = parse("".join(parts))
    guided(candidate)
    atomic(child(root, "hub", "config", ".env"), candidate.text.encode())
    return candidate


def install_projection(root, app, projection, overwrite=False):
    base = private_directory(child(root, "apps", app))
    path = child(private_directory(child(base, "launch")), "profile.json")
    try:
        prior = read_json(path)
        if prior is not None:
            expected = prior.pop("checksum", None)
            edited = digest(canonical(prior)) != expected
        else:
            edited = False
    except (ValueError, AttributeError):
        edited = True
    if edited and not overwrite:
        raise Problem("edited_projection", f"{APPS[app]} generated settings were manually edited.", 409)
    body = {"version": PROFILE_VERSIONS.get(app, 1),
            "notice": "RAH-generated settings. Edit the RAH .env or native configuration.", **projection}
    signed = body | {"checksum": digest(canonical(body))}
    write_json(path, signed)
    return signed


def applied(root, app):
    return read_json(child(root, "apps", app, "launch", "profile.json"))


def public_config(root, config, capabilities):
    states = {}
    for app in APPS:
        try:
            projection = resolve(config, app, capabilities(app))
            persisted = applied(root, app)
            states[app] = projection["states"] | {
                "projection": "Saved for next launch" if persisted and persisted.get("revision") == config.revision else "Apply settings"
            }
        except (Problem, ValueError, AttributeError) as error:
            states[app] = {"projection": str(error) if isinstance(error, Problem) else "Apply settings"}
    return {
        "revision": config.revision,
        "model_defaults": {"chat": DEFAULT_CHAT_MODEL, "embedding": DEFAULT_EMBEDDING_MODEL},
        "search_defaults": search_sources.DEFAULTS,
        "search_presets": search_sources.LABELS,
        "values": {key: config.values.get(key, "") for key in sorted(KEYS) if not secret(key)},
        "credentials": {key: bool(config.values.get(key)) for key in sorted(KEYS) if secret(key)},
        "states": states,
    }
