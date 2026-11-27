"use strict";

const names = {gptr: "GPT Researcher", pqa: "PaperQA2", ldr: "Local Deep Research"};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const projectTools = document.querySelector("#project-tools");
const breadcrumbs = document.querySelector("#breadcrumbs");
const expanded = new Set();
const drafts = new Map();
const staged = new Map();
const nativeReceipts = new Map();
let workspace;
let locationState = {};
let poll;
let routeGeneration = 0;

function projectPath(project) { return "/projects/" + project; }
function projectLabel(project) { return workspace?.projects.find(item => item.id === project)?.name || "Workspace"; }
function destination() { return locationState.project || locationState.chat; }
function draft() {
  const key = locationState.chat || "new:" + destination();
  if (!drafts.has(key)) drafts.set(key, {app: "gptr", question: "", quote: "", pdfs: [], files: [], mode: "guided", reportMode: "research_report"});
  return drafts.get(key);
}

function route(project, chat, page) {
  const path = [project || "chat", chat || "-", page].filter(Boolean).join("/");
  const hash = project || chat ? "#" + path.replace(/\/-$/, "") : "";
  const next = hash || "/";
  if (location.hash !== hash) history.pushState({}, "", next);
  if (narrow.matches) sidebar(false);
  return openWorkspace();
}

function readRoute() {
  const [project, chat, page, extra] = location.hash.slice(1).split("/");
  if ((!UUID.test(project || "") && !(project === "chat" && UUID.test(chat || ""))) || extra !== undefined) return {};
  return {project: project === "chat" ? undefined : project, chat: UUID.test(chat || "") ? chat : undefined,
    page: ["files", "papers"].includes(page) ? page : undefined};
}

function modal(title) {
  const dialog = element("dialog");
  const label = element("h2", title);
  label.id = "workspace-dialog-title";
  dialog.setAttribute("aria-labelledby", label.id);
  const status = element("p");
  status.setAttribute("role", "alert");
  status.hidden = true;
  dialog.append(label);
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove(), {once: true});
  function act(text, action, primary = false, busy = "Working…") {
    const node = button(text, async relabel => {
      try { status.hidden = true; return await action(relabel); }
      catch (error) { status.hidden = false; status.textContent = error.message; }
    }, busy);
    if (primary) node.className = "primary";
    return node;
  }
  function show(...actions) {
    const row = element("div");
    row.className = "dialog-actions";
    row.append(...actions);
    dialog.append(status, row);
    dialog.showModal();
  }
  return {dialog, act, show};
}

function editName(title, initial, action) {
  const {dialog, act, show} = modal(title);
  const input = control(dialog, "Name");
  input.value = initial;
  input.maxLength = 160;
  const save = act("Save", async () => { await action(input.value); dialog.close(); }, true, "Saving…");
  input.addEventListener("keydown", event => { if (event.key === "Enter") save.click(); });
  show(act("Cancel", () => dialog.close()), save);
  input.select();
}

function chatLink(chat) {
  const link = button(chat.title, () => route(chat.project_id || undefined, chat.id));
  link.className = "chat-link";
  link.title = chat.title;
  if (locationState.chat === chat.id) link.setAttribute("aria-current", "page");
  return link;
}

function renderSidebar() {
  const recent = document.querySelector("#recent");
  recent.replaceChildren(...workspace.chats.slice(0, 8).map(chatLink));
  if (!workspace.chats.length) recent.append(element("p", "No chats yet"));
  const projectList = document.querySelector("#projects");
  const items = workspace.projects;
  projectList.replaceChildren(...items.map(project => {
    const group = element("details");
    group.open = expanded.has(project.id) || locationState.project === project.id;
    const toggle = element("summary", project.name);
    group.ontoggle = () => { if (group.open) expanded.add(project.id); else expanded.delete(project.id); };
    const open = button("Open project", () => route(project.id));
    const chats = workspace.chats.filter(chat => chat.project_id === project.id);
    group.append(toggle, open, button("New chat in project", () => newChat(project.id)), ...chats.map(chatLink));
    return group;
  }));
  const standalone = workspace.chats.filter(chat => !chat.project_id);
  if (standalone.length) {
    const group = element("details");
    group.open = true;
    group.append(element("summary", "Standalone chats"), ...standalone.map(chatLink));
    projectList.append(group);
  }
}

async function newChat(project) {
  const item = await api("/chats", {project_id: project});
  if (project) expanded.add(project);
  await route(project, item.id);
  composer?.querySelector("textarea")?.focus();
}

function fileURL(project, id) { return projectPath(project) + "/artifacts/" + id; }

