"use strict";

// Guided research: purpose + quality composer, public start review, stage cards and checkpoints.
// All model-generated labels are inserted as text nodes. Reports render through the existing safe renderer.

const stepLabels = {
  survey: "Survey · GPT Researcher", scope_gate: "Scope questions · RAH", ldr_discovery: "Discovery · Local Deep Research",
  papers: "Papers Needed", paper_analysis: "PDF analysis · PaperQA2", gptr_deep: "Deep research · GPT Researcher",
  gptr_rapid: "Rapid report · GPT Researcher", ldr_comparison: "Report A · Local Deep Research",
  gptr_comparison: "Report B · GPT Researcher", paper_selection: "Choose my papers",
};
const qualityLabels = {budget: "Budget", standard: "Standard", premium: "Premium"};
const liveRegion = (() => {
  const node = document.createElement("p");
  node.className = "visually-hidden";
  node.setAttribute("role", "status");
  node.setAttribute("aria-live", "polite");
  document.body.append(node);
  return node;
})();
const announced = new Map();

function guidedDraft(current) {
  const defaults = workspace.guided?.default || {purpose: "literature_review", profile: "standard"};
  current.guided ||= {purpose: defaults.purpose, profile: defaults.profile, question: ""};
  return current.guided;
}

function pipelineBase(scope, chat) { return projectPath(scope) + "/chats/" + chat + "/pipelines"; }

function activeRun() {
  return conversation?.pipeline_runs?.find(run => !["succeeded", "failed", "cancelled"].includes(run.state));
}

function renderGuidedComposer(current) {
  const state = guidedDraft(current);
  const guided = workspace.guided;
  const tools = element("div");
  tools.className = "composer-tools guided-tools";
  const purpose = select(tools, "Purpose", guided.purposes.map(item => [item.id, item.label]));
  purpose.value = state.purpose;
  purpose.onchange = () => { state.purpose = purpose.value; renderComposer(); composer.querySelector("select")?.focus(); };
  const qualityBox = element("div");
  qualityBox.className = "quality-control";
  const quality = select(qualityBox, "Research quality", guided.profiles.qualities.map(item => {
    const pdf = guided.pdf_analysis[item.id];
    const needsPdf = ["literature_review", "deep_research", "analyse_my_papers"].includes(state.purpose);
    return [item.id, item.label + (needsPdf && pdf && !pdf.available ? " · PDF analysis unavailable" : "")];
  }));
  quality.value = state.profile;
  quality.onchange = () => { state.profile = quality.value; renderComposer(); composer.querySelector(".quality-control select")?.focus(); };
  tools.append(qualityBox);
  const classic = button("Classic apps", () => { current.mode = "classic"; renderComposer(); });
  classic.className = "link-button";
  tools.append(classic);
  const field = element("textarea");
  field.className = "question";
  const label = state.purpose === "analyse_my_papers" ? "Public topic for survey" : "Public research question";
  field.setAttribute("aria-label", label);
  field.placeholder = label + (state.purpose === "analyse_my_papers" ? " (your private question comes later)" : "");
  field.maxLength = 4000;
  field.rows = 3;
  field.value = state.question;
  field.oninput = () => { state.question = field.value; };
  composer.append(tools, field);
  const footer = element("div");
  footer.className = "composer-footer";
  const run = activeRun();
  const pdf = guided.pdf_analysis[state.profile];
  const notes = [];
  if (["literature_review", "deep_research", "analyse_my_papers"].includes(state.purpose) && pdf && !pdf.available) {
    notes.push("Managed PDF analysis is unavailable for " + qualityLabels[state.profile] + " here; you can still finish with a partial outcome or use native PaperQA2.");
  }
  const hint = element("p", run ? "Guided research is active in this chat. Cancel or finish it before starting another."
    : notes[0] || "Public questions only. You review the survey recipients before anything is sent.");
  footer.append(hint);
  const start = button("Review public start…", async () => {
    if (!field.value.trim()) { field.focus(); throw new Error("Enter a " + label.toLowerCase() + "."); }
    let chat = locationState.chat;
    let scope = destination();
    if (!chat) {
      const created = await api("/chats", {project_id: locationState.project});
      drafts.set(created.id, current);
      chat = created.id;
      scope = created.scope_id;
      await route(locationState.project, chat);
    }
    await publicStart(scope, chat, state);
  });
  start.className = "primary";
  start.disabled = Boolean(run);
  footer.append(start);
  composer.append(footer);
}

