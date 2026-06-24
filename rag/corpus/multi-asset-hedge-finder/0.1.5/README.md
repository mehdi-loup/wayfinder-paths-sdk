# Hedge Finder

Given a portfolio, this path resolves the holdings into Delta Lab basis symbols,
measures factor exposure, screens Hyperliquid perps, evaluates hedge baskets
against the null state, runs risk gating, and emits a monitorable rebalance job.

## Pipeline

```
intake → exposure_reader → beta_modeler → hedge_search → optimizer → skeptic → risk_gate → compile_job → finalize
```

1. **Exposure Reader** — resolves symbols, fetches hourly price series from Delta Lab, builds portfolio return series
2. **Beta Modeler** — estimates rolling betas against BTC/ETH factors, measures hedge stability
3. **Hedge Searcher** — screens Hyperliquid perps for candidates by correlation, funding cost, OI, and spread
4. **Optimizer** — finds minimum-variance hedge weights subject to constraints
5. **Skeptic** — compares each hedge to doing nothing, rejects weak or disguised-directional hedges
6. **Risk Verifier** — applies leverage, liquidity, and cost limits; downgrades to draft if needed
7. **Job Compiler** — produces an armed or draft rebalance job with monitoring and invalidation rules

## Output contract

The final JSON payload always includes:

- `signal_snapshot`
- `selected_playbook`
- `candidate_expressions`
- `null_state`
- `risk_checks`
- `job`
- `next_invalidation`

Each orchestrator stage also writes an artifact under `.wf-artifacts/<run_id>/`:

- `exposure_reader.json`
- `beta_modeler.json`
- `hedge_search.json`
- `optimizer.json`
- `skeptic.json`
- `risk_gate.json`
- `job.json`
- `finalize.json`

## Key design decisions

- **Null state is always valid.** If no hedge materially reduces risk vs doing nothing, the path returns null.
- **Anti-disguised-trade check.** A hedge that flips net portfolio beta above 0.15 is rejected — it's a directional trade, not a hedge.
- **Funding cost gate.** Hedges where annualized funding drag exceeds the variance reduction benefit are rejected.
- **Draft mode.** When a hedge is promising but fails soft constraints (notional slightly over budget, funding marginally high), it's downgraded to draft rather than rejected outright.
- **Hard limits.** Leverage and liquidity floors are non-negotiable — failures here always result in rejection.

## Inputs

- `inputs/assets.yaml` — portfolio holdings (symbol, notional USD, side)
- `inputs/constraints.yaml` — hedge budget, leverage cap, max legs, funding cost limit

## Running

```bash
poetry run python examples/paths/hedge-finder/scripts/main.py
```

Optional overrides:

```bash
poetry run python examples/paths/hedge-finder/scripts/main.py \
  --assets examples/paths/hedge-finder/inputs/assets.yaml \
  --constraints examples/paths/hedge-finder/inputs/constraints.yaml \
  --policy examples/paths/hedge-finder/policy/default.yaml \
  --run-id hedge-demo-001
```

## Publish source

`examples/paths/hedge-finder` is the canonical path source for render/publish.
The active `.claude/skills/hedge-finder` tree is generated install output.

To publish a new version from this source:

```bash
poetry run wayfinder path publish --path examples/paths/hedge-finder
```