function safeResult(text) {
  const node = element("pre");
  node.className = "result-text";
  for (const part of text.split(/(https?:\/\/[^\s<>"]+)/g)) {
    if (/^https?:\/\//.test(part)) {
      const url = part.replace(/[).,;\]]+$/, "");
      try {
        const parsed = new URL(url);
        if (parsed.username || parsed.password) throw new Error();
        node.append(external(url, parsed.href), document.createTextNode(part.slice(url.length)));
      } catch (_) { node.append(document.createTextNode(part)); }
    } else node.append(document.createTextNode(part));
  }
  return node;
}

async function showOutput(parent, project, item, current) {
  const card = element("div");
  card.className = "native-result";
  const actions = element("div");
  actions.className = "sub-actions";
  actions.append(element("strong", item.original_name), button("Download original", () => download({id: project}, item)));
  if (item.has_bib) actions.append(button("BibTeX", () => download({id: project}, item, true)));
  card.append(actions);
  if (!item.original_name.toLowerCase().endsWith(".ris")) {
    const response = await api(fileURL(project, item.id), undefined, true);
    const text = await (await response.blob()).slice(0, 512 * 1024).text();
    const content = safeResult(text);
    actions.append(button("Quote in next request", () => {
      const selection = window.getSelection();
      const selected = selection?.anchorNode && content.contains(selection.anchorNode) && content.contains(selection.focusNode)
        ? selection.toString() : "";
      const {dialog, act, show} = modal("Quote in next PaperQA2 request");
      const excerpt = control(dialog, "Excerpt", "textarea");
      excerpt.maxLength = 8000;
      excerpt.value = selected.slice(0, 8000);
      show(act("Cancel", () => dialog.close()), act("Add quote", () => {
        if (!excerpt.value.trim()) throw new Error("Enter or paste the passage you want to include.");
        current.quote = excerpt.value;
        setApp(current, "pqa");
        dialog.close();
        renderComposer();
      }, true));
    }));
    card.append(content);
  }
  parent.append(card);
}

async function uploadAttachment(project, entry, item, kind) {
  const form = new FormData();
  form.set("file", item.file);
  form.set("kind", kind);
  form.set("entry_id", entry);
  form.set("attachment_id", item.id);
  return (await api(projectPath(project) + "/files", form)).artifact_id;
}

// ---- Usage (spec §5.5). Figures are OpenRouter's per-response records; nothing is derived from shared-key totals.

function money(value) { return "$" + Number(value).toFixed(4); }

function usageText(usage, running, step, provider) {
  if (running) return "OpenRouter usage appears when this step ends.";
  if (!usage) return "Not recorded (before usage tracking)";
  if (usage.basis === "not_observed") return "Not observed: ran in the app's own interface.";
  if (!usage.requests) {
    if (usage.openrouter_expected === false) return "No OpenRouter requests observed. Model calls went to "
      + (provider || "another provider") + ", which RAH doesn't meter.";
    if (usage.coverage === "qualified" && usage.requests === 0 && step === "scope_gate") return "No model request was made.";
    return "No usage observed — coverage unknown";
  }
  const parts = ["OpenRouter · " + usage.requests + (usage.requests === 1 ? " request" : " requests"),
    usage.prompt_tokens.toLocaleString() + " input / " + usage.completion_tokens.toLocaleString() + " output tokens"];
  if (usage.cost_usd !== null && usage.cost_usd !== undefined) {
    parts.push(usage.complete ? money(usage.cost_usd) + " (reported by OpenRouter)" : "at least " + money(usage.cost_usd));
  } else parts.push("cost not reported");
  const notes = [];
  if (!usage.complete) {
    const reason = {interrupted: "RAH stopped during this step", truncated: "usage log truncated", unparsed: "some responses could not be read",
      coverage_unverified: "request paths for this app version aren't verified", no_usage_observed: "no usage observed"}[usage.reason];
    if (usage.cost_missing) notes.push(usage.cost_missing + " of " + usage.requests + " responses had no cost");
    if (usage.aborted) notes.push("stopped mid-response");
    if (reason) notes.push(reason);
  }
  const failed = [];
  if (usage.errors) failed.push(usage.errors + (usage.errors === 1 ? " error response" : " error responses"));
  if (usage.no_response) failed.push(usage.no_response + (usage.no_response === 1 ? " failed connection" : " failed connections"));
  if (failed.length) parts.push(failed.join(" and ") + " (no usage reported)");
  if (usage.byok) parts.push(usage.byok + " BYOK requests: OpenRouter fee only; provider charges billed separately");
  return parts.join(" · ") + (notes.length ? " — partial: " + notes.join("; ") : "");
}

function usageRow(usage, running, step, provider) {
  const row = element("p", usageText(usage, running, step, provider));
  row.className = "usage-row";
  return row;
}

// ---- One-click PDF send against a read-only server preflight (spec §4.4).

function sendPapers(parent, context) {
  // context: {inputs() -> {pdfs, question, excerpt, quality, entry_id?}, send(preflight, status), fallback?(error)}
  const box = element("div");
  box.className = "send-papers";
  const disclosure = element("div");
  const row = element("div");
  row.className = "sub-actions";
  let latest = null;
  let pending = 0;
  let timer;
  const send = button("Select PDFs to send", async status => {
    if (!latest) throw new Error("Wait for the transfer check to finish.");
    return context.send(latest, status);
  }, "Sending…");
  send.className = "primary";
  send.disabled = true;
  row.append(send);
  const fallback = element("span");
  row.append(fallback);
  box.append(disclosure, row);
  parent.append(box);
  function label(text, enabled) {
    send.dataset.label = text;
    if (send.getAttribute("aria-busy") !== "true") { send.textContent = text; send.disabled = !enabled; }
  }
  function render(info) {
    const providers = [...new Set(info.destinations.map(item => item.provider))].join(", ");
    const files = element("ol");
    files.append(...info.files.map(item => element("li", item.name)));
    const roles = element("ul");
    roles.append(...info.destinations.map(item => element("li", item.role + " · " + item.model + " · " + item.provider + " only · " + item.endpoint)));
    disclosure.replaceChildren(files,
      element("p", "PaperQA2 parses these PDFs locally and sends extracted text, your question, any excerpt and citation details."),
      roles, element("p", "Each role is restricted to one provider with fallbacks disabled."),
      element("p", "Sending confirms the key excludes this run from prompt logging or additional trace exports. "),
      external("OpenRouter privacy settings (ZDR optional)", "https://openrouter.ai/settings/privacy"), element("p", "Charges may apply."));
    label("Send " + info.files.length + (info.files.length === 1 ? " PDF" : " PDFs") + " to PaperQA2 · OpenRouter → " + providers, true);
  }
  async function check() {
    const inputs = context.inputs();
    fallback.replaceChildren();
    latest = null;
    if (!inputs.pdfs.length) { disclosure.replaceChildren(); label("Select PDFs to send", false); return; }
    if (!inputs.question?.trim()) { disclosure.replaceChildren(); label("Enter a question to send", false); return; }
    const ticket = ++pending;
    label("Checking…", false);
    try {
      const body = {pdfs: inputs.pdfs, ...(inputs.entry_id ? {entry_id: inputs.entry_id} : {question: inputs.question.trim(),
        excerpt: inputs.excerpt || "", quality: inputs.quality || "standard"})};
      const info = await api(projectPath(inputs.scope) + "/pqa-preflight", body);
      if (ticket !== pending) return;
      latest = info;
      render(info);
    } catch (error) {
      if (ticket !== pending) return;
      disclosure.replaceChildren();
      label("Cannot send yet", false);
      const problem = element("p", error.message);
      problem.className = "inline-error";
      problem.setAttribute("role", "alert");
      const action = recovery(error.code);
      if (action) problem.append(" ", action);
      fallback.append(problem);
      context.fallback?.(error, fallback);
    }
  }
  const update = () => { clearTimeout(timer); timer = setTimeout(() => check(), 400); };
  check();
  return {update, element: box};
}

async function approvePaperSend(project, entryId, pdfs, shown, status) {
  // Re-derive the bound preview; send only if the server reproduces the disclosure shown before the click.
  status?.("Checking transfer…");
  const info = await api(projectPath(project) + "/pqa-preview", {entry_id: entryId, pdfs});
  if (info.disclosure_digest !== shown.disclosure_digest) {
    const error = new Error("The transfer changed. Check it and press Send again.");
    error.code = "stale_preview";
    throw error;
  }
  status?.("Sending…");
  await api(projectPath(project) + "/pqa-approve", {preview_id: info.preview_id, binding: info.binding, approve: true,
    account_exports_checked: true});
}

async function dispatchPaper(entry, ids, shown, status) {
  await approvePaperSend(entry.project_id, entry.id, ids, shown, status);
  staged.delete(entry.id);
}

async function launchNative(app, request) {
  const endpoint = "/apps/" + app;
  let result;
  try { result = await api(endpoint + "/launch", request); }
  catch (error) {
    if (error.code !== "native_workspace_restart" || !confirm("Stop the current GPT Researcher Classic session and reopen it for this folder? Running research in Classic may be interrupted.")) throw error;
    await api(endpoint + "/stop", {});
    for (let i = 0; i < 35; ++i) {
      await new Promise(resolve => setTimeout(resolve, 350));
      if (["Stopped", "Exited"].includes((await api(endpoint + "/browser")).state)) break;
    }
    result = await api(endpoint + "/launch", request);
  }
  for (let i = 0; i < 175 && ["Starting", "Waiting for the native app to become ready"].includes(result.state); ++i) {
    await new Promise(resolve => setTimeout(resolve, 700));
    result = await api(endpoint + "/browser");
  }
  if (result.generation && result.state === "Ready" && !result.opened) {
    result = await api(endpoint + "/launch", {...request, ready_generation: result.generation});
  }
  if (!result.url && !result.command_line) throw new Error(result.state || "The native app could not open.");
  return result;
}

async function nativeLaunch(entry) {
  const result = await launchNative(entry.app, {entry_id: entry.id, project_id: entry.project_id});
  nativeReceipts.set(entry.id, result);
  return result;
}

function showNative(entry, result) {
  const {dialog, act, show} = modal(result.command_line ? "Standalone PaperQA2" : names[entry.app] + " is ready");
  if (result.command_line) dialog.append(element("p", result.note), element("pre", result.command_line));
  else dialog.append(element("p", result.prefill_prepared ? "Review the editable draft in the native app. Research has not started."
    : "Prefill was not available. Copy the draft and paste it into the native app. Research has not started."),
    external("Open " + names[entry.app], result.url));
  show(act("Close", () => dialog.close()), ...(result.command_line ? [
    act("Copy command", async () => { await navigator.clipboard.writeText(result.command_line); dialog.close(); }, true)
  ] : entry.question ? [act("Copy question", () => navigator.clipboard.writeText(entry.question), true)] : []));
}

async function submit(current, data, status) {
  const shown = data.shown;
  data = {...data};
  delete data.shown;
  const existing = current.submit;
  const intent = JSON.stringify(data);
  const payload = existing?.intent === intent ? existing.payload : {...data, entry_id: crypto.randomUUID()};
  current.submit = {intent, payload};
  const entry = await api("/workspace/requests", payload);
  current.submit = null;
  const visible = locationState.project === data.project_id && locationState.chat === data.chat_id && screen === "workspace";
  if (!entry.replay || entry.state === "Preparing") {
    if (data.app === "pqa" && data.action === "managed") {
      current.files = [];
      current.pdfs = [];
      current.quote = "";
    }
    if (data.action !== "native") current.question = "";
  }
  drafts.set(entry.chat_id, current);
  const project = entry.project_id === entry.chat_id ? undefined : entry.project_id;
  if (entry.replay && entry.state !== "Preparing") { if (visible) await route(project, entry.chat_id); return; }
  if (visible) await route(project, entry.chat_id);
  try {
    if (data.action === "native") {
      const native = await nativeLaunch(entry);
      if (locationState.chat === entry.chat_id && screen === "workspace") showNative(entry, native);
    } else if (entry.app === "gptr") {
      await api(projectPath(entry.project_id) + "/gptr-reports", {entry_id: entry.id, recipients: data.recipients});
    } else {
      await dispatchPaper(entry, [...entry.input_ids], shown, status);
    }
  } catch (error) {
    if (data.action === "native") {
      await api(projectPath(entry.project_id) + "/requests/" + entry.id + "/discard", {}).catch(() => {});
    }
    throw error;
  } finally {
    if (locationState.chat === entry.chat_id && screen === "workspace") await refreshConversation();
  }
}

function suppliedInChat(project, supplied) {
  const current = draft();
  picker(current, supplied, async () => {
    const chat = locationState.chat;
    await route(project, chat);
    setApp(draft(), "pqa");
    renderComposer();
  });
}

function picker(current, artifacts, refresh = renderComposer) {
  const {dialog, act, show} = modal("Select project PDFs");
  const pdfs = artifacts.filter(artifact => artifact.kind === "pdf");
  if (!pdfs.length) dialog.append(element("p", "There are no saved PDFs in this project. Attach files from your computer."));
  const boxes = new Map(pdfs.map(item => {
    const box = control(dialog, item.original_name, "input", "checkbox");
    box.checked = current.pdfs.some(pdf => pdf.id === item.id);
    return [item, box];
  }));
  show(act("Cancel", () => dialog.close()), act("Use selection", async () => {
    current.pdfs = [...boxes].filter(([, box]) => box.checked).map(([item]) => ({id: item.id, name: item.original_name}));
    dialog.close();
    await refresh();
  }, true));
}

let conversation;
let artifacts = [];
let historySignature;
let transcript;
let composer;
let availableCorpus = [];

// Newly attached PDFs are saved locally to this chat first; that sends nothing externally (spec §4.4).
async function saveToChat(files, status) {
  const usable = files.filter(file => file.name.toLowerCase().endsWith(".pdf") && file.size <= 256 * 1024 ** 2);
  if (usable.length !== files.length) say("Select PDFs no larger than 256 MiB each.");
  let chat = locationState.chat;
  if (!chat) {
    status?.("Creating chat…");
    const current = draft();
    const created = await api("/chats", {project_id: locationState.project});
    drafts.set(created.id, current);
    chat = created.id;
    await route(locationState.project, chat);
  }
  const saved = [];
  for (const file of usable) {
    status?.("Saving " + file.name + " to this chat (local)…");
    const form = new FormData();
    form.set("file", file);
    form.set("kind", "pdf");
    form.set("chat_id", chat);
    saved.push({id: (await api(projectPath(destination()) + "/files", form)).artifact_id, name: file.name});
  }
  return saved;
}

function fileControl(parent, label, multiple, extensions, action) {
  const field = element("label", label);
  field.className = "file-button";
  const input = element("input");
  input.type = "file";
  input.setAttribute("aria-label", label);
  input.accept = extensions;
  input.multiple = multiple;
  const text = document.createTextNode(label);
  field.replaceChildren(text);
  input.onchange = async () => {
    const picked = [...input.files];
    if (!picked.length) return;
    input.disabled = true;
    field.setAttribute("aria-busy", "true");
    text.textContent = "Uploading " + (picked.length === 1 ? picked[0].name : picked.length + " files") + "…";
    clearInlineError(field);
    try { await action(picked, value => { text.textContent = value; }); }
    catch (error) { if (field.isConnected) inlineError(field, error); else say(error.message); }
    finally {
      input.value = "";
      input.disabled = false;
      field.removeAttribute("aria-busy");
      text.textContent = label;
    }
  };
  field.append(input);
  parent.append(field);
  return field;
}

function setApp(current, app) {
  current.app = app;
  current.mode = "classic";
  const {chat} = locationState;
  if (chat) api(projectPath(destination()) + "/chats/" + chat, {app}).catch(error => say(error.message));
}

function renderComposer() {
  if (!composer?.isConnected || screen !== "workspace") return;
  const current = draft();
  const app = current.app;
  const readiness = workspace.readiness[app];
  const project = locationState.project;
  const chat = locationState.chat;
  const active = conversation?.entries.find(entry => entry.action === "managed" &&
    ["Preparing", "Awaiting approval", "Running"].includes(entry.state));
  composer.replaceChildren();
  if (current.mode !== "classic") return renderGuidedComposer(current);
  const toolbar = element("div");
  toolbar.className = "composer-tools";
  const apps = select(toolbar, "Research app", Object.entries(names));
  apps.value = app;
  apps.onchange = () => {
    setApp(current, apps.value);
    renderComposer();
    composer.querySelector("select").focus();
  };
  if (app === "gptr") {
    const mode = select(toolbar, "Report mode", [["research_report", "Research report"], ["deep", "Deep research"]]);
    mode.value = current.reportMode;
    mode.onchange = () => { current.reportMode = mode.value; };
  }
  const guidedSwitch = button("Guided research", () => { current.mode = "guided"; renderComposer(); });
  guidedSwitch.className = "link-button";
  toolbar.append(guidedSwitch);
  const field = element("textarea");
  field.className = "question";
  field.setAttribute("aria-label", app === "ldr" ? "Research task or note" : "Research question");
  field.placeholder = app === "pqa" ? "Ask a question about the papers" : app === "ldr" ? "Add a research task or note" : "Enter a public research question";
  field.maxLength = 8000;
  field.rows = 3;
  field.value = current.question;
  let sender;
  field.oninput = () => { current.question = field.value; sender?.update(); };
  composer.append(toolbar, field);
  if (app === "pqa") {
    const attach = element("div");
    attach.className = "sub-actions";
    const chips = element("div");
    chips.className = "chips";
    for (const [key, items] of [["files", current.files], ["pdfs", current.pdfs]]) {
      for (const item of items) {
        const name = item.name || item.file.name;
        const remove = button(name + "  ×", () => {
          current[key] = current[key].filter(other => other !== item);
          renderComposer();
        });
        remove.title = name;
        remove.setAttribute("aria-label", "Remove " + name);
        chips.append(remove);
      }
    }
    if (current.quote) {
      const quote = element("details");
      quote.append(element("summary", "Selected quote"), element("blockquote", current.quote),
        button("Remove quote", () => { current.quote = ""; renderComposer(); }));
      chips.append(quote);
    }
    fileControl(attach, "Attach PDF", true, ".pdf", async (files, status) => {
      for (const item of await saveToChat(files, status)) if (!current.pdfs.some(pdf => pdf.id === item.id)) current.pdfs.push(item);
      renderComposer();
    });
    attach.append(element("span", "Attached PDFs are saved to this chat (local) before anything is sent."));
    if (destination()) attach.append(button("From saved files", () => picker(current, artifacts)));
    if (availableCorpus.length && !current.pdfs.length && !current.files.length) attach.append(button("Use last corpus", () => {
      current.pdfs = [...availableCorpus];
      renderComposer();
    }));
    composer.append(chips, attach);
  }
  const footer = element("div");
  footer.className = "composer-footer";
  const hint = element("p", !readiness.ready ? readiness.reason : app === "gptr" ?
    "Sends this public question to " + readiness.recipients.join(" and ") + ". Research may also fetch public websites. RAH can't detect private text."
    : app === "pqa" ? "The transfer below is checked by RAH before you send." : "Research runs in the native LDR window.");
  footer.append(hint);
  function payload(action, target = app) {
    return {app: target, action, project_id: project, chat_id: chat, question: action === "native" ? field.value : field.value.trim(),
      ...(action === "managed" && app === "gptr" ? {mode: current.reportMode} : {}),
      ...(action === "managed" && app === "pqa" ? {quote: current.quote, pdfs: current.pdfs.map(item => item.id)} : {})};
  }
  const availableActions = element("details");
  availableActions.className = "composer-actions";
  const menu = element("summary", "Open in…");
  const options = element("div");
  availableActions.append(menu, options);
  availableActions.onkeydown = event => {
    if (event.key === "Escape" && availableActions.open) { availableActions.open = false; menu.focus(); }
  };
  for (const target of ["gptr", "ldr"]) {
    const open = button(target === "gptr" ? "GPT Researcher Classic" : "Local Deep Research", async () => {
      availableActions.open = false;
      await submit(current, payload("native", target));
    });
    if (workspace.readiness[target].code === "app_not_installed") open.disabled = true;
    options.append(open);
  }
  if (project && !chat && workspace.readiness.gptr.code !== "app_not_installed") {
    options.append(button("Open Classic for project (no draft)", () => launchNative("gptr", {project_id: project, open: true})));
  }
  const copy = button("Copy question & open Local Deep Research", async () => {
    await navigator.clipboard.writeText(field.value);
    availableActions.open = false;
    await submit(current, {...payload("native", "ldr"), question: ""});
  });
  copy.disabled = workspace.readiness.ldr.code === "app_not_installed";
  options.append(copy, button("Show standalone PaperQA2 command", () => submit(current, payload("native", "pqa"))));
  footer.append(availableActions);
  const send = button(!readiness.ready ? (readiness.code === "app_not_installed" || readiness.code === "unverified_profile" ? "App details" : "Finish setup")
    : "Run public GPT Researcher report", async status => {
    if (!readiness.ready) {
      if (readiness.code === "app_not_installed" || readiness.code === "unverified_profile") return tool("apps");
      await tool("settings");
      if (app === "gptr" && screen === "settings") {
        const key = readiness.code === "missing_models" ? "RAH_OPENROUTER_CHAT_MODEL" :
          ["needs_search_credential", "search_configuration"].includes(readiness.code) ? "RAH_NATIVE_GPTR__RETRIEVER" : null;
        const input = key ? view.querySelector(`[data-key=${key}]`) : null;
        for (let parent = input?.parentElement; parent; parent = parent.parentElement) {
          if (parent.tagName === "DETAILS") parent.open = true;
        }
        input?.focus();
      }
      return;
    }
    const data = payload("managed");
    if (!data.question) { field.focus(); throw new Error("Enter a question."); }
    status("Starting report…");
    await submit(current, {...data, public: true, recipients: readiness.recipients});
  }, readiness.ready ? "Starting report…" : "Opening…");
  send.className = "primary";
  if (active && readiness.ready) {
    send.disabled = true;
    hint.textContent = "A request is active in this chat.";
  }
  field.onkeydown = event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      availableActions.open = true;
      availableActions.querySelector("button:not(:disabled)")?.focus();
    }
  };
  if (app === "pqa" && readiness.ready) {
    composer.append(footer);
    sender = sendPapers(composer, {
      inputs: () => ({scope: destination(), pdfs: current.pdfs.map(item => item.id), question: field.value,
        excerpt: current.quote, quality: "standard"}),
      send: async (shown, status) => {
        const data = payload("managed");
        if (!data.question) { field.focus(); throw new Error("Enter a question."); }
        if (!data.pdfs.length || data.pdfs.length > 40) throw new Error("Attach one to 40 PDFs.");
        if (active) throw new Error("A request is active in this chat.");
        await submit(current, {...data, shown}, status);
      }});
    return;
  }
  if (app !== "ldr" || !readiness.ready) footer.append(send);
  composer.append(footer);
}

