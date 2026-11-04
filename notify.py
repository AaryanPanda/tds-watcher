"""Notification channels. Each one is enabled only if its env vars are set — use any mix.

  Telegram  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   (a group/channel id works → whole group gets it)
  Discord   DISCORD_WEBHOOK_URL
  ntfy.sh   NTFY_TOPIC  (free phone push, no account; anyone who subscribes to the topic gets it)
  GitHub    GITHUB_TOKEN + GITHUB_REPOSITORY  (opens an issue; everyone who "Watches" the repo is emailed)
  Slack     SLACK_WEBHOOK_URL
"""

import json
import os
import urllib.request

UA = "tds-watcher/1.0"


def _post(url, payload, headers=None, raw=False):
    data = payload if raw else json.dumps(payload).encode()
    h = {"User-Agent": UA, **({} if raw else {"Content-Type": "application/json"}), **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=30) as r:
        return r.status


def _chunks(text, n):
    while text:
        yield text[:n]
        text = text[n:]


def telegram(title, text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return None
    for part in _chunks(text, 4000):
        _post(f"https://api.telegram.org/bot{tok}/sendMessage",
              {"chat_id": chat, "text": part, "disable_web_page_preview": True})
    return "telegram"


def discord(title, text):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return None
    for part in _chunks(text, 1900):
        _post(url, {"content": part})
    return "discord"


def slack(title, text):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return None
    _post(url, {"text": text[:39000]})
    return "slack"


def ntfy(title, text):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return None
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    # ntfy headers must be latin-1; strip emoji from the title header
    safe_title = title.encode("latin-1", "ignore").decode("latin-1").strip()
    _post(f"{server}/{topic}", text[:3900].encode(), {"Title": safe_title, "Tags": "bell"}, raw=True)
    return "ntfy"


def github_issue(title, text):
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (tok and repo) or os.environ.get("GITHUB_ISSUES", "1") == "0":
        return None
    _post(f"https://api.github.com/repos/{repo}/issues",
          {"title": title, "body": f"```\n{text[:60000]}\n```", "labels": ["tds-update"]},
          {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    return "github"


def send_all(title, text):
    sent = []
    for fn in (telegram, discord, slack, ntfy, github_issue):
        try:
            r = fn(title, text)
            if r:
                sent.append(r)
        except Exception as e:  # noqa: BLE001 — one broken channel shouldn't block the others
            print(f"[notify] {fn.__name__} failed: {e}")
    print(f"[notify] sent via: {', '.join(sent) or 'nothing configured'}")
    return sent
