// Update notice for the desktop app, added on top of the dashboard by the launcher (the dashboard's
// own code is not changed). Shows when a new version has been downloaded.
(function () {
  "use strict";
  if (window.__copilotBanner) return;
  window.__copilotBanner = true;

  const box = document.createElement("div");
  box.setAttribute("role", "status");
  box.style.cssText = "position:fixed;right:16px;bottom:56px;z-index:2147483000;max-width:min(380px,calc(100vw - 32px));" +
    "background:#fff;border:1px solid #e1e4e9;border-left:3px solid #2357c6;border-radius:8px;padding:12px 14px;" +
    "box-shadow:0 8px 24px rgba(16,24,40,.12);font:13px/1.45 -apple-system,'Segoe UI',system-ui,sans-serif;color:#1c2430;display:none";
  const text = document.createElement("div");
  const err = document.createElement("div");
  err.style.cssText = "color:#c0392b;margin-top:6px;display:none";
  const row = document.createElement("div");
  row.style.cssText = "display:flex;gap:8px;margin-top:10px;justify-content:flex-end";
  const later = document.createElement("button");
  const now = document.createElement("button");
  for (const [b, label, primary] of [[later, "Later", false], [now, "Restart now", true]]) {
    b.type = "button";
    b.textContent = label;
    b.style.cssText = "font:inherit;font-weight:500;border-radius:6px;padding:6px 12px;cursor:pointer;border:1px solid " +
      (primary ? "#2357c6;background:#2357c6;color:#fff" : "#e1e4e9;background:#fff;color:#1c2430");
  }
  row.append(later, now);
  box.append(text, err, row);
  document.body.appendChild(box);

  let dismissed = "";
  later.addEventListener("click", () => { dismissed = text.dataset.version; box.style.display = "none"; });
  now.addEventListener("click", async () => {
    now.disabled = true;
    err.style.display = "none";
    try {
      const r = await fetch("/desktop/update/restart", { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail[0]);
      text.textContent = "Installing the update… Copilot will open again in a moment.";
      row.style.display = "none";
    } catch (e) {
      err.textContent = e.message || "Couldn't restart now.";
      err.style.display = "block";
      now.disabled = false;
    }
  });

  async function poll() {
    try {
      const st = await (await fetch("/desktop/status", { cache: "no-store" })).json();
      if (!st.license.allowed) { location.replace("/desktop/"); return; }
      const u = st.update;
      if (u.status === "ready" && u.version !== dismissed) {
        text.dataset.version = u.version;
        text.textContent = `A new version of Copilot (${u.version}) is ready. Restart to install it, or it installs ` +
          "the next time you close Copilot. Your data and sheets are kept.";
        box.style.display = "block";
      }
    } catch (e) { /* app closing or restarting */ }
  }
  poll();
  setInterval(poll, 60000);
})();
