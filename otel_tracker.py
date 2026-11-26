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
ABANDONED_PR_DAYS = 30
REVERIFY_AFTER_S = 12 * 3600
MAINTAINER_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}
TASK_PATTERNS = [
    (re.compile(r"\b(document|documentation|docs?)\b.{0,60}\b(workaround|example|behaviou?r|usage|how to)\b", re.I | re.S), "documentation"),
    (re.compile(r"\b(should|could|need to|needs to|please|we can) document\b|\bdocument (this|these|it|the [a-z ]{0,30}) in [\w./-]+\.md\b", re.I), "documentation"),
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
    if issue.get("pull_request"):
        return
    if issue.get("state") != "open":
        c = state["candidates"].get(str(issue.get("id")))
        if c:
            c.update(status="closed", withheld_reasons=["not_open"], verified_at=_iso(now))
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

ROLE_TTL_S = 7 * 24 * 3600
ROLE_SECTIONS = re.compile(r"^(#{1,6})\s*(maintainers|approvers)\b[^\n]*$", re.I | re.M)
HANDLE_RE = re.compile(r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?=[)/\s\"'>\]]|$)")
DIRECTION_RE = re.compile(r"\b(pr|pull request|contribution)s?\b.{0,30}?\b(is|are|would be) (very )?(welcome|appreciated)|\bhappy to review\b|"
                          r"\bfeel free to (open|send|submit|pick|take)\b|\bgo ahead\b|\bwould (accept|welcome)\b|"
                          r"\bsounds good,? (please|go)\b|\bup for grabs\b|\bplease (open|send|submit) a (pr|pull request)\b", re.I)
RELEASE_RE = re.compile(r"\b(not|no longer) (working|going to work) on (this|it)\b|\bunassign(ing)? (me|myself)\b|"
                        r"\b(feel free|free) to take (this|it|over)\b|\bi('|’)?m (dropping|releasing) (this|it)\b", re.I)
OPEN_FOR_OTHERS_RE = re.compile(r"\bup for grabs\b|\banyone (can|is welcome to) (take|pick)\b|\bfree to take\b|"
                                r"\b(this|it) is (open|available) (again|for (anyone|others))\b", re.I)
MAX_PAGES = 5
MAX_LINKED_PRS = 5
PENDING_TTL_S = 14 * 24 * 3600


def _get_all(gh: GitHub, path: str, params: Optional[dict] = None) -> tuple[Optional[list], bool]:
    """Every page of a list endpoint. Returns (items, complete); a 404/410 or page cap is incomplete."""
    items, page = [], 1
    while page <= MAX_PAGES:
        rows = gh.get(path, {**(params or {}), "per_page": 100, "page": page})
        if rows is None:
            return (items or None), False
        items.extend(rows)
        if len(rows) < 100:
            return items, True
        page += 1
    return items, False


def _repo_roles(gh: GitHub, state: dict, repo: str, now: dt.datetime) -> Optional[dict]:
    """Maintainers and approvers of one repo from its README (cached), or None when unknown."""
    cache = state.setdefault("roles", {}).get(repo)
    if cache and (now - _parse(cache["at"])).total_seconds() < ROLE_TTL_S:
        return cache["handles"] or None
    data = gh.get(f"/repos/{repo}/readme")
    handles: dict = {}
    if isinstance(data, dict) and data.get("content"):
        import base64
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
        heads = list(re.finditer(r"^(#{1,6})\s*([^\n]*)$", text, re.M))
        for i, h in enumerate(heads):
            m = ROLE_SECTIONS.match(h.group(0))
            if not m:
                continue
            level, end = len(m.group(1)), len(text)
            for nxt in heads[i + 1:]:
                if len(nxt.group(1)) <= level or "emeritus" in nxt.group(2).lower():
                    end = nxt.start()
                    break
            for handle in HANDLE_RE.findall(text[h.end():end]):
                handles.setdefault(handle.lower(), m.group(2).lower().rstrip("s"))
    state["roles"][repo] = {"at": _iso(now), "handles": handles,
                            "source": f"https://github.com/{repo}#readme"}
    return handles or None


def _claims(comments: list) -> list[dict]:
    """Unresolved work claims by anyone (maintainers included). Age alone never resolves a claim."""
    open_claims: dict = {}
    for cm in comments:
        login = ((cm.get("user") or {}).get("login") or "").lower()
        body = cm.get("body") or ""
        if CLAIM_RE.search(body):
            open_claims[login] = {"by": login, "url": cm.get("html_url"), "at": cm.get("created_at")}
        if RELEASE_RE.search(body):
            open_claims.pop(login, None)
        if OPEN_FOR_OTHERS_RE.search(body) and cm.get("author_association") in MAINTAINER_ASSOC:
            open_claims.clear()
    return list(open_claims.values())


def _linked_prs(gh: GitHub, repo: str, num: int, timeline: list) -> tuple[list[dict], bool]:
    """Every PR that references the issue: timeline cross-references, connections and a direct PR search."""
    refs: dict = {}
    for ev in timeline:
        src = (ev.get("source") or {}).get("issue") or {}
        if ev.get("event") == "cross-referenced" and src.get("pull_request") is not None:
            prepo = (src.get("repository_url") or f"/repos/{repo}").split("/repos/")[-1]
            refs[(prepo, src.get("number"))] = src.get("html_url")
        elif ev.get("event") == "connected":
            refs.setdefault(("?connected", 0), None)
    complete = True
    found = gh.get("/search/issues", {"q": f"repo:{repo} is:pr {num} in:body,title", "per_page": 30}, search=True)
    if found is None:
        complete = False
    else:
        pat = re.compile(rf"(#{num}\b|/issues/{num}\b)")
        for it in found.get("items", []):
            if pat.search(f"{it.get('title', '')} {it.get('body') or ''}"):
                refs[(repo, it["number"])] = it.get("html_url")
        if found.get("incomplete_results"):
            complete = False
    prs = []
    if ("?connected", 0) in refs:
        refs.pop(("?connected", 0))
        prs.append({"state": "connected", "url": None})
    for (prepo, pnum), url in list(refs.items())[:MAX_LINKED_PRS]:
        pr = gh.get(f"/repos/{prepo}/pulls/{pnum}")
        if pr is None:
            complete = False
            continue
        prs.append({"repo": prepo, "number": pnum, "url": pr.get("html_url") or url, "state": pr.get("state"),
                    "merged": bool(pr.get("merged_at")), "closed_at": pr.get("closed_at")})
    if len(refs) > MAX_LINKED_PRS:
        complete = False
    return prs, complete


def _withhold_without_calls(c: dict, now: dt.datetime) -> bool:
    """An issue already assigned in the listing is unavailable; record it without spending API calls."""
    if not c.get("assignees"):
        return False
    c.update(evidence=[], verified_at=_iso(now), verified_issue_updated=c.get("issue_updated"),
             withheld_reasons=["assigned"], status="withheld")
    c["fingerprint"] = _fingerprint(c)
    return True


def _verify(gh: GitHub, state: dict, key: str, now: dt.datetime) -> None:
    """Full current verification of one candidate. Anything unconfirmed withholds the lead."""
    c = state["candidates"][key]
    if _withhold_without_calls(c, now):
        return
    repo, num = c["repo"], c["number"]
    reasons, evidence = [], []
    issue = gh.get(f"/repos/{repo}/issues/{num}")
    if issue is None:
        reasons.append("issue_unavailable")
    elif issue.get("state") != "open" or issue.get("pull_request"):
        c.update(status="closed", verified_at=_iso(now), withheld_reasons=["not_open"])
        c["fingerprint"] = _fingerprint(c)
        return
    else:
        c.update(title=issue.get("title", "")[:200], issue_updated=issue.get("updated_at"),
                 assignees=[a.get("login") for a in issue.get("assignees") or []],
                 author=((issue.get("user") or {}).get("login") or "").lower(),
                 author_association=issue.get("author_association"),
                 body_excerpt=(issue.get("body") or "")[:600],
                 body_hash=hashlib.sha256((issue.get("body") or "").encode()).hexdigest()[:12])
        if c["assignees"]:
            reasons.append("assigned")
    comments, ok_c = _get_all(gh, f"/repos/{repo}/issues/{num}/comments")
    timeline, ok_t = _get_all(gh, f"/repos/{repo}/issues/{num}/timeline")
    comments, timeline = comments or [], timeline or []
    if not (ok_c and ok_t):
        reasons.append("verification_incomplete")
    roles = _repo_roles(gh, state, repo, now)
    role_source = (state.get("roles", {}).get(repo) or {}).get("source")
    if roles is None:
        reasons.append("maintainer_roles_unavailable")
    else:
        if c.get("author") in roles and _signal({"title": c["title"], "body": c.get("body_excerpt"), "labels": []}):
            evidence.append({"kind": "maintainer_issue", "url": c["url"], "by": c["author"],
                             "role": roles[c["author"]], "role_source": role_source})
        for cm in comments:
            login = ((cm.get("user") or {}).get("login") or "").lower()
            if login in roles and DIRECTION_RE.search(cm.get("body") or ""):
                evidence.append({"kind": "maintainer_comment", "url": cm.get("html_url"), "by": login,
                                 "role": roles[login], "role_source": role_source,
                                 "excerpt": (cm.get("body") or "")[:240]})
        if not any(e["kind"].startswith("maintainer") for e in evidence):
            reasons.append("no_maintainer_direction")
    for claim in _claims(comments):
        reasons.append("claimed_in_comments")
        evidence.append({"kind": "claim", "url": claim["url"], "by": claim["by"]})
    prs, ok_p = _linked_prs(gh, repo, num, timeline)
    if not ok_p:
        reasons.append("verification_incomplete")
    for pr in prs:
        if pr["state"] == "connected":
            reasons.append("connected_pr")
            continue
        if pr["merged"]:
            reasons.append("fixed_by_merged_pr")
        elif pr["state"] == "open":
            reasons.append("open_pr")
        else:
            closed = _parse(pr.get("closed_at"))
            if closed and (now - closed).days < ABANDONED_PR_DAYS:
                reasons.append("recently_closed_pr")
        evidence.append({"kind": "linked_pr", "url": pr["url"], "state": pr["state"], "merged": pr["merged"]})
    if repo not in {state["repos"][r]["name"] for r in _eligible(state)}:
        reasons.append("repo_not_eligible")
    c["evidence"] = evidence
    c["verified_at"] = _iso(now)
    c["verified_run"] = state.get("run_id")
    c["verified_issue_updated"] = c.get("issue_updated")
    c["withheld_reasons"] = sorted(set(reasons))
    c["status"] = "withheld" if reasons else "verified"
    c["fingerprint"] = _fingerprint(c)


def _fingerprint(c: dict) -> str:
    material = {"title": c.get("title"), "kind": c.get("kind"), "assignees": c.get("assignees"),
                "body": c.get("body_hash"),
                "evidence": sorted((e.get("kind"), e.get("url"), e.get("state")) for e in c.get("evidence", []))}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def _needs_verify(c: dict, now: dt.datetime) -> bool:
    if c["status"] in ("new", "stale"):
        return True
    at = _parse(c.get("verified_at"))
    return c["status"] == "verified" and (not at or (now - at).total_seconds() > REVERIFY_AFTER_S)


def _shown(state: dict, key: str) -> list:
    v = state["shown"].get(key, [])
    return [v] if isinstance(v, str) else list(v)          # migrate the earlier single-fingerprint format


def _card(c: dict) -> dict:
    return {"repo": c["repo"], "number": c["number"], "title": c["title"], "url": c["url"], "kind": c["kind"],
            "requested_change": c.get("body_excerpt", "")[:280],
            "why_available": "Open and unassigned now, no open, merged or recently closed linked PR, no unresolved claim; "
                             "direction from a listed maintainer or approver.",
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
    """One bounded scan. Leads are re-verified in this run immediately before staging.

    staged_ids are immutable revision IDs ("<issue id>:<fingerprint>"). A pending revision survives
    later scans until mark_shown() acknowledges it or it expires, so a delayed receipt still
    acknowledges exactly the revision that was sent.
    """
    state_dir = Path(state_dir)
    now = now.astimezone(dt.timezone.utc)
    gh = GitHub(max_calls=max_calls, deadline_s=deadline_s, token=token, transport=transport, clock=clock)
    notes: list[str] = []
    with _locked(state_dir):
        state = _load(state_dir)
        state.setdefault("pending", {})
        state.setdefault("acks", {})
        import uuid
        state["run_id"] = uuid.uuid4().hex
        status = "ok"
        visited: list[str] = []
        leads, staged = [], []
        try:
            _refresh_inventory(gh, state, now)
            rotation_budget = max(1, int(max_calls * ROTATION_SHARE))
            verify_budget = max(6, int(max_calls * VERIFY_SHARE))
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
                if gh.left() < 6:
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
        run_at = _iso(now)
        if initial_pass and status != "unavailable":
            ranked = sorted((k for k, c in state["candidates"].items()
                             if c["status"] == "verified" and c.get("fingerprint") not in _shown(state, k)),
                            key=lambda k: state["candidates"][k].get("verified_at") or "", reverse=True)
            for key in ranked:
                if len(leads) >= MAX_CARDS:
                    break
                c = state["candidates"][key]
                if c.get("verified_run") != state["run_id"]:
                    # Leads are staged only from a verification made in this run.
                    try:
                        if gh.left() < 6:
                            raise BudgetExhausted
                        _verify(gh, state, key, now)
                    except (BudgetExhausted, GitHubUnavailable):
                        notes.append("a verified lead is waiting for re-verification before it can be shown")
                        status = "partial" if status == "ok" else status
                        break
                    if c["status"] != "verified" or c["fingerprint"] in _shown(state, key):
                        continue
                sid = f"{key}:{c['fingerprint']}"
                state["pending"].setdefault(sid, {"key": key, "fingerprint": c["fingerprint"], "staged_at": run_at})
                state["pending"][sid]["card"] = _card(c)
                leads.append(_card(c))
                staged.append(sid)
        elif not initial_pass:
            notes.append("initial pass over all eligible repositories not yet complete; leads withheld")
        for sid in [s for s, p in state["pending"].items()
                    if (now - _parse(p["staged_at"])).total_seconds() > PENDING_TTL_S]:
            state["pending"].pop(sid)
        coverage = {"repos_inventory": len(state["repos"]), "repos_eligible": len(eligible),
                    "repos_full_pass": len(done), "repos_pending_full_pass": len(eligible) - len(done),
                    "repos_scanned_this_run": len(visited),
                    "initial_pass_complete": initial_pass, "http_attempts": gh.calls,
                    "candidates": len(state["candidates"]),
                    "verified": sum(c["status"] == "verified" for c in state["candidates"].values()),
                    "withheld": sum(c["status"] == "withheld" for c in state["candidates"].values()),
                    "pending_revisions": len(state["pending"])}
        state["runs"].append({"at": run_at, "status": status, **coverage})
        _save(state_dir, state)
    return {"status": status, "leads": leads, "coverage": coverage, "staged_ids": staged, "notes": notes}


def mark_shown(state_dir, staged_ids, receipt_id: str) -> dict:
    """Acknowledge exactly the revisions a confirmed send contained.

    Returns {"confirmed": [...], "already": [...], "unknown": [...]}. Replaying the same receipt is
    idempotent ("already"). An unknown ID means the revision was never staged or has expired; the
    caller should keep that receipt pending rather than assume it was recorded.
    """
    state_dir = Path(state_dir)
    out = {"confirmed": [], "already": [], "unknown": []}
    with _locked(state_dir):
        state = _load(state_dir)
        state.setdefault("pending", {})
        state.setdefault("acks", {})
        for sid in staged_ids or []:
            if sid in state["acks"]:
                out["already"].append(sid)
                continue
            p = state["pending"].get(sid)
            if not p:
                out["unknown"].append(sid)
                continue
            shown = _shown(state, p["key"])
            if p["fingerprint"] not in shown:
                shown.append(p["fingerprint"])
            state["shown"][p["key"]] = shown
            state["acks"][sid] = {"receipt": receipt_id, "at": _iso(dt.datetime.now(dt.timezone.utc))}
            state["pending"].pop(sid)
            out["confirmed"].append(sid)
        _save(state_dir, state)
    return out
