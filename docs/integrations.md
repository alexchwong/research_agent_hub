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
| LDR | `LDR_LLM_PROVIDER`, `LDR_LLM_MODEL` and the documented scoped OpenRouter key field | Headless guided route verified by the installed probe with fake transports (see below). Native pinned provider inspected. Live saved override, release to native, academic search and role precedence pending. |
| GPT Researcher | `FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM` and `EMBEDDING` through `openrouter:<model-id>` | Pin inspected. Classic native HTML and origin controls tested locally. The guided survey projection was verified on the installed CLI with fake transports (see below). Live roles and native modes pending. |
| PaperQA2 | Standalone: `llm`, `summary_llm`, `agent.agent_llm` and `embedding` via the native settings or RAH role values. Managed: the per-quality routes below. | Installed pinned clients serialized the OpenRouter URLs, key and exact provider restriction for all four roles and all three candidate routes, with a native agent tool request. Live qualification of the new routes is pending (P6). |

LiteLLM’s chat transport flattens the provider field from `extra_body`. Its pinned embedding path forwards the provider field supplied as an embedding keyword. Leaving the embedding setting under `extra_body` sent a nested field. The original resolution of `fhlmi 1.11.0` also failed before sending PaperQA2 tool requests due to the callback signature used with Aviary. The lock pins the newest inspected compatible release, `0.46.2`, without changing the upstream app versions.

The default GPT Researcher search source is PubMed Central for both Classic and managed reports; Local Deep Research defaults to PubMed. Neither requires a separate search key. PubMed Central searches a full-text archive, not the full PubMed index. Search queries still go to the selected service. PaperQA2 continues to search the selected PDFs locally. Model calls use their own settings and credentials.

Use **Settings → Search sources** to choose another source. For broad web search, supply a local SearXNG base URL, enable JSON output on the instance, and use **Test SearXNG** before saving. The test sends a fixed public query and only succeeds if the instance returns usable links. Tavily is optional and requires a key only when selected. Explicit source settings are retained on upgrade. Restart running native apps after changing their settings. Managed GPT Researcher reports with zero retrieved sources are not saved as sourced reports; Classic shows a warning and retains its native output.

RAH can reopen or stop Local Deep Research and GPT Researcher Classic browser sessions that it launched and can authenticate after a hub restart. Use **Stop running app** in **App details**. RAH does not stop standalone listeners or sessions launched by older builds that lack a recovery record. PaperQA2 has no persistent browser server: use **Request cancellation** in the running chat to stop its managed worker before result collection. Cancellation does not undo provider requests already sent. Standalone PaperQA2 commands are controlled from their own terminal.

The structured import uses the pinned RIS export. Arbitrary JSON files can still be retained and downloaded. The release still needs a live academic survey through the native applications.

The LDR and GPT Researcher locks include small platform-independent wheels built from the exact source archives and SHA-256 values listed under `built_sources` in the installer manifest. The packaged wheelhouses carry those wheels so deployment can remain binary-only. The locks record the bundled wheel hashes and do not change package versions.

## Guided research routes (purpose pipeline refactor)

### GPT Researcher survey and downstream reports

The guided route uses the packaged CLI (`597a99e2…`) through the installed app interpreter with `--report_type research_report|deep --report_source web --no-pdf --no-docx -- <question>`. The installed `--help` lists both modes. The projection is built from the checked saved GPT Researcher profile but keeps only its single supported retriever (`pubmed_central`, local `searx` with its URL, or `tavily` with its key) and the shared OpenRouter key. `CONFIG_PATH`, legacy base/model overrides, other credentials and multi-retriever values are rejected for guided runs and remain available to Classic. Image generation and MCP are disabled.

