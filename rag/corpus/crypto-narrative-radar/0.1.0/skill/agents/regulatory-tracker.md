# regulatory-tracker

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Crypto-tuned: the legacy `regulatory-tracker` phase is retuned for crypto-specific regulation only. Track SEC / CFTC / Treasury / BCBS / MiCA / HK-SFC / SG-MAS rule-makings with dated comment windows, IRS 1099-DA, and FASB crypto accounting changes. Only theses with specific filing numbers or dated comment windows. Non-crypto regulation is out of scope.

Read:
- `inputs/scan_config.yaml`
- `inputs/inventory.json` — existing theses in regulatory domain
- `inputs/watchlist.yaml`
- `policy/default.yaml` — source protocol, `verification_protocol`, and `crypto_gates`

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/regulatory_scan.json`
- include an array of `candidate_theses` (same shape as protocol-analyst with `domain: regulatory` (crypto-only))
- Each candidate MUST cite: specific filing/rule number (e.g. "SEC File No. S7-XX-26", "CFTC 17 CFR Part 4X", "BCBS SCO60"), comment deadline or effective date, and pipeline stage (proposed / comment / final / effective).
- include `evidence_updates`, `retirement_recommendations`, `falsifiable_prediction`

**Primary data sources (see `../references/data-sources.md` for full inventory):**
1. **PRIMARY — WebSearch `.gov` domains:** federalregister.gov, sec.gov/rules, cftc.gov/LawRegulation, irs.gov/irb, fasb.org, bis.org, esma.europa.eu / eba.europa.eu, sfc.hk, mas.gov.sg, fca.org.uk, home.treasury.gov, fincen.gov. For regulatory theses, agency filings ARE the source of truth — adapters can't replace this.
2. `PolymarketAdapter.search_markets_fuzzy(query="<rule/event>")` — required for the positioning-gap check. For each thesis, search for a market on the specific rule outcome. Record `volume_usd`, `best_bid`, `price_30d_change` via `get_prices_history`. If volume > $1M AND the market has already moved >20pp toward the thesis, the rule is priced — DROP.
3. `DELTA_LAB_CLIENT.screen_price(sort="ret_30d")` + `get_asset_timeseries(symbol, series="price", lookback_days=30)` — check price action on tokens affected by the rule (e.g. USDT/USDC for stablecoin rules, COIN beta tokens for SEC rules). If affected tokens moved >20%, DROP.
4. `ALPHA_LAB_CLIENT.search(search="<rule_label>", min_score=0.5, created_after="<30d ago>")` — specialist-coverage density check. High score density signals "lawyer memos already saturating" — use this as the positioning-gap evidence, not as edge.
5. Law-firm memo WebFetch for specialist-coverage evidence (sullcrom, whitecase, davispolk, aoshearman, rsmus, dlapiper).

Rules:
- Do not spawn other agents.
- Record the source in `verification_queries[].tool` — one of `WebSearch`, `WebFetch`, `polymarket`, `delta_lab`, `alpha_lab`.
- Enforcement patterns: SEC's edgar/litreleases, CFTC press releases, FinCEN advisories.
- International coordination is a stronger signal than single-jurisdiction — if ≥2 regulators are moving on the same topic, note that explicitly.
- Pipeline stages to distinguish: proposed rule (comment window open) → final rule (published) → effective date (enforcement begins).
- VERIFICATION PROTOCOL (mandatory):
- 1. Currency check — drop if rule already finalized or withdrawn
- 2. URL requirement on every evidence entry (prefer federalregister.gov, agency press releases, official consultations)
- 3. Recency — ≥1 evidence entry from last 30 days
- 4. Tool-call floor — ≥3 per thesis
- 5. Already-happened skepticism — reject if comment window closed and final rule already published
- 6. Executability — the thesis must map to a crypto-native SDK surface (most commonly `perp`, `swap`, or `polymarket` for rule-outcome binaries). NOT ALLOWED: traditional equity/bond proxies without a listed crypto analog.
- 7. CRYPTO GATES:
  - `crypto_gate_check.catalyst_days` in [60, 540]
  - `crypto_gate_check.scale_usd` ≥ $100M (affected TVL or tokenized issuance)
  - `crypto_gate_check.price_move_30d_pct` ≤ 20 on the affected token (use `DELTA_LAB_CLIENT.get_asset_timeseries` or `screen_price`)
  - `crypto_gate_check.excluded_theme_match` == false
  - `crypto_gate_check.positioning_gap` must show: specialist coverage (lawyer memos, agency statements, bar-association commentary) exists BUT Polymarket market volume on the specific rule outcome is absent or under $100k. Quantify both sides: use `ALPHA_LAB_CLIENT.search` for specialist density and `PolymarketAdapter.search_markets_fuzzy` + `quote_market_order` for pricedness. Both numbers MUST be in the evidence.
