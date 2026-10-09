"use strict";
const $ = (id) => document.getElementById(id);
let currentOperation = null, askController = null;
let apiKey = "", config = { modes: ["bm25"], generation: false }, documents = [];
const modeNames = { bm25: "Keyword · BM25", dense: "Semantic · neural", hybrid: "Hybrid · fused" };

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function notice(message) { $("notice").textContent = message; $("notice").hidden = !message; }
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), ...(apiKey ? { "X-API-Key": apiKey } : {}) };
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !$("settings-dialog").open) $("settings-dialog").showModal();
    throw new Error(typeof data.detail === "string" ? data.detail : Array.isArray(data.detail) ? data.detail.map(e => e.msg).join("; ") : `Request failed (${response.status}).`);
  }
  return data;
}
function post(path, body) { return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
function changeView(view) {
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `view-${view}`;
  for (const button of document.querySelectorAll(".nav")) {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  }
  const names = { ask: "Ask your docs", library: "Document library", compare: "Compare retrieval", activity: "System activity", model: "Model setup" };
  $("breadcrumb").textContent = `Workspace / ${names[view]}`;
  notice("");
  if (view === "model") loadProvider().catch(e => notice(e.message));
  if (view === "activity") loadActivity().catch(e => notice(e.message));
}
document.querySelectorAll("[data-view]").forEach(b => b.addEventListener("click", () => changeView(b.dataset.view)));
document.querySelectorAll("[data-go]").forEach(b => b.addEventListener("click", () => changeView(b.dataset.go)));
$("settings-open").addEventListener("click", () => $("settings-dialog").showModal());
$("settings-close").addEventListener("click", () => $("settings-dialog").close());
$("source-close").addEventListener("click", () => $("source-dialog").close());
$("key-form").addEventListener("submit", async e => {
  e.preventDefault(); apiKey = $("api-key").value; $("api-key").value = "";
  $("settings-dialog").close(); await initialize();
});

async function initialize() {
  try {
    config = await api("/api/config");
    $("mode").replaceChildren(...config.modes.map(mode => {
      const option = node("option", modeNames[mode]); option.value = mode; return option;
    }));
    $("mode").value = config.default_mode;
    $("connection").textContent = config.provider?.last_check?.status === "ready" ? "Model tested" : config.generation ? "Model configured · untested" : "Evidence mode";
    $("privacy-label").textContent = config.provider?.privacy === "remote" ? "◈ Remote model · sends passages" : "◈ Local workspace";
    $("compare-hint").textContent = config.modes.length === 1 ? "Keyword retrieval is enabled. Install the semantic extra, enable fastembed, and re-index to compare neural and hybrid retrieval." : "Compare the same corpus using BM25 keywords, neural embeddings, and reciprocal rank fusion. Evaluation datasets and regression gates are available through the CLI.";
    await loadLibrary(); notice("");
  } catch (e) { $("connection").textContent = "Connection required"; notice(e.message); }
}

async function loadLibrary() {
  const data = await api("/api/documents"); documents = data.documents;
  $("nav-count").textContent = documents.length;
  $("library-count").textContent = `${documents.length} document${documents.length === 1 ? "" : "s"}`;
  const total = documents.reduce((sum, d) => sum + d.chunks, 0);
  $("corpus-summary").textContent = `${documents.length} documents · ${total} indexed passages`;
  const previous = $("doc-filter").value;
  const all = node("option", "All documents"); all.value = "";
  $("doc-filter").replaceChildren(all, ...documents.map(d => {
    const option = node("option", d.filename); option.value = d.id; return option;
  }));
  if (documents.some(d => d.id === previous)) $("doc-filter").value = previous;
  const list = $("library-list"); list.replaceChildren();
  if (!documents.length) list.append(node("div", "Your library is ready. Add a document above to make it searchable.", "empty-state"));
  for (const doc of documents) {
    const row = node("article", undefined, "document-row");
    row.append(node("span", doc.filename.split(".").pop().toUpperCase().slice(0, 5), "file-icon"));
    const info = node("div", undefined, "document-info");
    info.append(node("h3", doc.filename), node("p", `${doc.chunks} passages · ${(doc.byte_size / 1024).toFixed(1)} KB · updated ${new Date(doc.updated_at).toLocaleDateString()}`));
    const actions = node("div", undefined, "row-actions");
    const inspect = node("button", "Inspect ↗", "text-button"); inspect.addEventListener("click", () => openSource(doc.id));
    const remove = node("button", "Remove", "delete-button");
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Remove ${doc.filename} and its indexed passages? You can re-upload the document later.`)) return;
      remove.disabled = true;
      try { await api(`/api/documents/${doc.id}`, { method: "DELETE" }); await loadLibrary(); $("answer-region").replaceChildren(); }
      catch (e) { notice(e.message); remove.disabled = false; }
    });
    actions.append(inspect, remove); row.append(info, actions); list.append(row);
  }
}

$("upload-form").addEventListener("submit", async e => {
  e.preventDefault(); notice("");
  const files = Array.from($("files").files);
  if ($("source-url").value && files.length > 1) { notice("Attach a source URL when uploading one file at a time."); return; }
  $("upload-button").disabled = true;
  const outcomes = [];
  try {
    for (const file of files) {
      $("upload-status").textContent = `Indexing ${file.name}…`;
      if (file.size > config.max_upload_mb * 1024 * 1024) { outcomes.push(`${file.name}: exceeds upload limit`); continue; }
      const form = new FormData(); form.append("file", file); form.append("source", $("source-url").value);
      try { const result = await api("/api/documents", { method: "POST", body: form }); outcomes.push(`${file.name}: ${result.status}`); }
      catch (e) { outcomes.push(`${file.name}: ${e.message}`); }
    }
    $("upload-status").textContent = outcomes.join(" · ");
    $("files").value = ""; $("source-url").value = ""; await loadLibrary();
  } catch (e) { notice(e.message); }
  finally { $("upload-button").disabled = false; }
});

async function openSource(id) {
  try {
    const doc = await api(`/api/documents/${encodeURIComponent(id)}`);
    $("source-title").textContent = doc.filename;
    const content = $("source-content"); content.replaceChildren();
    if (/^https?:\/\//.test(doc.source)) {
      const link = node("a", "Open original source ↗", "source-link"); link.href = doc.source; link.target = "_blank"; link.rel = "noopener noreferrer"; content.append(link);
    }
    for (const passage of doc.passages) {
      const article = node("article", undefined, "passage");
      article.append(node("h3", `${passage.section}${passage.page ? ` · page ${passage.page}` : ""}`), node("pre", passage.text)); content.append(article);
    }
    $("source-dialog").showModal();
  } catch (e) { notice(e.message); }
}

function evidenceCard(hit, index) {
  const card = node("article", undefined, "evidence-card");
  const title = node("h3", `[${index + 1}] ${hit.filename}`);
  const inspect = node("button", "Inspect ↗", "text-button"); inspect.addEventListener("click", () => openSource(hit.document_id));
  card.append(title, node("div", `${hit.section}${hit.page ? ` · page ${hit.page}` : ""}`, "evidence-meta"), node("pre", hit.text), inspect);
  return card;
}

$("ask-form").addEventListener("submit", async e => {
  e.preventDefault(); notice("");
  $("ask-button").disabled = true; $("ask-empty").hidden = true;
  currentOperation = crypto.randomUUID(); askController = new AbortController(); $("cancel-ask").hidden = false;
  const region = $("answer-region"); region.replaceChildren(node("div", "Searching your documentation…", "loading"));
  try {
    const data = await api("/api/ask", { method: "POST", signal: askController.signal, headers: {"Content-Type": "application/json"}, body: JSON.stringify({ question: $("question").value, mode: $("mode").value, document_id: $("doc-filter").value || null, operation_id: currentOperation }) });
    region.replaceChildren(); const card = node("article", undefined, "answer-card");
    const label = node("div", undefined, "answer-label"); label.append(node("span", data.status === "answered" ? "Draft answer · quotes checked" : data.status.replaceAll("_", " ")), node("span", `${data.latency_ms} ms · ${data.mode}`));
    card.append(label, node("p", data.message, "answer-status"));
    for (const claim of data.claims) {
      const paragraph = node("div", claim.text, "claim");
      const index = data.hits.findIndex(h => h.id === claim.chunk_id);
      const cite = node("button", `[${index + 1}]`, "citation-button");
      cite.addEventListener("click", () => openSource(data.hits[index].document_id));
      paragraph.append(cite, node("small", `“${claim.quote}”`)); card.append(paragraph);
    }
    const feedback = node("div", undefined, "feedback"); feedback.append(node("span", "Was this useful?"));
    for (const [text, helpful] of [["Yes", true], ["No", false]]) {
      const button = node("button", text, "secondary");
      button.addEventListener("click", async () => {
        try { await post("/api/feedback", { request_id: data.request_id, helpful }); feedback.replaceChildren(node("span", "Feedback saved. Thank you.")); }
        catch (e) { notice(e.message); }
      }); feedback.append(button);
    }
    card.append(feedback); region.append(card);
    region.append(node("div", `RETRIEVED EVIDENCE · ${data.hits.length} PASSAGES`, "evidence-heading"));
    data.hits.forEach((hit, i) => region.append(evidenceCard(hit, i)));
  } catch (e) { region.replaceChildren(node("div", e.name === "AbortError" ? "Question cancelled. The model server may finish work already dispatched." : e.message, "notice")); }
  finally { $("ask-button").disabled = false; $("cancel-ask").hidden = true; currentOperation = null; askController = null; }
});

$("compare-form").addEventListener("submit", async e => {
  e.preventDefault(); notice(""); $("compare-button").disabled = true;
  const comparison = $("comparison"); comparison.replaceChildren(); comparison.classList.toggle("single", config.modes.length === 1);
  try {
    const outcomes = await Promise.allSettled(config.modes.map(mode => post("/api/search", { question: $("compare-question").value, mode, k: 4 })));
    outcomes.forEach((result, i) => {
      const column = node("section"); column.append(node("h3", modeNames[config.modes[i]]));
      if (result.status === "fulfilled") {
        column.append(node("p", `${result.value.latency_ms} ms · ${result.value.hits.length} passages`, "hint"));
        result.value.hits.forEach((hit, n) => column.append(evidenceCard(hit, n)));
        if (!result.value.hits.length) column.append(node("p", "No matching passages.", "hint"));
      } else column.append(node("p", result.reason.message, "error-text"));
      comparison.append(column);
    });
  } finally { $("compare-button").disabled = false; }
});

async function loadActivity() {
  const data = await api("/api/metrics");
  const cards = $("activity-cards"); cards.replaceChildren();
  for (const [value, label] of [[data.recent_requests, "Requests in latency window"], [`${data.p95_ms} ms`, "Request latency · p95"], [documents.length, "Indexed documents"]]) {
    const card = node("div", undefined, "metric"); card.append(node("strong", value), node("span", label)); cards.append(card);
  }
  const table = node("table"), head = node("thead"), title = node("tr"), body = node("tbody");
  title.append(node("th", "Event"), node("th", "Count")); head.append(title);
  for (const [key, value] of Object.entries(data.counts)) { const row = node("tr"); row.append(node("td", key.replaceAll("_", " ")), node("td", value)); body.append(row); }
  table.append(head, body); $("activity-table").replaceChildren(table);
}
$("refresh-activity").addEventListener("click", () => loadActivity().catch(e => notice(e.message)));
initialize();


$("cancel-ask").addEventListener("click", async () => {
  if (!currentOperation) return;
  $("cancel-ask").disabled = true;
  try {
    await api(`/api/requests/${currentOperation}`, {method: "DELETE"});
    askController?.abort();
  } catch (e) { notice(e.message); }
  finally { $("cancel-ask").disabled = false; }
});

function providerValue() {
  const value = {
    kind: $("provider-kind").value, base_url: $("provider-url").value,
    model: $("provider-model").value, response_format: $("provider-format").value,
    timeout_seconds: Number($("provider-timeout").value), context_tokens: Number($("provider-context").value),
    max_output_tokens: Number($("provider-output").value), clear_key: $("provider-clear-key").checked,
  };
  if ($("provider-key").value) value.api_key = $("provider-key").value;
  return value;
}
function providerPrivacy() {
  const kind = $("provider-kind").value;
  $("provider-fields").hidden = kind === "disabled";
  $("provider-fields").querySelectorAll("input, select").forEach(el => el.disabled = kind === "disabled");
  $("provider-check").disabled = kind === "disabled";
  $("provider-privacy").textContent = kind === "api" ? "Your questions and selected document passages will be sent to this provider. Use an HTTPS endpoint you trust." : kind === "lmstudio" ? "Questions and passages go to your local LM Studio server. No external API fallback is used." : "Show retrieved passages without calling a generation model.";
}
$("provider-kind").addEventListener("change", () => {
  if ($("provider-kind").value === "lmstudio") $("provider-url").value = "http://127.0.0.1:1234/v1";
  if ($("provider-kind").value === "api" && $("provider-url").value.includes("127.0.0.1")) $("provider-url").value = "";
  $("provider-key").value = ""; providerPrivacy();
});
async function loadProvider() {
  const p = await api("/api/provider");
  $("provider-kind").value = p.kind; $("provider-url").value = p.base_url; $("provider-model").value = p.model;
  $("provider-format").value = p.response_format; $("provider-timeout").value = p.timeout_seconds;
  $("provider-context").value = p.context_tokens; $("provider-output").value = p.max_output_tokens;
  $("provider-key").value = ""; $("provider-clear-key").checked = false;
  $("provider-key-state").textContent = p.key_configured ? "A provider key is saved. It is not shown here." : "No provider key saved.";
  $("provider-status").textContent = p.last_check ? `${p.last_check.message} Last tested: ${new Date(p.last_check.checked_at).toLocaleString()}` : p.configured ? "Settings loaded. Test generation to check the model." : "Start LM Studio, discover models, choose a model, and save your settings.";
  providerPrivacy();
}
async function providerAction(button, action) {
  const controls = [$("provider-save"), $("provider-check"), $("discover-models")];
  controls.forEach(b => b.disabled = true); $("provider-status").textContent = button === "provider-check" ? "Testing real model generation… This may take longer while the model loads." : "Connecting…";
  try { await action(); }
  catch (e) { $("provider-status").textContent = e.message; }
  finally { controls.forEach(b => b.disabled = false); if ($("provider-kind").value === "disabled") $("provider-check").disabled = true; }
}
$("provider-form").addEventListener("submit", e => {
  e.preventDefault();
  providerAction("provider-save", async () => {
    await api("/api/provider", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(providerValue())});
    $("provider-key").value = ""; await loadProvider(); await initialize();
    $("provider-status").textContent = "Settings saved. Run Test generation to check the selected model.";
  });
});
$("discover-models").addEventListener("click", () => providerAction("discover-models", async () => {
  const data = await post("/api/provider/models", providerValue());
  $("provider-models").replaceChildren(...data.models.map(id => {const option = node("option", id); option.value = id; return option;}));
  $("provider-status").textContent = data.models.length ? `${data.models.length} models discovered. Choose a chat model in Model ID, save settings, then test generation.` : "No models found. Download and load a chat model in LM Studio.";
}));
$("provider-check").addEventListener("click", () => providerAction("provider-check", async () => {
  const data = await post("/api/provider/check", providerValue());
  $("provider-status").textContent = `${data.message} ${data.latency_ms} ms · ${data.model}`;
  await initialize();
}));
