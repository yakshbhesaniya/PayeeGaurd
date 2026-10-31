"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (a, c) => (a == null ? "—" : `${c || ""} ${Number(a).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`.trim());
const STAMP = { PAY: "Cleared to pay", HOLD: "On hold", BLOCK: "Blocked", APPROVE: "Approved", REVIEW: "Review", REJECT: "Rejected" };
const STEPS = ["Reading the email and re-checking every field", "Resolving the vendor in Crustdata", "Checking the sender's domain and headers",
  "Looking up the signatory's employment", "Following the money through payment history", "Weighing evidence and naming the attack"];
const ONB_STEPS = ["Resolving the website and name in Crustdata", "Reading founders and executives", "Checking age, headcount and hiring", "Comparing claims with the record"];
const seen = {};
let samples = { emails: [], onboarding: [] };

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

/* ---------- tray ---------- */
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("on", p.id === `pane-${t.dataset.tab}`));
}));
$("#dateline").textContent = new Date().toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" });

// Badge text + colour from the case state the server remembers: PAY / HOLD / BLOCK / APPROVED / PAID.
function badge(m) {
  const st = seen[m.id] || m.state;
  if (!st) return null;
  return { RELEASED: ["PAID", "PAY"], APPROVED: ["APPROVED", "PAY"] }[st] || [st, st];
}

/* ---------- remember what was open, so a refresh restores it ---------- */
const store = {
  get() { try { return JSON.parse(localStorage.getItem("pg:last") || "null"); } catch { return null; } },
  set(v) { try { localStorage.setItem("pg:last", JSON.stringify(v)); } catch { /* private mode: fine */ } },
};

function renderInbox() {
  $("#inbox").innerHTML = samples.emails.map((m, i) => `
    <li data-i="${i}" tabindex="0" class="${badge(m) ? "tagged" : ""}">
      <span class="subj">${esc(m.subject)}</span><span class="amt">${esc(money(m.amount, m.currency))}</span>
      <span class="who">${esc(m.from_name ? m.from_name + " · " : "")}${esc(m.from || "no sender")}</span>
      ${badge(m) ? `<span class="tagd ${badge(m)[1]}">${badge(m)[0]}</span>` : ""}
    </li>`).join("");
  document.querySelectorAll("#inbox li").forEach((li) => {
    const go = () => openEmail(+li.dataset.i);
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => e.key === "Enter" && go());
  });
  $("#onbExamples").innerHTML = samples.onboarding.map((r, i) =>
    `<li data-i="${i}">${esc(r.vendor_name)}<small>${esc(r.website)}</small></li>`).join("");
  document.querySelectorAll("#onbExamples li").forEach((li) => li.addEventListener("click", () => {
    const r = samples.onboarding[+li.dataset.i], f = $("#onbForm");
    f.vendor_name.value = r.vendor_name; f.website.value = r.website;
    f.claimed_country.value = r.claimed_country || ""; f.claimed_headcount.value = r.claimed_headcount || "";
    f.claimed_offices.value = r.claimed_offices || "";
  }));
}

async function openEmail(i) {
  store.set({ kind: "email", id: samples.emails[i].id });
  link(samples.emails[i].id.slice(0, 2));
  document.querySelectorAll("#inbox li").forEach((li) => li.classList.toggle("on", +li.dataset.i === i));
  await investigate(samples.emails[i].raw, samples.emails[i].id);
}

$("#pasteGo").addEventListener("click", () => {
  const raw = $("#pasteBox").value.trim();
  if (raw) { store.set({ kind: "raw", raw }); link(""); selectNone(); investigate(raw, null); }
});

$("#onbForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target), body = Object.fromEntries(f.entries());
  ["claimed_headcount", "claimed_offices"].forEach((k) => (body[k] = body[k] ? +body[k] : null));
  store.set({ kind: "onboard", body });
  link(""); selectNone();
  running(ONB_STEPS, "Checking the vendor…");
  try { renderOnboarding(await api("/api/onboard", body), body); refreshStats(); } catch (err) { fail(err); }
});

$("#resetBtn").addEventListener("click", async () => {
  if (!confirm("Wipe the demo memory (cases, flags, payments) and reload the seeded history?")) return;
  try {
    await api("/api/reset", {});
    Object.keys(seen).forEach((k) => delete seen[k]);
    samples.emails.forEach((m) => { m.state = null; m.decision = null; });
    store.set(null);
    link("");
    renderInbox(); showEmpty(); refreshStats();
  } catch (err) { fail(err); }
});

/* ---------- case file ---------- */
let stepTimer;
function running(steps, title) {
  $("#empty").hidden = true;
  const v = $("#caseView"); v.hidden = false;
  v.innerHTML = `<div class="running"><p class="kicker">Investigating</p><h2>${esc(title)}</h2>
    <ol class="steps">${steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></div>`;
  const lis = [...v.querySelectorAll(".steps li")]; let k = 0;
  clearInterval(stepTimer);
  lis[0].classList.add("now");
  stepTimer = setInterval(() => {
    if (k < lis.length - 1) { lis[k].classList.replace("now", "done"); lis[++k].classList.add("now"); }
  }, 520);
}
function stopRunning() { clearInterval(stepTimer); }
function showEmpty() { $("#caseView").hidden = true; $("#empty").hidden = false; }
function fail(err) { stopRunning(); $("#caseView").innerHTML = `<p class="err">Something went wrong: ${esc(err.message)}</p>`; }

