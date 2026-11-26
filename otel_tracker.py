"""OpenTelemetry contribution-lead tracker for the Morning Digest (private trial first).

Finds small, available, maintainer-directed tasks across every official
open-telemetry repository, verifies each candidate before recommending it, and
returns at most two cards per Digest. Read-only against GitHub.

Interface used by the Digest:

    collect(state_dir, now, *, max_calls=45, deadline_s=120, token=None) -> dict
        {"status": "ok" | "partial" | "unavailable",
         "leads": [card, ...],            # at most MAX_CARDS, verified, new or materially changed
         "coverage": {...},               # what was actually checked
         "staged_ids": [lead_key, ...],   # pass to mark_shown after a confirmed send
         "notes": [str, ...]}
    mark_shown(state_dir, staged_ids, receipt_id) -> int

Leads are only staged by collect(); they become "shown" only when the caller
reports Gmail acceptance or Sent reconciliation through mark_shown(). An
unchanged lead stays quiet; a material change can surface again.

Budget: every HTTP attempt counts against max_calls, a monotonic deadline
bounds wall time, and GitHub rate-limit headers reduce the budget further.
Work that does not fit is kept for the next run and reported as deferred.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

ORG = "open-telemetry"
API = "https://api.github.com"
STATE_FILE = "otel_tracker_state.json"
LOCK_FILE = ".otel_tracker.lock"
MAX_CARDS = 2
INVENTORY_TTL_S = 24 * 3600
RECENT_WINDOW_OVERLAP_S = 2 * 3600       # overlapping search windows: never miss an edge update
ROTATION_SHARE = 0.4                     # budget reserved for the repo rotation
VERIFY_SHARE = 0.3                       # budget reserved for verifying candidates
CLAIM_WINDOW_DAYS = 60
ABANDONED_PR_DAYS = 30
REVERIFY_AFTER_S = 12 * 3600
MAINTAINER_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}
TASK_PATTERNS = [
    (re.compile(r"\b(document|documentation|docs?)\b.{0,60}\b(workaround|example|behaviou?r|usage|how to)\b", re.I | re.S), "documentation"),
    (re.compile(r"\b(add|write|missing)\b.{0,40}\b(regression |unit |integration )?tests?\b", re.I | re.S), "test"),
    (re.compile(r"\b(release|publish|ci|workflow|github actions?)\b.{0,50}\b(fix|broken|fail|repair|update)\b", re.I | re.S), "ci_release"),
    (re.compile(r"\b(typo|broken link|dead link|outdated)\b", re.I), "small_fix"),
]
HELP_LABELS = {"good first issue", "help wanted", "documentation", "docs", "contribfest", "easy", "good-first-issue"}
CLAIM_RE = re.compile(r"\b(i('| a)?m (working|taking)|i('|’)ll (take|work|pick)|i('|’)d like to (work|take|pick)|"
                      r"can i (work|take|pick)|assign (it to )?me|please assign|working on (this|it))\b", re.I)


class BudgetExhausted(Exception):
    """The call budget, deadline, or GitHub quota for this run is used up."""


class GitHubUnavailable(Exception):
    """GitHub cannot be reached or refused the credentials; nothing could be checked."""


class _Response:
    def __init__(self, status: int, data, headers: dict):
        self.status, self.data, self.headers = status, data, {k.lower(): v for k, v in headers.items()}


def _urllib_transport(url: str, headers: dict, timeout: float) -> _Response:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return _Response(r.status, json.loads(body) if body else None, dict(r.headers))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            data = json.loads(body)
        except ValueError:
            data = {"message": body[:200]}
        return _Response(e.code, data, dict(e.headers or {}))


class GitHub:
    """Bounded, read-only GitHub REST client."""

    def __init__(self, *, max_calls: int, deadline_s: float, token: Optional[str],
                 transport: Callable = _urllib_transport, clock: Callable = time.monotonic):
        self.max_calls, self.calls, self.token = max_calls, 0, token
        self.transport, self.clock = transport, clock
        self.deadline = clock() + deadline_s
        self.remaining_quota: Optional[int] = None
        self.search_remaining: Optional[int] = None
        self.incomplete = False

    def left(self) -> int:
        left = self.max_calls - self.calls
        if self.remaining_quota is not None:
            left = min(left, max(0, self.remaining_quota - 5))   # keep a small quota reserve
        return max(0, left)

    def get(self, path: str, params: Optional[dict] = None, *, search: bool = False):
        if self.left() <= 0 or self.clock() >= self.deadline:
            raise BudgetExhausted
        if search and self.search_remaining is not None and self.search_remaining <= 1:
            raise BudgetExhausted
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "morning-digest-otel-tracker"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        self.calls += 1
        timeout = max(1.0, min(20.0, self.deadline - self.clock()))
        try:
            r = self.transport(url, headers, timeout)
        except (OSError, ValueError) as exc:
            raise GitHubUnavailable(type(exc).__name__) from None
        quota = r.headers.get("x-ratelimit-remaining")
        if quota is not None and str(quota).isdigit():
            if r.headers.get("x-ratelimit-resource") == "search":
                self.search_remaining = int(quota)
            else:
                self.remaining_quota = int(quota)
        if r.status in (401,):
            raise GitHubUnavailable("unauthorized")
        if r.status in (403, 429) and (quota == "0" or "rate limit" in str((r.data or {}).get("message", "")).lower()):
            raise BudgetExhausted
        if r.status >= 500:
            raise GitHubUnavailable(f"http {r.status}")
        if r.status == 422 and search:            # e.g. search beyond 1,000 results
            self.incomplete = True
            return None
        if r.status in (404, 410):
            return None
        if r.status != 200:
            raise GitHubUnavailable(f"http {r.status}")
        if search and isinstance(r.data, dict) and r.data.get("incomplete_results"):
            self.incomplete = True
        return r.data


# ---------------------------------------------------------------- state

@contextlib.contextmanager
def _locked(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / LOCK_FILE, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load(state_dir: Path) -> dict:
    try:
        state = json.loads((state_dir / STATE_FILE).read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    state.setdefault("version", 1)
    for key in ("repos", "checkpoints", "candidates", "shown", "runs"):
        state.setdefault(key, {} if key != "runs" else [])
    state.setdefault("rotation_cursor", 0)
    state.setdefault("staged", {})
    return state


def _save(state_dir: Path, state: dict) -> None:
    state["runs"] = state["runs"][-60:]
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".otel-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, state_dir / STATE_FILE)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- inventory

def _refresh_inventory(gh: GitHub, state: dict, now: dt.datetime) -> bool:
    """Page through the org's repos. Returns True when the inventory is complete."""
    inv_at = _parse(state.get("inventory_at"))
    if inv_at and (now - inv_at).total_seconds() < INVENTORY_TTL_S and state.get("inventory_complete"):
        return True
    page = state.get("inventory_next_page", 1)
    seen = set(state.get("inventory_seen", [])) if page > 1 else set()
    while True:
        rows = gh.get(f"/orgs/{ORG}/repos", {"type": "public", "per_page": 100, "page": page, "sort": "full_name"})
        rows = rows or []
        for r in rows:
            rid = str(r["id"])
            seen.add(rid)
            entry = state["repos"].setdefault(rid, {})
            entry.update(name=r["full_name"], archived=bool(r.get("archived")),
                         has_issues=bool(r.get("has_issues", True)), language=r.get("language"),
                         open_issues=r.get("open_issues_count", 0))
        state["inventory_next_page"], state["inventory_seen"] = page + 1, sorted(seen)
        if len(rows) < 100:
            break
        page += 1
    for rid in list(state["repos"]):
        if rid not in seen:
            state["repos"][rid]["removed"] = True
    state.update(inventory_at=_iso(now), inventory_complete=True, inventory_next_page=1, inventory_seen=[])
    return True


