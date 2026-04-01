"""eventbrite_fetcher.py — Scrapes events from Eventbrite via JSON-LD (no API key needed)."""
import json
import re
import html
from datetime import datetime

import requests
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d")
    except Exception:
        return iso[:10] if iso else ""


def fetch_eventbrite(url: str, label: str, max_results: int = 7) -> tuple[str, str]:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        events = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            # itemListElement wrapping individual Event objects
            if isinstance(data, dict) and "itemListElement" in data:
                for item in data["itemListElement"]:
                    ev = item.get("item", {})
                    if ev.get("@type") not in ("Event", None) and "startDate" not in ev:
                        continue
                    name = ev.get("name", "").strip()
                    url_ = ev.get("url", "")
                    start = _fmt_date(ev.get("startDate", ""))
                    loc = ev.get("location", {})
                    venue = loc.get("name", "") if isinstance(loc, dict) else ""
                    if name:
                        events.append((name, url_, start, venue))
            elif isinstance(data, list):
                for ev in data:
                    if not isinstance(ev, dict):
                        continue
                    name = ev.get("name", "").strip()
                    url_ = ev.get("url", "")
                    start = _fmt_date(ev.get("startDate", ""))
                    loc = ev.get("location", {})
                    venue = loc.get("name", "") if isinstance(loc, dict) else ""
                    if name:
                        events.append((name, url_, start, venue))

        if not events:
            return label, "<ul><li><em>No events found.</em></li></ul>"

        items = ""
        for name, url_, start, venue in events[:max_results]:
            meta = " — ".join(filter(None, [start, venue]))
            link = f'<a href="{html.escape(url_)}">{html.escape(name)}</a>' if url_ else html.escape(name)
            items += f"<li>{link}{(' — ' + html.escape(meta)) if meta else ''}</li>"
        return label, f"<ul>{items}</ul>"

    except Exception as e:
        return label, f"<ul><li><em>Error: {e}</em></li></ul>"