async function investigate(raw, id) {
  running(STEPS, "Following the money…");
  const t0 = Date.now();
  try {
    const data = await api("/api/verify", { raw_text: raw });
    await new Promise((r) => setTimeout(r, Math.max(0, 1600 - (Date.now() - t0))));   // let the steps read
    if (id) { seen[id] = data.verdict.decision; renderInbox(); document.querySelector(`#inbox li[data-i="${samples.emails.findIndex((m) => m.id === id)}"]`)?.classList.add("on"); }
    renderCase(data);
    refreshStats();
  } catch (err) { fail(err); }
}

function gauge(v) {
  const facts = v.facts || {};
  const f = (k, label) => `<div class="fact ${facts[k] ? "yes" : "no"}"><i>${facts[k] ? "✓" : "·"}</i>${label}</div>`;
  return `<div class="gauges">
    <div><div class="glabel"><span>Fraud risk</span><b>${v.verdict.risk_score}<small style="font-size:12px;color:var(--ink-3)">/100</small></b></div>
      <div class="ruler"><span class="zone h"></span><span class="zone b"></span><span class="needle" style="left:0%"></span></div>
      <div class="ticks"><span>0</span><span>25 hold</span><span>60 block</span><span>100</span></div></div>
    <div><div class="glabel"><span>Verified facts</span><b>${esc(v.verdict.confidence)}</b></div>
      <div class="facts">${f("who", "WHO")}${f("channel", "CHANNEL")}${f("money", "MONEY")}</div></div>
  </div>`;
}

