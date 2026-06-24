# Signals

Every pipeline run ends in the standard response envelope:
- `signal_snapshot`
- `selected_playbook`
- `candidate_expressions`
- `null_state`
- `risk_checks`
- `job`
- `next_invalidation`

For this research path the envelope encodes a **replication verdict**, not a live trade:
- `armed` — at least one signal reproduced and generalized (PASS)
- `draft` — a winner that is `REGIME_DEPENDENT` (passed a window but not multi-year/multi-symbol); tactical overlay only
- `null-state-selected` — zero reproduction (REJECT across the pool); ship the Next-steps section
- `error` — the run could not complete

The per-paper verdict vocabulary is `PASS` / `REGIME_DEPENDENT` / `REJECT` (plus skeptic-rejected and signal-failed breakdowns in the report). See `references/reporting-format.md`.
