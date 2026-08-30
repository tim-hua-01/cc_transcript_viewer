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
// Chevron head for a collapsed-by-default block: clicking it toggles the
// block, and the shared .collapsible class carries the collapse CSS.
function toggleHead(block, cls, ...kids) {
  block.classList.add("collapsible");
  const head = el("div", { class: cls }, el("span", { class: "chev" }, "▼"), ...kids);
  head.addEventListener("click", () => block.classList.toggle("collapsed"));
  return head;
}

const esc = (s) => (s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------- theme picker ----------
const THEMES = [
  { id: "warm", name: "Warm", colors: ["#fffdfa", "#efe8dc", "#c0492a"] },
  { id: "paper", name: "Paper", colors: ["#ffffff", "#e9ecef", "#315ca8"] },
  { id: "botanical", name: "Botanical", colors: ["#f8faf6", "#e2e8dd", "#586f5b"] },
  { id: "lavender", name: "Lavender", colors: ["#faf9fc", "#e6e1e9", "#65557b"] },
  { id: "sorbet", name: "Sorbet", colors: ["#fff9f5", "#eddcd7", "#8e4b61"] },
  { id: "night", name: "Night", colors: ["#171a1f", "#292e37", "#e07a5f"] },
  { id: "terminal", name: "Terminal", colors: ["#111614", "#222c27", "#77c995"] },
  { id: "highlighter", name: "Highlighter", colors: ["#fffdf2", "#ffe261", "#8a2457"] },
  { id: "nineties", name: "Nineties", colors: ["#dedede", "#f7f7f7", "#000080"] },
  { id: "system7", name: "System 7", colors: ["#f7f7f7", "#d7d7d7", "#111111"] },
  { id: "bauhaus", name: "Bauhaus", colors: ["#f7f2e7", "#d9b52f", "#962f2f"] },
  { id: "artdeco", name: "Art Deco", colors: ["#faf7ed", "#d8c99f", "#73591f"] },
];

function currentTheme() {
  const id = document.documentElement.dataset.theme;
  return THEMES.some((theme) => theme.id === id) ? id : "warm";
}

function setTheme(id, persist = true) {
  if (!THEMES.some((theme) => theme.id === id)) return;
  document.documentElement.dataset.theme = id;
  if (persist) {
    try { localStorage.setItem("transcript-viewer:theme", id); } catch (_error) {}
  }
  $$(".theme-option").forEach((option) => {
    const selected = option.dataset.theme === id;
    option.setAttribute("aria-checked", selected ? "true" : "false");
    $(".theme-check", option).textContent = selected ? "✓" : "";
  });
}

function closeThemeMenu() {
  $("#theme-menu").hidden = true;
  $("#theme-toggle").setAttribute("aria-expanded", "false");
}

function buildThemePicker() {
  const menu = $("#theme-menu");
  const toggle = $("#theme-toggle");
  for (const theme of THEMES) {
    const swatches = el("span", { class: "theme-swatches", "aria-hidden": "true" },
      theme.colors.map((color) => el("span", { class: "theme-swatch", style: `background:${color}` }))
    );
    const option = el("button", {
      class: "theme-option", type: "button", role: "menuitemradio", "data-theme": theme.id,
      "aria-checked": "false", onclick: () => { setTheme(theme.id); closeThemeMenu(); toggle.focus(); },
    }, swatches, theme.name, el("span", { class: "theme-check", "aria-hidden": "true" }));
    menu.append(option);
  }
  setTheme(currentTheme(), false);
  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    const opening = menu.hidden;
    menu.hidden = !opening;
    toggle.setAttribute("aria-expanded", opening ? "true" : "false");
    if (opening) $(".theme-option[aria-checked='true']", menu)?.focus();
  });
  menu.addEventListener("click", (event) => event.stopPropagation());
  menu.addEventListener("keydown", (event) => {
    const options = $$(".theme-option", menu);
    const index = options.indexOf(document.activeElement);
    if (event.key === "Escape") { closeThemeMenu(); toggle.focus(); return; }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    options[(index + step + options.length) % options.length].focus();
  });
}

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