function renderCase(data) {
  stopRunning();
  const v = data.verdict, inv = data.invoice || {}, d = v.decision;
  const hyp = v.hypothesis;
  const rows = [...v.evidence].sort((a, b) => ({ contradicted: 0, unchecked: 1, verified: 2 }[a.status] - { contradicted: 0, unchecked: 1, verified: 2 }[b.status]) || b.weight - a.weight);
  const html = `
  <div class="case-top">
    <div class="stampbox"><div class="stamp ${d}">${STAMP[d]}<small>case ${esc(v.case_id)}</small></div></div>
    <div class="meta">
      <p class="kicker">${esc(inv.invoice_number || "Invoice")} · ${esc(money(inv.amount, inv.currency))}${inv.bank_change ? " · bank change requested" : ""}</p>
      <h2>${esc(splitLabel(data.vendor_label || inv.vendor || "Unknown vendor")[0])}</h2>
      <div class="sub">${esc(splitLabel(data.vendor_label || "")[1])}${splitLabel(data.vendor_label || "")[1] ? " · " : ""}from ${esc(inv.sender || "no sender")}${inv.account ? ` → pays ${esc(inv.account)}` : ""}</div>
      ${gauge({ ...data, verdict: v })}
      <div class="rule-line">Decision rule: ${esc(v.decision_rule)}</div>
      ${data.banner ? `<div class="banner">${esc(data.banner)}</div>` : ""}
    </div>
  </div>

  <section class="sec"><h3><span>Investigation</span></h3>
    ${hyp ? `<p class="hyp-title">${esc(hyp.title)}<code>${esc(hyp.attack_type)}</code></p><p class="narr">${narr(hyp.narrative)}</p>`
          : `<p class="hyp-title">${d === "PAY" ? "No attack pattern detected" : "Nothing proves fraud, but key facts are unverified"}</p>
             <p class="narr">${d === "PAY" ? "Every critical fact checks out against Crustdata and your payment history." : "PayeeGuard never treats missing data as fraud. A human verifies the remaining facts before money moves."}</p>`}
    ${v.also_matched?.length ? `<p class="also">Also consistent with: ${v.also_matched.map((h) => esc(h.title)).join(", ")}</p>` : ""}
    ${v.ruled_out?.length ? `<div class="ruled">${v.ruled_out.map((r) => `<span title="${esc(r.because)} — ${esc(r.source)}"><s>${esc(r.attack_type.replace(/_/g, " "))}</s> <em>ruled out</em></span>`).join("")}</div>` : ""}
  </section>

  <section class="sec"><h3><span>Money trail</span><span>graph built from Crustdata + your payment history</span></h3>${trail(v.graph_edges)}</section>

  <section class="sec"><h3><span>Evidence ledger · ${rows.filter((e) => e.unchecked_reason !== "not_applicable").length} checks</span>
      <label class="toggle"><input type="checkbox" id="showVerified">show not-applicable checks too</label></h3>
    <div class="tablewrap"><table class="ledger"><thead><tr><th></th><th>Check</th><th>Finding</th><th>Points</th><th>Source</th></tr></thead>
    <tbody>${rows.map((e, i) => `<tr class="${e.status[0]}${e.unchecked_reason === "not_applicable" ? " na" : ""}" ${e.unchecked_reason === "not_applicable" ? "hidden" : ""} style="animation-delay:${i * 35}ms">
      <td class="mk">${e.status === "contradicted" ? "✕" : e.status === "verified" ? "✓" : "?"}</td>
      <td class="chk">${esc(e.check)}<span class="fam">${esc(e.family)}</span></td>
      <td class="det">${esc(e.detail)}${e.status === "unchecked" ? ` <span class="fam">unchecked: ${esc(e.unchecked_reason)}</span>` : ""}</td>
      <td class="pts">${e.status === "contradicted" ? (e.hard_block ? "HARD" : "+" + e.weight) : ""}</td>
      <td class="src">${esc(e.source)}</td></tr>`).join("")}</tbody></table></div>
  </section>

  <section class="sec"><h3><span>Finished work</span><span>${v.actions.length} documents</span></h3>
    <div class="docs"><ul class="doclist">${v.actions.map((a, i) => `<li data-i="${i}" class="${i ? "" : "on"}"><small>${esc(a.kind.replace(/_/g, " "))}</small>${esc(a.title)}</li>`).join("")}</ul>
    <div class="doc" id="docBody"></div></div>
  </section>

  <section class="sec"><h3><span>Payment gate</span><span class="statepill" id="statePill">${esc(data.state)}</span></h3>
    <div class="gatebar">
      <button class="btn ${d === "PAY" ? "ink" : ""}" id="payBtn">Release payment →</button>
      ${d === "HOLD" ? `<form class="form" id="resolveForm"><input name="reviewer" placeholder="Your name" required><input name="note" placeholder="e.g. Called the official number, vendor confirmed" required></form>
        <button class="btn" data-dec="approve">Approve after callback</button><button class="btn ghost" data-dec="reject">Reject</button>` : ""}
      ${d === "BLOCK" ? `<form class="form" id="resolveForm"><input name="reviewer" placeholder="Reviewer name" required><input name="note" placeholder="Why this block was wrong" required></form>
        <button class="btn ghost" data-dec="override">Override wrong block</button>` : ""}
    </div>
    <div id="tele"></div>
  </section>`;
  $("#caseView").innerHTML = html;
  $("#footMeta").textContent = `${v.crustdata_calls} live Crustdata calls · ${data.cache_hits} from cache · ≈${v.crustdata_credits} credits`;
  requestAnimationFrame(() => {
    const n = $(".needle"); if (n) setTimeout(() => (n.style.left = `calc(${Math.min(100, v.risk_score)}% - 1.5px)`), 250);
    if (d === "BLOCK") $(".stampbox").classList.add("shake");
  });
  $("#showVerified").addEventListener("change", (e) => document.querySelectorAll(".ledger tr.na").forEach((tr) => (tr.hidden = !e.target.checked)));
  document.querySelectorAll(".doclist li").forEach((li) => li.addEventListener("click", () => {
    document.querySelectorAll(".doclist li").forEach((x) => x.classList.toggle("on", x === li));
    showDoc(v.actions[+li.dataset.i], v);
  }));
  if (v.actions[0]) showDoc(v.actions[0], v);
  $("#payBtn").addEventListener("click", () => gateCall("/api/pay", { case_id: v.case_id }));
  document.querySelectorAll("[data-dec]").forEach((b) => b.addEventListener("click", () => {
    const f = $("#resolveForm");
    if (!f.reportValidity()) return;
    gateCall("/api/resolve", { case_id: v.case_id, decision: b.dataset.dec, reviewer: f.reviewer.value, note: f.note.value });
  }));
  if (matchMedia("(max-width: 900px)").matches) $("#file").scrollIntoView({ behavior: "smooth" });
  else window.scrollTo({ top: 0, behavior: "smooth" });
}

async function gateCall(path, body) {
  const tele = $("#tele");
  tele.innerHTML = `<div class="teleprinter">…</div>`;
  try {
    const r = await api(path, body);
    tele.innerHTML = `<div class="teleprinter ${r.ok ? "good" : "bad"}"></div>`;
    type($(".teleprinter", tele), r.message);
    if (r.state) $("#statePill").textContent = r.state;
    refreshStats();
  } catch (err) { tele.innerHTML = `<div class="teleprinter bad">${esc(err.message)}</div>`; }
}

function type(el, text) {
  let i = 0;
  const t = setInterval(() => { el.textContent = text.slice(0, (i += 3)); if (i >= text.length) clearInterval(t); }, 12);
}

const stripBanner = (s) => s.replace(/^> DRAFT FOR HUMAN RELEASE[^\n]*\n\n/, "");

/* Tiny, safe Markdown -> HTML for the finished-work documents. Everything is escaped FIRST, so text from an email
   can never inject markup; only our own patterns (headings, bold, code, lists, tables, citations) become HTML. */
