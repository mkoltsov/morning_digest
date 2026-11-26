"""Offline tests for otel_tracker: a fake GitHub transport, no network."""
import datetime as dt
import json
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import otel_tracker as ot  # noqa: E402

NOW = dt.datetime(2026, 11, 26, 13, 0, tzinfo=dt.timezone.utc)
OLD = "2026-04-10T10:00:00Z"


def repo(i, name, **kw):
    return {"id": i, "full_name": f"open-telemetry/{name}", "archived": kw.get("archived", False),
            "has_issues": kw.get("has_issues", True), "language": kw.get("language", "Python"), "open_issues_count": 1}


def issue(i, num, repo_name, title, body, *, assoc="MEMBER", labels=(), assignees=(), updated=OLD, pr=False):
    d = {"id": i, "number": num, "title": title, "body": body, "state": "open", "author_association": assoc,
         "labels": [{"name": n} for n in labels], "assignees": [{"login": a} for a in assignees],
         "updated_at": updated, "html_url": f"https://github.com/{repo_name}/issues/{num}",
         "repository_url": f"https://api.github.com/repos/{repo_name}"}
    if pr:
        d["pull_request"] = {"url": "x"}
    return d


DOC_5139 = issue(5139, 5139, "open-telemetry/opentelemetry-python",
                 "Logging handler drops extra attributes",
                 "We should document the workaround for passing extra attributes until the API changes.")
JAVA_TEST = issue(901, 77, "open-telemetry/opentelemetry-java",
                  "Add regression test for span limits", "Please add a regression test covering attribute limits.",
                  labels=("help wanted",))
UNRELATED = issue(902, 78, "open-telemetry/opentelemetry-java", "Question about exporters", "How do I configure x?",
                  assoc="NONE")


class FakeGitHub:
    def __init__(self, repos, issues, *, comments=None, timelines=None, fail=None, quota=5000,
                 incomplete_search=False):
        self.repos, self.issues = repos, issues
        self.comments, self.timelines = comments or {}, timelines or {}
        self.fail, self.quota, self.incomplete_search = fail, quota, incomplete_search
        self.urls = []

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        assert "Authorization" not in headers or headers["Authorization"].startswith("Bearer ")
        if self.fail:
            if self.fail == "network":
                raise OSError("down")
            return ot._Response(self.fail, {"message": "fail"}, {})
        self.quota -= 1
        h = {"X-RateLimit-Remaining": str(self.quota)}
        p = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(p.query))
        path = p.path
        if path == "/orgs/open-telemetry/repos":
            page = int(q.get("page", 1))
            return ot._Response(200, self.repos[(page - 1) * 100: page * 100], h)
        if path == "/search/issues":
            h["X-RateLimit-Resource"] = "search"
            return ot._Response(200, {"incomplete_results": self.incomplete_search, "items": list(self.issues)}, h)
        m = re.match(r"/repos/(.+)/issues/(\d+)/(comments|timeline)$", path)
        if m:
            src = self.comments if m.group(3) == "comments" else self.timelines
            return ot._Response(200, src.get(int(m.group(2)), []), h)
        m = re.match(r"/repos/(.+)/issues$", path)
        if m:
            name = m.group(1)
            return ot._Response(200, [i for i in self.issues if i["repository_url"].endswith(name)], h)
        return ot._Response(404, {}, h)


REPOS = [repo(1, "opentelemetry-python"), repo(2, "opentelemetry-java", language="Java"),
         repo(3, "old-thing", archived=True), repo(4, "no-issues", has_issues=False)]


def run(tmp_path, fake, **kw):
    kw.setdefault("max_calls", 45)
    return ot.collect(tmp_path, NOW, transport=fake, **kw)


def test_surfaces_unlabelled_doc_task_and_non_python_candidate(tmp_path):
    fake = FakeGitHub(REPOS, [DOC_5139, JAVA_TEST, UNRELATED])
    out = run(tmp_path, fake)
    assert out["status"] == "ok"
    assert out["coverage"]["initial_pass_complete"] is True
    assert out["coverage"]["repos_eligible"] == 2          # archived and issue-disabled excluded
    keys = {(l["repo"], l["number"]) for l in out["leads"]}
    assert ("open-telemetry/opentelemetry-python", 5139) in keys
    assert ("open-telemetry/opentelemetry-java", 77) in keys
    assert len(out["leads"]) <= ot.MAX_CARDS
    assert out["leads"][0]["evidence"]


