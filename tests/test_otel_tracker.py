"""Offline tests for otel_tracker: a fake GitHub transport, no network."""
import base64
import datetime as dt
import json
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import otel_tracker as ot  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = dt.datetime(2026, 11, 26, 13, 0, tzinfo=dt.timezone.utc)
OLD = "2026-04-10T10:00:00Z"
PY = "open-telemetry/opentelemetry-python"
JAVA = "open-telemetry/opentelemetry-java"

README = {
    PY: "# OTel Python\n\n## Maintainers\n\n- [Riccardo](https://github.com/xrmx)\n- [Leighton](https://github.com/lzchen)\n\n"
        "## Approvers\n\n- [Approver One](https://github.com/approver1)\n\n### Emeritus\n\n- [Old](https://github.com/oldie)\n",
    JAVA: "# OTel Java\n\n## Maintainers\n\n- [Jack](https://github.com/jack-berg)\n\n## Triagers\n\n- [T](https://github.com/triager)\n",
}


def repo(i, name, **kw):
    return {"id": i, "full_name": f"open-telemetry/{name}", "archived": kw.get("archived", False),
            "has_issues": kw.get("has_issues", True), "language": kw.get("language", "Python"), "open_issues_count": 1}


def issue(i, num, repo_name, title, body, *, author="xrmx", assoc="CONTRIBUTOR", labels=(), assignees=(),
          updated=OLD, state="open"):
    return {"id": i, "number": num, "title": title, "body": body, "state": state, "author_association": assoc,
            "user": {"login": author, "type": "User"}, "labels": [{"name": n} for n in labels],
            "assignees": [{"login": a} for a in assignees], "updated_at": updated,
            "html_url": f"https://github.com/{repo_name}/issues/{num}",
            "repository_url": f"https://api.github.com/repos/{repo_name}"}


def comment(login, body, assoc="NONE", at="2026-11-01T00:00:00Z", n=1):
    return {"user": {"login": login}, "author_association": assoc, "body": body, "created_at": at,
            "html_url": f"https://github.com/x/issues/1#issuecomment-{n}"}


REAL_5139 = json.loads((FIX / "issue_5139_april.json").read_text())
REAL_TIMELINE = json.loads((FIX / "timeline_5139_later.json").read_text())
APRIL_TIMELINE = [e for e in REAL_TIMELINE if (e.get("created_at") or "") < "2026-05-01"]
PR_5867 = json.loads((FIX / "pull_5867.json").read_text())

JAVA_TEST = issue(901, 77, JAVA, "Add regression test for span limits",
                  "Please add a regression test covering attribute limits.", author="jack-berg", assoc="MEMBER",
                  labels=("help wanted",))
UNRELATED = issue(902, 78, JAVA, "Question about exporters", "How do I configure x?", author="someone", assoc="NONE")
REPOS = [repo(1, "opentelemetry-python"), repo(2, "opentelemetry-java", language="Java"),
         repo(3, "old-thing", archived=True), repo(4, "no-issues", has_issues=False)]


