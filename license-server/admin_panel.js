// Copilot admin panel (runs on this Mac only; see admin_panel.py).
(function () {
  "use strict";
  const ONLINE_S = 180; // apps check their licence every minute: seen in the last 3 minutes = running now
  const INTRO = {
    production: "Your clients' PCs (the client release). Changes reach a running PC within about a minute.",
    test: "Your own and your testers' PCs (test builds). Changes reach a running PC within about a minute.",
  };

  // The session key arrives after '#' in the address the panel opened; keep it for reloads of this tab only.
  let session = location.hash.slice(1) || sessionStorage.getItem("copilot-admin") || "";
  sessionStorage.setItem("copilot-admin", session);

  let env = new URLSearchParams(location.search).get("env") || sessionStorage.getItem("copilot-admin-env") || "production";
  if (!["test", "production"].includes(env)) env = "production";
  history.replaceState(null, "", "/");
  let data = { test: null, production: null };
  let skew = 0; // server clock minus this Mac's clock
  let loadedAt = 0;
  let showBuild = false;

  const $ = (id) => document.getElementById(id);
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("show"), 2600);
  }
  async function api(path, body) {
    const r = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: { "X-Admin-Session": session, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 403) throw new Error("This panel was opened by an older launch. Close this tab and start Copilot Admin again.");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || `Error ${r.status}`);
    return j;
  }

  const nowS = () => Date.now() / 1000 + skew;
  function two(n) { return String(n).padStart(2, "0"); }
  function left(s) {
    s = Math.max(0, Math.floor(s));
    const d = Math.floor(s / 86400); s %= 86400;
    const h = Math.floor(s / 3600); s %= 3600;
    const m = Math.floor(s / 60); s %= 60;
    return `${d ? `${d} day${d > 1 ? "s" : ""} ` : ""}${two(h)}:${two(m)}:${two(s)}`;
  }
  function ago(t) {
    if (!t) return "never";
    const s = Math.max(0, nowS() - t);
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }
  function date(t) {
    return t ? new Date(t * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–";
  }

  // ---------- loading ----------
  async function load(which) {
    try {
      const j = await api(`/api/${which}/devices`);
      skew = j.now - Date.now() / 1000;
      data[which] = j.licenses;
      $(`n-${which}`).textContent = j.licenses.filter((p) => p.note !== "GitHub build machine").length;
      if (which === env) { loadedAt = Date.now(); render(); }
    } catch (e) {
      if (which === env) showError(e.message);
    }
  }
  function refresh() { load(env); load(env === "test" ? "production" : "test"); }

  function showError(msg) {
    const box = $("list");
    box.replaceChildren(el("div", "error", msg));
  }

  // ---------- actions ----------
  async function act(pc, body, done, ask) {
    const who = pc.note || `PC ${pc.machine.slice(0, 12)}`;
    if (ask && !confirm(ask)) return;
    if (env === "production" && !confirm(`This changes a CLIENT's PC: ${who}.\n\nContinue?`)) return;
    try {
      await api(`/api/${env}/devices/${pc.machine}`, body);
      toast(`${who}: ${done}`);
      await load(env);
    } catch (e) {
      toast(e.message);
    }
  }
  function hoursPrompt(title) {
    const v = prompt(`${title}\n\nHow many hours? (for minutes, write e.g. 0.5 = 30 minutes)`, "24");
    if (v == null) return null;
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) { toast("Enter a number of hours greater than 0."); return null; }
    return n;
  }

  function buttons(pc) {
    const row = el("div", "actions");
    const add = (label, cls, fn, disabled, title) => {
      const b = el("button", `btn ${cls}`, label);
      b.type = "button";
      b.disabled = !!disabled;
      if (title) b.title = title;
      b.addEventListener("click", fn);
      row.append(b);
      return b;
    };
    const permanent = pc.expires_at == null;
    const end = pc.expires_at || 0;

    add("+ 1 hour", "", () => act(pc, { action: "extend_hours", hours: 1 }, "1 hour added"), permanent, permanent ? "No end date" : "");
    add("+ 1 day", "", () => act(pc, { action: "extend_hours", hours: 24 }, "1 day added"), permanent, permanent ? "No end date" : "");
    add("− 1 hour", "", () => act(pc, { action: "set_expiry", expires_at: Math.max(Math.floor(nowS()), end - 3600) }, "1 hour removed"),
      permanent || end <= nowS(), "Take an hour off the time left");
    add("Set time left…", "", () => {
      const h = hoursPrompt(`Set the time left for ${pc.note || "this PC"}, counting from now.`);
      if (h != null) act(pc, { action: "set_expiry", expires_at: Math.floor(nowS() + h * 3600) }, `time left set to ${h} h`);
    });
    add("Restart 2-day trial", "quiet", () => act(pc, { action: "trial_minutes", minutes: 2880 }, "48-hour trial from now"),
      false, "Trial of 48 hours from now; also lifts a block");
    row.append(el("span", "gap"));
    add(pc.plan === "paid" && permanent ? "Paid ✓" : "Mark as paid", "primary",
      () => act(pc, { action: "paid" }, "marked as paid (no end date)"), pc.plan === "paid" && permanent);
    if (pc.revoked) {
      add("Unblock", "", () => act(pc, { action: "unrevoke" }, "unblocked"));
    } else {
      add("Block access", "danger", () => act(pc, { action: "revoke" }, "blocked",
        "Block this PC? Copilot shows \"Access has been turned off\" until you unblock it."));
    }
    add("Stop now", "solid-danger", () => act(pc, { action: "expire" }, "stopped",
      "End this PC's subscription now? Copilot shows \"Your subscription has been completed\" within about a minute."),
      !permanent && end <= nowS());
    add("Forget", "quiet", () => act(pc, { action: "reset" }, "forgotten",
      "Forget this PC? Its next start begins a NEW 2-day trial. Use this only for test PCs."));
    add("Rename", "quiet", () => {
      const name = prompt("Name for this PC (e.g. \"Client: ACME Ltd\", \"Tejas Windows PC\")", pc.note || "");
      if (name != null) act(pc, { action: "note", note: name.trim() }, "renamed");
    });
    return row;
  }

  // ---------- drawing ----------
  function statusBadge(pc) {
    if (pc.revoked) return el("span", "badge b-blocked", "Blocked");
    if (pc.status === "expired") return el("span", "badge b-ended", "Ended");
    if (pc.plan === "paid") return el("span", "badge b-paid", "Paid");
    return el("span", "badge b-active", "Trial");
  }
  function leftBox(pc) {
    const box = el("div", "left");
    box.dataset.machine = pc.machine;
    box.append(el("strong"), el("span"));
    return box;
  }
  function tickLeft(box, pc) {
    const [big, small] = box.children;
    box.classList.remove("warn", "over");
    if (pc.revoked) { big.textContent = "Blocked"; small.textContent = "access turned off"; box.classList.add("over"); return; }
    if (pc.expires_at == null) { big.textContent = "No end date"; small.textContent = "paid, permanent"; return; }
    const s = pc.expires_at - nowS();
    if (s <= 0) { big.textContent = "Ended"; small.textContent = `on ${date(pc.expires_at)}`; box.classList.add("over"); return; }
    big.textContent = left(s);
    small.textContent = `left · ends ${date(pc.expires_at)}`;
    if (s < 3 * 3600) box.classList.add("warn");
  }

  function render() {
    for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.env === env));
    $("intro").textContent = INTRO[env];
    const all = data[env];
    // GitHub's build machines register while CI tests the app: hidden unless asked for.
    const hidden = (all || []).filter((p) => p.note === "GitHub build machine").length;
    const list = all && (showBuild ? all : all.filter((p) => p.note !== "GitHub build machine"));
    const toggle = $("build-toggle");
    toggle.hidden = !hidden;
    toggle.textContent = showBuild ? "Hide GitHub build machines" : `Show GitHub build machines (${hidden})`;
    const box = $("list");
    if (!list) { box.replaceChildren(el("div", "empty", "Loading…")); return; }
    if (!list.length) {
      box.replaceChildren(el("div", "empty", env === "production"
        ? "No client PCs yet. A PC appears here the first time someone opens the client release of Copilot."
        : "No test PCs yet. A PC appears here the first time someone opens a test build of Copilot."));
      return;
    }
    const cards = list.map((pc) => {
      const card = el("article", "pc");
      const head = el("div", "pc-head");
      const online = el("span", `online${nowS() - (pc.last_seen_at || 0) < ONLINE_S ? " on" : ""}`);
      online.append(el("i"), document.createTextNode(nowS() - (pc.last_seen_at || 0) < ONLINE_S ? "Copilot open now" : `last seen ${ago(pc.last_seen_at)}`));
      head.append(el("span", "name", pc.note || "Unnamed PC"), statusBadge(pc), online);
      const meta = el("div", "meta");
      meta.append(
        el("span", "id", `ID ${pc.machine.slice(0, 12)}`),
        el("span", "", `First opened ${date(pc.trial_started_at || pc.created_at)}`),
        el("span", "", `Version ${pc.last_version || "–"}`),
      );
      const lb = leftBox(pc);
      tickLeft(lb, pc);
      card.append(head, lb, meta);
      const ended = pc.expires_at != null && pc.expires_at <= nowS();
      if (pc.revoked || ended) {
        card.append(el("div", "hint", pc.revoked && ended
          ? "Blocked and ended. To let this PC use Copilot again: Unblock, then + 1 day, Set time left, Restart 2-day trial or Mark as paid."
          : pc.revoked
            ? "Blocked. Unblock to let this PC use Copilot again (its time left is unchanged)."
            : "Ended. To let this PC use Copilot again: + 1 day, Set time left, Restart 2-day trial or Mark as paid."));
      }
      card.append(buttons(pc));
      return card;
    });
    box.replaceChildren(...cards);
  }

  // countdowns every second; data every 10 seconds
  setInterval(() => {
    for (const box of document.querySelectorAll(".left")) {
      const pc = (data[env] || []).find((p) => p.machine === box.dataset.machine);
      if (pc) tickLeft(box, pc);
    }
    $("updated").textContent = loadedAt ? `Updated ${Math.round((Date.now() - loadedAt) / 1000)} s ago` : "";
  }, 1000);
  setInterval(() => load(env), 10000);

  for (const b of document.querySelectorAll(".tabs button")) {
    b.addEventListener("click", () => {
      env = b.dataset.env;
      sessionStorage.setItem("copilot-admin-env", env);
      render();
      load(env);
    });
  }
  $("build-toggle").addEventListener("click", () => { showBuild = !showBuild; render(); });
  render();
  refresh();
})();
