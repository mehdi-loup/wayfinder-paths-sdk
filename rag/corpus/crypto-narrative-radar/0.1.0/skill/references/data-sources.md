# Data Sources for Crypto Narrative Radar

This is the authoritative inventory of structured data sources agents in this pipeline MUST use before falling back to WebSearch. Rule: for every quantitative claim (price, rate, OI, funding, TVL, market volume, implied probability), prefer an adapter/client call over a web-search snippet.

## Source inventory (what each uniquely provides)

### Alpha Lab — `ALPHA_LAB_CLIENT` / `wayfinder://alpha-lab/...`
Scored (0-1 `insightfulness_score`) feed of: scored tweets, DefiLlama chain flows, DefiLlama protocol highlights, Delta Lab top-APY picks, Delta Lab best delta-neutral picks.
- **Strength:** Cross-cuts twitter + chain-flow + quant-yield in one ranked stream with time bounds.
- **Caveat:** High score = mainstream attention = likely already priced. Use as a specialist-coverage proxy, NOT a positive-edge signal.

### Delta Lab — `DELTA_LAB_CLIENT` / `wayfinder://delta-lab/...`
Cross-venue rate aggregator + timeseries engine over Hyperliquid, Moonwell, Boros, Hyperlend, Pendle, and more.
- `get_basis_apy_sources(basis_symbol, lookback_days)` — all yield opportunities for an asset
- `get_best_delta_neutral_pairs(basis_symbol)` — hedged-carry pair candidates + Pareto frontier
- `screen_price(sort, basis)` — `ret_1d / ret_7d / ret_30d / vol_7d / mdd_30d` cross-asset (use for the 20%-30d price gate)
- `screen_lending(sort, basis)` — `net_supply_apr_now / util_now / borrow_spike_score` cross-venue
- `screen_perp(sort, basis)` — `funding_now / funding_mean_30d / basis_now / oi_now / volume_24h`
- `screen_borrow_routes(sort, basis, borrow_basis)` — `ltv_max / liq_threshold / debt_ceiling_usd`
- `get_asset_timeseries(symbol, series, lookback_days)` — DataFrame with `price`, `lending`, `funding`
- **APY values are decimal floats** (0.98 = 98%). Always check for `null` and `warnings[].stale_data`.

### Pool / Token / Balance — `POOL_CLIENT`, `TOKEN_CLIENT`
Broadest pool-screening surface (DefiLlama-merged) + canonical token resolution.
- `POOL_CLIENT.get_pools(chain_id, project)` → `matches[]` with tvlUsd, apy, apyBase, apyReward
- `POOL_CLIENT.get_pools_by_ids([...])` for shortlists
- `TOKEN_CLIENT.get_token_details(...)` for address↔coingecko↔decimals↔chain resolution

### Hyperliquid adapter — market data
- `HyperliquidAdapter.get_meta_and_asset_ctxs()` → perp roster with OI, funding, mark price, leverage caps
- `HyperliquidAdapter.get_spot_meta()` / `get_spot_assets()`
- `HyperliquidAdapter.get_l2_book(coin)` / `get_spot_l2_book(asset_id)` — book depth for size checks
- `HYPERLIQUID_DATA_CLIENT.get_funding_history(coin, start_ms, end_ms)` — hourly funding, ~7mo retention
- `HyperliquidAdapter.get_user_state(address)` for any public address (positioning visibility)

### Polymarket adapter — prediction markets
- `PolymarketAdapter.search_markets_fuzzy(query)` — Gamma fuzzy search (filter for `enableOrderBook && acceptingOrders && !closed`)
- `PolymarketAdapter.list_markets(order="volume24hr", ascending=False)` — trending
- `PolymarketAdapter.get_event_by_slug(slug)` — coherent market sets (e.g. MVP brackets)
- `PolymarketAdapter.get_prices_history(token_id, start_ts, end_ts)` — implied-probability timeseries
- `PolymarketAdapter.quote_market_order(token_id, side, amount)` — weighted-average execution price at a given size
- MCP: `mcp__wayfinder__polymarket(action="search" | "status" | "quote" | "price_history" | "trending" | "get_market")`

### CCXT adapter — multi-CEX
- Tickers, orderbooks, OHLCV, funding (per exchange), balances/positions across Binance/Bybit/OKX/Aster/dYdX/etc.
- Best for cross-exchange spread detection and liquidity cross-check.

### Protocol adapters (research-unique reads)
Only list fields that aren't already exposed via Delta Lab / PoolClient:

| Adapter | Unique research read |
|---|---|
| **Aave V3** | Reward composition per reserve, supply/borrow cap headroom, liquidation thresholds |
| **Morpho** | Market `warnings` flags, MetaMorpho vault composition, **`get_market_historical_apy(interval)`** |
| **Pendle** | PT/YT fixed vs floating split, days-to-expiry, **`fetch_market_history(timeframe)`** — APY + TVL + price candles |
| **Ethena** | sUSDe spot APY embeds reserve fund ratio; cooldown queue state |
| **Ether.fi** | Async withdrawal-request queue + finalization state |
| **Eigencloud** | Queued withdrawal metadata, operator delegation, restaking share accounting |
| **Moonwell** | mToken↔underlying map + per-market reward breakdown |
| **Hyperlend** | Buffer-based market health, **lend-rate history (hours lookback)** |
| **Avantis** | Vault manager buffer ratio |
| **Uniswap V3** | Tick/sqrtPrice state + in-range probability math + fee APR estimation |
| **Aerodrome classic** | Vote-weighted pool efficiency, **Sugar epoch history** (fees/bribes/emissions), ve lock state |
| **Aerodrome Slipstream** | In-range probability, `sigma_annual` from swap logs, deployment variants |
| **ProjectX** | Subgraph swap history (time-windowed), points balances |
| **Boros** | Fixed-rate tenor curve, funding term structure, **market_history (5m/1h/1d/1w)**, vault TVL/tenor/collateral |
| **SparkLend** | Stable vs variable borrow APY split |
| **Euler V2** | Vault perspective taxonomy, EVC batching metadata |

### MCP wallet / balances / contracts
- `wayfinder://wallets/{label}` / `wayfinder://balances/{label}` / `wayfinder://activity/{label}` — internal state, portfolio-strategist only
- `wayfinder://contracts/{chain_id}/{address}` — ABIs for deployed research contracts

## Data-source → pipeline-stage mapping

### Scanner stage (intake + domain scans)

| Scanner | Primary data sources (in order of preference) |
|---|---|
| **tech-scout** (protocol) | 1. `ALPHA_LAB` scan_type `defi_llama_protocol` for protocol highlights · 2. `DELTA_LAB.screen_price(sort="ret_30d")` to enforce 20%-30d price gate · 3. `POOL_CLIENT.get_pools(project=...)` for protocol TVL scale gate · 4. WebSearch governance forums / GitHub issues for dated catalysts |
| **macro-strategist** (DeFi-macro) | 1. `DELTA_LAB.screen_lending(sort="borrow_spike_score")` · 2. `DELTA_LAB.screen_perp(sort="funding_mean_30d")` · 3. Adapter histories: `MorphoAdapter.get_market_historical_apy`, `PendleAdapter.fetch_market_history`, `HyperlendAdapter` lend-rate history, `BorosAdapter.market_history` · 4. `EthenaVaultAdapter` for sUSDe APY + reserve-embedded spot rate · 5. WebSearch governance forums for cap-vote dates |
| **regulatory-tracker** (crypto regulation) | 1. WebSearch `federalregister.gov / sec.gov / cftc.gov / esma.europa.eu / bis.org` (PRIMARY — government filings are the source of truth) · 2. `PolymarketAdapter.search_markets_fuzzy` for rule-outcome binaries (volume = pricedness check) · 3. `DELTA_LAB.screen_price` for 30d price impact on affected tokens |
| **structural-analyst** (microstructure) | 1. `HyperliquidAdapter.get_meta_and_asset_ctxs()` for OI + funding + leverage · 2. `HYPERLIQUID_DATA_CLIENT.get_funding_history` · 3. `DELTA_LAB.screen_perp(sort="funding_now")` · 4. CCXT adapter for cross-exchange spread · 5. `ALPHA_LAB` `defi_llama_chain_flow` for validator/staking capital flow · 6. WebSearch for specific upgrade dates |
| **geopolitical-analyst** (crypto-adjacent) | 1. `ALPHA_LAB.search(scan_type="twitter_post", min_score=0.7)` for specialist attention · 2. `PolymarketAdapter.search_markets_fuzzy` for policy-outcome markets · 3. `DELTA_LAB.screen_price` for affected-token impact check · 4. WebSearch `.gov / .int` for sanctions, export-control, CBDC events |

