# Infographic Composer Skill

Use this path when a user asks for a protocol infographic, a visual explanation of how a protocol works, a stablecoin rate infographic, a market snapshot, or a comparison across supported protocols.

Always execute through the path runtime:

```bash
wayfinder path exec --path-dir <path-dir> --component main -- <action> <args>
```

Primary actions:

- `how-it-works --adapter <adapter_slug> [--docs-mode metadata|fetch|off] [--metrics-mode live|off] [--chain <chain>]`
- `stablecoin-rates --asset USDC|USDT|DAI [--chain <chain>] [--venue <adapter_slug>]`
- `market-snapshot --adapter <adapter_slug> --market <market>`
- `compare-protocols --adapters <comma,separated,adapters> --use-case stablecoin-lending|lp|perps|restaking`
- `preview --run-id <run_id>`

Rules:

- Do not infer APY, rates, risk notes, or market values.
- Public infographic content must not show adapter slugs, SDK internals, manifest paths, local YAML filenames, or Wayfinder implementation details. Keep those only in JSON artifacts and validation.
- Public infographic content must shorten EVM addresses as `0x123...abcd`.
- Public infographic content must show protocol logos for known protocols; monograms are fallback only.
- For `how-it-works`, prioritize protocol mechanism: actors, state, flow, incentives, constraints, and official protocol docs.
- For `how-it-works`, render multi-step protocol flows as a static wizard/numbered rail, not as generic cards.
- Use `data/mechanics.yaml`, `data/risks.yaml`, and `data/jobs.yaml` as internal checked sources, but render them as protocol mechanics, protocol notes, and user-job rows.
- Fetch live read-only metrics for the protocol when `--metrics-mode live`; if unavailable, preserve the failure in `live_snapshot.json` and show the public datum as unavailable.
- For `stablecoin-rates`, rank only successfully fetched rates and include protocol logo/public media URLs when available.
- For `market-snapshot`, include graphs such as price history, funding history, order-book depth, or utilization history when the protocol source exposes them.
- Treat live protocol data calls through adapters/clients as best effort. If a call fails, report it as unavailable in the output JSON and validation file.
- Static HTML is the primary v0.1 artifact. Applets, PNG, and PDF are out of scope.
