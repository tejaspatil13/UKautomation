// Start-up screen of the desktop app: licence check, first-time data download, then the dashboard.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let continued = false;
  try { continued = sessionStorage.getItem("copilot.continued") === "1"; } catch (e) { /* ignore */ }
  let busy = false;

  function button(label, onClick, primary) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (primary) b.className = "primary";
    b.addEventListener("click", async () => {
      b.disabled = true;
      busy = true;
      try { await onClick(); } finally { busy = false; b.disabled = false; tick(); }
    });
    return b;
  }

  function show({ title, message, spinner, bad, actions }) {
    $("card").className = "card" + (bad ? " tone-bad" : "");
    $("title").innerHTML = "";
    if (spinner) {
      const s = document.createElement("span");
      s.className = "spinner";
      s.setAttribute("aria-hidden", "true");
      $("title").appendChild(s);
    }
    $("title").appendChild(document.createTextNode(title));
    $("message").textContent = message || "";
    const a = $("actions");
    a.replaceChildren(...(actions || []));
  }

  async function post(path) {
    const r = await fetch(path, { method: "POST" });
    if (!r.ok) {
      let msg = "Something went wrong.";
      try { msg = (await r.json()).detail[0]; } catch (e) { /* keep default */ }
      throw new Error(msg);
    }
    return r.json();
  }

  const diagnostics = () => button("Save diagnostics", async () => { location.href = "/desktop/diagnostics"; });

  function enterDashboard(st) {
    // One user on one PC: no "who's working today?" prompt. The dashboard still labels searches and
    // default file names with a name, and the name can be changed from the header as before.
    try {
      if (!localStorage.getItem("ukbf.employee")) localStorage.setItem("ukbf.employee", st.default_name);
      sessionStorage.setItem("copilot.continued", "1");
    } catch (e) { /* storage unavailable: the dashboard asks for the name instead */ }
    location.replace("/");
  }

  function render(st) {
    $("foot").textContent = `Version ${st.version}` + (st.channel !== "stable" ? ` · ${st.channel} build` : "");
    const lic = st.license;
    $("datasets").classList.add("hidden");
    $("progress").classList.add("hidden");

    if (lic.kind === "checking") return show({ title: "Checking your licence…", spinner: true });
    if (!lic.allowed) {
      return show({
        title: lic.title || "Copilot is not activated",
        message: lic.message,
        bad: lic.kind === "expired" || lic.kind === "revoked" || lic.kind === "invalid",
        actions: [button("Check again", async () => render(await post("/desktop/license/check")), true), diagnostics()],
      });
    }
    if (!st.backend_ready) return show({ title: "Starting Copilot…", spinner: true });

    const missing = st.datasets.filter((d) => !d.installed);
    if (!missing.length || continued) return enterDashboard(st);

    // First run: the existing automatic download (the same one the server did on first boot).
    const updating = st.datasets.some((d) => d.updating);
    const failed = st.datasets.find((d) => d.error && !d.updating);
    show({
      title: updating ? "Setting up your data" : failed ? "The data download stopped" : "Preparing to download your data",
      spinner: updating || !failed,
      bad: !!failed && !updating,
      message: failed && !updating
        ? `${failed.error} Check the internet connection, then choose Try again. Data already downloaded is kept.`
        : "The first time, Copilot downloads the government rating list, business listings and Companies House " +
          "data (about 4 GB). This usually takes 1–3 hours and runs in the background. You can start using " +
          "Copilot now; searches work fully once all three are installed.",
      actions: [
        button("Continue to Copilot", async () => { continued = true; enterDashboard(st); }, true),
        ...(failed && !updating ? [button("Try again", async () => {
          await post("/api/updates/check");
          await post("/api/updates/install");
        })] : []),
        diagnostics(),
      ],
    });
    const list = $("datasets");
    list.replaceChildren(...st.datasets.map((d) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = d.name;
      const s = document.createElement("span");
      [s.className, s.textContent] = d.installed && !d.updating ? ["ok", "Installed"]
        : d.updating ? ["busy", "Downloading…"] : d.error ? ["err", "Not installed"] : ["wait", "Waiting"];
      li.append(name, s);
      return li;
    }));
    list.classList.remove("hidden");
    if (updating && st.import_progress) {
      $("progress").textContent = st.import_progress;
      $("progress").classList.remove("hidden");
    }
  }

  async function tick() {
    if (busy) return;
    try {
      const r = await fetch("/desktop/status", { cache: "no-store" });
      if (r.ok) render(await r.json());
    } catch (e) {
      show({ title: "Starting Copilot…", spinner: true });
    }
  }

  tick();
  setInterval(tick, 2000);
})();
