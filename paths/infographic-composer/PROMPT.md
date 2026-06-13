# Path: infographic-composer

**Slug:** `infographic-composer`
**Primary kind:** dashboard
**Risk tier:** read-only
**One-liner:** Create a small set of high-demand protocol-data infographics: how a protocol works, where stablecoin rates are best, and what a specific market/pool/vault looks like.

---

## Product focus

This path is not a general-purpose design generator. It specializes in the protocol questions users and agents ask most often:

- How does this protocol work?
- Where are the best stablecoin rates right now?
- What is happening in this specific market, pool, vault, or position surface?
- How do similar protocols compare for the same user job?

The output should feel like a polished one-page protocol card: accurate, legible, reusable by other agents, and grounded in protocol docs, protocol state, market data, chain data, and read-only source data rather than invented protocol lore.

## Public render content contract

Adapters and SDK metadata are implementation details. The generated `infographic.html`, `infographic.svg`, and `alt_text.md` must not show adapter slugs, SDK internals, Wayfinder internals, manifest paths, local YAML filenames, cache filenames, or read-method names as user-facing content.

Public content must describe the protocol or market directly:
- Use protocol names, product names, chain names, market IDs, assets, current metrics, charts, official documentation, public API/data sources, and public media such as protocol logos or favicons.
- Always show protocol logos when a public domain/logo source is known. Use a monogram only as a fallback when no public logo source is available.
- Always shorten EVM addresses in public HTML/SVG/alt text as `0x123...abcd`; keep full addresses only in JSON artifacts.
- `how-it-works` must prioritize protocol mechanism from official docs: actors, state transitions, incentives, constraints, lifecycle, and risk surfaces.
- `stablecoin-rates` must rank only actually fetched protocol rates and include illustrative protocol media when available.
- `market-snapshot` must include graph data such as price history, funding history, utilization history, or order-book depth when the market source exposes it.
- `compare-protocols` must compare protocol behavior and user jobs, not adapter capability inventories.

Internal artifacts may retain adapter names, manifests, source files, call metadata, and validation mappings for traceability. Those details stay in JSON files and validation output unless the invoking agent explicitly asks for implementation diagnostics.

## Protocol scope

Build first-class support for protocol adapters that exist in `wayfinder_paths/adapters/`. Adapters are internal selectors and read connectors; the rendered infographic uses protocol-facing labels.

Primary protocol adapters:
- `aave_v3_adapter` - Aave V3 lending and borrowing.
- `aerodrome_adapter` - Aerodrome liquidity, gauges, ve locks, voting, rewards.
- `aerodrome_slipstream_adapter` - Aerodrome Slipstream concentrated liquidity.
- `avantis_adapter` - Avantis vaults and positions.
- `boros_adapter` - Boros markets, vaults, collateral, positions.
- `eigencloud_adapter` - EigenCloud restaking, delegation, rewards.
- `ethena_vault_adapter` - Ethena vault and APY surfaces.
- `etherfi_adapter` - Ether.fi staking, wrapping, withdrawals.
- `euler_v2_adapter` - Euler V2 lending and borrowing.
- `hyperlend_adapter` - HyperLend markets, rates, lending, borrowing.
- `hyperliquid_adapter` - Hyperliquid markets, funding, orderbook, positions.
- `lido_adapter` - Lido staking, wrapping, withdrawals.
- `moonwell_adapter` - Moonwell lending, borrowing, collateral, rewards.
- `morpho_adapter` - Morpho markets, vaults, rewards, allocation.
- `pendle_adapter` - Pendle PT/YT markets, history, prices, swaps.
- `polymarket_adapter` - Polymarket markets, orderbooks, positions.
- `projectx_adapter` - ProjectX liquidity positions and pool actions.
- `sparklend_adapter` - SparkLend lending and borrowing.
- `uniswap_adapter` - Uniswap liquidity positions, pools, fees.

Supporting data adapters:
- `pool_adapter` for pool discovery and analytics.
- `token_adapter` for token metadata, prices, and gas.
- `balance_adapter` for wallet balance context when explicitly requested.
- `ledger_adapter` for historical activity context when explicitly requested.
- `brap_adapter` only for read-only quote/route visuals; no execution.
- `multicall_adapter` only for read aggregation.
- `ccxt_adapter` only for centralized exchange context when relevant.

If a requested protocol has no adapter in this repo, the path must stop and say it is unsupported in v0.1 instead of inventing a generic infographic.

## Reference reading (load before implementing)

Repo docs:
- `README.md` Paths section - path manifests, build, doctor, and publish flow.
- `AGENTS.md` Data accuracy and adapter guidance - fetch rates/APYs/funding via adapters/clients when possible; do not guess.
- `wayfinder_paths/paths/scaffold.py` - path scaffold patterns for `dashboard` primary kind.
- `wayfinder_paths/paths/doctor.py` - manifest, component, skill, and static asset validation rules.

