"use strict";

const view = document.querySelector("#view");
const message = document.querySelector("#message");
let token = location.hash.slice(1);
if (location.hash) history.replaceState({}, "", "/");
if (!/^[0-9a-f]{64}$/.test(token)) token = "";
let screen;

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function say(text) {
  message.textContent = text || "";
}

function button(text, action) {
  const node = element("button", text);
  node.type = "button";
  node.onclick = async () => {
    node.disabled = true;
    say("");
    try { await action(); }
    catch (error) { say(error.message); }
    finally { node.disabled = false; }
  };
  return node;
}

function section(parent, title, text) {
  const node = element("section");
  node.append(element("h2", title));
  if (text) node.append(element("p", text));
  parent.append(node);
  return node;
}

function control(parent, label, tag = "input", type) {
  const wrapper = element("label", label);
  const input = element(tag);
  if (type) input.type = type;
  wrapper.append(input);
  parent.append(wrapper);
  return input;
}

function select(parent, label, entries) {
  const node = control(parent, label, "select");
  entries.forEach(([value, text]) => {
    const option = element("option", text);
    option.value = value;
    node.append(option);
  });
  return node;
}

function external(label, url) {
  const link = element("a", label);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

async function api(path, data, raw = false) {
  const headers = {"Authorization": "Bearer " + token};
  const options = {headers, credentials: "omit", redirect: "error"};
  if (data !== undefined) {
    options.method = "POST";
    if (data instanceof FormData) options.body = data;
    else {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(data);
    }
  }
  const response = await fetch("/api" + path, options);
  if (!response.ok) {
    const result = await response.json();
    throw new Error(result.message || "The operation failed.");
  }
  return raw ? response : response.json();
}

async function progress(parent, operation, success) {
  const box = element("p", "Queued");
  const cancel = button("Request cancellation", async () => {
    await api("/operations/" + operation + "/cancel", {});
    say("Cancellation requested.");
  });
  const detail = element("ul");
  parent.append(box, detail, cancel);
  while (box.isConnected) {
    const state = await api("/operations/" + operation);
    box.textContent = state.state + (state.reason ? " · " + state.reason : "");
    detail.replaceChildren(...Object.entries(state.apps || {}).map(([name, item]) =>
      element("li", ({ldr: "Local Deep Research", gptr: "GPT Researcher", pqa: "PaperQA2"})[name] +
        " · " + item.state + " · " + item.message)));
    cancel.hidden = !state.cancellable;
    if (state.state === "Succeeded") {
      await success(state.result);
      return;
    }
    if (!["Running", "Queued"].includes(state.state)) return;
    await new Promise(resolve => setTimeout(resolve, 800));
  }
}

function installDetails(parent, approved) {
  for (const [label, list] of [["Prerequisites", approved.prerequisites], ["Installs", approved.essential],
    ["Separate services", approved.extras]]) {
    if (list?.length) parent.append(element("p", label + " · " + list.join(", ")));
  }
  if (approved.evidence_status === "candidate") parent.append(element("p", "Pinned candidate. Desktop acceptance is pending."));
}

async function apps() {
  const rows = await api("/apps");
  if (screen !== "apps") return;
  const grid = element("div");
  grid.className = "grid app-grid";
  view.replaceChildren(element("h1", "Apps"));
  const incomplete = rows.filter(app => !app.current);
  if (incomplete.length) {
    const setup = section(view, "Core setup");
    const plan = element("div");
    const label = rows.some(app => app.current) ? "Retry incomplete apps" : "Install three research apps";
    setup.append(button(label, async () => {
      const info = await api("/apps/core/install-plan");
      plan.replaceChildren();
      for (const item of info.apps) {
        const detail = section(plan, item.name, item.installed ? "Already installed" :
          (item.plan ? "To be installed" : "No install bundle for this host"));
        if (item.plan && !item.installed) {
          const size = item.plan.estimated_bytes;
          detail.append(element("p", size ? "Downloads locked packages · " + Math.ceil(size / (1024 ** 2)) +
            " MB estimated" : "Downloads and installs locked packages in the hub data folder."));
          const details = element("details");
          details.append(element("summary", "Dependencies and prerequisites"));
          installDetails(details, item.plan);
          detail.append(details);
        }
      }
      plan.append(button("Confirm and install", async () => {
        const run = await api("/apps/core/install", {digest: info.digest});
        await progress(plan, run.operation_id, async result => {
          if (result.complete) {
            await apps();
            say("All three core apps are installed.");
          } else {
            plan.append(button("Review incomplete apps", apps));
            say("Review the per-app results and retry incomplete apps.");
          }
        });
        if (plan.isConnected && !plan.querySelector("[data-retry]")) {
          const retry = button("Refresh app status", apps);
          retry.dataset.retry = "true";
          plan.append(retry);
        }
      }));
    }), plan);
  }
  view.append(grid);
  for (const app of rows) {
    const card = section(grid, app.name, app.state === "Exited" ? "Failed" : app.current ? "Ready" : "Needs setup");
    const advanced = element("details");
    advanced.append(element("summary", "Advanced / Troubleshooting"),
      element("p", (app.version || "Pin " + app.candidate) + " · " + app.state));
    if (app.id === "pqa") {
      const reason = app.paper_analysis?.reason;
      const analyse = button("Analyse papers", () => choose("analysis"));
      analyse.className = "primary";
      card.append(analyse);
      if (reason) card.append(element("p", reason));
    }
    if (app.configuration) card.append(element("p", app.configuration));
    if (!app.installable) card.append(element("p", "See the release documentation for platform status."));
    if (app.current) {
      const install = element("div");
      advanced.append(button("Review reinstall", async () => {
        const info = await api("/apps/" + app.id + "/install-plan");
        install.replaceChildren(element("h3", "Installation plan"));
        installDetails(install, info.plan);
        install.append(button("Reinstall this pin", async () => {
          const run = await api("/apps/" + app.id + "/install", {digest: info.digest});
          await progress(install, run.operation_id, apps);
        }));
      }), install);
      const running = element("div");
      (app.id === "pqa" ? advanced : card).append(button(app.id === "pqa" ? "Show standalone command" : app.id === "ldr" ? "Open LDR" : "Open GPT Researcher", async () => {
        running.replaceChildren();
        const stop = button("Stop native app", async () => {
          await api("/apps/" + app.id + "/stop", {});
          state.textContent = "Stopping";
        });
        const state = element("p");
        let result = await api("/apps/" + app.id + "/launch", {});
        if (result.command_line) {
          running.append(element("pre", result.command_line), button("Copy standalone command", () => navigator.clipboard.writeText(result.command_line)));
          return;
        }
        running.append(state, stop);
        while (running.isConnected && result.state === "Starting") {
          state.textContent = result.state;
          await new Promise(resolve => setTimeout(resolve, 700));
          result = await api("/apps/" + app.id + "/browser");
        }
        if (!running.isConnected) return;
        state.textContent = result.state;
        stop.hidden = result.state === "Exited" || result.state === "Stopped";
        if (result.url) {
          const opened = await api("/apps/" + app.id + "/launch", {ready_generation: result.generation});
          if (opened.url) running.append(external("Open " + app.name, opened.url));
          if (opened.restart_needed) running.append(button("Restart to apply settings", async () => {
            await api("/apps/" + app.id + "/stop", {});
            say("Once the app stops, use its Open button to start it with the saved settings.");
          }));
        }
      }), running);
      if (app.can_stop) card.append(button("Stop running app", async () => {
        await api("/apps/" + app.id + "/stop", {});
        await apps();
      }));
    }
    card.append(advanced);
  }
}

async function usage(refresh = false) {
  const info = await api("/usage" + (refresh ? "/refresh" : ""), refresh ? {} : undefined);
  if (screen !== "usage") return;
  view.replaceChildren(element("h1", "OpenRouter usage"), button("Refresh", () => usage(true)));
  view.append(element("p", "Shared-key figures in USD may include use outside RAH. Other keys and direct provider costs are separate."));
  if (info.refreshed_at) view.append(element("p", "Last refreshed " + new Date(info.refreshed_at).toLocaleString() + (info.stale ? " · Stale" : "")));
  if (info.error) view.append(element("p", "Usage status · " + info.error.replaceAll("_", " ")));
  const labels = ["Total", "Daily", "Weekly", "Monthly"];
  for (const [prefix, title] of [["usage", "OpenRouter-reported usage"], ["byok_usage", "Bring your own key usage"]]) {
    const card = section(view, title);
    const list = element("dl");
    ["", "_daily", "_weekly", "_monthly"].forEach((suffix, index) => {
      list.append(element("dt", labels[index]), element("dd", info.fields[prefix + suffix] ?? "Unavailable"));
    });
    card.append(list);
  }
  view.append(external("Open OpenRouter Activity", "https://openrouter.ai/activity"));
}

const modelLabels = {
  RAH_OPENROUTER_CHAT_MODEL: "Shared OpenRouter chat model",
  RAH_OPENROUTER_EMBEDDING_MODEL: "Shared OpenRouter embedding model", RAH_LDR_CHAT: "LDR chat role",
  RAH_GPTR_FAST: "GPT Researcher fast role", RAH_GPTR_SMART: "GPT Researcher smart role",
  RAH_GPTR_STRATEGIC: "GPT Researcher strategic role", RAH_GPTR_EMBEDDING: "GPT Researcher embeddings",
  RAH_PQA_ANSWER: "PaperQA2 answer", RAH_PQA_SUMMARY: "PaperQA2 summary",
  RAH_PQA_AGENT: "PaperQA2 agent", RAH_PQA_EMBEDDING: "PaperQA2 embeddings",
  RAH_PQA_SETTINGS_FILE: "Native PaperQA2 settings path",
};
const credentialLabels = {
  RAH_OPENROUTER_API_KEY: "Shared OpenRouter key",
};

async function settings() {
  const info = await api("/settings");
  if (screen !== "settings") return;
  view.replaceChildren(element("h1", "Settings"));
  const managed = section(view, "Shared and app settings", "Blank roles inherit the relevant chat or embedding default. Enter native to release an override or openrouter:model for an explicit selection.");
  managed.append(element("p", "The shared key applies to OpenRouter roles. Release the LDR override and restart to use its native model controls."));
  const nativeDetails = element("details");
  nativeDetails.append(element("summary", "Native configuration and search credentials"));
  const changes = {};
  const cleared = {};
  for (const [key, value] of Object.entries(info.values)) {
    const input = control(modelLabels[key] ? managed : nativeDetails, modelLabels[key] || key);
    input.value = value;
    changes[key] = input;
  }
  for (const family of ["chat", "embedding"]) {
    const defaults = element("datalist");
    const roleModels = element("datalist");
    defaults.id = "openrouter-default-" + family;
    roleModels.id = "openrouter-roles-" + family;
    changes["RAH_OPENROUTER_" + family.toUpperCase() + "_MODEL"].setAttribute("list", defaults.id);
    const roles = family === "chat" ? ["LDR_CHAT", "GPTR_FAST", "GPTR_SMART", "GPTR_STRATEGIC",
      "PQA_ANSWER", "PQA_SUMMARY", "PQA_AGENT"] : ["GPTR_EMBEDDING", "PQA_EMBEDDING"];
    for (const role of roles) changes["RAH_" + role].setAttribute("list", roleModels.id);
    managed.append(button("Browse OpenRouter " + family + " models", async () => {
      const catalog = await api("/models/" + family + "?details=1");
      const models = catalog.models;
      say(catalog.stale ? "Showing saved or release model suggestions. Live data is unavailable." :
        "Catalogue refreshed " + catalog.refreshed_at +
        (catalog.benchmark_source ? " · Intelligence shortlist by " + catalog.benchmark_source : ""));
      for (const [list, prefix] of [[defaults, ""], [roleModels, "openrouter:"]]) {
        list.replaceChildren(...models.map(model => {
          const option = element("option");
          option.value = prefix + model.id;
          option.label = [catalog.shortlist.includes(model.id) ? "Shortlisted" : null,
            model.input === null ? "Price unavailable" : "$" + model.input + " input / 1M tokens",
            model.output === null ? null : "$" + model.output + " output / 1M tokens",
            model.context ? model.context + " context" : null, model.tools ? "Tool use" : null]
            .filter(Boolean).join(" · ");
          return option;
        }));
      }
    }), defaults, roleModels);
  }
  managed.append(button("Use managed PaperQA2 models", () => {
    for (const [key, value] of Object.entries(info.paper_suggestions)) changes[key].value = value;
    say("Review and save the four PaperQA2 model settings.");
  }));
  for (const [app, prefix] of [["LDR", "LDR_"], ["GPT Researcher", "GPTR_"], ["PaperQA2", "PQA_"]]) {
    managed.append(button("Reset " + app + " to native", () => {
      for (const key of Object.keys(modelLabels).filter(key => key.startsWith("RAH_" + prefix))) {
        changes[key].value = "native";
      }
      say("Save settings to apply the native model settings for " + app + ".");
    }));
  }
  for (const [key, exists] of Object.entries(info.credentials)) {
    const parent = credentialLabels[key] ? managed : nativeDetails;
    changes[key] = control(parent, (credentialLabels[key] || key) + (exists ? " · Saved" : ""), "input", "password");
    if (exists) cleared[key] = control(parent, "Clear " + (credentialLabels[key] || key), "input", "checkbox");
  }
  managed.append(nativeDetails, button("Save settings", async () => {
    const result = await api("/settings", {revision: info.revision,
      changes: Object.fromEntries(Object.entries(changes).map(([name, input]) => [name, input.value])),
      clear: Object.keys(cleared).filter(name => cleared[name].checked)});
    await settings();
    say(Object.values(result.failures).join(" ") || "Settings saved for the next launch.");
  }));
  for (const [name, states] of Object.entries(info.states)) {
    const card = section(view, {ldr: "Local Deep Research", gptr: "GPT Researcher", pqa: "PaperQA2"}[name]);
    for (const [role, state] of Object.entries(states)) card.append(element("p", role + " · " + state));
  }
  const overwrite = control(view, "Regenerate edited generated profiles", "input", "checkbox");
  view.append(button("Apply saved settings", async () => {
    const result = await api("/settings/apply", {revision: info.revision, regenerate: overwrite.checked});
    await settings();
    say(Object.values(result.failures).join(" ") || "Settings applied.");
  }));
}

const recipes = [
  ["Academic literature survey", "Start with a literature question in LDR's academic search. Attach its original report, review the citations in Papers Needed, obtain PDFs through your own access and approve selected files for PaperQA2. Revisit the original report and cited answer in the project. Search, models and embeddings may incur charges."],
  ["Deep research to paper analysis", "Run GPT Researcher Deep Research with a public question. Attach the original cited report, review the requested full texts and choose which PDFs PaperQA2 should analyse. Continue in Papers Needed. Native search, model and embedding charges may apply."],
  ["Broad-to-deep investigation", "Start with a broad web question in LDR. Review its original report and write a focused follow-up in GPT Researcher's Deep Research mode. Keep both reports in the project. Configured search and model providers may charge."],
  ["Independent report comparison", "Run the public question independently in LDR and GPT Researcher. Attach the original cited reports and choose two to compare. Search and model services may charge for both runs."],
];

async function workflows(analysis = false) {
  const projects = await api("/projects");
  if (!["workflows", "analysis"].includes(screen)) return;
  view.replaceChildren(element("h1", analysis ? "Analyse papers" : "Workflows"));
  const grid = element("div");
  grid.className = "grid";
  if (!analysis) view.append(grid);
  if (!analysis) recipes.forEach(([title, steps]) => section(grid, title, steps));
  const list = section(view, "Projects");
  for (const project of projects) list.append(button(project.name, () => showProject(project.id)));
  const name = control(list, "New project name");
  list.append(button("Create project", async () => {
    const item = await api("/projects", {name: name.value});
    await showProject(item.id);
  }));
}

const citationFields = [["title", "Title"], ["doi", "DOI"], ["pmid", "PMID"], ["citation_url", "Citation URL"], ["note", "Upstream note"]];

function editor(parent, item, pdfs) {
  const card = element("section");
  parent.append(card);
  const controls = {};
  for (const [field, label] of citationFields) {
    controls[field] = control(card, label);
    controls[field].value = item[field] || "";
  }
  const state = select(card, "Paper status", [["needed", "Full text needed"], ["supplied", "PDF supplied"], ["skipped", "Skipped"]]);
  state.value = item.state || "needed";
  const attached = select(card, "Associated PDF", [["", "Select uploaded PDF"], ...pdfs.map(pdf => [pdf.id, pdf.original_name])]);
  attached.value = item.pdf_artifact_id || "";
  const abstract = control(card, "Abstract only", "input", "checkbox");
  abstract.checked = Boolean(item.abstract_only);
  return [card, () => ({...item, ...Object.fromEntries(Object.entries(controls).map(([field, input]) => [field, input.value])),
    state: state.value, pdf_artifact_id: attached.value || null, abstract_only: abstract.checked})];
}

async function download(project, item, bib = false) {
  const response = await api("/projects/" + project.id + "/artifacts/" + item.id + (bib ? "?format=bib" : ""), undefined, true);
  const url = URL.createObjectURL(await response.blob());
  const link = element("a");
  link.href = url;
  link.download = bib ? "native-citations.bib" : item.original_name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function showProject(id, answerId) {
  const project = await api("/projects/" + id);
  if (!["workflows", "analysis"].includes(screen)) return;
  const path = "/projects/" + id;
  const pdfs = project.artifact.filter(item => item.kind === "pdf");
  view.replaceChildren(button("All projects", () => workflows(screen === "analysis")), element("h1", project.name),
    button("Open project folder", async () => { await api(path + "/open-folder", {}); }));
  if (answerId) {
    const response = await api(path + "/artifacts/" + answerId, undefined, true);
    section(view, "PaperQA2 answer and native references").append(element("pre", await response.text()));
  }
  const files = section(view, "Original files");
  for (const item of project.artifact) {
    const row = element("p", item.original_name + " ");
    if (item.kind === "native_link") {
      row.append(button("Open original result", async () => {
        const original = await api(path + "/artifacts/" + item.id);
        row.append(external("Open native result", original.url));
      }));
    } else {
      row.append(button("Download", () => download(project, item)));
      if (item.kind === "answer") row.append(button("View answer and references", () => showProject(id, item.id)));
      if (item.kind === "answer" && item.has_bib) {
        row.append(button("Download native citations", () => download(project, item, true)));
      }
    }
    files.append(row);
  }
  const reports = project.artifact.filter(item => ["report", "answer"].includes(item.kind) && !item.original_name.toLowerCase().endsWith(".ris"));
  if (reports.length >= 2) {
    const compare = section(view, "Compare original reports");
    const ids = reports.map(item => [item.id, item.original_name]);
    const left = select(compare, "First report", ids);
    const right = select(compare, "Second report", ids);
    right.selectedIndex = 1;
    const outcomes = element("div");
    outcomes.className = "grid report-compare";
    compare.append(button("Show reports", async () => {
      if (left.value === right.value) throw new Error("Choose two different reports.");
      const records = await Promise.all([left.value, right.value].map(async value => {
        const item = reports.find(report => report.id === value);
        const response = await api(path + "/artifacts/" + value, undefined, true);
        return [item.original_name, (await (await response.blob()).slice(0, 1024 * 1024).text())];
      }));
      outcomes.replaceChildren();
      for (const [name, text] of records) section(outcomes, name).append(element("pre", text));
    }), element("p", "The comparison displays up to 1 MB from each report. Download the original for the full file."), outcomes);
  }
  const upload = control(files, "Attach a native report (.md, .txt, .json), RIS citations or PDF", "input", "file");
  upload.accept = ".pdf,.md,.txt,.json,.ris";
  files.append(button("Attach file", async () => {
    const file = upload.files[0];
    if (!file) throw new Error("Select a file first.");
    const form = new FormData();
    form.set("file", file);
    form.set("revision", project.revision);
    form.set("kind", file.name.toLowerCase().endsWith(".pdf") ? "pdf" : "report");
    await api(path + "/files", form);
    await showProject(id);
  }));
  const papers = section(view, "Papers Needed", "Use your own access to obtain missing PDFs. Upload them, then associate each file with its paper.");
  const paste = control(papers, "Paste citations", "textarea");
  let importExport;
  if (project.capabilities.structured_citations) {
    const exportFile = select(papers, "LDR RIS export", [["", "Select an attached export"],
      ...project.artifact.filter(a => a.kind === "report" && a.original_name?.toLowerCase().endsWith(".ris")).map(a => [a.id, a.original_name])]);
    importExport = button("Preview export", async () => {
      if (!exportFile.value) throw new Error("Choose an attached export.");
      preview(await api(path + "/papers/parse", {artifact_id: exportFile.value}));
    });
  } else {
    papers.append(element("p", "Structured LDR import is awaiting verification. Paste citations or add papers manually."));
  }
  const draft = element("div");
  function preview(rows) {
    draft.replaceChildren();
    const editors = rows.map(item => {
      const [card, read] = editor(draft, item, pdfs);
      const keep = control(card, "Save this row", "input", "checkbox");
      keep.checked = true;
      return [keep, read];
    });
    if (editors.length) draft.append(button("Save selected rows", async () => {
      await api(path + "/papers", {revision: project.revision, rows: editors.filter(([keep]) => keep.checked).map(([, read]) => read())});
      await showProject(id);
    }));
  }
  papers.append(button("Preview pasted citations", async () => preview(await api(path + "/papers/parse", {text: paste.value}))),
    ...(importExport ? [importExport] : []),
    button("Add paper manually", () => preview([{}])), draft);
  for (const item of project.paper) {
    if (item.citation_url) papers.append(external("Open citation", item.citation_url));
    const [card, read] = editor(papers, item, pdfs);
    card.append(button("Save paper changes", async () => {
      await api(path + "/papers", {revision: project.revision, rows: [read()]});
      await showProject(id);
    }));
  }

  const publicRun = section(view, "GPT Researcher", "Use a public question. It is visible to local process inspection and goes to your configured native research services.");
  publicRun.hidden = screen === "analysis";
  const publicQuestion = control(publicRun, "Public research question", "textarea");
  const mode = select(publicRun, "Native report mode", [["research_report", "Research report"], ["deep", "Deep Research"]]);
  const publicOK = control(publicRun, "I confirm this question is public", "input", "checkbox");
  publicRun.append(button("Run GPT Researcher", async () => {
    const result = await api(path + "/gptr-reports", {question: publicQuestion.value, mode: mode.value, public: publicOK.checked});
    await progress(publicRun, result.operation_id, async () => { await showProject(id); say("Original report saved."); });
  }));

  const paperRun = section(view, "Continue in PaperQA2", project.capabilities.paper_route ?
    "Each approved run builds a fresh index from the selected PDFs." :
    project.capabilities.paper_reason || "Managed PDF transfer is unavailable.");
  if (!project.capabilities.paper_route) {
    paperRun.append(button("Open Settings", () => choose("settings")), button("Standalone PaperQA2", () => choose("apps")));
    return;
  }
  const selected = new Map();
  for (const artifact of pdfs.filter(pdf => project.paper.some(item => item.state === "supplied" && item.pdf_artifact_id === pdf.id))) {
    selected.set(artifact.id, control(paperRun, artifact.original_name, "input", "checkbox"));
  }
  const paperQuestion = control(paperRun, "Question about selected PDFs", "textarea");
  if (screen === "analysis") paperRun.scrollIntoView();
  else papers.append(button("Continue in PaperQA2", () => paperRun.scrollIntoView()));
  paperRun.append(button("Review cloud transfer", async () => {
    const result = await api(path + "/pqa-preview", {revision: project.revision, question: paperQuestion.value,
      pdfs: Array.from(selected).filter(([, item]) => item.checked).map(([key]) => key)});
    const dialog = element("dialog");
    const title = element("h2", "Confirm PaperQA2 transfer");
    title.id = "paper-transfer-title";
    dialog.setAttribute("aria-labelledby", title.id);
    dialog.append(title, element("p", "PaperQA2 will re-index " + result.reindex +
      " files. OpenRouter and the listed providers will receive the data below. Charges may apply."),
      element("p", result.files.join(", ")), element("p", result.question), element("p", result.profile.parser));
    const destinations = element("ul");
    for (const dest of result.profile.destinations) {
      destinations.append(element("li", dest.role + " · " + dest.service + " → " + dest.provider +
        " · " + dest.model + " · " + dest.data));
    }
    const warning = element("p", "Account Zero Data Retention is unverified by RAH. Consider enabling it before sending paper text. ");
    warning.append(external("Open OpenRouter Privacy Settings", "https://openrouter.ai/settings/privacy"));
    const error = element("p");
    error.setAttribute("role", "alert");
    dialog.append(destinations, warning, error);
    dialog.append(element("p", "Raw PDFs and page images stay local. The listed services receive text, including citation details that PaperQA2 includes in its prompts."));
    const exportsChecked = control(dialog, "I confirm that prompt logging and Broadcast or other trace exports exclude this run", "input", "checkbox");
    let busy = false;
    async function approve(choice) {
      if (busy) return;
      if (choice && !exportsChecked.checked) {
        error.textContent = "Confirm the account export setting before approval.";
        return;
      }
      busy = true;
      let run;
      try {
        run = await api(path + "/pqa-approve", {preview_id: result.preview_id, binding: result.binding, approve: choice, account_exports_checked: exportsChecked.checked});
      } catch (problem) {
        if (choice) {
          error.textContent = problem.message;
          busy = false;
          return;
        }
      }
      dialog.close();
      dialog.remove();
      if (run?.operation_id) {
        await progress(paperRun, run.operation_id, async outcome => {
          await showProject(id, outcome.artifact_id);
          say("Original answer saved.");
        });
      }
    }
    dialog.addEventListener("cancel", event => { event.preventDefault(); approve(false); });
    dialog.append(button("Cancel", () => approve(false)), button("Approve and run", () => approve(true)));
    paperRun.append(dialog);
    dialog.showModal();
  }));

}

const views = {apps, workflows, usage, settings, analysis: () => workflows(true)};
async function choose(name) {
  screen = name;
  document.querySelectorAll("[data-view]").forEach(node => {
    if (node.dataset.view === name) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  view.replaceChildren(element("p", "Loading…"));
  try { await views[name](); }
  catch (error) { say(error.message); }
}

if (token) {
  document.querySelectorAll("[data-view]").forEach(node => {
    node.onclick = () => { say(""); choose(node.dataset.view); };
  });
  document.querySelector("#quit").onclick = async () => {
    try { await api("/quit", {}); view.replaceChildren(element("p", "Research Agent Hub has stopped.")); }
    catch (error) { say(error.message); }
  };
  choose("apps");
} else {
  document.querySelectorAll("header button").forEach(node => { node.disabled = true; });
}
