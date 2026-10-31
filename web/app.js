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

function renderInbox() {
  $("#inbox").innerHTML = samples.emails.map((m, i) => `
    <li data-i="${i}" tabindex="0" class="${seen[m.id] ? "tagged" : ""}">
      <span class="subj">${esc(m.subject)}</span><span class="amt">${esc(money(m.amount, m.currency))}</span>
      <span class="who">${esc(m.from_name ? m.from_name + " · " : "")}${esc(m.from || "no sender")}</span>
      ${seen[m.id] ? `<span class="tagd ${seen[m.id]}">${seen[m.id]}</span>` : ""}
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
  document.querySelectorAll("#inbox li").forEach((li) => li.classList.toggle("on", +li.dataset.i === i));
  await investigate(samples.emails[i].raw, samples.emails[i].id);
}

$("#pasteGo").addEventListener("click", () => {
  const raw = $("#pasteBox").value.trim();
  if (raw) investigate(raw, null);
});

$("#onbForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target), body = Object.fromEntries(f.entries());
  ["claimed_headcount", "claimed_offices"].forEach((k) => (body[k] = body[k] ? +body[k] : null));
  running(ONB_STEPS, "Checking the vendor…");
  try { renderOnboarding(await api("/api/onboard", body), body); } catch (err) { fail(err); }
});

$("#resetBtn").addEventListener("click", async () => {
  if (!confirm("Wipe the demo memory (cases, flags, payments) and reload the seeded history?")) return;
  try {
    await api("/api/reset", {});
    Object.keys(seen).forEach((k) => delete seen[k]);
    renderInbox(); showEmpty();
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
    <div class="doc" id="docBody">${esc(stripBanner(v.actions[0]?.body || ""))}</div></div>
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
    $("#docBody").textContent = stripBanner(v.actions[+li.dataset.i].body);
  }));
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
  } catch (err) { tele.innerHTML = `<div class="teleprinter bad">${esc(err.message)}</div>`; }
}

function type(el, text) {
  let i = 0;
  const t = setInterval(() => { el.textContent = text.slice(0, (i += 3)); if (i >= text.length) clearInterval(t); }, 12);
}

const stripBanner = (s) => s.replace(/^> SIMULATED \/ DRAFT[^\n]*\n\n/, "");
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

api("/api/samples").then((s) => {
  samples = s; renderInbox();
  // Deep link for demos: #06 opens the inbox email whose id starts with "06".
  const h = decodeURIComponent(location.hash.slice(1));
  const i = h ? samples.emails.findIndex((m) => m.id.startsWith(h)) : -1;
  if (i >= 0) openEmail(i);
  const o = h.match(/^onb(\d)$/);   // #onb1 runs onboarding example 1
  if (o && samples.onboarding[+o[1] - 1]) {
    document.querySelector('.tab[data-tab="onboard"]').click();
    document.querySelectorAll("#onbExamples li")[+o[1] - 1].click();
    $("#onbForm").requestSubmit();
  }
}).catch(fail);
