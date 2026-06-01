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
function md(text) {
  if (!text) return "";
  try {
    if (window.marked && window.DOMPurify) {
      const raw = window.marked.parse(text, { breaks: true, gfm: true });
      return window.DOMPurify.sanitize(raw);
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
  return String(m).replace(/^gpt-/, "gpt-").replace(/-codex$/, "");
}
function shortPath(p) {
  const parts = (p || "").split("/").filter(Boolean);
  if (parts.length <= 2) return p || "(unknown project)";
  return ".../" + parts.slice(-2).join("/");
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
      return (s.title + " " + s.cwd + " " + s.id + " " + s.source).toLowerCase().includes(q);
    });
    if (!matches.length) continue;

    const group = el("div", { class: "project-group" });
    const header = el(
      "div",
      { class: "project-header", title: proj.path },
      el("span", { class: "chev" }, "v"),
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
          el("span", { class: "badge" }, `${s.n_user} user`),
          el("span", { class: "badge" }, `${s.n_tool} tools`),
          s.n_web ? el("span", { class: "badge" }, `${s.n_web} web`) : null,
          s.model ? el("span", { class: "badge" }, shortModel(s.model)) : null,
          s.source ? el("span", { class: "badge" }, s.source) : null
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
  e.stopPropagation();
  const node = e.currentTarget;
  const restore = node.textContent;
  navigator.clipboard?.writeText(id).then(
    () => { node.textContent = "copied"; setTimeout(() => (node.textContent = restore), 900); },
    () => {}
  );
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
  t.innerHTML = `<div class="spinner">Loading transcript...</div>`;

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
      meta.cwd ? el("span", {}, "cwd: " + meta.cwd) : null,
      meta.originator ? el("span", {}, meta.originator) : null,
      meta.source ? el("span", {}, "source: " + meta.source) : null,
      meta.model ? el("span", {}, shortModel(meta.model)) : null,
      meta.reasoning_effort ? el("span", {}, "effort: " + meta.reasoning_effort) : null,
      meta.version ? el("span", {}, "v" + meta.version) : null,
      el("span", {}, data.events.length + " events"),
      el("span", { class: "session-id", style: "margin:0", title: "Click to copy", onclick: (e) => copyId(e, data.id) }, "id: " + data.id)
    ),
    el(
      "div",
      { class: "t-controls" },
      el("button", { class: "btn", onclick: () => setAll(".thinking-block", true) }, "Collapse reasoning"),
      el("button", { class: "btn", onclick: () => setAll(".thinking-block", false) }, "Expand reasoning"),
      el("button", { class: "btn", onclick: () => setAll(".tool-block", true) }, "Collapse tools"),
      el("button", { class: "btn", onclick: () => setAll(".tool-block", false) }, "Expand tools"),
      el("button", { class: "btn", onclick: () => setAll(".status-block", true) }, "Collapse status"),
      el("button", { class: "btn", onclick: () => setAll(".status-block", false) }, "Expand status"),
      el("button", { class: "btn", onclick: () => document.body.classList.toggle("show-tokens") }, "Toggle tokens")
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
    case "reasoning": return turnShell("reasoning", "Reasoning", ev, [renderReasoning(ev)]);
    case "tool": return turnShell("tool", "Tool", ev, [renderTool(ev)]);
    case "web_search": return turnShell("web_search", "Web search", ev, [renderWebSearch(ev)]);
    case "web_call": return turnShell("web_call", "Web search", ev, [renderWebSearch(ev)]);
    case "status": return renderStatus(ev);
    case "context": return renderContext(ev);
    case "tokens": return renderTokens(ev);
    case "raw": return turnShell("raw", "Raw - " + ev.record_type, ev, [preFrom(JSON.stringify(ev.payload, null, 2))]);
    default: return null;
  }
}

function turnShell(kind, label, ev, bodyNodes) {
  const head = el(
    "div",
    { class: "turn-head" },
    el("span", {}, label),
    ev.phase ? el("span", { class: "phase-tag" }, ev.phase) : null,
    ev.status ? el("span", { class: "status-tag" }, ev.status) : null,
    ev.model ? el("span", { class: "muted", style: "font-weight:400" }, shortModel(ev.model)) : null,
    el("span", { class: "turn-time" }, fmtTime(ev.ts))
  );
  return el(
    "div",
    { class: `turn turn-${kind}` },
    head,
    el("div", { class: "turn-body" }, ...bodyNodes)
  );
}