class FakeGitHub:
    def __init__(self, repos, issues, *, comments=None, timelines=None, pulls=None, pr_search=None, readmes=None,
                 fail=None, quota=5000, incomplete_search=False, missing=(), files=None):
        self.repos, self.issues = repos, issues
        self.files = files or {}
        self.comments, self.timelines = comments or {}, timelines or {}
        self.pulls, self.pr_search = pulls or {}, pr_search or {}
        self.readmes = README if readmes is None else readmes
        self.fail, self.quota, self.incomplete_search, self.missing = fail, quota, incomplete_search, set(missing)
        self.urls = []

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        if self.fail:
            if self.fail == "network":
                raise OSError("down")
            return ot._Response(self.fail, {"message": "fail"}, {})
        self.quota -= 1
        h = {"X-RateLimit-Remaining": str(self.quota)}
        p = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(p.query))
        path, page = p.path, int(q.get("page", 1))
        if any(path.endswith(m) for m in self.missing):
            return ot._Response(404, {"message": "Not Found"}, h)
        m = re.match(r"/orgs/([^/]+)/repos$", path)
        if m:
            rows = [r for r in self.repos if r["full_name"].startswith(m.group(1) + "/")]
            return ot._Response(200, rows[(page - 1) * 100: page * 100], h)
        m = re.match(r"/repos/([^/]+/[^/]+)/contents/(.+)$", path)
        if m:
            text = self.files.get((m.group(1), m.group(2)))
            if text is None:
                return ot._Response(404, {}, h)
            return ot._Response(200, {"content": base64.b64encode(text.encode()).decode()}, h)
        if path == "/search/issues":
            h["X-RateLimit-Resource"] = "search"
            if "is:pr" in q["q"]:
                num = int(re.search(r"is:pr (\d+)", q["q"]).group(1))
                return ot._Response(200, {"incomplete_results": False, "items": self.pr_search.get(num, [])}, h)
            return ot._Response(200, {"incomplete_results": self.incomplete_search, "items": list(self.issues)}, h)
        m = re.match(r"/repos/([^/]+/[^/]+)/readme$", path)
        if m:
            text = self.readmes.get(m.group(1))
            if text is None:
                return ot._Response(404, {}, h)
            return ot._Response(200, {"content": base64.b64encode(text.encode()).decode()}, h)
        m = re.match(r"/repos/([^/]+/[^/]+)/pulls/(\d+)$", path)
        if m:
            pr = self.pulls.get(int(m.group(2)))
            return ot._Response(200, pr, h) if pr else ot._Response(404, {}, h)
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)/(comments|timeline)$", path)
        if m:
            src = self.comments if m.group(3) == "comments" else self.timelines
            rows = src.get(int(m.group(2)), [])
            return ot._Response(200, rows[(page - 1) * 100: page * 100], h)
        m = re.match(r"/repos/([^/]+/[^/]+)/issues/(\d+)$", path)
        if m:
            found = [i for i in self.issues if i["number"] == int(m.group(2)) and i["repository_url"].endswith(m.group(1))]
            return ot._Response(200, found[0], h) if found else ot._Response(404, {}, h)
        m = re.match(r"/repos/([^/]+/[^/]+)/issues$", path)
        if m:
            return ot._Response(200, [i for i in self.issues if i["repository_url"].endswith(m.group(1))
                                      and i["state"] == "open"], h)
        return ot._Response(404, {}, h)


def run(tmp_path, fake, **kw):
    kw.setdefault("max_calls", 60)
    return ot.collect(tmp_path, NOW, transport=fake, **kw)


def state(tmp_path):
    return json.loads((tmp_path / ot.STATE_FILE).read_text())


KEY = str(REAL_5139["id"])


# ---------------------------------------------------------------- the real #5139 case

def test_real_5139_april_snapshot_surfaces_the_documentation_task(tmp_path):
    out = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    assert [(l["repo"], l["number"], l["kind"]) for l in out["leads"]] == [(PY, 5139, "documentation")]
    ev = out["leads"][0]["evidence"][0]
    assert ev["kind"] == "maintainer_issue" and ev["by"] == "xrmx" and ev["role"] == "maintainer"
    assert ev["role_source"].endswith("opentelemetry-python#readme")


def test_real_5139_later_snapshot_with_pr_5867_withholds_it(tmp_path):
    out = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: REAL_TIMELINE}, pulls={5867: PR_5867}))
    assert out["leads"] == []
    assert "open_pr" in state(tmp_path)["candidates"][KEY]["withheld_reasons"]


def test_fixing_pr_found_by_direct_search_even_when_missing_from_timeline(tmp_path):
    hit = {"number": 5867, "title": PR_5867["title"], "body": PR_5867["body"], "html_url": PR_5867["html_url"]}
    out = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE},
                                   pulls={5867: PR_5867}, pr_search={5139: [hit]}))
    assert out["leads"] == []
    assert "open_pr" in state(tmp_path)["candidates"][KEY]["withheld_reasons"]


# ---------------------------------------------------------------- current verification

def test_cached_lead_that_closed_is_not_offered(tmp_path):
    assert run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))["leads"]
    closed = dict(REAL_5139, state="closed")
    out = run(tmp_path, FakeGitHub(REPOS, [closed], timelines={5139: APRIL_TIMELINE}))
    assert out["leads"] == []
    assert state(tmp_path)["candidates"][KEY]["status"] == "closed"


def test_lead_is_reverified_in_the_run_that_stages_it(tmp_path):
    run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    fake = FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE})
    out = run(tmp_path, fake)
    assert out["leads"]
    assert any(urllib.parse.urlsplit(u).path.endswith("/issues/5139") for u in fake.urls)