def _eligible(state: dict) -> list[str]:
    return sorted(rid for rid, r in state["repos"].items()
                  if not r.get("archived") and r.get("has_issues") and not r.get("removed"))


# ---------------------------------------------------------------- discovery

def _signal(issue: dict) -> Optional[str]:
    labels = {(lb.get("name") or "").lower() for lb in issue.get("labels", [])}
    text = f"{issue.get('title', '')}\n{issue.get('body') or ''}"
    for rx, kind in TASK_PATTERNS:
        if rx.search(text):
            return kind
    if labels & HELP_LABELS:
        return "labelled"
    return None


BOT_TITLES = re.compile(r"^(dependency dashboard|renovate|dependabot)\b", re.I)


def _is_bot(issue: dict) -> bool:
    user = issue.get("user") or {}
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]") or bool(BOT_TITLES.match(issue.get("title") or ""))


def _priority(c: dict) -> tuple:
    """Verify the strongest candidates first: maintainer-raised, help-labelled, then most recent."""
    return (c.get("author_association") in MAINTAINER_ASSOC, c.get("kind") == "labelled" or bool(c.get("help_label")),
            c.get("issue_updated") or "")


def _consider(state: dict, issue: dict, repo_name: str, now: dt.datetime) -> None:
    if issue.get("pull_request") or issue.get("state") != "open":
        return
    if _is_bot(issue):
        state["candidates"].pop(str(issue["id"]), None)
        return
    kind = _signal(issue)
    if not kind:
        return
    key = str(issue["id"])
    c = state["candidates"].setdefault(key, {"status": "new", "first_seen": _iso(now)})
    c.update(repo=repo_name, number=issue["number"], title=issue.get("title", "")[:200],
             url=issue.get("html_url"), kind=kind, issue_updated=issue.get("updated_at"),
             assignees=[a.get("login") for a in issue.get("assignees") or []],
             author_association=issue.get("author_association"),
             help_label=bool({(lb.get("name") or "").lower() for lb in issue.get("labels", [])} & HELP_LABELS),
             body_excerpt=(issue.get("body") or "")[:600])
    if c["status"] in ("verified", "withheld") and c.get("verified_issue_updated") != issue.get("updated_at"):
        c["status"] = "stale"


