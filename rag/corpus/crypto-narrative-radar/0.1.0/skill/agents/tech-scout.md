# tech-scout

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Crypto-tuned: research crypto protocol architecture changes, tokenomics updates (fee switches, buybacks, emissions), governance binaries, and dated mainnet milestones with catalysts 60-540 days out. The legacy `tech-scout` phase is retuned here for crypto protocol tech, not general-purpose tech.

Read:
- `inputs/scan_config.yaml` — domain config and focus areas (crypto-tuned)
- `inputs/inventory.json` — existing theses in protocol domain
- `inputs/watchlist.yaml` — user-specified protocols
- `policy/default.yaml` — source protocol, `verification_protocol`, and `crypto_gates` thresholds

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/tech_scan.json`
- include an array of `candidate_theses`, each with: `thesis_id`, `label`, `domain: tech` (crypto-protocol), `mechanism` (causal chain), `preconditions` (list with met/unmet status), `evidence` (list — EACH entry MUST contain `source_url` with a real https:// URL returned by WebSearch/WebFetch, `source_name`, `quality` (high/medium/low), `date` in YYYY-MM-DD, `summary`), `catalysts` (list with `event` and `estimated_date` strictly in the future AND at least 60 days out), `timeline_months`, `initial_confidence`, `currency_check` (object: `searched_for`, `already_happened` bool, `evidence_url`, `last_updated`), `verification_queries` (array of `{query, url, tool}` records, length >= 3), `crypto_gate_check` (object: `catalyst_days`, `scale_usd`, `price_move_30d_pct`, `excluded_theme_match`, `positioning_gap`), `executability` (object: `tier` = `A`|`B`|`reject`, `primary_leg` = {surface in [swap,perp,lending,vault,lp,pendle,contract,polymarket,ccxt], instrument (concrete symbol/contract/market_slug), venue, liquidity_check (quantified volume/OI/depth)}, `proxy_basis`)
- include `evidence_updates` for existing protocol theses
- include `retirement_recommendations`
- include `falsifiable_prediction` for each new thesis

**Primary data sources (see `../references/data-sources.md` for full inventory):**
1. `ALPHA_LAB_CLIENT.search(scan_type="defi_llama_protocol", min_score=0.5, created_after="<30d ago>")` — surface protocol highlights scoring high with specialist attention. Also `scan_type="twitter_post", min_score=0.7` for governance/roadmap chatter.
2. `DELTA_LAB_CLIENT.screen_price(sort="ret_30d", basis="all")` + per-token `get_asset_timeseries(symbol, series="price", lookback_days=30)` — enforce the 20%-30d price gate WITHOUT WebSearch.
3. `POOL_CLIENT.get_pools(project=<protocol>)` — verify the $100M+ scale gate with actual TVL, not a blog number.
4. Per-protocol adapter histories (`MorphoAdapter.get_market_historical_apy`, `PendleAdapter.fetch_market_history`, `HyperlendAdapter` lend-rate history, `BorosAdapter.market_history`) — quantify that a governance binary would actually change a rate/utilization.
5. WebSearch ONLY for: dated governance forum posts, Snapshot vote IDs, protocol GitHub issues, audit report pages. Skip WebSearch for any quantitative value that a client/adapter above returns.

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Record the source in `verification_queries[].tool` — one of `alpha_lab`, `delta_lab`, `pool_client`, `<protocol>_adapter`, `WebSearch`, `WebFetch`.
- Favor dated governance binaries over narrative trades.
- VERIFICATION PROTOCOL (mandatory — a thesis that fails any check MUST be dropped, not downgraded):
- 1. Currency check: BEFORE writing any candidate thesis, run at least one WebSearch for `"<event label>" 2026` to confirm the catalyst has NOT already fired, been cancelled, or been superseded. Record in `currency_check` and drop the thesis if `already_happened` is true.
- 2. URL requirement: every `evidence[]` entry MUST carry `source_url` containing a real, accessible https:// URL. No human-readable source labels without URLs.
- 3. Recency: at least one `evidence[]` entry per thesis MUST have a `date` within the last 30 days AND a verifiable `source_url`.
- 4. Tool-call floor: issue at least 3 WebSearch/WebFetch calls per candidate thesis BEFORE writing it. Your total tool_uses count must be >= 3 * number_of_theses.
- 5. Already-happened skepticism: explicitly ask "has this catalyst already fired, been cancelled, or been superseded?" — any `true` in `currency_check.already_happened` means DROP.
- 6. Executability check: every thesis MUST declare `executability` with a concrete `primary_leg.surface` and a specific listed instrument with quantified liquidity. `reject` tier must DROP.
- 7. CRYPTO GATES (additional — required for this path):
  - `crypto_gate_check.catalyst_days` MUST be between `policy.crypto_gates.min_catalyst_days` (60) and `policy.crypto_gates.max_catalyst_days` (540). Anything closer is likely priced; anything further is stale by next scan.
  - `crypto_gate_check.scale_usd` (protocol TVL or token market cap) MUST be ≥ `policy.crypto_gates.min_scale_usd` ($100M). Below this, edge can't compound.
  - `crypto_gate_check.price_move_30d_pct` MUST be ≤ 20. Use `DELTA_LAB_CLIENT.get_asset_timeseries(symbol, series="price", lookback_days=30)` or `screen_price` (PRIMARY). CoinGecko/DefiLlama WebFetch is a fallback only. If the token has moved >20% in the last 30 days, the thesis is priced — DROP.
  - `crypto_gate_check.excluded_theme_match` MUST be false. Reject any thesis whose core mechanism is a token unlock calendar, ETF flow tracking, generic distribution overhang, or funding-regime-shift-without-specific-catalyst.
  - `crypto_gate_check.positioning_gap` MUST be non-empty — explicitly name the specialist coverage that exists alongside the retail/derivatives positioning that is absent (e.g. "governance forum 80+ comments, but CEX perp OI flat and no Polymarket market on the specific vote").
