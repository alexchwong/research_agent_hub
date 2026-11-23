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
function projectLabel(project) { return workspace?.projects.find(item => item.id === project)?.name || "Unfiled"; }
function destination() { return locationState.project || workspace.unfiled; }
function draft() {
  const key = locationState.chat || "new:" + destination();
  if (!drafts.has(key)) drafts.set(key, {app: "gptr", questions: {}, quote: "", pdfs: [], files: [], mode: "research_report"});
  return drafts.get(key);
}

function route(project, chat, page) {
  const path = [project, chat || "-", page].filter(Boolean).join("/");
  const hash = project ? "#" + path.replace(/\/-$/, "") : "";
  const next = hash || "/";
  if (location.hash !== hash) history.pushState({}, "", next);
  if (narrow.matches) sidebar(false);
  return openWorkspace();
}

function readRoute() {
  const [project, chat, page, extra] = location.hash.slice(1).split("/");
  if (!UUID.test(project || "") || extra !== undefined) return {};
  return {project, chat: UUID.test(chat || "") ? chat : undefined,
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
  function act(text, action, primary = false) {
    const node = button(text, async () => {
      try { status.hidden = true; await action(); }
      catch (error) { status.hidden = false; status.textContent = error.message; }
    });
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
  const save = act("Save", async () => { await action(input.value); dialog.close(); }, true);
  input.addEventListener("keydown", event => { if (event.key === "Enter") save.click(); });
  show(act("Cancel", () => dialog.close()), save);
  input.select();
}

function chatLink(chat) {
  const link = button(chat.title, () => route(chat.project_id, chat.id));
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
  const items = workspace.projects.filter(item => item.id !== workspace.unfiled);
  items.push({id: workspace.unfiled, name: "Unfiled"});
  projectList.replaceChildren(...items.map(project => {
    const group = element("details");
    group.open = expanded.has(project.id) || locationState.project === project.id;
    const toggle = element("summary", project.name);
    group.ontoggle = () => { if (group.open) expanded.add(project.id); else expanded.delete(project.id); };
    const open = button("Open project", () => route(project.id));
    const chats = workspace.chats.filter(chat => chat.project_id === project.id);
    group.append(toggle, open, ...chats.map(chatLink));
    return group;
  }));
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

function paperReview(entry, info) {
  const {dialog, act, show} = modal("Review PaperQA2 transfer");
  const list = element("ul");
  list.append(...info.files.map(name => element("li", name)));
  dialog.append(element("p", info.files.length + (info.files.length === 1 ? " PDF" : " PDFs") + " · PaperQA2 → OpenRouter → Azure"),
    list, element("p", "PaperQA2 parses these PDFs locally and sends extracted text, question and excerpts, and citation details for analysis and embeddings. Azure is required by the routing policy. The embedding response does not independently identify the serving provider. Charges may apply."),
    element("p", info.question));
  if (info.quote) dialog.append(element("blockquote", info.quote));
  const details = element("details");
  details.append(element("summary", "Models and endpoints"), ...info.profile.destinations.map(row =>
    element("p", row.role + " · " + row.model + " · " + row.endpoint)));
  dialog.append(details, element("p", "By approving, you confirm the key excludes this run from prompt logging or additional trace exports."),
    external("OpenRouter privacy settings (ZDR optional)", "https://openrouter.ai/settings/privacy"));
  async function decide(approve) {
    for (const node of dialog.querySelectorAll("button")) node.disabled = true;
    try {
      await api(projectPath(entry.project_id) + "/pqa-approve", {preview_id: info.preview_id, binding: info.binding,
        approve, account_exports_checked: approve});
      dialog.close();
      await refreshConversation();
    } finally {
      for (const node of dialog.querySelectorAll("button")) node.disabled = false;
    }
  }
  dialog.addEventListener("cancel", event => { event.preventDefault(); dialog.querySelector("button").click(); });
  show(act("Cancel", () => decide(false)), act("Approve and run", () => decide(true), true));
}

async function dispatchPaper(entry, local, ids) {
  if (entry.state === "Preparing") {
    for (const item of local.files) {
      const artifact = await uploadAttachment(entry.project_id, entry.id, item, "pdf");
      if (!ids.includes(artifact)) ids.push(artifact);
    }
    local.files = [];
  }
  const info = await api(projectPath(entry.project_id) + "/pqa-preview", {entry_id: entry.id, pdfs: ids});
  staged.delete(entry.id);
  await refreshConversation();
  if (locationState.chat === entry.chat_id && screen === "workspace") paperReview(entry, info);
}

async function nativeLaunch(entry) {
  const endpoint = "/apps/" + entry.app;
  const request = {entry_id: entry.id, project_id: entry.project_id};
  let result = await api(endpoint + "/launch", request);
  while (result.state === "Starting") {
    await new Promise(resolve => setTimeout(resolve, 700));
    result = await api(endpoint + "/browser");
  }
  if (result.generation && result.state === "Ready" && !result.opened) {
    result = await api(endpoint + "/launch", {...request, ready_generation: result.generation});
  }
  if (!result.url && !result.command_line) throw new Error(result.state || "The native app could not open.");
  nativeReceipts.set(entry.id, result);
  return result;
}

function showNative(entry, result) {
  const {dialog, act, show} = modal(result.command_line ? "Standalone PaperQA2" : names[entry.app] + " is ready");
  if (result.command_line) dialog.append(element("p", result.note), element("pre", result.command_line));
  else dialog.append(element("p", "Enter the question in the native app. Attach the exported report or RIS here when it is ready."),
    external("Open " + names[entry.app], result.url));
  show(act("Close", () => dialog.close()), ...(result.command_line ? [
    act("Copy command", async () => { await navigator.clipboard.writeText(result.command_line); dialog.close(); }, true)
  ] : []));
}

async function submit(current, data) {
  const existing = current.submit;
  const intent = JSON.stringify(data);
  const payload = existing?.intent === intent ? existing.payload : {...data, entry_id: crypto.randomUUID()};
  current.submit = {intent, payload};
  const entry = await api("/workspace/requests", payload);
  current.submit = null;
  const visible = locationState.project === data.project_id && locationState.chat === data.chat_id && screen === "workspace";
  if (!entry.replay || entry.state === "Preparing") {
    if (data.app === "pqa" && data.action === "managed") {
      staged.set(entry.id, {files: [...current.files], pdfs: [...current.pdfs]});
      current.files = [];
      current.pdfs = [];
      current.quote = "";
    }
    current.questions[data.app] = "";
  }
  drafts.set(entry.chat_id, current);
  if (entry.replay && entry.state !== "Preparing") { if (visible) await route(entry.project_id, entry.chat_id); return; }
  if (visible) await route(entry.project_id, entry.chat_id);
  try {
    if (data.action === "native") {
      const native = await nativeLaunch(entry);
      if (locationState.chat === entry.chat_id && screen === "workspace") showNative(entry, native);
    } else if (entry.app === "gptr") {
      await api(projectPath(entry.project_id) + "/gptr-reports", {entry_id: entry.id});
    } else {
      const local = staged.get(entry.id);
      await dispatchPaper(entry, local, [...entry.input_ids]);
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

function confirmPublic(current, data, readiness) {
  const {dialog, act, show} = modal("Send a public question");
  dialog.append(element("p", "GPT Researcher sends the current question to " +
    readiness.recipients.join(" and ") + ". Research may also fetch public websites."),
    element("blockquote", data.question));
  show(act("Cancel", () => dialog.close()), act("Confirm and run", async () => {
    await submit(current, {...data, public: true});
    dialog.close();
  }, true));
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

function addLocalFiles(target, files) {
  for (const file of files) {
    if (!file.name.toLowerCase().endsWith(".pdf") || file.size > 256 * 1024 ** 2) {
      say("Select PDFs no larger than 256 MiB each.");
      continue;
    }
    if (!target.files.some(item => item.file.name === file.name && item.file.size === file.size
        && item.file.lastModified === file.lastModified)) target.files.push({id: crypto.randomUUID(), file});
  }
}

function fileControl(parent, label, multiple, extensions, action) {
  const field = element("label", label);
  field.className = "file-button";
  const input = element("input");
  input.type = "file";
  input.setAttribute("aria-label", label);
  input.accept = extensions;
  input.multiple = multiple;
  input.onchange = async () => {
    try { say(""); await action([...input.files]); }
    catch (error) { say(error.message); }
    finally { input.value = ""; }
  };
  field.append(input);
  parent.append(field);
  return field;
}

function setApp(current, app) {
  current.app = app;
  const {project, chat} = locationState;
  if (chat) api(projectPath(project) + "/chats/" + chat, {app}).catch(error => say(error.message));
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
    mode.value = current.mode;
    mode.onchange = () => { current.mode = mode.value; };
  }
  const field = element("textarea");
  field.className = "question";
  field.setAttribute("aria-label", app === "ldr" ? "Research task or note" : "Research question");
  field.placeholder = app === "pqa" ? "Ask a question about the papers" : app === "ldr" ? "Add a research task or note" : "Enter a public research question";
  field.maxLength = 8000;
  field.rows = 3;
  field.value = current.questions[app] || "";
  field.oninput = () => { current.questions[app] = field.value; };
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
    fileControl(attach, "Attach PDF", true, ".pdf", files => { addLocalFiles(current, files); renderComposer(); });
    if (project) attach.append(button("From project", () => picker(current, artifacts)));
    if (availableCorpus.length && !current.pdfs.length && !current.files.length) attach.append(button("Use last corpus", () => {
      current.pdfs = [...availableCorpus];
      renderComposer();
    }));
    composer.append(chips, attach);
  }
  const footer = element("div");
  footer.className = "composer-footer";
  const hint = element("p", !readiness.ready ? readiness.reason : app === "gptr" ?
    "Public questions only" : app === "pqa" ? "You review the selected material before transfer." : "Research runs in the native LDR window.");
  footer.append(hint);
  const action = app === "ldr" ? "native" : "managed";
  function payload(action) {
    return {app, action, project_id: project, chat_id: chat, question: field.value.trim(),
      ...(action === "managed" && app === "gptr" ? {mode: current.mode} : {}),
      ...(action === "managed" && app === "pqa" ? {quote: current.quote, pdfs: current.pdfs.map(item => item.id)} : {})};
  }
  if (app !== "ldr" && readiness.code !== "app_not_installed") {
    footer.append(button(app === "pqa" ? "Show standalone command" : "Open native app", () => submit(current, payload("native"))));
  }
  const send = button(!readiness.ready ? (readiness.code === "app_not_installed" || readiness.code === "unverified_profile" ? "App details" : "Finish setup")
    : app === "ldr" ? "Open LDR" : app === "pqa" ? "Analyse papers" : "Send", async () => {
    if (!readiness.ready) {
      if (readiness.code === "app_not_installed" || readiness.code === "unverified_profile") return tool("apps");
      await tool("settings");
      if (app === "gptr" && screen === "settings") {
        for (const details of view.querySelectorAll("details.settings-credentials")) details.open = true;
        view.querySelector("[data-key=RAH_NATIVE_GPTR__TAVILY_API_KEY]")?.focus();
      }
      return;
    }
    const data = payload(action);
    if (action === "managed" && !data.question) { field.focus(); throw new Error("Enter a question."); }
    if (app === "pqa") {
      const total = current.pdfs.length + current.files.length;
      if (!total || total > 40) throw new Error("Attach one to 40 PDFs.");
    }
    if (app === "gptr") return confirmPublic(current, data, readiness);
    await submit(current, data);
  });
  send.className = "primary";
  if (active && readiness.ready && action === "managed") {
    send.disabled = true;
    hint.textContent = "A request is active in this chat.";
  }
  field.onkeydown = event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !send.disabled) send.click();
  };
  footer.append(send);
  composer.append(footer);
}

async function renderTranscript(next, files, generation) {
  if (generation !== routeGeneration || !transcript?.isConnected) return;
  const entries = next?.entries || [];
  const signature = JSON.stringify(entries);
  if (historySignature === signature) return;
  const container = document.createDocumentFragment();
  const project = next?.project_id || destination();
  const current = draft();
  const lookup = new Map(files.map(item => [item.id, item]));
  if (!entries.length) {
    const welcome = element("div");
    welcome.className = "welcome";
    welcome.append(element("h1", "What are you researching?"), element("p", "Choose an app to start a chat."));
    container.append(welcome);
  }
  for (const row of entries) {
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
      if (entry.state === "Preparing") {
        const local = staged.get(entry.id) || {files: [], pdfs: []};
        staged.set(entry.id, local);
        const controls = element("div");
        controls.className = "sub-actions";
        const note = element("p", "Attach any remaining PDFs before reviewing this request.");
        function updateNote() {
          note.textContent = [...local.files.map(item => item.file.name), ...local.pdfs.map(item => item.name)].join(", ")
            || "Attach any remaining PDFs before reviewing this request.";
        }
        updateNote();
        fileControl(controls, "Attach remaining PDFs", true, ".pdf", chosen => { addLocalFiles(local, chosen); updateNote(); });
        controls.append(button("From project", () => picker(local, files, updateNote)));
        controls.append(button("Review transfer", async () => {
          const currentEntry = (await api(projectPath(project) + "/chats/" + entry.chat_id)).entries.find(item => item.id === entry.id);
          const ids = [...new Set([...local.pdfs.map(item => item.id), ...currentEntry.input_ids])];
          await dispatchPaper(entry, local, ids);
        }));
        card.append(note, controls);
      }
      if (entry.state === "Awaiting approval") {
        card.append(button("Review again", () => dispatchPaper(entry, {}, [...entry.input_ids])));
      }
      if (["Cancelled", "Failed", "Interrupted"].includes(entry.state)) {
        card.append(button("Use in a new attempt", () => {
          setApp(current, "pqa");
          current.questions.pqa = entry.question;
          current.quote = entry.quote;
          current.pdfs = entry.input_ids.filter(id => lookup.has(id)).map(id => ({id, name: lookup.get(id).original_name}));
          renderComposer();
          composer.querySelector("textarea").focus();
        }));
      }
    }
    if (entry.state === "Preparing" && entry.app === "gptr" && entry.action === "managed") {
      card.append(button("Continue public request", () => {
        const {dialog, act, show} = modal("Continue public request");
        dialog.append(element("p", "Send the saved public question to " +
          (workspace.readiness.gptr.recipients || []).join(" and ") + "."), element("blockquote", entry.question));
        show(act("Cancel", () => dialog.close()), act("Confirm and run", async () => {
          await api(projectPath(project) + "/gptr-reports", {entry_id: entry.id});
          dialog.close();
          await refreshConversation();
        }, true));
      }));
    }
    if (["Preparing", "Awaiting approval"].includes(entry.state) && entry.action === "managed") {
      card.append(button("Discard request", async () => {
        await api(projectPath(project) + "/requests/" + entry.id + "/discard", {});
        staged.delete(entry.id);
        await refreshConversation();
      }));
    }
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

async function refreshConversation() {
  clearTimeout(poll);
  const generation = routeGeneration;
  const {project, chat} = locationState;
  if (screen !== "workspace") return;
  const path = projectPath(project);
  const result = await Promise.all([
    chat ? api(path + "/chats/" + chat) : null,
    project && workspace.projects.some(item => item.id === project) ? api(path) : null
  ]);
  if (generation !== routeGeneration || screen !== "workspace") return;
  const stickToEnd = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 100;
  const oldState = JSON.stringify(conversation?.entries.map(entry => entry.state));
  [conversation] = result;
  artifacts = result[1]?.artifact || [];
  const last = conversation?.entries.findLast(item => item.app === "pqa" && item.input_ids.length > 0 && item.state !== "Preparing");
  availableCorpus = last?.input_ids.flatMap(id => {
    const item = artifacts.find(file => file.id === id);
    return item ? [{id, name: item.original_name}] : [];
  }) || [];
  await renderTranscript(conversation, artifacts, generation);
  if (generation !== routeGeneration || screen !== "workspace") return;
  if (oldState !== JSON.stringify(conversation?.entries.map(entry => entry.state)) || !composer.childElementCount) renderComposer();
  if (stickToEnd) transcript.scrollTop = transcript.scrollHeight;
  if (conversation?.entries.some(entry => ["Running", "Preparing", "Awaiting approval"].includes(entry.state))) {
    poll = setTimeout(() => refreshConversation().catch(error => say(error.message)), 1600);
  }
}

function navHeader() {
  const {project, chat} = locationState;
  const currentChat = workspace.chats.find(item => item.id === chat);
  const label = project ? projectLabel(project) : "Workspace";
  breadcrumbs.replaceChildren();
  if (project && currentChat) {
    breadcrumbs.append(button(label, () => route(project)), element("span", " / "), element("strong", currentChat.title));
  } else breadcrumbs.append(element("strong", label));
  projectTools.replaceChildren();
  if (project && workspace.projects.some(item => item.id === project)) {
    projectTools.append(button("Files", () => route(project, chat, "files")),
      button("Papers Needed", () => route(project, chat, "papers")));
  }
  if (locationState.page || screen !== "workspace") projectTools.prepend(button("Back to chat", () => route(project, chat)));
  if (currentChat && screen === "workspace") {
    const rename = button("Rename", () => editName("Rename chat", currentChat.title, async title => {
      await api(projectPath(project) + "/chats/" + chat, {title});
      await openWorkspace();
    }));
    rename.setAttribute("aria-label", "Rename chat");
    projectTools.append(rename);
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
  if (locationState.chat && !workspace.chats.some(item => item.id === locationState.chat && item.project_id === locationState.project)) {
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
    await showProject(locationState.project, undefined, locationState.page);
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
  document.querySelector("#new-chat").onclick = () => {
    if (!workspace) return;
    const project = locationState.project;
    drafts.delete("new:" + destination());
    route(project).catch(error => say(error.message));
    if (narrow.matches) sidebar(false);
  };
  document.querySelector("#new-project").onclick = () => editName("New project", "", async name => {
    const item = await api("/projects", {name});
    expanded.add(item.id);
    await route(item.id);
  });
  addEventListener("popstate", () => openWorkspace().catch(error => say(error.message)));
  openWorkspace().catch(error => say(error.message));
}