def _search_recent(gh: GitHub, state: dict, now: dt.datetime) -> None:
    since = _parse(state.get("recent_since")) or (now - dt.timedelta(days=14))
    since -= dt.timedelta(seconds=RECENT_WINDOW_OVERLAP_S)
    names = {r["name"]: r for r in state["repos"].values()}
    q = f"org:{ORG} is:issue is:open updated:>={since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    page = 1
    while page <= 3:
        data = gh.get("/search/issues", {"q": q, "sort": "updated", "order": "desc", "per_page": 100, "page": page}, search=True)
        if not data:
            break
        for it in data.get("items", []):
            repo_name = it.get("repository_url", "").split("/repos/")[-1]
            if repo_name in names and not names[repo_name].get("archived"):
                _consider(state, it, repo_name, now)
        if len(data.get("items", [])) < 100:
            state["recent_since"] = _iso(now)
            return
        page += 1
    gh.incomplete = True          # more than 300 recent updates: next run continues from the same window


def _rotate(gh: GitHub, state: dict, now: dt.datetime, budget: int) -> list[str]:
    """Scan quieter repos' open issues (oldest first after the checkpoint)."""
    eligible = _eligible(state)
    if not eligible:
        return []
    visited = []
    cursor = state["rotation_cursor"] % len(eligible)
    for step in range(len(eligible)):
        if budget <= 0:
            break
        rid = eligible[(cursor + step) % len(eligible)]
        repo = state["repos"][rid]
        cp = state["checkpoints"].setdefault(rid, {})
        params = {"state": "open", "sort": "created", "direction": "asc", "per_page": 100, "page": cp.get("next_page", 1)}
        data = gh.get(f"/repos/{repo['name']}/issues", params)
        budget -= 1
        rows = data or []
        for it in rows:
            _consider(state, it, repo["name"], now)
        # Advance the checkpoint only after this page's results are stored in state.
        if len(rows) < 100:
            cp.update(next_page=1, last_full_pass=_iso(now))
        else:
            cp["next_page"] = params["page"] + 1
        cp["last_scan"] = _iso(now)
        visited.append(rid)
        state["rotation_cursor"] = (cursor + step + 1) % len(eligible)
    return visited


# ---------------------------------------------------------------- verification

