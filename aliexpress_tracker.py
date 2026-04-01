"""aliexpress_tracker.py — Extracts AliExpress shipping updates from Yandex inbox emails."""
import imaplib
import email as emaillib
import re
from datetime import date, timedelta
from email.header import decode_header as _decode_header

from config import YANDEX_IMAP, YANDEX_USER, YANDEX_PASS


def _imap_decode(s) -> str:
    if s is None:
        return ""
    result = []
    for part, enc in _decode_header(s):
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


# Yandex supports SUBJECT search reliably; FROM search with partial address fails
_SEARCH_SUBJECTS = ["package", "customs", "delivery update", "order shipped"]
_DONE_KW = ["has been delivered", "delivered successfully", "delivered to"]


def fetch_aliexpress_orders() -> tuple[str, str]:
    label = "📦 AliExpress — Packages En Route"
    try:
        since = (date.today() - timedelta(days=30)).strftime("%d-%b-%Y")
        mail = imaplib.IMAP4_SSL(YANDEX_IMAP, 993)
        mail.login(YANDEX_USER, YANDEX_PASS)
        mail.select("INBOX")

        # Collect message IDs from multiple subject searches, keep only numeric IDs
        all_ids: set[bytes] = set()
        for kw in _SEARCH_SUBJECTS:
            _, data = mail.search(None, f'(SUBJECT "{kw}" SINCE "{since}")')
            all_ids.update(x for x in data[0].split() if x.isdigit())

        tracking_latest: dict[str, dict] = {}

        for uid in sorted(all_ids):
            _, msg_data = mail.fetch(uid, "(RFC822)")
            msg = emaillib.message_from_bytes(msg_data[0][1])
            sender = _imap_decode(msg.get("From", ""))
            # Only process AliExpress emails
            if "aliexpress" not in sender.lower():
                continue
            subj = _imap_decode(msg.get("Subject", ""))
            date_str = msg.get("Date", "")[:20]

            trk_match = re.search(r'\b([A-Z]{2,4}[0-9]{8,20})\b', subj)
            trk_no = trk_match.group(1) if trk_match else f"ORDER_{uid.decode()}"

            status = re.sub(r'^Package\s+[A-Z0-9]+[:\s\-–]+', '', subj, flags=re.IGNORECASE).strip()
            status = status or subj

            if trk_no not in tracking_latest or date_str > tracking_latest[trk_no]["date"]:
                tracking_latest[trk_no] = {
                    "tracking": trk_no,
                    "status": status,
                    "date": date_str,
                    "delivered": any(k in subj.lower() for k in _DONE_KW),
                }

        mail.logout()

        in_transit = [v for v in tracking_latest.values() if not v["delivered"]]
        if not in_transit:
            return label, "<ul><li><em>No packages currently in transit.</em></li></ul>"

        items = "".join(
            f'<li><strong>{v["tracking"]}</strong> — {v["status"]} '
            f'<span style="color:#888;font-size:.85em">({v["date"]})</span></li>'
            for v in sorted(in_transit, key=lambda x: x["date"], reverse=True)
        )
        return label, f"<ul>{items}</ul>"

    except Exception as e:
        return label, f"<ul><li><em>Error: {e}</em></li></ul>"