function definition(list, pairs) {
  for (const [term, value] of pairs) {
    if (value === undefined || value === null || value === "") continue;
    list.append(element("dt", term), element("dd", String(value)));
  }
  return list;
}

function routeDetails(route, title = "Recipients and models") {
  const details = element("details");
  details.append(element("summary", title));
  const list = element("dl");
  definition(list, [["App", route.app + (route.mode ? " · " + route.mode : "")], ["Version", route.version],
    ["Search", route.search], ["Deadline", route.deadline_seconds ? route.deadline_seconds / 60 + " minutes" : null],
    ["Reasoning effort", route.effort]]);
  for (const [role, model] of Object.entries(route.models || {})) list.append(element("dt", role), element("dd", model));
  if (route.controls) for (const [name, value] of Object.entries(route.controls)) list.append(element("dt", name.replaceAll("_", " ")), element("dd", String(value)));
  details.append(list);
  if (route.destinations) {
    const items = element("ul");
    items.append(...route.destinations.map(text => element("li", text)));
    details.append(element("p", "Destinations"), items);
  }
  if (route.attestation) details.append(element("p", route.attestation));
  if (route.configured) details.append(element("p", "Configured LDR settings · " + Object.entries(route.configured).map(([k, v]) => k + ": " + v).join(", ")));
  if (route.relaunch_needed) details.append(element("p", "The running LDR process has older settings. Restart it from App details to apply saved settings."));
  return details;
}

async function publicStart(scope, chat, state) {
  const preview = await api(pipelineBase(scope, chat) + "/preview", {purpose: state.purpose, profile: state.profile, question: state.question});
  const {dialog, act, show} = modal("Start " + preview.purpose_label + " · " + qualityLabels[preview.profile]);
  dialog.append(element("p", "RAH will send this exact public question to a short GPT Researcher survey, then ask the Scope Gate for a few scope questions."),
    element("blockquote", preview.question), routeDetails(preview.survey, "Survey recipients"));
  const gate = element("details");
  gate.append(element("summary", "Scope Gate"));
  if (preview.scope_gate.available) gate.append(element("p", "OpenRouter · " + preview.scope_gate.model + " · " + preview.scope_gate.provider),
    element("p", preview.scope_gate.sends), element("p", "One request per round, no tools, no retries, " + preview.scope_gate.deadline_seconds + "-second deadline."));
  else gate.append(element("p", preview.scope_gate.reason));
  const prices = element("details");
  prices.append(element("summary", "Advertised prices"), element("p", preview.pricing.note));
  for (const [model, price] of Object.entries(preview.pricing.models)) {
    prices.append(element("p", model + " · " + (price ? "$" + (price.input ?? "?") + " in / $" + (price.output ?? "?") + " out per 1M tokens" + (price.stale ? " · stale" : "") : "unknown")));
  }
  dialog.append(gate, prices, element("p", preview.notice));
  const confirmBox = control(dialog, "This question is public and safe to send", "input", "checkbox");
  show(act("Cancel", () => dialog.close()), act("Confirm and start survey", async () => {
    if (!confirmBox.checked) throw new Error("Confirm that the question is public and safe to send.");
    const request = state.request && state.request.digest === preview.digest ? state.request.id : crypto.randomUUID();
    state.request = {id: request, digest: preview.digest};
    await api(pipelineBase(scope, chat), {request_id: request, purpose: preview.purpose, profile: preview.profile,
      question: preview.question, confirmation_digest: preview.digest, public_confirmed: true});
    state.question = "";
    state.request = null;
    dialog.close();
    await refreshConversation();
  }, true));
}

async function act(run, action, data = {}, step) {
  const path = pipelineBase(run.scope_id, run.chat_id) + "/" + run.id + "/actions";
  try {
    const result = await api(path, {action, action_id: crypto.randomUUID(), expected_revision: run.revision, step_id: step || null, data});
    return result;
  } finally {
    await refreshConversation();
  }
}

function announce(run) {
  const label = run.purpose_label + ": " + run.display;
  if (announced.get(run.id) !== label) {
    if (announced.has(run.id)) liveRegion.textContent = label;
    announced.set(run.id, label);
  }
}