async function renderTranscript(next, files, generation) {
  if (generation !== routeGeneration || !transcript?.isConnected) return;
  const runs = next?.pipeline_runs || [];
  const guidedIds = guidedEntryIds(next);
  const entries = next?.entries || [];
  const signature = JSON.stringify([entries, runs.map(run => [run.id, run.revision, run.state])]);
  if (historySignature === signature) return;
  const container = document.createDocumentFragment();
  const project = next?.project_id || destination();
  const current = draft();
  const lookup = new Map(files.map(item => [item.id, item]));
  if (!entries.length) {
    const welcome = element("div");
    welcome.className = "welcome";
    welcome.append(element("h1", "What are you researching?"),
      element("p", "Choose a purpose and research quality to start guided research, or use Classic apps."));
    container.append(welcome);
  }
  const placed = new Set();
  for (const row of entries) {
    if (guidedIds.has(row.id)) {
      const run = runs.find(item => item.steps.some(step => step.entry_id === row.id));
      if (run && !placed.has(run.id)) {
        placed.add(run.id);
        try { await renderRun(container, run, lookup); }
        catch (error) { container.append(element("p", error.message)); }
      }
      continue;
    }
    const entry = {...row, project_id: project};
    const card = element("article");
    card.className = "turn";
    const state = element("div");
    state.className = "turn-meta";
    const badge = element("span", entry.review_expired ? "Review again" : entry.state);
    badge.dataset.state = entry.state;
    state.append(element("strong", names[entry.app]), badge);
    card.append(state);
    if (entry.question) {
      const prompt = element("p", entry.question);
      prompt.className = "prompt";
      card.append(prompt);
    }
    if (entry.reason) card.append(element("p", entry.reason));
    if (entry.input_ids.length) {
      card.append(element("p", "PDFs saved · " + entry.input_ids.map(id => lookup.get(id)?.original_name || "Unavailable").join(", ")));
    }
    if (entry.app === "pqa" && entry.action === "managed") {
      if (entry.quote) {
        const details = element("details");
        details.append(element("summary", "Selected quote"), element("blockquote", entry.quote));
        card.append(details);
      }
      if (["Preparing", "Awaiting approval"].includes(entry.state)) {
        const local = staged.get(entry.id) || {pdfs: entry.input_ids.map(id => ({id, name: lookup.get(id)?.original_name || id}))};
        staged.set(entry.id, local);
        const controls = element("div");
        controls.className = "sub-actions";
        const note = element("p");
        let sender;
        function updateNote() {
          note.textContent = local.pdfs.map(item => item.name).join(", ") || "Attach the PDFs for this request.";
          sender?.update();
        }
        updateNote();
        if (entry.state === "Preparing") {
          fileControl(controls, "Attach remaining PDFs", true, ".pdf", async (chosen, status) => {
            for (const file of chosen) {
              status("Saving " + file.name + " to this chat (local)…");
              const id = await uploadAttachment(project, entry.id, {id: crypto.randomUUID(), file}, "pdf");
              if (!local.pdfs.some(item => item.id === id)) local.pdfs.push({id, name: file.name});
            }
            updateNote();
          });
          controls.append(button("From project", () => picker(local, files, updateNote)));
        }
        card.append(note, controls);
        sender = sendPapers(card, {
          inputs: () => ({scope: project, entry_id: entry.id, pdfs: local.pdfs.map(item => item.id), question: entry.question}),
          send: async (shown, status) => {
            await dispatchPaper(entry, local.pdfs.map(item => item.id), shown, status);
            await refreshConversation();
          }});
      }
      if (["Cancelled", "Failed", "Interrupted"].includes(entry.state)) {
        card.append(button("Use in a new attempt", () => {
          setApp(current, "pqa");
          current.question = entry.question;
          current.quote = entry.quote;
          current.pdfs = entry.input_ids.filter(id => lookup.has(id)).map(id => ({id, name: lookup.get(id).original_name}));
          renderComposer();
          composer.querySelector("textarea").focus();
        }));
      }
    }
    if (entry.state === "Preparing" && entry.app === "gptr" && entry.action === "managed") {
      const readiness = workspace.readiness.gptr;
      if (readiness.ready) card.append(element("p", "Sends the saved public question to " + readiness.recipients.join(" and ") + "."));
      card.append(button("Run public GPT Researcher report", async () => {
        if (!readiness.ready) throw new Error(readiness.reason);
        await api(projectPath(project) + "/gptr-reports", {entry_id: entry.id, recipients: readiness.recipients});
        await refreshConversation();
      }, "Starting report…"));
    }
    if (["Preparing", "Awaiting approval"].includes(entry.state) && entry.action === "managed") {
      card.append(button("Discard request", async () => {
        await api(projectPath(project) + "/requests/" + entry.id + "/discard", {});
        staged.delete(entry.id);
        await refreshConversation();
      }));
    }
    if (entry.action === "managed") {
      if (["Running", "Preparing"].includes(entry.state)) {
        const live = element("p", entryProgressText(entry));
        live.dataset.progressEntry = entry.id;
        card.append(live);
      }
      card.append(usageRow(entry.usage, ["Running", "Preparing", "Awaiting approval"].includes(entry.state)));
    } else if (entry.action === "native") card.append(usageRow({basis: "not_observed"}, false));
    if (entry.state === "Running" && entry.operation_id) {
      const state = await api("/operations/" + entry.operation_id).catch(() => null);
      if (state?.cancellable) card.append(button("Request cancellation", async () => {
        await api("/operations/" + entry.operation_id + "/cancel", {});
        say("Cancellation requested.");
      }));
    }
    if (entry.state === "Opened" || entry.state === "Command shown") {
      const receipt = nativeReceipts.get(entry.id);
      if (receipt?.url) card.append(external("Open " + names[entry.app], receipt.url));
      if (receipt?.command_line) card.append(button("Show command", () => showNative(entry, receipt)));
      if (entry.state === "Opened") {
        fileControl(card, entry.app === "ldr" ? "Attach LDR result" : "Attach native result", false, ".md,.txt,.json,.ris",
          async picked => {
            if (picked.length) await uploadAttachment(project, entry.id, {id: crypto.randomUUID(), file: picked[0]}, "report");
            await refreshConversation();
          });
      }
    }
    for (const id of entry.result_ids) {
      if (lookup.has(id)) {
        try { await showOutput(card, project, lookup.get(id), current); }
        catch (error) { card.append(element("p", error.message)); }
      }
    }
    container.append(card);
  }
  if (generation !== routeGeneration || screen !== "workspace") return;
  const nearEnd = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 100;
  transcript.replaceChildren(container);
  if (nearEnd) transcript.scrollTop = transcript.scrollHeight;
  historySignature = signature;
}