Adapter sources:
- `wayfinder_paths/adapters/<adapter>/manifest.yaml` - authoritative capability list.
- `wayfinder_paths/adapters/<adapter>/README.md` - protocol-specific usage notes.
- `wayfinder_paths/adapters/<adapter>/examples.json` when present - canonical example inputs and outputs.
- `wayfinder_paths/adapters/<adapter>/adapter.py` only when the manifest/README do not explain the available read methods.

## Component plan

```
paths/infographic-composer/
|-- wfpath.yaml
|-- README.md
|-- inputs/
|   |-- request.yaml
|   `-- style.yaml
|-- data/
|   |-- jobs.yaml
|   |-- mechanics.yaml
|   |-- risks.yaml
|   `-- support.yaml
|-- skill/
|   `-- instructions.md
|-- scripts/
|   `-- main.py
`-- tests/
    `-- fixtures/
        |-- aave-v3-how-it-works.yaml
        |-- usdc-stablecoin-rates.yaml
        |-- hyperliquid-btc-market-snapshot.yaml
        `-- stablecoin-lending-compare.yaml
```

Use:

```
poetry run wayfinder path init infographic-composer --dir paths --kind dashboard --no-applet --skill
```

Then replace the generated implementation.

## Input Specs

Fixtures may use YAML equivalents of the CLI arguments. Keep the runtime interface CLI-first so other agents can call a specific infographic product directly.

`inputs/request.yaml`:
```yaml
kind: how_it_works              # how_it_works | stablecoin_rates | market_snapshot | compare_protocols
adapter: morpho_adapter         # required for how_it_works and market_snapshot
adapters: []                    # compare_protocols only; 2-4 adapter names
protocol_name: "Morpho"         # optional; infer from adapter when omitted
asset: USDC                     # stablecoin_rates only
chains: [8453, 42161, 1, 999]   # stablecoin_rates only; optional
use_case: stablecoin_lending    # compare_protocols only
market: null                    # market_snapshot only; symbol, pool, market, or vault id
include_live_data: true         # read-only adapter/client calls only
apy_normalization_version: "apy-normalization-v1"
output:
  directory: ".wf-artifacts/$RUN_ID"
  filename_base: "morpho-protocol-infographic"
```

`inputs/style.yaml`:
```yaml
palette:
  background: "#f7f8fb"
  ink: "#141821"
  muted: "#667085"
  accent: "#2563eb"
  positive: "#059669"
  warning: "#d97706"
  danger: "#dc2626"
layout:
  target_width: 1080
  target_height: 1350
  density: medium
  show_source_footer: true
accessibility:
  min_contrast_ratio: 4.5
