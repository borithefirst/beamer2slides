// The playground's page: runs a job, then draws what each stage decided from the job's deck.json.
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const KINDS = { text: "--k-text", image: "--k-image", shape: "--k-shape", table: "--k-table", diagram: "--k-diagram" };
const FONTS = { sans: "Lato, sans-serif", serif: "'PT Serif', serif", mono: "'Roboto Mono', monospace",
                typewriter: "'Roboto Mono', monospace", math: "Lato, 'Cambria Math', sans-serif" };

let config = null, job = null, deck = null, current = 0, mode = "rebuilt";

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = r.headers.get("Content-Type")?.startsWith("application/json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body.error || body || r.statusText);
  return body;
}

// ---------------------------------------------------------------- the source side

async function init() {
  config = await api("/api/config");
  const sel = $("#example");
  for (const ex of config.examples) sel.append(new Option(ex.name, ex.name));
  sel.onchange = loadExample;
  if (!config.examples.length) $("#tex").value = MINIMAL;
  else if (!location.search.includes("job=")) await loadExample();
  if (!config.engines.length) {
    $("#notex").hidden = false;
    $("#tex").readOnly = true;
    $("#go").textContent = "Convert the example's PDF";
  }
  if (!config.google) $("li[data-s=emit] b").textContent = "recorded";
  $("#go").onclick = () => config.engines.length ? runTex() : runExamplePdf();
  $("#pdf").onchange = e => e.target.files[0] && runPdf(e.target.files[0]);
  $("#toslides").onclick = toSlides;
  $$(".tab").forEach(b => b.onclick = () => {
    $$(".tab").forEach(t => t.classList.toggle("on", t === b));
    $$("main.panel").forEach(p => p.classList.toggle("on", p.id === b.dataset.tab));
  });
  $$(".modes button").forEach(b => b.onclick = () => { mode = b.dataset.mode; showSlide(current); });
  gallery();
  addEventListener("resize", () => deck && showSlide(current));
  const q = new URLSearchParams(location.search);
  current = +(q.get("slide") || 1) - 1;
  if (q.get("mode") && $(`.modes button[data-mode=${q.get("mode")}]`)) mode = q.get("mode");
  if (q.get("job")) {                          // a shared link: that talk, not the first example
    fetch(`/api/jobs/${q.get("job")}/source`).then(r => r.ok && r.text()).then(t => { if (t) $("#tex").value = t; });
    follow(q.get("job"));
  }
}

async function loadExample() {
  const ex = config.examples.find(e => e.name === $("#example").value);
  $("#about").textContent = ex ? ex.about : "";
  $("#go").disabled = !config.engines.length && !(ex && ex.pdf);
  if (ex) $("#tex").value = await api(`/api/example/${ex.name}.tex`);
}

async function runTex() { start(api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" },
                                                     body: JSON.stringify({ tex: $("#tex").value }) })); }
