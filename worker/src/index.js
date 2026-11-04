// TDS alerts — signup backend. Stores the email list privately in D1.
// It never sends email itself: the watcher (GitHub Actions) sends confirmations and alerts over SMTP.
//
// Public:  POST /subscribe {email}        → pending + triggers a confirmation email
//          GET  /confirm?t=TOKEN          → active
//          GET|POST /unsubscribe?t=TOKEN  → unsubscribed (POST = one-click from mail apps)
//          GET  /stats                    → {subscribers}
// Admin (Authorization: Bearer ADMIN_TOKEN):
//          GET  /admin/pending            → [{email, token}] needing a confirmation email
//          POST /admin/confirm-sent {tokens}
//          GET  /admin/active             → [{email, token}]

const EMAIL_RE = /^[^\s@<>()[\]\\,;:"]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}$/;
// Only IITM addresses: one per student, so nobody can sign up several times. Override with the
// ALLOWED_DOMAINS variable (comma-separated); a domain also allows its subdomains (ds.study.iitm.ac.in).
const DEFAULT_ALLOWED_DOMAINS = "study.iitm.ac.in";

function allowedDomain(email, env) {
  const domain = email.split("@").pop();
  return (env.ALLOWED_DOMAINS || DEFAULT_ALLOWED_DOMAINS).split(",").map((d) => d.trim().toLowerCase()).filter(Boolean)
    .some((d) => domain === d || domain.endsWith("." + d));
}
const MAX_PER_IP_PER_HOUR = 5;
const RESEND_AFTER_MIN = 10;

export default {
  // Cron trigger (wrangler.toml [triggers]): ask GitHub to run the checker. Cloudflare's cron fires on time;
  // GitHub's own scheduler can delay or skip runs when it's busy.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(kickWorkflow(env, "check"));
  },

  async fetch(req, env) {
    const url = new URL(req.url);
    const cors = corsHeaders(env);
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    try {
      const route = `${req.method} ${url.pathname.replace(/\/+$/, "") || "/"}`;
      switch (route) {
        case "POST /subscribe": return withCors(await subscribe(req, env), cors);
        case "GET /confirm": return await confirm(url, env);
        case "GET /unsubscribe":
        case "POST /unsubscribe": return await unsubscribe(req, url, env);
        case "GET /stats": return withCors(await stats(env), cors);
        case "GET /admin/pending": return await admin(req, env, pending);
        case "POST /admin/confirm-sent": return await admin(req, env, confirmSent);
        case "GET /admin/active": return await admin(req, env, active);
        case "GET /": return json({ ok: true, service: "tds-alerts" });
        default: return json({ error: "not found" }, 404);
      }
    } catch (e) {
      console.error(e);
      return withCors(json({ error: "server error" }, 500), cors);
    }
  },
};

// ---------------------------------------------------------------- public

async function subscribe(req, env) {
  let body;
  try { body = await req.json(); } catch { return json({ error: "Send JSON: {\"email\": \"...\"}" }, 400); }
  const ok = () => json({ ok: true, message: "Almost done — check your inbox (and spam) for a confirmation link." });
  if (body.website) return ok(); // honeypot field filled → a bot; pretend success

  const email = String(body.email || "").trim().toLowerCase();
  if (email.length > 254 || !EMAIL_RE.test(email)) return json({ error: "That doesn't look like a valid email address." }, 400);
  if (!allowedDomain(email, env)) return json({ error: "Use your IITM student email, the one ending in study.iitm.ac.in (for example 23f1000000@ds.study.iitm.ac.in)." }, 400);

  const ip = req.headers.get("CF-Connecting-IP") || "unknown";
  const hourAgo = new Date(Date.now() - 3600e3).toISOString();
  const { n } = await env.DB.prepare("SELECT COUNT(*) AS n FROM signups WHERE ip = ? AND at > ?").bind(ip, hourAgo).first();
  if (n >= MAX_PER_IP_PER_HOUR) return json({ error: "Too many signups from your network. Try again in an hour." }, 429);
  await env.DB.batch([
    env.DB.prepare("INSERT INTO signups (ip, at) VALUES (?, ?)").bind(ip, now()),
    env.DB.prepare("DELETE FROM signups WHERE at < ?").bind(new Date(Date.now() - 86400e3).toISOString()),
  ]);

  const row = await env.DB.prepare("SELECT status, created_at FROM subscribers WHERE email = ?").bind(email).first();
  // Same response whether or not the address is already on the list, so the form can't be used to probe who signed up.
  if (row?.status === "active") return ok();
  if (row?.status === "pending" && Date.now() - Date.parse(row.created_at) < RESEND_AFTER_MIN * 60e3) return ok();

  await env.DB.prepare(
    `INSERT INTO subscribers (email, token, status, created_at, confirm_sent) VALUES (?, ?, 'pending', ?, 0)
     ON CONFLICT(email) DO UPDATE SET token = excluded.token, status = 'pending', created_at = excluded.created_at, confirm_sent = 0`
  ).bind(email, token(), now()).run();

  await kickWorkflow(env);
  return ok();
}