async function renderRun(container, run, lookup) {
  const block = element("section");
  block.className = "guided-run";
  block.setAttribute("aria-label", run.purpose_label + " revision " + run.brief_revision);
  const header = element("div");
  header.className = "turn-meta";
  const badge = element("span", run.display + (run.outcome && run.outcome !== "complete" ? " · " + run.outcome.replaceAll("_", " ") : ""));
  badge.dataset.state = run.display;
  header.append(element("strong", run.purpose_label), element("span", qualityLabels[run.selected_profile]),
    element("span", "Revision " + run.brief_revision), badge);
  const prompt = element("p", run.original_question);
  prompt.className = "prompt";
  block.append(header, prompt);
  announce(run);
  if (run.purpose === "independent_corroboration" && run.brief) {
    const framing = element("details");
    framing.append(element("summary", "Shared framing"), element("blockquote", run.brief.public_prompts?.ldr_comparison || run.brief.approved_prompt || ""));
    block.append(framing);
  }
  const comparison = run.purpose === "independent_corroboration" ? element("div") : null;
  if (comparison) comparison.className = "comparison";
  const current = run.steps.findLast(step => step.current) || run.steps.at(-1);
  for (const step of run.steps) {
    if (!step.current && step.state === "skipped") continue;
    const card = await stageCard(run, step, step === current, lookup);
    if (comparison && ["ldr_comparison", "gptr_comparison"].includes(step.step_key) && step.state === "succeeded") comparison.append(card);
    else block.append(card);
  }
  if (comparison?.childElementCount) block.append(comparison);
  const tools = element("div");
  tools.className = "sub-actions";
  if (run.actions.includes("pause")) tools.append(button("Pause after this step", () => act(run, "pause")));
  if (run.actions.includes("resume")) tools.append(button("Resume", () => act(run, "resume")));
  if (run.actions.includes("cancel")) tools.append(button("Cancel research", async () => {
    if (!confirm("Cancel this guided research? Saved results are kept.")) return;
    const result = await act(run, "cancel");
    if (result.finishing) say("Finishing current step. Automatic continuation is discarded.");
  }));
  if (run.state === "succeeded" || run.state === "cancelled" || run.state === "failed") tools.append(...followUps(run, lookup));
  block.append(tools);
  container.append(block);
}

function followUps(run) {
  const actions = [];
  const answer = run.steps.findLast(step => step.step_key === "paper_analysis" && step.state === "succeeded");
  const corpus = answer ? run.steps.findLast(step => step.detail?.corpus)?.detail.corpus?.pdf_ids || [] : [];
  const current = draft();
  const start = (purpose, question = "") => {
    current.mode = "guided";
    current.guided = {purpose, profile: run.selected_profile, question};
    renderComposer();
    composer.querySelector("textarea")?.focus();
  };
  if (corpus?.length) actions.push(button("Reuse corpus", () => { start("analyse_my_papers"); current.guided.corpus = [...corpus]; }));
  actions.push(button("Seek new literature", () => start("literature_review")));
  actions.push(button("Revise scope", () => start(run.purpose, run.original_question)));
  return actions;
}

async function outputList(run, step, lookup) {
  const box = element("div");
  box.className = "stage-outputs";
  for (const output of step.outputs) {
    const row = element("div");
    row.className = "sub-actions";
    const name = output.title || output.original_name;
    row.append(element("span", name + " · " + output.producer + " · " + (output.origin_class || "").replaceAll("_", " ")));
    const item = lookup.get(output.id) || {...output, original_name: output.original_name};
    row.append(button("Download", () => download({id: run.scope_id}, item)));
    row.append(button("Download named", async () => {
      const response = await api(fileURL(run.scope_id, output.id) + "?named=1", undefined, true);
      const disposition = response.headers.get("Content-Disposition") || "";
      const filename = /filename="?([^";]+)"?/.exec(disposition)?.[1] || output.original_name;
      const url = URL.createObjectURL(await response.blob());
      const link = element("a");
      link.href = url;
      link.download = filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }));
    box.append(row);
    if (["research_report", "survey", "discovery_report", "answer", "native_result"].includes(output.role) && output.kind !== "citations"
        && /\.md$/i.test(output.original_name)) {
      const details = element("details");
      details.open = output.role !== "survey" && step.step_key !== "survey";
      details.append(element("summary", "Show original"));
      details.addEventListener("toggle", async () => {
        if (!details.open || details.dataset.loaded) return;
        details.dataset.loaded = "1";
        const response = await api(fileURL(run.scope_id, output.id), undefined, true);
        details.append(safeResult(await (await response.blob()).slice(0, 512 * 1024).text()));
      });
      if (details.open) details.dispatchEvent(new Event("toggle"));
      box.append(details);
    }
  }
  return box;
}

