#!/usr/bin/env python3
"""
Morning Digest — entry point.

Usage:
    python morning_digest.py            # terminal output
    python morning_digest.py --html     # open as HTML in browser
    python morning_digest.py --email    # send to yourself via Gmail

Add new digest sections:
  - Web sources: edit web_digest_sources.json
  - Email rules: edit categories.json, muted_senders.json, important_senders.json
  - New modules:  import and call from main(), add result to render_html()
"""

import argparse
import tempfile
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from googleapiclient.discovery import build

from config import CODEX_BIN  # noqa: F401 — ensures CODEX_BIN resolved at startup
from email_fetcher import (
    get_credentials,
    fetch_unread_emails,
    fetch_yandex_unread,
    triage_emails,
    send_email,
)
from calendar_tasks import fetch_week_events, fetch_tasks
from web_digest import fetch_web_digests
from renderer import render_terminal, render_html


def main():
    parser = argparse.ArgumentParser(description="Morning Digest")
    parser.add_argument("--html",  action="store_true", help="Open as HTML in browser")
    parser.add_argument("--email", action="store_true", help="Send digest via Gmail")
    args = parser.parse_args()

    now = datetime.now()
    print("Fetching digest...", end=" ", flush=True)

    creds   = get_credentials()
    gmail   = build("gmail",    "v1", credentials=creds)
    cal     = build("calendar", "v3", credentials=creds)
    tasks_s = build("tasks",    "v1", credentials=creds)

    emails                     = fetch_unread_emails(gmail)
    events                     = fetch_week_events(cal)
    tasks                      = fetch_tasks(tasks_s)
    yandex_emails, yandex_total = fetch_yandex_unread()

    print("triaging with Codex + fetching web digests...", end=" ", flush=True)
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_gmail  = ex.submit(triage_emails, emails,         "Gmail")
        f_yandex = ex.submit(triage_emails, yandex_emails,  "Yandex")
        f_web    = ex.submit(fetch_web_digests)
        gmail_idx  = f_gmail.result()
        yandex_idx = f_yandex.result()
        web_digests = f_web.result()

    for i, e in enumerate(emails):
        e["important"] = i in gmail_idx
    for i, e in enumerate(yandex_emails):
        e["important"] = i in yandex_idx

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
        # Rebuild service after long Codex run to avoid connection reset
        creds = get_credentials()
        gmail = build("gmail", "v1", credentials=creds)
        profile = gmail.users().getProfile(userId="me").execute()
        address = profile["emailAddress"]
        subject = f"🌅 Morning Digest — {now.strftime('%A, %B %d %Y')}"
        send_email(gmail, address, subject, html)
        print(f"✅ Digest sent to {address}")

    if not args.html and not args.email:
        render_terminal(emails, yandex_emails, yandex_total, events, tasks)


if __name__ == "__main__":
    main()
