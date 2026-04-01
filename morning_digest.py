#!/usr/bin/env python3
"""
Morning Digest — unread emails + calendar events + Google Tasks.

Usage:
    python morning_digest.py            # terminal output
    python morning_digest.py --html     # open as HTML in browser
    python morning_digest.py --email    # send to yourself via Gmail

Config files (all in same directory as this script):
    credentials.json      — API keys and account credentials
    muted_senders.json    — senders whose email goes straight to Other
    important_senders.json — senders always flagged as important
"""

import base64
import re
import argparse
import webbrowser
import tempfile
import os
import json
import imaplib
import email as emaillib
from email.header import decode_header as imap_decode_header
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
]
BASE_DIR = Path(__file__).parent

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

# ── Load config ───────────────────────────────────────────────────────────────

def _load_json(filename):
    p = BASE_DIR / filename
    if not p.exists():
        print(f"WARNING: {filename} not found, using empty list")
        return []
    return json.loads(p.read_text())

def _load_creds_json():
    p = BASE_DIR / "credentials.json"
    if not p.exists():
        raise SystemExit(f"ERROR: credentials.json not found in {BASE_DIR}")
    return json.loads(p.read_text())

_CREDS        = _load_creds_json()
CREDS_FILE    = Path(os.path.expanduser(_CREDS["gmail_client_secret"]))
TOKEN_FILE    = Path(os.path.expanduser(_CREDS["gmail_token"]))
YANDEX_IMAP   = _CREDS["yandex_imap"]
YANDEX_USER   = _CREDS["yandex_user"]
YANDEX_PASS   = _CREDS["yandex_password"]

MUTED_SENDERS  = _load_json("muted_senders.json")
ALWAYS_IMPORTANT = _load_json("important_senders.json")

# ── Importance rules ──────────────────────────────────────────────────────────

HIGH_PRIORITY_DOMAINS = [
    "greenhope",           # Green Hope schools
    "irs.gov", "ssa.gov", "dmv", "gov",
    "bankofamerica.com", "chase.com", "wellsfargo.com", "citibank.com",
    "usbank.com", "capitalone.com", "discover.com", "americanexpress.com",
    "paypal.com", "venmo.com", "zelle",
    "schwab.com", "fidelity.com", "vanguard.com", "etrade.com",
    "robinhood.com", "coinbase.com",
    "apple.com", "google.com", "microsoft.com",
    "amazon.com", "fedex.com", "ups.com", "usps.com",
    "appdynamics.com", "cisco.com",
]

HIGH_PRIORITY_KEYWORDS = [
    "urgent", "action required", "your account", "security alert",
    "password", "verify", "suspended", "overdue", "payment due",
    "invoice", "statement", "refund", "receipt", "order confirmed",
    "shipment", "delivery", "sign in", "unusual activity",
    "green hope", "wcpss", "principal", "school",
]

SCHOOL_SENDERS = ["wcpss.net", "greenhope", "green hope"]

CATEGORIES = {
    "🏫 Schools": SCHOOL_SENDERS,
    "🏦 Financial": ["bank", "chase", "wellsfargo", "citi", "capitalone",
                     "discover", "amex", "americanexpress", "paypal", "venmo",
                     "schwab", "fidelity", "vanguard", "etrade", "robinhood",
                     "affirm", "creditkarma", "transunion", "experian", "equifax",
                     "zondacrypto", "coinbase"],
    "📦 Shipping": ["fedex", "ups", "usps", "amazon"],
    "🔐 Security": ["security", "noreply", "no-reply", "alert"],
    "🏢 Work": ["appdynamics", "cisco"],
}


