# macro-strategist

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Crypto-tuned: the legacy `macro-strategist` phase is retuned for DeFi-macro — research DeFi lending curves, stablecoin collateral regime shifts, LRT/restaking structural changes, and Pendle PT/YT term-structure dislocations with dated resolution. Traditional macro is out of scope.

Read:
- `inputs/scan_config.yaml` — domain config and focus areas
- `inputs/inventory.json` — existing theses in defi domain
- `inputs/watchlist.yaml` — user-specified tokens/vaults
- `policy/default.yaml` — source protocol, `verification_protocol`, and `crypto_gates` thresholds

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/macro_scan.json`
- include an array of `candidate_theses`, each with: `thesis_id`, `label`, `domain: macro` (DeFi-macro subclass), `mechanism`, `preconditions`, `evidence` (URL-required), `catalysts` (dated, ≥60 days out), `timeline_months`, `initial_confidence`, `currency_check`, `verification_queries` (≥3), `crypto_gate_check`, `executability`
- include `evidence_updates`, `retirement_recommendations`, `falsifiable_prediction`

**Primary data sources (see `../references/data-sources.md` for full inventory):**
1. `DELTA_LAB_CLIENT.screen_lending(sort="borrow_spike_score" | "util_now" | "net_supply_apr_now", basis="all")` — surface utilization spikes and APR regime shifts across venues without WebSearch.
2. `DELTA_LAB_CLIENT.screen_perp(sort="funding_mean_30d" | "basis_now", basis="all")` — funding/basis regime candidates.
3. `DELTA_LAB_CLIENT.screen_borrow_routes(sort="debt_ceiling_usd")` — collateral → debt route binaries (cap change catalysts).
4. `DELTA_LAB_CLIENT.get_basis_apy_sources(basis_symbol)` for each candidate symbol — cross-protocol rate comparison.
5. Adapter histories — use these to quantify the thesis mechanism:
   - `MorphoAdapter.get_market_historical_apy(market_id, interval)` for Morpho Blue / MetaMorpho APY trajectory
   - `PendleAdapter.fetch_market_history(market_address, timeframe)` for PT/YT implied APY + TVL candles
   - `HyperlendAdapter` lend-rate history (hours lookback)
   - `BorosAdapter.market_history(market_id, interval)` for fixed-rate tenor curve changes
6. `EthenaVaultAdapter` spot APY (already bakes reserve fund ratio in); `EtherFiAdapter` withdrawal-request queue state for LRT redemption-risk theses.
7. `POOL_CLIENT.get_pools(project="pendle" | "aave" | "morpho" | "ethena" | ...)` for TVL scale gate and pool discovery.
8. WebSearch ONLY for governance forum CDP/cap-vote dates, Snapshot IDs, and research-desk memos with dated commentary.

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Record the source in `verification_queries[].tool` — one of `delta_lab`, `morpho_adapter`, `pendle_adapter`, `boros_adapter`, `hyperlend_adapter`, `ethena_adapter`, `etherfi_adapter`, `pool_client`, `WebSearch`, `WebFetch`.
- Adapter returns are `(ok, data)` tuples — destructure and check `ok` before using. Clients (ALPHA_LAB, DELTA_LAB, POOL_CLIENT) return data directly.
- Look for:
  - Term-structure dislocations between PT expiries
  - Collateral regime stress (reserve fund ratio, delta-neutral hedge ratio)
  - Cap / utilization binary events (CDP cap changes, market cap increases, gauge vote decisions)
  - LRT concentration / redemption queue depth shifts
- VERIFICATION PROTOCOL (mandatory):
- 1. Currency check — drop if catalyst already fired or superseded
- 2. URL requirement on every evidence entry
- 3. Recency — ≥1 evidence entry from last 30 days
- 4. Tool-call floor — ≥3 per thesis
- 5. Already-happened skepticism — explicit drop
- 6. Executability — SDK surface + listed instrument + liquidity
- 7. CRYPTO GATES:
  - `crypto_gate_check.catalyst_days` in [60, 540]
  - `crypto_gate_check.scale_usd` ≥ $100M (vault TVL, pool TVL, or protocol TVL)
  - `crypto_gate_check.price_move_30d_pct` ≤ 20 — use `DELTA_LAB_CLIENT.get_asset_timeseries(symbol, series="price", lookback_days=30)` as the authoritative check
  - `crypto_gate_check.excluded_theme_match` == false
  - `crypto_gate_check.positioning_gap` must be populated with concrete evidence