function localFileLinkTarget(anchor, cwd) {
  if (!cwd) return null;
  let href = anchor.getAttribute("href") || "";
  try { href = decodeURI(href); } catch (_error) { return null; }
  if (!href.startsWith("/")) return null;

  let line = null;
  const hashLine = href.match(/#L(\d+)(?:C\d+)?$/);
  if (hashLine) {
    line = Number(hashLine[1]);
    href = href.slice(0, hashLine.index);
  } else {
    const suffixLine = href.match(/:(\d+)(?::\d+)?$/);
    if (suffixLine) {
      line = Number(suffixLine[1]);
      href = href.slice(0, suffixLine.index);
    }
  }

  const root = String(cwd).replace(/\/+$/, "");
  if (href !== root && !href.startsWith(root + "/")) return null;
  return { path: href, line };
}

// Send JSON to a server endpoint; throws with the server's error message.
async function requestJson(url, body, method = "POST") {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await res.json();
  if (!res.ok || result.error) throw new Error(result.error || `HTTP ${res.status}`);
  return result;
}

async function openLocalFileLink(event, anchor, target) {
  event.preventDefault();
  if (anchor.dataset.opening === "1") return;
  anchor.dataset.opening = "1";
  anchor.classList.add("opening");
  try {
    const result = await requestJson("/api/open-local", { file: CURRENT_FILE, path: target.path });
    anchor.title = "Opened with the default app: " + result.opened;
  } catch (error) {
    window.alert("Could not open local file: " + String(error));
  } finally {
    anchor.dataset.opening = "0";
    anchor.classList.remove("opening");
  }
}

// Session ids that name a database row rather than a transcript file on disk
// (Cursor IDE/CLI and opencode) — nothing to reveal in Finder, nothing to stat.
const SYNTHETIC_ID_RE = /^(cursordb|cursorcli|opencode):/;

function hasTranscriptFile(file) {
  return typeof file === "string" && !!file && !SYNTHETIC_ID_RE.test(file);
}

async function revealTranscriptFile(button, file) {
  if (button.dataset.opening === "1") return;
  button.dataset.opening = "1";
  try {
    await requestJson("/api/reveal-transcript", { file });
  } catch (error) {
    window.alert("Could not reveal transcript file: " + String(error));
  } finally {
    button.dataset.opening = "0";
  }
}

function decorateMarkdownLinks(container, data) {
  const cwd = (data.meta || {}).cwd || "";
  $$(".md a", container).forEach((anchor) => {
    const local = localFileLinkTarget(anchor, cwd);
    if (local) {
      anchor.classList.add("local-file-link");
      // Nothing can open a file on the author's machine from a shared export,
      // so the link becomes a labelled, unclickable path.
      if (STANDALONE) {
        anchor.removeAttribute("href");
        anchor.title = "Local path on the original machine: " + local.path;
        return;
      }
      anchor.href = "#";
      anchor.setAttribute("role", "button");
      anchor.title = "Open with the default app: " + local.path
        + (local.line ? ` (link references line ${local.line})` : "");
      anchor.addEventListener("click", (event) => openLocalFileLink(event, anchor, local));
      return;
    }
    const href = anchor.getAttribute("href") || "";
    if (/^https?:\/\//i.test(href)) {
      anchor.classList.add("external-link");
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
    }
  });
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
// opencode qualifies every model with the provider it was routed through
// ("openrouter/x-ai/grok-4.6"). The provider is the same for a whole session
// list and just crowds the label, so drop that leading segment.
function stripProvider(m) {
  const s = String(m || "");
  const cut = s.indexOf("/");
  return cut === -1 ? s : s.slice(cut + 1);
}
function shortModel(m) {
  if (!m) return "";
  return stripProvider(m)
    .replace(/^claude-/, "")
    .replace(/-\d{8}$/, "")
    .replace(/\[1m\]$/, " (1M)")
    .replace(/-codex$/, "");
}
// The agent / sub-agent / CLI badge trio shown before a session title, in the
// sidebar and in the transcript header. `s` is a summary or a parsed session.
function agentTags(s) {
  return [
    el("span", { class: "agent-tag agent-" + s.agent }, agentLabel(s.agent)),
    s.is_subagent
      ? el("span", { class: "sidechain-tag" }, s.subagent_type === "guardian" ? "guardian" : "sub-agent")
      : null,
    s.cursor_source && String(s.cursor_source).startsWith("cli") && !s.is_subagent
      ? el("span", { class: "sidechain-tag", title: "Cursor CLI agent transcript" }, "CLI")
      : null,
  ];
}

function agentLabel(a) {
  if (a === "codex") return "Codex";
  if (a === "cursor") return "Cursor";
  if (a === "opencode") return "opencode";
  return "Claude";
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

// ---------- standalone export mode ----------
// A saved single-file transcript (see export_html.py) embeds its session here.
// Such a page has no server behind it, so everything that would call one is
// switched off and the session list is dropped; rendering is otherwise
// identical, which is the whole reason the export reuses this file.
const EXPORT_DATA = window.__TRANSCRIPT_EXPORT__ || null;
const STANDALONE = !!EXPORT_DATA;

// ---------- state ----------
let SESSIONS = [];
let SESSIONS_LOADED = false;
let CURRENT_FILE = null;
let CURRENT_DATA = null;
let CURRENT_AGENT = "claude";
let AGENT_FILTER = "all";
// ---------- live auto-refresh (always on) ----------
const SIDEBAR_POLL_MS = 1000;    // heavier scan across every transcript source
const TRANSCRIPT_POLL_MS = 300;  // cheap stat of the open on-disk transcript
let LAST_RENDERED_MTIME = 0;     // mtime of the open transcript as last rendered
let LAST_RENDERED_TITLE = "";    // detects custom-name changes without transcript writes
let LAST_SIG = "";               // cheap fingerprint of the session list
// file -> snippet for the active content search, or null when no search is active
let CONTENT_MATCHES = null;
// Selected values for the dropdown filters; empty set = no constraint (all).
const SELECTED_MODELS = new Set();
const SELECTED_DIRS = new Set();
// Inclusive local-date range over a session's last activity; "" = unbounded.
const DATE_FILTER = { from: "", to: "" };
// Parent session file keys whose linked subagent subtrees are hidden.
// Kept outside renderSidebar so live polling does not reopen collapsed groups.
const COLLAPSED_SUBAGENT_PARENTS = new Set();

// ---------- sidebar ----------
async function loadSessions() {
  const res = await fetch("/api/sessions");
  const data = await res.json();
  SESSIONS = data.sessions || [];
  SESSIONS_LOADED = true;
  LAST_SIG = sessionsSignature(SESSIONS);
  buildFilters();
  renderSidebar($("#search").value || "");
}

// Cheap fingerprint of the session list: changes whenever a file is added,
// removed, or rewritten (mtime bumps). Lets the poller skip needless rebuilds.
function sessionsSignature(list) {
  let sig = list.length + "|";
  for (const s of list) sig += s.file + ":" + (s.mtime || 0) + ":" + (s.custom_title || "") + ";";
  return sig;
}

function sessionMtime(file) {
  const s = SESSIONS.find((x) => x.file === file);
  return s ? (s.mtime || 0) : 0;
}

// ---------- dropdown filters (model / directory / date) ----------
function modelFamily(m) {
  // Match on the bare model id so provider-qualified opencode models
  // ("openrouter/anthropic/claude-opus-4") land in the same family as bare ones.
  const s = String(m || "").toLowerCase().split("/").pop();
  if (s.startsWith("claude")) return "Claude";
  if (s.startsWith("gpt") || s.includes("codex") || s.startsWith("o1") || s.startsWith("o3")) return "GPT";
  return "Other";
}

// Shared shell of every filter dropdown: labelled button, count span, panel,
// and the open-one-close-the-rest wiring.
function dropdownShell(title, panelClass = "dropdown-panel hidden") {
  const wrap = el("div", { class: "dropdown" });
  const count = el("span", { class: "dropdown-count" });
  const btn = el("button", { class: "dropdown-btn" }, title, count, el("span", { class: "chev" }, "▾"));
  const panel = el("div", { class: panelClass });
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = !panel.classList.contains("hidden");
    $$(".dropdown-panel").forEach((p) => p.classList.add("hidden"));
    panel.classList.toggle("hidden", open);
  });
  panel.addEventListener("click", (e) => e.stopPropagation());
  wrap.append(btn, panel);
  return { wrap, count, panel };
}

// Generic multi-select dropdown.
//   groups: [{ label, items: [{value, label}] }]  (no group header if label is "")
//   selected: a Set the dropdown reads from and writes to
//   onChange: called after any change
function makeDropdown(title, groups, selected, onChange) {
  const { wrap, count, panel } = dropdownShell(title);

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

  updateCount();
  return wrap;
}

// Local calendar day of an epoch-seconds timestamp, as sortable "YYYY-MM-DD".
function dayOf(mtime) {
  const d = new Date((mtime || 0) * 1000);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
    "-" + String(d.getDate()).padStart(2, "0");
}

// "YYYY-MM-DD" for the day `back` days before today.
function dayAgo(back) {
  return dayOf(Date.now() / 1000 - back * 86400);
}

function makeDateDropdown(onChange) {
  const { wrap, count, panel } = dropdownShell("Date", "dropdown-panel anchor-right hidden");

  const fromInput = el("input", { type: "date" });
  const toInput = el("input", { type: "date" });
  fromInput.value = DATE_FILTER.from;
  toInput.value = DATE_FILTER.to;

  const shortDay = (day) => {
    const [y, m, d] = day.split("-");
    return Number(m) + "/" + Number(d);
  };
  const updateCount = () => {
    const { from, to } = DATE_FILTER;
    count.textContent =
      from && to ? ` (${shortDay(from)}–${shortDay(to)})`
      : from ? ` (≥ ${shortDay(from)})`
      : to ? ` (≤ ${shortDay(to)})`
      : "";
  };
  const apply = () => {
    DATE_FILTER.from = fromInput.value || "";
    DATE_FILTER.to = toInput.value || "";
    updateCount(); onChange();
  };
  fromInput.addEventListener("change", apply);
  toInput.addEventListener("change", apply);

  panel.append(
    el("label", { class: "dd-date-row" }, "From", fromInput),
    el("label", { class: "dd-date-row" }, "To", toInput)
  );

  const preset = (label, back) => {
    const b = el("button", { class: "dd-preset" }, label);
    b.addEventListener("click", () => {
      fromInput.value = dayAgo(back);
      toInput.value = "";
      apply();
    });
    return b;
  };
  panel.append(el("div", { class: "dd-presets" },
    preset("Today", 0), preset("7 days", 7), preset("30 days", 30)));

  const clear = el("button", { class: "dd-clear" }, "Clear");
  clear.addEventListener("click", () => {
    fromInput.value = toInput.value = "";
    apply();
  });
  panel.append(clear);

  updateCount();
  return wrap;
}

// The dropdowns' option sets. Rebuilding the dropdowns closes any open panel,
// so the live poller only rebuilds when an option actually appeared or
// vanished — not on every session-list change.
let FILTER_OPTIONS_SIG = "";
function filterOptionsSignature() {
  const models = [...new Set(SESSIONS.map((s) => s.model).filter(Boolean))].sort();
  const dirs = [...new Set(SESSIONS.map((s) => s.cwd).filter(Boolean))].sort();
  return JSON.stringify([models, dirs]);
}

function buildFilters() {
  FILTER_OPTIONS_SIG = filterOptionsSignature();
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

  host.append(makeDateDropdown(() => renderSidebar($("#search").value)));
}

function renderSidebar(query) {
  const list = $("#session-list");
  list.innerHTML = "";
  const q = query.trim().toLowerCase();

  // Every filter except the agent chips, which are handled by the caller —
  // the chips display counts of what selecting them would show.
  const matchesFilters = (s) => {
    if (SELECTED_MODELS.size && !SELECTED_MODELS.has(s.model || "")) return false;
    if (SELECTED_DIRS.size && !SELECTED_DIRS.has(s.cwd || "")) return false;
    if (DATE_FILTER.from || DATE_FILTER.to) {
      // Same recency the row displays: last activity, not file mtime.
      const day = dayOf(s.last_ts ? Date.parse(s.last_ts) / 1000 : s.mtime);
      if (DATE_FILTER.from && day < DATE_FILTER.from) return false;
      if (DATE_FILTER.to && day > DATE_FILTER.to) return false;
    }
    if (!q) return true;
    const metaHit = (
      s.title + " " + (s.original_title || "") + " " + (s.ai_title || "") + " " +
      (s.cwd || "") + " " + s.id + " " + s.agent
    ).toLowerCase().includes(q);
    const contentHit = CONTENT_MATCHES && CONTENT_MATCHES.has(s.file);
    return metaHit || contentHit;
  };
  const matches = SESSIONS.filter(
    (s) => (AGENT_FILTER === "all" || s.agent === AGENT_FILTER) && matchesFilters(s)
  );

  // With an active query, order by relevance score; otherwise keep the
  // server's newest-first ordering.
  if (q) {
    const scoreOf = (s) => {
      const cm = CONTENT_MATCHES && CONTENT_MATCHES.get(s.file);
      return cm ? cm.score : 0.5; // metaHit-only (e.g. id match) sits below content hits
    };
    matches.sort((a, b) => (scoreOf(b) - scoreOf(a)) || ((b.mtime || 0) - (a.mtime || 0)));
  }

  // Per-agent counts ride on the filter chips, counted against every filter
  // except the agent chip itself (collapsed sub-agent groups still count —
  // collapsing hides rows, it doesn't exclude sessions). The chips make a
  // separate totals line redundant.
  const chipEligible = SESSIONS.filter(matchesFilters);
  for (const chip of $$("#filter-row .filter-chip")) {
    const a = chip.dataset.agent;
    const n = a === "all" ? chipEligible.length : chipEligible.filter((s) => s.agent === a).length;
    chip.replaceChildren(
      a === "all" ? "All" : agentLabel(a),
      el("span", { class: "chip-count" }, String(n))
    );
    // An agent with nothing to show contributes only clutter — but never hide
    // "All" or the selected chip (the way back out of a filter).
    chip.hidden = n === 0 && a !== "all" && a !== AGENT_FILTER;
  }

  const matchedByFile = new Map(matches.map((s) => [s.file, s]));
  const subagentCounts = new Map();
  for (const s of matches) {
    if (!s.is_subagent || !s.parent_file || !matchedByFile.has(s.parent_file)) continue;
    subagentCounts.set(s.parent_file, (subagentCounts.get(s.parent_file) || 0) + 1);
  }

  for (const s of matches) {
    // Searches should always expose their matching results. A filtered orphan
    // also remains visible when its parent is not in the current result set.
    if (!q && s.is_subagent) {
      let parentFile = s.parent_file;
      const visited = new Set();
      let hiddenByAncestor = false;
      while (parentFile && matchedByFile.has(parentFile) && !visited.has(parentFile)) {
        if (COLLAPSED_SUBAGENT_PARENTS.has(parentFile)) {
          hiddenByAncestor = true;
          break;
        }
        visited.add(parentFile);
        parentFile = matchedByFile.get(parentFile).parent_file;
      }
      if (hiddenByAncestor) continue;
    }

    const subagentCount = subagentCounts.get(s.file) || 0;
    const subagentsCollapsed = COLLAPSED_SUBAGENT_PARENTS.has(s.file);
    const subagentToggle = subagentCount && !q
      ? el(
          "button",
          {
            class: "subagent-toggle",
            type: "button",
            title: `${subagentsCollapsed ? "Expand" : "Collapse"} ${subagentCount} sub-agent${subagentCount === 1 ? "" : "s"}`,
            "aria-label": `${subagentsCollapsed ? "Expand" : "Collapse"} sub-agents`,
            "aria-expanded": subagentsCollapsed ? "false" : "true",
            onclick: (e) => {
              e.stopPropagation();
              if (subagentsCollapsed) COLLAPSED_SUBAGENT_PARENTS.delete(s.file);
              else COLLAPSED_SUBAGENT_PARENTS.add(s.file);
              renderSidebar($("#search").value);
              markActive();
            },
          },
          el("span", { "aria-hidden": "true" }, subagentsCollapsed ? "▸" : "▾"),
          `${subagentsCollapsed ? "Expand" : "Collapse"} subagents`,
          el("span", { class: "subagent-count" }, `(${subagentCount})`)
        )
      : null;
    const tsForRel = s.last_ts || (s.mtime ? new Date(s.mtime * 1000).toISOString() : null);
    const item = el(
      "div",
      { class: "session-item" + (s.is_subagent ? " subagent" : ""), "data-file": s.file },
      el(
        "div",
        { class: "session-toprow" },
        ...agentTags(s),
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
        s.agent_name && s.agent_name !== s.title
          ? el("span", { class: "badge", title: "Claude agent name" }, s.agent_name)
          : null,
        s.model ? el("span", { class: "badge" }, shortModel(s.model)) : null
      ),
      CONTENT_MATCHES && CONTENT_MATCHES.has(s.file)
        ? el("div", { class: "session-snippet" }, CONTENT_MATCHES.get(s.file).snippet)
        : null,
      el(
        "div",
        { class: "session-id", title: "Click to copy full id: " + s.id, onclick: (e) => copyId(e, s.id) },
        s.id
      ),
      subagentToggle
    );
    item.addEventListener("click", () => openSession(s.file, item));
    item.addEventListener("contextmenu", (e) => showSessionContextMenu(e, s.file));
    list.append(item);
  }

  if (!list.children.length) {
    // "No transcripts found" is only true when nothing is filtered out —
    // an active chip/model/directory/date filter empties the list too.
    const filtered = AGENT_FILTER !== "all" || SELECTED_MODELS.size ||
      SELECTED_DIRS.size || DATE_FILTER.from || DATE_FILTER.to;
    const message = q || filtered
      ? "No matching sessions."
      : SESSIONS_LOADED
        ? "No transcripts found."
        : "Loading sessions…";
    list.append(el("div", { class: "empty-note" }, message));
  }
}

// Briefly show `text` on a control, then put its label back.
function flashLabel(node, text, ms, restore = node.textContent) {
  node.textContent = text;
  setTimeout(() => { node.textContent = restore; }, ms);
}

function copyId(e, id) {
  e.stopPropagation();
  const node = e.currentTarget;
  navigator.clipboard?.writeText(id).then(() => flashLabel(node, "copied ✓", 900), () => {});
}

const COPY_ALLOWED_KINDS = new Set(["user", "assistant", "reasoning", "tool"]);

function appendCopyEvent(lines, ev, branchLabel = "") {
  if (ev.kind === "branch") {
    (ev.groups || []).forEach((group, index) => {
      lines.push(`## Abandoned branch ${index + 1}`);
      for (const child of group) appendCopyEvent(lines, child, "abandoned branch");
    });
    return;
  }

  // Only copy user/model messages, reasoning, and tool calls (with diffs).
  // Skip system messages, tool results, and other non-conversational noise.
  if (!COPY_ALLOWED_KINDS.has(ev.kind)) return;

  const labels = { user: "User", assistant: "Assistant", reasoning: "Reasoning", tool: "Tool" };
  // Same rule as the turn headers: "<synthetic>" is Claude Code's placeholder
  // on machine-written records, not a model worth naming in copied text.
  const details = [ev.model !== "<synthetic>" && ev.model, ev.phase, ev.status, branchLabel]
    .filter(Boolean);
  lines.push("## " + (labels[ev.kind] || ev.kind || "Event") + (details.length ? " · " + details.join(" · ") : ""));

  if (Array.isArray(ev.blocks)) {
    for (const block of ev.blocks) {
      if (block.type === "text" || block.type === "thinking") {
        if (block.type === "thinking") lines.push("<thinking>");
        lines.push(block.text || "");
        if (block.type === "thinking") lines.push("</thinking>");
      } else if (block.type === "image") {
        lines.push("[image]");
      } else if (block.type === "tool_use") {
        lines.push("Tool: " + (block.name || "tool"));
        lines.push(...toolCallToText(block.name, block.input));
      }
    }
  } else if (ev.text) {
    lines.push(ev.text);
  }

  if (ev.kind === "tool") {
    lines.push("Tool: " + (ev.name || "tool"));
    lines.push(...toolCallToText(ev.name, ev.input));
  }
  for (const _image of ev.images || []) lines.push("[image]");
  for (const image of ev.local_images || []) lines.push("[local image: " + image + "]");
  lines.push("");
}

function transcriptToText(data) {
  const meta = data.meta || {};
  const lines = ["# " + (data.title || "(untitled session)")];
  if (meta.cwd) lines.push("Project: " + meta.cwd);
  if (meta.model) lines.push("Model: " + meta.model);
  if (data.id) lines.push("Session: " + data.id);
  lines.push("");
  for (const ev of data.events || []) appendCopyEvent(lines, ev);
  return lines.join("\n").trimEnd() + "\n";
}

async function writeClipboard(text) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  const area = el("textarea", { style: "position:fixed;left:-9999px;top:0" });
  area.value = text;
  document.body.append(area);
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw new Error("Clipboard access unavailable");
}

