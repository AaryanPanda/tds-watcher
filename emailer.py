"""Email to subscribers who signed up on the dashboard.

The signup list lives in the Cloudflare Worker (worker/). This module reads it and sends over SMTP,
so any provider works — just change the SMTP_* settings:

  Gmail (start here)   SMTP_HOST=smtp.gmail.com  SMTP_PORT=587  SMTP_USER=you@gmail.com  SMTP_PASS=<16-char app password>
                       ~500 recipients/day on a personal account — fine if only important changes are emailed.
  Amazon SES           SMTP_HOST=email-smtp.<region>.amazonaws.com  (≈ $0.10 per 1,000 emails, needs a verified domain)
  Zoho ZeptoMail       SMTP_HOST=smtp.zeptomail.in  (pay-as-you-go, needs a verified domain)
  Brevo                SMTP_HOST=smtp-relay.brevo.com  (free 300/day — too small for 500 people)

Env: ALERTS_API (worker URL), ALERTS_ADMIN_TOKEN, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS,
     MAIL_FROM (defaults to SMTP_USER), MAIL_FROM_NAME, DASHBOARD_URL, EMAIL_DAILY_LIMIT (default 480)
"""

import html
import json
import os
import smtplib
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

IST = timezone(timedelta(hours=5, minutes=30))


def _cfg():
    return {
        "api": os.environ.get("ALERTS_API", "").rstrip("/"),
        "admin": os.environ.get("ALERTS_ADMIN_TOKEN", ""),
        "host": os.environ.get("SMTP_HOST", ""),
        "port": int(os.environ.get("SMTP_PORT") or 587),
        "user": os.environ.get("SMTP_USER", ""),
        "pass": os.environ.get("SMTP_PASS", ""),
        "from": os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER", ""),
        "from_name": os.environ.get("MAIL_FROM_NAME", "TDS Alerts"),
        "dash": os.environ.get("DASHBOARD_URL", "").rstrip("/"),
        "limit": int(os.environ.get("EMAIL_DAILY_LIMIT") or 480),
    }


def enabled():
    c = _cfg()
    return bool(c["api"] and c["admin"] and c["host"] and c["from"])


def _api(path, payload=None):
    c = _cfg()
    req = urllib.request.Request(
        c["api"] + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {c['admin']}", "Content-Type": "application/json", "User-Agent": "tds-watcher"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


class _Mailer:
    """One SMTP connection, reconnects if the server drops it mid-batch."""

    def __init__(self, c):
        self.c, self.s = c, None

    def _connect(self):
        if self.c["port"] == 465:
            self.s = smtplib.SMTP_SSL(self.c["host"], 465, context=ssl.create_default_context(), timeout=30)
        else:
            self.s = smtplib.SMTP(self.c["host"], self.c["port"], timeout=30)
            if os.environ.get("SMTP_TLS", "starttls") != "none":  # "none" only for a local test server
                self.s.starttls(context=ssl.create_default_context())
        if self.c["user"]:
            self.s.login(self.c["user"], self.c["pass"])

    def send(self, msg):
        for attempt in range(2):
            try:
                if not self.s:
                    self._connect()
                self.s.send_message(msg)
                return
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPSenderRefused, ConnectionError):
                self.s = None
                if attempt:
                    raise
                time.sleep(2)

    def close(self):
        if self.s:
            try:
                self.s.quit()
            except Exception:  # noqa: BLE001
                pass


def _message(c, to, subject, text, body_html, unsub_url=None):
    m = EmailMessage()
    m["From"] = formataddr((c["from_name"], c["from"]))
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = make_msgid(domain=c["from"].split("@")[-1])
    if unsub_url:
        # Gmail/Yahoo bulk-sender rules: one-click unsubscribe headers
        m["List-Unsubscribe"] = f"<{unsub_url}>"
        m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    m.set_content(text)
    m.add_alternative(body_html, subtype="html")
    return m


def _wrap_html(inner, footer):
    return f"""<!doctype html><html><body style="margin:0;padding:24px;background:#f4f5f7;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1b1f24">
<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:10px;padding:24px;border:1px solid #e3e6ea">{inner}</div>
<p style="max-width:560px;margin:12px auto 0;font-size:12px;color:#6b7280;text-align:center">{footer}</p></body></html>"""