function md(src) {
  const inline = (t) => esc(t)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[((?:graph|crustdata|extract|dns|rdap|policy|seed|ledger|vendor_master)[^\]]*)\]/g, '<span class="cite">$1</span>');
  const lines = stripBanner(String(src || "")).split("\n");
  let out = "", list = null;
  const close = () => { if (list) { out += `</${list}>`; list = null; } };
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (/^\s*\|/.test(l)) {                                   // table block
      close();
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) {
        if (!/^\s*\|[\s:|-]+\|\s*$/.test(lines[i])) rows.push(lines[i].trim().replace(/^\||\|$/g, "").split("|"));
        i++;
      }
      i--;
      out += `<table class="mdtable">${rows.map((r, k) => `<tr>${r.map((c) => `<${k ? "td" : "th"}>${inline(c.trim())}</${k ? "td" : "th"}>`).join("")}</tr>`).join("")}</table>`;
      continue;
    }
    let m;
    if ((m = l.match(/^#\s+(.*)/))) { close(); out += `<h4 class="mdh1">${inline(m[1])}</h4>`; }
    else if ((m = l.match(/^#{2,3}\s+(.*)/))) { close(); out += `<h5 class="mdh2">${inline(m[1])}</h5>`; }
    else if ((m = l.match(/^\s*[-•]\s+(.*)/))) { if (list !== "ul") { close(); out += "<ul>"; list = "ul"; } out += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^\s*\d+\.\s+(.*)/))) { if (list !== "ol") { close(); out += "<ol>"; list = "ol"; } out += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^>\s?(.*)/))) { close(); out += `<blockquote>${inline(m[1])}</blockquote>`; }
    else if (!l.trim()) { close(); }
    else { close(); out += `<p>${inline(l)}</p>`; }
  }
  close();
  return out;
}
// "Freshworks (crustdata_company_id 6042422)" -> ["Freshworks", "crustdata_company_id 6042422"]
const splitLabel = (s) => { const m = String(s).match(/^(.*?)\s*\((crustdata_company_id \d+)\)$/); return m ? [m[1], m[2]] : [String(s), ""]; };
const narr = (s) => esc(s).replace(/\[([^\]]+)\]/g, '<span class="src">[$1]</span>');

/* ---------- money trail (SVG) ---------- */
function trail(edges) {
  if (!edges?.length) return `<p class="hint">No graph data for this case.</p>`;
  const col = { sender: 0, invoice: 1, vendor: 2, account: 2 };
  const nodes = {};
  const place = (id, label, from) => {
    if (nodes[id]) return;
    const c = col[id] ?? 3;
    const taken = new Set(Object.values(nodes).filter((n) => n.c === c).map((n) => n.r));
    // vendor on row 0, account on row 1; end-column nodes sit on their source's row so edges don't cross
    let r = id === "vendor" ? 0 : id === "account" ? 1 : c === 3 && from ? from.r : 0;
    while (taken.has(r)) r++;
    nodes[id] = { id, label, c, r };
  };
  ["vendor", "account"].forEach((k) => { const e = edges.find((x) => x.dst === k); if (e) place(k, e.dst_label); });
  edges.forEach((e) => { place(e.src, e.src_label); place(e.dst, e.dst_label, nodes[e.src]); });
  const W = 1000, colX = [8, 262, 528, 800], boxW = 186, boxH = 50;
  const REL = { SENT: "sent", CLAIMS_VENDOR: "claims", PAYS_TO: "pays to", FLAGGED_IN: "flagged in" };
  edges = edges.map((e) => ({ ...e, rel: REL[e.rel] || e.rel.replace("PAID", "paid") }));
  const rows = Math.max(1, ...Object.values(nodes).map((n) => n.r + 1));
  const H = Math.max(170, rows * 92 + 30);
  const pos = (n) => ({ x: colX[n.c], y: 24 + n.r * 92 + (n.c <= 1 ? (rows - 1) * 46 : 0) });
  const color = { ok: "var(--ink)", bad: "var(--block)", warn: "var(--hold)", history: "var(--ink-3)", unknown: "var(--ink-3)" };
  let out = `<svg class="trail" viewBox="0 0 ${W} ${H}" role="img" aria-label="Money trail graph">
    <defs>${Object.entries(color).map(([k, c]) => `<marker id="ar-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${c}"/></marker>`).join("")}</defs>`;
  edges.forEach((e) => {
    const a = pos(nodes[e.src]), b = pos(nodes[e.dst]);
    const x1 = a.x + boxW, y1 = a.y + boxH / 2, x2 = b.x - 4, y2 = b.y + boxH / 2, mx = (x1 + x2) / 2;
    const dash = e.state === "bad" ? "7 5" : e.state === "history" ? "2 5" : e.state === "unknown" ? "4 4" : "";
    out += `<path d="M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}" fill="none" stroke="${color[e.state] || color.ok}" stroke-width="${e.state === "bad" ? 2.6 : 1.6}" ${dash ? `stroke-dasharray="${dash}"` : ""} marker-end="url(#ar-${e.state in color ? e.state : "ok"})"/>
      <rect x="${mx - e.rel.length * 3.7 - 5}" y="${(y1 + y2) / 2 - 9}" width="${e.rel.length * 7.4 + 10}" height="16" fill="#fbf9f4"/>
      <text x="${mx}" y="${(y1 + y2) / 2 + 3}" text-anchor="middle" font-size="10.5" fill="${color[e.state] || color.ok}" letter-spacing=".04em">${esc(e.rel)}</text>`;
  });
  const stateOf = (id) => (edges.find((e) => e.dst === id) || edges.find((e) => e.src === id) || {}).state;
  Object.values(nodes).forEach((n) => {
    const p = pos(n), st = n.id === "invoice" ? "ok" : stateOf(n.id), c = color[st] || color.ok;
    const fill = st === "bad" ? "rgba(216,53,42,.08)" : st === "warn" ? "rgba(217,149,17,.10)" : "#fbf9f4";
    const label = splitLabel(n.label || n.id)[0];
    out += `<g><rect x="${p.x}" y="${p.y}" width="${boxW}" height="${boxH}" fill="${fill}" stroke="${c}" stroke-width="${st === "bad" ? 2.4 : 1.4}"/>
      <text x="${p.x + 10}" y="${p.y + 17}" font-size="9.5" fill="var(--ink-3)" letter-spacing=".12em">${esc(n.id.replace(/\d+$/, "").replace("hist_", "paid acct ").replace("case", "case").toUpperCase())}</text>
      <text x="${p.x + 10}" y="${p.y + 35}" font-size="12.5" fill="var(--ink)">${esc(label.length > 27 ? label.slice(0, 26) + "…" : label)}</text></g>`;
  });
  return out + `</svg>`;
}

