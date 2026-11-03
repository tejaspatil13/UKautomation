// Desktop additions on top of the dashboard, added by the launcher (the dashboard's own code is not
// changed): a slim bar at the bottom with the time left on the licence and "Check for app updates",
// and a notice when a new version is ready to install.
(function () {
  "use strict";
  if (window.__copilotBanner) return;
  window.__copilotBanner = true;

  const FONT = "font:12px/1.4 -apple-system,'Segoe UI',system-ui,sans-serif;";
  function el(tag, css, text) {
    const e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text) e.textContent = text;
    return e;
  }
  function btn(label, primary) {
    const b = el("button", "font:inherit;font-weight:500;border-radius:6px;padding:6px 12px;cursor:pointer;border:1px solid " +
      (primary ? "#2357c6;background:#2357c6;color:#fff" : "#e1e4e9;background:#fff;color:#1c2430"), label);
    b.type = "button";
    return b;
  }

  // ---------- bottom bar: licence time left · version · Check for app updates ----------
  const bar = el("div", FONT + "position:fixed;left:0;right:0;bottom:0;z-index:2147482999;display:flex;gap:16px;" +
    "align-items:center;justify-content:space-between;padding:5px 16px;background:#fafbfc;border-top:1px solid #e1e4e9;color:#5b6573");
  const left = el("span", "font-variant-numeric:tabular-nums");
  const right = el("span", "display:flex;gap:12px;align-items:center");
  const version = el("span");
  const updText = el("span");
  const check = el("button", "font:inherit;font-weight:500;border:0;background:none;padding:0;color:#2357c6;" +
    "text-decoration:underline;cursor:pointer", "Check for app updates");
  check.type = "button";
  right.append(updText, version, check);
  bar.append(left, right);
  document.body.appendChild(bar);
  document.body.style.paddingBottom = "34px"; // the dashboard's footer stays visible above the bar

  let expiresAt = null; // unix seconds; null = no end date
  let plan = "";
  function two(n) { return String(n).padStart(2, "0"); }
  function tick() {
    if (!plan) { left.textContent = ""; return; }
    const label = plan === "paid" ? "Subscription" : "Trial";
    if (expiresAt == null) { left.textContent = `${label}: active, no end date`; return; }
    let s = Math.max(0, Math.floor(expiresAt - Date.now() / 1000));
    const d = Math.floor(s / 86400); s %= 86400;
    const h = Math.floor(s / 3600); s %= 3600;
    const m = Math.floor(s / 60); s %= 60;
    left.textContent = `${label}: ${d ? `${d} day${d > 1 ? "s" : ""} ` : ""}${two(h)}:${two(m)}:${two(s)} left`;
    left.style.color = expiresAt - Date.now() / 1000 < 3 * 3600 ? "#a15c00" : "#5b6573";
  }
  setInterval(tick, 1000);

  let checking = false; // the user pressed the button: say "latest version" when the check finishes
  check.addEventListener("click", async () => {
    updText.style.color = "#5b6573";
    try {
      const r = await fetch("/desktop/update/check", { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail[0]);
      checking = true;
      updText.textContent = "Checking for updates…";
      pollSoon();
    } catch (e) {
      updText.textContent = e.message || "Couldn't check for updates.";
    }
  });

  function showUpdate(u) {
    if (u.status === "checking") updText.textContent = "Checking for updates…";
    else if (u.status === "downloading") updText.textContent = `Downloading version ${u.version}… ${u.progress || 0}%`;
    else if (u.status === "ready") updText.textContent = `Version ${u.version} is ready`;
    else if (checking && u.status === "error") { updText.textContent = u.error; updText.style.color = "#c0392b"; checking = false; }
    else if (checking && u.status === "idle") { updText.textContent = "You have the latest version"; checking = false; }
    else if (!checking) updText.textContent = "";
  }

  // ---------- update ready: Restart now / Later ----------
  const box = el("div", "position:fixed;right:16px;bottom:48px;z-index:2147483000;max-width:min(380px,calc(100vw - 32px));" +
    "background:#fff;border:1px solid #e1e4e9;border-left:3px solid #2357c6;border-radius:8px;padding:12px 14px;" +
    "box-shadow:0 8px 24px rgba(16,24,40,.12);font:13px/1.45 -apple-system,'Segoe UI',system-ui,sans-serif;color:#1c2430;display:none");
  box.setAttribute("role", "status");
  const text = el("div");
  const err = el("div", "color:#c0392b;margin-top:6px;display:none");
  const row = el("div", "display:flex;gap:8px;margin-top:10px;justify-content:flex-end");
  const later = btn("Later", false);
  const now = btn("Restart now", true);
  row.append(later, now);
  box.append(text, err, row);
  document.body.appendChild(box);

  let dismissed = "";
  let force = false; // second press after the "data download is running" warning
  later.addEventListener("click", () => { dismissed = text.dataset.version; box.style.display = "none"; });
  now.addEventListener("click", async () => {
    now.disabled = true;
    err.style.display = "none";
    try {
      const r = await fetch("/desktop/update/restart", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force }),
      });
      if (!r.ok) {
        const body = await r.json();
        if (body.busy) { force = true; now.textContent = "Restart anyway"; }
        throw new Error(body.detail[0]);
      }
      text.textContent = "Installing the update… Copilot will open again in a moment.";
      row.style.display = "none";
    } catch (e) {
      err.textContent = e.message || "Couldn't restart now.";
      err.style.display = "block";
      now.disabled = false;
    }
  });

  // ---------- the name entered in the dashboard: shown in the admin panel ("Herschel's PC") ----------
  let sentName = "";
  function sendName() {
    let name = "";
    try { name = (localStorage.getItem("ukbf.employee") || "").trim(); } catch (e) { /* storage blocked */ }
    if (!name || name === sentName) return;
    sentName = name;
    fetch("/desktop/user", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) })
      .catch(() => { sentName = ""; });
  }
  window.addEventListener("storage", sendName);

  // ---------- status ----------
  let timer = null;
  async function poll() {
    let busy = false;
    sendName(); // also picks up a rename (the dashboard saves it in this tab: no storage event)
    try {
      const st = await (await fetch("/desktop/status", { cache: "no-store" })).json();
      if (!st.license.allowed) { location.replace("/desktop/"); return; }
      plan = st.license.kind === "disabled" ? "" : (st.license.plan || "trial");
      expiresAt = st.license.expires_at;
      tick();
      version.textContent = `Version ${st.version}`;
      const u = st.update;
      showUpdate(u);
      busy = u.status === "checking" || u.status === "downloading";
      if (u.status === "ready" && (u.required || u.version !== dismissed)) {
        text.dataset.version = u.version;
        text.textContent = u.required
          ? `A new version of Copilot (${u.version}) must be installed before you can start new searches. ` +
            "Restart now to install it. Your data and sheets are kept."
          : `A new version of Copilot (${u.version}) is ready. Restart to install it, or it installs ` +
            "the next time you close Copilot. Your data and sheets are kept.";
        later.style.display = u.required ? "none" : "";
        box.style.borderLeftColor = u.required ? "#c0392b" : "#2357c6";
        box.style.display = "block";
      }
    } catch (e) { /* app closing or restarting */ }
    schedule(busy ? 1000 : 15000);
  }
  function schedule(ms) { clearTimeout(timer); timer = setTimeout(poll, ms); }
  function pollSoon() { schedule(500); }
  poll();
})();