// ---- Status polling (spec §3.4): one timer per open chat, tiered by what is active, with back-off on failure.

const FAST = 1600;
const SLOW = 6000;
const BACKOFF = [1600, 3200, 6400];
let pollFailures = 0;
let inFlight = 0;
let lastTier = null;

function clock(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const minutes = Math.floor(seconds / 60);
  return minutes + ":" + String(Math.floor(seconds % 60)).padStart(2, "0");
}

function age(serverTime, stamp) {
  // Ages use the server's clock from the same response, never the browser clock.
  if (!serverTime || !stamp) return null;
  return Math.max(0, (Date.parse(serverTime) - Date.parse(stamp)) / 1000);
}

function entryProgressText(entry) {
  return names[entry.app] + " · " + entry.state + (entry.elapsed_seconds !== null && entry.elapsed_seconds !== undefined
    ? " · " + clock(entry.elapsed_seconds) + " elapsed" : "");
}

function pollTier(next) {
  const entries = next?.entries || [];
  const runs = next?.pipeline_runs || [];
  const steps = runs.flatMap(run => run.steps);
  if (inFlight || entries.some(entry => ["Running", "Preparing"].includes(entry.state))
      || steps.some(step => ["queued", "surveying", "running", "waiting_app"].includes(step.state))
      || runs.some(run => ["queued", "surveying", "running", "waiting_app", "brief_approved"].includes(run.state))) return FAST;
  if (entries.some(entry => entry.state === "Awaiting approval")
      || runs.some(run => ["awaiting_scope", "awaiting_papers", "awaiting_disclosure", "reconfirm_recipients", "paused", "interrupted"].includes(run.state)))
    return SLOW;
  return null;
}