/* ---------- onboarding ---------- */
function renderOnboarding(r, req) {
  stopRunning();
  const d = r.decision;
  $("#caseView").innerHTML = `
  <div class="case-top">
    <div class="stampbox"><div class="stamp ${d}">${STAMP[d]}<small>vendor onboarding</small></div></div>
    <div class="meta"><p class="kicker">New vendor check · KYB</p><h2>${esc(req.vendor_name)}</h2>
      <div class="sub">${esc(req.website)}${r.cid ? ` · crustdata_company_id ${esc(r.cid)}` : ""}</div>
      ${r.contact ? `<div class="rule-line">Official contact on record: ${esc(r.contact.phone || "—")} · ${esc(r.contact.email || "—")}</div>` : ""}
    </div>
  </div>
  <section class="sec"><h3><span>What we found</span></h3>
    <table class="ledger onb-table"><tbody>${(r.rows || []).map(([q, a, s]) => `<tr class="v"><td>${esc(q)}</td><td class="det">${esc(a)}</td><td class="src">${esc(s)}</td></tr>`).join("")}</tbody></table>
  </section>
  ${r.leaders?.length ? `<section class="sec"><h3><span>Who runs it</span><span>founders and executives in Crustdata</span></h3>
    <div class="leaders">${r.leaders.map((l) => `<div class="leader"><b>${esc(l.name)}</b><span>${esc(l.title || "")}${l.since ? " · since " + esc(l.since) : ""}</span></div>`).join("")}</div></section>` : ""}
  <section class="sec"><h3><span>Anything off?</span></h3>
    ${r.reasons?.length ? `<ul class="offlist">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="narr">Nothing looks off. The vendor is added to your vendor master.</p>`}
  </section>`;
  if (d === "REJECT") $(".stampbox").classList.add("shake");
}

/* ---------- impact strip ---------- */
const usd = (n) => "$" + Math.round(n || 0).toLocaleString("en-US");
let lastStats = {};
async function refreshStats() {
  try {
    const s = await api("/api/stats");
    const set = (id, val) => { const el = $(id); if (el && el.textContent !== String(val)) { el.textContent = val; el.closest("b")?.classList.remove("bump"); void el.offsetWidth; el.closest("b")?.classList.add("bump"); } };
    set("#stStopped", usd(s.blocked_usd));
    $("#stStoppedSub").textContent = `blocked · ${usd(s.held_usd)} on hold`;
    set("#stCases", s.cases);
    set("#stPay", s.by_decision.PAY || 0); set("#stHold", s.by_decision.HOLD || 0); set("#stBlock", s.by_decision.BLOCK || 0);
    set("#stMs", s.avg_ms == null ? "—" : s.avg_ms < 1000 ? `${s.avg_ms} ms` : `${(s.avg_ms / 1000).toFixed(1)} s`);
    set("#stCredits", s.credits_balance == null ? "—" : Math.round(s.credits_balance).toLocaleString("en-US"));
    $("#stCreditsSub").textContent = s.credits_balance == null ? "credits: offline" : "credits left · live";
    $("#stCreditsSub").title = s.credits_balance == null ? "" : `${s.credits_spent ?? 0} credits spent this session`;
    lastStats = s;
  } catch { /* the strip is decorative; never block the desk */ }
}