@pytest.mark.parametrize("missing", ["/issues/5139/comments", "/issues/5139/timeline", "/readme"])
def test_404_on_any_required_check_withholds(tmp_path, missing):
    out = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}, missing=[missing]))
    assert out["leads"] == []
    reasons = state(tmp_path)["candidates"][KEY]["withheld_reasons"]
    assert {"verification_incomplete", "maintainer_roles_unavailable"} & set(reasons)


def test_comments_are_fully_paginated(tmp_path):
    many = [comment(f"user{i}", "+1", n=i) for i in range(150)]
    many.append(comment("newbie", "I'd like to work on this", n=999))
    run(tmp_path, FakeGitHub(REPOS, [REAL_5139], comments={5139: many}, timelines={5139: APRIL_TIMELINE}))
    assert "claimed_in_comments" in state(tmp_path)["candidates"][KEY]["withheld_reasons"]


# ---------------------------------------------------------------- authoritative maintainer evidence

def test_org_member_without_listed_role_is_not_direction(tmp_path):
    iss = issue(903, 90, JAVA, "Add regression test for exporter retry", "Please add a regression test for retries.",
                author="randomuser", assoc="NONE")
    cm = {90: [comment("org-member", "Thanks for reporting", assoc="MEMBER")]}
    out = run(tmp_path, FakeGitHub(REPOS, [iss], comments=cm))
    assert out["leads"] == []
    assert "no_maintainer_direction" in state(tmp_path)["candidates"]["903"]["withheld_reasons"]


def test_listed_maintainer_thanks_is_not_direction_but_explicit_invitation_is(tmp_path):
    iss = issue(904, 91, JAVA, "Add regression test for exporter retry", "Please add a regression test for retries.",
                author="randomuser", assoc="NONE")
    out = run(tmp_path, FakeGitHub(REPOS, [iss], comments={91: [comment("jack-berg", "Thanks for reporting", "MEMBER")]}))
    assert out["leads"] == []
    out = run(tmp_path, FakeGitHub(REPOS, [dict(iss, updated_at="2026-11-25T00:00:00Z")], comments={91: [
        comment("jack-berg", "Thanks for reporting", "MEMBER", n=1),
        comment("jack-berg", "A PR for this is welcome.", "MEMBER", n=2)]}))
    assert [l["number"] for l in out["leads"]] == [91]
    ev = [e for e in out["leads"][0]["evidence"] if e["kind"] == "maintainer_comment"][0]
    assert ev["by"] == "jack-berg" and ev["role"] == "maintainer" and ev["url"].endswith("issuecomment-2")


def test_triager_and_emeritus_are_not_roles(tmp_path):
    iss = issue(905, 92, JAVA, "Add regression test x", "Please add a regression test.", author="triager")
    run(tmp_path, FakeGitHub(REPOS, [iss]))
    assert "no_maintainer_direction" in state(tmp_path)["candidates"]["905"]["withheld_reasons"]
    run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    roles = state(tmp_path)["roles"]
    assert "oldie" not in roles[PY]["handles"] and "approver1" in roles[PY]["handles"]
    assert "triager" not in roles[JAVA]["handles"]


def test_maintainer_claim_is_still_a_claim(tmp_path):
    cm = {5139: [comment("lzchen", "I'll take this", "MEMBER")]}
    run(tmp_path, FakeGitHub(REPOS, [REAL_5139], comments=cm, timelines={5139: APRIL_TIMELINE}))
    assert "claimed_in_comments" in state(tmp_path)["candidates"][KEY]["withheld_reasons"]


def test_unresolved_claim_does_not_expire_but_a_release_resolves_it(tmp_path):
    old = {5139: [comment("dev1", "I'd like to work on this", at="2026-05-01T00:00:00Z")]}
    assert run(tmp_path, FakeGitHub(REPOS, [REAL_5139], comments=old, timelines={5139: APRIL_TIMELINE}))["leads"] == []
    released = {5139: old[5139] + [comment("dev1", "I'm not working on this anymore", at="2026-06-01T00:00:00Z", n=2)]}
    out = run(tmp_path, FakeGitHub(REPOS, [dict(REAL_5139, updated_at="2026-11-25T00:00:00Z")], comments=released,
                                   timelines={5139: APRIL_TIMELINE}))
    assert [l["number"] for l in out["leads"]] == [5139]


