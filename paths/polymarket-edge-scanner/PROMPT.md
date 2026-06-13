# Path: polymarket-edge-scanner

**Slug:** `polymarket-edge-scanner`
**Primary kind:** strategy
**Risk tier:** execution (research-only mode by setting `execution_enabled: false` in config)
**One-liner:** Surface mispriced Polymarket positions by combining base-rate libraries, catalyst detection (Alpha Lab), and orderbook microstructure — then size and execute through the existing Polymarket adapter.

---

## Market gap

The Polymarket adapter is fully wired (`mcp__wayfinder__polymarket`, `polymarket_execute`) but **zero paths use it**. Prediction markets are perpetually mispriced relative to base rates, and the adapter already handles USDC.e bridging, CLOB signing, position close, and redemption. The gap is the *intelligence layer* — a path that ranks markets by edge, attaches catalyst context, and sizes positions Kelly-style.

## Reference reading (load before implementing)

Skills:
- `/using-polymarket-adapter` — required before any execution call (USDC.e collateral, tradability filters, outcome selection)
- `/using-alpha-lab` — `twitter_post`, `defi_llama_chain_flow` for catalyst signals
- `/using-brap-adapter` — for funding from non-Polygon sources
- `/developing-wayfinder-paths`

Existing tooling to study:
- `wayfinder_paths/adapters/polymarket_adapter/` — adapter API surface
- `mcp__wayfinder__polymarket` action menu (`search`, `status`, `history`)
- `mcp__wayfinder__polymarket_execute` actions (`bridge_deposit`, `buy`, `sell`, `close_position`, `limit_order`, `redeem_positions`)
- Alpha Lab `search` resource shape

## Component plan

```
paths/polymarket-edge-scanner/
├── wfpath.yaml
├── README.md
├── inputs/
│   ├── config.yaml
│   ├── filters.yaml
│   └── base_rates.yaml
├── skill/
│   └── instructions.md
└── scripts/
    └── main.py
```

Single component. The path is read-heavy by design; execution is opt-in per-trade.

## Config slots

`inputs/config.yaml`:
```yaml
wallet: main
execution_enabled: true                  # set false for pure-research mode
risk:
  max_position_usd: 50
  max_concurrent_positions: 5
  max_portfolio_concentration_pct: 30    # any one market
  kelly_fraction: 0.25                   # fractional Kelly for sizing
funding:
  prefer_existing_usdce: true
  bridge_threshold_usdce: 5              # if USDC.e < this, bridge from native USDC
slippage_bps: 50
ledger_record: true
```

`inputs/filters.yaml`:
```yaml
min_orderbook_depth_usd: 500             # at top-of-book on intended side
min_24h_volume_usd: 5000
days_to_resolution:
  min: 1
  max: 90
allowed_categories: [politics, crypto, sports, business]   # exclude novelty/junk
exclude_keywords: [meme, joke]
min_edge_bps: 300                        # don't surface markets with < 3% edge
```

`inputs/base_rates.yaml`:
```yaml
# user-curated priors. Each entry maps a regex over market.title to a base-rate.
# Edge = base_rate - implied_prob_of_yes (signed; positive = buy YES, negative = buy NO)
priors:
  - pattern: "Fed.*(rate|cut|hike).*(by|before|in).*<DATE>"
    base_rate_yes: 0.55
    rationale: "Implied vol from Fed funds futures + dot plot deltas"
  - pattern: "Bitcoin.*above.*\\$\\d+k.*by.*<DATE>"
    base_rate_yes: null                  # null = compute from BTC IV / time-to-expiry / strike
    method: lognormal_drift
  # users add more
```

## Action surface

| Action | Args | Behavior |
|---|---|---|
| `scan` | `--limit N` | Read-only. Pulls active markets, applies tradability filters, computes implied prob from orderbook midpoint, looks up base rate, computes edge. Returns ranked table. |
| `research` | `--slug` | Pulls market description, related Alpha Lab posts (catalyst + sentiment), recent trade flow, and writes a one-paragraph summary. |
| `quote` | `--slug`, `--outcome` | Shows orderbook depth on the requested side, expected slippage at config max-position size, Kelly-suggested size given configured fraction. |
| `buy` | `--slug`, `--outcome`, `--amount?` | Default amount = Kelly-sized capped by `max_position_usd`. Bridges USDC.e if needed. |
| `status` | — | Open positions, PnL, unrealized + realized, days to resolution per market. |
| `close` | `--slug`, `--outcome` | Sells full position size at market. |
| `redeem` | `--condition-id?` | Sweeps all redeemable resolved positions, or one by condition_id. |

## Skill triggers

- "scan polymarket for edge"
- "what's mispriced on polymarket"
- "research <slug> on polymarket"
- "buy YES on <slug>"
- "redeem my polymarket positions"
- "close my polymarket position on <slug>"

## Safety rules

- **USDC.e is the only collateral.** Native Polygon USDC (0x3c499c…) does NOT work — the adapter handles bridging, but the path must not assume it's already USDC.e. Always check via `wayfinder://balances/{wallet}` before buy.
- **Kelly cap.** `kelly_fraction: 0.25` is the default — never let computed size exceed `max_position_usd` even if Kelly says larger.
- **Show edge before executing.** Don't buy without surfacing the implied prob, base rate, edge, and slippage at proposed size. Get confirmation unless user explicitly said "just buy".
- **Tradability check.** Confirm `is_active && !is_resolved && orderbook_depth >= filter` before any limit/buy attempt — markets close fast around resolution.
- **Resolution-day caution.** If `days_to_resolution < 1`, mark as "settling soon" and require explicit confirmation; payouts are at the binary, slippage spikes near close.

## Acceptance criteria

1. `scan` returns a ranked table where every row passes the configured filters and has both `implied_prob` and `edge_bps` populated.
2. `research` produces a coherent paragraph that cites at least one Alpha Lab post when one exists for the market topic.
3. `quote` correctly degrades when orderbook depth < requested size (suggests reducing size to fit top-of-book).
4. `buy` correctly bridges USDC → USDC.e if balance below threshold, then places order; ledger entry includes `condition_id`, `outcome_token_id`, fill price, fees.
5. `redeem` is idempotent — running twice on a resolved market doesn't double-credit and doesn't error on already-redeemed positions.
6. `execution_enabled: false` mode disables `buy`, `close`, `redeem` and reports them as research-only — `scan`, `research`, `quote`, `status` still work.
7. Smoke test runs `scan` against live market data and asserts at least one market passes filters; execution actions covered by mock adapter tests only (no live test orders).

## Build & publish

```
wayfinder path fmt --path .
wayfinder path doctor --path .
wayfinder path build --path .
wayfinder path publish --path .
```

## Out of scope (v0.1)

- Limit-order ladders / market-making strategies (separate `polymarket-mm` path).
- Auto-trading on signal — every buy requires explicit user confirmation in v0.1.
- Cross-event basket trades (e.g., correlated election markets) — single-market focus first.
- Probability calibration backtesting — assume the user-supplied base rates are reasonable; calibration belongs in a sibling research path.