# ------------------------------------------------------------------ confirmations

def send_confirmations():
    """Email a confirmation link to everyone who signed up since the last run."""
    if not enabled():
        return 0
    c = _cfg()
    pending = _api("/admin/pending")
    if not pending:
        return 0
    mailer, done = _Mailer(c), []
    try:
        for p in pending:
            link = f"{c['api']}/confirm?t={p['token']}"
            text = (f"Someone (hopefully you) asked to get TDS course alerts at {p['email']}.\n\n"
                    f"Confirm here: {link}\n\nIf this wasn't you, ignore this email — you won't be subscribed.")
            body = _wrap_html(
                f"""<h2 style="margin:0 0 12px;font-size:20px">Confirm your TDS alerts</h2>
<p>Someone (hopefully you) asked to get alerts about new TDS assignments and deadline changes at <b>{html.escape(p['email'])}</b>.</p>
<p style="margin:24px 0"><a href="{html.escape(link)}" style="background:#2563eb;color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:600">Yes, send me alerts</a></p>
<p style="color:#6b7280;font-size:14px">If this wasn't you, ignore this email. You won't be subscribed.</p>""",
                "Student-run tool, not an official IITM service.")
            try:
                mailer.send(_message(c, p["email"], "Confirm your TDS alerts", text, body))
                done.append(p["token"])
            except Exception as e:  # noqa: BLE001
                print(f"[email] confirmation to {p['email']} failed: {e}")
    finally:
        mailer.close()
        if done:
            _api("/admin/confirm-sent", {"tokens": done})
    print(f"[email] sent {len(done)} confirmation(s)")
    return len(done)


# ------------------------------------------------------------------ alerts

def broadcast(subject, items, text, state):
    """Send one alert email to every confirmed subscriber. `state` tracks the daily count."""
    if not enabled():
        return 0
    c = _cfg()
    subs = _api("/admin/active")
    if not subs:
        print("[email] no confirmed subscribers yet")
        return 0

    today = datetime.now(IST).strftime("%Y-%m-%d")
    q = state.setdefault("email_quota", {})
    if q.get("day") != today:
        q.update(day=today, sent=0)
    room = c["limit"] - q["sent"]
    if room < len(subs):
        print(f"[email] daily limit: {room} left for {len(subs)} subscribers — sending to the first {max(room, 0)}. "
              f"Raise EMAIL_DAILY_LIMIT only if your provider allows it.")
        subs = subs[:max(room, 0)]

    dash = c["dash"]
    li = "".join(f'<li style="margin:0 0 10px">{_linkify(i)}</li>' for i in items)
    mailer, sent = _Mailer(c), 0
    try:
        for s in subs:
            unsub = f"{c['api']}/unsubscribe?t={s['token']}"
            dash_line = f'<p style="margin:20px 0 0"><a href="{html.escape(dash)}" style="color:#2563eb">Open the dashboard →</a> all deadlines with countdowns</p>' if dash else ""
            body = _wrap_html(
                f'<h2 style="margin:0 0 16px;font-size:18px">{html.escape(subject)}</h2><ul style="padding-left:18px;margin:0">{li}</ul>{dash_line}',
                f'You signed up for TDS alerts. <a href="{html.escape(unsub)}" style="color:#6b7280">Unsubscribe</a> · Student-run, not an official IITM service.')
            plain = f"{text}\n\n{('Dashboard: ' + dash) if dash else ''}\nUnsubscribe: {unsub}\n"
            try:
                mailer.send(_message(c, s["email"], subject, plain, body, unsub))
                sent += 1
            except smtplib.SMTPDataError as e:
                print(f"[email] provider refused ({e.smtp_code}); stopping this batch: {e}")
                break
            except Exception as e:  # noqa: BLE001
                print(f"[email] to {s['email']} failed: {e}")
            if sent % 50 == 0:
                time.sleep(1)  # be gentle with rate limits
    finally:
        mailer.close()
    q["sent"] += sent
    print(f"[email] alert sent to {sent} subscriber(s)")
    return sent


def _linkify(s):
    import re
    out = html.escape(s)
    return re.sub(r"(https?://[^\s<)]+)", r'<a href="\1" style="color:#2563eb">\1</a>', out)