function renderUser(ev) {
  const body = [];
  if (ev.text) body.push(el("div", { class: "md", html: md(ev.text) }));
  for (const img of ev.images || []) {
    if (img.kind === "inline" && img.src) {
      body.push(el("img", { src: img.src, style: "max-width:100%;border-radius:8px" }));
    } else if (img.kind === "object" && img.value) {
      const src = imageObjectSrc(img.value);
      if (src) body.push(el("img", { src, style: "max-width:100%;border-radius:8px" }));
      else body.push(el("pre", { class: "payload truncatable" }, JSON.stringify(img.value, null, 2)));
    } else {
      body.push(el("div", { class: "attach-meta" }, `image omitted (${img.bytes || "unknown"} chars): ${img.reason || img.kind}`));
    }
  }
  for (const img of ev.local_images || []) {
    body.push(el("div", { class: "attach-meta" }, "local image: " + img));
  }
  return turnShell("user", "User", ev, body);
}

function imageObjectSrc(obj) {
  if (!obj || typeof obj !== "object") return "";
  if (obj.src) return obj.src;
  if (obj.url) return obj.url;
  if (obj.data) {
    const mime = obj.media_type || obj.mime_type || "image/png";
    return String(obj.data).startsWith("data:") ? obj.data : `data:${mime};base64,${obj.data}`;
  }
  if (obj.source && obj.source.data) {
    const mime = obj.source.media_type || "image/png";
    return `data:${mime};base64,${obj.source.data}`;
  }
  return "";
}

function renderAssistant(ev) {
  return turnShell("assistant", "Codex", ev, [
    el("div", { class: "md", html: md(ev.text || "") }),
  ]);
}

function renderReasoning(ev) {
  const hasText = ev.text && ev.text.trim();
  const block = el("div", { class: "thinking-block collapsed" + (hasText ? "" : " empty") });
  const head = el(
    "div",
    { class: "thinking-head" },
    el("span", { class: "chev" }, "v"),
    el("span", {}, hasText ? "Reasoning summary" : "Reasoning not readable")
  );
  head.addEventListener("click", () => block.classList.toggle("collapsed"));
  const body = hasText
    ? el("div", { class: "thinking-body md", html: md(ev.text) })
    : el(
        "div",
        { class: "thinking-body thinking-empty" },
        ev.has_encrypted
          ? "Codex saved encrypted reasoning content for continuation, not readable reasoning text."
          : "No reasoning text was recorded."
      );
  block.append(head, body);
  return block;
}

function renderStatus(ev) {
  const facts = [
    ev.turn_id ? "turn: " + ev.turn_id : "",
    ev.reason ? "reason: " + ev.reason : "",
    ev.duration_ms != null ? "duration: " + fmtDuration(ev.duration_ms) : "",
    ev.time_to_first_token_ms != null ? "first token: " + fmtDuration(ev.time_to_first_token_ms) : "",
    ev.context_window ? "context: " + ev.context_window.toLocaleString() : "",
    ev.collaboration_mode ? "mode: " + ev.collaboration_mode : "",
  ].filter(Boolean).join("\n");
  const block = collapsibleBlock("status-block collapsed", "Status details", [preFrom(facts || JSON.stringify(ev, null, 2))]);
  return turnShell("status", "Task", ev, [block]);
}

function renderContext(ev) {
  const payload = {
    turn_id: ev.turn_id,
    cwd: ev.cwd,
    model: ev.model,
    effort: ev.effort,
    approval_policy: ev.approval_policy,
    sandbox_policy: ev.sandbox_policy,
    summary: ev.summary,
  };
  const block = collapsibleBlock("status-block collapsed", "Turn context", [preFrom(JSON.stringify(payload, null, 2))]);
  return turnShell("context", "Context", ev, [block]);
}

function renderTokens(ev) {
  const usage = ev.usage || {};
  const body = el(
    "div",
    { class: "token-grid" },
    ...Object.entries(usage).map(([k, v]) => el("div", {}, el("strong", {}, k.replace(/_/g, " ")), el("br"), String(v)))
  );
  const extra = ev.context_window ? el("div", { class: "attach-meta", style: "margin-top:8px" }, "context window: " + ev.context_window.toLocaleString()) : null;
  const block = collapsibleBlock("status-block collapsed", "Token usage", [body, extra]);
  const node = turnShell("tokens", "Usage", ev, [block]);
  node.classList.add("token-event");
  return node;
}

function collapsibleBlock(cls, label, bodyNodes) {
  const block = el("div", { class: cls });
  const head = el(
    "div",
    { class: "tool-head" },
    el("span", { class: "chev" }, "v"),
    el("span", { class: "tool-name" }, label)
  );
  head.addEventListener("click", () => block.classList.toggle("collapsed"));
  block.append(head, el("div", { class: "tool-body" }, ...bodyNodes));
  return block;
}

