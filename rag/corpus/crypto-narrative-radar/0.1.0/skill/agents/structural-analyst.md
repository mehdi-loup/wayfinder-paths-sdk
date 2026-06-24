# structural-analyst

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Crypto-tuned: the legacy `structural-analyst` phase is retuned for crypto market microstructure — research CEX perp funding regimes, ETF creation/redemption flow shifts, validator/staking concentration changes, and MEV/orderflow supply-chain structural shifts. Traditional structural themes (demographics, energy transition) out of scope.

Read:
- `inputs/scan_config.yaml`
- `inputs/inventory.json` — existing theses in structural domain
- `inputs/watchlist.yaml`
- `policy/default.yaml`

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/structural_scan.json`
- include an array of `candidate_theses` (same shape as protocol-analyst with `domain: structural` (crypto-microstructure))
- include `evidence_updates`, `retirement_recommendations`, `falsifiable_prediction`

**Primary data sources (see `../references/data-sources.md` for full inventory):**
1. `HyperliquidAdapter.get_meta_and_asset_ctxs()` — full perp roster with OI, funding, mark, leverage. Destructure `(ok, data)`; `data[0]` is meta, `data[1]` is ctxs. Use for OI-by-coin, funding-now, and leverage-cap reads.
2. `HYPERLIQUID_DATA_CLIENT.get_funding_history(coin, start_ms, end_ms)` — hourly funding history, ~7mo retention. Use this for funding-regime verification and for the "has funding already moved" pre-mortem signal.
3. `DELTA_LAB_CLIENT.screen_perp(sort="funding_mean_30d" | "oi_now" | "basis_now", basis="all")` — cross-venue perp snapshot; supplement with `screen_perp(basis="<symbol>")` for a specific asset.
4. `ALPHA_LAB_CLIENT.search(scan_type="defi_llama_chain_flow", min_score=0.5)` for validator/staking capital flow shifts and for ETF-flow candidate seeding. Also `scan_type="twitter_post"` for MEV/upgrade chatter from specialist accounts.
5. CCXT adapter for cross-exchange spread/funding (Binance/Bybit/OKX) vs Hyperliquid, useful for identifying venue-specific microstructure regime changes.
6. WebSearch ONLY for specific upgrade dates (MEV-Boost, PBS, Lido Snapshot votes, ETF issuer decisions) and for dashboards without API exposure (rated.network, beaconcha.in, mevboost.pics).

Rules:
- Do not spawn other agents.
- Record the source in `verification_queries[].tool` — one of `hyperliquid`, `delta_lab`, `alpha_lab`, `ccxt`, `WebSearch`, `WebFetch`.
- Focus on STRUCTURAL shifts with dated catalysts (e.g. upcoming MEV-Boost upgrade, upcoming proposer-builder separation, upcoming Lido dominance-reduction snapshot vote, ETF issuer approval for in-kind redemptions) — NOT intraday funding-rate prints.
- Avoid "funding rate spike → mean reversion" and "CVD divergence → reversal" — those are saturated short-horizon trades, not narrative-radar material.
- VERIFICATION PROTOCOL (mandatory):
- 1. Currency check — drop if the structural change already activated
- 2. URL requirement on every evidence entry
- 3. Recency — ≥1 evidence entry from last 30 days
- 4. Tool-call floor — ≥3 per thesis
- 5. Already-happened skepticism — explicit drop
- 6. Executability — SDK surface + listed instrument
- 7. CRYPTO GATES:
  - `crypto_gate_check.catalyst_days` in [60, 540]
  - `crypto_gate_check.scale_usd` ≥ $100M (affected open interest, staked TVL, or daily flow)
  - `crypto_gate_check.price_move_30d_pct` ≤ 20 (if the structural thesis has a direct token proxy) — verify via `DELTA_LAB_CLIENT.get_asset_timeseries` or `screen_price`
  - `crypto_gate_check.excluded_theme_match` == false (reject pure funding-regime themes without a specific catalyst)
  - `crypto_gate_check.positioning_gap` populated with concrete evidence — e.g. "validator concentration reduction vote scheduled but no snapshot derivatives or Polymarket market exists on the outcome"