async function stageCard(run, step, isCurrent, lookup) {
  const card = element("article");
  card.className = "stage-card";
  const head = element("div");
  head.className = "turn-meta";
  const badge = element("span", step.display);
  badge.dataset.state = step.display;
  head.append(element("strong", stepLabels[step.step_key] || step.step_key), badge);
  if (step.attempt > 1) head.append(element("span", "Attempt " + step.attempt));
  if (step.round) head.append(element("span", "Round " + step.round + (step.questionnaire_source === "fallback" ? " · editable generic questions" : "")));
  card.append(head);
  const facts = element("dl");
  facts.className = "facts";
  const detail = step.detail || {};
  const count = detail.sources_count;
  definition(facts, [["Native sources", count === undefined ? null : count === null ? "Unknown" : count],
    ["Advertised native cost", detail.total_cost_usd ? "$" + detail.total_cost_usd + " (native estimate)" : null],
    ["Scope Gate model", detail.observed?.model], ["Scope Gate provider", detail.observed ? detail.observed.provider || "Unobserved" : null],
    ["Scope Gate cost", detail.observed?.cost ? "$" + detail.observed.cost : null],
    ["Native result", step.native_result_id && step.app === "ldr" ? step.native_result_id + (detail.verified === false ? " · unverified" : "") : null],
    ["Received", detail.received?.join(", ")]]);
  const profile = workspace.guided.profiles.qualities.find(item => item.id === run.selected_profile);
  const models = {survey: profile?.roles.survey, scope_gate: profile?.roles.scope_gate,
    gptr_deep: profile?.roles.gptr_smart, gptr_rapid: profile?.roles.gptr_smart, gptr_comparison: profile?.roles.gptr_smart,
    paper_analysis: profile && [profile.roles.pqa_summary, profile.roles.pqa_agent, profile.roles.pqa_answer].join(" / ")}[step.step_key];
  definition(facts, [["Configured models", models], ["PaperQA2 route", step.step_key === "paper_analysis" ? profile?.pqa_route : null]]);
  if (facts.childElementCount) card.append(facts);
  if (run.brief?.routes?.[step.step_key]) card.append(routeDetails(run.brief.routes[step.step_key], "Approved recipients"));
  if (step.error_detail && !["succeeded", "skipped"].includes(step.state)) card.append(element("p", step.error_detail));
  if (step.detail?.coverage) card.append(coverage(step.detail.coverage));
  if (step.outputs.length) card.append(await outputList(run, step, lookup));
  if (isCurrent && !["succeeded", "cancelled", "skipped"].includes(run.state)) {
    try { await stageActions(card, run, step, lookup); }
    catch (error) { card.append(element("p", error.message)); }
  }
  return card;
}

function coverage(data) {
  const box = element("details");
  box.open = true;
  box.append(element("summary", "PDF coverage"));
  const list = element("dl");
  definition(list, [["PDFs submitted", data.submitted?.join(", ")],
    ["Full text cited", data.cited === null ? "Unknown (not observable)" : data.cited?.join(", ") || "None observed"],
    ["Reviewed", data.reviewed === null ? "Unknown (not observable)" : data.reviewed?.join(", ")],
    ["Abstract or citation only", data.abstract_only], ["Papers not obtained", data.not_obtained]]);
  box.append(list, element("p", "A submitted PDF is not evidence that PaperQA2 selected passages from it."));
  return box;
}

async function stageActions(card, run, step, lookup) {
  const fresh = await api(pipelineBase(run.scope_id, run.chat_id) + "/" + run.id);
  const actions = fresh.actions;
  const row = element("div");
  row.className = "sub-actions";
  card.append(row);
  if (fresh.route_problem) card.append(element("p", fresh.route_problem.message));
  if (step.state === "reconfirm_recipients" && fresh.reconfirm) {
    card.append(routeDetails(fresh.reconfirm.route, "Current recipients"));
    row.append(button("Re-confirm recipients", () => act(fresh, "reconfirm_route", {digest: fresh.reconfirm.digest}, step.id)));
  }
  if (actions.includes("rerun")) row.append(button("Rerun this stage…", async () => {
    if (!confirm("An earlier attempt may already have incurred charges. Start a new attempt?")) return;
    await act(fresh, "rerun", {confirm_charges: true}, step.id);
  }));
  if (actions.includes("reuse_output") && fresh.recovery) row.append(button("Reuse verified output", () =>
    act(fresh, "reuse_output", {digest: fresh.recovery.digest}, step.id)));
  if (step.step_key === "scope_gate" && step.state === "awaiting_scope") scopeForm(card, fresh, step);
  if (["ldr_discovery", "ldr_comparison"].includes(step.step_key) && ["waiting_native", "paused"].includes(step.state)) nativeStage(card, fresh, step);
  if (["papers", "paper_selection"].includes(step.step_key) && step.state === "awaiting_papers") papersStage(card, fresh, step, lookup);
  if (step.step_key === "paper_analysis" && step.state === "awaiting_disclosure") {
    row.append(button("Review PDF transfer", async () => {
      const entry = conversation.entries.find(item => item.id === step.entry_id);
      const info = await api(projectPath(run.scope_id) + "/pqa-preview", {entry_id: step.entry_id, pdfs: entry.input_ids});
      paperReview({...entry, project_id: run.scope_id}, info);
    }));
    row.append(button("Open native PaperQA2 instead", () => tool("apps")));
  }
  if (actions.includes("finish_partial")) row.append(button(run.purpose === "literature_review" ? "Finish with discovery only" : "Finish with report",
    () => act(fresh, "finish_partial", {}, step.id)));
}