function schedule(delay) {
  clearTimeout(poll);
  poll = delay ? setTimeout(() => refreshConversation().catch(() => {}), delay) : undefined;
}

function reconnecting(on) {
  for (const header of document.querySelectorAll(".guided-run > .turn-meta, .turn > .turn-meta")) {
    let note = header.querySelector(".reconnecting");
    if (on && !note) {
      note = element("span", "Reconnecting to RAH…");
      note.className = "reconnecting";
      header.append(note);
    } else if (!on) note?.remove();
  }
  if (on && !document.querySelector(".reconnecting")) say("Reconnecting to RAH…");
  else if (!on && message.textContent === "Reconnecting to RAH…") say("");
}

function updateProgress(next) {
  // Progress-only changes update in place, so open details and focused controls are untouched.
  for (const run of next?.pipeline_runs || []) {
    for (const step of run.steps) {
      for (const node of document.querySelectorAll(`[data-progress-step="${step.id}"]`)) {
        if (node.tagName === "PROGRESS") {
          const percent = step.progress?.percent;
          if (typeof percent === "number") node.value = percent;
          else node.removeAttribute("value");
        } else {
          const text = stepProgressText(step);
          node.textContent = text.text;
          node.classList.toggle("stalled", text.stalled);
        }
      }
    }
    for (const node of document.querySelectorAll(`[data-progress-run="${run.id}"]`)) node.textContent = nowLine(run);
  }
  for (const entry of next?.entries || []) {
    for (const node of document.querySelectorAll(`[data-progress-entry="${entry.id}"]`)) node.textContent = entryProgressText(entry);
  }
}

