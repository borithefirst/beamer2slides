// The workbench tab: a folder on the server, and the journeys run in it.
// A tool's form is built from the schema the server publishes, so what a visitor fills in and the
// signature that runs are the same text (src/beamer2slides/agent/schema.py).
// Everything is inside one function: app.js owns the page's globals, and `start`, `follow` and
// `table` are names it already uses.
"use strict";

(function () {

let ws = null, tools = null, shown = null, catalogue = null;
// What the open file said when it was opened, and what it said then. A journey rewrites the
// files it is pointed at (`doc_sync` regenerates the canonical HTML from the document it has
// just written), so a buffer opened before a run is older than the file: saving it back is a
// revert, and the next sync reads that as the source dropping what the rewrite brought in.
let stamp = null, loaded = "";

const TEXT = /\.(tex|html|json|md|txt|sty|cls|bib|csv|log|patch|diff|xml|ya?ml|cfg|toml|aux|out)$/i;
const IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i;

async function enter() {
  if (catalogue) return;
  try {
    if (!config) config = await api("/api/config");
    catalogue = await api("/api/tools");
    tools = catalogue.tools;
    const sel = $("#tool");
    for (const t of tools) sel.append(new Option(t.name, t.name));
    sel.onchange = pickTool;
    $("#savefile").onclick = saveFile;
    $("#delfile").onclick = deleteFile;
    $("#newfile").onclick = newFile;
    $("#runtool").onclick = runTool;
    $("#wbupload").onchange = e => e.target.files[0] && upload(e.target.files[0]);
    $("#wbfresh").onclick = fresh;
    $("#instructions").textContent = catalogue.instructions;
    pickTool();
    await session();
  } catch (e) {
    catalogue = null;
    note(e.message, true);
  }
}

async function session() {
  const kept = localStorage.getItem("b2s-ws");
  if (kept) {
    try { await api(`/api/ws/${kept}`); ws = kept; } catch { ws = null; }
  }
  if (!ws) {
    ws = (await api("/api/ws", { method: "POST" })).id;
    localStorage.setItem("b2s-ws", ws);
  }
  await listFiles();
  await openFile("README.md");
}

async function fresh() {
  if (!confirm("Start a new workspace? Everything in this one goes.")) return;
  localStorage.removeItem("b2s-ws");
  ws = null;
  await session();
}

// ---------------------------------------------------------------- files

async function listFiles() {
  const view = await api(`/api/ws/${ws}`);
  $("#filelist").replaceChildren(...view.files.map(f => {
    const li = document.createElement("li");
    if (f.dir) li.className = "dir";
    const b = document.createElement("button");
    b.textContent = f.path;
    b.title = f.dir ? f.path : `${f.path} — ${bytes(f.bytes)}`;
    b.onclick = () => f.dir || openFile(f.path);
    li.append(b);
    if (!f.dir) li.append(span(bytes(f.bytes)));
    return li;
  }));
  $("#wsnote").textContent =
    `${view.files.filter(f => !f.dir).length} files, ${bytes(view.bytes)} of ` +
    `${bytes(view.limits.bytes)}. A run is stopped after ${view.limits.seconds} s.`;
}

function bytes(n) {
  return n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} kB` : `${(n / 1048576).toFixed(1)} MB`;
}

function span(text, cls = "muted") {
  return Object.assign(document.createElement("span"), { className: cls, textContent: text });
}

function fileUrl(path) { return `/api/ws/${ws}/file?path=${encodeURIComponent(path)}`; }

async function openFile(path) {
  const r = await fetch(fileUrl(path));
  if (!r.ok) return;
  shown = path;
  stamp = r.headers.get("X-B2S-Version");
  $("#openpath").textContent = path;
  const editable = TEXT.test(path);
  $("#filetext").hidden = !editable;
  $("#fileview").hidden = editable;
  $("#savefile").disabled = !editable;
  if (editable) loaded = $("#filetext").value = await r.text();
  else $("#fileview").replaceChildren(IMAGE.test(path)
    ? Object.assign(new Image(), { src: fileUrl(path), alt: path })
    : Object.assign(document.createElement("a"),
        { href: fileUrl(path), target: "_blank", rel: "noopener", textContent: `open ${path} ↗` }));
}

async function put(path, body, version) {
  const url = fileUrl(path) + (version == null ? "" : `&version=${encodeURIComponent(version)}`);
  const said = await api(url, { method: "PUT",
                                headers: { "Content-Type": "application/octet-stream" }, body });
  await listFiles();
  return said;
}

async function saveFile() {
  if (!shown) return;
  const text = $("#filetext").value;
  try {
    // The stamp the file had when it was opened: the server refuses the save if a run has
    // rewritten it since, rather than letting this buffer put the older text back.
    const said = await put(shown, text, stamp ?? "");
    stamp = said.version;
    loaded = text;
    note(`${shown} saved`);
  } catch (e) { note(e.message, true); }
}

// A run may have rewritten the file that is open. Never discard what somebody has typed:
// an untouched buffer is reloaded, a touched one is kept and its owner told why the save
// that would revert the file is about to be refused.
async function afterRun() {
  if (!shown || !TEXT.test(shown)) return;
  const r = await fetch(fileUrl(shown));
  if (!r.ok) return;
  const now = r.headers.get("X-B2S-Version");
  if (now === stamp) return;
  const text = await r.text();
  if ($("#filetext").value === loaded) {          // nothing typed: show what the run wrote
    stamp = now;
    loaded = $("#filetext").value = text;
    note(`${shown} was rewritten by the run and reloaded`);
  } else {
    note(`the run rewrote ${shown}; your unsaved edits are still here, and saving them ` +
         `would put the older text back — copy them out and open the file again`, true);
  }
}

async function newFile() {
  const name = prompt("A new file, named as a path in the workspace:", "notes.tex");
  if (!name) return;
  try { await put(name, "", ""); await openFile(name); }   // "" = nothing of that name yet
  catch (e) { note(e.message, true); }
}

async function deleteFile() {
  if (!shown || !confirm(`Delete ${shown}?`)) return;
  await api(fileUrl(shown), { method: "DELETE" });
  shown = stamp = null;
  loaded = $("#filetext").value = "";
  $("#openpath").textContent = "";
  await listFiles();
}

async function upload(file) {
  try { await put(file.name, await file.arrayBuffer()); await openFile(file.name); }
  catch (e) { note(e.message, true); }
}

// ---------------------------------------------------------------- the tool's own form

function chosen() { return tools.find(t => t.name === $("#tool").value); }

function pickTool() {
  const t = chosen();
  $("#toolabout").textContent = t.description.split("\n\n")[0];
  const form = $("#toolform");
  const rows = Object.entries(t.input_schema.properties).map(([name, prop]) => {
    const kinds = Array.isArray(prop.type) ? prop.type : [prop.type];
    const required = (t.input_schema.required || []).includes(name);
    const row = document.createElement("label");
    row.className = "arg";
    row.append(Object.assign(document.createElement("b"),
                             { textContent: name + (required ? " *" : "") }));
    const input = document.createElement("input");
    if (kinds.includes("boolean")) {
      input.type = "checkbox";
      input.checked = prop.default === true;
    } else if (kinds.includes("array")) {
      input.placeholder = "separated by commas";
      input.value = (prop.default || []).join(", ");
    } else {
      input.type = kinds.includes("integer") || kinds.includes("number") ? "number" : "text";
      if (prop.default !== undefined && prop.default !== null) input.value = prop.default;
      input.placeholder = required ? "needed" : "left out";
    }
    input.dataset.arg = name;
    input.dataset.kinds = kinds.join(" ");
    row.append(input);
    if (PICKABLE[name] && canPick()) {
      const b = Object.assign(document.createElement("button"),
                              { type: "button", className: "pick", textContent: "Pick from Drive…" });
      b.onclick = () => pick(name, input);
      row.append(b);
    }
    row.append(span(prop.description));
    return row;
  });
  form.replaceChildren(...rows);
  if (!rows.length) form.textContent = "It takes no arguments.";
  const what = [];
  if (t.effects.google)
    what.push(t.effects.writes_google ? "it writes to your Drive" : "it uses your Google account");
  if (t.effects.writes_local) what.push("it writes files in the workspace");
  note(`${t.name}: ${what.join(", ") || "it only reads"}.`
    + (t.effects.google && config.google === "signin" ? " You will be asked to sign in." : ""));
}

function values() {
  const out = {};
  for (const input of $$("#toolform [data-arg]")) {
    const kinds = input.dataset.kinds.split(" ");
    if (kinds.includes("boolean")) { out[input.dataset.arg] = input.checked; continue; }
    const raw = input.value.trim();
    if (!raw) continue;                          // left out, so the tool's own default stands
    out[input.dataset.arg] = kinds.includes("array")
      ? raw.split(",").map(s => s.trim()).filter(Boolean)
      : kinds.includes("integer") || kinds.includes("number") ? Number(raw) : raw;
  }
  return out;
}

// ---------------------------------------------------------------- reaching a file it did not make

// `drive.file` is the whole of what this app asks for, and it reaches only the files the app
// itself created - which is why it needs no Google review, and why a deck somebody else built is
// invisible to `deck_adopt` and `deck_pull`. The Picker is Google's own answer: the visitor
// chooses the file in Google's window, and that choice grants this app `drive.file` on that one
// file. The app id is the project number, which is what a web client id starts with.
//
// It is a `signin` thing only: the Picker wants an OAuth token from the browser, and the browser
// has one exactly where visitors sign in. In `local` mode the token is the host's own, it carries
// `presentations` as well, and it already reaches whatever its owner can open - so there is
// nothing for a Picker to grant there, and the button would only be a promise the page cannot keep.
const PICKABLE = { deck: "presentation", doc: "document" };
let pickerLoaded = null;

function canPick() {
  return config.google === "signin" && config.google_api_key && config.google_client_id;
}

function pickerReady() {
  pickerLoaded = pickerLoaded || loadScript("https://apis.google.com/js/api.js")
    .then(() => new Promise(done => gapi.load("picker", done)));
  return pickerLoaded;
}

async function pick(name, input) {
  try {
    note("waiting for the Google window…");
    const [token] = await Promise.all([googleToken(false), pickerReady()]);
    const view = new google.picker.DocsView(PICKABLE[name] === "presentation"
      ? google.picker.ViewId.PRESENTATIONS : google.picker.ViewId.DOCUMENTS).setIncludeFolders(false);
    new google.picker.PickerBuilder()
      .setAppId(config.google_client_id.split("-")[0])
      .setOAuthToken(token)
      .setDeveloperKey(config.google_api_key)
      .addView(view)
      .setCallback(data => {
        if (data.action !== google.picker.Action.PICKED) return;
        input.value = data.docs[0].url || data.docs[0].id;
        note(`${data.docs[0].name} chosen; this app may now reach that one file.`);
      })
      .build().setVisible(true);
    note("choose a file in the Google window");
  } catch (e) {
    note(e.message, true);
  }
}

// ---------------------------------------------------------------- running one

async function runTool() {
  const t = chosen();
  $("#runtool").disabled = true;
  $("#runlog").textContent = "";
  $("#runresult").replaceChildren();
  try {
    const body = { tool: t.name, args: values() };
    // A token already granted goes with the journey that needs one, so the second click of a
    // session opens no window and costs no round trip. Only where the journey *needs* Google:
    // the server drops it for a local one, and the page keeps the same rule rather than
    // handing somebody's credentials to a compile.
    if (t.effects?.google) {
      const held = heldToken();
      if (held) body.access_token = held;
    }
    let made;
    try {
      made = await ask(body);
    } catch (e) {
      // The server asks for the visitor's own sign-in only where the journey needs Google.
      if (!/sign in with Google/.test(e.message)) throw e;
      note("waiting for the Google sign-in window…");
      body.access_token = await googleToken(false);
      made = await ask(body);
    }
    note(`${t.name} is running…`);
    await watch(made.id);
  } catch (e) {
    note(e.message, true);
  }
  $("#runtool").disabled = false;
}

function ask(body) {
  return api(`/api/ws/${ws}/runs`, { method: "POST",
                                     headers: { "Content-Type": "application/json" },
                                     body: JSON.stringify(body) });
}

async function watch(id) {
  let since = 0;
  for (;;) {
    const state = await api(`/api/ws/${ws}/runs/${id}?since=${since}`);
    if (state.log.length) {
      since = state.lines;
      const log = $("#runlog");
      log.textContent += state.log.join("\n") + "\n";
      log.scrollTop = log.scrollHeight;
    }
    if (state.state === "done") {
      // The journey is the one thing that finds out a held token is dead: Google refused it.
      if (state.result.code === "needs_consent") forgetToken();
      verdict(state.result, state.seconds);
      await listFiles();
      await afterRun();
      note(state.result.ok ? `${state.result.tool} finished in ${state.seconds} s`
           : state.result.code === "needs_consent"
             ? `${state.result.tool}: the Google sign-in has run out — run it again to sign in`
             : `${state.result.tool}: ${state.result.code}`, !state.result.ok);
      return;
    }
    await new Promise(r => setTimeout(r, 500));
  }
}

function verdict(result, seconds) {
  const box = $("#runresult");
  box.replaceChildren(
    Object.assign(document.createElement("div"),
      { className: "verdict " + (result.ok ? "ok" : "bad"),
        textContent: result.ok ? `${result.tool} · ${seconds} s` : `${result.tool} · ${result.code}` }),
    Object.assign(document.createElement("p"), { textContent: result.summary }));

  for (const d of result.diagnostics || [])
    box.append(Object.assign(document.createElement("p"),
      { className: "diag " + d.level,
        textContent: `${d.level}${d.where ? ` (${d.where})` : ""}: ${d.message}` }));

  for (const url of urls(result)) {
    const p = document.createElement("p");
    p.append(Object.assign(document.createElement("a"),
      { href: url, target: "_blank", rel: "noopener", textContent: `${url} ↗` }));
    box.append(p);
  }

  if (result.artifacts?.length) {
    const ul = document.createElement("ul");
    ul.className = "artifacts";
    for (const a of result.artifacts) {
      const li = document.createElement("li");
      const b = Object.assign(document.createElement("button"), { textContent: a.ref });
      b.onclick = () => openFile(a.ref);
      li.append(b, span(a.description || a.kind));
      ul.append(li);
    }
    box.append(head("Files it wrote"), ul);
  }
  if (result.next_steps?.length) {
    const ul = document.createElement("ul");
    for (const s of result.next_steps)
      ul.append(Object.assign(document.createElement("li"), { textContent: s }));
    box.append(head("What to consider next"), ul);
  }
  const data = { ...result.data };
  delete data.log;
  if (Object.keys(data).length) {
    const d = document.createElement("details");
    d.append(Object.assign(document.createElement("summary"), { textContent: "data" }),
             Object.assign(document.createElement("pre"),
                           { textContent: JSON.stringify(data, null, 1) }));
    box.append(d);
  }
}

function urls(result) {
  return Object.values(result.data || {})
    .filter(v => typeof v === "string" && /^https:\/\/(docs|drive)\.google\.com\//.test(v));
}

function head(text) { return Object.assign(document.createElement("h4"), { textContent: text }); }

function note(text, bad = false) {
  const n = $("#runnote");
  n.textContent = text;
  n.classList.toggle("bad", bad);
}

document.querySelector(".tab[data-tab=workbench]").addEventListener("click", enter);

})();