async function copyAll(e) {
  const button = e.currentTarget;
  try {
    await writeClipboard(transcriptToText(CURRENT_DATA || {}));
    flashLabel(button, "Copied", 1200);
  } catch (_error) {
    flashLabel(button, "Copy failed", 1200);
  }
}

// Content-Disposition: attachment; filename="foo.html"  ->  foo.html
function filenameFromDisposition(header) {
  const m = /filename="([^"]+)"/.exec(header || "");
  return m ? m[1] : "";
}

// Download the open transcript as one self-contained HTML file: the viewer's
// own UI with this session baked in, shareable without the server or the
// original ~/.claude / ~/.codex / database it was read from.
async function saveTranscriptHtml(e) {
  const button = e.currentTarget;
  const original = button.textContent;
  if (button.dataset.saving === "1") return;
  button.dataset.saving = "1";
  button.textContent = "Saving…";
  try {
    const res = await fetch("/api/export?file=" + encodeURIComponent(CURRENT_FILE));
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).error || detail; } catch (_error) {}
      throw new Error(detail);
    }
    const blob = await res.blob();
    const name = filenameFromDisposition(res.headers.get("Content-Disposition")) || "transcript.html";
    const url = URL.createObjectURL(blob);
    const anchor = el("a", { href: url, download: name });
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    // Revoked late: Safari cancels the download if the blob dies too early.
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    flashLabel(button, "Saved", 1400, original);
  } catch (error) {
    button.title = "Could not save transcript: " + String(error);
    flashLabel(button, "Save failed", 1400, original);
  } finally {
    button.dataset.saving = "0";
  }
}