async function refreshConversation() {
  clearTimeout(poll);
  const generation = routeGeneration;
  const {chat} = locationState;
  if (screen !== "workspace") return;
  const scope = destination();
  const path = projectPath(scope);
  let result;
  try {
    result = await Promise.all([
      chat ? api(path + "/chats/" + chat) : null,
      scope ? api(path) : null
    ]);
  } catch (error) {
    if (generation !== routeGeneration || screen !== "workspace") throw error;
    pollFailures += 1;
    reconnecting(true);
    // Never stop while work is active or waiting: back off, then keep trying every 10 s.
    if (lastTier || inFlight) schedule(BACKOFF[pollFailures - 1] || 10000);
    throw error;
  }
  if (generation !== routeGeneration || screen !== "workspace") return;
  if (pollFailures) { pollFailures = 0; reconnecting(false); }
  const stickToEnd = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 100;
  const oldState = JSON.stringify([conversation?.entries.map(entry => entry.state), conversation?.pipeline_runs?.map(run => run.state)]);
  [conversation] = result;
  artifacts = result[1]?.artifact || [];
  const last = conversation?.entries.findLast(item => item.app === "pqa" && item.input_ids.length > 0 && item.state !== "Preparing");
  availableCorpus = last?.input_ids.flatMap(id => {
    const item = artifacts.find(file => file.id === id);
    return item ? [{id, name: item.original_name}] : [];
  }) || [];
  updateProgress(conversation);
  await renderTranscript(conversation, artifacts, generation);
  if (generation !== routeGeneration || screen !== "workspace") return;
  const newState = JSON.stringify([conversation?.entries.map(entry => entry.state), conversation?.pipeline_runs?.map(run => run.state)]);
  if (oldState !== newState || !composer.childElementCount) renderComposer();
  if (stickToEnd) transcript.scrollTop = transcript.scrollHeight;
  lastTier = pollTier(conversation);
  schedule(lastTier);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && screen === "workspace") refreshConversation().catch(() => {});
});