/* ---------- vendor list screening ---------- */
async function screenList(csv) {
  running(["Resolving every vendor domain in Crustdata", "Enriching companies in batches of 25", "Cross-checking bank accounts across vendors", "Looking for duplicates and lookalike domains"], "Auditing the vendor list…");
  try {
    const r = await api("/api/screen", csv ? { csv } : {});
    stopRunning();
    if (r.error_msg) { $("#caseView").innerHTML = `<p class="err">${esc(r.error_msg)}</p>`; return; }
    const c = r.counts || {};
    $("#caseView").innerHTML = `
      <div class="case-top"><div class="stampbox"><div class="stamp ${c.ALERT ? "BLOCK" : c.REVIEW ? "HOLD" : "PAY"}">${c.ALERT ? "Alerts" : c.REVIEW ? "Review" : "Clean"}<small>vendor master audit</small></div></div>
        <div class="meta"><p class="kicker">Vendor master · ${r.total} vendors</p><h2>${c.ALERT || 0} alerts, ${c.REVIEW || 0} to review</h2>
          <div class="sub">≈${esc(r.credits_estimate)} Crustdata credits · cross-vendor patterns no single invoice check can see</div>
          <div class="vm-sum">${["ALERT", "REVIEW", "OK"].map((k) => `<span class="${k}">${c[k] || 0} ${k}</span>`).join("")}</div></div></div>
      <section class="sec"><h3><span>Findings</span><span>sorted by severity</span></h3>
        <div class="tablewrap"><table class="ledger"><thead><tr><th>Row</th><th>Vendor</th><th>Status</th><th>Findings and sources</th></tr></thead><tbody>
        ${[...r.vendors].sort((a, b) => ({ ALERT: 0, REVIEW: 1, OK: 2 }[a.status] - { ALERT: 0, REVIEW: 1, OK: 2 }[b.status]) || a.row - b.row).map((v, i) => `
          <tr style="animation-delay:${i * 40}ms"><td class="chk">${v.row}</td>
          <td class="det"><b>${esc(v.name)}</b><span class="fam">${esc(v.domain || "no domain")}${v.account ? " · " + esc(v.account) : ""}${v.company_id ? " · id " + esc(v.company_id) : ""}</span></td>
          <td><span class="vstat ${v.status}">${v.status}</span></td>
          <td><ul class="vfind">${v.findings.map((f) => `<li class="${f.level}">${esc(f.text)}<small>${esc(f.source)}</small></li>`).join("")}</ul></td></tr>`).join("")}
        </tbody></table></div>
        ${r.errors?.length ? `<p class="banner">Some lookups failed and were treated as unknown, not fraud: ${esc(r.errors.join("; "))}</p>` : ""}
      </section>`;
    refreshStats();
  } catch (err) { fail(err); }
}
$("#screenSample").addEventListener("click", () => { store.set({ kind: "vendors" }); link("vendors"); selectNone(); screenList(null); });
$("#screenCsv").addEventListener("click", () => { const t = $("#csvBox").value.trim(); if (t) screenList(t); });

refreshStats();

api("/api/samples").then((s) => {
  samples = s; renderInbox();
  // Deep link for demos: #06 opens the inbox email whose id starts with "06".
  const h = decodeURIComponent(location.hash.slice(1));
  const i = h ? samples.emails.findIndex((m) => m.id.startsWith(h)) : -1;
  if (i >= 0) openEmail(i);
  if (!h) offerResume();
  if (h === "memory") { document.querySelector('.tab[data-tab="memory"]').click(); }
  if (h === "vendors") { document.querySelector('.tab[data-tab="master"]').click(); screenList(null); }
  const o = h.match(/^onb(\d)$/);   // #onb1 runs onboarding example 1
  if (o && samples.onboarding[+o[1] - 1]) {
    document.querySelector('.tab[data-tab="onboard"]').click();
    document.querySelectorAll("#onbExamples li")[+o[1] - 1].click();
    $("#onbForm").requestSubmit();
  }
}).catch(fail);


function restoreLast() {
  const last = store.get();
  if (!last) return;
  if (last.kind === "email") { const i = samples.emails.findIndex((m) => m.id === last.id); if (i >= 0) openEmail(i); }
  else if (last.kind === "raw") { document.querySelector('.tab[data-tab="paste"]').click(); $("#pasteBox").value = last.raw; investigate(last.raw, null); }
  else if (last.kind === "vendors") { document.querySelector('.tab[data-tab="master"]').click(); screenList(null); }
  else if (last.kind === "memory") { document.querySelector('.tab[data-tab="memory"]').click(); }
  else if (last.kind === "onboard" && last.body) {
    document.querySelector('.tab[data-tab="onboard"]').click();
    const f = $("#onbForm"); Object.entries(last.body).forEach(([k, val]) => { if (f[k]) f[k].value = val ?? ""; });
    f.requestSubmit();
  }
}