```

## Public action surface

Keep the user-facing action set product-specific. Do not expose generic `create`, `brief`, `plan`, `render`, `revise`, `export`, or `validate` commands in v0.1.

| Action | Args | Behavior |
|---|---|---|
| `how-it-works` | `--adapter`, `--docs-mode off|metadata|fetch?`, `--metrics-mode off|live?`, `--chain?`, `--source-artifact?`, `--style?`, `--serve?` | Builds a protocol mechanics infographic: actors, state, mechanism flow, constraints, protocol notes, public sources, and optional read-only live protocol metrics. |
| `stablecoin-rates` | `--asset USDC|USDT|DAI`, `--chain?`, `--venue?`, `--min-tvl?`, `--source-artifact?`, `--style?`, `--serve?` | Builds a best-rates infographic for stablecoin lending/vault/fixed-yield opportunities available through supported protocols. Uses read-only live data only; omitted values are shown as unavailable, never guessed. |
| `market-snapshot` | `--adapter`, `--market`, `--chain?`, `--source-artifact?`, `--style?`, `--serve?` | Builds a one-market infographic for a pool, lending market, vault, PT market, order book, or LP position surface, including graphs when market data is available. |
| `compare-protocols` | `--adapters`, `--use-case`, `--asset?`, `--chain?`, `--source-artifact?`, `--style?`, `--serve?` | Builds a side-by-side infographic comparing 2-4 protocols for the same user job, such as stablecoin lending, LP, perp trading, or restaking. |
| `preview` | `--run-id`, `--serve?` | Prints the static `infographic.html` path for an existing run. If `--serve` is set, starts an explicit local static server for run artifacts and prints `preview_url`. |

Each infographic action internally resolves protocol support, fetches optional read-only live data, loads official docs metadata when requested, plans layout, validates facts/layout, exports files, and prints one JSON result envelope.

## Infographic products

### `how-it-works`

Purpose: answer "how does this protocol work?" for one supported protocol.

Template:

1. **Protocol identity** - protocol name, category, supported chains if known, and current context.
2. **What it does** - one plain-English sentence based on official docs and protocol-specific mechanism data.
3. **Core mechanics** - 3-5 static wizard steps using a numbered rail, protocol-facing step labels, serif step headlines, concise bodies, and optional value/badge callouts.
4. **Protocol data** - current read-only metrics when available, such as market count, TVL/liquidity, utilization, active assets, funding, or other protocol-state indicators.
5. **Protocol notes** - checked risk/mechanism notes such as liquidation, impermanent loss, withdrawal queues, funding sign, oracle/market depth, or bridge/collateral constraints.
6. **Sources footer** - official docs, protocol homepage, and public protocol/chain data sources. Do not render manifest paths or local source files.

### `stablecoin-rates`

Purpose: answer "where are the best stablecoin rates right now?" across supported stablecoin yield protocols.

Internal candidate venues:
- Lending markets: `aave_v3_adapter`, `morpho_adapter`, `euler_v2_adapter`, `hyperlend_adapter`, `moonwell_adapter`, `sparklend_adapter`.
- Vault/fixed-yield surfaces: `morpho_adapter`, `ethena_vault_adapter`, `pendle_adapter`, `avantis_adapter` only when the adapter exposes a read-only APY/rate surface for the requested stablecoin.
- Supporting data: `token_adapter`, `pool_adapter`, `brap_adapter` quote-only context when relevant.

Template:

1. **Best rate leaderboard** - protocol venue, chain, asset, net APY/rate, TVL/liquidity if available, protocol logo/public media, and timestamp.
2. **Rate quality flags** - fixed vs variable, lending vs vault vs PT/fixed-yield, utilization/cap/liquidity warnings when available.
3. **Safety notes** - liquidation/no liquidation, withdrawal/lockup, bridge/cross-chain friction, oracle/market depth, and rate volatility from checked protocol notes plus live-data flags.
4. **Methodology footer** - public data source labels, missing venues, timestamp, normalization version, and filters. Do not render adapter slugs or call names.

Hard rule: if no live/read-only rate can be fetched for a venue, mark it `unavailable` and exclude it from the ranking.
Default rule: apply `inputs/request.yaml:min_tvl_usd` to rankable rows so dust or zero-liquidity markets do not become the default "best rate." Callers can override with `--min-tvl`.

### `market-snapshot`

Purpose: answer "what is this specific market/pool/vault?" for a known protocol and market identifier.

Template:

1. **Market identity** - protocol, chain, market/pool/vault id, assets.
2. **Current snapshot** - APY/funding/fee APR/TVL/order-book depth/utilization/range state as appropriate and available.
3. **Graphs** - price/funding/utilization history, liquidity depth, or order-book depth when the market source provides enough data.
4. **Mechanics mini-flow** - how a user interacts with this market through the protocol.
5. **Risk panel** - market-specific risks from checked protocol notes, with live read data used only for current state flags.
6. **Sources footer** - official docs, protocol homepage, and public protocol/market data sources.

### `compare-protocols`

Purpose: answer "which protocol fits this job better?" for 2-4 supported protocols.

Supported `--use-case` values:
- `stablecoin-lending` - compare Aave V3, Morpho, Euler V2, HyperLend, Moonwell, SparkLend, and compatible vault surfaces.
- `lp` - compare Uniswap, Aerodrome, Aerodrome Slipstream, and ProjectX LP surfaces.
- `perps` - compare Hyperliquid, Avantis, Boros, and related market/funding surfaces when read-only data is available.
- `restaking` - compare Lido, Ether.fi, EigenCloud, and restaking/staking surfaces.

`prediction-markets` is out of scope for `compare-protocols` in v0.1 because there is only one first-class prediction-market adapter. Polymarket remains supported for `how-it-works` and `market-snapshot`.

Template:

1. **Comparison headline** - use case, protocols compared, asset/chain filter if provided.
2. **Protocol matrix** - rows are normalized user jobs; columns are protocols; cells show public protocol behavior, available current metrics, or explicit unavailable labels.
3. **Live metric row** - APY, funding, fee APR, depth, TVL, or utilization when fetched through read-only calls. Unavailable values are explicit.
4. **Tradeoff strip** - where each protocol is strongest: simplicity, capital efficiency, fixed yield, liquidity depth, range control, collateral flexibility, or execution surface.
5. **Risk comparison** - protocol-specific risks from checked protocol notes, with read data used only for current state flags.
6. **Best-fit notes** - short, sourced guidance for each protocol; no generic winner unless the data supports it.
7. **Sources footer** - official docs, protocol homepages, and public protocol/market data sources for every compared protocol.

Hard rules:
- Require 2-4 protocols selected by adapter arguments.
- All compared protocols must map to the same `--use-case`; reject mismatched sets such as Lido vs Polymarket for `stablecoin-lending`.
- Protocol matrix rows must come from checked-in `data/jobs.yaml`, not ad hoc row labels.
- Never rank protocols by a metric that was unavailable for one or more contenders; show "insufficient comparable data" instead.

## Output Spec

Every successful infographic action writes a run folder under `.wf-artifacts/$RUN_ID/`.
These are runtime artifacts for the invoking agent/user; `.wf-artifacts` is not included in SDK path bundles.
The primary v0.1 render target is static HTML, with SVG as the embedded/canonical visual source.

```
.wf-artifacts/$RUN_ID/
|-- adapter_inventory.json
|-- request.json
|-- data_snapshot.json
|-- live_snapshot.json
|-- design_spec.json
|-- infographic.html
|-- infographic.svg
|-- alt_text.md
`-- validation.json
```