async function confirm(url, env) {
  const t = url.searchParams.get("t") || "";
  const r = await env.DB.prepare(
    "UPDATE subscribers SET status = 'active', confirmed_at = ? WHERE token = ? AND status != 'unsubscribed' RETURNING email"
  ).bind(now(), t).first();
  if (!r) return page(env, "Link expired", "This confirmation link is no longer valid. Sign up again from the dashboard.");
  return page(env, "You're subscribed ✅", `TDS alerts will be sent to <b>${esc(r.email)}</b>. Every email has a one-click unsubscribe link.`);
}

async function unsubscribe(req, url, env) {
  const t = url.searchParams.get("t") || "";
  const r = await env.DB.prepare("UPDATE subscribers SET status = 'unsubscribed' WHERE token = ? RETURNING email").bind(t).first();
  if (req.method === "POST") return new Response(null, { status: 204 }); // RFC 8058 one-click
  if (!r) return page(env, "Already done", "This address isn't receiving alerts.");
  return page(env, "Unsubscribed", `<b>${esc(r.email)}</b> won't get any more TDS alerts. You can re-subscribe from the dashboard anytime.`);
}

async function stats(env) {
  const { n } = await env.DB.prepare("SELECT COUNT(*) AS n FROM subscribers WHERE status = 'active'").first();
  return json({ subscribers: n }, 200, { "Cache-Control": "public, max-age=300" });
}

// ---------------------------------------------------------------- admin (used by the watcher)

async function admin(req, env, fn) {
  const auth = req.headers.get("Authorization") || "";
  if (!env.ADMIN_TOKEN || !timingSafeEqual(auth, `Bearer ${env.ADMIN_TOKEN}`)) return json({ error: "unauthorized" }, 401);
  return fn(req, env);
}

async function pending(req, env) {
  const twoDays = new Date(Date.now() - 2 * 86400e3).toISOString();
  const { results } = await env.DB.prepare(
    "SELECT email, token FROM subscribers WHERE status = 'pending' AND confirm_sent = 0 AND created_at > ? LIMIT 200"
  ).bind(twoDays).all();
  return json(results.filter((r) => allowedDomain(r.email, env)));
}

async function confirmSent(req, env) {
  const { tokens = [] } = await req.json();
  if (tokens.length) {
    await env.DB.batch(tokens.slice(0, 500).map((t) =>
      env.DB.prepare("UPDATE subscribers SET confirm_sent = 1 WHERE token = ?").bind(String(t))));
  }
  return json({ ok: true, updated: tokens.length });
}

async function active(req, env) {
  const { results } = await env.DB.prepare("SELECT email, token FROM subscribers WHERE status = 'active' ORDER BY confirmed_at").all();
  return json(results.filter((r) => allowedDomain(r.email, env)));
}

// ---------------------------------------------------------------- helpers

async function kickWorkflow(env, eventType = "signup") {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    console.log("GITHUB_TOKEN or GITHUB_REPO not set; skipping workflow trigger");
    return;
  }
  try {
    const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "tds-alerts-worker",
      },
      body: JSON.stringify({ event_type: eventType }),
    });
    if (!r.ok) console.error(`dispatch ${eventType} failed: HTTP ${r.status} ${await r.text()}`);
  } catch (e) {
    console.error("dispatch failed", e); // the scheduled run will still send it
  }
}

function corsHeaders(env) {
  let origin = "*";
  try { origin = new URL(env.DASHBOARD_URL).origin; } catch {}
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    Vary: "Origin",
  };
}

function withCors(res, cors) {
  const h = new Headers(res.headers);
  for (const [k, v] of Object.entries(cors)) h.set(k, v);
  return new Response(res.body, { status: res.status, headers: h });
}

function json(data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json", ...extra } });
}

function page(env, title, msg) {
  const back = env.DASHBOARD_URL ? `<p><a href="${esc(env.DASHBOARD_URL)}">← Back to the TDS dashboard</a></p>` : "";
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#1b1f24;background:#f6f7f9}
@media(prefers-color-scheme:dark){body{color:#e6e8eb;background:#111418}a{color:#7cc4ff}}h1{font-size:1.5rem}</style>
<h1>${esc(title)}</h1><p>${msg}</p>${back}`,
    { headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}

function token() {
  const b = crypto.getRandomValues(new Uint8Array(24));
  return btoa(String.fromCharCode(...b)).replace(/[+/=]/g, (c) => ({ "+": "-", "/": "_", "=": "" })[c]);
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

const now = () => new Date().toISOString();
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
