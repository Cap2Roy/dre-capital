/* DRE-Capital frontend logic v2 — shared API helpers + UI utilities */
const API = {
  async get(path) {
    const r = await fetch(path);
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
  async patch(path, body) {
    const r = await fetch(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
  async upload(path, formData) {
    const r = await fetch(path, { method: "POST", body: formData });
    if (r.status === 401) { window.location.href = "/login"; return; }
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
};

function fmtMoney(n) {
  if (n == null) return "—";
  return "$" + Math.round(n).toLocaleString("en-US");
}
function fmtMoneyK(n) {
  if (n == null) return "—";
  if (Math.abs(n) >= 1000) return "$" + (n / 1000).toFixed(0) + "k";
  return "$" + n.toFixed(0);
}
function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
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
  return Math.ceil((new Date(iso) - new Date()) / 86400000);
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
  };
  const [cls, label] = map[status] || ["badge-neutral", status];
  return `<span class="badge ${cls}">${label}</span>`;
}

function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function openModal(title, bodyHtml, footerHtml = "") {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h3>${title}</h3>
        <button class="modal-close" onclick="this.closest('.modal-overlay').remove()">×</button>
      </div>
      <div class="modal-body">${bodyHtml}</div>
      ${footerHtml ? `<div class="modal-footer">${footerHtml}</div>` : ""}
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
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

// Active nav highlight
document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page) {
    document.querySelectorAll(".sidebar-nav a").forEach(a => {
      if (a.dataset.page === page) a.classList.add("active");
    });
  }
});
