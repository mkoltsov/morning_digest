"""web_digest.py — Fetches web digests from external sites via Codex or direct HTTP."""

import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aliexpress_tracker import fetch_aliexpress_orders
from eventbrite_fetcher import fetch_eventbrite
from circuit_breaker import CircuitBreaker
from config import CODEX_BIN

BASE_DIR = Path(__file__).parent

# One circuit breaker per source, keyed by label
_breakers: dict[str, CircuitBreaker] = {}

def _breaker(label: str) -> CircuitBreaker:
    if label not in _breakers:
        _breakers[label] = CircuitBreaker(label)
    return _breakers[label]


def _load_sources():
    p = BASE_DIR / "web_digest_sources.json"
    if not p.exists():
        raise SystemExit(f"ERROR: web_digest_sources.json not found in {BASE_DIR}")
    return json.loads(p.read_text())


def _extract_ul(content: str) -> str | None:
    m = re.search(r"<ul>.*?</ul>", content, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group()
    items = re.findall(r"<li>.*?</li>", content, re.DOTALL | re.IGNORECASE)
    if items:
        return "<ul>" + "".join(items) + "</ul>"
    lines = [l.strip() for l in content.splitlines() if re.match(r"^[-*•]\s+", l.strip())]
    if lines:
        lis = "".join(f"<li>{re.sub(r'^[-*•]\\s+', '', l)}</li>" for l in lines)
        return f"<ul>{lis}</ul>"
    return None


def _fetch_via_codex(source: dict) -> str:
    """Runs Codex for a source. RAISES on failure so circuit breaker can retry."""
    timeout = source.get("timeout", 300)
    out_file = tempfile.mktemp(suffix=".txt")
    try:
        result = subprocess.run(
            [CODEX_BIN, "exec", "--skip-git-repo-check", "--ephemeral", "-o", out_file, "-"],
            input=source["prompt"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if os.path.exists(out_file):
            content = open(out_file).read().strip()
            os.unlink(out_file)
            ul = _extract_ul(content)
            if ul:
                return ul
        raise RuntimeError(f"No usable output from Codex (exit {result.returncode})")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Codex timed out after {timeout}s")
    finally:
        if os.path.exists(out_file):
            os.unlink(out_file)


def _fetch_via_http(source: dict) -> str:
    """Scrapes via HTTP. RAISES on failure."""
    _, html = fetch_eventbrite(source["url"], source["label"])
    if "<em>Error" in html or "<em>No events" in html:
        raise RuntimeError(html)
    return html


def fetch_one_web_digest(source: dict) -> tuple[str, str]:
    label = source["label"]
    cb = _breaker(label)
    if source.get("url"):
        _ok, result = cb.call(_fetch_via_http, source)
    else:
        _ok, result = cb.call(_fetch_via_codex, source)
    return label, result


def fetch_web_digests():
    sources = _load_sources()
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch_one_web_digest, s): s["label"] for s in sources}
        ali_future = ex.submit(fetch_aliexpress_orders)
        for f in futures:
            label, html = f.result()
            results[label] = html
        label, html = ali_future.result()
        results[label] = html
    return results