Artifact contracts:
- `adapter_inventory.json` - internal parsed manifest capabilities, README path, and examples path for every adapter referenced in this run. This file is for traceability only and must not be rendered as public infographic content.
- `request.json` - resolved CLI/YAML input including `apy_normalization_version`.
- `data_snapshot.json` - normalized rows (APY normalization v1), upstream-path metadata when `--source-artifact` was used, and `cache_status`.
- `live_snapshot.json` - raw adapter/client responses keyed by `<adapter>.<method>` source call, before normalization. `{}` when `include_live_data: false`.
- `design_spec.json` - resolved palette, density, section order, target dimensions, and computed measurements used to lay out the visual.
- `infographic.html` - public rendered card (see Serving model) with no adapter/SDK/local-file implementation details.
- `infographic.svg` - SVG render of the same public content as HTML.
- `alt_text.md` - accessible description of the infographic.
- `validation.json` - shape defined below.

`validation.json` must include:
- `ok`: boolean.
- `errors`: blocking issues.
- `warnings`: non-blocking issues.
- `source_coverage`: every rendered claim mapped to official docs, public protocol source, public market/chain source, checked internal catalog, or read-only source call.
- `layout_checks`: overflow, contrast, viewport fit, missing legend/source footer.
- `adapter_checks`: requested adapter/venue exists, capability groups parsed, live data calls used only when read-only.
- `risk_checks`: every rendered risk note maps to `data/risks.yaml`.
- `job_checks`: every `compare-protocols` matrix row maps to `data/jobs.yaml`.
- `apy_checks`: every rendered APY/rate has normalized fields and the expected `apy_normalization_version` when the infographic compares or ranks yield.

`validation.json`, `adapter_inventory.json`, `data_snapshot.json`, and `live_snapshot.json` may include adapter and SDK details for debugging. `infographic.html`, `infographic.svg`, and `alt_text.md` may not.

Final stdout envelope:

```json
{
  "ok": true,
  "action": "stablecoin-rates",
  "kind": "stablecoin_rates",
  "adapter": null,
  "venues": ["aave_v3_adapter", "morpho_adapter", "euler_v2_adapter"],
  "protocol_name": null,
  "asset": "USDC",
  "apy_normalization_version": "apy-normalization-v1",
  "run_id": "infographic-composer-20260512-153000",
  "artifacts_dir": ".wf-artifacts/infographic-composer-20260512-153000",
  "primary_artifact": ".wf-artifacts/infographic-composer-20260512-153000/infographic.html",
  "files": {
    "html": ".wf-artifacts/infographic-composer-20260512-153000/infographic.html",
    "svg": ".wf-artifacts/infographic-composer-20260512-153000/infographic.svg",
    "validation": ".wf-artifacts/infographic-composer-20260512-153000/validation.json"
  },
  "preview_url": null,
  "validation": {
    "ok": true,
    "warnings": [],
    "errors": []
  }
}
```

On failure, print the same envelope shape with `ok: false`, clear `errors`, and exit non-zero.

Failure stdout envelope:

```json
{
  "ok": false,
  "action": "stablecoin-rates",
  "kind": "stablecoin_rates",
  "run_id": null,
  "artifacts_dir": null,
  "primary_artifact": null,
  "files": {},
  "preview_url": null,
  "errors": [
    {
      "code": "unsupported_adapter",
      "message": "Adapter is not supported for stablecoin-rates in v0.1.",
      "field": "venue",
      "hint": "Run a supported action or use a template-compatible adapter only for how-it-works."
    }
  ],
  "warnings": []
}
```

Failure rules:
- `errors[].code` must be stable enough for agents to branch on.
- Use `unsupported_adapter`, `unsupported_use_case`, `missing_required_arg`, `read_call_failed`, `normalization_failed`, `risk_catalog_missing`, `job_taxonomy_missing`, `layout_validation_failed`, or `internal_error`.
- Never return only prose on failure.

## Serving model

Static HTML is the primary v0.1 serving surface. A path applet is a v0.2 stretch, not part of the v0.1 spec.

- The generated `infographic.html` must be self-contained except for sibling `infographic.svg` if referenced with a relative URL.
- Generated infographic files under `.wf-artifacts/<run_id>/` are not served by default.
- `preview --run-id <id>` prints the static HTML path and the primary artifact metadata.
- `preview --run-id <id> --serve` may start a local static server for the run folder and print `preview_url`.
- Do not rely on `wayfinder path preview` in v0.1 because no applet is declared.

## Data accuracy rules

