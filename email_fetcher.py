"""email_fetcher.py — Gmail and Yandex email fetching, triage, and classification."""
import base64
import re
import os
import imaplib
import email as emaillib
import subprocess
import tempfile
import json
import urllib.parse
from datetime import date, timedelta
from email.header import decode_header as imap_decode_header
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import (
    SCOPES, CREDS_FILE, TOKEN_FILE,
    YANDEX_IMAP, YANDEX_USER, YANDEX_PASS,
    MUTED_SENDERS, ALWAYS_IMPORTANT, ALWAYS_IMPORTANT_HINTS,
    HIGH_PRIORITY_KEYWORDS, SCHOOL_SENDERS, CATEGORIES,
    load_prompt,
)


# ── Auth ──────────────────────────────────────────────────────────────────────

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


def build_services(creds):
    gmail  = build("gmail",    "v1", credentials=creds)
    cal    = build("calendar", "v3", credentials=creds)
    tasks  = build("tasks",    "v1", credentials=creds)
    return gmail, cal, tasks


# ── Classification ────────────────────────────────────────────────────────────

def classify_email(sender, subject):
    _, addr = parseaddr(sender)
    addr_lower   = addr.lower()
    sender_lower = sender.lower()

    if any(m in addr_lower for m in MUTED_SENDERS):
        return "📧 Other", False

    if any(a in addr_lower or a in sender_lower for a in ALWAYS_IMPORTANT):
        hints = ALWAYS_IMPORTANT_HINTS
        if any(k in addr_lower for k in hints.get("schools", [])):
            return "🏫 Schools", True
        if any(k in addr_lower for k in hints.get("financial", [])):
            return "🏦 Financial", True
        if any(k in addr_lower for k in hints.get("work", [])):
            return "🏢 Work", True
        if any(k in addr_lower for k in hints.get("security", [])):
            return "🔐 Security", True
        return "⚠️ Important", True

    subject_lower = subject.lower()

    if any(k in sender_lower for k in SCHOOL_SENDERS):
        return "🏫 Schools", True

    for cat, keywords in CATEGORIES.items():
        if any(k in sender_lower for k in keywords):
            is_important = any(k in subject_lower for k in HIGH_PRIORITY_KEYWORDS)
            return cat, is_important

    if any(k in subject_lower for k in HIGH_PRIORITY_KEYWORDS):
        return "⚠️ Important", True

    return "📧 Other", False


# ── Gmail ─────────────────────────────────────────────────────────────────────

def _get_body(payload):
    def decode(data):
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain" and part["body"].get("data"):
                return decode(part["body"]["data"])
        for part in payload["parts"]:
            if part["mimeType"] == "text/html" and part["body"].get("data"):
                return re.sub(r"<[^>]+>", " ", decode(part["body"]["data"]))
        for part in payload["parts"]:
            if "parts" in part:
                result = _get_body(part)
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
        sender  = headers.get("From", "?")
        date    = headers.get("Date", "?")
        _, addr = parseaddr(sender)
        body    = " ".join(_get_body(msg["payload"]).split())[:300]
        category, important = classify_email(sender, subject)
        emails.append({
            "id":        m["id"],
            "subject":   subject,
            "sender":    sender,
            "addr":      addr,
            "date":      date,
            "body":      body,
            "category":  category,
            "important": important,
            "link":      f"https://mail.google.com/mail/u/0/#all/{m['id']}",
            "link_app":  f"googlegmail:///mail/u/0/#all/{m['id']}",
        })
    return emails


# ── Yandex ────────────────────────────────────────────────────────────────────

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
        since_date = (date.today() - timedelta(days=1)).strftime("%d-%b-%Y")
        mail = imaplib.IMAP4_SSL(YANDEX_IMAP, 993)
        mail.login(YANDEX_USER, YANDEX_PASS)
        mail.select("INBOX")
        _, data = mail.search(None, f'(UNSEEN SINCE "{since_date}")')
        ids = data[0].split()
        emails = []
        for uid in ids[-50:]:
            _, msg_data = mail.fetch(uid, "(RFC822)")
            msg      = emaillib.message_from_bytes(msg_data[0][1])
            subject  = _imap_decode(msg["Subject"])
            sender   = _imap_decode(msg["From"])
            date_str = msg.get("Date", "")
            category, important = classify_email(sender, subject)
            search_q = urllib.parse.quote(subject[:80])
            emails.append({
                "subject":   subject,
                "sender":    sender,
                "date":      date_str,
                "body":      "",
                "category":  category,
                "important": important,
                "addr":      parseaddr(sender)[1],
                "link":      f"https://mail.yandex.com/#search?query={search_q}",
                "link_app":  f"yandexmail://search?query={search_q}",
            })
        mail.logout()
        return emails, len(ids)
    except Exception as e:
        print(f"  [Yandex fetch failed: {e}]")
        return [], 0


# ── Codex triage ──────────────────────────────────────────────────────────────

def triage_emails(email_list, label=""):
    """Run email list through Codex to identify which need attention."""
    if not email_list:
        return set()

    lines = "\n".join(
        f"{i}. Subject: {e['subject']} | From: {e['sender'].split('<')[0].strip()}"
        for i, e in enumerate(email_list)
    )
    prompt_template = load_prompt("triage_prompt.txt")
    prompt = prompt_template.replace("{email_list}", lines)

    try:
        out_file = tempfile.mktemp(suffix=".txt")
        subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-o", out_file, "-"],
            input=prompt, capture_output=True, text=True, timeout=180
        )
        if os.path.exists(out_file):
            text = open(out_file).read().strip()
            os.unlink(out_file)
            match = re.search(r'\[[\d,\s]*\]', text)
            if match:
                return set(json.loads(match.group()))
        print(f"  [Codex triage {label}: no valid response]")
    except Exception as e:
        print(f"  [Codex triage {label} failed: {e}]")
    return set()


def send_email(gmail, to_address, subject, html_content):
    import email.mime.multipart
    import email.mime.text
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = to_address
    msg["To"]      = to_address
    msg.attach(email.mime.text.MIMEText(html_content, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
