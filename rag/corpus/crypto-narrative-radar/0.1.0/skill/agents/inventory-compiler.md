# inventory-compiler

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Compile the final thesis inventory with confidence trajectories, evidence logs, portfolio actions, and monitoring checklists for persistence across runs.

Read:
- all previous artifacts in the pipeline
- `inputs/inventory.json` — the previous inventory state
- `policy/default.yaml` — confidence and status rules

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/inventory.json`
- include the updated `thesis_inventory` with confidence trajectories and evidence logs
- include `run_summary` — theses added, updated, killed, retired this run
- include `monitoring_checklist` — what to watch before next run
- include the standard output contract fields
- include a human-readable `trade_book.md` written to `.wf-artifacts/$RUN_ID/trade_book.md` as the primary final output

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- **Pure merge step — no web calls, no adapter calls.** Read all upstream JSONs and combine.
- Preserve `tool` tags from upstream evidence through to the final inventory — useful for post-hoc analysis of which data sources fed the signal.
- Null-state runs MUST still write `trade_book.md` with the H1 + NULL STATE section + confidence ladder table + re-run triggers.
- Merge all pipeline results into the final inventory state.
- Update thesis statuses: new → active, active → escalating (if confidence > threshold), active → dormant (if confidence < threshold).
- Write the inventory in a format that can be loaded as `inputs/inventory.json` for the next run.
- The monitoring checklist should list specific, actionable items — not vague 'watch this space' notes.
- Compile the standard output contract: signal_snapshot, selected_playbook, candidate_expressions, null_state, risk_checks, job, next_invalidation, trade_book.
- TRADE BOOK FORMAT (mandatory — this is the final interpretable artifact for the user):
- - Write `trade_book.md` alongside `inventory.json`. File must begin with exactly one markdown H1 header then a summary table, then a per-trade section.
- - SUMMARY TABLE columns (markdown pipe table, exact order): `#`, `Thesis`, `Catalyst date`, `SDK surface`, `Instrument`, `Direction`, `Size ($)`, `Max loss ($)`, `Target PnL ($)`, `Invalidation`. One row per retained thesis ranked by final_confidence descending.
- - PER-TRADE SECTION (one per retained thesis, in same order as summary table). Each uses an H2 header `## #<N> — <thesis label>` and includes ≤5 short paragraphs covering: (1) what's happening / the mechanism in 2-3 sentences, (2) the exact SDK call as a fenced code block with real parameters, (3) entry/exit/stop rules, (4) risk (max loss and what invalidates), (5) why this has edge (1-2 sentences tying to positioning gap or base rate).
- - Lead the file with a one-paragraph book-level overview: total gross deployed, number of trades, earliest catalyst date, and any correlated-exposure notes.
- - If the run produced ZERO retained theses, `trade_book.md` must still be written with the H1 header plus an explicit NULL STATE section explaining why.
- - Prefer concrete numbers over adjectives. No emojis. No hedging language.