- Never invent APY, funding, TVL, utilization, market counts, fees, or position values.
- For live values, use adapter/client calls where available. If fetching fails, render the infographic without that live metric and record the missing call in `validation.warnings`.
- Before using external docs, read the adapter's `manifest.yaml`, README, examples, and relevant adapter read methods.
- For adapter calls, remember: adapters return `(ok, data)` tuples; clients return data directly.
- Do not execute fund-moving actions. This path is read-only even when an adapter manifest exposes write capabilities.
- Visible source labels should point to official protocol docs, protocol homepages, public market APIs, public chain data, or user-provided source artifacts. Do not show adapter files, SDK method names, local YAML files, or manifests in the public footer.

## Support Matrix Spec

`data/support.yaml` defines which adapters each product may use and at what confidence level.

Minimum shape:

```yaml
products:
  how-it-works:
    verified: [aave_v3_adapter, morpho_adapter, pendle_adapter]
    template_compatible: [hyperliquid_adapter, uniswap_adapter, polymarket_adapter]
    unsupported: []
  stablecoin-rates:
    verified: [aave_v3_adapter, morpho_adapter, moonwell_adapter]
    template_compatible: [euler_v2_adapter, hyperlend_adapter, sparklend_adapter]
    unsupported: [hyperliquid_adapter, polymarket_adapter]
  market-snapshot:
    verified: [hyperliquid_adapter, pendle_adapter, morpho_adapter]
    template_compatible: [uniswap_adapter, projectx_adapter, polymarket_adapter]
    unsupported: []
  compare-protocols:
    use_cases:
      stablecoin-lending:
        verified: [aave_v3_adapter, morpho_adapter, moonwell_adapter]
        template_compatible: [euler_v2_adapter, hyperlend_adapter, sparklend_adapter]
        unsupported: [polymarket_adapter]
```

Rules:
- `verified` means covered by fixtures and at least one successful end-to-end run for that product/use case.
- `template_compatible` means the adapter manifest can populate the template, but live data and edge cases are not fully verified.
- `unsupported` means reject for that product with `unsupported_adapter`.
- Render the support level in `validation.json.adapter_checks`.

## Read-Only Method Allowlist

Each product must restrict live calls to a checked-in or code-level allowlist. The allowlist can be generated from manifests, but must be explicit before calls are made.

Default allowed capability patterns:
- `how-it-works`: manifest/README/examples only; no live adapter call required.
- `stablecoin-rates`: `market.list`, `market.read`, `market.apy`, `market.stable_markets`, `market.assets_view`, `market.rate_history`, `vault.read`, `pendle.market.snapshot`, `pendle.router_static.rates`, `pool.read`, `pool.discover`, `token.read`, `token.price`, `gas.estimate`.
- `market-snapshot`: `market.read`, `market.meta`, `market.funding`, `market.candles`, `market.orderbook`, `market.state`, `market.history`, `vault.read`, `position.read`, `pool.read`, `*.pool.state`, `*.pool.overview`, `*.analytics.*`, `pendle.market.snapshot`, `pendle.market.history`, `pendle.prices.*`.
- `compare-protocols`: union of the relevant read-only patterns for the selected `--use-case`.

Denied patterns:
- Any capability containing `execute`, `deposit`, `withdraw`, `lend`, `unlend`, `borrow`, `repay`, `stake`, `unstake`, `claim`, `vote`, `mint`, `burn`, `increase`, `decrease`, `transfer`, `send`, `open`, `close`, `swap`, `bridge`, `authorize`, or `reallocate`.

Rules:
- A denied pattern always wins over an allowed pattern.
- If an adapter exposes only write methods for a desired datum, mark the datum unavailable.
- Log allowed/denied call decisions into `data_snapshot.json`.
- The public render should describe the unavailable protocol datum, not the denied method or adapter implementation reason.

## Job taxonomy spec

`compare-protocols` must use checked-in `data/jobs.yaml` for protocol matrix rows. This prevents each comparison from inventing different row labels for the same user job.

Minimum shape:

