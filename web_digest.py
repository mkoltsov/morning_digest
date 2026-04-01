"""web_digest.py — Fetches web digests from external sites via Codex."""
import re
import os
import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE_DIR = Path(__file__).parent


def _load_sources():
    p = BASE_DIR / "web_digest_sources.json"
    if not p.exists():
        raise SystemExit(f"ERROR: web_digest_sources.json not found in {BASE_DIR}")
    return json.loads(p.read_text())


def fetch_one_web_digest(source):
    try:
        out_file = tempfile.mktemp(suffix=".txt")
        subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-o", out_file, "-"],
            input=source["prompt"], capture_output=True, text=True, timeout=180
        )
        if os.path.exists(out_file):
            content = open(out_file).read().strip()
            os.unlink(out_file)
            match = re.search(r'<ul>.*?</ul>', content, re.DOTALL)
            if match:
                return source["label"], match.group()
        return source["label"], "<ul><li><em>Could not fetch content</em></li></ul>"
    except Exception as e:
        return source["label"], f"<ul><li><em>Error: {e}</em></li></ul>"


def fetch_web_digests():
    sources = _load_sources()
    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fetch_one_web_digest, s): s["label"] for s in sources}
        for f in futures:
            label, html = f.result()
            results[label] = html
    return results
