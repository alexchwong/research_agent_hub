# Workflows

## Guided recipes (recipe version 1)

Every guided request starts with an explicitly confirmed public survey and a questionnaire-only Scope Gate. The user approves the exact public prompt. RAH then follows the fixed positions below; no model decides the next step.

| Purpose | Fixed downstream steps | Completion | Human checkpoints |
| --- | --- | --- | --- |
| Literature review | `ldr_discovery` → `papers` → `paper_analysis` | Validated RIS plus a cited PaperQA2 answer from the selected PDFs. **Finish with discovery only** after RIS intake is a partial outcome. | Start in LDR after reviewing its settings; confirm the LDR result; review citations; pick PDFs; review the transfer. |
| Deep research | `gptr_deep` (deep) → `papers` → `paper_analysis` | The sourced deep report. **Finish with report** skips the optional paper work and transfer. | Scope approval; optional paper collection; transfer review. |
| Rapid research | `gptr_rapid` (research_report) | The scoped report, or **Use survey as result**. | Scope approval. |
| Independent corroboration | `ldr_comparison` → `gptr_comparison` (research_report) | Two independently labelled original reports on a shared framing, side by side. | Start in LDR; confirm its Markdown result. |
| Analyse my papers | `paper_selection` → `paper_analysis` | A cited PaperQA2 answer for the exact approved subset. Declining the transfer cannot complete this recipe. | Public topic for the survey; private question and excerpt only at the PaperQA2 stage; transfer review. |

Resume a stopped run from its stage card: **Re-confirm recipients** after a route change or restart, **Reopen LDR handoff** to save an existing LDR result, **Reuse verified output** for an interrupted stage whose native output was proven complete, or **Rerun this stage…** after acknowledging possible earlier charges. Original outputs stay in the chat with their roles and lineage; **Download named** gives predictable file names.

## Classic recipes

| Workflow | Actions and outputs |
|---|---|
| Academic literature survey | Start with a literature question in LDR’s native academic search. Attach its report and RIS export. Review citations in Papers Needed and obtain PDFs with your own access. Select a subset of the PDFs and review the PaperQA2 destinations. Reopen the original reports and citations in the project. |
| Deep research to paper analysis | Use GPT Researcher’s native Deep Research with a public question. Retain the original cited report. Add its citations and obtain the relevant full texts. Continue in Papers Needed. |
| Broad-to-deep investigation | Run LDR broad search. Use its original report to write a focused, public question for GPT Researcher’s Deep Research mode. Attach both reports. |
| Independent report comparison | Run the public question separately in LDR and GPT Researcher. Attach and compare the original reports side by side in the project. |

Model, embedding and search providers can charge. OpenRouter Usage covers the shared key's reports. Managed PaperQA2 builds a fresh index for the approved PDFs, with separate confirmation for each run.

In the workspace, use project **Files** and **Papers Needed** to collect and review original outputs. When you are ready to ask PaperQA2, choose it in the composer and attach the desired PDFs directly or select saved project PDFs. Open **Workflows** for all four recipes.
