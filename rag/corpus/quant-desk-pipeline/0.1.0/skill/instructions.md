# Quant Desk Pipeline

Use this skill when the user wants to take a **narrow academic signal topic**, get a simple backtest working on crypto, then fit and (optionally) stress-test it — ending in an honest replication verdict. Examples:

- "Find papers on funding-rate regime signals and test them"
- "Replicate time-series momentum research on crypto"

**Do NOT use for:** shallow questions, practitioner-blog replication, or new-signal design.

Read `references/pipeline.md`, `references/signals.md`, and `references/risk.md` before starting.

## Orchestration — you run this pipeline by spawning one worker per phase

**You are the orchestrator.** Walk the phases in order and, for each, **spawn its declared worker subagent** (the table below maps phase → worker agent → artifact). You invoke this skill directly, so *your* agent does the spawning — there is no separate hidden orchestrator in the loop. The `/quant-desk-pipeline` slash command routes through a generated orchestrator agent instead; prefer invoking by name/topic so the run stays in your session.

Contract for every run:
- one worker per phase, each writing **exactly one** artifact under `.wf-artifacts/$RUN_ID/`;
- workers are **leaf** — a worker never spawns another agent; it does its phase and returns;
- don't use `general`/`explore`; use the declared `quant-desk-pipeline-<worker>` agents;
- don't present a final ARMED/DRAFT/NULL verdict until the required artifacts exist (use `assert_required_artifacts`).

Each worker doc is self-contained — the worker reads it (plus the references it names), not this whole file.

**Check artifacts at phase boundaries with `read_artifact` or `scripts/verify_artifacts.py` — not throwaway `python -c` lines.** e.g. after discovery: `python scripts/verify_artifacts.py cross-check $RUN_ID discovery.json candidates id methodology.json papers paper_id --filter verdict=KEEP` (confirms no KEEP paper was dropped), and `python scripts/verify_artifacts.py verify $RUN_ID methodology.json --array papers --fields signal_formula parameters claimed_sharpe sample frequency` (confirms per-paper fields).

| Graph node | Worker | Artifact |
|------------|--------|----------|
| `discovery` | `paper-finder` | `discovery.json` (find + feasibility-prune) |
| `methodology` | `methodology-extractor` | `methodology.json` |
| `skeptic` | `skeptic` | `skeptic.json` (fatal-flaw, isolated) |
| `implementation` | `signal-implementer` | `implementation.json` |
| `universe_builder` | `universe-builder` | `universe.json` (resolve + lock fitting basket; owns `pair_screener`) |
| `signal_research` | `signal-researcher` | `first_pass.json` (simple gate) |
| `fitting` | `fitting-engineer` | `fitting.json` (iterate + crypto-adapt, train/test) |
| `robustness` | `robustness-tester` | `robustness.json` (optional; stub if skipped) |
| `synthesis` | `synthesizer` | `synthesis.json` |

`intake` and `finalize` are orchestrator-owned; `pair_screener` is a pass-through the universe-builder covers. **Phase 0 Scout is not in this table and not in the graph** — see below.

## The shape: simple first, then fit, then (optionally) stress

This pipeline is deliberately ordered to get something working before spending compute on robustness:

1. **First pass** (`signal_research`) — one simple backtest at paper-spec params on the small fitting basket. Fail-fast **only** if the signal is degenerate (broken impl / no variance / no trades). A signal that merely underperforms thresholds still advances — improving it is the fitting phase's job.
2. **Fitting** (`fitting`) — the main loop: grid-search (forward horizon first), apply remedial/experimental templates, and adapt to crypto (funding, fees, 24/7, regimes). **A train/test split inside the fitting window is mandatory — select configs by test-side metrics, never pure in-sample.** Bounded by an iteration budget. See `references/fitting.md`.
3. **Robustness** (`robustness`, optional, last) — take the fitted config **unchanged** and stress it on **held-out** data: longer window (incl. a bear regime), wider universe, and a fee-tier sweep → a robustness report + PASS / REGIME_DEPENDENT / REJECT verdict. See `references/robustness.md`. If the run isn't authorized to include robustness, the agent emits a `{"status":"skipped"}` stub and the fitted signal is reported `PROVISIONAL`.

The held-out split is what keeps this honest: fitting may optimize freely, but robustness is the unbiased out-of-sample test. Locked PASS thresholds apply at robustness, not at the first pass.

**Expectations.** Reproduction from paper-spec alone is low (~0–1 of 10) — fitting's grid + templates exist to extract value from the rest, so don't declare a paper dead until the fitting budget (grid + the 6-variant template loop, incl. the mandatory C1 sign-flip) is exhausted. A 0-reproduction run is a legitimate but expensive outcome; when it happens, the Next-steps section must be exceptionally strong (closest signal, untried axes/templates, what would change the answer).

## Two entry modes

- **Scout (interactive, pre-pipeline)** — for broad asks ("what's spicy in funding signals?"). Not a graph node, not a worker, never run by the orchestrator. Run it yourself in the main thread: brainstorm broadly with any core tools (`core_web_search`/`core_web_fetch`, `research_search_alpha`, the `research_*` Delta Lab screens, sentiment, memory), cluster into candidate themes, tag each with a fast feasibility + memory flag, and loop until the user locks one narrow topic. Read-only. Then write the topic to `inputs/theme.md` and start the pipeline. See `references/scouting.md`.
- **Deep (the pipeline)** — the user already has a narrow topic that passes `references/topic-scope.md`, or Scout produced one. Start at `discovery`.

## Locked guarantees

- Topic must be a **specific signal family**, not a phenomenon (`references/topic-scope.md`). Max 10 candidate papers.
- **Academic sources only** (arxiv, NBER, top journals, BIS/Fed/ECB). **SSRN rejected.**
- **Minimum tradable frequency 15m**; each signal is tested at its native frequency (`bar_interval`/`forward_bars` are paper-driven, never shoehorned).
- **CCXT Binance** is the default data source; Hyperliquid only for HL-native symbols.
- The 3-phase harness lives at `scripts/evaluate_signal.py` — do not build a new evaluator.
- **Every run's report includes a Next-steps section**, even when nothing reproduced (`references/reporting-format.md`).

## References

- `topic-scope.md`, `canonical-baselines.md` — accepting/seeding a topic
- `feasibility.md` — discovery's five-axis KEEP/PRUNE rubric (data-access lists, value thresholds, universe-mismatch)
- `signal-contract.md`, `implementability-rules.md` — implementing the signal_fn
- `red-flag-taxonomy.md` — skeptic's fatal-flaw codes
- `universe-selection.md` — locking the fitting basket
- `fitting.md` — grid + templates + train/test discipline
- `robustness.md` — held-out window/universe/fee stress + verdicts
- `reporting-format.md` — final report + memory entry
- `scouting.md` — Phase 0 Scout

## Scripts

- `scripts/evaluate_signal.py` — the 3-phase harness (locked thresholds).
- `scripts/fetch_ccxt_history.py` — Binance multi-year OHLCV fetcher (for robustness window extension; the HL candle API caps at ~5000 bars).

## Memory

Each run writes a memory entry per `references/reporting-format.md` (topic, verdicts, fitted config, robustness verdict or PROVISIONAL, do-not-retest list). Check memory first on adjacent topics.