// ---------- tool rendering ----------
function renderTool(ev) {
  const name = ev.name || "tool";
  const isErr = ev.result && ev.result.is_error;
  const block = el("div", { class: "tool-block collapsed" + (isErr ? " error" : "") });
  const head = el(
    "div",
    { class: "tool-head" },
    el("span", { class: "chev" }, "v"),
    el("span", { class: "tool-icon" }, isErr ? "x" : "tool"),
    el("span", { class: "tool-name" }, name),
    el("span", { class: "tool-summary", title: ev.summary || "" }, ev.summary || "")
  );
  head.addEventListener("click", () => block.classList.toggle("collapsed"));

  const bodyKids = [];
  bodyKids.push(el("div", { class: "tool-section-label" }, "Input"));
  bodyKids.push(formatToolInput(name, ev.input));
  if (ev.result) {
    bodyKids.push(el("div", { class: "tool-section-label" }, isErr ? "Error" : "Result"));
    if (ev.result.output) {
      bodyKids.push(preFrom(ev.result.output, "payload truncatable" + (isErr ? " result-error" : "")));
    }
    for (const img of ev.result.images || []) {
      bodyKids.push(renderImagePayload(img));
    }
    if (ev.result.raw) {
      bodyKids.push(preFrom(JSON.stringify(ev.result.raw, null, 2), "payload truncatable"));
    }
    if (!ev.result.output && !(ev.result.images || []).length && !ev.result.raw) {
      bodyKids.push(preFrom("(no output)", "payload truncatable"));
    }
  } else {
    bodyKids.push(el("div", { class: "tool-section-label muted" }, "No result recorded"));
  }

  block.append(head, el("div", { class: "tool-body" }, ...bodyKids));
  return block;
}

function renderImagePayload(img) {
  if (img.kind === "inline" && img.src) {
    return el("img", { src: img.src, class: "tool-image", loading: "lazy" });
  }
  if (img.kind === "object" && img.value) {
    const src = imageObjectSrc(img.value);
    if (src) return el("img", { src, class: "tool-image", loading: "lazy" });
    return preFrom(JSON.stringify(img.value, null, 2), "payload truncatable");
  }
  return el("div", { class: "attach-meta" }, `image omitted (${img.bytes || "unknown"} chars): ${img.reason || img.kind}`);
}

function renderWebSearch(ev) {
  const action = ev.action || {};
  const block = el("div", { class: "tool-block collapsed" });
  const head = el(
    "div",
    { class: "tool-head" },
    el("span", { class: "chev" }, "v"),
    el("span", { class: "tool-icon" }, "web"),
    el("span", { class: "tool-name" }, "web_search"),
    el("span", { class: "tool-summary", title: ev.query || "" }, ev.query || action.query || "")
  );
  head.addEventListener("click", () => block.classList.toggle("collapsed"));
  block.append(
    head,
    el("div", { class: "tool-body" },
      el("div", { class: "tool-section-label" }, "Query"),
      preFrom(ev.query || action.query || "", "payload"),
      el("div", { class: "tool-section-label" }, "Action"),
      preFrom(JSON.stringify(action, null, 2), "payload truncatable")
    )
  );
  return block;
}

function preFrom(text, cls = "payload truncatable") {
  return el("pre", { class: cls }, text == null ? "" : String(text));
}

function formatToolInput(name, input) {
  const n = (name || "").toLowerCase();
  const value = input || {};
  if (n === "exec_command" && typeof value === "object") {
    return el("div", {},
      value.workdir ? el("div", { class: "attach-meta", style: "margin-bottom:6px" }, "cwd: " + value.workdir) : null,
      preFrom(value.cmd || "", "payload"),
      value.yield_time_ms ? el("div", { class: "attach-meta", style: "margin-top:6px" }, "yield: " + value.yield_time_ms + "ms") : null
    );
  }
  if (n === "write_stdin" && typeof value === "object") {
    return el("div", {},
      el("div", { class: "attach-meta", style: "margin-bottom:6px" }, "session: " + (value.session_id || "")),
      preFrom(value.chars || "", "payload")
    );
  }
  if (n === "parallel" && Array.isArray(value.tool_uses)) {
    const wrap = el("div", {});
    value.tool_uses.forEach((use, i) => {
      wrap.append(el("div", { class: "tool-section-label" }, "call " + (i + 1) + " - " + (use.recipient_name || "")));
      wrap.append(preFrom(JSON.stringify(use.parameters || {}, null, 2), "payload truncatable"));
    });
    return wrap;
  }
  return preFrom(typeof value === "string" ? value : JSON.stringify(value, null, 2), "payload truncatable");
}

function fmtDuration(ms) {
  if (ms == null) return "";
  if (ms < 1000) return ms + "ms";
  return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + "s";
}

// ---------- search wiring ----------
let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const v = e.target.value;
  searchTimer = setTimeout(() => renderSidebar(v), 120);
});

$("#refresh").addEventListener("click", async () => {
  const btn = $("#refresh");
  btn.textContent = "Refresh...";
  await loadSessions();
  renderSidebar($("#search").value);
  if (CURRENT_FILE) {
    const item = $(`.session-item[data-file="${cssEscape(CURRENT_FILE)}"]`);
    if (item) item.classList.add("active");
  }
  btn.textContent = "Refresh";
});

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
