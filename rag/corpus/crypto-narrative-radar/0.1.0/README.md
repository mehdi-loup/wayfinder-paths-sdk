# Crypto Narrative Radar

Discover uncrowded crypto-native narratives with catalyst ≥60 days, $100M+ scale, and verified executability via the SDK.

## What this is

A crypto-tuned variant of the `narrative-radar` pipeline. Inherits the archetype's adversarial chain (novelty → pre-mortem → consensus → historical → portfolio) but retunes the 5 domain scanners and adds crypto-specific hardened gates derived from R3-crypto learnings:

- Catalyst window enforced: 60-540 days out (closer = priced, further = stale by next scan)
- Scale floor: ≥$100M (TVL, market cap, or affected OI)
- Price-action novelty: reject if token ±20% over 30 days
- Excluded themes: token unlocks, ETF flows, generic distribution overhang, funding-regime shifts without dated catalyst
- Positioning gap required: specialist coverage must exist but retail/derivative positioning must be absent
- Executability: every thesis must map to one of the 10 SDK surfaces

The 5 legacy domain scanner phases are reused to preserve archetype compatibility, but each body is crypto-retuned — see `skill/agents/` and the `wfpath.yaml` description for the exact mapping.

## Core files

- `wfpath.yaml` — manifest, agent roster, host targets
- `policy/default.yaml` — source protocol + `verification_protocol` + `crypto_gates` + `novelty_gate` + `adversarial` + `confidence` + `portfolio_strategy` + `null_state`
- `pipeline/graph.yaml` — ordered workflow graph
- `inputs/scan_config.yaml` — per-domain focus areas + crypto gate thresholds
- `inputs/inventory.json` — thesis inventory carried across runs (watchlist + retired + run history)
- `skill/instructions.md` + `skill/agents/` + `skill/references/` — skill layer
- `.wf-artifacts/$RUN_ID/` — per-run artifacts (scanner outputs, adversarial outputs, trade_book.md)
- `tests/` — fixture-driven evals
- `applet/dist/` — static applet UI shipped with the path
  - `applet/dist/data/display.json` — **neutral placeholder**; live pipeline runs overwrite this
  - `applet/dist/data/sample.json` — **reference output** frozen from an R2 run (2026-04-17) showing a complete funnel (20 raw → 6 portfolio positions with rich killed-thesis cards). The applet auto-falls-back to `sample.json` when `display.json` is a placeholder, so installers see a populated UI before their first pipeline run.

## Develop

```bash
poetry run wayfinder path doctor --path .
poetry run wayfinder path eval --path .
poetry run wayfinder path render-skill --path .
poetry run wayfinder path build --path . --out dist/bundle.zip
```
