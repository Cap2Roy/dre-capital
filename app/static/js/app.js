/* DRE-Capital frontend logic v2 — shared API helpers + UI utilities */

/** Error for a failed request: "<status> <message>" — FastAPI's `detail` text when present
 *  (validation errors joined), else the raw body. Callers strip the leading status code. */
async function apiError(r) {
  const text = await r.text();
  let msg = text;
  try {
    const d = JSON.parse(text).detail;
    if (typeof d === "string") msg = d;
    else if (Array.isArray(d)) msg = d.map(e => (e && e.msg) || JSON.stringify(e)).join("; ");
  } catch (_) { /* not JSON — keep the body */ }
  return new Error(`${r.status} ${msg}`);
}

const API = {
  async get(path) {
    const r = await fetch(path);
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw await apiError(r);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw await apiError(r);
    return r.json();
  },
  async patch(path, body) {
    const r = await fetch(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw await apiError(r);
    return r.json();
  },
  async put(path, body) {
    const r = await fetch(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw await apiError(r);
    return r.json();
  },
  async delete(path) {
    const r = await fetch(path, { method: "DELETE" });
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw await apiError(r);
    return r.json();
  },
  async upload(path, formData) {
    const r = await fetch(path, { method: "POST", body: formData });
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw await apiError(r);
    return r.json();
  },
};

/** Escape any API-provided string before it goes into innerHTML / template literals.
 *  Never put user data inside inline onclick='...' strings — pass IDs only. */
function escHtml(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
/** "City, ST" without "null, null" when parts are missing. */
function fmtCityState(city, state) {
  return [city, state].filter(Boolean).map(escHtml).join(", ");
}

function fmtMoney(n) {
  if (n == null) return "—";
  return "$" + Math.round(n).toLocaleString("en-US");
}
function fmtMoneyK(n) {
  if (n == null) return "—";
  if (Math.abs(n) >= 1000) return "$" + (n / 1000).toFixed(0) + "k";
  return "$" + n.toFixed(0);
}
// API datetimes are UTC ("...Z"). Defensively treat an offset-less ISO
// datetime as UTC too (older data), and bare dates ("YYYY-MM-DD") as local days.
function parseApiDate(iso) {
  if (iso instanceof Date) return iso;
  const s = String(iso);
  if (/T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) return new Date(s + "Z");
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return new Date(s + "T00:00:00");
  return new Date(s);
}
function fmtDate(iso) {
  if (!iso) return "—";
  const d = parseApiDate(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = parseApiDate(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " " + d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}
function fmtPhone(num) {
  if (!num) return "—";
  const m = num.replace(/^\+1/, "").match(/^(\d{3})(\d{3})(\d{4})$/);
  return m ? `(${m[1]}) ${m[2]}-${m[3]}` : num;
}
function daysFromNow(iso) {
  if (!iso) return null;
  return Math.ceil((parseApiDate(iso) - new Date()) / 86400000);
}
// <input type="datetime-local"> holds a local wall-clock value with no zone.
// Prefill from an API (UTC) datetime, and send back as UTC ISO with 'Z'.
function toDatetimeLocalValue(iso) {
  if (!iso) return "";
  const d = parseApiDate(iso);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fromDatetimeLocalValue(value) {
  return value ? new Date(value).toISOString() : null;
}

function stackBadge(depth) {
  const cls = depth >= 3 ? "stack-3" : depth === 2 ? "stack-2" : "stack-1";
  return `<span class="stack-badge ${cls}">${depth}</span>`;
}
function tierLabel(depth) {
  if (depth >= 3) return `<span class="tier-label tier-today">CALL TODAY</span>`;
  if (depth === 2) return `<span class="tier-label tier-this-week">THIS WEEK</span>`;
  return `<span class="tier-label tier-mail">MAIL ONLY</span>`;
}
function statusBadge(status) {
  const map = {
    new: ["badge-info", "New"], working: ["badge-warning", "Working"],
    warm: ["badge-warning", "Warm"], under_contract: ["badge-success", "Under Contract"],
    assigned: ["badge-success", "Assigned"], closed: ["badge-success", "Closed"],
    dead: ["badge-danger", "Dead"],
    pending: ["badge-warning", "Pending"], signed: ["badge-success", "Signed"],
    fallen: ["badge-danger", "Fallen Through"],
  };
  const [cls, label] = map[status] || ["badge-neutral", escHtml(status)];
  return `<span class="badge ${cls}">${label}</span>`;
}

function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function openModal(title, bodyHtml, footerHtml = "", opts = {}) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal${opts.wide ? " modal-wide" : ""}">
      <div class="modal-header">
        <h3>${title}</h3>
        <button class="modal-close" onclick="this.closest('.modal-overlay').remove()">×</button>
      </div>
      <div class="modal-body">${bodyHtml}</div>
      ${footerHtml ? `<div class="modal-footer">${footerHtml}</div>` : ""}
    </div>`;
  document.body.appendChild(overlay);
  // Wide modals hold in-progress call notes — don't drop them on a stray backdrop click.
  if (!opts.wide) overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  return overlay;
}

function closeModal() {
  const m = document.querySelector(".modal-overlay");
  if (m) m.remove();
}

async function doLogout() {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/login";
}

// Active nav highlight + call-queue count badge on every page
document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page) {
    document.querySelectorAll(".sidebar-nav a").forEach(a => {
      if (a.dataset.page === page) a.classList.add("active");
    });
  }
  const badge = document.getElementById("nav-queue-count");
  if (badge && page !== "login") {
    fetch("/api/dashboard/queue").then(r => r.ok ? r.json() : null)
      .then(q => { if (q) badge.textContent = q.count; }).catch(() => {});
  }
});

// ── Property brief (value, tax, debt, MLS, photo) + number checks ──────────
// Shared by lead detail, the guided-call screen, and AI Test Call.
const LIST_LABELS = {
  pre_foreclosure: "Pre-foreclosure", tax_delinquent: "Tax delinquent", probate: "Probate", divorce: "Divorce",
  absentee_owner: "Absentee", tired_landlord: "Tired landlord", vacant_code_violation: "Vacant / code",
  government_sale: "Gov. sale / REO",
};

function renderPropertyBrief(b, opts = {}) {
  const v = b.value || {}, t = b.tax || {}, d = b.debt || {}, l = b.listing;
  const range = v.low && v.high ? `<div class="pb-sub">${fmtMoneyK(v.low)} – ${fmtMoneyK(v.high)}</div>` : "";
  const listingTile = l
    ? `<div class="pb-tile ${String(l.status).toLowerCase() === "active" ? "pb-warn" : ""}"><div class="pb-k">MLS</div>
         <div class="pb-v">${escHtml(l.status)}${l.price ? " · " + fmtMoneyK(l.price) : ""}</div>
         <div class="pb-sub">${l.days_on_market != null ? l.days_on_market + " days · " : ""}${escHtml(l.mls || "")}</div></div>`
    : `<div class="pb-tile"><div class="pb-k">MLS</div><div class="pb-v">${b.data && b.data.fetched_at ? "Not listed" : "—"}</div></div>`;
  const debtRows = (d.items || []).map(i => `<li class="${i.certain ? "" : "pb-est"}">${i.amount != null ? `<strong>${fmtMoney(i.amount)}</strong> · ` : ""}${escHtml(i.label)}</li>`).join("");
  const ownerRec = (b.owner_of_record || []).length
    ? `Owner of record: <strong>${escHtml(b.owner_of_record.join(", "))}</strong> ${b.owner_match === true ? '<span class="badge badge-success">matches lead</span>' : b.owner_match === false ? '<span class="badge badge-danger">different owner — may have sold</span>' : ""}`
    : "";
  const agent = l && l.agent ? `Listing agent: ${escHtml(l.agent.name || "")}${l.agent.phone ? " · " + escHtml(fmtPhone(l.agent.phone)) : ""}${l.agent.office ? " · " + escHtml(l.agent.office) : ""}` : "";
  const photo = b.photo && b.photo.configured
    ? `<img class="pb-img" src="${b.photo.url}" alt="Photo of ${escHtml(b.address)}" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'pb-noimg',textContent:'No photo'}))">`
    : `<div class="pb-noimg">No photo<br><span class="text-xs">Add a Google Maps key in Settings</span></div>`;
  const fetched = b.data && b.data.fetched_at
    ? `Data: ${escHtml(b.data.source || "")} · ${fmtDateTime(b.data.fetched_at)}${b.data.error ? ` · <span class="text-danger">${escHtml(b.data.error)}</span>` : ""}`
    : "No property data pulled yet.";
  return `<div class="pb ${opts.compact ? "pb-compact" : ""}">
    <div class="pb-photo">${photo}</div>
    <div class="pb-main">
      ${opts.compact ? `<div class="pb-addr">${escHtml(b.address)}<span class="text-muted"> · ${escHtml(b.owner || "")}</span></div>` : ""}
      <div class="pb-grid">
        <div class="pb-tile"><div class="pb-k">Est. value</div><div class="pb-v">${fmtMoneyK(v.estimate)}</div>${range}</div>
        <div class="pb-tile"><div class="pb-k">Assessed</div><div class="pb-v">${fmtMoneyK(v.assessed)}</div></div>
        <div class="pb-tile"><div class="pb-k">Property tax${t.year ? " " + t.year : ""}</div><div class="pb-v">${t.annual ? fmtMoney(t.annual) + "/yr" : "—"}</div></div>
        <div class="pb-tile ${t.owed ? "pb-bad" : ""}"><div class="pb-k">Taxes owed</div><div class="pb-v">${t.owed ? fmtMoney(t.owed) : "—"}</div></div>
        <div class="pb-tile"><div class="pb-k">Est. equity</div><div class="pb-v">${fmtMoneyK(d.est_equity)}</div></div>
        ${listingTile}
      </div>
      ${debtRows ? `<div class="pb-sec"><span class="pb-k">Debt</span><ul class="pb-debt">${debtRows}</ul></div>` : `<div class="pb-sec text-sm text-muted">No debt on record${b.data && b.data.fetched_at ? "" : " (pull property data)"}.</div>`}
      ${(b.lists || []).length ? `<div class="pb-sec">${b.lists.map(x => `<span class="badge badge-neutral">${escHtml(LIST_LABELS[x] || x)}</span>`).join(" ")}</div>` : ""}
      ${ownerRec ? `<div class="pb-sec text-sm">${ownerRec}</div>` : ""}
      ${agent ? `<div class="pb-sec text-sm">${agent}</div>` : ""}
      <div class="pb-sec text-xs text-muted">${fetched}
        ${opts.refreshable ? ` <button class="btn btn-outline btn-sm" style="margin-left:6px" onclick="refreshPropertyData('${b.lead_id}', this)">↻ Refresh property data</button>` : ""}</div>
    </div>
  </div>`;
}

function renderNumberCheck(v, opts = {}) {
  const icon = { pass: "✓", warn: "⚠", fail: "✗" };
  // For a person dialing, consent/AI-only gates don't apply — show whether to dial at all.
  const HARD = ["belongs", "dnc", "opt_out", "wrong_number", "hours", "owner_record"];
  const humanFails = v.fails.filter(k => HARD.includes(k));
  const verdict = opts.human
    ? (humanFails.length ? '<span class="badge badge-danger">✗ Check before dialing</span>'
      : v.verified ? '<span class="badge badge-success">✓ Verified number</span>'
      : '<span class="badge badge-warning">⚠ Unverified — confirm the owner first</span>')
    : v.ai_allowed ? '<span class="badge badge-success">✓ Verified — AI may call</span>'
    : v.verified ? '<span class="badge badge-success">✓ Verified number</span>'
    : v.fails.length ? '<span class="badge badge-danger">✗ Don\'t call with AI</span>'
    : '<span class="badge badge-warning">⚠ Not verified</span>';
  const rows = v.checks.filter(c => !(opts.human && c.key === "consent"))
    .filter(c => opts.all || c.status !== "pass" || ["confirmed", "owner_record"].includes(c.key))
    .map(c => `<li class="nc-${c.status}"><span class="nc-i">${icon[c.status]}</span>${escHtml(c.label)}${c.detail ? ` <span class="text-muted">— ${escHtml(c.detail)}</span>` : ""}</li>`).join("");
  return `<div class="nc"><div class="nc-head"><span class="col-mono">${escHtml(fmtPhone(v.number))}</span> ${verdict}</div><ul class="nc-list">${rows}</ul></div>`;
}

async function refreshPropertyData(leadId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Pulling data…"; }
  try {
    await API.post(`/api/leads/${leadId}/property-data`);
    toast("Property data updated", "success");
    if (typeof onBriefRefreshed === "function") onBriefRefreshed(leadId);
  } catch (e) {
    toast("Couldn't refresh: " + e.message.replace(/^\d+\s*/, ""), "error");
    if (btn) { btn.disabled = false; btn.textContent = "↻ Refresh property data"; }
  }
}

// ── Properties: edit / archive / delete (shared by the Properties list and property page) ──
function editPropertyModal(p, onSaved) {
  const f = (id, label, value, type = "text", extra = "") => `<div class="form-group"><label class="form-label">${label}</label>
    <input class="form-input" id="${id}" type="${type}" value="${escHtml(value == null ? "" : String(value))}" ${extra}></div>`;
  const html = `
    <h4 class="mb-2">Property</h4>
    ${f("ep-addr", "Address *", p.property_address)}
    <div class="grid-2">${f("ep-city", "City", p.property_city)}${f("ep-state", "State", p.property_state, "text", 'maxlength="2"')}</div>
    <div class="grid-2">${f("ep-zip", "ZIP", p.property_zip)}${f("ep-year", "Year built", p.year_built, "number")}</div>
    <div class="grid-2">${f("ep-beds", "Beds", p.beds, "number")}${f("ep-baths", "Baths", p.baths, "number", 'step="0.5"')}</div>
    <div class="grid-2">${f("ep-sqft", "Sqft", p.sqft, "number")}${f("ep-assessed", "Assessed value", p.assessed_value, "number")}</div>
    ${f("ep-taxes", "Taxes owed", p.taxes_owed, "number")}
    <h4 class="mb-2 mt-4">Owner</h4>
    ${f("ep-owner", "Owner name", p.owner_name)}
    <label class="text-sm"><input type="checkbox" id="ep-llc" ${p.owner_is_llc ? "checked" : ""}> Owner is an LLC / company / trust</label>
    ${f("ep-email", "Owner email", p.owner_email, "email")}
    ${f("ep-mail", "Mailing address", p.mailing_address)}
    <div class="grid-2">${f("ep-mcity", "Mailing city", p.mailing_city)}${f("ep-mstate", "Mailing state", p.mailing_state, "text", 'maxlength="2"')}</div>
    ${f("ep-mzip", "Mailing ZIP", p.mailing_zip)}`;
  const footer = `<button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" id="ep-save">Save changes</button>`;
  openModal("Edit property", html, footer, { wide: true });
  document.getElementById("ep-save").addEventListener("click", async () => {
    const v = (id) => document.getElementById(id).value.trim();
    const num = (id, int) => { const x = v(id); if (x === "") return null; const n = int ? parseInt(x, 10) : parseFloat(x); return isNaN(n) ? null : n; };
    const body = {
      property_address: v("ep-addr"), property_city: v("ep-city"), property_state: v("ep-state"), property_zip: v("ep-zip"),
      year_built: num("ep-year", true), beds: num("ep-beds", true), baths: num("ep-baths"), sqft: num("ep-sqft", true),
      assessed_value: num("ep-assessed"), taxes_owed: num("ep-taxes"),
      owner_name: v("ep-owner"), owner_is_llc: document.getElementById("ep-llc").checked, owner_email: v("ep-email"),
      mailing_address: v("ep-mail"), mailing_city: v("ep-mcity"), mailing_state: v("ep-mstate"), mailing_zip: v("ep-mzip"),
    };
    if (!body.property_address) { toast("The address can't be empty.", "error"); return; }
    try {
      const saved = await API.patch(`/api/leads/${p.id}`, body);
      closeModal(); toast("Property saved", "success"); if (onSaved) onSaved(saved);
    } catch (e) { toast(e.message.replace(/^\d+\s*/, ""), "error"); }
  });
}

function archiveProperty(p, onDone) {
  const html = `<p class="text-sm">Archive <strong>${escHtml(p.property_address)}</strong>? It leaves Properties, the call queue and the dashboard, and new scans won't bring it back. You can restore it from 🗄 Archived.</p>
    <div class="form-group mt-4"><label class="form-label">Reason <span class="text-muted">(optional)</span></label>
      <select class="form-select" id="ar-reason"><option value="">—</option><option>Not a fit</option><option>Sold / no longer available</option>
        <option>Duplicate</option><option>Bad data</option><option>Owner not interested</option><option>Other</option></select></div>`;
  openModal("Archive property", html, `<button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    <button class="btn btn-primary" id="ar-go">🗄 Archive</button>`);
  document.getElementById("ar-go").addEventListener("click", async () => {
    try {
      await API.post(`/api/leads/${p.id}/archive`, { reason: document.getElementById("ar-reason").value || null });
      closeModal(); toast("Property archived", "success"); if (onDone) onDone();
    } catch (e) { toast(e.message.replace(/^\d+\s*/, ""), "error"); }
  });
}

async function deleteProperty(p, onDone) {
  if (!confirm(`Permanently delete ${p.property_address}? Its phones, calls and notes are removed. This can't be undone.`)) return;
  try { await API.delete(`/api/leads/${p.id}`); toast("Property deleted", "success"); if (onDone) onDone(); }
  catch (e) { toast(e.message.replace(/^\d+\s*/, ""), "error"); }
}

// ── Scan property: sourced findings from Grok web research, RentCast and paid property APIs ──
// Server: POST /api/leads/{id}/scan, GET /api/leads/{id}/scans (latest first). Every fact carries
// its source + link; Grok facts are re-read on the cited page ("check").
const SCAN_CHECK = {
  confirmed: ["badge-success", "✓ on page", "We re-read the cited page and found this value."],
  not_found: ["badge-warning", "⚠ not on page", "We re-read the cited page and did not find this value — treat with care."],
  unchecked: ["badge-neutral", "not re-checked", "The site doesn't let our bot read the page; open the link to confirm."],
  api: ["badge-info", "data API", "Structured data from the provider's API."],
};
const SCAN_STATUS = { done: "badge-success", partial: "badge-warning", failed: "badge-danger", running: "badge-info", interrupted: "badge-danger" };

function _scanLink(url, text) {
  return /^https?:\/\//i.test(url || "")
    ? `<a href="${escHtml(url)}" target="_blank" rel="noopener noreferrer">${escHtml(text)}</a>`
    : escHtml(text);
}

function renderPropertyScan(scan) {
  if (!scan) return `<div class="text-sm text-muted">Not scanned yet.</div>`;
  const secs = scan.duration_ms != null ? ` · ${Math.round(scan.duration_ms / 1000)} s` : "";
  const cost = scan.cost_usd ? ` · $${Number(scan.cost_usd).toFixed(3)}` : "";
  const head = `<div class="flex items-center gap-2 text-sm">
      <span class="badge ${SCAN_STATUS[scan.status] || "badge-neutral"}">${escHtml(scan.status)}</span>
      <span class="text-muted">Scanned ${fmtDateTime(scan.created_at)}${secs}${cost}</span></div>`;
  const sources = (scan.sources || []).map(s => {
    const cls = s.status === "ok" ? "badge-success" : s.status === "error" ? "badge-danger" : "badge-neutral";
    const bits = [s.status === "ok" ? `${s.facts} facts` : s.status, s.latency_ms != null ? `${Math.round(s.latency_ms / 1000)} s` : "",
      s.cost_usd ? `$${Number(s.cost_usd).toFixed(3)}` : "", s.model || ""].filter(Boolean).join(" · ");
    const why = s.error || s.note;
    return `<div class="text-xs"><span class="badge ${cls}">${escHtml(s.name)}</span> <span class="text-muted">${escHtml(bits)}</span>${why ? ` <span class="${s.error ? "text-danger" : "text-muted"}">— ${escHtml(why)}</span>` : ""}</div>`;
  }).join("");
  const grok = (scan.sources || []).find(s => s.id === "grok") || {};
  const dropped = Object.values(grok.dropped || {}).reduce((a, b) => a + b, 0);
  // Distress facts lead: the AI summary is web research only and can miss what a county list shows.
  const distress = (scan.facts || []).filter(f => f.category === "distress" && f.check !== "not_found");
  const alerts = distress.length ? `<div class="pb-sec">${distress.map(f => `<div class="text-sm"><span class="badge ${f.official && f.check === "confirmed" ? "badge-danger" : "badge-warning"}">⚠ ${escHtml(f.label)}</span>
      ${escHtml(f.value)} <span class="text-xs text-muted">— ${_scanLink(f.source_url, f.source_name || f.source)}</span></div>`).join("")}</div>` : "";
  const summary = scan.summary ? `<div class="pb-sec text-sm"><span class="pb-k">Grok summary</span> ${escHtml(scan.summary)}
      <div class="text-xs text-muted">AI summary of the web research only — rely on the sourced facts below.</div></div>` : "";
  const applied = (scan.applied || []).length
    ? `<div class="pb-sec text-xs">Filled empty lead fields: ${scan.applied.map(a => `${escHtml(a.field)} = <strong>${escHtml(a.value)}</strong> (${_scanLink(a.source_url, a.source)})`).join(", ")}</div>` : "";
  const groups = (scan.groups || []).map(g => {
    const rows = g.fields.map(e => e.values.map((f, i) => {
      const chk = SCAN_CHECK[f.check] || SCAN_CHECK.unchecked;
      const label = i === 0
        ? `<td rowspan="${e.values.length}" style="vertical-align:top">${escHtml(e.label)}${e.conflict ? ` <span class="badge badge-warning" title="Sources disagree — compare side by side">⚠ differs</span>` : ""}</td>` : "";
      const official = f.official ? ` <span class="badge badge-gold" title="Official government / appraisal-district site">official</span>` : "";
      return `<tr${e.conflict ? ' style="background:rgba(234,179,8,.08)"' : ""}>${label}
        <td title="${escHtml(f.quote || "")}"><strong>${escHtml(f.value)}</strong>${f.as_of ? ` <span class="text-xs text-muted">(${escHtml(f.as_of)})</span>` : ""}</td>
        <td class="text-xs">${_scanLink(f.source_url, f.source_name || f.source)}${official}</td>
        <td class="text-xs"><span class="badge ${chk[0]}" title="${escHtml(f.check_note || chk[2])}">${chk[1]}</span></td></tr>`;
    }).join("")).join("");
    return `<div class="pb-sec"><div class="pb-k">${escHtml(g.label)}${g.conflicts ? ` <span class="badge badge-warning">${g.conflicts} conflict${g.conflicts > 1 ? "s" : ""}</span>` : ""}</div>
      <div class="table-wrap"><table class="data-table" style="table-layout:fixed;width:100%"><colgroup><col style="width:20%"><col style="width:44%"><col style="width:22%"><col style="width:14%"></colgroup>
        <thead><tr><th>Field</th><th>Value</th><th>Source</th><th>Check</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }).join("");
  const checked = (grok.checked || []).length
    ? `<div class="pb-sec text-xs text-muted">Searched, nothing found (not proof of absence): ${grok.checked.map(escHtml).join("; ")}</div>` : "";
  const droppedNote = dropped ? `<div class="text-xs text-muted">${dropped} unsourced or unverifiable Grok fact${dropped > 1 ? "s were" : " was"} discarded.</div>` : "";
  const empty = !(scan.facts || []).length && scan.status !== "running" ? `<div class="text-sm text-muted pb-sec">No facts found for this address.</div>` : "";
  return `<div class="property-scan">${head}<div class="pb-sec">${sources}</div>${alerts}${summary}${applied}${groups}${empty}${checked}${droppedNote}</div>`;
}

/** Run a scan (takes ~30–60 s); calls onPropertyScanned(scan) when the page defines it. */
async function runPropertyScan(leadId, btn) {
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Scanning… (about a minute)"; }
  try {
    const scan = await API.post(`/api/leads/${encodeURIComponent(leadId)}/scan`);
    toast(scan.status === "failed" ? "Scan failed — see sources" : `Scan complete: ${(scan.facts || []).length} facts`, scan.status === "failed" ? "error" : "success");
    if (typeof onPropertyScanned === "function") onPropertyScanned(scan);
    return scan;
  } catch (e) {
    toast("Scan failed: " + e.message.replace(/^\d+\s*/, ""), "error");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}
