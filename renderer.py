"""renderer.py — Terminal and HTML rendering for the morning digest."""
import html as _html
from datetime import datetime
from email.utils import parseaddr


# ── Helpers ───────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return _html.escape(str(s))


def _sender_name(s: str) -> str:
    name = s.split("<")[0].strip().strip('"')
    return name or s


def _make_link(e: dict, color: str = "#1a0dab") -> str:
    link = e.get("link", "")
    subj = _esc(e["subject"])
    if not link:
        return f"<b>{subj}</b>"
    return f'<b><a href="{link}" style="color:{color};text-decoration:none">{subj}</a></b>'


def _fmt_event_dt(start: dict) -> str:
    raw = start.get("dateTime", start.get("date", ""))
    try:
        return datetime.fromisoformat(raw).strftime("%a %b %d  %I:%M %p")
    except Exception:
        return raw


# ── Terminal renderer ─────────────────────────────────────────────────────────

def render_terminal(emails, yandex_emails, yandex_total, events, tasks):
    now = datetime.now()
    print(f"\n{'═'*64}")
    print(f"  🌅 MORNING DIGEST  —  {now.strftime('%A, %B %d %Y')}")
    print(f"{'═'*64}\n")

    for label, mail_list in [("📬 GMAIL", emails), ("📬 YANDEX", yandex_emails)]:
        important = [e for e in mail_list if e["important"] or e["category"] != "📧 Other"]
        other     = [e for e in mail_list if not e["important"] and e["category"] == "📧 Other"]
        total     = yandex_total if "YANDEX" in label else len(mail_list)
        print(f"{label} — {total} unread\n")
        if important:
            print("  ⭐ NEEDS ATTENTION\n")
            for e in important:
                flag = "🔴" if e["important"] else "🟡"
                name = _sender_name(e["sender"])
                print(f"  {flag} {e['category']}  {e['subject']}")
                print(f"     {name}")
                if e["category"] == "🏫 Schools" and e.get("body"):
                    print(f"     ↳ {' '.join(e['body'].split())[:120]}")
                print()
        if other:
            print(f"  📋 OTHER ({len(other)}) — skimming\n")
            for e in other:
                print(f"     • {e['subject']}  [{_sender_name(e['sender'])}]")
            print()
        print(f"{'─'*64}\n")

    print(f"📅 THIS WEEK ({len(events)} events)\n")
    if not events:
        print("  No events.\n")
    for ev in events:
        loc = f"  @ {ev['location'].split(',')[0]}" if ev.get("location") else ""
        print(f"  📌 {_fmt_event_dt(ev['start'])}  —  {ev.get('summary','(no title)')}{loc}")
    print()

    print(f"{'─'*64}")
    print(f"\n✅ OPEN TASKS ({len(tasks)})\n")
    if not tasks:
        print("  None.\n")
    for t in tasks:
        due = f"  ⚠️ due {t['due'][:10]}" if t.get("due") else ""
        print(f"  ⬜ {t.get('title','(no title)')}{due}  [{t['_list']}]")
    print(f"\n{'═'*64}\n")


# ── HTML renderer ─────────────────────────────────────────────────────────────

def _render_email_table(mail_list: list) -> tuple[str, str]:
    """Returns (rows_important_html, rows_other_html)."""
    important = [e for e in mail_list if e["important"] or e["category"] != "📧 Other"]
    other     = [e for e in mail_list if not e["important"] and e["category"] == "📧 Other"]

    rows_imp = ""
    for e in important:
        flag = "🔴" if e["important"] else "🟡"
        preview = ""
        if e["category"] == "🏫 Schools" and e.get("body"):
            preview = f'<div class="preview">{_esc(" ".join(e["body"].split())[:150])}</div>'
        rows_imp += f"""
        <tr class="imp">
          <td class="cat">{flag} {e['category']}</td>
          <td>{_make_link(e)}{preview}</td>
          <td class="from">{_esc(_sender_name(e['sender']))}</td>
        </tr>"""

    rows_other = "".join(
        f'<li>{_make_link(e, "#555")} <span class="from">[{_esc(_sender_name(e["sender"]))}]</span></li>'
        for e in other
    )
    return rows_imp, rows_other, len(important), len(other)


