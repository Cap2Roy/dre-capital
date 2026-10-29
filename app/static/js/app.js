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