function setPanelCollapsed(panel, collapsed) {
  document.body.classList.toggle(panel + "-collapsed", collapsed);
  try { localStorage.setItem("transcript-viewer:" + panel + "-collapsed", collapsed ? "1" : "0"); } catch (_error) {}
}

function wirePanelToggles() {
  for (const panel of ["sidebar", "outline"]) {
    let collapsed = false;
    try { collapsed = localStorage.getItem("transcript-viewer:" + panel + "-collapsed") === "1"; } catch (_error) {}
    setPanelCollapsed(panel, collapsed);
    $("#collapse-" + panel).addEventListener("click", () => setPanelCollapsed(panel, true));
    $("#show-" + panel).addEventListener("click", () => setPanelCollapsed(panel, false));
  }
}

async function renameSession(data) {
  const file = CURRENT_FILE;
  const entered = window.prompt(
    "Custom transcript name (leave blank to restore the original)",
    data.custom_title || ""
  );
  if (entered === null) return;
  try {
    const result = await requestJson("/api/session-name", { file, name: entered }, "PUT");
    Object.assign(data, result);
    const summary = SESSIONS.find((s) => s.file === file);
    if (summary) Object.assign(summary, result);
    LAST_SIG = sessionsSignature(SESSIONS);
    await runSearch($("#search").value);
    markActive();
    if (CURRENT_FILE === file) await renderTranscript(data, { keepScroll: true });
  } catch (e) {
    window.alert("Could not save transcript name: " + String(e));
  }
}

// ---------- session view ----------
function sessionHashUrl(file) {
  return location.pathname + location.search + "#file=" + encodeURIComponent(file);
}

function openSessionInNewTab(file) {
  window.open(sessionHashUrl(file), "_blank", "noopener,noreferrer");
}

let SESSION_CTX_MENU = null;
function hideSessionContextMenu() {
  if (SESSION_CTX_MENU) {
    SESSION_CTX_MENU.remove();
    SESSION_CTX_MENU = null;
  }
}

function showSessionContextMenu(e, file) {
  e.preventDefault();
  e.stopPropagation();
  hideSessionContextMenu();
  const menu = el(
    "div",
    { class: "ctx-menu", role: "menu" },
    el("button", {
      class: "ctx-menu-item",
      type: "button",
      role: "menuitem",
      onclick: () => {
        hideSessionContextMenu();
        openSessionInNewTab(file);
      },
    }, "Open in new tab")
  );
  document.body.append(menu);
  SESSION_CTX_MENU = menu;

  const pad = 6;
  const rect = menu.getBoundingClientRect();
  let left = e.clientX;
  let top = e.clientY;
  if (left + rect.width > window.innerWidth - pad) left = window.innerWidth - rect.width - pad;
  if (top + rect.height > window.innerHeight - pad) top = window.innerHeight - rect.height - pad;
  menu.style.left = Math.max(pad, left) + "px";
  menu.style.top = Math.max(pad, top) + "px";
}

async function openSession(file, itemEl) {
  if (STANDALONE) return;
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
  t.replaceChildren(el("div", { class: "spinner" }, "Loading transcript…"));

  try {
    const res = await fetch("/api/session?file=" + encodeURIComponent(file));
    const data = await res.json();
    if (CURRENT_FILE !== file) return;
    if (data.error) { t.replaceChildren(el("div", { class: "empty-note" }, "Error: " + data.error)); return; }
    const rendered = await renderTranscript(data);
    if (rendered && CURRENT_FILE === file) LAST_RENDERED_MTIME = sessionMtime(file);
  } catch (e) {
    t.replaceChildren(el("div", { class: "empty-note" }, "Failed to load: " + String(e)));
  }
}

let RENDER_GENERATION = 0;

function renderTranscript(data, opts = {}) {
  const t = $("#transcript");
  t.innerHTML = "";
  CURRENT_AGENT = data.agent || "claude";
  LAST_RENDERED_TITLE = data.title || "";
  CURRENT_DATA = data;

  const meta = data.meta || {};
  // Parsed sessions carry no path of their own; the id we loaded them by is one
  // (except for the synthetic Cursor database schemes).
  const transcriptFile = data.file || CURRENT_FILE;
  const header = el(
    "div",
    { class: "t-header" },
    el(
      "div",
      { class: "t-title-row" },
      el(
        "h1",
        { class: "t-title" },
        ...agentTags(data),
        " " + (data.title || "(untitled session)")
      ),
      STANDALONE ? null : el("button", {
        class: "rename-btn",
        title: "Rename transcript",
        "aria-label": "Rename transcript",
        onclick: () => renameSession(data),
      }, "✎")
    ),
    data.custom_title && data.original_title
      ? el("div", { class: "t-aititle", title: "Title derived from the transcript" }, "Original title: " + data.original_title)
      : null,
    // The agent's own AI-generated session title (Claude Code's /resume label),
    // shown as a muted subtitle when present and not identical to the prompt title.
    data.ai_title && data.ai_title !== data.original_title
      ? el("div", { class: "t-aititle", title: "AI-generated session title" }, "Short title: " + data.ai_title)
      : null,
    el(
      "div",
      { class: "t-meta" },
      data.is_subagent && data.parent_file
        ? (STANDALONE
            ? el("span", { class: "parent-link", title: "The parent session is not part of this export" },
                "↑ parent session")
            : el(
                "a",
                {
                  class: "parent-link",
                  href: "#",
                  onclick: (e) => { e.preventDefault(); openSession(data.parent_file); },
                },
                "↑ parent session"
              ))
        : null,
      // Cross-session fork: link back to the session this one branched from.
      data.forked_from
        ? (data.forked_from.file && !STANDALONE
            ? el(
                "a",
                {
                  class: "parent-link",
                  href: "#",
                  title: "Branched from session " + data.forked_from.session_id,
                  onclick: (e) => { e.preventDefault(); openSession(data.forked_from.file); },
                },
                "⑂ forked from " + (data.forked_from.session_id || "").slice(0, 8) + "…"
              )
            : el("span", { title: "Branched from session " + data.forked_from.session_id },
                "⑂ forked from " + (data.forked_from.session_id || "").slice(0, 8) + "…"))
        : null,
      meta.pr && meta.pr.url
        ? el(
            "a",
            {
              class: "parent-link",
              href: meta.pr.url,
              target: "_blank",
              rel: "noopener noreferrer",
              title: meta.pr.repository || "Open pull request",
            },
            meta.pr.number != null ? "PR #" + meta.pr.number : "Pull request"
          )
        : null,
      data.agent_name && data.agent_name !== data.title
        ? el("span", { class: "badge", title: "Claude agent name" }, data.agent_name)
        : null,
      meta.cwd ? el("span", {}, "📁 " + meta.cwd) : null,
      meta.git_branch ? el("span", {}, "⎇ " + meta.git_branch) : null,
      meta.model ? el("span", {}, shortModel(meta.model)) : null,
      meta.reasoning_effort ? el("span", {}, "effort: " + meta.reasoning_effort) : null,
      typeof meta.source === "string" ? el("span", {}, "source: " + meta.source) : null,
      meta.version ? el("span", {}, "v" + meta.version) : null,
      el("span", {}, data.events.length + " events"),
      el("span", { class: "session-id", style: "margin:0", title: "Click to copy", onclick: (e) => copyId(e, data.id) }, "id: " + data.id),
      !STANDALONE && hasTranscriptFile(transcriptFile)
        ? el("button", {
            class: "reveal-btn",
            title: "Show the transcript file in Finder: " + transcriptFile,
            "aria-label": "Show transcript file in Finder",
            onclick: (e) => revealTranscriptFile(e.currentTarget, transcriptFile),
          }, "📂")
        : null
    ),
    el(
      "div",
      { class: "t-controls" },
      el("button", { class: "btn", onclick: () => setAll(".thinking-block", true) }, "Collapse thinking"),
      el("button", { class: "btn", onclick: () => setAll(".thinking-block", false) }, "Expand thinking"),
      el("button", { class: "btn", onclick: () => setAll(".tool-block", true) }, "Collapse tools"),
      el("button", { class: "btn", onclick: () => setAll(".tool-block", false) }, "Expand tools"),
      el("button", { class: "btn", onclick: copyAll, title: "Copy transcript as plain text" }, "Copy all text"),
      STANDALONE ? null : el("button", {
        class: "btn",
        onclick: saveTranscriptHtml,
        title: "Download this transcript as one self-contained HTML file you can share",
      }, "Save HTML"),
      el("button", { class: "btn", onclick: scrollToEnd }, "⤓ Jump to end")
    )
  );
  t.append(header);

  const events = data.events || [];
  const progressFill = el("div", { class: "render-progress-fill" });
  const progress = el(
    "div",
    {
      class: "render-progress",
      role: "progressbar",
      "aria-label": "Rendering transcript",
      "aria-valuemin": "0",
      "aria-valuemax": String(events.length),
      "aria-valuenow": "0",
    },
    progressFill
  );
  if (events.length > 100) t.append(progress);

  const generation = ++RENDER_GENERATION;
  return new Promise((resolve) => {
    let index = 0;
    function renderBatch(budgetMs) {
      if (generation !== RENDER_GENERATION) {
        resolve(false);
        return;
      }
      const fragment = document.createDocumentFragment();
      const started = performance.now();
      while (index < events.length && performance.now() - started < budgetMs) {
        const node = renderEvent(events[index++]);
        if (node) fragment.append(node);
      }
      t.append(fragment);
      if (progress.isConnected) {
        const percent = events.length ? (index / events.length) * 100 : 100;
        progressFill.style.width = percent + "%";
        progress.setAttribute("aria-valuenow", String(index));
      }
      if (index < events.length) {
        requestAnimationFrame(() => renderBatch(9));
        return;
      }
      progress.remove();
      decorateMarkdownLinks(t, data);
      groupTurnRuns(t);
      if (!opts.keepScroll) $("#main").scrollTop = 0;
      buildOutline();
      resolve(true);
    }
    renderBatch(30);
  });
}