def _render_web_digests(web_digests: dict) -> str:
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


_CSS = """
    *    { box-sizing:border-box; }
    body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
           font-size:15px; max-width:640px; margin:0 auto; padding:12px;
           background:#f4f4f4; color:#222; }
    h1   { font-size:1.2em; margin:8px 0 16px; color:#333; }
    h2   { font-size:.95em; text-transform:uppercase; letter-spacing:.05em;
           color:#4285f4; margin:20px 0 6px; }
    .card { background:#fff; border-radius:10px; padding:12px 14px;
             box-shadow:0 1px 3px rgba(0,0,0,.08); margin-bottom:12px; }
    table { width:100%; border-collapse:collapse; }
    td    { padding:6px 4px; border-bottom:1px solid #f0f0f0; vertical-align:top; font-size:.88em; }
    tr:last-child td { border-bottom:none; }
    tr.imp td { background:#fff8e1; }
    .cat  { white-space:nowrap; padding-right:8px; font-size:.8em; }
    .from { color:#888; font-size:.82em; }
    .due  { color:#d32f2f; font-size:.82em; }
    .preview { color:#555; font-size:.82em; margin-top:2px; }
    ul    { margin:0; padding:0 0 0 18px; }
    li    { padding:4px 0; font-size:.88em; border-bottom:1px solid #f0f0f0; }
    li:last-child { border-bottom:none; }
    .card ul { padding-left: 16px; }
    .card li  { list-style: disc; }
"""


def render_html(emails, yandex_emails, yandex_total, events, tasks, web_digests=None):
    now = datetime.now()

    g_imp, g_other, g_imp_n, g_other_n = _render_email_table(emails)
    y_imp, y_other, y_imp_n, y_other_n = _render_email_table(yandex_emails)

    event_rows = "".join(
        f'<li><b>{_esc(ev.get("summary",""))}</b>  {_fmt_event_dt(ev["start"])}'
        f'{"  <span class=from>@ " + _esc(ev["location"].split(",")[0]) + "</span>" if ev.get("location") else ""}'
        f'</li>'
        for ev in events
    )
    task_rows = "".join(
        f'<li>⬜ {_esc(t.get("title",""))}'
        f'{"  <span class=due>⚠️ due " + t["due"][:10] + "</span>" if t.get("due") else ""}'
        f' <span class="from">[{_esc(t["_list"])}]</span></li>'
        for t in tasks
    )

    g_other_section = (
        f"<h2>📧 Gmail — Other ({g_other_n})</h2>"
        f"<div class='card'><ul>{g_other}</ul></div>" if g_other else ""
    )
    y_other_section = (
        f"<h2>📧 Yandex — Other ({y_other_n})</h2>"
        f"<div class='card'><ul>{y_other}</ul></div>" if y_other else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Digest — {now.strftime('%b %d')}</title>
  <style>{_CSS}</style>
</head>
<body>
  <h1>🌅 {now.strftime('%A, %B %d %Y')}</h1>

  <h2>📧 Gmail — ⭐ Needs Attention ({g_imp_n} of {len(emails)} unread)</h2>
  <div class="card">
    <table>{g_imp or '<tr><td colspan="3" class="from">Nothing urgent</td></tr>'}</table>
  </div>
  {g_other_section}

  <h2>📧 Yandex — ⭐ Needs Attention ({y_imp_n} of {yandex_total} unread)</h2>
  <div class="card">
    <table>{y_imp or '<tr><td colspan="3" class="from">Nothing urgent</td></tr>'}</table>
  </div>
  {y_other_section}

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