```yaml
use_cases:
  stablecoin-lending:
    label: Stablecoin lending
    rows:
      - id: supply_stablecoin
        label: Supply stablecoin
        capability_patterns: ["lending.lend", "vault.deposit"]
      - id: withdraw_stablecoin
        label: Withdraw stablecoin
        capability_patterns: ["lending.unlend", "vault.withdraw"]
      - id: read_market_rate
        label: Read market rate
        capability_patterns: ["market.read", "market.apy", "vault.read"]
      - id: read_position
        label: Read position
        capability_patterns: ["position.read"]
  lp:
    label: Liquidity providing
    rows:
      - id: discover_pool
        label: Discover pool
        capability_patterns: ["pool.discover", "*.pool.discover", "*.pool.get"]
      - id: provide_liquidity
        label: Provide liquidity
        capability_patterns: ["pool.add_liquidity", "position.mint", "lp.deposit"]
      - id: collect_fees
        label: Collect LP fees
        capability_patterns: ["position.collect", "lp.claim_fees"]
      - id: stake_lp
        label: Stake LP / gauge
        capability_patterns: ["gauge.deposit", "lp.stake", "*.gauge.stake"]
      - id: read_position
        label: Read LP position
        capability_patterns: ["position.read", "lp.read"]
  perps:
    label: Perp trading
    rows:
      - id: read_funding_rate
        label: Read funding rate
        capability_patterns: ["market.funding", "*.funding.read", "funding.*"]
      - id: read_market_state
        label: Read market state
        capability_patterns: ["market.read", "orderbook.read", "market.oi"]
      - id: open_position
        label: Open position
        capability_patterns: ["order.market", "order.limit", "position.open"]
      - id: close_position
        label: Close position
        capability_patterns: ["position.close", "order.reduce"]
      - id: read_position
        label: Read position
        capability_patterns: ["position.read", "account.read"]
  restaking:
    label: Restaking and liquid staking
    rows:
      - id: stake_native
        label: Stake native asset
        capability_patterns: ["staking.stake", "vault.deposit"]
      - id: wrap_liquid_token
        label: Wrap liquid staking token
        capability_patterns: ["staking.wrap", "token.wrap"]
      - id: request_withdraw
        label: Request withdraw
        capability_patterns: ["staking.request_withdraw", "withdraw.queue", "withdraw.request"]
      - id: delegate_operator
        label: Delegate to operator
        capability_patterns: ["restaking.delegate", "operator.delegate"]
      - id: claim_rewards
        label: Claim rewards
        capability_patterns: ["rewards.claim", "staking.claim"]
      - id: read_position
        label: Read position
        capability_patterns: ["position.read", "staking.read"]
```

Rules:
- `capability_patterns` use Python `fnmatch` semantics (shell-style glob, case-insensitive). `*` matches any sequence of characters; `?` matches a single character. Internally, a cell is eligible when at least one of the row's patterns matches a capability id in the adapter's `manifest.yaml`.
- Every supported `compare-protocols --use-case` must have a `data/jobs.yaml` entry.
- Matrix row order is the order in `jobs.yaml`.
- A cell is supported only when at least one adapter manifest capability matches a row pattern, but the rendered cell must use protocol-facing labels.
- Render unknown or missing capabilities as `not exposed`, not as unsupported by the protocol itself.
- Validation fails if a rendered comparison row does not map to a `jobs.yaml` row id.

## APY Normalization Spec

Any infographic that ranks or compares APY/rate values must normalize them before rendering. Store the normalized rows in `data_snapshot.json` and persist source metadata for each row.

Version: `apy-normalization-v1`.

Persist the version in three places:
- `request.json` as `apy_normalization_version`.
- Every normalized rate row in `data_snapshot.json` as `apy_normalization_version`.
- `validation.json.apy_checks.version`.

Required normalized fields:

```json
{
  "apy_normalization_version": "apy-normalization-v1",
  "venue": "morpho_adapter",
  "protocol": "Morpho",
  "chain_id": 8453,
  "asset": "USDC",
  "market_id": "0x...",
  "rate_type": "supply_apy",
  "rate_kind": "variable",
  "gross_apy": 0.0521,
  "net_apy": 0.0498,
  "fee_apy": 0.0023,
  "reward_apy": 0.0,
  "reward_tokens": [],
  "compounding": "adapter_reported",
  "sampling_window": "current",
  "timestamp": "2026-05-12T15:30:00Z",
  "source": {
    "adapter": "morpho_adapter",
    "method": "market.read",
    "artifact": null
  },
  "normalization_notes": []
}
```

Rules:
- Reject unknown `apy_normalization_version` values unless the implementation explicitly supports them.
- `rate_type` must be one of `supply_apy`, `borrow_apy`, `vault_apy`, `pt_fixed_yield`, `funding_apr`, `lp_fee_apr`, `lp_reward_apr`, or an explicit adapter-defined string accompanied by a `normalization_notes` entry explaining the value. Do not invent new types silently.
- `gross_apy` is the protocol-reported headline yield before explicit known protocol or venue fees.
- `net_apy` is the value used for rankings. If fees are unknown, set `net_apy` to `null`, mark `fee_status: "unknown"`, and exclude the row from ranked comparisons.
- `reward_apy` must be separated from base yield when the source exposes reward-token yield. Include `reward_tokens`; do not silently blend rewards into base APY.
- `fee_apy` must include protocol/performance/management fees only when the source or checked-in metadata exposes them. Do not estimate fees.
- `compounding` must be one of `adapter_reported`, `simple_annualized`, `continuous`, `fixed_maturity`, or `unknown`.
- `sampling_window` must be explicit: `current`, `1h`, `24h`, `7d`, `30d`, `fixed_maturity`, or the exact adapter-provided window.
- For Pendle/fixed-maturity surfaces, label the value as fixed-maturity implied APY and include maturity date when available.
- For Hyperliquid/perp funding, do not mix funding APR with lending APY in the same ranking unless the infographic is explicitly about carry and labels the metric as funding APR.
- For LP surfaces (Uniswap, Aerodrome, Aerodrome Slipstream, ProjectX), use `lp_fee_apr` for trading-fee yield and `lp_reward_apr` for gauge/incentive rewards. Do not blend LP fee APR with lending APY in the same ranking.

