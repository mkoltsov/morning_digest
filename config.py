"""config.py — loads credentials and filter lists from JSON files."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent


def _load_json(filename, default=None):
    p = BASE_DIR / filename
    if not p.exists():
        print(f"WARNING: {filename} not found, using default")
        return default if default is not None else []
    return json.loads(p.read_text())


def _load_creds_json():
    p = BASE_DIR / "credentials.json"
    if not p.exists():
        raise SystemExit(f"ERROR: credentials.json not found in {BASE_DIR}")
    return json.loads(p.read_text())


_CREDS = _load_creds_json()

CREDS_FILE       = Path(os.path.expanduser(_CREDS["gmail_client_secret"]))
TOKEN_FILE       = Path(os.path.expanduser(_CREDS["gmail_token"]))
YANDEX_IMAP      = _CREDS["yandex_imap"]
YANDEX_USER      = _CREDS["yandex_user"]
YANDEX_PASS      = _CREDS["yandex_password"]

MUTED_SENDERS    = _load_json("muted_senders.json")
ALWAYS_IMPORTANT = _load_json("important_senders.json")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
]

# ── Category / classification config (from categories.json) ──────────────────

_CAT = _load_json("categories.json", default={})

HIGH_PRIORITY_KEYWORDS = _CAT.get("high_priority_keywords", [])
SCHOOL_SENDERS         = _CAT.get("schools", {}).get("sender_keywords", [])
ALWAYS_IMPORTANT_HINTS = _CAT.get("always_important_category_hints", {})

# Build CATEGORIES dict: label -> list of sender keywords
CATEGORIES = {
    v["label"]: v["sender_keywords"]
    for k, v in _CAT.items()
    if isinstance(v, dict) and "sender_keywords" in v
}

# ── Prompts (from text files) ─────────────────────────────────────────────────

def load_prompt(filename):
    p = BASE_DIR / filename
    if not p.exists():
        raise SystemExit(f"ERROR: prompt file {filename} not found in {BASE_DIR}")
    return p.read_text()

