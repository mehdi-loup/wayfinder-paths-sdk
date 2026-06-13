# Infographic Composer

Infographic Composer is a read-only Wayfinder Path that creates static protocol infographics from actual protocol and chain data. Adapters, checked-in YAML, and SDK metadata are internal routing and validation inputs; the public infographic should show protocol names, current metrics, official docs, protocol sources, charts, and public media such as protocol logos.

The primary output is `infographic.html`, with `infographic.svg`, `alt_text.md`, `data_snapshot.json`, and `validation.json` written beside it under `.wf-artifacts/<run_id>/`.

By default artifacts are local to the Path runtime. Add `--publish` to upload the rendered files to Cloudflare Pages and return a user-viewable HTTPS URL.

## Actions

```bash
poetry run -- wayfinder path exec --path-dir paths/infographic-composer --component main -- how-it-works --adapter aave_v3_adapter --docs-mode fetch --metrics-mode live --chain base
poetry run -- wayfinder path exec --path-dir paths/infographic-composer --component main -- stablecoin-rates --asset USDC --chain base
poetry run -- wayfinder path exec --path-dir paths/infographic-composer --component main -- market-snapshot --adapter hyperliquid_adapter --market BTC
poetry run -- wayfinder path exec --path-dir paths/infographic-composer --component main -- compare-protocols --adapters aave_v3_adapter,morpho_adapter,moonwell_adapter --use-case stablecoin-lending --asset USDC --chain base
poetry run -- wayfinder path exec --path-dir paths/infographic-composer --component main -- preview --run-id <run_id>
```

## Publishing to Cloudflare Pages

Cloudflare Pages Direct Upload is the v0.1 hosted delivery path. It is free for typical static infographic usage and turns the local `infographic.html` into a public HTTPS URL that Wayfinder Cloud users can open.

Create a Cloudflare Pages project first, then provide credentials through environment variables:

```bash
export CLOUDFLARE_ACCOUNT_ID="<account-id>"
export CLOUDFLARE_API_TOKEN="<api-token>"
export CLOUDFLARE_PAGES_PROJECT="wayfinder-infographics"
```

For local development, the Path also loads `paths/infographic-composer/.env` if those variables are not already present in the process environment:

```bash
CLOUDFLARE_ACCOUNT_ID=<account-id>
CLOUDFLARE_API_TOKEN=<api-token>
CLOUDFLARE_PAGES_PROJECT=wayfinder-infographics
```

Both `KEY=value` and `KEY: value` lines are accepted. The loader also accepts the shorter local aliases `account_id`, `api_token`, and `page_project`.

`.env` is ignored by git and excluded from Wayfinder Path bundles. Do not paste live tokens into chat, documentation, or committed files.

The API token must have permission to deploy to Cloudflare Pages for the account. Keep it in the Wayfinder Cloud runtime environment; do not commit it to this Path or include it in request artifacts.

Publish any render action with:

```bash
poetry run -- wayfinder path exec --path-dir paths/infographic-composer --component main -- stablecoin-rates --asset USDC --chain base --publish
```

Publishing stages only these public files by default:

- `infographic.html`
- `infographic.svg`
- `alt_text.md`
- `index.html`, copied from `infographic.html`

Raw snapshots such as `data_snapshot.json`, `live_snapshot.json`, `design_spec.json`, and `request.json` stay local unless `--publish-data` is explicitly set.

Useful flags:

- `--publish` uploads the rendered files to Cloudflare Pages.
- `--publish-required` fails the Path run if upload fails.
- `--publish-branch <name>` overrides the default per-run Cloudflare preview branch. By default the branch is the `run_id`.
- `--publish-data` also uploads JSON snapshots. Use only for non-sensitive artifacts.
- `--publish-provider cloudflare-pages` selects the publisher. Cloudflare Pages is the only v0.1 provider.

The success envelope includes:

```json
{
  "render_mode": "static_html",
  "primary_artifact": ".../.wf-artifacts/<run_id>/infographic.html",
  "artifact_url": "https://<run_id>.<project>.pages.dev/infographic.html",
  "publish": {
    "enabled": true,
    "published": true,
    "provider": "cloudflare_pages",
    "deployment_url": "https://<run_id>.<project>.pages.dev",
    "files": {
      "html": "https://<run_id>.<project>.pages.dev/infographic.html",
      "svg": "https://<run_id>.<project>.pages.dev/infographic.svg",
      "alt_text": "https://<run_id>.<project>.pages.dev/alt_text.md",
      "root": "https://<run_id>.<project>.pages.dev"
    }
  }
}
```

Security notes:

- `CLOUDFLARE_API_TOKEN` is read server-side by the Path and is never written into HTML, SVG, JSON snapshots, stdout, or URLs.
- Published Cloudflare Pages artifacts are public to anyone with the URL unless you add Cloudflare Access or serve through a private Worker/R2 flow.
- Do not use `--publish-data` for user-specific or sensitive raw snapshots.

## Scope

- Static HTML is the primary render target for v0.1.
- Applet rendering is a v0.2 stretch item.
- PNG and PDF exports are intentionally out of scope.
- Live reads are best effort. If a read fails, the path records the exact failure in `validation.json` and emits an unavailable data row rather than inventing values.
- Public HTML/SVG content must not expose adapter slugs, SDK details, manifest paths, local YAML filenames, or Wayfinder implementation details.
- Public HTML/SVG content shortens EVM addresses, shows protocol logos for known protocols, and uses a static numbered wizard rail for multi-step protocol mechanisms.

## Data Contracts

The path persists the APY normalization version in every rate row as `apy-normalization-v1`.

Risk notes are loaded from `data/risks.yaml` as an internal source of checked protocol risk copy. The rendered infographic labels them as protocol notes, not YAML-backed implementation details.

Mechanism copy is loaded from `data/mechanics.yaml` as a fallback and should be supplemented by official documentation metadata whenever `--docs-mode fetch` is available. `how-it-works` should focus on protocol mechanism: actors, state transitions, incentives, flows, and protocol constraints.

Protocol comparison rows come from `data/jobs.yaml` internally. The public matrix should compare protocol behavior and user jobs, not adapter capability metadata.

`stablecoin-rates` ranks only successfully fetched rates, uses protocol logos/public media where available, and keeps unavailable venues outside the ranking. The default minimum TVL filter is `$100,000` to avoid promoting dust markets, and callers can override it with `--min-tvl`. `market-snapshot` should include graph data, such as price history and order-book depth, whenever the protocol or market source can provide it.
