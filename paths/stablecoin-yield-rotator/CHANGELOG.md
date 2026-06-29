# Changelog

Notable changes to the Stablecoin Yield Rotator path.

## 0.4.0

Correctness and reliability fixes. No new assets, venues, or actions.

### Fixed

- **Diversification cap can no longer be bypassed by multiple source positions.** When two
  positions of the same stablecoin both rank into the same top venue, the planner now
  tracks committed inflow within a single pass, so their combined size can't exceed
  `max_position_pct_per_venue`. Previously each leg was sized to the cap independently and
  could stack to twice it.

### Changed

- **Morpho vaults are ranked on base, reward-free APY** (`net_apy_excluding_rewards`)
  instead of the rewards-inclusive `net_apy`, matching the lending venues (which scan
  without rewards). Reward-token yields are volatile and often claim-gated, so the previous
  basis over-ranked incentivized vaults.

### Removed

- **Dropped the unused `ledger_record` config knob.** It was never wired to a ledger write;
  removed from `inputs/config.yaml` and the prompt spec so it no longer implies a feature
  that doesn't exist.

### Internal

- **`scripts/venues.py` is now mypy-clean.** Narrowed the adapter `(ok, payload)` result
  guards (`isinstance(..., list)` / `isinstance(..., dict)`) so the success payload type
  resolves, clearing 58 pre-existing `union-attr`/`arg-type` errors. No behavior change.

## 0.2.3

Republish for archive-policy compliance. No changes to path content, assets, venues, or
actions.

### Fixed

- **Deterministic skill-export archives.** The uploaded skill-export zips previously
  carried live filesystem timestamps, so a server-side rebuild could not match the upload
  and archive-policy verification failed. The archiver now normalizes every entry (fixed
  timestamp + permissions, junk filtered), matching the bundle/source archives, so the
  skill exports are reproducible.

## 0.2.2

Documentation clarity for review. No functional changes to assets, venues, or actions.

### Docs

- **Made the position data-flow guarantee explicit in both `README.md` and
  `skill/instructions.md`.** Both now state that position/balance objects are used **only
  locally** (ranking, rotation plan, status) or passed to **host-bound Wayfinder execution
  paths** that sign/broadcast, and are **never transmitted to third parties** (no
  analytics/telemetry/external POST). Clarified that the `auto-rotate` notify email carries
  a human-readable rotation summary only, not raw position objects.

## 0.2.1

Review-compliance fixes. No functional changes to assets, venues, or actions.

### Changed

- **Applet: removed the external GitHub origin/link.** Dropped the footer link to the
  external issues page and emptied `applet.permissions.externalOrigins` (previously listed
  one external host). The applet is now fully self-contained — `bridge: []`,
  `externalOrigins: []`, no runtime network access — and its footer states it does not
  read or transmit wallet data.

### Docs

- **Documented wallet & data flow.** README now has an explicit "Wallet & data flow"
  section: wallet address is read for balances/positions (never keys), ranking and the
  rotation plan compute locally, and the only outbound fund-moving traffic is signed
  transactions (plus the `auto-rotate` email summary). Added a per-action confirmation-gate
  table covering the read-only vs `--confirm` vs runner (`auto-rotate`) execution paths.

## 0.2.0

Automation, safer wallet handling, and a large cut in API request volume. No changes to
supported assets or venues — purely new actions, reliability, and efficiency over 0.1.6.

### Added

- **Scheduled auto-rotation** — new `auto-rotate` action runs the full rotation
  unattended for the Wayfinder runner (no interactive `--confirm`); the constraints in
  `inputs/config.yaml` are the only gate. Emails a summary on executed rotations and on
  new failures (no-ops are silent; repeated identical halts alert once). Schedule it as a
  runner job, e.g. daily with `--cron "0 9 * * *"`. Note: runner jobs are live
  fund-moving automation outside the Claude safety-review hook.
- **Idle-balance sweep** — idle balances of configured stables in the wallet are planned
  as deposit legs, so a fresh wallet bootstraps into the best venue without a manual
  `deposit`.
- **Automatic gas top-up** — when a rotation's destination chain has no native gas, a
  small slice of the rotating stable is bridged into gas first, and its cost is counted in
  the payback / max-gas gates — a rotation that can't amortize its own gas funding is
  skipped. Gas-starved *source* chains (no gas to sign with) are surfaced as skipped legs
  with a fund-gas reason instead of silently failing.

### Fixed

- **Wallet resolution** — the path now prefers the session-connected wallet over a bundled
  `wallet: main` default, so it no longer operates a local dev wallet when a different
  wallet is connected. The shipped `wallet` is blank by default.
- **Rate limiting (429s)** — quote/update now bound concurrent RPC reads
  (`rpc_concurrency`, default 4) with one shared limiter and retry transient 429s with
  backoff, instead of aborting the whole quote on the first rate-limit error.

### Performance

- **~5× fewer API requests per quote (~750 → ~150)** — Euler's stable vaults are
  discovered via a single Delta Lab call instead of enumerating every vault on-chain, and
  Morpho-vault, Moonwell, idle-balance, and native-gas reads are batched per chain via
  Multicall3.
- **Configurable scan cache** — `scan_cache_ttl_seconds` (default 6h) caches the
  wallet-agnostic APY/TVL ranking; wallet positions are always read live and target venues
  are re-checked before any fund-moving execution.

## 0.1.6

- Added stablecoins **USDS, USDe, and GHO** and the **Polygon** network (joining Ethereum,
  Base, Arbitrum, and HyperEVM); widened Morpho to Arbitrum/Polygon and Euler to HyperEVM.
- Excluded zero-APY (sub-1bp) markets from the ranked scan.
- Applet: venues / networks summary cards and 50-row pagination; refreshed scan snapshot.

## 0.1.5

- Added the **Moonwell** venue; applet refresh and rotation safety fixes.