async function runPdf(file) { start(api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/pdf" }, body: file })); }
async function runExamplePdf() {
  const r = await fetch(`/api/example/${$("#example").value}.pdf`);
  if (r.ok) runPdf(await r.blob()); else setStatus("no compiled PDF for this example", true);
}

async function start(submitted) {
  $("#go").disabled = true;
  setStatus("submitting…");
  try { follow((await submitted).id); }
  catch (e) { setStatus(e.message, true); $("#go").disabled = false; }
}

function setStatus(text, bad = false) { const s = $("#status"); s.textContent = text; s.classList.toggle("bad", bad); }

const ORDER = ["queued", "compiling", "classifying", "rendering", "done"];
const STAGE_OF = { compiling: "compile", classifying: "extract + classify", rendering: "render" };

async function follow(id) {
  history.replaceState(null, "", `?job=${id}`);
  $$("#stages li").forEach(li => { li.className = ""; if (li.dataset.s !== "emit") $("b", li).textContent = ""; });
  for (;;) {
    let j;
    try { j = await api(`/api/jobs/${id}`); } catch (e) { setStatus(e.message, true); break; }
    $("#log").textContent = j.log;
    for (const li of $$("#stages li")) {
      const t = j.timings[li.dataset.s];
      if (t !== undefined) { li.className = "done"; $("b", li).textContent = `${t.toFixed(2)} s`; }
      else if (STAGE_OF[j.state] === li.dataset.s) li.className = "busy";
    }
    if (j.state === "done" || j.state === "error") {
      if (!("compile" in j.timings)) $("li[data-s=compile]").classList.add("skip");
      $("#go").disabled = false;
      if (j.state === "error") { setStatus(j.error, true); $("#logbox").open = true; }
      else { setStatus(""); await show(j); }
      break;
    }
    setStatus(j.state + "…");
    await new Promise(r => setTimeout(r, 600));
  }
}

// ---------------------------------------------------------------- the result side

async function show(j) {
  job = j;
  deck = await api(`/api/jobs/${j.id}/files/deck.json`);
  const s = j.result.stats, slides = j.result.slides;
  const counts = {};
  slides.forEach(sl => sl.elements.forEach(e => counts[e.kind] = (counts[e.kind] || 0) + 1));
  $("#summary").innerHTML = `<b>${slides.length}</b> slides from ${j.result.pages} pages · ` +
    `<b>${Math.round((s.native_share || 0) * 100)}%</b> of the characters become editable text · ` +
    Object.entries(counts).map(([k, n]) => `${n} ${k}${n > 1 ? "s" : ""}`).join(", ");
  const strip = $("#strip");
  strip.replaceChildren(...slides.map((sl, i) => {
    const b = document.createElement("button");
    b.innerHTML = `<img alt="slide ${i + 1}" src="${file(sl.files.page)}"><span>${i + 1}</span>`;
    b.onclick = () => showSlide(i);
    return b;
  }));
  $("#legend").innerHTML = Object.entries(KINDS).map(([k, v]) => `<span><i style="background:var(${v})"></i>${k}</span>`).join("");
  $("#toslides").hidden = !config.google;
  $("#slidesnote").innerHTML = j.slides_url ? link(j.slides_url) : config.google ? "" :
    "This server has no Google account: the <a href=\"#\" onclick=\"$('.tab[data-tab=gallery]').click();return false\">recorded runs</a> show the deck side.";
  $("#result").hidden = false;
  showSlide(Math.min(current, slides.length - 1));
}

const file = rel => `/api/jobs/${job.id}/files/${rel}`;
const link = url => `<a href="${url}" target="_blank" rel="noopener">Open the Google Slides deck ↗</a>`;

function showSlide(i) {
  current = i;
  history.replaceState(null, "", `?job=${job.id}&slide=${i + 1}&mode=${mode}`);
  $$("#strip button").forEach((b, k) => b.classList.toggle("on", k === i));
  $$(".modes button").forEach(b => b.classList.toggle("on", b.dataset.mode === mode));
  const sl = job.result.slides[i], ir = deck.slides[i];
  $("#rightcap").textContent = $(`.modes button[data-mode=${mode}]`).textContent +
    (sl.left.length ? ` · left in the background: ${sl.left.map(l => l.reason).join(", ")}` : "");
  const left = $("#left");
  left.replaceChildren(img(file(sl.files.page), "page"));
  const right = $("#right"), json = $("#json");
  json.hidden = mode !== "json";
  right.hidden = mode === "json";
  if (mode === "json") { json.textContent = JSON.stringify(ir, null, 1); return; }
  if (mode === "debug") { right.replaceChildren(img(file(sl.files.debug), "page")); return; }
  const [w, h] = ir.size, k = right.clientWidth / w;
  right.style.height = left.style.height = `${h * k}px`;
  const slide = div("slide", { width: `${w}px`, height: `${h}px`, transform: `scale(${k})` });
  if (mode === "native") {
    slide.append(img(file(sl.files.page), "bg", { width: `${w}px`, height: `${h}px` }), overlay(ir, sl));
  } else {
    slide.style.background = ir.background_color || "#fff";
    if (ir.background) slide.append(img(file(ir.background), "bg", { width: `${w}px`, height: `${h}px` }));
    if (mode === "rebuilt") rebuild(ir, slide);
  }
  right.replaceChildren(slide);
  if (mode === "rebuilt") { placeHoles(slide); document.fonts.ready.then(() => placeHoles(slide)); }
}

function img(src, cls, style = {}) { const i = new Image(); i.src = src; i.className = cls; Object.assign(i.style, style); i.alt = ""; return i; }
function div(cls, style = {}) { const d = document.createElement("div"); d.className = cls; Object.assign(d.style, style); return d; }
const box = ([x0, y0, x1, y1]) => ({ left: `${x0}px`, top: `${y0}px`, width: `${x1 - x0}px`, height: `${y1 - y0}px` });
const svgEl = (tag, attrs) => { const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [a, v] of Object.entries(attrs)) if (v !== undefined && v !== null) e.setAttribute(a, v); return e; };

// Boxes coloured by kind, the element's words in a tooltip.
function overlay(ir, sl) {
  const [w, h] = ir.size, svg = svgEl("svg", { width: w, height: h, class: "overlay" });
  const tip = $(".tip") || document.body.appendChild(div("tip"));
  tip.hidden = true;
  for (const e of sl.elements) {
    const [x0, y0, x1, y1] = e.bbox, color = getComputedStyle(document.documentElement).getPropertyValue(KINDS[e.kind] || "--muted");
    const r = svgEl("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, fill: color, stroke: color });
    r.onmousemove = ev => { tip.hidden = false; tip.textContent = `${e.kind}${e.role ? " · " + e.role : ""}: ${e.label || e.id}`;
                            tip.style.left = `${ev.clientX + 12}px`; tip.style.top = `${ev.clientY + 12}px`; };
    r.onmouseleave = () => tip.hidden = true;
    svg.append(r);
  }
  return svg;
}

// ---------------------------------------------------------------- the slide rebuilt from the IR

const ORDER_OF = { shape: 0, image: 1, table: 2, diagram: 2, text: 3 };

function rebuild(ir, slide) {
  const els = [...ir.elements].sort((a, b) => (ORDER_OF[a.kind] ?? 1) - (ORDER_OF[b.kind] ?? 1));
  for (const e of els) {
    if (e.kind === "shape") slide.append(shape(e));
    else if (e.kind === "image" && e.file) {
      const i = img(file(e.file), "el", box(e.bbox));
      if (e.anchor) Object.assign(i.dataset, { anchor: e.anchor, x0: e.bbox[0] });
      slide.append(i);
    }
    else if (e.kind === "text" && e.rotation) {
      // Laid out in its own frame (x along the words), then turned: that is how emit writes it too.
      const frame = div("el", { left: 0, top: 0, transformOrigin: "0 0", transform: `rotate(${e.rotation}deg)` });
      const x0 = Math.min(...e.paragraphs.flatMap(p => p.lines.map(l => l.x0)));
      const x1 = Math.max(...e.paragraphs.flatMap(p => p.lines.map(l => l.x1)));
      e.paragraphs.forEach(p => frame.append(...paragraph(p, { ...e, bbox: [x0, 0, x1, 0] })));
      slide.append(frame);
    }
    else if (e.kind === "text") e.paragraphs.forEach(p => slide.append(...paragraph(p, e).map(d => (d.dataset.el = e.id, d))));
    else if (e.kind === "table") slide.append(...table(e));
    else if (e.kind === "diagram") slide.append(...diagram(e));
  }
  // Footer texts shared by the slides go onto the layouts: drawn, but not the slide's to edit.
  for (const t of ir.theme_texts || [])
    t.paragraphs.forEach(p => slide.append(...paragraph(p, t).map(d => (d.contentEditable = "false", d))));
  slide.addEventListener("input", () => placeHoles(slide));
}

// A formula picture sits in the gap its text keeps for it (emit measures the gap on Google's renderer
// and moves the picture the same way), so it follows the words as they are set here, and as they are edited.
function placeHoles(slide) {
  for (const h of $$(".hole", slide)) {
    const para = h.closest(".para"), x0 = +h.dataset.x0;
    const pic = $$(`img[data-anchor="${para.dataset.el}"]`, slide).find(i => Math.abs(+i.dataset.x0 + 1 - x0) < 2.5);
    if (!pic) continue;
    const top = para.offsetTop + h.offsetTop;
    h.dataset.top0 ??= top;                                   // the line it was set on first
    pic.style.left = `${para.offsetLeft + h.offsetLeft - (x0 - +pic.dataset.x0)}px`;
    pic.style.transform = `translateY(${top - +h.dataset.top0}px)`;
  }
}

function shape(e) {
  const d = div("el", box(e.bbox));
  d.style.background = e.fill || "transparent";
  if (e.alpha !== undefined) d.style.opacity = e.alpha;
  d.style.borderRadius = e.shape === "ELLIPSE" ? "50%" : `${e.radius || 0}px`;
  return d;
}

const SCALE = { sans: 1.02 };   // Lato runs wider than CM Sans: emit sets it at size / 1.020 (docs/calibration.md)
const family = run => FONTS[run.family] || `'${run.family}', Lato, sans-serif`;

function runSpan(run) {
  if (run.hole) {
    const h = document.createElement("span");
    h.className = "hole"; h.style.width = `${run.hole}px`; h.dataset.x0 = run.hole_x0;
    return h;
  }
  const s = document.createElement(run.link && !run.link.startsWith("#") ? "a" : "span");
  if (s.tagName === "A") { s.href = run.link; s.target = "_blank"; }
  s.textContent = run.text.replace(//g, "\n");
  Object.assign(s.style, {
    fontFamily: family(run), fontSize: `${run.size / (SCALE[run.family] || 1)}px`, color: run.color || "#000",
    fontWeight: run.bold ? 700 : 400, fontStyle: run.italic ? "italic" : "normal",
    fontVariant: run.smallcaps ? "small-caps" : "normal",
    textDecoration: [run.underline && "underline", run.strike && "line-through"].filter(Boolean).join(" ") || "none",
    background: run.highlight || "transparent",
  });
  if (run.script) { s.style.verticalAlign = run.script === "super" ? "super" : "sub"; s.style.fontSize = `${run.size * 0.7}px`; }
  return s;
}

// A paragraph laid out on its PDF lines: the first baseline, the line pitch and the left edges.
function paragraph(p, e) {
  const size = p.size || p.runs[0]?.size || 11, lines = p.lines || [];
  const pitch = lines.length > 1 ? (lines[lines.length - 1].baseline - lines[0].baseline) / (lines.length - 1) : size * 1.2;
  const first = lines[0] || { baseline: e.bbox[1] + size, x0: e.bbox[0], x1: e.bbox[2] };
  const x0s = lines.map(l => l.x0), right = Math.max(e.bbox[2], p.wrap_limit || 0, ...lines.map(l => l.x1));
  let left = p.align === "left" || p.align === "justified" ? Math.min(...x0s, p.text_x0 ?? Infinity) : e.bbox[0];
  const d = div("para", {
    left: `${left}px`, top: `${first.baseline - pitch / 2 - 0.387 * size}px`, width: `${right - left + 3}px`,
    lineHeight: `${pitch}px`, textAlign: p.align === "justified" ? "left" : p.align,
    textIndent: p.align === "left" ? `${(p.text_x0 ?? first.x0) - left}px` : 0,
    whiteSpace: lines.length > 1 ? "pre-wrap" : "pre",   // one PDF line stays one line, as in the deck
  });
  d.contentEditable = "true";
  d.spellcheck = false;
  d.append(...p.runs.map(runSpan));
  const out = [d];
  const b = p.bullet;
  if (b) {
    const color = b.color || p.runs[0]?.color || "#000";
    if (b.bbox && (b.kind === "image" || b.kind === "shape")) {
      const m = div("el", box(b.bbox));
      m.style.background = color;
      m.style.borderRadius = /SQUARE|DIAMOND|ARROW|STAR|TRIANGLE/.test(b.glyph || b.shape || "") ? "0" : "50%";
      out.push(m);
    } else if (b.text) {
      const t = div("para", { left: `${(b.bbox ? b.bbox[0] : left - size)}px`, top: d.style.top, lineHeight: `${pitch}px` });
      t.append(runSpan({ ...(p.runs[0] || {}), text: b.text, color, hole: null, link: null, highlight: null }));
      out.push(t);
    }
  }
  return out;
}

function table(e) {
  const [fx0, fy0] = e.frame || e.bbox, xs = e.bounds, out = [];
  const tops = [fy0];
  e.row_heights.forEach(h => tops.push(tops[tops.length - 1] + h));
  const colX = c => [xs[c], xs[c + 1]];
  for (const f of e.fills || []) {
    const [x0, x1] = colX(f.col);
    const d = div("el", box([x0, tops[f.row], x1, tops[f.row + 1]])); d.style.background = f.color; out.push(d);
  }
  const svg = svgEl("svg", { width: 1, height: 1 });
  for (const r of e.rules || []) svg.append(svgEl("line", { x1: xs[0], x2: xs[xs.length - 1], y1: r.y, y2: r.y, stroke: r.color, "stroke-width": r.weight }));
  for (const b of e.borders || []) if (b.x !== undefined) svg.append(svgEl("line", { x1: b.x, x2: b.x, y1: b.y0, y2: b.y1, stroke: b.color || "#000", "stroke-width": b.weight || 0.4 }));
  out.push(svg);
  e.cells.forEach((row, r) => row.forEach((runs, c) => {
    if (!runs || !runs.length) return;
    const col = e.columns[c] || { x0: xs[c], x1: xs[c + 1], align: "left" }, size = runs[0].size || e.size;
    const d = div("para", { left: `${col.x0 - 2}px`, width: `${col.x1 - col.x0 + 4}px`, textAlign: col.align,
                            top: `${e.row_baselines[r] - size * 0.6 - 0.387 * size}px`, lineHeight: `${size * 1.2}px`, whiteSpace: "nowrap" });
    d.contentEditable = "true"; d.append(...runs.map(runSpan)); out.push(d);
  }));
  return out;
}

function diagram(e) {
  const svg = svgEl("svg", { width: 1, height: 1 }), out = [svg], defs = svgEl("defs", {});
  svg.append(defs);
  const marker = color => {
    const id = `tip-${color.slice(1)}`;
    if (!defs.querySelector(`#${id}`)) {
      const m = svgEl("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
      m.append(svgEl("path", { d: "M0,0 L10,5 L0,10 L3,5 z", fill: color })); defs.append(m);
    }
    return `url(#${id})`;
  };
  for (const n of e.nodes) {
    const [x0, y0, x1, y1] = n.bbox, a = { fill: n.fill || "none", stroke: n.stroke || "none", "stroke-width": n.width };
    svg.append(n.shape === "ELLIPSE" ? svgEl("ellipse", { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, rx: (x1 - x0) / 2, ry: (y1 - y0) / 2, ...a })
      : svgEl("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, rx: n.shape === "ROUND_RECTANGLE" ? Math.min(6, (y1 - y0) / 4) : 0, ...a }));
    (n.paragraphs || []).forEach((runs, k) => {
      const size = runs[0]?.size || 10, base = (n.baselines || [])[k] ?? (y0 + y1) / 2 + size * 0.35;
      const d = div("para", { left: `${x0}px`, width: `${x1 - x0}px`, textAlign: "center", top: `${base - size * 0.6 - 0.387 * size}px`, lineHeight: `${size * 1.2}px` });
      d.contentEditable = "true"; d.append(...runs.map(runSpan)); out.push(d);
    });
  }
  for (const l of e.lines) {
    const pts = l.points || [l.from, l.to];
    svg.append(svgEl("polyline", { points: pts.map(p => p.join(",")).join(" "), fill: "none", stroke: l.stroke || "#000", "stroke-width": l.width || 0.4,
      "marker-end": l.arrow_to ? marker(l.stroke || "#000") : null, "marker-start": l.arrow_from ? marker(l.stroke || "#000") : null }));
  }
  return out;
}

// ---------------------------------------------------------------- Google, and the recorded runs

async function toSlides() {
  const b = $("#toslides");
  b.disabled = true; $("#slidesnote").textContent = "building the deck (15-30 s)…";
  try { $("#slidesnote").innerHTML = link((await api(`/api/jobs/${job.id}/slides`, { method: "POST" })).url); }
  catch (e) { $("#slidesnote").textContent = e.message; }
  b.disabled = false;
}

function gallery() {
  $("#gallerylist").replaceChildren(...config.gallery.map(g => {
    const f = document.createElement("figure");
    f.innerHTML = `<figcaption></figcaption><img loading="lazy" alt="">`;
    f.firstChild.textContent = g.caption; f.lastChild.src = `/media/${g.file}`;
    return f;
  }));
  if (!config.gallery.length) $("#gallerylist").textContent = "No recorded runs on this server.";
}

const MINIMAL = String.raw`\documentclass{beamer}
\usetheme{Madrid}
\title{Hello}
\begin{document}
\begin{frame}{A first slide}
  \begin{itemize}
    \item Text becomes text
    \item Math like $e^{i\pi} + 1 = 0$ becomes runs
  \end{itemize}
\end{frame}
\end{document}
`;

init().catch(e => setStatus(e.message, true));
