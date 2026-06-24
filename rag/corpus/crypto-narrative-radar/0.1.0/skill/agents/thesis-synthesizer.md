# thesis-synthesizer

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Merge domain scan outputs, deduplicate theses, reconcile cross-domain reinforcement, and apply confidence updates to the existing inventory.

Read:
- all five domain scan artifacts
- `inputs/inventory.json` — the existing thesis inventory
- `policy/default.yaml` — confidence update rules

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/thesis_synthesis.json`
- include a merged `theses` array combining new candidates with updated existing theses
- include `cross_domain_reinforcement` — theses that appear independently in multiple domains (flag these as higher confidence)
- include `confidence_deltas` — for each existing thesis, the confidence change and reason
- include deduplication notes — which candidates were merged and why

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- **Pure merge/dedupe step — no web calls, no adapter calls.** Read scanner JSONs and combine.
- Deduplicate aggressively — if two domain agents found the same structural risk, merge into one thesis with evidence from both. Preserve source domain in `source_domains: [...]`.
- Apply the confidence update rules from policy: supporting evidence increases, contrary evidence decreases, no new evidence decays.
- Cross-domain reinforcement is a strong signal — a thesis identified independently by ≥2 scanners deserves a +0.08 boost + `cross_domain_reinforcement: true`.
- Flag `gate_flag: "below_catalyst_floor"` for any thesis with `catalyst_days < 60` but do NOT drop — the adversarial chain decides.
- Preserve every `tool` tag from scanner evidence — downstream agents use these for source-quality weighting.
- Do not generate new theses — only merge and score what the domain agents produced.