def test_later_snapshot_with_linked_pr_withholds_the_task(tmp_path):
    run(tmp_path, FakeGitHub(REPOS, [DOC_5139]))
    later = dict(DOC_5139, updated_at="2026-11-20T10:00:00Z")
    tl = {5139: [{"event": "cross-referenced", "source": {"issue": {
        "html_url": "https://github.com/open-telemetry/opentelemetry-python/pull/5867", "state": "open",
        "pull_request": {"merged_at": None}}}}]}
    out = run(tmp_path, FakeGitHub(REPOS, [later], timelines=tl))
    assert not [l for l in out["leads"] if l["number"] == 5139]
    state = json.loads((tmp_path / ot.STATE_FILE).read_text())
    assert "open_pr" in state["candidates"]["5139"]["withheld_reasons"]


@pytest.mark.parametrize("case,reason", [
    ("claim", "claimed_in_comments"), ("merged", "fixed_by_merged_pr"),
    ("recent_closed", "recently_closed_pr"), ("no_direction", "no_maintainer_direction"),
    ("assigned", "assigned"), ("pagination", "incomplete_pagination")])
def test_withholding_rules(tmp_path, case, reason):
    iss, comments, tl = dict(DOC_5139), {}, {}
    if case == "claim":
        comments = {5139: [{"author_association": "NONE", "body": "I'd like to work on this!", "created_at": "2026-11-01T00:00:00Z"}]}
    if case == "merged":
        tl = {5139: [{"event": "cross-referenced", "source": {"issue": {"state": "closed", "html_url": "u",
                     "pull_request": {"merged_at": "2026-10-01T00:00:00Z"}}}}]}
    if case == "recent_closed":
        tl = {5139: [{"event": "cross-referenced", "source": {"issue": {"state": "closed", "html_url": "u",
                     "closed_at": "2026-11-20T00:00:00Z", "pull_request": {"merged_at": None}}}}]}
    if case == "no_direction":
        iss["author_association"] = "NONE"
    if case == "assigned":
        iss["assignees"] = [{"login": "someone"}]
    if case == "pagination":
        comments = {5139: [{"author_association": "NONE", "body": "+1", "created_at": OLD}] * 100}
    out = run(tmp_path, FakeGitHub(REPOS, [iss], comments=comments, timelines=tl))
    assert not out["leads"]
    state = json.loads((tmp_path / ot.STATE_FILE).read_text())
    assert reason in state["candidates"]["5139"]["withheld_reasons"]


def test_abandoned_pr_and_old_claim_do_not_block(tmp_path):
    tl = {5139: [{"event": "cross-referenced", "source": {"issue": {"state": "closed", "html_url": "u",
                 "closed_at": "2026-06-01T00:00:00Z", "pull_request": {"merged_at": None}}}}]}
    comments = {5139: [{"author_association": "NONE", "body": "I'll take this", "created_at": "2026-05-01T00:00:00Z"}]}
    out = run(tmp_path, FakeGitHub(REPOS, [DOC_5139], comments=comments, timelines=tl))
    assert [l["number"] for l in out["leads"]] == [5139]


def test_maintainer_comment_supplies_direction(tmp_path):
    iss = dict(DOC_5139, author_association="NONE")
    comments = {5139: [{"author_association": "MEMBER", "body": "Yes, a docs PR is welcome.", "html_url": "c1",
                        "created_at": "2026-11-01T00:00:00Z"}]}
    out = run(tmp_path, FakeGitHub(REPOS, [iss], comments=comments))
    assert out["leads"] and out["leads"][0]["evidence"][0]["kind"] == "maintainer_comment"