// Collapse consecutive turns that share a header (same author/model) into one
// connected run: the first keeps its header (run-start), the rest hide theirs
// and butt up against it (run-mid / run-end). This is what
// turns a wall of repeated "Claude · model · time" cards into a single tidy
// column of actions. User prompts are never folded — they stay distinct so they
// remain clear anchors (and outline targets).
function groupTurnRuns(container) {
  // Only group top-level turns; turns nested inside an abandoned-branch block
  // are their own (collapsed) context and shouldn't merge with the live thread.
  const turns = $$(".turn", container).filter((t) => !t.closest(".branch-block"));
  let uid = 0;
  const sig = (t) => {
    if (t.classList.contains("turn-user")) return "user-" + uid++; // never groups
    const head = t.querySelector(".turn-head");
    if (!head) return "none-" + uid++;
    // Key on author/model/etc. but NOT the timestamp — otherwise a continuous
    // run of actions spanning a minute boundary would split and re-show the
    // header every minute. The displayed time stays on the run-start head.
    const timeText = (head.querySelector(".turn-time") || {}).textContent || "";
    const headText = head.textContent.replace(timeText, "").replace(/\s+/g, " ").trim();
    return t.className + "|" + headText;
  };
  const sigs = turns.map(sig);
  let i = 0;
  while (i < turns.length) {
    let j = i;
    while (j + 1 < turns.length && sigs[j + 1] === sigs[i]) j++;
    if (j > i) {
      turns[i].classList.add("run-start");
      for (let k = i + 1; k < j; k++) turns[k].classList.add("run-mid");
      turns[j].classList.add("run-end");
    }
    i = j + 1;
  }
}

// ---------- live transcript refresh (scroll- and state-preserving) ----------
// Snapshot what the reader is looking at: scroll offset, whether they're pinned
// to the bottom (so we can tail-follow), and which collapsible blocks are open.
function captureView() {
  const main = $("#main");
  const atBottom = main.scrollHeight - main.scrollTop - main.clientHeight < 40;
  const expanded = {};
  for (const sel of [".thinking-block", ".tool-block", ".status-block", ".instructions-block", ".branch-block"])
    expanded[sel] = $$(sel).map((n) => !n.classList.contains("collapsed"));
  return { top: main.scrollTop, atBottom, expanded };
}

// Re-apply a captured view after a full re-render. Existing blocks keep their
// index (the transcript only grows by appending), so open/closed state sticks;
// new blocks appended at the end stay collapsed.
function restoreView(v) {
  for (const sel of Object.keys(v.expanded)) {
    const nodes = $$(sel);
    v.expanded[sel].forEach((open, i) => { if (nodes[i]) nodes[i].classList.toggle("collapsed", !open); });
  }
  const main = $("#main");
  main.scrollTop = v.atBottom ? main.scrollHeight : v.top;
}

let transcriptRefreshInFlight = false;
async function refreshOpenTranscript(knownMtime = null) {
  if (!CURRENT_FILE) return;
  if (transcriptRefreshInFlight) return;
  transcriptRefreshInFlight = true;
  const file = CURRENT_FILE;
  const view = captureView();
  try {
    const res = await fetch("/api/session?file=" + encodeURIComponent(file));
    const data = await res.json();
    if (data.error || CURRENT_FILE !== file) return;
    const rendered = await renderTranscript(data, { keepScroll: true });
    if (rendered && CURRENT_FILE === file) {
      restoreView(view);
      LAST_RENDERED_MTIME = knownMtime == null ? sessionMtime(file) : knownMtime;
    }
  } catch (e) { /* transient; try again next poll */ }
  finally { transcriptRefreshInFlight = false; }
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
  // Skip prompts inside abandoned branches — they aren't part of the live thread.
  USER_TURNS = $$(".turn-user:not(.sidechain)", t).filter((n) => !n.closest(".branch-block"));
  const outline = $("#outline");
  const list = $("#outline-list");
  const nav = $("#nav-buttons");
  list.innerHTML = "";

  if (!USER_TURNS.length) {
    outline.hidden = true;
    nav.hidden = true;
    document.body.classList.remove("has-outline");
    return;
  }
  outline.hidden = false;
  nav.hidden = false;
  document.body.classList.add("has-outline");

  // Walk user prompts and abandoned-branch markers together in document order so
  // branches appear in the outline at the point they occur. Branch entries use a
  // separate class so they stay out of the numbered `.outline-item` sequence that
  // highlightOutline()/nav rely on.
  const anchors = $$(".turn-user:not(.sidechain), .branch-block", t).filter((n) =>
    n.classList.contains("branch-block") ? true : !n.closest(".branch-block")
  );
  let ui = 0;
  anchors.forEach((node) => {
    if (node.classList.contains("branch-block")) {
      const label = ((node.querySelector(".tool-name") || {}).textContent || "abandoned branch").replace(/^⑂\s*/, "");
      const item = el(
        "div",
        { class: "outline-branch", title: label },
        el("span", { class: "outline-num" }, "⑂"),
        el("span", { class: "outline-text" }, label)
      );
      item.addEventListener("click", () => {
        node.classList.remove("collapsed");
        node.scrollIntoView({ block: "start" });
        $("#main").scrollBy(0, -20);
      });
      list.append(item);
      return;
    }
    const i = ui++;
    node.id = "user-turn-" + i;
    const full = (($(".turn-body", node) || {}).textContent || "").trim().replace(/\s+/g, " ");
    const label = full.slice(0, OUTLINE_MAX) || "(empty message)";
    const item = el(
      "div",
      { class: "outline-item", title: full.slice(0, 500) },
      el("span", { class: "outline-num" }, String(i + 1)),
      el("span", { class: "outline-text" }, label)
    );
    item.addEventListener("click", () => scrollToTurn(node));
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
    case "instructions": return renderInstructions(ev);
    case "system": return renderSystem(ev);
    case "notice": return renderNotice(ev);
    case "attachment": return renderAttachment(ev);
    case "guardian_request": return renderGuardianRequest(ev);
    case "guardian_decision": return renderGuardianDecision(ev);
    case "reasoning": return turnShell("reasoning", "Reasoning", ev, [renderReasoning(ev)]);
    case "tool": return turnShell("tool", "Tool · " + (ev.name || "tool"), ev, [renderCodexTool(ev)]);
    case "web_search":
    case "web_call": return turnShell("web_call", "Web search", ev, [renderWebSearch(ev)]);
    case "branch": return renderBranch(ev);
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
    ev.recovered
      ? el("span", {
          class: "recovered-tag",
          title: "Cursor dropped this message from its conversation index during a checkpoint rebuild; recovered from orphaned data — placement in the timeline is approximate.",
        }, "recovered")
      : null,
    ev.phase ? el("span", { class: "phase-tag" }, ev.phase) : null,
    ev.status ? el("span", { class: "status-tag" }, ev.status) : null,
    // "<synthetic>" is Claude Code's placeholder on machine-written records
    // (interruptions, API errors) — not a model worth labelling the turn with.
    ev.model && ev.model !== "<synthetic>"
      ? el("span", { class: "muted", style: "font-weight:400" }, shortModel(ev.model)) : null,
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
      else if (b.type === "image" && (b.data_uri || b.src)) body.push(el("img", { src: b.data_uri || b.src, style: "max-width:100%;border-radius:8px", loading: "lazy" }));
    }
  } else {
    // Codex shape
    if (ev.text) body.push(el("div", { class: "md", html: md(ev.text) }));
    for (const img of ev.images || []) body.push(renderImagePayload(img));
    for (const img of ev.local_images || []) body.push(el("div", { class: "attach-meta" }, "local image: " + img));
  }
  if (ev.turn_metadata) body.push(renderTurnMetadata(ev.turn_metadata));
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
    if (ev.turn_metadata) body.push(renderTurnMetadata(ev.turn_metadata));
    return turnShell("assistant", agentLabel(CURRENT_AGENT), ev, body);
  }
  // Codex shape (flat text; reasoning/tools are separate events). An exported
  // Cursor/opencode rollout parses as Codex but belongs to the tool it came
  // from, so name the turn after the session's agent rather than hardcoding it.
  const body = [el("div", { class: "md", html: md(ev.text || "") })];
  if (ev.turn_metadata) body.push(renderTurnMetadata(ev.turn_metadata));
  return turnShell("assistant", agentLabel(CURRENT_AGENT), ev, body);
}

