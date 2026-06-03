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

// ---------- markdown (with KaTeX math) ----------
// Pulls $…$, $$…$$, \(…\), \[…\] out of `text` before markdown sees them,
// so things like $\bm h_\perp$ don't get mangled by italic/asterisk parsing.
function extractMath(text) {
  const math = [];
  const tok = (i) => `<span data-katex-i="${i}"></span>`;
  const codes = [];
  let s = text.replace(/```[\s\S]*?```/g, (m) => { codes.push(m); return `<!--CODE${codes.length - 1}-->`; });
  s = s.replace(/`[^`\n]*`/g, (m) => { codes.push(m); return `<!--CODE${codes.length - 1}-->`; });

  s = s.replace(/\$\$([\s\S]+?)\$\$/g, (_, expr) => { math.push({ expr, display: true }); return tok(math.length - 1); });
  s = s.replace(/\\\[([\s\S]+?)\\\]/g, (_, expr) => { math.push({ expr, display: true }); return tok(math.length - 1); });
  s = s.replace(/\\\(([\s\S]+?)\\\)/g, (_, expr) => { math.push({ expr, display: false }); return tok(math.length - 1); });
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
  return String(m)
    .replace(/^claude-/, "")
    .replace(/-\d{8}$/, "")
    .replace(/\[1m\]$/, " (1M)")
    .replace(/-codex$/, "");
}
function shortPath(p) {
  const parts = (p || "").split("/").filter(Boolean);
  if (parts.length <= 2) return p || "";
  return ".../" + parts.slice(-2).join("/");
}
function fmtDuration(ms) {
  if (ms == null) return "";
  if (ms < 1000) return ms + "ms";
  return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + "s";
}

// ---------- state ----------
let SESSIONS = [];
let CURRENT_FILE = null;
let AGENT_FILTER = "all";
// file -> snippet for the active content search, or null when no search is active
let CONTENT_MATCHES = null;
// Selected values for the dropdown filters; empty set = no constraint (all).
const SELECTED_MODELS = new Set();
const SELECTED_DIRS = new Set();

// ---------- sidebar ----------
async function loadSessions() {
  const res = await fetch("/api/sessions");
  const data = await res.json();
  SESSIONS = data.sessions || [];
  buildFilters();
  renderSidebar($("#search").value || "");
}

// ---------- dropdown filters (model / directory) ----------
function modelFamily(m) {
  const s = String(m || "").toLowerCase();
  if (s.startsWith("claude")) return "Claude";
  if (s.startsWith("gpt") || s.includes("codex") || s.startsWith("o1") || s.startsWith("o3")) return "GPT";
  return "Other";
}

// Generic multi-select dropdown.
//   groups: [{ label, items: [{value, label}] }]  (no group header if label is "")
//   selected: a Set the dropdown reads from and writes to
//   onChange: called after any change
function makeDropdown(title, groups, selected, onChange) {
  const wrap = el("div", { class: "dropdown" });
  const count = el("span", { class: "dropdown-count" });
  const btn = el("button", { class: "dropdown-btn" }, title, count, el("span", { class: "chev" }, "▾"));
  const panel = el("div", { class: "dropdown-panel hidden" });

  const updateCount = () => { count.textContent = selected.size ? ` (${selected.size})` : ""; };

  const itemBoxes = [];
  for (const g of groups) {
    const groupBoxes = [];
    let groupCb = null;
    if (g.label) {
      groupCb = el("input", { type: "checkbox" });
      const groupRow = el("label", { class: "dd-group" }, groupCb, g.label);
      groupCb.addEventListener("change", () => {
        for (const { cb, value } of groupBoxes) {
          cb.checked = groupCb.checked;
          if (groupCb.checked) selected.add(value); else selected.delete(value);
        }
        groupCb.indeterminate = false;
        updateCount(); onChange();
      });
      panel.append(groupRow);
    }
    const syncGroup = () => {
      if (!groupCb) return;
      const on = groupBoxes.filter((b) => b.cb.checked).length;
      groupCb.checked = on === groupBoxes.length && on > 0;
      groupCb.indeterminate = on > 0 && on < groupBoxes.length;
    };
    for (const it of g.items) {
      const cb = el("input", { type: "checkbox", value: it.value });
      cb.checked = selected.has(it.value);
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(it.value); else selected.delete(it.value);
        syncGroup(); updateCount(); onChange();
      });
      panel.append(el("label", { class: "dd-item" + (g.label ? " nested" : "") }, cb, it.label));
      groupBoxes.push({ cb, value: it.value });
      itemBoxes.push(cb);
    }
    syncGroup();
  }

  const clear = el("button", { class: "dd-clear" }, "Clear");
  clear.addEventListener("click", () => {
    selected.clear();
    itemBoxes.forEach((cb) => (cb.checked = false));
    $$(".dd-group input", panel).forEach((cb) => { cb.checked = false; cb.indeterminate = false; });
    updateCount(); onChange();
  });
  panel.append(clear);

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = !panel.classList.contains("hidden");
    $$(".dropdown-panel").forEach((p) => p.classList.add("hidden"));
    panel.classList.toggle("hidden", open);
  });
  panel.addEventListener("click", (e) => e.stopPropagation());

  updateCount();
  wrap.append(btn, panel);
  return wrap;
}

function buildFilters() {
  const host = $("#filter-dropdowns");
  host.innerHTML = "";

  // ----- models, grouped by family -----
  const families = { Claude: new Map(), GPT: new Map(), Other: new Map() };
  for (const s of SESSIONS) {
    if (!s.model) continue;
    families[modelFamily(s.model)].set(s.model, shortModel(s.model));
  }
  const modelGroups = [];
  for (const fam of ["Claude", "GPT", "Other"]) {
    const m = families[fam];
    if (!m.size) continue;
    const items = [...m.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([value, label]) => ({ value, label }));
    modelGroups.push({ label: fam, items });
  }
  // prune selections that no longer exist
  for (const v of [...SELECTED_MODELS]) if (!SESSIONS.some((s) => s.model === v)) SELECTED_MODELS.delete(v);
  if (modelGroups.length) {
    host.append(makeDropdown("Model", modelGroups, SELECTED_MODELS, () => renderSidebar($("#search").value)));
  }

  // ----- directories (flat) -----
  const dirs = [...new Set(SESSIONS.map((s) => s.cwd).filter(Boolean))].sort();
  for (const v of [...SELECTED_DIRS]) if (!dirs.includes(v)) SELECTED_DIRS.delete(v);
  if (dirs.length) {
    const items = dirs.map((d) => ({ value: d, label: shortPath(d) }));
    host.append(makeDropdown("Directory", [{ label: "", items }], SELECTED_DIRS, () => renderSidebar($("#search").value)));
  }
}

function renderSidebar(query) {
  const list = $("#session-list");
  list.innerHTML = "";
  const q = query.trim().toLowerCase();

  const matches = SESSIONS.filter((s) => {
    if (AGENT_FILTER !== "all" && s.agent !== AGENT_FILTER) return false;
    if (SELECTED_MODELS.size && !SELECTED_MODELS.has(s.model || "")) return false;
    if (SELECTED_DIRS.size && !SELECTED_DIRS.has(s.cwd || "")) return false;
    if (!q) return true;
    const metaHit = (s.title + " " + (s.cwd || "") + " " + s.id + " " + s.agent).toLowerCase().includes(q);
    const contentHit = CONTENT_MATCHES && CONTENT_MATCHES.has(s.file);
    return metaHit || contentHit;
  });

  // With an active query, order by relevance score (first-message hits weigh
  // most); otherwise keep the server's newest-first ordering.
  if (q) {
    const scoreOf = (s) => {
      const cm = CONTENT_MATCHES && CONTENT_MATCHES.get(s.file);
      return cm ? cm.score : 0.5; // metaHit-only (e.g. id match) sits below content hits
    };
    matches.sort((a, b) => (scoreOf(b) - scoreOf(a)) || ((b.mtime || 0) - (a.mtime || 0)));
  }

  const nClaude = matches.filter((s) => s.agent === "claude").length;
  const nCodex = matches.filter((s) => s.agent === "codex").length;
  $("#sidebar-stats").textContent = `${matches.length} sessions · ${nClaude} Claude · ${nCodex} Codex`;

  for (const s of matches) {
    const tsForRel = s.last_ts || (s.mtime ? new Date(s.mtime * 1000).toISOString() : null);
    const item = el(
      "div",
      { class: "session-item" + (s.is_subagent ? " subagent" : ""), "data-file": s.file },
      el(
        "div",
        { class: "session-toprow" },
        el("span", { class: "agent-tag agent-" + s.agent }, s.agent === "codex" ? "Codex" : "Claude"),
        s.is_subagent ? el("span", { class: "sidechain-tag" }, "sub-agent") : null,
        el("span", { class: "session-title" }, s.title)
      ),
      s.cwd ? el("div", { class: "session-cwd", title: s.cwd }, shortPath(s.cwd)) : null,
      el(
        "div",
        { class: "session-meta" },
        el("span", {}, relDays(tsForRel)),
        el("span", { class: "badge" }, `${s.n_user || 0}💬`),
        el("span", { class: "badge" }, `${s.n_tool || 0}🔧`),
        s.n_web ? el("span", { class: "badge" }, `${s.n_web}🌐`) : null,
        s.model ? el("span", { class: "badge" }, shortModel(s.model)) : null
      ),
      CONTENT_MATCHES && CONTENT_MATCHES.has(s.file)
        ? el("div", { class: "session-snippet" }, CONTENT_MATCHES.get(s.file).snippet)
        : null,
      el(
        "div",
        { class: "session-id", title: "Click to copy full id: " + s.id, onclick: (e) => copyId(e, s.id) },
        s.id
      )
    );
    item.addEventListener("click", () => openSession(s.file, item));
    list.append(item);
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
    () => { node.textContent = "copied ✓"; setTimeout(() => (node.textContent = restore), 900); },
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
  $("#outline").hidden = true;
  $("#nav-buttons").hidden = true;
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
  const isCodex = data.agent === "codex";

  const meta = data.meta || {};
  const header = el(
    "div",
    { class: "t-header" },
    el(
      "h1",
      { class: "t-title" },
      el("span", { class: "agent-tag agent-" + data.agent }, isCodex ? "Codex" : "Claude"),
      data.is_subagent ? el("span", { class: "sidechain-tag" }, "sub-agent") : null,
      " " + (data.title || "(untitled session)")
    ),
    el(
      "div",
      { class: "t-meta" },
      data.is_subagent && data.parent_file
        ? el(
            "a",
            {
              class: "parent-link",
              href: "#",
              onclick: (e) => { e.preventDefault(); openSession(data.parent_file); },
            },
            "↑ parent session"
          )
        : null,
      meta.cwd ? el("span", {}, "📁 " + meta.cwd) : null,
      meta.git_branch ? el("span", {}, "⎇ " + meta.git_branch) : null,
      meta.model ? el("span", {}, shortModel(meta.model)) : null,
      meta.reasoning_effort ? el("span", {}, "effort: " + meta.reasoning_effort) : null,
      meta.source ? el("span", {}, "source: " + meta.source) : null,
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
      el("button", { class: "btn", onclick: () => setAll(".tool-block", false) }, "Expand tools"),
      isCodex ? el("button", { class: "btn", onclick: () => setAll(".status-block", true) }, "Collapse status") : null,
      isCodex ? el("button", { class: "btn", onclick: () => setAll(".status-block", false) }, "Expand status") : null,
      isCodex ? el("button", { class: "btn", onclick: () => document.body.classList.toggle("show-tokens") }, "Toggle tokens") : null,
      el("button", { class: "btn", onclick: scrollToEnd }, "⤓ Jump to end")
    )
  );
  t.append(header);

  for (const ev of data.events) {
    const node = renderEvent(ev);
    if (node) t.append(node);
  }
  $("#main").scrollTop = 0;
  buildOutline();
}

// ---------- user-message navigation (outline + jump buttons) ----------
const OUTLINE_MAX = 80; // max chars shown per user message in the outline
let USER_TURNS = [];     // .turn-user elements in document order, for the current transcript

// Height of the sticky transcript header — scroll targets land just below it.
function headerOffset() {
  const h = $(".t-header");
  return h ? h.offsetHeight + 8 : 0;
}

function scrollToTurn(turn) {
  $("#main").scrollTo({ top: Math.max(0, turn.offsetTop - headerOffset()), behavior: "smooth" });
}

function scrollToEnd() {
  const main = $("#main");
  main.scrollTo({ top: main.scrollHeight, behavior: "smooth" });
}

// Index of the user turn currently at or above the viewport top.
function currentUserIndex() {
  const ref = $("#main").scrollTop + headerOffset() + 4;
  let idx = -1;
  USER_TURNS.forEach((turn, i) => { if (turn.offsetTop <= ref + 1) idx = i; });
  return idx;
}

// dir > 0 → next user message; dir < 0 → previous one. `anchor` is the content
// y-position at the top of the viewport — scrollToTurn() parks a turn exactly
// there, so the ±2 tolerance excludes the turn we're currently sitting on.
function jumpUser(dir) {
  if (!USER_TURNS.length) return;
  const main = $("#main");
  const anchor = main.scrollTop + headerOffset();
  if (dir > 0) {
    const next = USER_TURNS.find((t) => t.offsetTop > anchor + 2);
    if (next) scrollToTurn(next); else scrollToEnd();
  } else {
    let prev = null;
    for (const t of USER_TURNS) { if (t.offsetTop < anchor - 2) prev = t; else break; }
    if (prev) scrollToTurn(prev); else main.scrollTo({ top: 0, behavior: "smooth" });
  }
}

function buildOutline() {
  const t = $("#transcript");
  // top-level user prompts only — skip sub-agent (sidechain) prompts
  USER_TURNS = $$(".turn-user:not(.sidechain)", t);
  const outline = $("#outline");
  const list = $("#outline-list");
  const nav = $("#nav-buttons");
  list.innerHTML = "";

  if (!USER_TURNS.length) {
    outline.hidden = true;
    nav.hidden = true;
    return;
  }
  outline.hidden = false;
  nav.hidden = false;

  USER_TURNS.forEach((turn, i) => {
    turn.id = "user-turn-" + i;
    const full = (($(".turn-body", turn) || {}).textContent || "").trim().replace(/\s+/g, " ");
    const label = full.slice(0, OUTLINE_MAX) || "(empty message)";
    const item = el(
      "div",
      { class: "outline-item", title: full.slice(0, 500) },
      el("span", { class: "outline-num" }, String(i + 1)),
      el("span", { class: "outline-text" }, label)
    );
    item.addEventListener("click", () => scrollToTurn(turn));
    list.append(item);
  });
  highlightOutline();
}

function highlightOutline() {
  const idx = currentUserIndex();
  const items = $$("#outline-list .outline-item");
  items.forEach((it, i) => it.classList.toggle("active", i === idx));
  if (idx >= 0 && items[idx]) items[idx].scrollIntoView({ block: "nearest" });
}

function setAll(sel, collapsed) {
  $$(sel).forEach((n) => n.classList.toggle("collapsed", collapsed));
}

// ---------- event dispatch (handles both Claude Code and Codex shapes) ----------
function renderEvent(ev) {
  switch (ev.kind) {
    case "user": return renderUser(ev);
    case "assistant": return renderAssistant(ev);
    case "system": return renderSystem(ev);
    case "attachment": return renderAttachment(ev);
    case "reasoning": return turnShell("reasoning", "Reasoning", ev, [renderReasoning(ev)]);
    case "tool": return turnShell("tool", "Tool · " + (ev.name || "tool"), ev, [renderCodexTool(ev)]);
    case "web_search":
    case "web_call": return turnShell("web_call", "Web search", ev, [renderWebSearch(ev)]);
    case "status": return renderStatus(ev);
    case "context": return renderContext(ev);
    case "tokens": return renderTokens(ev);
    case "raw": return turnShell("raw", "Raw · " + ev.record_type, ev, [preFrom(JSON.stringify(ev.payload, null, 2))]);
    default: return null;
  }
}

function turnShell(kind, label, ev, bodyNodes) {
  const head = el(
    "div",
    { class: "turn-head" },
    el("span", {}, label),
    ev.is_sidechain ? el("span", { class: "sidechain-tag" }, "sub-agent") : null,
    ev.phase ? el("span", { class: "phase-tag" }, ev.phase) : null,
    ev.status ? el("span", { class: "status-tag" }, ev.status) : null,
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
  if (ev.blocks) {
    // Claude Code shape
    for (const b of ev.blocks) {
      if (b.type === "text") body.push(el("div", { class: "md", html: md(b.text) }));
      else if (b.type === "image" && b.data_uri) body.push(el("img", { src: b.data_uri, style: "max-width:100%;border-radius:8px" }));
    }
  } else {
    // Codex shape
    if (ev.text) body.push(el("div", { class: "md", html: md(ev.text) }));
    for (const img of ev.images || []) body.push(renderImagePayload(img));
    for (const img of ev.local_images || []) body.push(el("div", { class: "attach-meta" }, "local image: " + img));
  }
  return turnShell("user", ev.queued ? "User · queued" : "User", ev, body);
}

function renderAssistant(ev) {
  if (ev.blocks) {
    // Claude Code shape
    const body = [];
    for (const b of ev.blocks) {
      if (b.type === "text") body.push(el("div", { class: "md", html: md(b.text) }));
      else if (b.type === "thinking") body.push(renderThinking(b));
      else if (b.type === "tool_use") body.push(renderTool(b));
    }
    return turnShell("assistant", "Claude", ev, body);
  }
  // Codex shape (flat text; reasoning/tools are separate events)
  return turnShell("assistant", "Codex", ev, [el("div", { class: "md", html: md(ev.text || "") })]);
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

function renderReasoning(ev) {
  const hasText = ev.text && ev.text.trim();
  const block = el("div", { class: "thinking-block collapsed" + (hasText ? "" : " empty") });
  const head = el(
    "div",
    { class: "thinking-head" },
    el("span", { class: "chev" }, "▼"),
    el("span", {}, hasText ? "💭 Reasoning summary" : "💭 Reasoning not readable")
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

function renderSystem(ev) {
  return turnShell("system", "System · " + (ev.subtype || ""), ev, [
    el("div", { class: "md", html: md(ev.text || "") }),
  ]);
}

function renderAttachment(ev) {
  const body = [];
  const t = ev.att_type;
  const name = ev.display_path || ev.filename || "";

  if (t === "compact_file_reference") {
    // post-/compact pointer: file was referenced but its bytes were dropped.
    // Show the same note the model is actually handed when its context is rebuilt.
    body.push(el("div", { class: "attach-meta" }, "📎 Referenced file " + name));
    body.push(el("div", { class: "attach-note" },
      "Note: " + (ev.filename || name) + " was read before the last conversation was " +
      "summarized, but the contents are too large to include. Use Read tool if you need " +
      "to access it."));
  } else if (t === "file") {
    // post-/compact re-attachment: the full file text was injected back in
    const lines = ev.num_lines != null ? ` (${ev.num_lines} lines)` : "";
    body.push(el("div", { class: "attach-meta" }, "📄 Read " + name + lines));
    if (ev.content) body.push(el("pre", { class: "payload truncatable" }, ev.content));
  } else if (t === "deferred_tools_delta") {
    const parts = [];
    if (ev.added_count) parts.push("+" + ev.added_count + " tools available via ToolSearch");
    if (ev.removed_count) parts.push("−" + ev.removed_count + " removed");
    if (ev.readded_count) parts.push(ev.readded_count + " re-added");
    body.push(el("div", { class: "attach-meta" }, parts.join(" · ") || "tool set updated"));
  } else {
    // hook output and everything else
    if (ev.command) body.push(el("div", { class: "attach-meta" }, "$ " + ev.command));
    const out = [ev.content, ev.stdout, ev.stderr].filter(Boolean).join("\n");
    if (out) body.push(el("pre", { class: "payload truncatable" }, out));
    if (ev.exit_code != null) body.push(el("div", { class: "attach-meta" }, "exit " + ev.exit_code));
  }

  const label = ev.hook_name
    ? ["Hook", ev.hook_name, t].filter(Boolean).join(" · ")
    : (t ? "Attachment · " + t : "Attachment");
  if (!body.length) return null;
  return turnShell("attachment", label, ev, body);
}

// ---------- Codex status / context / tokens ----------
function collapsibleBlock(cls, label, bodyNodes) {
  const block = el("div", { class: cls });
  const head = el(
    "div",
    { class: "tool-head" },
    el("span", { class: "chev" }, "▼"),
    el("span", { class: "tool-name" }, label)
  );
  head.addEventListener("click", () => block.classList.toggle("collapsed"));
  block.append(head, el("div", { class: "tool-body" }, ...bodyNodes));
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

// ---------- images (Codex) ----------
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

function renderImagePayload(img) {
  if (img.kind === "local" && img.src) {
    return el("figure", { class: "tool-image-frame" },
      el("img", { src: img.src, class: "tool-image", loading: "lazy" }),
      img.path ? el("figcaption", { class: "attach-meta" }, img.path) : null
    );
  }
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
  // Codex fires one search call but often with several sub-queries; collect them all.
  const queries = (Array.isArray(action.queries) && action.queries.length)
    ? action.queries
    : [ev.query || action.query].filter(Boolean);
  const block = el("div", { class: "tool-block collapsed" });
  const head = el(
    "div",
    { class: "tool-head" },
    el("span", { class: "chev" }, "▼"),
    el("span", { class: "tool-icon" }, "🌐"),
    el("span", { class: "tool-name" }, "web_search"),
    queries.length > 1 ? el("span", { class: "status-tag" }, queries.length + " queries") : null,
    el("span", { class: "tool-summary", title: queries.join("  •  ") }, queries[0] || "(no query recorded)")
  );
  head.addEventListener("click", () => block.classList.toggle("collapsed"));

  const ul = el("ul", { class: "query-list" });
  for (const q of queries) ul.append(el("li", {}, q));

  block.append(
    head,
    el("div", { class: "tool-body" },
      el("div", { class: "tool-section-label" }, queries.length > 1 ? "Queries" : "Query"),
      queries.length ? ul : preFrom("(no query recorded)", "payload"),
      el("div", { class: "attach-meta", style: "margin-top:8px;font-style:italic" },
        "Codex records only the search queries, not the results returned.")
    )
  );
  return block;
}

// ---------- tool rendering ----------
// Claude Code tool (input formatted client-side; result attached on the block).
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
  bodyKids.push(el("div", { class: "tool-section-label" }, "Input"));
  bodyKids.push(fmt.inputNode);
  if (b.result) {
    const imgs = b.result.images || [];
    const txt = b.result.text || (imgs.length ? "" : "(no output)");
    bodyKids.push(el("div", { class: "tool-section-label" }, isErr ? "Error" : "Result"));
    if (txt) bodyKids.push(el("pre", { class: "payload truncatable" + (isErr ? " result-error" : "") }, txt));
    for (const uri of imgs) bodyKids.push(el("img", { src: uri, class: "tool-image", loading: "lazy" }));
  } else {
    bodyKids.push(el("div", { class: "tool-section-label muted" }, "No result recorded"));
  }

  block.append(head, el("div", { class: "tool-body" }, ...bodyKids));
  return block;
}

// Codex tool (top-level event; summary precomputed server-side, result is a dict).
function renderCodexTool(ev) {
  const name = ev.name || "tool";
  const isErr = ev.result && ev.result.is_error;
  const block = el("div", { class: "tool-block collapsed" + (isErr ? " error" : "") });
  const head = el(
    "div",
    { class: "tool-head" },
    el("span", { class: "chev" }, "▼"),
    el("span", { class: "tool-icon" }, isErr ? "✗" : "🔧"),
    el("span", { class: "tool-name" }, name),
    ev.status ? el("span", { class: "status-tag" }, ev.status) : null,
    el("span", { class: "tool-summary", title: ev.summary || "" }, ev.summary || "")
  );
  head.addEventListener("click", () => block.classList.toggle("collapsed"));

  const bodyKids = [];
  bodyKids.push(el("div", { class: "tool-section-label" }, "Input"));
  bodyKids.push(formatToolInput(name, ev.input).inputNode);
  if (ev.result) {
    bodyKids.push(el("div", { class: "tool-section-label" }, isErr ? "Error" : "Result"));
    if (ev.result.output) {
      bodyKids.push(preFrom(ev.result.output, "payload truncatable" + (isErr ? " result-error" : "")));
    }
    for (const img of ev.result.images || []) bodyKids.push(renderImagePayload(img));
    if (ev.result.raw) {
      if (ev.result.raw.changes) bodyKids.push(renderChanges(ev.result.raw.changes));
      else bodyKids.push(preFrom(JSON.stringify(ev.result.raw, null, 2), "payload truncatable"));
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

function preFrom(text, cls = "payload truncatable") {
  return el("pre", { class: cls }, text == null ? "" : String(text));
}

// Build a readable diff view for Edit / Write (old/new strings).
function diffNode(oldStr, newStr) {
  const wrap = el("pre", { class: "payload" });
  const add = (line, cls) => wrap.append(el("span", { class: cls }, line + "\n"));
  if (oldStr != null) (oldStr.split("\n")).forEach((l) => add("- " + l, "diff-del"));
  if (newStr != null) (newStr.split("\n")).forEach((l) => add("+ " + l, "diff-add"));
  return wrap;
}

// Render a Codex apply_patch body (a raw unified-ish patch string) as a diff.
function diffLine(line) {
  let cls = "diff-ctx";
  if (line.startsWith("*** ")) cls = "diff-file";
  else if (line.startsWith("@@")) cls = "diff-hunk";
  else if (line.startsWith("+")) cls = "diff-add";
  else if (line.startsWith("-")) cls = "diff-del";
  return el("span", { class: "diff-line " + cls }, line === "" ? "​" : line);
}

function renderPatch(text) {
  const block = el("pre", { class: "payload diff" });
  for (const line of String(text || "").split("\n")) block.append(diffLine(line));
  return block;
}

function renderChanges(changes) {
  const wrap = el("div", {});
  for (const [path, info] of Object.entries(changes || {})) {
    const verb = info && info.type ? info.type : "change";
    wrap.append(el("div", { class: "tool-section-label" }, verb + ": " + path));
    const diffText = info && (info.unified_diff || info.content);
    if (diffText) wrap.append(renderPatch(diffText));
    else wrap.append(preFrom(JSON.stringify(info, null, 2), "payload truncatable"));
  }
  return wrap;
}

// Per-tool formatting for both Claude Code and Codex. Returns {summary, inputNode}.
function formatToolInput(name, input) {
  const n = (name || "").toLowerCase();
  const value = input == null ? {} : input;

  // ----- Codex tools -----
  if (n === "apply_patch") {
    const text = typeof value === "string" ? value : (value.raw || value.input || value.patch || JSON.stringify(value, null, 2));
    return { summary: "patch", inputNode: renderPatch(text) };
  }
  if ((n === "exec_command" || n === "shell") && typeof value === "object") {
    const node = el("div", {},
      value.workdir ? el("div", { class: "attach-meta", style: "margin-bottom:6px" }, "cwd: " + value.workdir) : null,
      preFrom(value.cmd || (Array.isArray(value.command) ? value.command.join(" ") : value.command) || "", "payload"),
      value.yield_time_ms ? el("div", { class: "attach-meta", style: "margin-top:6px" }, "yield: " + value.yield_time_ms + "ms") : null
    );
    return { summary: firstLine(value.cmd || ""), inputNode: node };
  }
  if (n === "write_stdin" && typeof value === "object") {
    const chars = value.chars || "";
    const meta = ["session: " + (value.session_id ?? "")];
    if (value.yield_time_ms) meta.push("wait: " + value.yield_time_ms + "ms");
    if (value.max_output_tokens) meta.push("max tokens: " + value.max_output_tokens);
    const node = el("div", {},
      el("div", { class: "attach-meta", style: "margin-bottom:6px" }, meta.join("  ·  ")),
      chars
        ? preFrom(chars, "payload")
        : el("div", { class: "attach-meta", style: "font-style:italic" }, "(no input — polling process for more output)")
    );
    return { summary: "session " + (value.session_id ?? ""), inputNode: node };
  }
  if (n === "parallel" && Array.isArray(value.tool_uses)) {
    const wrap = el("div", {});
    value.tool_uses.forEach((use, i) => {
      wrap.append(el("div", { class: "tool-section-label" }, "call " + (i + 1) + " · " + (use.recipient_name || "")));
      wrap.append(preFrom(JSON.stringify(use.parameters || {}, null, 2), "payload truncatable"));
    });
    return { summary: value.tool_uses.length + " tool calls", inputNode: wrap };
  }

  // ----- Claude Code tools -----
  if (n === "bash") {
    const cmd = value.command || "";
    const node = el("div", {},
      value.description ? el("div", { class: "muted", style: "margin-bottom:4px" }, value.description) : null,
      preFrom(cmd, "payload"),
      value.run_in_background ? el("div", { class: "muted", style: "margin-top:4px" }, "(background)") : null
    );
    return { summary: firstLine(cmd), inputNode: node };
  }
  if (n === "read") {
    const fp = value.file_path || "";
    const extra = [value.offset ? "offset " + value.offset : "", value.limit ? "limit " + value.limit : "", value.pages ? "pages " + value.pages : ""].filter(Boolean).join(", ");
    return { summary: fp + (extra ? "  (" + extra + ")" : ""), inputNode: el("div", { class: "attach-meta" }, fp + (extra ? "  " + extra : "")) };
  }
  if (n === "edit") {
    const fp = value.file_path || "";
    const node = el("div", {},
      el("div", { class: "attach-meta", style: "margin-bottom:6px" }, fp + (value.replace_all ? "  (replace all)" : "")),
      diffNode(value.old_string, value.new_string)
    );
    return { summary: fp, inputNode: node };
  }
  if (n === "write") {
    const fp = value.file_path || "";
    const node = el("div", {},
      el("div", { class: "attach-meta", style: "margin-bottom:6px" }, fp),
      preFrom(value.content || "", "payload truncatable")
    );
    return { summary: fp, inputNode: node };
  }
  if (n === "multiedit") {
    const fp = value.file_path || "";
    const edits = value.edits || [];
    const node = el("div", {}, el("div", { class: "attach-meta", style: "margin-bottom:6px" }, fp + `  (${edits.length} edits)`));
    edits.forEach((e, i) => {
      node.append(el("div", { class: "tool-section-label" }, "edit " + (i + 1)));
      node.append(diffNode(e.old_string, e.new_string));
    });
    return { summary: fp + `  (${edits.length} edits)`, inputNode: node };
  }
  if (n === "grep" || n === "glob") {
    const pat = value.pattern || value.query || "";
    const where = value.path || value.glob || "";
    const meta = [where ? "in " + where : "", value.output_mode ? value.output_mode : "", value["-i"] ? "case-insensitive" : ""].filter(Boolean).join(", ");
    return { summary: pat + (where ? "  in " + where : ""), inputNode: el("div", { class: "attach-meta" }, "pattern: " + pat + (meta ? "\n" + meta : "")) };
  }
  if (n === "task" || n === "agent") {
    const desc = value.description || "";
    const sub = value.subagent_type || value.agentType || "";
    const node = el("div", {},
      el("div", { class: "muted", style: "margin-bottom:4px" }, (sub ? "[" + sub + "] " : "") + desc),
      preFrom(value.prompt || "", "payload truncatable")
    );
    return { summary: (sub ? sub + ": " : "") + desc, inputNode: node };
  }
  if (n === "todowrite") {
    const todos = value.todos || [];
    const node = el("ul", { style: "margin:0;padding-left:18px" });
    const mark = { completed: "✅", in_progress: "🔄", pending: "⬜" };
    todos.forEach((td) => node.append(el("li", {}, (mark[td.status] || "•") + " " + (td.content || td.activeForm || ""))));
    return { summary: todos.length + " items", inputNode: node };
  }
  if (n === "webfetch") return { summary: value.url || "", inputNode: el("div", { class: "attach-meta" }, (value.url || "") + (value.prompt ? "\n\n" + value.prompt : "")) };
  if (n === "websearch") return { summary: value.query || "", inputNode: el("div", { class: "attach-meta" }, value.query || "") };

  // Fallback: pretty JSON (or raw string)
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return { summary: typeof value === "string" ? firstLine(value) : oneLineJson(value), inputNode: preFrom(text, "payload truncatable") };
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

// ---------- search / filter wiring ----------
// Fetch content matches from the server, then re-render. Title/cwd/id matching
// still happens instantly client-side in renderSidebar; this adds full-text.
let searchSeq = 0;
async function runSearch(query) {
  const q = (query || "").trim();
  if (!q) {
    CONTENT_MATCHES = null;
    renderSidebar(query);
    return;
  }
  const seq = ++searchSeq;
  renderSidebar(query); // instant feedback on title/cwd while content search is in flight
  try {
    const res = await fetch("/api/search?q=" + encodeURIComponent(q));
    const data = await res.json();
    if (seq !== searchSeq) return; // a newer search superseded this one
    CONTENT_MATCHES = new Map((data.matches || []).map((m) => [m.file, { snippet: m.snippet, score: m.score }]));
  } catch (e) {
    if (seq !== searchSeq) return;
    CONTENT_MATCHES = new Map();
  }
  renderSidebar(query);
}

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const v = e.target.value;
  searchTimer = setTimeout(() => runSearch(v), 180);
});

$$("#filter-row .filter-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    AGENT_FILTER = chip.dataset.agent;
    $$("#filter-row .filter-chip").forEach((c) => c.classList.toggle("on", c === chip));
    renderSidebar($("#search").value);
  });
});

$("#refresh").addEventListener("click", async () => {
  const btn = $("#refresh");
  btn.textContent = "↻ …";
  const res = await fetch("/api/sessions");
  SESSIONS = ((await res.json()).sessions) || [];
  await runSearch($("#search").value); // re-run content search against the fresh set
  if (CURRENT_FILE) {
    const item = $(`.session-item[data-file="${cssEscape(CURRENT_FILE)}"]`);
    if (item) item.classList.add("active");
  }
  btn.textContent = "↻ Refresh";
});

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $("#search")) {
    e.preventDefault();
    $("#search").focus();
  }
  if (e.key === "Escape") $$(".dropdown-panel").forEach((p) => p.classList.add("hidden"));
});

// ---------- nav button + outline wiring ----------
$("#nav-prev").addEventListener("click", () => jumpUser(-1));
$("#nav-next").addEventListener("click", () => jumpUser(1));
$("#nav-end").addEventListener("click", scrollToEnd);

let outlineRaf = 0;
$("#main").addEventListener("scroll", () => {
  if (outlineRaf) return;
  outlineRaf = requestAnimationFrame(() => { outlineRaf = 0; highlightOutline(); });
});

// click anywhere outside an open dropdown closes it
document.addEventListener("click", () => {
  $$(".dropdown-panel").forEach((p) => p.classList.add("hidden"));
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