## Risk Catalog Spec

Risk copy must come from checked-in `data/risks.yaml`, not ad hoc inference at render time.

Minimum shape:

```yaml
adapters:
  aave_v3_adapter:
    protocol: Aave V3
    risks:
      - id: liquidation
        severity: high
        applies_to: [borrow, collateral]
        text: "Borrowing against collateral can be liquidated if account health falls below protocol thresholds."
        sources:
          - "wayfinder_paths/adapters/aave_v3_adapter/README.md"
      - id: variable_rates
        severity: medium
        applies_to: [supply, borrow]
        text: "Supply and borrow rates are variable and can change with utilization."
        sources:
          - "wayfinder_paths/adapters/aave_v3_adapter/manifest.yaml"
```

Rules:
- Every adapter referenced internally by `how-it-works`, `market-snapshot`, or `compare-protocols` must have a `data/risks.yaml` entry.
- `stablecoin-rates` may rank only venues whose internal adapter has a risk entry, or it must put that venue in an "unverified risk catalog" section outside the ranking.
- Risk notes can be filtered by action/market context, but the text must be copied or lightly templated from `risks.yaml`.
- Validation fails if a rendered risk note lacks a risk id and source path.
- Live read data may add state flags such as "utilization high" or "cap nearly full", but those flags are separate from durable protocol risk copy.

## Scan cache

Read-heavy scans should use a local cache to avoid inconsistent snapshots and repeated adapter calls.

- Cache path: `.wf-cache/infographic-composer/scan_cache.json`.
- Default TTL: 300 seconds for `stablecoin-rates` and `compare-protocols`; 60 seconds for `market-snapshot`.
- Cache key includes action, adapter set, asset, chain, market, min TVL, source artifact hash, and APY normalization version.
- Store raw adapter responses separately from normalized rows.
- Include `cache_status` in `data_snapshot.json`: `hit`, `miss`, `refresh`, or `disabled`.
- Do not include `.wf-cache` in published bundles.

## Using other Paths

Other Paths can be useful, but they are optional upstream sources, not the canonical source of truth.

Supported usage:
- Accept `--source-artifact <path>` for JSON/YAML/Markdown artifacts produced by another Path run.
- If a future implementation adds `--use-path <slug>`, only invoke an installed path through `wayfinder path exec` when the target action is explicitly read-only and returns a machine-readable stdout envelope.
- Record upstream metadata in `data_snapshot.json`: path slug, version if known, run id, artifact path, timestamp, and the action that produced it.
- Record upstream path artifacts in JSON traceability. Public source footers should cite only the protocol, market, chain, or user-provided data source represented by that artifact.

Good optional upstreams when available:
- `stablecoin-yield-rotator` scan/quote artifacts for `stablecoin-rates`.
- `concentrated-lp-manager` status/scan artifacts for LP `market-snapshot` cards.
- `funding-rate-harvester` discover/status artifacts for perp funding snapshots.
- `polymarket-edge-scanner` research/quote artifacts for Polymarket market snapshots.

Hard limits:
- Do not require another Path to be installed for the core v0.1 flow.
- Do not trust another Path's prose summary as live data. Prefer structured fields and source metadata.
- Do not let an upstream Path execute deposits, swaps, orders, claims, votes, withdrawals, or any fund-moving action.
- If upstream artifacts conflict with fresh adapter/client reads, prefer the fresh read and add a validation warning.

## Design rules

- The infographic must be readable at `1080x1350` and in a mobile preview.
- Use SVG/HTML live text for all labels, legends, numbers, and source notes.
- Do not use AI-generated raster images for text, charts, or protocol facts.
- Use a polished protocol-card layout: header, protocol mechanism flow, current data snapshot, charts/graphs where available, protocol notes, source footer, and protocol media/logos when helpful.
- Keep text dense but scannable. Prefer short labels and grouped capability chips over paragraphs.
- Include alt text that names the protocol, explains the core mechanism, lists the key data points, and mentions any missing live data.
- Public text must read as protocol research, not as an adapter or SDK diagnostics report.

## Skill triggers (`skill/instructions.md`)

- "make a how-it-works infographic for <protocol>"
- "show me how <protocol> works"
- "make an infographic of the best USDC rates"
- "show the best stablecoin rates"
- "make a market snapshot for <market>"
- "visualize this pool/vault/market"
- "compare Aave and Morpho"
- "make a protocol comparison infographic"
- "compare these protocols for stablecoin lending"
- "preview the protocol infographic"

## Acceptance criteria

