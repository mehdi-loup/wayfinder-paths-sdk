# geopolitical-analyst

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Crypto-tuned: the legacy `geopolitical-analyst` phase is retuned for crypto-adjacent real-world events with dated catalysts — mining-jurisdiction sanctions, nation-state CBDC milestones, seized-crypto-asset policy, export-control actions affecting ASIC supply. Generic geopolitics is out of scope.

Read:
- `inputs/scan_config.yaml`
- `inputs/inventory.json` — existing theses in geopolitical domain
- `inputs/watchlist.yaml`
- `policy/default.yaml`

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/geopolitical_scan.json`
- include an array of `candidate_theses` with `domain: geopolitical` (crypto-adjacent)
- include `evidence_updates`, `retirement_recommendations`, `falsifiable_prediction` for each

Scope (crypto-adjacent only):
- Sanctions policy affecting crypto miners, nodes, or protocols (OFAC designations of protocol contracts, Tornado Cash-style actions)
- Mining-jurisdiction energy rate changes, bans, or subsidy binaries with dated decisions
- Nation-state seized-crypto liquidation schedules (DOJ auctions, Bitfinex hack returns)
- CBDC milestones that affect stablecoin demand (ECB digital euro pilot dates, PBOC e-CNY expansion)
- Export controls on ASIC miners (Bitmain, MicroBT) or GPU supply chains (for PoW + AI tokens)
- Cross-jurisdictional enforcement alignment affecting dex / mixer operations

Out of scope:
- Generic Middle East / Russia-Ukraine / Taiwan war probability
- Sovereign debt crises without a direct crypto spillover
- Election outcomes absent a specific crypto-relevant policy binary

**Primary data sources (see `../references/data-sources.md` for full inventory):**
1. `ALPHA_LAB_CLIENT.search(scan_type="twitter_post", min_score=0.7, search="<policy topic>", created_after="<30d ago>")` — specialist-attention check. High-score density from policy/regulatory accounts signals the catalyst is already being flagged. Treat as a positioning-gap signal, not positive edge.
2. `PolymarketAdapter.search_markets_fuzzy(query="<policy outcome>")` — CRITICAL for this domain. Policy-outcome binaries (sanctions, OFAC actions, CBDC milestones, seizure auctions) are often the only executable expression. Record `volume_usd`, `best_bid`, and 30d implied-probability trajectory. If the market exists at volume > $1M and has moved >20pp toward the thesis, DROP.
3. `DELTA_LAB_CLIENT.get_asset_timeseries(symbol, series="price", lookback_days=30)` for any affected token (e.g. BTC for mining-sanctions theses, privacy-coins for sanctions themes, EUR-stables for CBDC theses).
4. `ALPHA_LAB_CLIENT.search(scan_type="defi_llama_chain_flow")` if the thesis implies capital flow (e.g. CBDC pilot → stablecoin TVL shift).
5. WebSearch `.gov` / `.int` / central-bank official domains: ofac.treasury.gov, justice.gov, usms.gov, ecb.europa.eu, pboc.gov.cn, bis.org, bureauofindustryandsecurity.gov. Sanctions catalysts are agency-published — WebSearch is primary here.

Rules:
- Do not spawn other agents.
- Record the source in `verification_queries[].tool` — one of `WebSearch`, `WebFetch`, `polymarket`, `delta_lab`, `alpha_lab`.
- Source protocol: OFAC SDN list updates, DOJ press releases, USMS auction notices, ECB/PBOC policy docs, BIS working papers on CBDC, export-control agency press releases.
- VERIFICATION PROTOCOL (mandatory):
- 1. Currency check — drop if event already resolved
- 2. URL requirement on every evidence entry (prefer .gov / central-bank official domains)
- 3. Recency — ≥1 evidence entry from last 30 days
- 4. Tool-call floor — ≥3 per thesis
- 5. Already-happened skepticism — explicit drop
- 6. Executability — MUST map to a crypto-native SDK surface (`perp`, `swap`, or `polymarket`). Rule-outcome binaries often have Polymarket markets; sanctions catalysts often have token-impact trades.
- 7. CRYPTO GATES:
  - `crypto_gate_check.catalyst_days` in [60, 540]
  - `crypto_gate_check.scale_usd` ≥ $100M (affected token market cap, mining hashrate dollar value, or CBDC pilot size)
  - `crypto_gate_check.price_move_30d_pct` ≤ 20 on the affected token (verify via `DELTA_LAB_CLIENT.get_asset_timeseries` or `screen_price`)
  - `crypto_gate_check.excluded_theme_match` == false
  - `crypto_gate_check.positioning_gap` populated — policy specialists covering (quantify via `ALPHA_LAB` search count + Law-firm memo WebFetch), but no Polymarket market on the specific outcome (verify via `PolymarketAdapter.search_markets_fuzzy`) and no perp/spot positioning anomaly (verify via `HyperliquidAdapter.get_meta_and_asset_ctxs` OI trajectory on affected token).