def classify_email(sender, subject):
    _, addr = parseaddr(sender)
    addr_lower = addr.lower()
    sender_lower = sender.lower()

    # Mute known subscriptions/marketing first
    if any(m in addr_lower for m in MUTED_SENDERS):
        return "📧 Other", False

    # Always-important senders — bypass keyword checks
    if any(a in addr_lower or a in sender_lower for a in ALWAYS_IMPORTANT):
        # Determine category
        if any(k in addr_lower for k in SCHOOL_SENDERS):
            return "🏫 Schools", True
        if any(k in addr_lower for k in ["bank", "chase", "wellsfargo", "citi", "capitalone",
                                          "discover", "amex", "paypal", "venmo", "schwab",
                                          "fidelity", "vanguard", "creditkarma", "transunion",
                                          "experian", "affirm"]):
            return "🏦 Financial", True
        if any(k in addr_lower for k in ["appdynamics", "cisco"]):
            return "🏢 Work", True
        if "github" in addr_lower:
            return "🔐 Security", True
        return "⚠️ Important", True
    subject_lower = subject.lower()

    # Check school first
    if any(k in sender_lower for k in SCHOOL_SENDERS):
        return "🏫 Schools", True

    # Check categories
    for cat, keywords in CATEGORIES.items():
        if any(k in sender_lower for k in keywords):
            is_important = any(k in subject_lower for k in HIGH_PRIORITY_KEYWORDS)
            return cat, is_important

    # Keyword match on subject
    if any(k in subject_lower for k in HIGH_PRIORITY_KEYWORDS):
        return "⚠️ Important", True

    return "📧 Other", False


# ── Auth ─────────────────────────────────────────────────────────────────────

def get_credentials():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            print("Open this URL to authenticate:")
            creds = flow.run_local_server(port=0, open_browser=False,
                                          authorization_prompt_message="URL: {url}\n\nWaiting...")
        TOKEN_FILE.write_text(creds.to_json())
    return creds


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_body(payload):
    """Extract plain text or stripped HTML body."""
    def decode(data):
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    if "parts" in payload:
        # Prefer plain text
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain" and part["body"].get("data"):
                return decode(part["body"]["data"])
        # Fallback: HTML stripped
        for part in payload["parts"]:
            if part["mimeType"] == "text/html" and part["body"].get("data"):
                html = decode(part["body"]["data"])
                return re.sub(r"<[^>]+>", " ", html)
        # Recurse into multipart
        for part in payload["parts"]:
            if "parts" in part:
                result = get_body(part)
                if result:
                    return result
    elif payload["body"].get("data"):
        raw = decode(payload["body"]["data"])
        if payload.get("mimeType") == "text/html":
            return re.sub(r"<[^>]+>", " ", raw)
        return raw
    return ""


