# novelty-gate

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Filter out theses that are already mainstream by checking coverage volume, existing prediction-market pricedness, price action, and positioning saturation. This is the adversarial chain's first kill gate.

Read:
- the thesis synthesis artifact
- `policy/default.yaml` — novelty gate thresholds
- `../references/data-sources.md` — authoritative data-source inventory

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/novelty_gate.json`
- include a `surviving_theses` array — theses that passed the novelty check, each with the populated quantitative fields below
- include a `killed_theses` array — theses killed with the reason + evidence
- include for each surviving thesis a `novelty_score` (0-1) based on inverse mainstream saturation

**Required quantitative checks per thesis (ALL FOUR must be populated):**
1. **Polymarket pricedness** — `PolymarketAdapter.search_markets_fuzzy(query=<thesis angle>)`. If a market is found, also call `get_prices_history(token_id, lookback=30d)` and record `volume_usd`, `best_bid`, `best_ask`, `price_30d_change_pp`. KILL if `volume_usd > $1M` AND `price_30d_change_pp > 20pp` toward the thesis direction.
2. **Price-action** — `DELTA_LAB_CLIENT.get_asset_timeseries(symbol=<primary token>, series="price", lookback_days=30)`. Record `return_30d_pct`. KILL if `abs(return_30d_pct) > 20`.
3. **Positioning** — `HyperliquidAdapter.get_meta_and_asset_ctxs()` + `HYPERLIQUID_DATA_CLIENT.get_funding_history(coin, start_ms, end_ms)` for the affected token. Record `oi_now`, `oi_30d_change_pct`, `funding_now`, `funding_mean_30d`. KILL if `oi_30d_change_pct > 50` (positioning already loaded in thesis direction).
4. **Specialist coverage** — `ALPHA_LAB_CLIENT.search(search=<topic>, min_score=0.5, created_after="<30d ago>", limit=50)`. Record `results_count`, `top_score`. Flag as saturated if `results_count > 5 AND top_score >= 0.7` — this is positioning-gap evidence, not automatic kill.

**Secondary checks:**
- Mainstream media count — WebSearch `<topic> bloomberg.com OR reuters.com OR ft.com OR wsj.com OR cnbc.com 2026`. Record approximate article count. HIGH count alone is not a kill (relaxed mode), but combined with checks 1-3 it is.
- Crypto-native coverage — WebSearch theblock.co / coindesk.com / blockworks.co / delphi / kaiko / chaoslabs. Record count. Per R3 learning: absence is a WARNING (too small to matter), not edge.

**Relaxed mode (when `scan_config.filter_mode == "relaxed"` or `novelty_gate.relaxed_pass_on_angle == true`):**
Even if mainstream coverage is saturated, PASS the thesis if ALL of these hold:
- Polymarket volume on the SPECIFIC angle < $100k (or market does not exist)
- Price-action check <20%
- OI trajectory <50% 30d change
Record `novelty_notes.relaxed_pass_reason` explaining which leg remains uncrowded.

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Every entry in `surviving_theses[].novelty_evidence` must record `{check, value, tool}` where `tool` is one of `polymarket`, `delta_lab`, `hyperliquid`, `alpha_lab`, `WebSearch`.
- Adapters return `(ok, data)` tuples — destructure and check `ok`. Clients (ALPHA_LAB, DELTA_LAB) return data directly.
- Prefer the quantitative checks above over WebSearch-heavy filtering. WebSearch for mainstream article count is a supplement, not the primary signal.