/* ---------- Memory: what the agent remembers (persistent graph) ---------- */
async function openMemory() {
  store.set({ kind: "memory" });
  link("memory"); selectNone();
  $("#empty").hidden = true;
  const v = $("#caseView"); v.hidden = false;
  try {
    const m = await api("/api/memory");
    stopRunning();
    const t = m.totals;
    v.innerHTML = `
      <div class="case-top"><div class="stampbox"><div class="stamp PAY">Memory<small>persistent payment graph</small></div></div>
        <div class="meta"><p class="kicker">What PayeeGuard remembers</p><h2>${t.vendors} vendors · ${t.flagged} flagged accounts · ${t.cases} cases</h2>
          <div class="sub">Every verdict, payment, approval and attack is kept across refreshes and restarts. The next invoice is
            checked against all of it: that is how a perfect-looking email from a real vendor still gets blocked.</div></div></div>
      <section class="sec"><h3><span>Accounts tied to blocked attacks</span><span>any future invoice paying these is hard-blocked</span></h3>
        ${m.flagged.length ? `<div class="tablewrap"><table class="ledger"><thead><tr><th>Account</th><th>Attack</th><th>Claimed vendor</th><th>Case</th></tr></thead><tbody>
        ${m.flagged.map((f) => `<tr class="c"><td class="chk">${esc(f.account)}</td><td class="det">${esc((f.attack || "").replace(/_/g, " "))}</td><td>${esc(f.claimed_vendor || "—")}</td><td class="src">${esc(f.case_id)} · ${esc((f.at || "").slice(0, 16))}</td></tr>`).join("")}
        </tbody></table></div>` : `<p class="hint">No attacks remembered yet. Open sample 05 to see the agent learn one.</p>`}</section>
      <section class="sec"><h3><span>Vendors and their payment history</span><span>new accounts on these vendors are never auto-paid</span></h3>
        <div class="tablewrap"><table class="ledger"><thead><tr><th>Vendor</th><th>Payments</th><th>Accounts paid</th><th>Last paid</th></tr></thead><tbody>
        ${m.vendors.map((x) => `<tr class="v"><td class="det"><b>${esc(x.name)}</b><span class="fam">${esc(x.node)}</span></td><td class="chk">${x.payments}</td><td class="chk">${esc(x.accounts.join(", "))}</td><td class="src">${esc(x.last_paid || "—")}</td></tr>`).join("")}
        </tbody></table></div></section>
      ${m.onboarded.length ? `<section class="sec"><h3><span>Onboarded vendors</span></h3><ul class="offlist">${m.onboarded.map((o) => `<li>${esc(o.domain)} <span class="fam">${esc(o.node)} · ${esc((o.at || "").slice(0, 16))}</span></li>`).join("")}</ul></section>` : ""}
      <section class="sec"><h3><span>Recent cases</span></h3>
        <div class="tablewrap"><table class="ledger"><thead><tr><th>Case</th><th>Vendor</th><th>Amount</th><th>Decision</th><th>State</th><th>Attack</th></tr></thead><tbody>
        ${m.cases.map((c) => `<tr><td class="chk">${esc(c.case_id)}</td><td>${esc(c.vendor || "—")}</td><td class="chk">${esc(money(c.amount, c.currency))}</td><td><span class="vstat ${c.decision === "PAY" ? "OK" : c.decision === "HOLD" ? "REVIEW" : "ALERT"}">${esc(c.decision)}</span></td><td class="chk">${esc(c.state)}</td><td class="src">${esc((c.attack || "none").replace(/_/g, " "))}</td></tr>`).join("") || `<tr><td colspan="6" class="hint">No cases yet.</td></tr>`}
        </tbody></table></div></section>`;
  } catch (err) { fail(err); }
}
document.querySelector('.tab[data-tab="memory"]')?.addEventListener("click", openMemory);

/* ---------- finished work: real actions a human takes ---------- */
function docTools(a, v) {
  const tools = [];
  const body = stripBanner(a.body || "");
  if (a.kind === "verification_email_draft") {
    const to = (body.match(/^To:\s*(.+)$/m) || [])[1] || "";
    const subj = (body.match(/^Subject:\s*(.+)$/m) || [])[1] || "Payment verification";
    const text = body.replace(/^To:.*\n?/m, "").replace(/^Subject:.*\n?/m, "").trim();
    const addr = /@/.test(to) ? to.trim() : "";
    tools.push(`<a class="btn ink" href="mailto:${encodeURIComponent(addr)}?subject=${encodeURIComponent(subj)}&body=${encodeURIComponent(text)}">Open in my mail app</a>`);
  }
  const phone = ((v.callback || "").match(/\+?\d[\d\s().-]{7,}\d/) || [])[0];
  if ((a.kind === "callback_script" || a.kind === "exception_ticket") && phone) {
    tools.push(`<a class="btn" href="tel:${phone.replace(/[^\d+]/g, "")}">Call ${esc(phone)} (official number)</a>`);
  }
  if (INTEG.email && a.kind === "verification_email_draft") {
    tools.unshift(`<button class="btn red" data-send="${esc(a.kind)}">Send now (live)</button>`);
  }
  tools.push(`<button class="btn ghost" data-download="1">Download .md</button>`);
  return `<div class="doctools">${tools.join("")}</div>`;
}
const DOC_LABEL = { payment_instruction: "READY TO RELEASE", verification_email_draft: "DRAFT · YOU SEND",
  callback_script: "CALL SCRIPT", exception_ticket: "FOR REVIEW", fraud_case_file: "CASE FILE",
  do_not_pay_notice: "DO NOT PAY", kyb_onboarding_task: "KYB TASK", watcher: "MONITORING" };
