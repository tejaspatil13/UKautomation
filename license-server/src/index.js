// Copilot licence server (Cloudflare Worker + D1).
//
// POST /v1/license            the app: activate (first time) or check; returns a signed token
// GET  /health                is the server up, which environment
// /admin/...                  the administrator (Authorization: Bearer ADMIN_TOKEN), see admin() below
//
// Tokens are signed with Ed25519. The private key (SIGNING_KEY) and ADMIN_TOKEN are Worker secrets:
// they exist only here, never in the app or the repository. The test and production environments
// have different keys and databases, so a test token can never unlock a production app.

const PRODUCTION_TRIAL_SECONDS = 48 * 3600; // fixed: production ignores TRIAL_SECONDS
const TRIAL_OFFLINE_SECONDS = 24 * 3600; // a trial must check in at least daily
const PAID_OFFLINE_SECONDS = 14 * 24 * 3600;
const MIN_SECONDS_BETWEEN_CHECKS = 2; // per machine: stops a stuck client hammering the server
const MACHINE = /^[0-9a-f]{64}$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === "POST" && url.pathname === "/v1/license") return await license(request, env);
      if (url.pathname.startsWith("/admin/")) return await admin(request, env, url);
      if (url.pathname === "/health") return json({ ok: true, env: env.ENVIRONMENT });
      return json({ error: "not_found" }, 404);
    } catch (e) {
      console.error(e && e.stack ? e.stack : e);
      return json({ error: "server_error" }, 500);
    }
  },
};

const now = () => Math.floor(Date.now() / 1000);

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

function trialSeconds(env) {
  if (env.ENVIRONMENT === "production") return PRODUCTION_TRIAL_SECONDS;
  const s = Number(env.TRIAL_SECONDS);
  return Number.isFinite(s) && s > 0 ? s : PRODUCTION_TRIAL_SECONDS;
}

// ---------- the app ----------
async function license(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad_request" }, 400);
  }
  const machine = String(body.machine || "");
  const version = String(body.version || "").slice(0, 40);
  const label = String(body.label || "").slice(0, 60); // e.g. "GitHub build machine": the first note, if any
  if (!MACHINE.test(machine)) return json({ error: "bad_request" }, 400);

  const t = now();
  let row = await env.DB.prepare("SELECT * FROM licenses WHERE machine = ?").bind(machine).first();
  if (!row) {
    // First activation of this computer: the trial starts now, on the server's clock.
    await env.DB.prepare(
      "INSERT INTO licenses (machine, plan, revoked, trial_started_at, expires_at, created_at, last_seen_at, last_version, checks, note) " +
        "VALUES (?, 'trial', 0, ?, ?, ?, ?, ?, 1, ?) ON CONFLICT(machine) DO NOTHING",
    ).bind(machine, t, t + trialSeconds(env), t, t, version, label).run();
    await audit(env, machine, "trial_started", `${trialSeconds(env)} seconds`);
    row = await env.DB.prepare("SELECT * FROM licenses WHERE machine = ?").bind(machine).first();
  } else {
    if (row.last_seen_at && t - row.last_seen_at < MIN_SECONDS_BETWEEN_CHECKS) return json({ error: "too_many_requests" }, 429);
    await env.DB.prepare("UPDATE licenses SET last_seen_at = ?, last_version = ?, checks = checks + 1 WHERE machine = ?")
      .bind(t, version, machine).run();
  }
  return json({ token: await sign(env, payloadFor(env, row, t)) });
}

function payloadFor(env, row, t) {
  const expires = row.expires_at === null || row.expires_at === undefined ? null : Number(row.expires_at);
  let status = "active";
  if (row.revoked) status = "revoked";
  else if (expires !== null && t >= expires) status = "expired";
  let offlineUntil = t;
  if (status === "active") {
    const grace = row.plan === "paid" ? PAID_OFFLINE_SECONDS : TRIAL_OFFLINE_SECONDS;
    offlineUntil = expires === null ? t + grace : Math.min(expires, t + grace);
  }
  return {
    v: 1, env: env.ENVIRONMENT, machine: row.machine, status, plan: row.plan,
    expires_at: expires, issued_at: t, offline_until: offlineUntil,
  };
}

// ---------- signing ----------
const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
let keyCache = null;

async function signingKey(env) {
  if (!keyCache) {
    const der = Uint8Array.from(atob(env.SIGNING_KEY), (c) => c.charCodeAt(0));
    keyCache = await crypto.subtle.importKey("pkcs8", der, { name: "Ed25519" }, false, ["sign"]);
  }
  return keyCache;
}

async function sign(env, payload) {
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  const sig = await crypto.subtle.sign({ name: "Ed25519" }, await signingKey(env), bytes);
  return `${b64url(bytes)}.${b64url(sig)}`;
}