function scopeForm(card, run, step) {
  const form = element("form");
  form.className = "scope-form";
  form.onsubmit = event => event.preventDefault();
  const answers = {};
  for (const questionnaire of run.questionnaires || []) {
    if (questionnaire.error) form.append(element("p", "Scope Gate " + (questionnaire.round === 2 ? "round 2 " : "") + "unavailable: " + questionnaire.error.message));
    for (const question of questionnaire.questions) {
      const group = element("fieldset");
      group.append(element("legend", question.question + (question.selection === "multiple" ? " (choose any)" : "")));
      const entry = answers[question.id] = {selected: [], custom: "", custom_mode: "replace"};
      for (const option of question.options) {
        const box = control(group, option.label, "input", question.selection === "multiple" ? "checkbox" : "checkbox");
        box.onchange = () => {
          if (question.selection === "single" && box.checked) {
            for (const other of group.querySelectorAll("input[type=checkbox]")) if (other !== box && other.dataset.option) other.checked = false;
          }
          entry.selected = [...group.querySelectorAll("input[data-option]")].filter(input => input.checked).map(input => input.dataset.option);
        };
        box.dataset.option = option.id;
      }
      const custom = control(group, "Custom answer (optional)");
      custom.maxLength = 400;
      custom.oninput = () => { entry.custom = custom.value; };
      const mode = select(group, "Custom text", [["replace", "Replace selected choice(s)"], ["add", "Add to selected choice(s)"]]);
      mode.onchange = () => { entry.custom_mode = mode.value; };
      form.append(group);
    }
  }
  const instructions = control(form, "Additional public instructions (optional)", "textarea");
  instructions.maxLength = 1500;
  const broad = control(form, "Use broad scope (send the original question unchanged)", "input", "checkbox");
  const preview = element("div");
  preview.setAttribute("aria-live", "polite");
  const buttons = element("div");
  buttons.className = "sub-actions";
  const payload = () => ({answers: Object.fromEntries(Object.entries(answers).filter(([, value]) => value.selected.length || value.custom.trim())),
    instructions: instructions.value, broad: broad.checked});
  buttons.append(button("Preview scope", async () => {
    const result = await api(pipelineBase(run.scope_id, run.chat_id) + "/" + run.id + "/actions",
      {action: "preview_scope", action_id: crypto.randomUUID(), expected_revision: run.revision, step_id: step.id, data: payload()});
    preview.replaceChildren(element("h3", "Exact prompt to send (" + result.length + " / " + result.limit + " characters)"), element("blockquote", result.prompt));
    if (result.constraints.length) {
      const list = element("ul");
      list.append(...result.constraints.map(item => element("li", item.question + " — " + item.answer)));
      preview.append(element("p", "Pending constraints"), list);
    }
    for (const [key, route] of Object.entries(result.routes)) preview.append(routeDetails(route, (stepLabels[key] || key) + " recipients"));
    if (result.private_stage) preview.append(element("p", result.private_stage));
    const review = result.needs_conflict_review ? control(preview, "I reviewed these constraints for conflicts", "input", "checkbox") : null;
    preview.append(button("Approve scope", async () => {
      if (review && !review.checked) throw new Error("Confirm that you reviewed the constraints for conflicts.");
      await act(run, "approve_scope", {digest: result.digest, constraints_reviewed: Boolean(review?.checked)}, step.id);
    }));
    preview.querySelector("button")?.focus();
  }));
  if (run.actions.includes("ask_more")) buttons.append(button("Ask more questions", () =>
    act(run, "ask_more", {round_id: "r1", answers: payload().answers}, step.id)));
  if (run.actions.includes("use_survey_result")) buttons.append(button("Use survey as result", () => act(run, "use_survey_result", {}, step.id)));
  form.append(buttons, preview);
  card.append(form);
}

