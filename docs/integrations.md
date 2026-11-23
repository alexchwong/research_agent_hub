# Integrations

| App | Pinned artifacts | Native entry and data |
|---|---|---|
| Local Deep Research | PyPI 2.0.1, MIT | `ldr-web`, loopback port 58721, native history and exports. The pinned RIS exporter emits titled citation records with URLs and DOI values where supplied. RAH previews their available fields and keeps the original RIS. |
| GPT Researcher | Source v3.8.0, commit `597a99e2ef879606229754ec0d575a89eb587464`, and PyPI 0.17.0 | Original CLI and upstream Classic UI using its FastAPI server. Serve native assets and the WebSocket through one guarded loopback port, 58722. Use the upstream UI for its native modes. |
| PaperQA2 | PyPI 2026.8.12, Apache-2.0. Managed profile uses `fhlmi 0.46.2`, `fhaviary 0.37.0` and LiteLLM 1.84.1. | Standalone `pqa` and public `Settings`/`ask` API. The pin uses pypdf for local text parsing in the managed profile. |

The GPT Researcher source archive retains its licence and vendor notices. Review its source and package metadata before redistribution. Resolver inputs and wheel/archive hashes are recorded in the host manifests. Ubuntu and Windows use the CPU Torch index for LDR. The Apple silicon LDR lock requires macOS 14 or later for its pinned FAISS wheel.

## Model settings

| App | Intended OpenRouter mapping | Qualification |
|---|---|---|
| LDR | `LDR_LLM_PROVIDER`, `LDR_LLM_MODEL` and the documented scoped OpenRouter key field | Native pinned provider inspected. Live saved override, release to native, academic search and role precedence pending. |
| GPT Researcher | `FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM` and `EMBEDDING` through `openrouter:<model-id>` | Pin inspected. Classic native HTML and origin controls tested locally. Live roles and native modes pending. Configure its selected search retriever separately where needed. |
| PaperQA2 | `llm`, `summary_llm`, `agent.agent_llm` and `embedding` via the same shared key | Installed pinned clients serialized the OpenRouter URLs, key and exact provider restriction for all four roles, with a native agent tool request. The synthetic intercept verifies client request shape. The installed report observed Azure chat generations. Embedding endpoint constraints and rejection were observed, and serving-provider identity was unavailable in embedding responses. |

LiteLLM’s chat transport flattens the provider field from `extra_body`. Its pinned embedding path forwards the provider field supplied as an embedding keyword. Leaving the embedding setting under `extra_body` sent a nested field. The original resolution of `fhlmi 1.11.0` also failed before sending PaperQA2 tool requests due to the callback signature used with Aviary. The lock pins the newest inspected compatible release, `0.46.2`, without changing the upstream app versions.

The structured import uses the pinned RIS export. Arbitrary JSON files can still be retained and downloaded. The release still needs a live academic survey through the native applications.

The LDR and GPT Researcher locks include small platform-independent wheels built from the exact source archives and SHA-256 values listed under `built_sources` in the installer manifest. The packaged wheelhouses carry those wheels so deployment can remain binary-only. The locks record the bundled wheel hashes and do not change package versions.

## Reviewed PaperQA2 profile

The packaged `openrouter-text-v1` profile covers OpenRouter chat for answer, summary and agent with `openai/gpt-4.1-mini`, and embeddings with `openai/text-embedding-3-small`. Both endpoints restrict routing to Azure and disable fallbacks. The worker uses local text-only PDF parsing and a fresh corpus and index. It disables image parsing and document metadata lookups.

The Ubuntu and Apple silicon locks share a digest. Windows uses the same dependency versions with Windows-only `colorama` and `pywin32` additions. The reviewed parser and transport distributions share their universal wheel hashes across the locks. See [release evidence](releases.md) for digests, the report scope and separate desktop acceptance.