// ---------- administrator ----------
//   GET  /admin/licenses                 every computer
//   GET  /admin/licenses/<machine>       one computer
//   POST /admin/licenses/<machine>       {"action": ..., ...}:
//        trial_minutes {minutes}   trial ends <minutes> from now (testing: 5, 60, 2880 ...)
//        extend_hours  {hours}     add time to the current end (or from now if already ended)
//        set_expiry    {expires_at} end at this moment (unix seconds or ISO 8601)
//        expire                    end now
//        paid          {expires_at?} paid licence; permanent unless expires_at is given
//        revoke / unrevoke         turn access off / back on
//        reset                     forget this computer: its next start begins a new trial
//        note          {note}      free-text note (client name, invoice ...)
async function admin(request, env, url) {
  if (!(await authorised(request, env))) return json({ error: "unauthorised" }, 401);
  const m = url.pathname.match(/^\/admin\/licenses(?:\/([0-9a-f]{64}))?$/);
  if (!m) return json({ error: "not_found" }, 404);
  const machine = m[1];
  if (!machine) {
    if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405);
    const { results } = await env.DB.prepare("SELECT * FROM licenses ORDER BY created_at DESC").all();
    return json({ env: env.ENVIRONMENT, now: now(), licenses: results.map(describe) });
  }
  const row = await env.DB.prepare("SELECT * FROM licenses WHERE machine = ?").bind(machine).first();
  if (request.method === "GET") return row ? json({ env: env.ENVIRONMENT, now: now(), license: describe(row) }) : json({ error: "not_found" }, 404);
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  if (!row) return json({ error: "not_found" }, 404);

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad_request" }, 400);
  }
  const t = now();
  const set = (sql, ...args) => env.DB.prepare(`UPDATE licenses SET ${sql} WHERE machine = ?`).bind(...args, machine).run();
  const number = (v) => (Number.isFinite(Number(v)) && Number(v) > 0 ? Number(v) : null);
  const when = (v) => (typeof v === "number" ? Math.floor(v) : Number.isFinite(Date.parse(v)) ? Math.floor(Date.parse(v) / 1000) : null);
  let detail = "";
  switch (body.action) {
    case "trial_minutes": {
      const minutes = number(body.minutes);
      if (!minutes) return json({ error: "minutes required" }, 400);
      await set("plan = 'trial', revoked = 0, expires_at = ?", t + Math.round(minutes * 60));
      detail = `${minutes} minutes`;
      break;
    }
    case "extend_hours": {
      const hours = number(body.hours);
      if (!hours) return json({ error: "hours required" }, 400);
      if (row.expires_at === null) return json({ error: "this licence is permanent" }, 400);
      await set("expires_at = ?", Math.max(Number(row.expires_at), t) + Math.round(hours * 3600));
      detail = `${hours} hours`;
      break;
    }
    case "set_expiry": {
      const at = when(body.expires_at);
      if (at === null) return json({ error: "expires_at required" }, 400);
      await set("expires_at = ?", at);
      detail = new Date(at * 1000).toISOString();
      break;
    }
    case "expire":
      await set("expires_at = ?", t);
      break;
    case "paid": {
      const at = body.expires_at === undefined || body.expires_at === null ? null : when(body.expires_at);
      await set("plan = 'paid', revoked = 0, expires_at = ?", at);
      detail = at === null ? "permanent" : new Date(at * 1000).toISOString();
      break;
    }
    case "revoke":
      await set("revoked = 1");
      break;
    case "unrevoke":
      await set("revoked = 0");
      break;
    case "reset":
      await env.DB.prepare("DELETE FROM licenses WHERE machine = ?").bind(machine).run();
      await audit(env, machine, "reset", "");
      return json({ env: env.ENVIRONMENT, reset: machine });
    case "note":
      await set("note = ?", String(body.note || "").slice(0, 500));
      detail = String(body.note || "").slice(0, 100);
      break;
    default:
      return json({ error: "unknown action" }, 400);
  }
  await audit(env, machine, body.action, detail);
  const updated = await env.DB.prepare("SELECT * FROM licenses WHERE machine = ?").bind(machine).first();
  return json({ env: env.ENVIRONMENT, now: now(), license: describe(updated) });
}

function describe(row) {
  const p = payloadFor({ ENVIRONMENT: "" }, row, now());
  return { ...row, status: p.status, seconds_left: row.expires_at === null ? null : Math.max(0, row.expires_at - now()) };
}

async function authorised(request, env) {
  const given = (request.headers.get("authorization") || "").replace(/^Bearer /, "");
  if (!env.ADMIN_TOKEN || !given) return false;
  const a = new TextEncoder().encode(given);
  const b = new TextEncoder().encode(env.ADMIN_TOKEN);
  if (a.byteLength !== b.byteLength) return false;
  return crypto.subtle.timingSafeEqual(a, b);
}

async function audit(env, machine, action, detail) {
  await env.DB.prepare("INSERT INTO audit (at, machine, action, detail) VALUES (?, ?, ?, ?)").bind(now(), machine, action, detail).run();
}
