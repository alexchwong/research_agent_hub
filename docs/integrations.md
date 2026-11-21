# Integrations

| App | Pinned artifacts | Native entry and data |
|---|---|---|
| Local Deep Research | PyPI 2.0.1, MIT | `ldr-web`, loopback port 58721, native history and exports. The pinned RIS exporter emits titled citation records with URLs and DOI values where supplied. RAH previews their available fields and keeps the original RIS. |
| GPT Researcher | Source v3.8.0, commit `597a99e2ef879606229754ec0d575a89eb587464`, and PyPI 0.17.0 | Original CLI and upstream Classic UI using its FastAPI server. Serve native assets and the WebSocket through one guarded loopback port, 58722. Use the upstream UI for its native modes. |
| PaperQA2 | PyPI 2026.8.12, Apache-2.0. Managed candidate uses `fhlmi 0.46.2`, `fhaviary 0.37.0` and LiteLLM 1.84.1. | Standalone `pqa` and public `Settings`/`ask` API. The pin uses pypdf for local text parsing in the managed profile. |

The GPT Researcher source archive retains its licence and vendor notices. Review its source and package metadata before redistribution. Resolver inputs and wheel/archive hashes are recorded in the host manifests. Ubuntu and Windows use the CPU Torch index for LDR. The Apple silicon LDR lock requires macOS 14 or later for its pinned FAISS wheel.

## Model settings

| App | Intended OpenRouter mapping | Qualification |
|---|---|---|
| LDR | `LDR_LLM_PROVIDER`, `LDR_LLM_MODEL` and the documented scoped OpenRouter key field | Native pinned provider inspected. Live saved override, release to native, academic search and role precedence pending. |
| GPT Researcher | `FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM` and `EMBEDDING` through `openrouter:<model-id>` | Pin inspected. Classic native HTML and origin controls tested locally. Live roles and native modes pending. Configure its selected search retriever separately where needed. |
| PaperQA2 | `llm`, `summary_llm`, `agent.agent_llm` and `embedding` via the same shared key | Installed pinned clients serialized the OpenRouter URLs, key and exact provider restriction for all four roles, with a native agent tool request. The synthetic intercept verifies client request shape. Live downstream identity, actual document traffic and failure behaviour are pending. |

LiteLLM’s chat transport flattens the provider field from `extra_body`. Its pinned embedding path forwards the provider field supplied as an embedding keyword. Leaving the embedding setting under `extra_body` sent a nested field. The original resolution of `fhlmi 1.11.0` also failed before sending PaperQA2 tool requests due to the callback signature used with Aviary. The lock pins the newest inspected compatible release, `0.46.2`, without changing the upstream app versions.

The structured import uses the pinned RIS export. Arbitrary JSON files can still be retained and downloaded. The release still needs a live academic survey through the native applications.
