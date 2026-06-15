# Changelog

Notable changes to the Stablecoin Yield Rotator path.

## 0.2.0

Automation, safer wallet handling, and a large cut in API request volume. No changes to
supported assets or venues — purely new actions, reliability, and efficiency over 0.1.6.

### Added

- **Scheduled auto-rotation** — new `auto-rotate` action runs the full rotation
  unattended for the Wayfinder runner (no interactive `--confirm`); the constraints in
  `inputs/config.yaml` are the only gate. Emails a summary on executed rotations and on
  new failures (no-ops are silent; repeated identical halts alert once). Schedule it as a
  runner job, e.g. daily with `--cron "0 9 * * *"`. Note: runner jobs are live
  fund-moving automation managed by the Wayfinder Shell.
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
