"use strict";

// ---------- tiny DOM helpers ----------
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return n;
}
const esc = (s) => (s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------- markdown ----------
// Pulls $…$, $$…$$, \(…\), \[…\] out of `text` before markdown sees them,
// so things like $\bm h_\perp$ don't get mangled by italic/asterisk parsing.
// Returns { stripped, math } — math entries get rendered with KaTeX later.
function extractMath(text) {
  const math = [];
  // Inline-HTML placeholder survives paragraph / table-cell trimming that a
  // plain "MATH0" token wouldn't.
  const tok = (i) => `<span data-katex-i="${i}"></span>`;
  // Protect fenced code (```…```) and inline code (`…`) first.
  const codes = [];
  let s = text.replace(/```[\s\S]*?```/g, (m) => { codes.push(m); return `<!--CODE${codes.length - 1}-->`; });
  s = s.replace(/`[^`\n]*`/g, (m) => { codes.push(m); return `<!--CODE${codes.length - 1}-->`; });

  s = s.replace(/\$\$([\s\S]+?)\$\$/g, (_, expr) => { math.push({ expr, display: true }); return tok(math.length - 1); });
  s = s.replace(/\\\[([\s\S]+?)\\\]/g, (_, expr) => { math.push({ expr, display: true }); return tok(math.length - 1); });
  s = s.replace(/\\\(([\s\S]+?)\\\)/g, (_, expr) => { math.push({ expr, display: false }); return tok(math.length - 1); });
  // $…$ — require non-space immediately inside, and the closing $ not followed
  // by a digit, so "$5" / "$10" don't get eaten.
  s = s.replace(/(^|[^\\$])\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)/g, (_, pre, expr) => {
    math.push({ expr, display: false });
    return pre + tok(math.length - 1);
  });

  s = s.replace(/<!--CODE(\d+)-->/g, (_, i) => codes[Number(i)]);
  return { stripped: s, math };
}

function md(text) {
  if (!text) return "";
  try {
    if (window.marked && window.DOMPurify) {
      const { stripped, math } = extractMath(text);
      const raw = window.marked.parse(stripped, { breaks: true, gfm: true });
      let clean = window.DOMPurify.sanitize(raw, { ADD_ATTR: ["data-katex-i"] });
      if (math.length) {
        clean = clean.replace(/<span data-katex-i="(\d+)"><\/span>/g, (_, i) => {
          const m = math[Number(i)];
          if (!m) return "";
          if (window.katex) {
            try {
              return window.katex.renderToString(m.expr, {
                displayMode: m.display, throwOnError: false, output: "html",
              });
            } catch (e) { /* fall through */ }
          }
          const d = m.display ? "$$" : "$";
          return esc(d + m.expr + d);
        });
      }
      return clean;
    }
  } catch (e) { /* fall through */ }
  return `<p>${esc(text).replace(/\n/g, "<br>")}</p>`;
}

// ---------- time formatting ----------
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fmtDateOnly(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function relDays(ts) {
  const d = new Date(ts);
  if (isNaN(d)) return "";
  const days = Math.floor((Date.now() - d) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return days + "d ago";
  return fmtDateOnly(ts);
}
function shortModel(m) {
  if (!m) return "";
  return m.replace(/^claude-/, "").replace(/-\d{8}$/, "").replace(/\[1m\]$/, " (1M)");
}

// ---------- state ----------
let PROJECTS = [];
let CURRENT_FILE = null;

// ---------- sidebar ----------
async function loadSessions() {
  const res = await fetch("/api/sessions");
  const data = await res.json();
  PROJECTS = data.projects || [];
  const total = PROJECTS.reduce((a, p) => a + p.sessions.length, 0);
  $("#sidebar-stats").textContent = `${total} sessions across ${PROJECTS.length} projects`;
  renderSidebar("");
}

function renderSidebar(query) {
  const list = $("#session-list");
  list.innerHTML = "";
  const q = query.trim().toLowerCase();

  for (const proj of PROJECTS) {
    const matches = proj.sessions.filter((s) => {
      if (!q) return true;
      return (s.title + " " + s.cwd + " " + s.id).toLowerCase().includes(q);
    });
    if (!matches.length) continue;

    const group = el("div", { class: "project-group" });
    const header = el(
      "div",
      { class: "project-header", title: proj.path },
      el("span", { class: "chev" }, "▼"),
      el("span", { class: "project-path" }, shortPath(proj.path)),
      el("span", { class: "project-count" }, matches.length)
    );
    header.addEventListener("click", () => group.classList.toggle("collapsed"));
    group.append(header);

    for (const s of matches) {
      const item = el(
        "div",
        { class: "session-item", "data-file": s.file },
        el("div", { class: "session-title" }, s.title),
        el(
          "div",
          { class: "session-meta" },
          el("span", {}, relDays(s.last_ts || new Date(s.mtime * 1000).toISOString())),
          el("span", { class: "badge" }, `${s.n_user}💬`),
          el("span", { class: "badge" }, `${s.n_tool}🔧`),
          s.models && s.models[0] ? el("span", { class: "badge" }, shortModel(s.models[0])) : null
        ),
        el(
          "div",
          { class: "session-id", title: "Click to copy full id: " + s.id, onclick: (e) => copyId(e, s.id) },
          s.id
        )
      );
      item.addEventListener("click", () => openSession(s.file, item));
      group.append(item);
    }
    list.append(group);
  }
  if (!list.children.length) {
    list.append(el("div", { class: "empty-note" }, q ? "No matching sessions." : "No transcripts found."));
  }
}

function copyId(e, id) {
  e.stopPropagation(); // don't trigger opening the session
  const el = e.currentTarget;
  const restore = el.textContent;
  navigator.clipboard?.writeText(id).then(
    () => { el.textContent = "copied ✓"; setTimeout(() => (el.textContent = restore), 900); },
    () => {}
  );
}

function shortPath(p) {
  const parts = p.split("/").filter(Boolean);
  if (parts.length <= 2) return p;
  return ".../" + parts.slice(-2).join("/");
}

// ---------- session view ----------
async function openSession(file, itemEl) {
  CURRENT_FILE = file;
  location.hash = "file=" + encodeURIComponent(file);
  $$(".session-item.active").forEach((n) => n.classList.remove("active"));
  if (!itemEl) itemEl = $(`.session-item[data-file="${cssEscape(file)}"]`);
  if (itemEl) {
    itemEl.classList.add("active");
    itemEl.scrollIntoView({ block: "nearest" });
  }

  $("#welcome").hidden = true;
  const t = $("#transcript");
  t.hidden = false;
  t.innerHTML = `<div class="spinner">Loading transcript…</div>`;

  try {
    const res = await fetch("/api/session?file=" + encodeURIComponent(file));
    const data = await res.json();
    if (data.error) { t.innerHTML = `<div class="empty-note">Error: ${esc(data.error)}</div>`; return; }
    renderTranscript(data);
  } catch (e) {
    t.innerHTML = `<div class="empty-note">Failed to load: ${esc(String(e))}</div>`;
  }
}

function renderTranscript(data) {
  const t = $("#transcript");
  t.innerHTML = "";

  const meta = data.meta || {};
  const header = el(
    "div",
    { class: "t-header" },
    el("h1", { class: "t-title" }, data.title || "(untitled session)"),
    el(
      "div",
      { class: "t-meta" },
      meta.cwd ? el("span", {}, "📁 " + meta.cwd) : null,
      meta.git_branch ? el("span", {}, "⎇ " + meta.git_branch) : null,
      meta.version ? el("span", {}, "v" + meta.version) : null,
      el("span", {}, data.events.length + " events"),
      el("span", { class: "session-id", style: "margin:0", title: "Click to copy", onclick: (e) => copyId(e, data.id) }, "id: " + data.id)
    ),
    el(
      "div",
      { class: "t-controls" },
      el("button", { class: "btn", onclick: () => setAll(".thinking-block", true) }, "Collapse thinking"),
      el("button", { class: "btn", onclick: () => setAll(".thinking-block", false) }, "Expand thinking"),
      el("button", { class: "btn", onclick: () => setAll(".tool-block", true) }, "Collapse tools"),
      el("button", { class: "btn", onclick: () => setAll(".tool-block", false) }, "Expand tools")
    )
  );
  t.append(header);

  for (const ev of data.events) {
    const node = renderEvent(ev);
    if (node) t.append(node);
  }
  $("#main").scrollTop = 0;
}

function setAll(sel, collapsed) {
  $$(sel).forEach((n) => n.classList.toggle("collapsed", collapsed));
}

function renderEvent(ev) {
  switch (ev.kind) {
    case "user": return renderUser(ev);
    case "assistant": return renderAssistant(ev);
    case "system": return renderSystem(ev);
    case "attachment": return renderAttachment(ev);
    default: return null;
  }
}

function turnShell(kind, label, ev, bodyNodes) {
  const head = el(
    "div",
    { class: "turn-head" },
    el("span", {}, label),
    ev.is_sidechain ? el("span", { class: "sidechain-tag" }, "sub-agent") : null,
    ev.model ? el("span", { class: "muted", style: "font-weight:400" }, shortModel(ev.model)) : null,
    el("span", { class: "turn-time" }, fmtTime(ev.ts))
  );
  return el(
    "div",
    { class: `turn turn-${kind}` + (ev.is_sidechain ? " sidechain" : "") },
    head,
    el("div", { class: "turn-body" }, ...bodyNodes)
  );
}

function renderUser(ev) {
  const body = [];
  for (const b of ev.blocks) {
    if (b.type === "text") body.push(el("div", { class: "md", html: md(b.text) }));
    else if (b.type === "image" && b.data_uri) body.push(el("img", { src: b.data_uri, style: "max-width:100%;border-radius:8px" }));
  }
  return turnShell("user", "User", ev, body);
}

function renderAssistant(ev) {
  const body = [];
  for (const b of ev.blocks) {
    if (b.type === "text") body.push(el("div", { class: "md", html: md(b.text) }));
    else if (b.type === "thinking") body.push(renderThinking(b));
    else if (b.type === "tool_use") body.push(renderTool(b));
  }
  return turnShell("assistant", "Claude", ev, body);
}

function renderThinking(b) {
  const hasText = b.text && b.text.trim();
  const block = el("div", { class: "thinking-block collapsed" + (hasText ? "" : " empty") });
  const head = el(
    "div",
    { class: "thinking-head" },
    el("span", { class: "chev" }, "▼"),
    el("span", {}, hasText ? "💭 Thinking" : "💭 Thinking (not recorded)")
  );
  head.addEventListener("click", () => block.classList.toggle("collapsed"));
  const body = hasText
    ? el("div", { class: "thinking-body md", html: md(b.text) })
    : el(
        "div",
        { class: "thinking-body thinking-empty" },
        "Claude Code doesn't save thinking text to the transcript — only an encrypted signature is stored, so there's nothing to display here."
      );
  block.append(head, body);
  return block;
}

function renderSystem(ev) {
  return turnShell("system", "System · " + (ev.subtype || ""), ev, [
    el("div", { class: "md", html: md(ev.text || "") }),
  ]);
}

function renderAttachment(ev) {
  const body = [];
  const label = ["Hook", ev.hook_name, ev.att_type].filter(Boolean).join(" · ");
  if (ev.command) body.push(el("div", { class: "attach-meta" }, "$ " + ev.command));
  const out = [ev.content, ev.stdout, ev.stderr].filter(Boolean).join("\n");
  if (out) body.push(el("pre", { class: "payload truncatable" }, out));
  if (ev.exit_code != null) body.push(el("div", { class: "attach-meta" }, "exit " + ev.exit_code));
  if (!body.length) return null;
  return turnShell("attachment", label || "Attachment", ev, body);
}

// ---------- tool rendering ----------
function renderTool(b) {
  const name = b.name || "tool";
  const fmt = formatToolInput(name, b.input || {});
  const isErr = b.result && b.result.is_error;

  const block = el("div", { class: "tool-block collapsed" + (isErr ? " error" : "") });
  const head = el(
    "div",
    { class: "tool-head" },
    el("span", { class: "chev" }, "▼"),
    el("span", { class: "tool-icon" }, isErr ? "✗" : "🔧"),
    el("span", { class: "tool-name" }, name),
    el("span", { class: "tool-summary", title: fmt.summary }, fmt.summary),
    b.caller && b.caller !== "assistant" ? el("span", { class: "tool-caller" }, b.caller) : null
  );
  head.addEventListener("click", () => block.classList.toggle("collapsed"));

  const bodyKids = [];
  // input
  bodyKids.push(el("div", { class: "tool-section-label" }, "Input"));
  bodyKids.push(fmt.inputNode);
  // result
  if (b.result) {
    const imgs = b.result.images || [];
    const txt = b.result.text || (imgs.length ? "" : "(no output)");
    bodyKids.push(el("div", { class: "tool-section-label" }, isErr ? "Error" : "Result"));
    if (txt) bodyKids.push(el("pre", { class: "payload truncatable" + (isErr ? " result-error" : "") }, txt));
    for (const uri of imgs) {
      bodyKids.push(el("img", { src: uri, class: "tool-image", loading: "lazy" }));
    }
  } else {
    bodyKids.push(el("div", { class: "tool-section-label muted" }, "No result recorded"));
  }

  block.append(head, el("div", { class: "tool-body" }, ...bodyKids));
  return block;
}

function preFrom(text, cls = "payload truncatable") {
  return el("pre", { class: cls }, text);
}

// Build a readable diff view for Edit / Write.
function diffNode(oldStr, newStr) {
  const wrap = el("pre", { class: "payload" });
  const add = (line, cls) => {
    const span = el("span", { class: cls }, line + "\n");
    wrap.append(span);
  };
  if (oldStr != null) (oldStr.split("\n")).forEach((l) => add("- " + l, "diff-del"));
  if (newStr != null) (newStr.split("\n")).forEach((l) => add("+ " + l, "diff-add"));
  return wrap;
}

// Per-tool formatting: returns {summary, inputNode}
function formatToolInput(name, input) {
  const n = (name || "").toLowerCase();

  // Bash
  if (n === "bash") {
    const cmd = input.command || "";
    const node = el("div", {},
      input.description ? el("div", { class: "muted", style: "margin-bottom:4px" }, input.description) : null,
      preFrom(cmd, "payload"),
      input.run_in_background ? el("div", { class: "muted", style: "margin-top:4px" }, "(background)") : null
    );
    return { summary: firstLine(cmd), inputNode: node };
  }

  // Read
  if (n === "read") {
    const fp = input.file_path || "";
    const extra = [input.offset ? "offset " + input.offset : "", input.limit ? "limit " + input.limit : "", input.pages ? "pages " + input.pages : ""].filter(Boolean).join(", ");
    return { summary: fp + (extra ? "  (" + extra + ")" : ""), inputNode: el("div", { class: "attach-meta" }, fp + (extra ? "  " + extra : "")) };
  }

  // Edit
  if (n === "edit") {
    const fp = input.file_path || "";
    const node = el("div", {},
      el("div", { class: "attach-meta", style: "margin-bottom:6px" }, fp + (input.replace_all ? "  (replace all)" : "")),
      diffNode(input.old_string, input.new_string)
    );
    return { summary: fp, inputNode: node };
  }

  // Write
  if (n === "write") {
    const fp = input.file_path || "";
    const node = el("div", {},
      el("div", { class: "attach-meta", style: "margin-bottom:6px" }, fp),
      preFrom(input.content || "", "payload truncatable")
    );
    return { summary: fp, inputNode: node };
  }

  // MultiEdit
  if (n === "multiedit") {
    const fp = input.file_path || "";
    const edits = input.edits || [];
    const node = el("div", {}, el("div", { class: "attach-meta", style: "margin-bottom:6px" }, fp + `  (${edits.length} edits)`));
    edits.forEach((e, i) => {
      node.append(el("div", { class: "tool-section-label" }, "edit " + (i + 1)));
      node.append(diffNode(e.old_string, e.new_string));
    });
    return { summary: fp + `  (${edits.length} edits)`, inputNode: node };
  }

  // Grep / Glob
  if (n === "grep" || n === "glob") {
    const pat = input.pattern || input.query || "";
    const where = input.path || input.glob || "";
    const meta = [where ? "in " + where : "", input.output_mode ? input.output_mode : "", input["-i"] ? "case-insensitive" : ""].filter(Boolean).join(", ");
    return { summary: pat + (where ? "  in " + where : ""), inputNode: el("div", { class: "attach-meta" }, "pattern: " + pat + (meta ? "\n" + meta : "")) };
  }

  // Task / Agent
  if (n === "task" || n === "agent") {
    const desc = input.description || "";
    const sub = input.subagent_type || input.agentType || "";
    const node = el("div", {},
      el("div", { class: "muted", style: "margin-bottom:4px" }, (sub ? "[" + sub + "] " : "") + desc),
      preFrom(input.prompt || "", "payload truncatable")
    );
    return { summary: (sub ? sub + ": " : "") + desc, inputNode: node };
  }

  // TodoWrite
  if (n === "todowrite") {
    const todos = input.todos || [];
    const node = el("ul", { style: "margin:0;padding-left:18px" });
    const mark = { completed: "✅", in_progress: "🔄", pending: "⬜" };
    todos.forEach((td) => node.append(el("li", {}, (mark[td.status] || "•") + " " + (td.content || td.activeForm || ""))));
    return { summary: todos.length + " items", inputNode: node };
  }

  // WebFetch / WebSearch
  if (n === "webfetch") return { summary: input.url || "", inputNode: el("div", { class: "attach-meta" }, (input.url || "") + (input.prompt ? "\n\n" + input.prompt : "")) };
  if (n === "websearch") return { summary: input.query || "", inputNode: el("div", { class: "attach-meta" }, input.query || "") };

  // Fallback: pretty JSON
  const json = JSON.stringify(input, null, 2);
  return { summary: oneLineJson(input), inputNode: preFrom(json, "payload truncatable") };
}

function firstLine(s) { return (s || "").split("\n")[0].slice(0, 200); }
function oneLineJson(obj) {
  const keys = Object.keys(obj || {});
  if (!keys.length) return "";
  const k = keys[0];
  let v = obj[k];
  if (typeof v === "string") v = v.split("\n")[0];
  else v = JSON.stringify(v);
  return `${k}: ${String(v).slice(0, 120)}`;
}

// ---------- search wiring ----------
let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const v = e.target.value;
  searchTimer = setTimeout(() => renderSidebar(v), 120);
});

// refresh: re-scan the projects dir, keep the current search + open session
$("#refresh").addEventListener("click", async () => {
  const btn = $("#refresh");
  btn.textContent = "↻ …";
  await loadSessions();
  renderSidebar($("#search").value);
  if (CURRENT_FILE) {
    const item = $(`.session-item[data-file="${cssEscape(CURRENT_FILE)}"]`);
    if (item) item.classList.add("active");
  }
  btn.textContent = "↻ Refresh";
});

// keyboard: '/' to focus search
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $("#search")) {
    e.preventDefault();
    $("#search").focus();
  }
});

function cssEscape(s) {
  return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
}

function openFromHash() {
  const m = location.hash.match(/file=([^&]+)/);
  if (m) {
    try { openSession(decodeURIComponent(m[1])); } catch (e) { /* ignore */ }
  }
}

window.addEventListener("hashchange", () => {
  const m = location.hash.match(/file=([^&]+)/);
  const file = m ? decodeURIComponent(m[1]) : null;
  if (file && file !== CURRENT_FILE) openSession(file);
});

loadSessions().then(openFromHash);