@pytest.mark.parametrize("pr,blocks", [
    ({"state": "closed", "merged_at": "2026-10-01T00:00:00Z", "closed_at": "2026-10-01T00:00:00Z"}, True),
    ({"state": "closed", "merged_at": None, "closed_at": "2026-11-20T00:00:00Z"}, True),
    ({"state": "closed", "merged_at": None, "closed_at": "2026-06-01T00:00:00Z"}, False)])
def test_linked_pr_states_are_read_from_the_pr_itself(tmp_path, pr, blocks):
    tl = {5139: [{"event": "cross-referenced", "source": {"issue": {
        "number": 6000, "state": "open", "html_url": "u", "pull_request": {"merged_at": None},
        "repository_url": f"https://api.github.com/repos/{PY}"}}}]}
    out = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines=tl, pulls={6000: {**pr, "html_url": "u"}}))
    assert (out["leads"] == []) is blocks


def test_cross_repo_linked_pr_is_checked_in_its_own_repo(tmp_path):
    tl = {5139: [{"event": "cross-referenced", "source": {"issue": {
        "number": 42, "state": "open", "html_url": "u", "pull_request": {},
        "repository_url": "https://api.github.com/repos/open-telemetry/opentelemetry-python-contrib"}}}]}
    fake = FakeGitHub(REPOS, [REAL_5139], timelines=tl, pulls={42: {"state": "open", "merged_at": None, "html_url": "u"}})
    out = run(tmp_path, fake)
    assert out["leads"] == []
    assert any("/repos/open-telemetry/opentelemetry-python-contrib/pulls/42" in u for u in fake.urls)


# ---------------------------------------------------------------- staging across delayed receipts

def test_delayed_receipt_acknowledges_only_the_revision_it_sent(tmp_path):
    a = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    sid_a = a["staged_ids"][0]
    changed = dict(REAL_5139, title=REAL_5139["title"] + " (docs)", updated_at="2026-11-25T00:00:00Z")
    b = run(tmp_path, FakeGitHub(REPOS, [changed], timelines={5139: APRIL_TIMELINE}))
    sid_b = b["staged_ids"][0]
    assert sid_a != sid_b and sid_a.split(":")[0] == sid_b.split(":")[0]
    assert ot.mark_shown(tmp_path, [sid_a], "receipt-A") == {"confirmed": [sid_a], "already": [], "unknown": []}
    again = run(tmp_path, FakeGitHub(REPOS, [changed], timelines={5139: APRIL_TIMELINE}))
    assert again["staged_ids"] == [sid_b]                      # B still offered after A's late receipt
    assert ot.mark_shown(tmp_path, [sid_a], "receipt-A")["already"] == [sid_a]      # replay is idempotent
    assert ot.mark_shown(tmp_path, [sid_b], "receipt-B")["confirmed"] == [sid_b]
    assert run(tmp_path, FakeGitHub(REPOS, [changed], timelines={5139: APRIL_TIMELINE}))["leads"] == []


def test_receipt_for_a_withheld_or_unknown_revision(tmp_path):
    a = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    sid_a = a["staged_ids"][0]
    assigned = dict(REAL_5139, assignees=[{"login": "x"}], updated_at="2026-11-25T00:00:00Z")
    run(tmp_path, FakeGitHub(REPOS, [assigned], timelines={5139: APRIL_TIMELINE}))
    assert ot.mark_shown(tmp_path, [sid_a], "receipt-A")["confirmed"] == [sid_a]     # A was really sent
    assert ot.mark_shown(tmp_path, ["999:deadbeef"], "r")["unknown"] == ["999:deadbeef"]


def test_unsent_revision_stays_pending_and_is_offered_again(tmp_path):
    a = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    b = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    assert a["staged_ids"] == b["staged_ids"]
    assert b["coverage"]["pending_revisions"] == 1


# ---------------------------------------------------------------- discovery, budget, status

def test_non_python_candidate_and_ineligible_repos(tmp_path):
    out = run(tmp_path, FakeGitHub(REPOS, [JAVA_TEST, UNRELATED]))
    assert [(l["repo"], l["number"]) for l in out["leads"]] == [(JAVA, 77)]
    assert out["coverage"]["repos_eligible"] == 2


def test_bot_issues_are_ignored(tmp_path):
    bot = issue(950, 9098, JAVA, "Dependency Dashboard", "Renovate updates. CI workflow fix pending.", author="renovate[bot]")
    bot["user"]["type"] = "Bot"
    run(tmp_path, FakeGitHub(REPOS, [bot]))
    assert "950" not in state(tmp_path)["candidates"]