The P0 probe ran the installed CLI in-process with fake OpenRouter chat, embedding and PubMed Central responses at the installed transport boundary (`httpx` and the pinned OpenAI SDK's `httpx2`, plus `requests`), with real sockets blocked:

| Observation | Result |
| --- | --- |
| Effective settings | `MAX_ITERATIONS=1`, `MAX_SEARCH_RESULTS_PER_QUERY=3`, `MAX_SCRAPER_WORKERS=2`, `BROWSE_CHUNK_MAX_LENGTH=2048`, `TOTAL_WORDS=400`, `CURATE_SOURCES=False`, token limits 1,024/2,048/1,024/512, `LLM_KWARGS={"max_tokens":2048,"max_retries":0}`, image generation off, MCP disabled. |
| Chat requests | Four requests, all `deepseek/deepseek-v4.1-flash-1017`, each with `max_completion_tokens: 2048` on the wire. Native temperatures 0.15–0.4 were sent; no reasoning effort was sent. |
| Retriever | Three PubMed Central searches with `retmax=3` (the original question plus generated queries) and full-text fetches. The query count is a target, not a ceiling. |
| Embeddings | None for this route: the keyword context filter applies without a jev key. |
| Native retries | GPT Researcher's own wrapper retries a failed chat request up to 10 times; the provider adapter override (`max_retries: 0`) is separate. RAH adds no retry. |
| Tokenizer | The native cost estimator loads `o200k_base` from `openaipublic.blob.core.windows.net` on first use. Guided runs share a private `TIKTOKEN_CACHE_DIR` under `apps/gptr/native/` and disclose this destination. |
| Output | One Markdown report in `outputs/` with YAML frontmatter including `sources_count`. |

Downstream Rapid and comparison reports use `research_report`; Deep uses `deep`. Each uses the selected quality's FAST/SMART/STRATEGIC/EMBEDDING mapping for that run only.

### Scope Gate

One non-streaming OpenRouter Chat Completions request per explicit round to `anthropic/claude-haiku-5.5` with `max_completion_tokens=2400`, a 45-second deadline and a 128 KiB response limit. No tools, temperature, reasoning effort, fallbacks or retries. The response must be the exact `questions` shape; anything else opens the editable generic questionnaire.

### Local Deep Research headless route (guided)

Guided LDR stages run `openrouter_observer.py -- ldr_guided.py` in the LDR venv, in a private attempt folder (`hub/runs/guided/<step>/`) with the RAH LDR projection, private `HOME`/`LDR_DATA_DIR`/temp directories, `PYTHONNOUSERSITE=1`, `PYTHON_DOTENV_DISABLED=1` and `NO_PROXY=*`. Stdin carries only the approved prompt. The runner calls `quick_summary(query=…, progress_callback=…)` (decorated `@no_db_settings`, so the encrypted native database is never opened), writes `outputs/report.md` unchanged and `RISExporter().export(...).content` as `outputs/citations.ris`, then `result.json` and `completion.json` (it is the only writer of both). `run_group` writes `process.json`; `ldr_pipeline.collect` applies the status precedence (cancelled, timeout, start failure, non-zero exit, verified success, otherwise `native_output`).

Settings basis: LDR defaults plus RAH's projected `LDR_*` values; settings saved in LDR's UI don't apply. Effective on 2.0.1: `search.search_strategy = langgraph-agent`, provider and model from RAH settings, `search.tool = pubmed` by default.

Installed probe (`tests/probe_ldr_headless.py`, LDR 2.0.1 wheel, sockets blocked, fake OpenRouter and PubMed at the transports): the runner completed, the report had `## Sources`, the RIS parsed with a usable row, observer lines equalled transport-level OpenRouter POSTs, no socket refusals and no writes outside the attempt folder. Callback shapes seen: `(str, None|int|float, dict)`. Fixed hosts recorded for the PubMed configuration (`ldr_pipeline.FIXED_HOSTS`): `eutils.ncbi.nlm.nih.gov` (search), `api.openalex.org` (OpenAlex enrichment), and LDR's journal-quality reference data from `openalex.s3.amazonaws.com`, `doaj.org` and `raw.githubusercontent.com` (downloaded into each run's private data directory, so on every headless run). Public web pages linked from results may be fetched from any host and are disclosed as a class; there is no runtime allow-list. An unlisted fixed host fails the probe.