function renderTurnMetadata(metadata, label = "Turn metadata") {
  return collapsibleBlock(
    "turn-metadata collapsed",
    label,
    [preFrom(JSON.stringify(metadata, null, 2))]
  );
}

function renderGuardianRequest(ev) {
  const request = ev.request || {};
  const body = [];
  if (ev.context) {
    body.push(el("pre", { class: "payload guardian-review-context" }, ev.context));
  }
  const facts = [];
  if (request.tool) facts.push(el("span", { class: "badge" }, request.tool));
  if (request.sandbox_permissions) facts.push(el("span", { class: "badge" }, request.sandbox_permissions));
  if (facts.length) body.push(el("div", { class: "guardian-facts" }, facts));
  if (request.command) {
    let command = request.command;
    if (Array.isArray(command)) {
      command = command.length >= 3 && command[1] === "-lc" ? command[2] : command.join(" ");
    }
    body.push(el("pre", { class: "payload" }, String(command)));
  }
  if (request.cwd) body.push(el("div", { class: "attach-meta" }, "cwd: " + request.cwd));
  if (request.justification) body.push(el("div", { class: "guardian-justification" }, request.justification));
  if (ev.metadata && Object.keys(ev.metadata).length) {
    body.push(renderTurnMetadata(ev.metadata));
  }
  return turnShell("user", "Review input", ev, body);
}

// Guardian decisions carry bare values like "low"/"high"; label them so the
// badges say what they measure.
const GUARDIAN_DECISION_FACTS = [
  ["risk", "risk_level"],
  ["user authorization", "user_authorization"],
];

function renderGuardianDecision(ev) {
  const outcome = ev.outcome === "deny" ? "deny" : "allow";
  const body = [
    el("div", { class: "guardian-decision guardian-" + outcome }, outcome.toUpperCase()),
  ];
  const facts = GUARDIAN_DECISION_FACTS.filter(([, key]) => ev[key]);
  if (facts.length) {
    body.push(el("div", { class: "guardian-facts" }, facts.map(([label, key]) =>
      el(
        "span",
        { class: "badge guardian-fact" },
        el("span", { class: "guardian-fact-label" }, label),
        String(ev[key])
      )
    )));
  }
  if (ev.rationale) body.push(el("div", { class: "guardian-rationale" }, ev.rationale));
  return turnShell("guardian", "Decision", ev, body);
}

// Thinking (a block inside a turn) and reasoning (a Codex top-level event)
// render identically; only the labels and the empty-state sentence differ.
function thoughtBlock(text, { label, emptyLabel, emptyText, pill }) {
  const hasText = text && text.trim();
  const block = el("div", { class: "thinking-block collapsed" + (hasText ? "" : " empty") });
  const head = toggleHead(
    block,
    "thinking-head",
    el("span", {}, hasText ? label : emptyLabel),
    hasText && pill ? el("span", { class: "tool-caller" }, pill) : null
  );
  const body = hasText
    ? el("div", { class: "thinking-body md", html: md(text) })
    : el("div", { class: "thinking-body thinking-empty" }, emptyText);
  block.append(head, body);
  return block;
}

function renderThinking(b) {
  return thoughtBlock(b.text, {
    label: "💭 Thinking",
    emptyLabel: "💭 Thinking (not recorded)",
    // What's shown is a provider-written summary; the real chain of thought
    // came back encrypted and is not in the transcript.
    pill: b.has_encrypted ? "summary · full reasoning encrypted" : "",
    emptyText: "Claude Code doesn't save thinking text to the transcript — only an encrypted signature is stored, so there's nothing to display here.",
  });
}

function renderReasoning(ev) {
  return thoughtBlock(ev.text, {
    label: "💭 Reasoning summary",
    emptyLabel: "💭 Reasoning not readable",
    emptyText: ev.has_encrypted
      ? "Codex saved encrypted reasoning content for continuation, not readable reasoning text."
      : "No reasoning text was recorded.",
  });
}

// Codex injected system prompt / context (developer instructions, base prompt,
// environment_context, AGENTS.md). Collapsed by default — it's large and static.
function renderInstructions(ev) {
  const block = collapsibleBlock(
    "instructions-block collapsed",
    ev.label || "Instructions",
    [preFrom(ev.text || "", "payload")]
  );
  return turnShell("instructions", "📋 Instructions", ev, [block]);
}

// System-injected message recorded as a `user` record but not a real prompt
// (background-task notification, slash-command echo, hook output, …). Rendered
// de-emphasized and — crucially — not as a `.turn-user`, so it stays out of the
// user-message outline on the right.
function renderNotice(ev) {
  return turnShell("notice", ev.label || "Notice", ev, [
    preFrom(ev.text || ""),
  ]);
}