function navHeader() {
  const {project, chat} = locationState;
  const currentChat = workspace.chats.find(item => item.id === chat);
  const label = project ? projectLabel(project) : "Workspace";
  breadcrumbs.replaceChildren();
  if (project && currentChat) {
    breadcrumbs.append(button(label, () => route(project)), element("span", " / "), element("strong", currentChat.title));
  } else breadcrumbs.append(element("strong", currentChat?.title || label));
  projectTools.replaceChildren();
  if (currentChat || project && workspace.projects.some(item => item.id === project)) {
    projectTools.append(button("Files", () => route(project, chat, "files")),
      button("Open folder", () => api(projectPath(destination()) + "/open-folder", {chat_id: chat})));
    if (project) projectTools.append(button("Papers Needed", () => route(project, chat, "papers")),
      button("New chat in project", () => newChat(project)));
  }
  if (locationState.page || screen !== "workspace") projectTools.prepend(button("Back to chat", () => route(project, chat)));
  if (currentChat && screen === "workspace") {
    const rename = button("Rename", () => editName("Rename chat", currentChat.title, async title => {
      await api(projectPath(destination()) + "/chats/" + chat, {title});
      await openWorkspace();
    }));
    rename.setAttribute("aria-label", "Rename chat");
    projectTools.append(rename);
    projectTools.append(button("Delete chat", () => {
      const {dialog, act, show} = modal("Delete chat?");
      dialog.append(element("p", "Move this chat and its private files to Trash? Project files and native app history are kept."));
      show(act("Cancel", () => dialog.close()), act("Move to Trash", async () => {
        await api("/chats/" + chat + "/trash", {});
        drafts.delete(chat);
        dialog.close();
        await route(project);
      }, true));
    }));
  }
}