def _withhold_without_calls(c: dict, now: dt.datetime) -> bool:
    """An issue already assigned in the listing is unavailable; record it without spending API calls."""
    if not c.get("assignees"):
        return False
    c.update(evidence=[], verified_at=_iso(now), verified_issue_updated=c.get("issue_updated"),
             withheld_reasons=["assigned"], status="withheld")
    c["fingerprint"] = _fingerprint(c)
    return True


def _verify(gh: GitHub, state: dict, key: str, now: dt.datetime) -> None:
    c = state["candidates"][key]
    if _withhold_without_calls(c, now):
        return
    repo, num = c["repo"], c["number"]
    comments = gh.get(f"/repos/{repo}/issues/{num}/comments", {"per_page": 100}) or []
    timeline = gh.get(f"/repos/{repo}/issues/{num}/timeline", {"per_page": 100}) or []
    reasons, evidence = [], []
    if c.get("assignees"):
        reasons.append("assigned")
    direction = c.get("author_association") in MAINTAINER_ASSOC
    if direction:
        evidence.append({"kind": "maintainer_issue", "url": c["url"]})
    cutoff = now - dt.timedelta(days=CLAIM_WINDOW_DAYS)
    for cm in comments:
        assoc = cm.get("author_association")
        if assoc in MAINTAINER_ASSOC:
            direction = True
            evidence.append({"kind": "maintainer_comment", "url": cm.get("html_url"), "excerpt": (cm.get("body") or "")[:240]})
        elif CLAIM_RE.search(cm.get("body") or "") and (_parse(cm.get("created_at")) or now) >= cutoff:
            reasons.append("claimed_in_comments")
    for ev in timeline:
        if ev.get("event") == "cross-referenced":
            src = (ev.get("source") or {}).get("issue") or {}
            if not src.get("pull_request"):
                continue
            pr = src.get("pull_request") or {}
            if pr.get("merged_at"):
                reasons.append("fixed_by_merged_pr")
            elif src.get("state") == "open":
                reasons.append("open_pr")
            else:
                closed = _parse(src.get("closed_at"))
                if closed and (now - closed).days < ABANDONED_PR_DAYS:
                    reasons.append("recently_closed_pr")
            evidence.append({"kind": "linked_pr", "url": src.get("html_url"), "state": src.get("state"),
                             "merged": bool(pr.get("merged_at"))})
        elif ev.get("event") in ("assigned", "connected"):
            if ev.get("event") == "connected":
                reasons.append("connected_pr")
    if len(comments) >= 100 or len(timeline) >= 100:
        reasons.append("incomplete_pagination")
    if not direction:
        reasons.append("no_maintainer_direction")
    c["evidence"] = evidence
    c["verified_at"] = _iso(now)
    c["verified_issue_updated"] = c.get("issue_updated")
    c["withheld_reasons"] = sorted(set(reasons))
    c["status"] = "withheld" if reasons else "verified"
    c["fingerprint"] = _fingerprint(c)


def _fingerprint(c: dict) -> str:
    material = {"title": c.get("title"), "kind": c.get("kind"), "assignees": c.get("assignees"),
                "evidence": sorted((e.get("kind"), e.get("url"), e.get("state")) for e in c.get("evidence", []))}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def _needs_verify(c: dict, now: dt.datetime) -> bool:
    if c["status"] in ("new", "stale"):
        return True
    at = _parse(c.get("verified_at"))
    return c["status"] == "verified" and (not at or (now - at).total_seconds() > REVERIFY_AFTER_S)


def _card(c: dict) -> dict:
    return {"repo": c["repo"], "number": c["number"], "title": c["title"], "url": c["url"], "kind": c["kind"],
            "requested_change": c.get("body_excerpt", "")[:280],
            "why_available": "No assignee, no open or merged linked PR, no recent claim; maintainer direction present.",
            "next_step": {"documentation": "Draft the doc/example change described in the issue and open a PR.",
                          "test": "Add the regression test described in the issue and open a PR.",
                          "ci_release": "Reproduce the CI/release failure and propose the fix.",
                          "small_fix": "Make the small fix and open a PR.",
                          "labelled": "Confirm the scope with the maintainer comment, then open a PR."}[c["kind"]],
            "evidence": c.get("evidence", []), "verified_at": c.get("verified_at")}