function nativeStage(card, run, step) {
  const route = run.native_route;
  const box = element("div");
  if (route) {
    box.append(element("p", "Approved prompt for LDR"), element("blockquote", route.prompt), routeDetails(route, "LDR route"));
    const attest = control(box, "I will review the model and search settings inside LDR before pressing Start", "input", "checkbox");
    const open = async reopen => {
      if (!attest.checked) throw new Error("Confirm that you will review the LDR settings before starting.");
      let state = await api("/apps/ldr/launch", {ensure: true});
      for (let i = 0; i < 175 && state.state !== "Ready"; ++i) {
        await new Promise(resolve => setTimeout(resolve, 700));
        state = await api("/apps/ldr/browser");
        if (["Exited", "Stopped"].includes(state.state)) throw new Error("Local Deep Research stopped. Check App details.");
      }
      const result = await act(run, "launch_native", {route_digest: route.digest, attest: true, reopen}, step.id);
      if (result.native?.manual) say(result.native.manual);
    };
    const sub = element("div");
    sub.className = "sub-actions";
    sub.append(button(step.detail?.opened_at ? "Open LDR again" : "Review and start in LDR", () => open(false)),
      button("Reopen LDR handoff", () => open(true)), button("Copy prompt", () => navigator.clipboard.writeText(route.prompt)));
    box.append(sub);
  }
  const received = step.outputs;
  if (received.length) {
    const choose = element("fieldset");
    choose.append(element("legend", "Received for this stage"));
    const boxes = received.map(output => {
      const input = control(choose, (output.title || output.original_name) + " · " + output.output_key, "input", "checkbox");
      input.checked = true;
      return [output, input];
    });
    const publicBox = control(choose, "These came from the approved public LDR research and contain public citations", "input", "checkbox");
    const manual = received.filter(output => output.role === "native_result");
    const same = manual.length === 2 ? control(choose, "Both imported files belong to the same LDR result", "input", "checkbox") : null;
    choose.append(button("Use this LDR result", () => act(run, "confirm_native_result", {artifact_ids: boxes.filter(([, input]) => input.checked).map(([output]) => output.id),
      attribution: true, public_provenance: publicBox.checked, ...(same ? {same_result: same.checked} : {})}, step.id)));
    box.append(choose);
  }
  const manual = element("div");
  manual.className = "sub-actions";
  fileControl(manual, "Import LDR export", false, ".md,.ris", async picked => {
    if (!picked.length) return;
    if (!confirm("Import this file as a user-imported LDR export for this stage? Its native result ID stays unverified.")) return;
    const form = new FormData();
    form.set("file", picked[0]);
    form.set("guided_step_id", step.id);
    form.set("chat_id", run.chat_id);
    form.set("native_acknowledged", "true");
    await api(projectPath(run.scope_id) + "/files", form);
    await refreshConversation();
  });
  box.append(manual);
  card.append(box);
}

