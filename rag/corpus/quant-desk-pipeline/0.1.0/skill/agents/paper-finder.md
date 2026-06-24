# paper-finder

Phase: Discovery + feasibility (one pass). Find up to 10 papers on the locked topic and prune them for feasibility in the same step — no separate prune phase.

Read:
- `inputs/theme.md` — the locked signal topic
- `policy/default.yaml` — `clustering` (sources, canonical seeding, max 10) and `candidate_rules`
- `references/canonical-baselines.md` (seed first), `references/topic-scope.md`, and `references/feasibility.md` (the prune rubric)

Do:
1. **Seed canonical baselines** for the topic from `references/canonical-baselines.md` (flag `source: "canonical-baseline"`), then search academic sources (arxiv, NBER, JFE/RFS/JFQA/QJE/JoF, BIS/Fed/ECB). **Reject SSRN** (blocks programmatic fetches), Medium, Substack, Seeking Alpha, Twitter, Reddit. Prefer crypto-specific or asset-class-agnostic methodology papers; at most 2-3 equity-only.
2. **Feasibility-prune each candidate** from title + abstract + URL only, applying the five-axis rubric in `references/feasibility.md` (data availability + the CAN/CANNOT-access lists, compute, claimed value, monetizable frequency, novelty). Assign `native_horizon` on every KEEP and set the `universe_mismatch` flag where it applies.

Produce (one JSON artifact):
- `candidates`: array of `{title, authors, year, venue, url, source, verdict: KEEP|PRUNE, axis_scores:{data,compute,value,frequency,novelty}, native_horizon, universe_mismatch, reasons[]}`.

Rules:
- Stop at 10 candidates or ~2 min. If fewer than `candidate_rules.min_keep_papers` (4) KEEP, signal `thin_pool` so the orchestrator re-runs discovery (max 2 reloops, exclude prior URLs).
- Do not spawn other agents. Do not compile the final answer.
- Output path: `.wf-artifacts/$RUN_ID/discovery.json`