function showDoc(a, v) {
  const el = $("#docBody");
  if (!el || !a) return;
  el.dataset.label = DOC_LABEL[a.kind] || "DOCUMENT";
  el.innerHTML = docTools(a, v) + md(a.body || "");
  el.querySelector("[data-send]")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget; btn.disabled = true; btn.textContent = "Sending…";
    try {
      const r = await api("/api/send", { case_id: v.case_id, kind: a.kind });
      btn.textContent = r.ok ? "Sent ✓" : "Not sent";
      const note = document.createElement("p"); note.className = `teleprinter ${r.ok ? "good" : "bad"}`; note.textContent = r.message;
      el.querySelector(".doctools").after(note);
    } catch (err) { btn.textContent = "Not sent"; }
  });
  el.querySelector("[data-download]")?.addEventListener("click", () => {
    const blob = new Blob([stripBanner(a.body || "")], { type: "text/markdown" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob); link.download = `${v.case_id}_${a.kind}.md`; link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });
}

$("#memRefresh")?.addEventListener("click", openMemory);


/* ---------- link the sidebar, the case file and the URL ---------- */
function link(hash) {
  const url = location.pathname + location.search + (hash ? "#" + hash : "");
  if (location.hash.slice(1) !== hash) history.pushState(null, "", url);
}
function selectNone() { document.querySelectorAll("#inbox li").forEach((li) => li.classList.remove("on")); }
function showTab(name) { document.querySelector(`.tab[data-tab="${name}"]`)?.click(); }

function openFromHash() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (!h) { selectNone(); showEmpty(); return; }
  const i = samples.emails.findIndex((m) => m.id.startsWith(h));
  if (i >= 0) { showTab("inbox"); openEmail(i); return; }
  if (h === "vendors") { showTab("master"); screenList(null); return; }
  if (h === "memory") { showTab("memory"); return; }
}
window.addEventListener("popstate", openFromHash);       // Back / Forward re-open the linked view

function offerResume() {
  const last = store.get();
  if (!last) return;
  const label = last.kind === "email" ? (samples.emails.find((m) => m.id === last.id)?.subject || "last case")
    : { raw: "the pasted email", vendors: "the vendor list audit", memory: "the memory view", onboard: "the vendor check" }[last.kind];
  if (!label) return;
  const box = document.createElement("p");
  box.className = "resume";
  box.innerHTML = `<button class="btn ghost" id="resumeBtn">Resume: ${esc(label)}</button>`;
  $("#empty").appendChild(box);
  $("#resumeBtn").addEventListener("click", () => { box.remove(); restoreLast(); });
}


/* ---------- live connections: status lights + real-time mailbox ---------- */
let INTEG = {};
async function loadIntegrations() {
  try { INTEG = await api("/api/integrations"); } catch { INTEG = {}; }
  const items = [["crustdata", "Crustdata"], ["inbox", "Inbox"], ["email", "Email"], ["payments", INTEG.gateway ? `Payments · ${INTEG.gateway} test` : "Payments"], ["alerts", "Alerts"]];
  $("#conn").innerHTML = items.map(([k, label]) => `<span class="dot ${INTEG[k] ? "on" : "off"}" title="${INTEG[k] ? "live" : "not connected: demo mode"}"><i></i>${esc(label)}</span>`).join("");
  if (INTEG.inbox) { pollLive(); setInterval(pollLive, 15000); }
}
async function pollLive() {
  try {
    const r = await api("/api/live-inbox");
    const box = $("#liveBox");
    box.innerHTML = `<p class="hint small live-h"><i></i>Live mailbox · ${esc(r.inbox_user || "")}${r.error ? ` · <span class="err-inline">${esc(r.error)}</span>` : ""}</p>
      ${r.items.length ? `<ol class="inbox live-list">${r.items.map((m, k) => `<li data-k="${k}" class="${m.state || m.decision ? "tagged" : ""}">
        <span class="subj">${esc(m.subject || "(no subject)")}</span><span class="amt">${esc(m.received || "")}</span>
        <span class="who">${esc(m.from)}</span>
        ${m.state || m.decision ? `<span class="tagd ${({ RELEASED: "PAY", APPROVED: "PAY" })[m.state] || m.state || m.decision}">${esc(({ RELEASED: "PAID" })[m.state] || m.state || m.decision)}</span>` : ""}</li>`).join("")}</ol>`
        : `<p class="hint">Waiting for mail. Forward any invoice or bank-change email to ${esc(r.inbox_user || "the mailbox")}.</p>`}`;
    box.querySelectorAll("li").forEach((li) => li.addEventListener("click", () => {
      const m = r.items[+li.dataset.k];
      document.querySelectorAll("#inbox li, .live-list li").forEach((x) => x.classList.toggle("on", x === li));
      link(""); investigate(m.raw, null);
    }));
  } catch { /* keep the last list */ }
}
loadIntegrations();