function papersStage(card, run, step, lookup) {
  const papers = run.papers || {rows: [], suggestions: []};
  const box = element("div");
  box.className = "papers-stage";
  if (step.step_key === "papers" && papers.settings) {
    if (papers.suggestions.length && !papers.rows.length) {
      const table = element("fieldset");
      table.append(element("legend", "Suggested citations (" + papers.suggestions.length + ")"));
      const picks = papers.suggestions.map((item, index) => {
        const label = [item.title || item.note || "Untitled", item.doi && "DOI " + item.doi, item.pmid && "PMID " + item.pmid, item.pmcid].filter(Boolean).join(" · ");
        const input = control(table, label, "input", "checkbox");
        input.checked = true;
        return [item, index, input];
      });
      table.append(button("Save reviewed citations", () => act(run, "set_papers", {source_artifact_id: papers.source_artifact_id,
        rows: picks.map(([item, index, input]) => ({...item, accept: input.checked, suggestion: index}))}, step.id)));
      box.append(table);
    } else if (!papers.rows.length) {
      box.append(element("p", "No verified references section or RIS rows were found. Add papers manually."));
    }
    const manual = element("details");
    manual.append(element("summary", "Add a paper manually"));
    const fields = Object.fromEntries([["title", "Title"], ["doi", "DOI"], ["pmid", "PMID"], ["pmcid", "PMCID"]].map(([key, label]) => [key, control(manual, label)]));
    manual.append(button("Add paper", () => act(run, "set_papers", {source_artifact_id: null,
      rows: [{...Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.value || null])), accept: true}]}, step.id)));
    box.append(manual);
    if (papers.rows.length) {
      const list = element("div");
      list.className = "paper-rows";
      for (const row of papers.rows) {
        const item = element("div");
        item.className = "paper-row";
        item.append(element("strong", row.title || row.doi || row.pmcid || row.note || "Paper"),
          element("span", " · " + row.state + " · " + row.access_status.replaceAll("_", " ") + " · " + row.origin_class.replaceAll("_", " ")));
        const last = row.acquisitions.at(-1);
        if (last) item.append(element("p", last.outcome.replaceAll("_", " ") + (last.error_code ? " · " + last.error_code.replaceAll("_", " ") : "")
          + (last.licence ? " · " + last.licence : "") + (last.version ? " · version " + last.version : "")));
        for (const link of row.links) item.append(document.createTextNode(" "), external(link.label, link.url));
        if (row.origin_class !== "public_native" && row.access_status !== "oa_acquired" && (row.doi || row.pmcid)) {
          const preview = run.lookup_previews?.[row.id];
          if (preview) item.append(button("Review private lookup…", () => privateLookup(run, row, preview)));
        }
        list.append(item);
      }
      box.append(list);
      const tools = element("div");
      tools.className = "sub-actions";
      if (!papers.settings.oa_auto) tools.append(element("span", "Automatic open-access download is off in Settings."));
      tools.append(button("Obtain open-access PDFs (first 20 public)", () => act(run, "start_acquisition", {paper_ids: null}, step.id)));
      for (const [fmt, label] of [["csv", "Export requests CSV"], ["ris", "Export RIS"], ["ids", "Export identifiers"]]) {
        tools.append(button(label, async () => {
          const response = await api(pipelineBase(run.scope_id, run.chat_id) + "/" + run.id + "/papers/export?format=" + fmt, undefined, true);
          const url = URL.createObjectURL(await response.blob());
          const link = element("a");
          link.href = url;
          link.download = /filename="?([^";]+)"?/.exec(response.headers.get("Content-Disposition") || "")?.[1] || "requests";
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }));
      }
      box.append(tools);
    }
  }
  box.append(corpusPicker(run, step, lookup));
  card.append(box);
}

function privateLookup(run, row, preview) {
  const {dialog, act: dialogAct, show} = modal("Private identifier lookup");
  if (preview.blocked) {
    dialog.append(element("p", preview.blocked + ". Add a maintainer contact under Settings → Guided research to enable DOI conversion."));
    show(dialogAct("Close", () => dialog.close()));
    return;
  }
  const sends = element("ul");
  sends.append(...preview.sends.map(value => element("li", value)));
  const destinations = element("ul");
  destinations.append(...preview.destinations.map(value => element("li", value)));
  dialog.append(element("p", "This identifier was typed, pasted or extracted privately. Approve one " +
    (preview.stage === "converter" ? "PMC ID Converter request. You review the Cloud Service step separately afterwards." : "PMC Cloud Service lookup.")),
    element("p", "Sends"), sends, element("p", "Destinations"), destinations, element("p", preview.notice));
  const decide = approve => dialogAct(approve ? "Approve lookup" : "Decline", async () => {
    await act(run, "approve_private_lookup", {paper_id: row.id, stage: preview.stage, digest: preview.digest, approve});
    dialog.close();
  }, approve);
  show(decide(false), decide(true));
}