1. `how-it-works --adapter aave_v3_adapter --docs-mode fetch` produces a one-page Aave V3 mechanics infographic that uses official protocol docs when they are reachable and does not expose adapter/SDK/local-file details in public content.
2. `how-it-works --adapter pendle_adapter` explains PT/YT market mechanics as protocol behavior, not as adapter capabilities.
3. `stablecoin-rates --asset USDC` ranks only venues with successfully fetched read-only live rates, shows unavailable venues separately, includes protocol logos/public media where available, and does not show method names publicly.
4. `market-snapshot --adapter hyperliquid_adapter --market BTC` includes at least one graph when public market data is reachable, such as price history or order-book depth; otherwise it records a warning and omits the unavailable graph.
5. `compare-protocols --adapters aave_v3_adapter,morpho_adapter,euler_v2_adapter --use-case stablecoin-lending --asset USDC` produces a protocol-facing matrix with comparable user jobs, live metrics only where fetched, and explicit unavailable cells.
6. Unsupported adapters/protocol names fail clearly with a list of supported adapters.
7. No public action invokes write/execution adapter capabilities.
8. The generated SVG and HTML have no text overflow, include a source footer, and pass contrast checks.
9. `preview --run-id <id>` prints the static `infographic.html` path; `preview --run-id <id> --serve` prints a local `preview_url`.
10. `wayfinder path doctor --path paths/infographic-composer` passes; `wayfinder path fmt --path paths/infographic-composer` is clean.
11. Public HTML/SVG/alt text contain no visible adapter slugs, SDK/Wayfinder implementation text, manifest paths, local YAML filenames, or cache filenames.
12. Public HTML/SVG/alt text shorten all EVM addresses and show protocol logos for known protocols.
13. `how-it-works` mechanism flow uses the static infographic wizard pattern from the design handoff, not a plain card grid.

## Golden Fixture Expectations

Each public infographic function must have at least one golden fixture under `tests/fixtures/` and must produce these files:

- `request.json`
- `data_snapshot.json`
- `design_spec.json`
- `infographic.html`
- `infographic.svg`
- `validation.json`

Required fixtures:
- `aave-v3-how-it-works.yaml` -> `how-it-works --adapter aave_v3_adapter`
- `usdc-stablecoin-rates.yaml` -> `stablecoin-rates --asset USDC`
- `hyperliquid-btc-market-snapshot.yaml` -> `market-snapshot --adapter hyperliquid_adapter --market BTC`
- `stablecoin-lending-compare.yaml` -> `compare-protocols --adapters aave_v3_adapter,morpho_adapter,moonwell_adapter --use-case stablecoin-lending --asset USDC`

Fixture validation rules:
- Golden outputs compare JSON schemas and required keys, not byte-for-byte HTML.
- `validation.json.ok` must be true for golden fixtures.
- HTML/SVG must include a source footer and no placeholder text such as `TODO`, `lorem`, or `unknown` unless it is an explicit unavailable-data label.
- Fixture runs may use cached/mock adapter responses, but the cache status must be visible in `data_snapshot.json`.
- Fixture checks must scan public HTML/SVG/alt text for implementation leaks listed in the public render content contract.

## Build & publish

Build rules:
- Static HTML is primary. Do not add applet build or `wayfinder path preview --check` to the v0.1 release gate.
- The skill runtime must execute the main script through the path runtime: `wayfinder path exec --path-dir <path> --component main -- <args>`. Reject generated skill exports that point at a stale component, applet preview, or direct artifact file.
- Inspect `bundle.zip` before publish. It must include `wfpath.yaml`, `scripts/main.py`, `inputs/request.yaml`, `inputs/style.yaml`, `data/jobs.yaml`, `data/risks.yaml`, `data/support.yaml`, `README.md`, and `skill/instructions.md`; it must not include `.wf-artifacts`, `.wf-cache`, tests, `dist`, or local config.
- Run at least one cached read-heavy action before publishing and verify `.wf-cache/infographic-composer/scan_cache.json` is created locally but excluded from the bundle.

```
poetry run wayfinder path fmt --path paths/infographic-composer
poetry run wayfinder path doctor --path paths/infographic-composer
poetry run -- wayfinder path exec --path-dir paths/infographic-composer --component main -- stablecoin-rates --asset USDC
poetry run wayfinder path render-skill --path paths/infographic-composer
poetry run wayfinder path build --path paths/infographic-composer --out paths/infographic-composer/dist/bundle.zip
python3 -m zipfile -l paths/infographic-composer/dist/bundle.zip
poetry run wayfinder path publish --path paths/infographic-composer --out paths/infographic-composer/dist/bundle.zip
```

## Out of scope (v0.1)

- General infographics unrelated to supported protocol data sources.
- Multi-page reports, slide decks, dashboards, or whitepapers.
- Autonomous web research for unsupported protocol facts.
- Full brand-system extraction from protocol websites. Lightweight public logos/favicons for protocol identification are in scope.
- Path applet UI; static HTML is the v0.1 primary surface.
- PNG/PDF export; SVG and static HTML are the v0.1 artifact targets.
- Prediction-market comparisons; Polymarket remains available for `how-it-works` and `market-snapshot`.
- Trading, swapping, deposits, withdrawals, claims, votes, order placement, or any fund-moving action.
- Editing arbitrary previous infographics. Regenerate via the relevant infographic action instead.