function renderSystem(ev) {
  if (ev.subtype === "compact_boundary") {
    const body = [];
    const c = ev.compaction || {};
    const stats = [];
    if (c.trigger) stats.push(el("span", { class: "badge" }, c.trigger));
    if (c.pre_tokens != null || c.post_tokens != null) {
      const before = c.pre_tokens != null ? Number(c.pre_tokens).toLocaleString() : "?";
      const after = c.post_tokens != null ? Number(c.post_tokens).toLocaleString() : "?";
      stats.push(el("span", { class: "badge" }, before + " → " + after + " tokens"));
    }
    if (c.duration_ms != null) stats.push(el("span", { class: "badge" }, fmtDuration(c.duration_ms)));
    if (c.preserved_messages != null) {
      stats.push(el("span", { class: "badge" }, c.preserved_messages + " messages preserved"));
    }
    if (c.discovered_tools) stats.push(el("span", { class: "badge" }, c.discovered_tools + " tools retained"));
    if (c.window_number != null) stats.push(el("span", { class: "badge" }, "window " + c.window_number));
    if (c.replacement_items != null) {
      stats.push(el("span", { class: "badge" }, c.replacement_items + " replacement items"));
    }
    if (c.summary_encrypted) stats.push(el("span", { class: "badge" }, "summary encrypted"));
    if (stats.length) body.push(el("div", { class: "compact-stats" }, stats));
    if (ev.text && ev.text !== ev.subtype) {
      body.push(el("div", { class: "md", html: md(ev.text) }));
    } else if (c.summary_encrypted) {
      body.push(el("div", { class: "attach-meta" }, "Compaction summary is not readable from the transcript."));
    }
    if (ev.metadata) body.push(renderTurnMetadata(ev.metadata, "Compaction metadata"));
    return turnShell("system", "Compaction", ev, body);
  }
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
// An abandoned branch (a path you rewound away from), folded into a collapsed
// marker at the fork. Expands in place to show the rewound turns, de-emphasized.
function renderBranch(ev) {
  const groups = ev.groups || [];
  const count = ev.count || groups.reduce((n, g) => n + g.length, 0);
  const label =
    groups.length > 1
      ? `⑂ ${count} messages on ${groups.length} abandoned branches`
      : `⑂ ${count} message${count === 1 ? "" : "s"} on an abandoned branch`;
  const block = el("div", { class: "branch-block collapsed" });
  const head = toggleHead(block, "tool-head", el("span", { class: "tool-name" }, label));
  const body = el("div", { class: "tool-body" });
  groups.forEach((g, i) => {
    if (groups.length > 1) body.append(el("div", { class: "tool-section-label" }, "branch " + (i + 1)));
    for (const child of g) {
      const node = renderEvent(child);
      if (node) body.append(node);
    }
  });
  block.append(head, body);
  return block;
}

function collapsibleBlock(cls, label, bodyNodes) {
  const block = el("div", { class: cls });
  block.append(
    toggleHead(block, "tool-head", el("span", { class: "tool-name" }, label)),
    el("div", { class: "tool-body" }, ...bodyNodes)
  );
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
  return turnShell("tokens", "Usage", ev, [block]);
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
  const head = toggleHead(
    block,
    "tool-head",
    el("span", { class: "tool-icon" }, "🌐"),
    el("span", { class: "tool-name" }, "web_search"),
    queries.length > 1 ? el("span", { class: "status-tag" }, queries.length + " queries") : null,
    el("span", { class: "tool-summary", title: queries.join("  •  ") }, queries[0] || "(no query recorded)")
  );

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

// Caller pill text, or "" to hide it. Claude Code records `caller` either as a
// string or an object like {type:"direct"}; the normal direct/assistant call is
// noise, so we only surface a genuinely different caller (e.g. a sub-agent).
function callerLabel(caller) {
  const t = caller && typeof caller === "object" ? caller.type : caller;
  if (!t || t === "assistant" || t === "direct") return "";
  return String(t);
}

// ---------- tool rendering ----------
// The disclosure shell both tool renderers share: error styling, the icon/name
// head with per-source extras, and the body. What goes *in* the body differs
// per source and stays in the callers.
function toolShell(name, isErr, headExtras, bodyKids) {
  const block = el("div", { class: "tool-block collapsed" + (isErr ? " error" : "") });
  const head = toggleHead(
    block,
    "tool-head",
    el("span", { class: "tool-icon" }, isErr ? "✗" : "🔧"),
    el("span", { class: "tool-name" }, name),
    ...headExtras
  );
  block.append(head, el("div", { class: "tool-body" }, ...bodyKids));
  return block;
}

// Claude Code tool (input formatted client-side; result attached on the block).
function renderTool(b) {
  const name = b.name || "tool";
  const fmt = formatToolInput(name, b.input || {});
  const isErr = b.result && b.result.is_error;

  const bodyKids = [];
  bodyKids.push(el("div", { class: "tool-section-label" }, "Input"));
  bodyKids.push(fmt.inputNode);
  // opencode records which session a `task` call spawned, so the sub-agent's
  // own transcript is one click away rather than a hunt through the sidebar.
  // An export carries one session, so there is nothing on the other end of it.
  if (b.child_file && !STANDALONE) {
    bodyKids.push(
      el("a", {
        href: "#", class: "parent-link",
        onclick: (e) => { e.preventDefault(); openSession(b.child_file); },
      }, "→ open sub-agent session")
    );
  }
  if (b.instructions) {
    bodyKids.push(el("div", { class: "tool-section-label" }, "Skill instructions"));
    bodyKids.push(el("pre", { class: "payload truncatable" }, b.instructions));
  }
  if (b.result) {
    const imgs = b.result.images || [];
    if (b.result.missing && !b.result.text && !imgs.length) {
      bodyKids.push(el("div", { class: "tool-section-label muted" }, "No result recorded (CLI transcript)"));
    } else {
      const txt = b.result.text || (imgs.length ? "" : "(no output)");
      bodyKids.push(el("div", { class: "tool-section-label" }, isErr ? "Error" : "Result"));
      if (txt) bodyKids.push(el("pre", { class: "payload truncatable" + (isErr ? " result-error" : "") }, txt));
      for (const uri of imgs) bodyKids.push(el("img", { src: uri, class: "tool-image", loading: "lazy" }));
    }
  } else {
    bodyKids.push(el("div", { class: "tool-section-label muted" }, "No result recorded"));
  }

  return toolShell(name, isErr, [
    el("span", { class: "tool-summary", title: fmt.summary }, fmt.summary),
    callerLabel(b.caller) ? el("span", { class: "tool-caller" }, callerLabel(b.caller)) : null,
  ], bodyKids);
}

// Codex tool (top-level event; summary precomputed server-side, result is a dict).
function renderCodexTool(ev) {
  const name = ev.name || "tool";
  const isErr = ev.result && ev.result.is_error;

  const bodyKids = [];
  bodyKids.push(el("div", { class: "tool-section-label" }, "Input"));
  bodyKids.push(formatToolInput(name, ev.input).inputNode);
  if (ev.result) {
    bodyKids.push(el("div", { class: "tool-section-label" }, isErr ? "Error" : "Result"));
    const resultMeta = ev.result.metadata || {};
    const resultFacts = [
      resultMeta.exit_code != null ? "exit " + resultMeta.exit_code : "",
      resultMeta.wall_time_seconds != null ? fmtDuration(resultMeta.wall_time_seconds * 1000) : "",
      resultMeta.session_id != null ? "session " + resultMeta.session_id : "",
      resultMeta.chunk_id ? "chunk " + resultMeta.chunk_id : "",
      resultMeta.original_token_count ? resultMeta.original_token_count + " tokens" : "",
    ].filter(Boolean);
    if (resultFacts.length) {
      bodyKids.push(el("div", { class: "tool-result-meta" }, resultFacts.map((fact) => el("span", { class: "badge" }, fact))));
    }
    if (ev.result.output) {
      bodyKids.push(preFrom(ev.result.output, "payload truncatable" + (isErr ? " result-error" : "")));
    }
    for (const img of ev.result.images || []) bodyKids.push(renderImagePayload(img));
    if (ev.result.raw) {
      if (ev.result.raw.changes) bodyKids.push(renderChanges(ev.result.raw.changes));
      else bodyKids.push(preFrom(JSON.stringify(ev.result.raw, null, 2), "payload truncatable"));
    }
    if (!ev.result.output && !(ev.result.images || []).length && !ev.result.raw && !resultFacts.length) {
      bodyKids.push(preFrom("(no output)", "payload truncatable"));
    }
  } else {
    bodyKids.push(el("div", { class: "tool-section-label muted" }, "No result recorded"));
  }

  return toolShell(name, isErr, [
    ev.status ? el("span", { class: "status-tag" }, ev.status) : null,
    el("span", { class: "tool-summary", title: ev.summary || "" }, ev.summary || ""),
  ], bodyKids);
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
  if (n === "exec" && typeof value === "object" && value.code) {
    const calls = Array.isArray(value.calls) ? value.calls : [];
    const wrap = el("div", {});
    calls.forEach((call, i) => {
      const callName = call.name || "tool";
      wrap.append(el("div", { class: "tool-section-label" }, `call ${i + 1} · ${callName}`));
      if (call.input != null) wrap.append(formatToolInput(callName, call.input).inputNode);
      else wrap.append(el("div", { class: "attach-meta" }, "Arguments generated at runtime"));
    });
    const source = el(
      "details",
      { class: "orchestration-source" },
      el("summary", {}, "Orchestration source"),
      preFrom(value.code, "payload")
    );
    wrap.append(source);
    const summary = calls.map((call) => call.name).filter(Boolean).join(" · ") || "orchestration code";
    return { summary, inputNode: wrap };
  }
  if (n === "apply_patch") {
    const text = typeof value === "string" ? value : (value.raw || value.input || value.patch || JSON.stringify(value, null, 2));
    return { summary: "patch", inputNode: renderPatch(text) };
  }
  if ((n === "exec_command" || n === "shell") && typeof value === "object") {
    // Codex sends `cmd`; Cursor's Shell sends a `command` string plus a `description`.
    const cmd = value.cmd || (Array.isArray(value.command) ? value.command.join(" ") : value.command) || "";
    const node = el("div", {},
      value.description ? el("div", { class: "muted", style: "margin-bottom:4px" }, value.description) : null,
      value.workdir ? el("div", { class: "attach-meta", style: "margin-bottom:6px" }, "cwd: " + value.workdir) : null,
      preFrom(cmd, "payload"),
      value.yield_time_ms ? el("div", { class: "attach-meta", style: "margin-top:6px" }, "yield: " + value.yield_time_ms + "ms") : null
    );
    return { summary: firstLine(cmd), inputNode: node };
  }
  if (n === "write_stdin" && typeof value === "object") {
    const chars = value.chars || "";
    const meta = ["session: " + (value.session_id ?? "")];
    if (value.yield_time_ms) meta.push("wait: " + value.yield_time_ms + "ms");
    if (value.max_output_tokens) meta.push("max tokens: " + value.max_output_tokens);
    const node = el("div", {},
      el("div", { class: "attach-meta", style: "margin-bottom:6px" }, meta.join("  ·  ")),
      chars === "\u0003"
        ? el("div", { class: "attach-meta" }, "sent: Ctrl-C")
        : chars === "\u0004"
          ? el("div", { class: "attach-meta" }, "sent: Ctrl-D")
          : chars
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

  // ----- Cursor tools -----
  // (Cursor's other tools — terminal/read/edit/grep/glob — are normalized to the
  // canonical Claude Code names server-side and handled by those branches below.)
  if (n === "awaitshell") {
    const meta = [
      value.shell_id != null ? "shell: " + value.shell_id : "",
      value.pattern ? "until: " + value.pattern : "",
      value.block_until_ms ? "timeout: " + fmtDuration(value.block_until_ms) : "",
    ].filter(Boolean).join("  ·  ");
    return { summary: meta, inputNode: el("div", { class: "attach-meta" }, meta || "(polling shell)") };
  }
  if (n === "delete" && value.path) {
    return { summary: value.path, inputNode: el("div", { class: "attach-meta" }, "delete: " + value.path) };
  }
  if (n === "readlints") {
    const paths = value.paths || (value.path ? [value.path] : []);
    return { summary: paths.join(", "), inputNode: el("div", { class: "attach-meta" }, paths.join("\n") || "(workspace)") };
  }
  if (n === "semanticsearch") {
    const dirs = value.target_directories || [];
    const meta = [dirs.length ? "in " + dirs.join(", ") : "", value.num_results ? value.num_results + " results" : ""].filter(Boolean).join(", ");
    return { summary: value.query || "", inputNode: el("div", { class: "attach-meta" }, (value.query || "") + (meta ? "\n" + meta : "")) };
  }
  if (n === "websearch" && value.search_term) {
    return { summary: value.search_term, inputNode: el("div", { class: "attach-meta" }, value.search_term + (value.explanation ? "\n\n" + value.explanation : "")) };
  }

  // ----- Claude Code tools -----
  if (n === "bash") {
    const cmd = value.command || "";
    const node = el("div", {},
      value.description ? el("div", { class: "muted", style: "margin-bottom:4px" }, value.description) : null,
      // opencode's bash takes an explicit working directory instead of `cd`.
      value.workdir ? el("div", { class: "attach-meta", style: "margin-bottom:6px" }, "cwd: " + value.workdir) : null,
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
    // Cursor edits arrive as a precomputed unified-diff patch; Claude edits as
    // old/new strings. Render whichever is present.
    const body = value.patch != null
      ? renderPatch(value.patch)
      : diffNode(value.old_string, value.new_string);
    const node = el("div", {},
      el("div", { class: "attach-meta", style: "margin-bottom:6px" }, fp + (value.replace_all ? "  (replace all)" : "")),
      body
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

// Compact plain-text rendering of a tool call for "Copy all". Mirrors the
// per-tool branches in formatToolInput but emits minimal text instead of DOM,
// dropping redundant wrappers (e.g. Codex exec orchestration `code`) so the
// copied transcript uses as few tokens as possible.
function toolCallToText(name, input) {
  const n = (name || "").toLowerCase();
  const v = input == null ? {} : input;
  const out = [];

  if (n === "exec" && typeof v === "object") {
    const calls = Array.isArray(v.calls) ? v.calls : [];
    if (calls.length) {
      calls.forEach((call) => out.push(...toolCallToText(call.name, call.input)));
      return out;
    }
    if (v.code) { out.push("$ " + v.code); return out; }
  }
  if (n === "exec_command" || n === "bash") {
    const cmd = v.command || v.cmd || "";
    if (v.description) out.push("# " + v.description);
    if (v.workdir) out.push("(in " + v.workdir + ")");
    if (cmd) out.push("$ " + cmd);
    if (v.run_in_background) out.push("(background)");
    return out;
  }
  if (n === "read") {
    const extra = [v.offset ? "offset " + v.offset : "", v.limit ? "limit " + v.limit : "", v.pages ? "pages " + v.pages : ""].filter(Boolean).join(", ");
    out.push("read " + (v.file_path || "") + (extra ? "  (" + extra + ")" : ""));
    return out;
  }
  if (n === "edit") {
    out.push("edit " + (v.file_path || "") + (v.replace_all ? "  (replace all)" : ""));
    if (v.patch != null) out.push(String(v.patch));
    else {
      if (v.old_string != null) String(v.old_string).split("\n").forEach((l) => out.push("- " + l));
      if (v.new_string != null) String(v.new_string).split("\n").forEach((l) => out.push("+ " + l));
    }
    return out;
  }
  if (n === "multiedit") {
    out.push("multi-edit " + (v.file_path || "") + "  (" + (v.edits || []).length + " edits)");
    (v.edits || []).forEach((e) => {
      if (e.old_string != null) String(e.old_string).split("\n").forEach((l) => out.push("- " + l));
      if (e.new_string != null) String(e.new_string).split("\n").forEach((l) => out.push("+ " + l));
    });
    return out;
  }
  if (n === "write") {
    out.push("write " + (v.file_path || ""));
    if (v.content) out.push(String(v.content));
    return out;
  }
  if (n === "grep" || n === "glob") {
    const pat = v.pattern || v.query || "";
    const where = v.path || v.glob || "";
    out.push(n + " " + pat + (where ? "  in " + where : ""));
    return out;
  }
  if (n === "task" || n === "agent") {
    const sub = v.subagent_type || v.agentType || "";
    out.push("task" + (sub ? " [" + sub + "]" : "") + ": " + (v.description || ""));
    if (v.prompt) out.push(String(v.prompt));
    return out;
  }
  if (n === "todowrite") {
    (v.todos || []).forEach((td) => out.push("- [" + (td.status || "?") + "] " + (td.content || td.activeForm || "")));
    return out;
  }
  if (n === "webfetch") {
    out.push("webfetch " + (v.url || ""));
    if (v.prompt) out.push(String(v.prompt));
    return out;
  }
  if (n === "websearch") {
    out.push("websearch " + (v.query || v.search_term || ""));
    return out;
  }
  if (n === "delete" && v.path) { out.push("delete " + v.path); return out; }

  // Fallback: compact single-line JSON.
  out.push(n + " " + JSON.stringify(v));
  return out;
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

function markActive() {
  if (!CURRENT_FILE) return;
  const item = $(`.session-item[data-file="${cssEscape(CURRENT_FILE)}"]`);
  if (item) item.classList.add("active");
}

// Rebuild the sidebar from a fresh SESSIONS list without disturbing the
// reader: keep the sidebar's own scroll offset and re-mark the active session.
async function refreshSidebar() {
  const listEl = $("#session-list");
  const sTop = listEl.scrollTop;
  await runSearch($("#search").value); // re-run content search against the fresh set
  listEl.scrollTop = sTop;
  markActive();
}

// ---------- live polling (always on) ----------
// Real transcript files get a fast, tiny stat request. The full session list
// remains on a slower loop for sidebar changes and synthetic Cursor sessions.
function supportsFastTranscriptPoll(file) {
  return !!file && !SYNTHETIC_ID_RE.test(file);
}

let transcriptStatePolling = false;
async function pollOpenTranscript() {
  const file = CURRENT_FILE;
  if (!supportsFastTranscriptPoll(file) || transcriptStatePolling) return;
  transcriptStatePolling = true;
  try {
    const res = await fetch("/api/session-state?file=" + encodeURIComponent(file));
    const state = await res.json();
    if (
      CURRENT_FILE === file && state.supported &&
      (state.mtime || 0) > LAST_RENDERED_MTIME
    ) await refreshOpenTranscript(state.mtime);
  } catch (e) { /* transient; try again next poll */ }
  finally { transcriptStatePolling = false; }
}

let sidebarPolling = false;
async function pollSidebar() {
  if (sidebarPolling) return;
  sidebarPolling = true;
  try {
    let next;
    try {
      const res = await fetch("/api/sessions");
      next = (await res.json()).sessions || [];
    } catch (e) { return; } // server momentarily unreachable; retry next tick
    const sig = sessionsSignature(next);
    if (sig !== LAST_SIG) {
      LAST_SIG = sig;
      SESSIONS = next;
      // A session in a new project or on a new model must show up in the
      // Model/Directory dropdowns without a page reload.
      if (filterOptionsSignature() !== FILTER_OPTIONS_SIG) buildFilters();
      await refreshSidebar();
    }
    // Synthetic Cursor sessions have no standalone file to stat, so retain
    // mtime detection here. Title changes for every source also flow here.
    if (CURRENT_FILE) {
      const cur = next.find((s) => s.file === CURRENT_FILE);
      if (cur && (
        (!supportsFastTranscriptPoll(CURRENT_FILE) &&
          (cur.mtime || 0) > LAST_RENDERED_MTIME) ||
        (cur.title || "") !== LAST_RENDERED_TITLE
      )) await refreshOpenTranscript();
    }
  } finally {
    sidebarPolling = false;
  }
}
if (!STANDALONE) {
  setInterval(pollOpenTranscript, TRANSCRIPT_POLL_MS);
  setInterval(pollSidebar, SIDEBAR_POLL_MS);
}

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !STANDALONE && document.activeElement !== $("#search")) {
    e.preventDefault();
    $("#search").focus();
  }
  if (e.key === "Escape") {
    $$(".dropdown-panel").forEach((p) => p.classList.add("hidden"));
    closeThemeMenu();
    hideSessionContextMenu();
  }
});

// ---------- nav button + outline wiring ----------
$("#nav-prev").addEventListener("click", () => jumpUser(-1));
$("#nav-next").addEventListener("click", () => jumpUser(1));
$("#nav-end").addEventListener("click", scrollToEnd);
wirePanelToggles();

let outlineRaf = 0;
$("#main").addEventListener("scroll", () => {
  if (outlineRaf) return;
  outlineRaf = requestAnimationFrame(() => { outlineRaf = 0; highlightOutline(); });
});

// click anywhere outside an open dropdown closes it
document.addEventListener("click", () => {
  $$(".dropdown-panel").forEach((p) => p.classList.add("hidden"));
  closeThemeMenu();
  hideSessionContextMenu();
});
window.addEventListener("scroll", hideSessionContextMenu, true);

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

// A saved export already holds its one session, so there is nothing to fetch
// and nothing to list: render straight from the embedded payload.
function startStandalone() {
  document.body.classList.add("standalone");
  CURRENT_FILE = EXPORT_DATA.file || null;
  $("#welcome").hidden = true;
  $("#transcript").hidden = false;
  renderTranscript(EXPORT_DATA);
}

buildThemePicker();
if (STANDALONE) startStandalone();
else loadSessions().then(openFromHash);
