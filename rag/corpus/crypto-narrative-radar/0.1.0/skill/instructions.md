# Crypto Narrative Radar

Use this skill when the user wants to discover emerging crypto-native narratives that could become dominant market drivers but are not yet mainstream. This pipeline identifies structural risks and theses across crypto protocols, DeFi, crypto regulation, and market microstructure, then validates them adversarially and maps surviving theses to tradeable instruments on SDK-supported surfaces.

**All agents must read `references/common-rules.md` first.** It defines the kind of thesis this pipeline is hunting (asymmetrically skewed upside) and the data discipline every worker is bound by.

Read `references/pipeline.md`, `references/signals.md`, `references/risk.md`, **and `references/data-sources.md`** before starting. The data-sources reference is the authoritative guide for which adapter/client to call at each stage — every agent prompt points to it.

Crypto-specific edge profile (enforced by `policy/default.yaml`):
- Catalyst window: 60-540 days out. Closer = likely priced; further = stale by next scan.
- Scale floor: ≥$100M (TVL, market cap, or affected OI).
- Price-action novelty: reject if token is up/down >20% in last 30 days.
- Excluded themes: token unlocks, ETF flows, generic distribution overhang, funding-regime shifts without dated catalyst.
- Positioning gap required: specialist coverage EXISTS but retail/derivatives positioning is ABSENT.
- Executability: every thesis must map to one of the 10 SDK surfaces (swap/perp/lending/vault/lp/pendle/contract/polymarket/ccxt).

Data-source discipline (enforced by agent prompts):
- Adapters/clients (Alpha Lab, Delta Lab, Pool/Token, Hyperliquid, Polymarket, protocol adapters, CCXT) are the PRIMARY source for any quantitative claim (price, rate, OI, funding, TVL, market volume, implied probability).
- WebSearch is for qualitative context (dates, filings, governance posts) — never the first choice for numbers.
- Every evidence entry records `tool` alongside `source_url`.
- See `references/data-sources.md` for the canonical mapping of data source → pipeline stage.

Execution order:
1. Load `inputs/scan_config.yaml` and `inputs/inventory.json` (previous state if recurring run).
2. Spawn all five crypto-tuned scan agents in parallel (phase names are inherited from the narrative-radar archetype; bodies are crypto-retuned):
   - `geopolitical-analyst` → crypto-adjacent events (mining sanctions, CBDC milestones, seized-crypto policy)
   - `macro-strategist` → DeFi-macro (collateral regimes, PT term structure, lending curves)
   - `regulatory-tracker` → crypto-specific rule-makings (SEC/CFTC/BCBS/MiCA/HK/SG/IRS/FASB)
   - `tech-scout` → crypto protocol architecture + tokenomics governance binaries
   - `structural-analyst` → crypto market microstructure (ETF flow, MEV, validator concentration)
3. Run `thesis-synthesizer` to merge domain outputs with existing inventory.
4. Run the adversarial chain in sequence:
   `novelty-gate` → `pre-mortem-analyst` → `consensus-auditor` → `historical-analogist`.
5. Run `portfolio-strategist` only on theses that survived all adversarial gates.
6. Run `inventory-compiler` to persist updated inventory AND emit `trade_book.md`.
7. **Primary user-facing output is `trade_book.md`** — a summary table plus short per-trade sections. Always surface it to the user after the run completes.

Rules:
1. You are the only orchestrator.
2. Workers are leaf agents and must not spawn more agents.
3. Every worker writes exactly one artifact under `.wf-artifacts/$RUN_ID/`.
4. The adversarial chain is mandatory — never skip the novelty gate or skeptic phases.
5. Every thesis must have a specific, falsifiable prediction with a deadline.
6. If this is a recurring run, existing theses get confidence updates, not re-generation.
7. Theses that are already mainstream (high media coverage, large Polymarket volume, token >20% 30d move) must be killed (unless relaxed filter mode rescues a specific uncrowded leg).
8. The final output must contain:
   - `signal_snapshot`
   - `selected_playbook`
   - `candidate_expressions`
   - `null_state`
   - `risk_checks`
   - `job`
   - `next_invalidation`
   - `trade_book` — path to the markdown trade book (primary human-readable artifact)