### OpenRouter usage observer

`rah/openrouter_observer.py` runs inside each RAH-launched upstream process (guided GPT Researcher CLI, headless LDR, managed PaperQA2 worker, Classic managed GPT Researcher CLI). It wraps `Client.send`/`AsyncClient.send` of `httpx` and `httpx2` and observes only POSTs to `openrouter.ai` `/api/v1/{chat/completions,completions,embeddings,responses}`. Non-streamed bodies are parsed up to 16 MiB; streamed bodies are wrapped in a subclass of the module's own `SyncByteStream`/`AsyncByteStream` that yields the same chunks in order, delegates `close`/`aclose`, propagates exceptions and parses SSE lines with a 64 KiB buffer. One JSON line per request: endpoint, status, stream flag, generation ID, model, provider, `usage` and a reason (`aborted`, `unparsed`, `too_large`, `no_usage`, `no_response`). No content, headers or keys. In PaperQA2 the worker's tuple guard wraps the observer, so refused requests never reach it.

Probe evidence: `tests/probe_gptr_survey.py --observer --compare` (research_report and deep) produced identical request bodies, outputs and effective settings with and without the observer once the upstream's own clock values and run UUIDs are masked, with observer lines equal to the transport count, including one streamed request. `tests/probe_paper_transport.py --observer` passed for all three candidate routes (4 lines for 4 transport requests; an unapproved tuple refused before the observer and the transport). `usage.COVERAGE` remains empty until the owner adds entries from these reports.

### Open-access acquisition

The fixed fallback uses the PMC ID Converter (`/tools/idconv/api/v1/articles/`, DOI to PMCID only, with `tool=research-agent-hub` and a configured maintainer contact) and the PMC Cloud Service bucket (`pmc-oa-opendata.s3.amazonaws.com`: ListObjectsV2 for versions, per-version JSON metadata, then the metadata's `pdf_url`). Accepted records are in the PMC Open Access Subset with a Creative Commons or CC0 `license_code` and a same-bucket PDF; `TDM`, retracted, unknown-licence and identity mismatches stay in manual review. MD5 from the metadata URL is checked and SHA-256 is stored. Text parseability is checked with the installed `paperqa_pypdf.parse_pdf_to_pages` in a separate, network-blocked process; the P0 check rejected an empty-text page, an invalid file and an encrypted PDF.

## Managed PaperQA2 routes

The `openrouter-text-v1` profile (GPT-4.1 Mini via Azure) is retired from RAH-managed use. Its historical answers and BibTeX sidecars stay readable, and its earlier evidence remains attached to that route in [releases.md](releases.md).

| Quality | Route | Summary / agent / answer | Embeddings | Status |
| --- | --- | --- | --- | --- |
| Budget | `pqa-budget-v1` | Haiku / Haiku / Haiku | `openai/text-embedding-3-small` | Disabled candidate |
| Standard | `pqa-standard-v1` | Haiku / Terra / Terra | `openai/text-embedding-3-small` | Disabled candidate; must qualify on each release platform |
| Premium | `pqa-premium-v1` | Sonnet / Sonnet / Sonnet | `openai/text-embedding-3-small` | Disabled candidate |

Each role uses the official OpenRouter Chat Completions or Embeddings endpoint, exactly one provider and `allow_fallbacks: false`. The worker refuses any request whose URL, model or provider differs from the approved tuple, any redirect and any model list. The installed pinned clients (paper-qa 2026.8.12, fhlmi 0.46.2, fhaviary 0.37.0, LiteLLM 1.84.1, pypdf 7.1.1) serialized the restriction for all three routes in the offline transport probe, including agent tool requests and embeddings. Provider tuples are chosen and approved during P6; a route becomes available only when its evidenced platform identity is added to `rah/pqa_profiles.py`. The PaperQA2 lock files are unchanged.