def test_shown_only_after_confirmed_send_and_unchanged_lead_stays_quiet(tmp_path):
    first = run(tmp_path, FakeGitHub(REPOS, [DOC_5139]))
    assert first["staged_ids"] == ["5139"]
    again = run(tmp_path, FakeGitHub(REPOS, [DOC_5139]))          # not marked shown: still offered
    assert again["staged_ids"] == ["5139"]
    assert ot.mark_shown(tmp_path, again["staged_ids"], "gmail-msg-1") == 1
    quiet = run(tmp_path, FakeGitHub(REPOS, [DOC_5139]))
    assert quiet["leads"] == []
    changed = dict(DOC_5139, title="Logging handler drops extra attributes (docs)", updated_at="2026-11-25T00:00:00Z")
    back = run(tmp_path, FakeGitHub(REPOS, [changed]))
    assert back["staged_ids"] == ["5139"]                         # material change resurfaces


def test_mark_shown_ignores_unstaged_or_changed(tmp_path):
    out = run(tmp_path, FakeGitHub(REPOS, [DOC_5139]))
    assert ot.mark_shown(tmp_path, ["999"], "r") == 0
    assigned = dict(DOC_5139, assignees=[{"login": "someone"}], updated_at="2026-11-25T00:00:00Z")
    run(tmp_path, FakeGitHub(REPOS, [assigned]))                  # now withheld: no longer staged
    assert ot.mark_shown(tmp_path, out["staged_ids"], "r") == 0


def test_budget_is_enforced_and_progress_resumes(tmp_path):
    many = [repo(100 + i, f"repo-{i:03d}") for i in range(250)]
    fake = FakeGitHub(many, [])
    out = run(tmp_path, fake, max_calls=10)
    assert len(fake.urls) <= 10
    assert out["coverage"]["initial_pass_complete"] is False
    assert out["coverage"]["repos_pending_full_pass"] > 0
    assert out["leads"] == []
    for _ in range(40):
        out = run(tmp_path, FakeGitHub(many, []), max_calls=45)
        if out["coverage"]["initial_pass_complete"]:
            break
    assert out["coverage"]["initial_pass_complete"] is True
    assert out["coverage"]["repos_full_pass"] == 250


def test_deadline_stops_the_run(tmp_path):
    t = [0.0]

    def clock():
        t[0] += 50
        return t[0]
    fake = FakeGitHub(REPOS, [DOC_5139])
    out = ot.collect(tmp_path, NOW, transport=fake, clock=clock, deadline_s=120)
    assert out["status"] == "partial" and len(fake.urls) <= 3


def test_low_quota_reduces_budget(tmp_path):
    fake = FakeGitHub(REPOS, [DOC_5139], quota=8)
    out = run(tmp_path, fake)
    assert len(fake.urls) <= 4 and out["status"] == "partial"


def test_unavailable_vs_partial(tmp_path):
    assert run(tmp_path, FakeGitHub(REPOS, [], fail="network"))["status"] == "unavailable"
    assert run(tmp_path, FakeGitHub(REPOS, [], fail=401))["status"] == "unavailable"
    out = run(tmp_path, FakeGitHub(REPOS, [DOC_5139], incomplete_search=True))
    assert out["status"] == "partial" and any("incomplete" in n for n in out["notes"])


def test_state_file_is_private(tmp_path):
    run(tmp_path, FakeGitHub(REPOS, [DOC_5139]))
    assert oct((tmp_path / ot.STATE_FILE).stat().st_mode & 0o777) == "0o600"


def test_bot_issues_are_ignored_and_strong_candidates_verified_first(tmp_path):
    bot = issue(950, 9098, "open-telemetry/opentelemetry-java", "Dependency Dashboard",
                "This issue lists Renovate updates. CI workflow fix pending.")
    bot["user"] = {"login": "renovate[bot]", "type": "Bot"}
    weak = issue(951, 12, "open-telemetry/opentelemetry-java", "Docs typo", "Fix the broken link in README.", assoc="NONE",
                 updated="2026-11-25T00:00:00Z")
    out = run(tmp_path, FakeGitHub(REPOS, [bot, weak, DOC_5139]), max_calls=9)
    state = json.loads((tmp_path / ot.STATE_FILE).read_text())
    assert "950" not in state["candidates"]
    assert state["candidates"]["5139"]["status"] != "new"            # maintainer-raised verified before the newer weak one


def test_assigned_issue_is_withheld_without_api_calls(tmp_path):
    iss = dict(DOC_5139, assignees=[{"login": "someone"}])
    fake = FakeGitHub(REPOS, [iss])
    run(tmp_path, fake)
    assert not [u for u in fake.urls if "/issues/5139/" in u]