# ---------------------------------------------------------------- public API

def collect(state_dir, now: dt.datetime, *, max_calls: int = 45, deadline_s: float = 120,
            token: Optional[str] = None, transport: Callable = _urllib_transport,
            clock: Callable = time.monotonic) -> dict:
    state_dir = Path(state_dir)
    now = now.astimezone(dt.timezone.utc)
    gh = GitHub(max_calls=max_calls, deadline_s=deadline_s, token=token, transport=transport, clock=clock)
    notes: list[str] = []
    with _locked(state_dir):
        state = _load(state_dir)
        status = "ok"
        visited: list[str] = []
        try:
            _refresh_inventory(gh, state, now)
            rotation_budget = max(1, int(max_calls * ROTATION_SHARE))
            verify_budget = max(2, int(max_calls * VERIFY_SHARE))
            try:
                _search_recent(gh, state, now)
            except BudgetExhausted:
                status = "partial"
            try:
                visited = _rotate(gh, state, now, min(rotation_budget, max(0, gh.left() - verify_budget)))
            except BudgetExhausted:
                status = "partial"
            pending = sorted((k for k, c in state["candidates"].items() if _needs_verify(c, now)),
                             key=lambda k: _priority(state["candidates"][k]), reverse=True)
            for key in pending:
                if _withhold_without_calls(state["candidates"][key], now):
                    continue
                if gh.left() < 2:
                    status = "partial"
                    notes.append(f"{len(pending)} candidate(s) waiting for verification")
                    break
                _verify(gh, state, key, now)
        except BudgetExhausted:
            status = "partial"
        except GitHubUnavailable as exc:
            status = "unavailable" if gh.calls <= 1 else "partial"
            notes.append(f"GitHub unavailable: {exc}")
        if gh.incomplete and status == "ok":
            status = "partial"
            notes.append("GitHub returned incomplete or truncated results")

        eligible = _eligible(state)
        done = [r for r in eligible if state["checkpoints"].get(r, {}).get("last_full_pass")]
        initial_pass = bool(eligible) and len(done) == len(eligible)
        leads, staged = [], []
        if initial_pass:
            for key, c in sorted(state["candidates"].items(), key=lambda kv: kv[1].get("verified_at") or "", reverse=True):
                if c["status"] != "verified" or state["shown"].get(key) == c.get("fingerprint"):
                    continue
                leads.append(_card(c))
                staged.append(key)
                if len(leads) >= MAX_CARDS:
                    break
        else:
            notes.append("initial pass over all eligible repositories not yet complete; leads withheld")
        state["staged"] = {k: state["candidates"][k]["fingerprint"] for k in staged}
        coverage = {"repos_inventory": len(state["repos"]), "repos_eligible": len(eligible),
                    "repos_full_pass": len(done), "repos_pending_full_pass": len(eligible) - len(done),
                    "repos_scanned_this_run": len(visited),
                    "initial_pass_complete": initial_pass, "http_attempts": gh.calls,
                    "candidates": len(state["candidates"]),
                    "verified": sum(c["status"] == "verified" for c in state["candidates"].values()),
                    "withheld": sum(c["status"] == "withheld" for c in state["candidates"].values())}
        state["runs"].append({"at": _iso(now), "status": status, **coverage})
        _save(state_dir, state)
    return {"status": status, "leads": leads, "coverage": coverage, "staged_ids": staged, "notes": notes}


def mark_shown(state_dir, staged_ids, receipt_id: str) -> int:
    """Record leads as shown after a confirmed send. Only staged leads with an unchanged fingerprint count."""
    state_dir = Path(state_dir)
    with _locked(state_dir):
        state = _load(state_dir)
        n = 0
        for key in staged_ids or []:
            fp = state["staged"].get(key)
            if fp and state["candidates"].get(key, {}).get("fingerprint") == fp:
                state["shown"][key] = fp
                state["candidates"][key]["shown_receipt"] = receipt_id
                n += 1
        state["staged"] = {k: v for k, v in state["staged"].items() if k not in set(staged_ids or [])}
        _save(state_dir, state)
    return n