def test_assigned_issue_is_withheld_without_api_calls(tmp_path):
    iss = dict(REAL_5139, assignees=[{"login": "someone"}])
    fake = FakeGitHub(REPOS, [iss])
    run(tmp_path, fake)
    assert not [u for u in fake.urls if "/issues/5139" in u]


def test_budget_is_enforced_and_progress_resumes(tmp_path):
    many = [repo(100 + i, f"repo-{i:03d}") for i in range(250)]
    fake = FakeGitHub(many, [])
    out = run(tmp_path, fake, max_calls=10)
    assert len(fake.urls) <= 10
    assert out["coverage"]["initial_pass_complete"] is False and out["coverage"]["repos_pending_full_pass"] > 0
    for _ in range(40):
        out = run(tmp_path, FakeGitHub(many, []), max_calls=45)
        if out["coverage"]["initial_pass_complete"]:
            break
    assert out["coverage"]["repos_full_pass"] == 250


def test_deadline_stops_the_run(tmp_path):
    t = [0.0]

    def clock():
        t[0] += 50
        return t[0]
    fake = FakeGitHub(REPOS, [REAL_5139])
    out = ot.collect(tmp_path, NOW, transport=fake, clock=clock, deadline_s=120)
    assert out["status"] == "partial" and len(fake.urls) <= 3


def test_low_quota_reduces_budget(tmp_path):
    fake = FakeGitHub(REPOS, [REAL_5139], quota=8)
    out = run(tmp_path, fake)
    assert len(fake.urls) <= 4 and out["status"] == "partial"


def test_unavailable_vs_partial(tmp_path):
    assert run(tmp_path, FakeGitHub(REPOS, [], fail="network"))["status"] == "unavailable"
    assert run(tmp_path, FakeGitHub(REPOS, [], fail=401))["status"] == "unavailable"
    out = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], incomplete_search=True))
    assert out["status"] == "partial" and any("incomplete" in n for n in out["notes"])