function corpusPicker(run, step, lookup) {
  const box = element("fieldset");
  box.append(element("legend", "PDFs for PaperQA2 (exact ordered subset)"));
  const allPdfs = [...lookup.values()].filter(item => item.kind === "pdf");
  const named = run.scope_id !== run.chat_id;
  let scopeFilter = "chat";
  let pdfs = allPdfs.filter(item => item.chat_id === run.chat_id);
  if (named) {
    const filter = select(box, "Show PDFs from", [["chat", "This chat"], ["project", "This project"]]);
    filter.onchange = () => {
      scopeFilter = filter.value;
      pdfs = scopeFilter === "chat" ? allPdfs.filter(item => item.chat_id === run.chat_id) : allPdfs;
      refresh();
    };
  }
  const preset = draft().guided?.corpus || [];
  const chosen = new Set(preset.filter(id => lookup.has(id)));
  if (preset.length > chosen.size) box.append(element("p", (preset.length - chosen.size) + " previously used PDF(s) are no longer available and were not substituted."));
  const list = element("div");
  function refresh() {
    list.replaceChildren(...pdfs.map(item => {
      const label = element("label");
      const input = element("input");
      input.type = "checkbox";
      input.checked = chosen.has(item.id);
      input.onchange = () => { if (input.checked) chosen.add(item.id); else chosen.delete(item.id); };
      const producer = {ldr: "Local Deep Research", gptr: "GPT Researcher", pqa: "PaperQA2"}[item.app_id] || (item.step_id ? "RAH" : "Imported");
      label.append(input, document.createTextNode(" " + (item.title ? item.title + " · " : "") + item.original_name + " · " + producer + " · "
        + (item.origin_class || "private or unknown").replaceAll("_", " ") + (item.role ? " · " + item.role.replaceAll("_", " ") : "")
        + " · " + new Date(item.created_at).toLocaleString()));
      return label;
    }));
    if (!pdfs.length) list.append(element("p", scopeFilter === "chat" ? "No saved PDFs in this chat yet." : "No saved PDFs in this project yet."));
  }
  refresh();
  box.append(list);
  const sources = element("div");
  sources.className = "sub-actions";
  fileControl(sources, "From computer", true, ".pdf", async picked => {
    for (const file of picked) {
      const form = new FormData();
      form.set("file", file);
      form.set("kind", "pdf");
      form.set("chat_id", run.chat_id);
      const result = await api(projectPath(run.scope_id) + "/files", form);
      chosen.add(result.artifact_id);
    }
    await refreshConversation();
  });
  sources.append(button("Import PDFs from folder…", async () => {
    const selection = await api(pipelineBase(run.scope_id, run.chat_id) + "/" + run.id + "/folder", {});
    if (!selection.selection_id) return;
    if (!selection.files.length) throw new Error("No top-level PDFs were found in that folder.");
    const {dialog, act: dialogAct, show} = modal("Import PDFs from folder");
    const boxes = selection.files.map(file => [file, control(dialog, file.name + " · " + Math.ceil(file.size / 1024) + " KiB", "input", "checkbox")]);
    show(dialogAct("Cancel", () => dialog.close()), dialogAct("Import selected", async () => {
      const ids = boxes.filter(([, input]) => input.checked).map(([file]) => file.id);
      const result = await api(pipelineBase(run.scope_id, run.chat_id) + "/" + run.id + "/folder/import", {selection_id: selection.selection_id, file_ids: ids});
      for (const item of result.imported) chosen.add(item.artifact_id);
      dialog.close();
      await refreshConversation();
    }, true));
  }), button("Open chat folder", () => api(projectPath(run.scope_id) + "/open-folder", {chat_id: run.chat_id})));
  if (availableCorpus.length) sources.append(button("Use last corpus", () => {
    const missing = availableCorpus.filter(item => !lookup.has(item.id)).length;
    for (const item of availableCorpus) if (lookup.has(item.id)) chosen.add(item.id);
    refresh();
    if (missing) say(missing + " PDF(s) from the last corpus are unavailable and were not substituted.");
  }));
  box.append(sources);
  const question = control(box, "Private PaperQA2 analysis question", "textarea");
  question.maxLength = 8000;
  const approved = run.brief?.approved_prompt;
  if (approved) box.append(button("Use approved public question", () => { question.value = run.brief.approved_prompt; }));
  const excerpt = control(box, "Optional excerpt (sent only to PaperQA2 after review)", "textarea");
  excerpt.maxLength = 8000;
  const routeId = {budget: "pqa-budget-v1", standard: "pqa-standard-v1", premium: "pqa-premium-v1"}[run.selected_profile];
  const pdf = workspace.guided.pdf_analysis[run.selected_profile];
  box.append(element("p", "PaperQA2 route · " + routeId + (pdf?.available ? "" : " · currently unavailable on this platform")));
  box.append(button("Use these PDFs", () => {
    if (!question.value.trim()) throw new Error("Enter the private analysis question.");
    const ordered = pdfs.map(item => item.id).filter(id => chosen.has(id));
    return act(run, "select_corpus", {pdf_ids: ordered, analysis_question: question.value, excerpt: excerpt.value, route_id: routeId}, step.id);
  }));
  return box;
}

function guidedEntryIds(next) {
  return new Set((next?.pipeline_runs || []).flatMap(run => run.steps.map(step => step.entry_id)));
}
