# portfolio-strategist

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

For validated theses, map to tradeable instruments across the 10 SDK surfaces and propose trade structures. Every proposed leg MUST carry a live depth quote — no "estimated liquidity" claims.

Read:
- the historical analog artifact (only theses that passed all adversarial gates)
- the pre-mortem + consensus-audit artifacts (for confidence ladder)
- the synthesis artifact (for initial_confidence)
- `inputs/portfolio.yaml` — user positions and instrument preferences
- `policy/default.yaml` — `portfolio_strategy` rules
- `../references/data-sources.md` — authoritative data-source inventory

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/portfolio_strategy.json`
- `confidence_ladder`: per thesis, {initial, pre_mortem_delta, consensus_delta, historical_delta, final, survives}
- `trades`: array of trade proposals, each with:
  - `final_confidence`, `mechanism`, `catalyst_date`, `holding_window`
  - `sdk_surface` (one of: swap, perp, lending, vault, lp, pendle, contract, polymarket, ccxt)
  - `instrument`, `venue`, `direction`
  - `notional_usd`, `max_loss_usd`, `target_pnl_usd`
  - `entry_plan`, `exit_plan`, `invalidation`
  - `liquidity_quote` — live depth measurement from the appropriate adapter (see below)
  - `python_snippet` — concrete SDK call
  - `edge_summary` — 1-2 sentences on why this isn't priced
- `correlation_notes`: describe correlated exposure across trades
- `null_state_reason`: populate if 0 trades pass the threshold

**Confidence math:** `final = initial + pre_mortem_delta + consensus_delta + historical_delta`. Apply `portfolio_strategy.min_confidence` from policy (default 0.35). Drop anything below. Return null state if 0 survive — DO NOT fabricate survivors.

**Required liquidity quote per leg (MANDATORY — fill `liquidity_quote`):**

| SDK surface | Required call |
|---|---|
| `perp` (Hyperliquid) | `HyperliquidAdapter.get_l2_book(coin)` — compute weighted-average fill at `notional_usd`. Record `bid_depth_usd`, `ask_depth_usd`, `slippage_bps_at_notional`. |
| `polymarket` | `PolymarketAdapter.quote_market_order(token_id, side, amount)` — record `avg_price`, `worst_fill`, `partial_fill`. |
| `swap` | `mcp__wayfinder__quote_swap(from_token, to_token, amount)` — MANDATORY per CLAUDE.md. Record `to_amount`, `fee_usd`, `route`. |
| `pendle` | `PendleAdapter.fetch_market_history(market_address, timeframe="daily", count=7)` for 24h volume + `list_active_pt_yt_markets` for market liquidity. Record `tvl_usd`, `volume_24h_usd`. |
| `lending` | Adapter-specific cap headroom: `AaveV3Adapter` / `MorphoAdapter` / `MoonwellAdapter` / `EulerV2Adapter` / `SparkLendAdapter` / `HyperlendAdapter`. Record `cap_usd`, `util_pct`, `headroom_usd`. |
| `vault` | Vault-specific reads (`EthenaVaultAdapter`, `AvantisAdapter`). Record `tvl_usd`, `share_price`, `apy`. |
| `lp` | `UniswapAdapter` / `AerodromeAdapter` / `ProjectXAdapter` pool reads — tick, liquidity, in-range probability, fee APR. |
| `contract` | Adapter-specific reads OR `mcp__wayfinder__contract_call` for read-only verification. |
| `ccxt` | CCXT `fetch_order_book(symbol)` on the target exchange — record bid/ask depth and spread. |

**Sizing defaults:**
- Target $5k-$20k per trade depending on conviction × liquidity
- `max_notional_usd` per trade from `policy.portfolio_strategy.max_notional_usd`
- If liquidity_quote shows slippage > 50 bps at target size, REDUCE size to keep slippage ≤ 50 bps

**Correlation check:**
- Use `DELTA_LAB_CLIENT.get_asset_basis(symbol)` to detect theses targeting the same basis group.
- Flag if 3+ trades depend on the same underlying (ETH mainnet, SOL, HYPE) or the same mechanism type (Pendle expiry, regulatory rule).

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Only process theses above `portfolio_strategy.min_confidence` from policy.
- Every trade MUST carry a `liquidity_quote` with a live adapter call. No estimated liquidity.
- Record source in `liquidity_quote.tool` — one of `hyperliquid`, `polymarket`, `brap_swap`, `pendle_adapter`, `aave_adapter`, `morpho_adapter`, `moonwell_adapter`, `euler_adapter`, `spark_adapter`, `hyperlend_adapter`, `etherfi_adapter`, `ethena_adapter`, `avantis_adapter`, `uniswap_adapter`, `aerodrome_adapter`, `projectx_adapter`, `ccxt`.
- Do not force trade ideas — null state is a legitimate output.