def test_state_file_is_private(tmp_path):
    run(tmp_path, FakeGitHub(REPOS, [REAL_5139]))
    assert oct((tmp_path / ot.STATE_FILE).stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------- Grafana

GRAFANA = "grafana/loki"
G_REPOS = REPOS + [{"id": 70, "full_name": GRAFANA, "archived": False, "has_issues": True, "language": "Go",
                    "open_issues_count": 1},
                   {"id": 71, "full_name": "grafana/k6", "archived": False, "has_issues": True, "language": "Go",
                    "open_issues_count": 1}]
G_FILES = {(GRAFANA, "MAINTAINERS.md"): "@slim-bean is the main/default maintainer.\n\n"
                                       "- `@grafana/docs-logs`, which includes [@knylander-grafana](https://github.com/knylander-grafana)\n"
                                       "\n## Emeritus\n\n- @former-person\n",
           ("grafana/k6", "CODEOWNERS"): "* @grafana/k6-core\n"}
LOKI_DOC = issue(980, 15000, GRAFANA, "Docs: document retention workaround",
                 "We should document the workaround for compactor retention in docs/operations.md.",
                 author="slim-bean", assoc="MEMBER")
K6_TEST = issue(981, 4000, "grafana/k6", "Add regression test for thresholds",
                "Please add a regression test for threshold parsing.", author="core-dev", assoc="MEMBER")


def test_grafana_roles_come_from_maintainers_files_not_teams(tmp_path):
    out = run(tmp_path, FakeGitHub(G_REPOS, [LOKI_DOC, K6_TEST], files=G_FILES))
    by = {(l["repo"], l["number"]): l for l in out["leads"]}
    assert (GRAFANA, 15000) in by
    ev = by[(GRAFANA, 15000)]["evidence"][0]
    assert ev["by"] == "slim-bean" and ev["role_source"].endswith("MAINTAINERS.md")
    roles = state(tmp_path)["roles"][GRAFANA]["handles"]
    assert "knylander-grafana" in roles and "former-person" not in roles and "grafana" not in roles
    assert "maintainer_roles_unavailable" in state(tmp_path)["candidates"]["981"]["withheld_reasons"]   # team-only k6


def test_grafana_card_carries_the_cla_note_and_org(tmp_path):
    out = run(tmp_path, FakeGitHub(G_REPOS, [LOKI_DOC], files=G_FILES))
    card = [l for l in out["leads"] if l["repo"] == GRAFANA][0]
    assert card["org"] == "grafana" and "CLA" in card["note"]


def test_leads_alternate_between_organisations(tmp_path):
    loki2 = issue(982, 15001, GRAFANA, "Docs: document another workaround",
                  "We should document the workaround in docs/x.md.", author="slim-bean", assoc="MEMBER")
    out = run(tmp_path, FakeGitHub(G_REPOS, [LOKI_DOC, loki2, REAL_5139], files=G_FILES,
                                   timelines={5139: APRIL_TIMELINE}))
    assert sorted(l["org"] for l in out["leads"]) == ["grafana", "open-telemetry"]


def test_each_organisation_has_its_own_budget_and_coverage(tmp_path):
    fake = FakeGitHub(G_REPOS, [LOKI_DOC], files=G_FILES)
    out = run(tmp_path, fake, max_calls=12)
    by = out["coverage"]["by_org"]
    assert set(by) == {"open-telemetry", "grafana"}
    assert all(v["http_attempts"] <= 12 for v in by.values())
    assert by["grafana"]["repos_eligible"] == 2 and by["open-telemetry"]["repos_eligible"] == 2


def test_single_org_state_migrates(tmp_path):
    legacy = {"version": 1, "repos": {"1": {"name": PY, "archived": False, "has_issues": True}},
              "checkpoints": {}, "candidates": {}, "shown": {}, "runs": [],
              "rotation_cursor": 3, "recent_since": "2026-11-20T00:00:00Z", "inventory_complete": True}
    (tmp_path / ot.STATE_FILE).write_text(json.dumps(legacy))
    run(tmp_path, FakeGitHub(REPOS, []), orgs=("open-telemetry",))
    st = state(tmp_path)
    assert "rotation_cursor" not in st and st["org_state"]["open-telemetry"]["recent_since"]
    assert st["repos"]["1"]["org"] == "open-telemetry"


def test_per_org_call_budgets(tmp_path):
    out = run(tmp_path, FakeGitHub(G_REPOS, [LOKI_DOC], files=G_FILES), max_calls={"open-telemetry": 8, "grafana": 20})
    by = out["coverage"]["by_org"]
    assert by["open-telemetry"]["http_attempts"] <= 8 and by["grafana"]["http_attempts"] <= 20


def test_old_unchanged_revision_restaged_today_does_not_expire_in_the_same_scan(tmp_path):
    first = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    sid = first["staged_ids"][0]
    later = NOW + dt.timedelta(days=20)
    out = ot.collect(tmp_path, later, transport=FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}),
                     max_calls=60)
    assert out["staged_ids"] == [sid]
    assert ot.mark_shown(tmp_path, [sid], "receipt")["confirmed"] == [sid]


def test_pending_revision_not_offered_for_14_days_expires(tmp_path):
    first = run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    sid = first["staged_ids"][0]
    gone = dict(REAL_5139, assignees=[{"login": "x"}], updated_at="2026-11-27T00:00:00Z")
    ot.collect(tmp_path, NOW + dt.timedelta(days=20), transport=FakeGitHub(REPOS, [gone]), max_calls=60)
    assert ot.mark_shown(tmp_path, [sid], "receipt")["unknown"] == [sid]


def test_grafana_role_source_is_the_file_that_lists_the_handle(tmp_path):
    files = dict(G_FILES)
    files[(GRAFANA, "CODEOWNERS")] = "/docs/ @doc-owner\n"
    doc = issue(983, 15002, GRAFANA, "Docs: document the setting", "We should document the setting in docs/x.md.",
                author="doc-owner", assoc="MEMBER")
    out = run(tmp_path, FakeGitHub(G_REPOS, [doc], files=files))
    ev = [l for l in out["leads"] if l["number"] == 15002][0]["evidence"][0]
    assert ev["role"] == "codeowner" and ev["role_source"].endswith("/CODEOWNERS")


def test_http_attempts_include_staging_rechecks(tmp_path):
    run(tmp_path, FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE}))
    fake = FakeGitHub(REPOS, [REAL_5139], timelines={5139: APRIL_TIMELINE})
    out = run(tmp_path, fake, orgs=("open-telemetry",))
    assert out["coverage"]["by_org"]["open-telemetry"]["http_attempts"] == len(fake.urls)
    assert out["coverage"]["http_attempts"] == len(fake.urls)