### Novelty gate (adversarial — is this priced?)
**Every surviving thesis MUST have all of these populated:**
1. `PolymarketAdapter.search_markets_fuzzy` on the specific catalyst angle. Record `volume_usd`, `best_bid`, `best_ask`, `30d_price_move`. Killed if volume > $1M AND mid-price has already moved >20pp toward the thesis direction in last 30d.
2. `DELTA_LAB.get_asset_timeseries(symbol, series="price", lookback_days=30)` for the primary thesis token. Killed if abs(30d return) > 20%.
3. `HYPERLIQUID_DATA_CLIENT.get_funding_history` + `get_meta_and_asset_ctxs` for OI/funding trajectory. Killed if OI is up >50% over 30d (positioning already loaded).
4. `ALPHA_LAB.search(search="<topic>", min_score=0.5, created_after="<30d ago>")` — record count and top 3 scores. More than 5 results at score ≥0.7 = saturated specialist coverage.

### Pre-mortem (assume it's wrong — has it already partly unwound?)
1. `DELTA_LAB.get_asset_timeseries(series="funding"|"lending")` — has the rate already moved in the opposite direction to the thesis?
2. `HYPERLIQUID_DATA_CLIENT.get_funding_history` — same signal for perps.
3. Protocol adapter histories (Morpho, Pendle, Boros, Hyperlend) — regime-change verification.
4. WebSearch only for fresh counter-catalysts not reflected in rates yet.

### Consensus audit (what does informed consensus say?)
1. `ALPHA_LAB.search(scan_type="defi_llama_overview" | "defi_llama_protocol" | "defi_llama_chain_flow")` — where is capital actually flowing?
2. `ALPHA_LAB.search(scan_type="delta_lab_top_apy" | "delta_lab_best_delta_neutral")` — quant-consensus screening picks. If your thesis is the reverse of a high-score Alpha Lab pick, note the tension.
3. `DELTA_LAB.get_best_delta_neutral_pairs` — is the hedged expression of this thesis on the Pareto frontier, or is a competing pair dominating?
4. WebSearch for Delphi / Kaiko / Chaos Labs / Steakhouse / Galaxy published notes.

### Historical analog (what did the last analog event actually do?)
1. Protocol adapter histories (`MorphoAdapter.get_market_historical_apy`, `PendleAdapter.fetch_market_history`, `BorosAdapter.market_history`, `HyperlendAdapter` lend-rate history, `AerodromeAdapter` Sugar epochs) — the ONLY way to quantitatively verify a prior analog actually produced a rate dislocation.
2. `HYPERLIQUID_DATA_CLIENT.get_funding_history` bracketed around the analog date.
3. `DELTA_LAB.get_asset_timeseries` around the analog date.
4. `PolymarketAdapter.get_prices_history` for analog binary outcomes.

### Portfolio strategist (sizing + liquidity)
**Every proposed trade leg MUST carry a live depth quote recorded in `primary_leg.liquidity_check`:**
- Perp leg → `HyperliquidAdapter.get_l2_book(coin)` slippage check at proposed notional
- Polymarket leg → `PolymarketAdapter.quote_market_order(token_id, side, amount)` weighted-avg execution
- Spot swap leg → a `mcp__wayfinder__quote_swap` quote (MANDATORY per CLAUDE.md)
- Pendle leg → `PendleAdapter.fetch_market_history` 24h volume + `PendleAdapter` active-markets liquidity
- Lending leg → adapter-specific cap headroom (`AaveV3Adapter` / `MorphoAdapter` / `MoonwellAdapter`)
- CCXT leg → cross-exchange fill-quality check if size > $10k

## Rules of use

1. **Adapter call > WebSearch snippet** for any quantitative claim. WebSearch is for qualitative context (dates, proposals, filings), not for rates / prices / TVL / OI / funding.
2. **APY = decimal float** in Delta Lab. Multiply by 100 for display.
3. **Filter `apy.value is None`** in Delta Lab results.
4. **Respect adapter gotchas** — some return `(ok, data)` tuples (adapters), others return data directly (clients). See `CLAUDE.md` scripting gotchas.
5. **Record the tool in `verification_queries[]`** — every evidence entry MUST carry `{query, url_or_symbol, tool}` where `tool` is one of: `WebSearch`, `WebFetch`, `alpha_lab`, `delta_lab`, `pool_client`, `hyperliquid`, `polymarket`, `<protocol_adapter>`, `ccxt`.
6. **Stale-data check** — if a Delta Lab response has `warnings[].stale_data` on the instrument you cite, either refresh the call or drop the evidence.
