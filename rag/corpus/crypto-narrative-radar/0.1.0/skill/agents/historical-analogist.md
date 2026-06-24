# historical-analogist

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Find the closest historical parallel in crypto (prior protocol fork, prior fee switch, prior regulatory rule, prior Pendle expiry cascade, prior ETF flow regime). Quantify what actually happened using time-series data, not narrative memory.

Read:
- the consensus audit artifact
- the thesis synthesis artifact for full context
- `policy/default.yaml` — historical analog prompt
- `../references/data-sources.md` — authoritative data-source inventory

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/historical_analog.json`
- include for each thesis: `closest_historical_parallel`, `what_happened` (quantitative — reference the actual rate/price move from adapter history), `analog_outcome` ∈ {materialized, faded, front-run, mixed}, `base_rate` (0-1), `info_cycle_risk` (0-1, R3 crypto-specific: how likely is the edge to compress before entry?), `key_differences` from the analog, `confidence_delta` ∈ [-0.20, +0.08], `evidence` array with `{claim, source_url, tool, date}`.

**Primary data sources — USE THESE BEFORE WebSearch:**
1. **Protocol-adapter histories** are the authoritative source for analog outcomes:
   - `PendleAdapter.fetch_market_history(market_address, timeframe="daily", count=N)` — prior PT/YT expiry behavior (implied APY + TVL + price trajectory around the expiry date)
   - `MorphoAdapter.get_market_historical_apy(market_id, interval="daily")` — prior cap/utilization events
   - `HyperlendAdapter` lend-rate history — prior lending curve dislocations
   - `BorosAdapter.market_history(market_id, interval="1h"|"1d")` — prior fixed-rate curve resets
   - `AerodromeAdapter.sugar_epochs_latest` — prior gauge-vote incentive regime shifts
2. `HYPERLIQUID_DATA_CLIENT.get_funding_history(coin, start_ms, end_ms)` — bracket around prior analog dates (e.g. Pectra activation, Firedancer activation, UNIfication vote). Funding behavior 30d pre- and post-analog quantifies "sell-the-news" vs "front-run" vs "materialize".
3. `DELTA_LAB_CLIENT.get_asset_timeseries(symbol, series="price" | "funding" | "lending", lookback_days=N, as_of=<analog_date>)` — rate/price trajectory around the analog date. Use `as_of` to query the state at a historical timestamp.
4. `PolymarketAdapter.get_prices_history(token_id, start_ts, end_ts)` — analog binary outcomes (e.g. "did the rule-outcome market materialize as predicted 30 days pre-resolution?").
5. WebSearch ONLY for: narrative context, specific event dates, and dated research retrospectives. Do NOT use WebSearch to claim "the analog produced X rate move" — use the adapter history for that claim.

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Be specific about the parallel — not "prior protocol upgrade" but "the Dec 2025 Firedancer activation on SOL, where OI rose 40% in the 30d pre-event and SOL realized +6% on the activation day, then -12% in the following 14d."
- Quote actual numbers from adapter histories. A quantitative claim backed by a protocol history call is STRONGER evidence than a blog post.
- Crypto-specific base rates (R3 memory): protocol upgrades front-run in 70%+ of cases (buy-the-rumor-sell-the-news); fee-switch activations capture alpha at PROPOSAL not DEPLOY; mechanical DeFi plumbing events (Pendle expiries, Aave cap votes) show mixed outcomes depending on whether risk stewards front-run the demand.
- Record source in `evidence[].tool` — one of `pendle_adapter`, `morpho_adapter`, `boros_adapter`, `hyperlend_adapter`, `aerodrome_adapter`, `hyperliquid`, `delta_lab`, `polymarket`, `WebSearch`.
- If the analog faded/front-ran, that is strong evidence for rejection UNLESS the current situation has specific structural differences — enumerate them in `key_differences`.
