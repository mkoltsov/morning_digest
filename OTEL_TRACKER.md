# OpenTelemetry contribution tracker: integration notes

`otel_tracker.py` is self-contained: standard library only, read-only GitHub access, and private state in one JSON file. Tests are in `tests/test_otel_tracker.py` and use a fake GitHub transport with no network access.

## Interface

```python
import otel_tracker
result = otel_tracker.collect(state_dir, now, max_calls=45, deadline_s=120, token=github_token)
# result = {"status": "ok"|"partial"|"unavailable", "leads": [<=2 cards], "coverage": {...},
#           "staged_ids": [...], "notes": [...]}
# ...render result["leads"] before Automation Health, then after Gmail acceptance or Sent reconciliation:
otel_tracker.mark_shown(state_dir, result["staged_ids"], receipt_id)
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

## Verification rules (a lead is withheld for any of these)

- Assigned.
- A claim comment ("I'd like to work on this", "assign me" and similar) within the last 60 days.
- A cross-referenced PR that is open, merged, or closed within the last 30 days, or a connected PR.
- No direction from a maintainer, i.e. neither the issue author nor any commenter is OWNER, MEMBER or COLLABORATOR.
- Comment or timeline pagination at or above 100 items, which may be incomplete.
