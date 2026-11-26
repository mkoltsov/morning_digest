# OpenTelemetry contribution tracker: integration notes

`otel_tracker.py` is self-contained: standard library only, read-only GitHub access, and private state in one JSON file. Tests are in `tests/test_otel_tracker.py` and use a fake GitHub transport with no network access.

## Interface

```python
import otel_tracker
result = otel_tracker.collect(state_dir, now, max_calls=45, deadline_s=120, token=github_token)
# result = {"status": "ok"|"partial"|"unavailable", "leads": [<=2 cards], "coverage": {...},
#           "staged_ids": [...], "notes": [...]}
# ...render result["leads"] before Automation Health, then after Gmail acceptance or Sent reconciliation:
ack = otel_tracker.mark_shown(state_dir, result["staged_ids"], receipt_id)
# ack = {"confirmed": [...], "already": [...], "unknown": [...]}; keep a receipt pending while any ID is "unknown"
```

- **Switch:** keep the tracker behind its own switch, default off. During the private trial, call `collect()` every run but do not render the leads. Log the `coverage` and `notes` privately.
- **Staging:** a lead counts as shown only after `mark_shown()`. `collect()` restages every run, so a crash before the send leaves the lead available. An unchanged lead stays quiet once shown; a material change (title, assignees, linked-PR state, evidence) makes it eligible again.
- **Status:**
  - `partial` means the budget, deadline, quota or GitHub truncation cut the work; continue in the next run.
  - `unavailable` means nothing could be checked (network failure or 401).
  - Render "no new leads", "partial scan" and "GitHub unavailable" as three distinct cases.
- **When leads appear:** `leads` stays empty until every eligible repository has had a first full pass (`coverage.initial_pass_complete`).
- **Token:** a fine-grained, read-only token for public repositories, read from a private file outside the repository. Pass it as `token=`. Never log it. Without a token GitHub allows 60 requests an hour, and the search API is limited further.
- **State:** `state_dir/otel_tracker_state.json` (mode 0600) is the private ledger: inventory, per-repo checkpoints, candidates, evidence and withheld reasons, shown fingerprints, and per-run coverage. Each run takes a flock lock.

## Measured on 26 Nov 2026

From two live read-only runs, unauthenticated, 20 calls each:

- **Inventory:** 113 organisation repositories, of which 82 are eligible (31 archived or with issues disabled), with about 11,500 open issues between them.
- **First full pass:** about 200 issue-page reads. At about 18 rotation calls per 45-call run, that's roughly 11 runs (about 6 days) of the 28-run trial.
- **Speed:** each run took about 18 seconds.
- **Noise:** bot issues (Renovate and Dependabot "Dependency Dashboard") are excluded. Assigned issues are withheld without spending API calls.
- **Verification order:** candidates raised by a maintainer or with a help label are verified first.

## Revisions and receipts

- **Staging IDs:** `staged_ids` are immutable revision IDs, `<issue id>:<fingerprint>`.
- **Pending revisions:** a pending revision survives later scans until `mark_shown()` acknowledges it, or for 14 days. A late receipt for revision A therefore acknowledges only A, even if revision B is current or the issue has since been withheld.
- **Replays:** replaying a receipt is idempotent and returns its IDs under `already`.

## Verification rules (a lead is withheld for any of these)

Every lead is re-verified in the same run that stages it. That re-verification confirms the issue directly: it is open and unassigned, its repo is still eligible, and its comments and timeline are complete. A 404, a page cap or a truncated search counts as incomplete, and the lead is withheld.

- Assigned.
- An unresolved claim comment by anyone, maintainers included ("I'd like to work on this", "I'll take this" and similar). Age alone never resolves a claim. Only the claimant releasing it, or a maintainer opening the issue to others, does.
- A linked PR (from timeline cross-references, connected events, or a direct PR search for the issue number, in any repo) that is open, merged, or closed within the last 30 days. Each PR's state is read from the PR itself.
- No direction from a **listed** maintainer or approver of that repo. Roles come from the repo README's Maintainers and Approvers sections, cached for 7 days; Emeritus and Triagers are excluded. GitHub's MEMBER association alone doesn't count, and neither does "Thanks for reporting". Direction is the issue body when its author is listed, or an explicit invitation such as "a PR is welcome". The role, its source and the permalink are saved as evidence.
- Comment or timeline pagination at or above 100 items, which may be incomplete.