async function openWorkspace() {
  const generation = ++routeGeneration;
  clearTimeout(poll);
  say("");
  locationState = readRoute();
  const fresh = await api("/workspace");
  if (generation !== routeGeneration) return;
  workspace = fresh;
  const legacy = workspace.chats.find(item => item.id === locationState.chat && item.legacy_project_id && item.legacy_project_id === locationState.project);
  if (legacy) {
    locationState.project = undefined;
    history.replaceState({}, "", "#chat/" + legacy.id + (locationState.page === "files" ? "/files" : ""));
    if (locationState.page === "papers") locationState.page = undefined;
  }
  if (locationState.chat && !workspace.chats.some(item => item.id === locationState.chat && (item.project_id || undefined) === locationState.project)) {
    locationState = {project: locationState.project};
    say("This chat is unavailable.");
  }
  const savedChat = workspace.chats.find(item => item.id === locationState.chat);
  if (savedChat && !drafts.has(savedChat.id)) draft().app = savedChat.app;
  renderSidebar();
  if (locationState.page) {
    screen = locationState.page;
    view.className = "secondary";
    navHeader();
    await showProject(destination(), undefined, locationState.page, locationState.chat);
    return;
  }
  screen = "workspace";
  document.querySelectorAll("[data-view]").forEach(node => node.removeAttribute("aria-current"));
  navHeader();
  view.className = "workspace";
  transcript = element("div");
  transcript.className = "transcript";
  transcript.setAttribute("aria-label", "Chat history");
  composer = element("div");
  composer.className = "composer";
  view.replaceChildren(transcript, composer);
  conversation = null;
  historySignature = undefined;
  await refreshConversation();
}

function tool(name) {
  routeGeneration++;
  clearTimeout(poll);
  view.className = "secondary";
  screen = name;
  navHeader();
  return choose(name);
}

const layout = document.querySelector(".shell");
const toggle = document.querySelector("#sidebar-toggle");
const narrow = matchMedia("(max-width: 720px)");
function sidebar(open) {
  layout.classList.toggle("sidebar-hidden", !open);
  document.querySelector("#sidebar").inert = !open;
  toggle.setAttribute("aria-expanded", String(open));
}
sidebar(!narrow.matches);
narrow.addEventListener("change", event => sidebar(!event.matches));
toggle.onclick = () => sidebar(toggle.getAttribute("aria-expanded") !== "true");
if (token) {
  for (const item of document.querySelectorAll("#sidebar button")) item.disabled = false;
  document.querySelectorAll("[data-view]").forEach(node => { node.onclick = () => { say(""); tool(node.dataset.view); }; });
  const newChatButton = document.querySelector("#new-chat");
  newChatButton.onclick = async () => {
    if (newChatButton.disabled) return;
    const label = [...newChatButton.childNodes];
    newChatButton.disabled = true;
    newChatButton.setAttribute("aria-busy", "true");
    newChatButton.textContent = "Creating chat…";
    try { await newChat(); }
    catch (error) { say(error.message); }
    finally {
      newChatButton.replaceChildren(...label);
      newChatButton.disabled = false;
      newChatButton.removeAttribute("aria-busy");
    }
  };
  document.querySelector("#new-project").onclick = () => editName("New project", "", async name => {
    const item = await api("/projects", {name});
    expanded.add(item.id);
    await route(item.id);
  });
  addEventListener("popstate", () => openWorkspace().catch(error => say(error.message)));
  openWorkspace().catch(error => say(error.message));
}
