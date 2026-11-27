"use strict";

(async () => {
  const key = "rah-native-handoff";
  const fragment = /^#rah-handoff=([0-9a-f]{64})$/.exec(location.hash);
  const field = document.querySelector("#researchForm #task, textarea#query[name=query]");
  const ldr = !document.querySelector("#researchForm #task");
  const request = (path, ticket, body, headers = {}) => fetch("/_rah_handoff_" + path, {
    method: "POST", credentials: "same-origin", redirect: "error", cache: "no-store", body,
    headers: {"X-RAH-Ticket": ticket, ...headers},
  });
  const notice = (text, near) => {
    const box = document.createElement("p");
    box.setAttribute("role", "status");
    box.textContent = text;
    if (near) near.after(box);
    else document.body.prepend(box);
    return box;
  };
  try {
    if (fragment) {
      sessionStorage.setItem(key, fragment[1]);
      history.replaceState(history.state, "", location.pathname + location.search);
    }
    const ticket = sessionStorage.getItem(key);
    if (!ticket) return;
    // Take a staged ticket once per tab. A reopened handoff carries no draft; it only enables saving a result.
    if (fragment || (field && sessionStorage.getItem(key + "-received") !== ticket)) {
      const response = await request("take", ticket);
      if (!response.ok) {
        sessionStorage.removeItem(key);
        if (field) notice("RAH could not fill this field. Copy the draft from RAH and paste it here.", field);
        return;
      }
      const {question, export: canExport} = await response.json();
      if (question && field) {
        // Don't auto-submit or overwrite a question the user has already typed.
        if (field.value && field.value !== question) {
          const box = notice("A RAH draft is ready. ", field);
          const use = document.createElement("button");
          use.type = "button";
          use.textContent = "Replace with RAH draft";
          use.onclick = () => { field.value = question; field.dispatchEvent(new Event("input", {bubbles: true})); box.remove(); };
          box.append(use);
        } else {
          field.value = question;
          field.dispatchEvent(new Event("input", {bubbles: true}));
          notice("Draft from RAH. Review it and your LDR model and search settings before starting research.", field);
        }
      } else if (!question && canExport) {
        notice("RAH handoff ready. Open the finished result from LDR history to save it to RAH. Research is not resubmitted.");
      }
      sessionStorage.setItem(key + "-received", ticket);
      if (!canExport) sessionStorage.removeItem(key);
    }
    if (ldr && sessionStorage.getItem(key + "-received") === ticket) {
      const match = /\/(?:results|details)\/([0-9a-f-]{36})\/?$/i.exec(location.pathname);
      if (!match) return;
      const id = match[1];
      const box = notice("RAH · result " + id + ": ");
      for (const [fmt, text] of [["md", "Save report to RAH"], ["ris", "Save RIS to RAH"]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = text;
        button.onclick = async () => {
          button.disabled = true;
          try {
            const csrf = window.api?.getCsrfToken() || "";
            const native = await fetch(fmt === "ris" ? `/api/v1/research/${id}/export/ris` : `/api/report/${id}`, {
              method: fmt === "ris" ? "POST" : "GET", credentials: "same-origin", redirect: "error",
              headers: fmt === "ris" ? {"X-CSRFToken": csrf} : {},
            });
            if (!native.ok) throw Error();
            const payload = fmt === "ris" ? await native.blob() : (await native.json()).content;
            if (!payload) throw Error();
            const saved = await request("export", ticket, payload, {"X-RAH-Format": fmt, "X-RAH-Result": id});
            if (!saved.ok) throw Error();
            button.textContent = "Saved to RAH";
          } catch {
            button.textContent = text;
            button.disabled = false;
            notice("Could not save to RAH. Download the native export and use Import LDR export in RAH instead.", box);
          }
        };
        box.append(button, " ");
      }
    }
  } catch {
    notice("RAH handoff is unavailable. Copy and paste the question from RAH.", field);
  }
})();
