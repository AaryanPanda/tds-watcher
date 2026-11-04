"""Read the TDS exam portal's public files and pull out every question and its own dates.

For each assessment slug (e.g. tds-2026-09-ga4) the portal serves, without login:
  /exam-<slug>.info.js   title, start, end (the assessment window)
  /exam-<slug>.js        all question modules; each starts with  let X="q-<id>",Y="<title>"
Questions are added to the second file in stages. Some questions state their own dates in their text
("Submissions close Sunday, 1 November 2026 at 11:59:59 PM IST"); we classify each date found as an
opening or closing date by the words just before it.
"""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(
    r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+)?"
    r"(\d{1,2})\s+(" + MONTHS + r")\s+(20\d\d)"
    r"(?:,?\s+(?:at\s+)?(\d{1,2}(?::\d\d){0,2}\s*(?:AM|PM|am|pm)|12\s+noon|noon|midnight))?"
)
QUESTION_RE = re.compile(r'let \w+="(q-[^"]+)",\w+="((?:[^"\\]|\\.){2,200})"')

CLOSE_WORDS = ("close", "closes", "closed", "complete by", "submit by", "submissions close", "due", "deadline",
               "until", "to", "by", "ends", "end", "before")
OPEN_WORDS = ("open", "opens", "from", "starts", "start", "begins", "available", "released", "after")


def _js_string(s):
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def strip_markup(js):
    """Bundle chunk -> readable text: drop tags, ${...} expressions and escapes."""
    s = re.sub(r"\$\{[^{}]*(\{[^{}]*\}[^{}]*)*\}", " ", js)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\\n", " ").replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s)


def _to_datetime(day, month, year, time_txt, kind):
    d = datetime.strptime(f"{day} {month} {year}", "%d %B %Y").replace(tzinfo=IST)
    t = (time_txt or "").lower().strip()
    if not t:
        return d.replace(hour=23, minute=59, second=59) if kind == "close" else d
    if "noon" in t:
        return d.replace(hour=12)
    if t == "midnight":
        return d.replace(hour=23, minute=59, second=59)  # "Monday 2 November, midnight" = end of that day
    m = re.match(r"(\d{1,2})(?::(\d\d))?(?::(\d\d))?\s*(am|pm)", t)
    h = int(m.group(1)) % 12 + (12 if m.group(4) == "pm" else 0)
    return d.replace(hour=h, minute=int(m.group(2) or 0), second=int(m.group(3) or 0))


def classify(prefix):
    """'open' / 'close' / None from the ~40 characters before a date: the keyword nearest the date wins."""
    p = " " + prefix.lower()
    best, pos = None, -1
    for kind, words in (("close", CLOSE_WORDS), ("open", OPEN_WORDS)):
        for w in words:
            for m in re.finditer(r"\b" + re.escape(w) + r"\b", p):
                if m.start() > pos:
                    best, pos = kind, m.start()
    return best


def find_dates(text):
    out, seen = [], set()
    for m in DATE_RE.finditer(text):
        prefix = text[max(0, m.start() - 40):m.start()]
        kind = classify(prefix)
        # "after X closes on <date>" describes a close date even though "after" is an open-word
        if re.search(r"clos(e|es|ed)\s+(on\s+)?$", prefix.lower()):
            kind = "close"
        try:
            at = _to_datetime(m.group(1), m.group(2), m.group(3), m.group(4), kind or "mention")
        except (ValueError, AttributeError):
            continue
        key = (kind, at)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind or "mention", "at": at.isoformat(), "text": m.group(0).strip(" ,")})
    return out


def parse_info(js):
    get = lambda k: (re.search(k + r':\s*"([^"]*)"', js) or [None, None])[1]
    return {"title": _js_string(get("title") or ""), "start": get("start"), "end": get("end")}


def parse_questions(js):
    """[{id, title, opens_at, closes_at, own_deadline, dates, hash}] in bundle order."""
    marks = list(QUESTION_RE.finditer(js))
    qs, seen = [], set()
    for i, m in enumerate(marks):
        qid = m.group(1)
        if qid in seen:
            continue
        seen.add(qid)
        chunk = js[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(js)]
        text = strip_markup(chunk[len(m.group(0)):])
        dates = find_dates(text)
        closes = [d["at"] for d in dates if d["kind"] == "close"]
        opens = [d["at"] for d in dates if d["kind"] == "open"]
        qs.append({
            "id": qid,
            "title": _js_string(m.group(2)),
            "closes_at": max(closes) if closes else None,
            "opens_at": min(opens) if opens else None,
            "own_deadline": bool(closes),
            "dates": [d for d in dates if d["kind"] != "mention"][:8],
            "hash": hashlib.sha256(text.encode()).hexdigest()[:12],
        })
    return qs


def nice(iso):
    if not iso:
        return "—"
    d = datetime.fromisoformat(iso)
    t = "" if (d.hour, d.minute) == (0, 0) else ", " + d.strftime("%I:%M %p").lstrip("0")
    return d.strftime("%a %d %b") + t


def diff(label, old, new):
    """Human sentences for changes between two parsed exams ({info, questions})."""
    msgs = []
    oi, ni = old.get("info", {}), new.get("info", {})
    if oi.get("end") and ni.get("end") and oi["end"] != ni["end"]:
        msgs.append(f"⏰ {label} deadline on the exam portal changed: {nice(oi['end'])} → {nice(ni['end'])}")
    if oi.get("start") and ni.get("start") and oi["start"] != ni["start"]:
        msgs.append(f"📅 {label} start on the exam portal changed: {nice(oi['start'])} → {nice(ni['start'])}")
    oq = {q["id"]: q for q in old.get("questions", [])}
    nq = {q["id"]: q for q in new.get("questions", [])}
    for i, q in enumerate(new.get("questions", []), 1):
        o = oq.get(q["id"])
        if not o:
            due = f" — its own deadline: {nice(q['closes_at'])}" if q["own_deadline"] else ""
            msgs.append(f"🆕 New question in {label} (Q{i}): {q['title']}{due}")
            continue
        if q.get("closes_at") != o.get("closes_at") and (q.get("closes_at") or o.get("closes_at")):
            msgs.append(f"⏰ {label} Q{i} \"{q['title']}\" deadline changed: {nice(o.get('closes_at'))} → {nice(q.get('closes_at'))}")
        elif q["hash"] != o.get("hash"):
            msgs.append(f"✏️ {label} Q{i} \"{q['title']}\" wording was updated")
    for qid, o in oq.items():
        if qid not in nq:
            msgs.append(f"🗑️ Question removed from {label}: {o['title']}")
    return msgs
