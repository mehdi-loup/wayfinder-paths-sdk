# Changelog

Notable changes to the Stablecoin Yield Rotator path.

## 0.2.0

Automation, safer wallet handling, and a large cut in API request volume.

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

### Upgrading from 0.1.x

0.1.6 expanded coverage to **USDS, USDe, and GHO** and added **Polygon** (alongside
Ethereum, Base, Arbitrum, and HyperEVM), the **Moonwell** venue, an applet refresh, and a
zero-APY market filter. After upgrading, review `inputs/config.yaml` — especially `assets`,
`chains`, `venues`, and the new `rpc_concurrency` and `scan_cache_ttl_seconds` keys.
