# TDS Watcher

A student-run deadline board and alert service for IITM's **Tools in Data Science** course.

- **Dashboard** (GitHub Pages): every assessment with a live countdown, a term timeline, an "add to calendar" button, and a feed of what changed. Pin its link in the WhatsApp group.
- **Email alerts**: anyone can sign up with their student or personal email on the dashboard. They confirm by email, and each alert has a one-click unsubscribe link.
- **Checker**: runs every 20 minutes on GitHub Actions, for free.

```
GitHub Actions (every 20 min)                       Cloudflare Worker (free)
┌──────────────────────────────┐   reads list      ┌──────────────────────────┐
│ watcher.py                   │ ◀──────────────── │ /subscribe /confirm      │
│  1. course repo (git diff)   │                   │ /unsubscribe  + D1 list   │◀── signup form
│  2. tds.s-anand.net          │   "new signup"    │ (emails stay private)    │
│  3. exam portal pages        │ ────────────────▶ │                          │
│  → emails (SMTP)             │  repository_dispatch                         │
│  → site/data.json → Pages ───┼──▶ dashboard      └──────────────────────────┘
└──────────────────────────────┘
```

## What gets detected

| Source | How | Why it matters |
|---|---|---|
| Course repo, [sanand0/tools-in-data-science-public](https://github.com/sanand0/tools-in-data-science-public) | git diff; the term's Schedule table parsed row by row | The earliest signal. On 4 Nov 2026, GA4's new dates were committed at 06:30 while the live site still showed the old ones hours later. |
| Live site, `tds.s-anand.net/<term>/` | text snapshot + diff | Anything published outside the repo |
| Exam portal, every `exam.sanand.workers.dev/...` link | page plus its own JS/JSON, snapshot + diff | Changes inside an assignment page |
| Unlinked exams, `tds-<term>-ga5`, `-p2`, `-roe`… | probed each run | Spots an exam that is live before the course page links it |

**Emailed** (important only, to stay within sending limits): an assessment going live, new assessments, deadline / release-date / weight changes, exams spotted early.
**Dashboard only**: new course pages and edits to page text.

## Setup (about 30 minutes, all free)

You need a GitHub account, a Cloudflare account and a Gmail account. GitHub Pages needs a **public** repo, unless you have GitHub Pro (free with the GitHub Student Developer Pack). The repo never contains anyone's email address: the list lives only in Cloudflare.

### 1. GitHub repo

1. Create a repo (for example `tds-watcher`) and push these files.
2. **Settings → Pages → Source: GitHub Actions.**
3. **Settings → Actions → General → Workflow permissions: Read and write.**

Your dashboard address will be `https://<username>.github.io/<repo>/`.

### 2. Signup backend (Cloudflare Worker + D1)

```bash
cd worker
npx wrangler login
npx wrangler d1 create tds-alerts            # copy the database_id it prints into wrangler.toml
npx wrangler d1 execute tds-alerts --remote --file schema.sql
```

In `wrangler.toml`, set `DASHBOARD_URL` to your dashboard address and `GITHUB_REPO` to `username/repo`. Then:

```bash
openssl rand -hex 32                         # this is your ADMIN_TOKEN; keep it for step 4
npx wrangler secret put ADMIN_TOKEN
npx wrangler secret put GITHUB_TOKEN         # optional, see below
npx wrangler deploy                          # prints https://tds-alerts.<you>.workers.dev
```

`GITHUB_TOKEN` is a fine-grained personal access token limited to this one repo, with **Actions: Read and write** and **Contents: Read and write** permissions (repository_dispatch needs Contents write). It lets a new signup trigger the confirmation email within about a minute. Without it, confirmations go out on the next scheduled run, up to 20 minutes later.

### 3. Email sending (Gmail to start)

1. Turn on 2-Step Verification for the Gmail account. Use a separate account for the alerts if you can, not your personal one.
2. Go to <https://myaccount.google.com/apppasswords> and create an app password named "TDS alerts". Copy the 16 characters.

Gmail allows about **500 recipients a day**. With 500 subscribers, that's one alert per day. The watcher sends one combined email per run and stops at `EMAIL_DAILY_LIMIT` (default 480), logging who was skipped. If the course changes several times on busy days, move to a paid SMTP provider: only the settings below change.

| Provider | SMTP_HOST | Cost for ~500 people | Needs |
|---|---|---|---|
| Gmail | `smtp.gmail.com` | Free, ~500/day | App password |
| Amazon SES | `email-smtp.ap-south-1.amazonaws.com` | ≈ $0.10 per 1,000 emails | Verified domain, leave sandbox |
| Zoho ZeptoMail | `smtp.zeptomail.in` | Pay as you go, about $2.50 per 10,000 emails | Verified domain |

### 4. Repo secrets and variables

**Settings → Secrets and variables → Actions.**

Secrets:

| Name | Value |
|---|---|
| `ALERTS_ADMIN_TOKEN` | The ADMIN_TOKEN from step 2 |
| `SMTP_USER` | your.alerts@gmail.com |
| `SMTP_PASS` | The 16-character app password |

Variables:

| Name | Value |
|---|---|
| `ALERTS_API` | `https://tds-alerts.<you>.workers.dev` |
| `DASHBOARD_URL` | `https://<username>.github.io/<repo>` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `MAIL_FROM` | your.alerts@gmail.com |
| `MAIL_FROM_NAME` | `TDS Alerts` (optional) |
| `GITHUB_ISSUES` | `0` to stop opening a GitHub issue per change (optional) |

Optional extra channels, as secrets: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL`, `NTFY_TOPIC`, `AIPIPE_TOKEN` (adds a short AI summary).

### 5. Start it

**Actions → TDS watcher → Run workflow.** The first run records a baseline and publishes the dashboard. Sign yourself up on the dashboard to check the confirmation email arrives (look in spam the first time and mark it "Not spam").

Then pin the dashboard link in the WhatsApp group.

## Running locally

```bash
python watcher.py --dry-run --no-web --since HEAD~5   # replay recent course changes, writes site/data.json
cd site && python -m http.server 8000                 # view the dashboard at localhost:8000
cd worker && npx wrangler dev                         # signup backend with a local database
```

Python standard library only. The worker needs Node for `wrangler`.

## Limits

- **Questions behind login.** If an exam page only shows questions after sign-in, the watcher sees the public shell: it can tell you an exam went live and when that page changes, not each new question.
- **GitHub's scheduler** can run late by a few minutes, and pauses scheduled workflows after 60 days with no repo activity. The state commits keep it active.
- **Unofficial.** The course page is the source of truth. Both the dashboard and the emails say so.

## Privacy

- Emails are stored only in your Cloudflare D1 database, never in the repo or on the dashboard.
- Signup needs email confirmation, so nobody can subscribe someone else.
- The signup form answers the same way whether or not an address is already on the list, so it can't be used to check who signed up.
- Signups are limited to 5 per hour per network, and a hidden field catches simple bots.
- Unsubscribe is one click, including from Gmail's own "Unsubscribe" button.

## Files

| Path | What it does |
|---|---|
| `watcher.py` | Checks, change detection, dashboard data |
| `emailer.py` | Confirmation and alert emails over SMTP |
| `notify.py` | Telegram, Discord, Slack, ntfy, GitHub issues |
| `site/index.html` | The dashboard |
| `worker/` | Signup backend (Cloudflare Worker + D1) |
| `state/` | Last seen state and a running `changelog.md`, committed by the workflow |