def fetch_unread_emails(gmail):
    results = gmail.users().messages().list(
        userId="me", q="is:unread newer_than:1d", maxResults=50
    ).execute()
    messages = results.get("messages", [])

    emails = []
    for m in messages:
        msg = gmail.users().messages().get(userId="me", id=m["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "(no subject)")
        sender = headers.get("From", "?")
        date = headers.get("Date", "?")
        _, addr = parseaddr(sender)
        body = get_body(msg["payload"])
        body_preview = " ".join(body.split())[:300]

        category, important = classify_email(sender, subject)
        gmail_link_https = f"https://mail.google.com/mail/u/0/#all/{m['id']}"
        gmail_link_app   = f"googlegmail:///mail/u/0/#all/{m['id']}"
        emails.append({
            "id": m["id"],
            "subject": subject,
            "sender": sender,
            "addr": addr,
            "date": date,
            "body": body_preview,
            "category": category,
            "important": important,
            "link": gmail_link_https,
            "link_app": gmail_link_app,
        })

    return emails


# ── Ollama triage ─────────────────────────────────────────────────────────────

def triage_emails(email_list, label=""):
    """Ask Codex which emails need attention. Returns set of indices."""
    if not email_list:
        return set()

    lines = "\n".join(
        f"{i}. Subject: {e['subject']} | From: {e['sender'].split('<')[0].strip()}"
        for i, e in enumerate(email_list)
    )

    prompt = (
        "You are a strict personal email triage assistant. "
        "Return ONLY a JSON array of indices for emails that genuinely require action or awareness. "
        "TOP PRIORITY (always include): Green Hope High School, Green Hope Elementary, GHE, GHHS, wcpss.net. "
        "Also include: bank/financial alerts, government, security alerts, "
        "shipping problems, receipts for recent purchases, personal correspondence. "
        "EXCLUDE everything else: newsletters, subscriptions, promotions, deals, job alerts, "
        "social media, digest emails, loyalty programs, marketing from any brand. "
        "When in doubt, EXCLUDE. Reply with ONLY the JSON array.\n\n"
        f"Emails:\n{lines}\n\nImportant indices:"
    )

    try:
        import subprocess, tempfile, json
        out_file = tempfile.mktemp(suffix=".txt")
        result = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-o", out_file, "-"],
            input=prompt, capture_output=True, text=True, timeout=180
        )
        if os.path.exists(out_file):
            response_text = open(out_file).read().strip()
            os.unlink(out_file)
            match = re.search(r'\[[\d,\s]*\]', response_text)
            if match:
                return set(json.loads(match.group()))
        print(f"  [Codex triage {label}: no valid response]")
    except Exception as e:
        print(f"  [Codex triage {label} failed: {e}]")
    return set()




YANDEX_IMAP   = "imap.yandex.com"
YANDEX_USER   = "root@javabean.ru"
YANDEX_PASS   = "lkjzrdyojwatlzvr"


def _imap_decode(s):
    if s is None:
        return ""
    parts = imap_decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


def fetch_yandex_unread():
    try:
        from datetime import date, timedelta
        since_date = (date.today() - timedelta(days=1)).strftime("%d-%b-%Y")  # e.g. 31-Mar-2026
        mail = imaplib.IMAP4_SSL(YANDEX_IMAP, 993)
        mail.login(YANDEX_USER, YANDEX_PASS)
        mail.select("INBOX")
        _, data = mail.search(None, f'(UNSEEN SINCE "{since_date}")')
        ids = data[0].split()
        emails = []
        for uid in ids[-50:]:  # last 50 unread
            _, msg_data = mail.fetch(uid, "(RFC822)")
            msg = emaillib.message_from_bytes(msg_data[0][1])
            subject = _imap_decode(msg["Subject"])
            sender  = _imap_decode(msg["From"])
            date    = msg.get("Date", "")
            category, important = classify_email(sender, subject)
            import urllib.parse
            # yandexmail:// scheme opens Yandex Mail app; search by subject
            search_q = urllib.parse.quote(subject[:80])
            yandex_link       = f"https://mail.yandex.com/#search?query={search_q}"
            yandex_link_app   = f"yandexmail://search?query={search_q}"
            emails.append({
                "subject": subject, "sender": sender, "date": date,
                "body": "", "category": category, "important": important,
                "addr": parseaddr(sender)[1],
                "link": yandex_link, "link_app": yandex_link_app,
            })
        mail.logout()
        return emails, len(ids)
    except Exception as e:
        return [], 0


# ── Calendar ──────────────────────────────────────────────────────────────────

def fetch_week_events(cal):
    now = datetime.now(timezone.utc)
    week_end = now + timedelta(days=7)
    result = cal.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=week_end.isoformat(),
        maxResults=30,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


# ── Tasks ─────────────────────────────────────────────────────────────────────

def fetch_tasks(tasks_svc):
    lists = tasks_svc.tasklists().list(maxResults=20).execute().get("items", [])
    all_tasks = []
    for tl in lists:
        items = tasks_svc.tasks().list(
            tasklist=tl["id"], showCompleted=False, maxResults=100
        ).execute().get("items", [])
        for t in items:
            if t.get("status") != "completed":
                t["_list"] = tl["title"]
                all_tasks.append(t)
    return all_tasks


# ── Renderers ─────────────────────────────────────────────────────────────────

def render_terminal(emails, yandex_emails, yandex_total, events, tasks):
    now = datetime.now()
    print(f"\n{'═'*64}")
    print(f"  🌅 MORNING DIGEST  —  {now.strftime('%A, %B %d %Y')}")
    print(f"{'═'*64}\n")

    # ── Emails ────────────────────────────────────────────────────────
    important = [e for e in emails if e["important"] or e["category"] != "📧 Other"]
    other     = [e for e in emails if not e["important"] and e["category"] == "📧 Other"]

    print(f"📬 GMAIL — {len(emails)} unread\n")

    if important:
        print(f"  ⭐ NEEDS ATTENTION\n")
        for e in important:
            flag = "🔴" if e["important"] else "🟡"
            _, addr = parseaddr(e["sender"])
            name = e["sender"].split("<")[0].strip().strip('"') or addr
            print(f"  {flag} {e['category']}  {e['subject']}")
            print(f"     {name}")
            if e["category"] == "🏫 Schools" and e["body"]:
                print(f"     ↳ {' '.join(e['body'].split())[:120]}")
            print()

    if other:
        from collections import Counter
        print(f"  📋 OTHER ({len(other)}) — skimming\n")
        for e in other:
            _, addr = parseaddr(e["sender"])
            name = e["sender"].split("<")[0].strip().strip('"') or addr
            print(f"     • {e['subject']}  [{name}]")
        print()

    # ── Yandex ────────────────────────────────────────────────────────
    print(f"{'─'*64}")
    print(f"\n📬 YANDEX ({yandex_total} unread total, showing last 50)\n")
    y_important = [e for e in yandex_emails if e["important"] or e["category"] != "📧 Other"]
    y_other     = [e for e in yandex_emails if not e["important"] and e["category"] == "📧 Other"]
    if y_important:
        print(f"  ⭐ NEEDS ATTENTION\n")
        for e in y_important:
            flag = "🔴" if e["important"] else "🟡"
            name = e["sender"].split("<")[0].strip().strip('"') or e["addr"]
            print(f"  {flag} {e['category']}  {e['subject']}")
            print(f"     {name}")
            print()
    if y_other:
        print(f"  📋 OTHER ({len(y_other)})\n")
        for e in y_other:
            name = e["sender"].split("<")[0].strip().strip('"') or e["addr"]
            print(f"     • {e['subject']}  [{name}]")
        print()

    # ── Calendar ──────────────────────────────────────────────────────
    print(f"{'─'*64}")
    print(f"\n📅 THIS WEEK ({len(events)} events)\n")
    if not events:
        print("  No events.\n")
    for ev in events:
        start = ev["start"].get("dateTime", ev["start"].get("date", ""))
        try:
            dt = datetime.fromisoformat(start)
            label = dt.strftime("%a %b %d  %I:%M %p")
        except Exception:
            label = start
        loc = f"  @ {ev['location'].split(',')[0]}" if ev.get("location") else ""
        print(f"  📌 {label}  —  {ev.get('summary','(no title)')}{loc}")
    print()

    # ── Tasks ─────────────────────────────────────────────────────────
    print(f"{'─'*64}")
    print(f"\n✅ OPEN TASKS ({len(tasks)})\n")
    if not tasks:
        print("  None.\n")
    for t in tasks:
        due = f"  ⚠️ due {t['due'][:10]}" if t.get("due") else ""
        print(f"  ⬜ {t.get('title','(no title)')}{due}  [{t['_list']}]")
    print(f"\n{'═'*64}\n")


def _render_web_digests(web_digests):
    if not web_digests:
        return ""
    parts = []
    for label, ul_html in web_digests.items():
        parts.append(f"""
  <h2>{label}</h2>
  <div class="card" style="font-size:.88em">
    {ul_html}
  </div>""")
    return "\n".join(parts)


def render_html(emails, yandex_emails, yandex_total, events, tasks, web_digests=None):
    now = datetime.now()
    important = [e for e in emails if e["important"] or e["category"] != "📧 Other"]
    other     = [e for e in emails if not e["important"] and e["category"] == "📧 Other"]

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def sender_name(s):
        name = s.split("<")[0].strip().strip('"')
        return name or s

    def make_link(e, color="#1a0dab"):
        link     = e.get("link", "")
        link_app = e.get("link_app", "")
        subj = esc(e["subject"])
        if not link:
            return f"<b>{subj}</b>"
        app_btn = f' <a href="{link_app}" style="font-size:.75em;color:#4285f4;text-decoration:none;border:1px solid #4285f4;border-radius:4px;padding:1px 5px">app</a>' if link_app else ""
        return f'<b><a href="{link}" style="color:{color};text-decoration:none">{subj}</a>{app_btn}</b>'

    rows_important = ""
    for e in important:
        flag = "🔴" if e["important"] else "🟡"
        preview = ""
        if e["category"] == "🏫 Schools" and e["body"]:
            preview = f'<div class="preview">{esc(" ".join(e["body"].split())[:150])}</div>'
        rows_important += f"""
        <tr class="imp">
          <td class="cat">{flag} {e['category']}</td>
          <td>{make_link(e)}{preview}</td>
          <td class="from">{esc(sender_name(e['sender']))}</td>
        </tr>"""

    rows_other = ""
    for e in other:
        rows_other += f'<li>{make_link(e, "#555")} <span class="from">[{esc(sender_name(e["sender"]))}]</span></li>'

    event_rows = ""
    for ev in events:
        start = ev["start"].get("dateTime", ev["start"].get("date", ""))
        try:
            dt = datetime.fromisoformat(start)
            label = dt.strftime("%a %b %d  %I:%M %p")
        except Exception:
            label = start
        loc = ev.get("location", "").split(",")[0]
        loc_html = f' <span class="from">@ {esc(loc)}</span>' if loc else ""
        event_rows += f"<li><b>{esc(ev.get('summary',''))}</b>  {label}{loc_html}</li>"

    task_rows = ""
    for t in tasks:
        due = f' <span class="due">⚠️ due {t["due"][:10]}</span>' if t.get("due") else ""
        task_rows += f'<li>⬜ {esc(t.get("title",""))}{due} <span class="from">[{esc(t["_list"])}]</span></li>'

    y_important = [e for e in yandex_emails if e["important"] or e["category"] != "📧 Other"]
    y_other     = [e for e in yandex_emails if not e["important"] and e["category"] == "📧 Other"]

    rows_y_important = ""
    for e in y_important:
        flag = "🔴" if e["important"] else "🟡"
        rows_y_important += f"""
        <tr class="imp">
          <td class="cat">{flag} {e['category']}</td>
          <td>{make_link(e)}</td>
          <td class="from">{esc(sender_name(e['sender']))}</td>
        </tr>"""

    rows_y_other = "".join(
        f'<li>{make_link(e, "#555")} <span class="from">[{esc(sender_name(e["sender"]))}]</span></li>'
        for e in y_other
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Digest — {now.strftime('%b %d')}</title>
  <style>
    *    {{ box-sizing:border-box; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
           font-size:15px; max-width:640px; margin:0 auto; padding:12px;
           background:#f4f4f4; color:#222; }}
    h1   {{ font-size:1.2em; margin:8px 0 16px; color:#333; }}
    h2   {{ font-size:.95em; text-transform:uppercase; letter-spacing:.05em;
           color:#4285f4; margin:20px 0 6px; }}
    .card {{ background:#fff; border-radius:10px; padding:12px 14px;
             box-shadow:0 1px 3px rgba(0,0,0,.08); margin-bottom:12px; }}
    table {{ width:100%; border-collapse:collapse; }}
    td    {{ padding:6px 4px; border-bottom:1px solid #f0f0f0; vertical-align:top; font-size:.88em; }}
    tr:last-child td {{ border-bottom:none; }}
    tr.imp td {{ background:#fff8e1; }}
    .cat  {{ white-space:nowrap; padding-right:8px; font-size:.8em; }}
    .from {{ color:#888; font-size:.82em; }}
    .due  {{ color:#d32f2f; font-size:.82em; }}
    .preview {{ color:#555; font-size:.82em; margin-top:2px; }}
    ul    {{ margin:0; padding:0 0 0 18px; }}
    li    {{ padding:4px 0; font-size:.88em; border-bottom:1px solid #f0f0f0; }}
    li:last-child {{ border-bottom:none; }}
    .card ul {{ padding-left: 16px; }}
    .card li  {{ list-style: disc; }}
  </style>
</head>
<body>
  <h1>🌅 {now.strftime('%A, %B %d %Y')}</h1>

  <h2>📧 Gmail — ⭐ Needs Attention ({len(important)} of {len(emails)} unread)</h2>
  <div class="card">
    <table>
      {rows_important or '<tr><td colspan="3" class="from">Nothing urgent</td></tr>'}
    </table>
  </div>
  {"<h2>📧 Gmail — Other (" + str(len(other)) + ")</h2><div class='card'><ul>" + rows_other + "</ul></div>" if other else ""}

  <h2>📧 Yandex — ⭐ Needs Attention ({len(y_important)} of {yandex_total} unread)</h2>
  <div class="card">
    <table>
      {rows_y_important or '<tr><td colspan="3" class="from">Nothing urgent</td></tr>'}
    </table>
  </div>
  {"<h2>📧 Yandex — Other (" + str(len(y_other)) + ")</h2><div class='card'><ul>" + rows_y_other + "</ul></div>" if y_other else ""}

  <h2>📅 This Week ({len(events)} events)</h2>
  <div class="card">
    <ul>{event_rows or '<li class="from">No events</li>'}</ul>
  </div>

  <h2>✅ Open Tasks ({len(tasks)})</h2>
  <div class="card">
    <ul>{task_rows or '<li class="from">All clear</li>'}</ul>
  </div>

  {_render_web_digests(web_digests)}

  <p class="from" style="text-align:center;margin-top:16px">Generated {now.strftime('%H:%M')}</p>
</body>
</html>"""
    return html


# ── Web digests via Codex ─────────────────────────────────────────────────────

WEB_DIGEST_SOURCES = [
    {
        "label": "🇵🇱 Onet.pl — Biznes & Wiadomości",
        "prompt": (
            "Odwiedź stronę onet.pl. Przejrzyj sekcje 'Wiadomości' i 'Biznes'. "
            "Zignoruj wszystkie artykuły dotyczące Rosji i Ukrainy. "
            "Skup się na sekcji Biznes. "
            "Zwróć 5-7 najważniejszych artykułów jako listę HTML. "
            "Format każdego elementu: <li><a href='URL'>TYTUŁ</a> — jedno zdanie po polsku</li>. "
            "Zwróć TYLKO blok <ul>...</ul>, nic więcej."
        ),
    },
    {
        "label": "🛍️ Slickdeals — Best Deals",
        "prompt": (
            "Visit slickdeals.net right now. Find the top 7 hottest deals currently on the front page. "
            "Return them as an HTML list. "
            "Format each item: <li><a href='DEAL_URL'>DEAL TITLE</a> — price/discount info in one line</li>. "
            "Return ONLY the <ul>...</ul> block, nothing else."
        ),
    },
    {
        "label": "🟠 Hacker News — Top Stories",
        "prompt": (
            "Visit news.ycombinator.com right now and return the top 7 stories as an HTML list. "
            "Format each item: <li><a href='LINK'>TITLE</a> — one sentence summary</li>. "
            "Return ONLY the <ul>...</ul> block, nothing else."
        ),
    },
    {
        "label": "📈 Business Insider — Top Stories",
        "prompt": (
            "Visit businessinsider.com right now and find the top 7 news and business stories. "
            "Return them as an HTML list. "
            "Format each item: <li><a href='LINK'>TITLE</a> — one sentence summary</li>. "
            "Return ONLY the <ul>...</ul> block, nothing else."
        ),
    },
]


def fetch_one_web_digest(source):
    try:
        import subprocess, tempfile, re, os
        out_file = tempfile.mktemp(suffix=".txt")
        subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-o", out_file, "-"],
            input=source["prompt"], capture_output=True, text=True, timeout=180
        )
        if os.path.exists(out_file):
            content = open(out_file).read().strip()
            os.unlink(out_file)
            # Extract first <ul>...</ul> block
            match = re.search(r'<ul>.*?</ul>', content, re.DOTALL)
            if match:
                return source["label"], match.group()
        return source["label"], "<ul><li><em>Could not fetch content</em></li></ul>"
    except Exception as e:
        return source["label"], f"<ul><li><em>Error: {e}</em></li></ul>"


def fetch_web_digests():
    from concurrent.futures import ThreadPoolExecutor
    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fetch_one_web_digest, s): s["label"] for s in WEB_DIGEST_SOURCES}
        for f in futures:
            label, html = f.result()
            results[label] = html
    return results


# ── Send email ────────────────────────────────────────────────────────────────

def send_digest_email(gmail, to_address, html_content, subject):
    import email.mime.multipart
    import email.mime.text
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = to_address
    msg["To"] = to_address
    msg.attach(email.mime.text.MIMEText(html_content, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Morning Digest")
    parser.add_argument("--html", action="store_true", help="Open as HTML in browser")
    parser.add_argument("--email", action="store_true", help="Send digest to yourself via Gmail")
    args = parser.parse_args()

    now = datetime.now()
    print("Fetching digest...", end=" ", flush=True)
    creds = get_credentials()
    gmail   = build("gmail",    "v1", credentials=creds)
    cal     = build("calendar", "v3", credentials=creds)
    tasks_s = build("tasks",    "v1", credentials=creds)

    emails = fetch_unread_emails(gmail)
    events = fetch_week_events(cal)
    tasks  = fetch_tasks(tasks_s)
    yandex_emails, yandex_total = fetch_yandex_unread()

    print("triaging with Codex + fetching web digests...", end=" ", flush=True)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_gmail   = ex.submit(triage_emails, emails, "Gmail")
        f_yandex  = ex.submit(triage_emails, yandex_emails, "Yandex")
        f_web     = ex.submit(fetch_web_digests)
        gmail_important_idx  = f_gmail.result()
        yandex_important_idx = f_yandex.result()
        web_digests          = f_web.result()

    for i, e in enumerate(emails):
        e["important"] = i in gmail_important_idx
    for i, e in enumerate(yandex_emails):
        e["important"] = i in yandex_important_idx

    print("done.\n")

    if args.html or args.email:
        html = render_html(emails, yandex_emails, yandex_total, events, tasks, web_digests)

    if args.html:
        tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
        tmp.write(html)
        tmp.close()
        webbrowser.open(f"file://{tmp.name}")
        print(f"Opened in browser: {tmp.name}")

    if args.email:
        # Rebuild gmail service after long Codex triage to avoid connection reset
        creds = get_credentials()
        gmail = build("gmail", "v1", credentials=creds)
        profile = gmail.users().getProfile(userId="me").execute()
        address = profile["emailAddress"]
        subject = f"🌅 Morning Digest — {now.strftime('%A, %B %d %Y')}"
        send_digest_email(gmail, address, html, subject)
        print(f"✅ Digest sent to {address}")

    if not args.html and not args.email:
        render_terminal(emails, yandex_emails, yandex_total, events, tasks)


if __name__ == "__main__":
    main()
