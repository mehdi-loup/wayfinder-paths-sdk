# quant-desk

An 8-phase skill pipeline that takes a **narrow academic signal topic** (e.g. "cryptocurrency perpetual funding rate mean reversion") and returns an **honest replication verdict** — PASS, REGIME_DEPENDENT, or REJECT — for each paper the pipeline surfaces.

## What it does

1. **Discover** papers from academic sources (arxiv, SSRN, NBER, JFE, RFS, etc.)
2. **Extract** methodology from abstracts + intros (no full-PDF downloads)
3. **Skeptic-filter** fatal flaws only (underspecified, unimplementable, lookahead)
4. **Implement** each surviving paper's signal as a Python `signal_fn`
5. **Replicate** via a fixed 3-phase harness (quartile diagnostic → gated backtest → walk-forward)
6. **Iterate remedially (5b)** on near-miss signals using structured fix templates (A/B/C/D/E)
7. **Iterate experimentally (5c)** on winners using composition templates (F1 add short, F2 ensemble, F3 agreement gate, F4 direction-normalize, F5 hysteresis)
8. **Generalize (5d)** surviving winners via multi-year CCXT walk-forward + 11-symbol universe test

Output: a structured report distinguishing real alpha from regime-specific flukes.

## Why it matters

Published crypto signal research has well-documented reproducibility issues. This skill provides:

- A **pre-committed protocol** with locked thresholds, so iteration budget can't drift into p-hacking
- A **skeptic agent** in isolated context that enforces methodology filtering separate from implementation enthusiasm
- A **mandatory multi-year Phase 5d** that has historically reversed short-window "winners" — the M3 Hurst case showed Sharpe 3.54 on 7 months but 0.46 median on 2.5 years, revealing it as a bear-regime alpha, not universal

The skill's value is not finding alpha. It is producing an honest rejection report: "we tried 10 papers on topic X, here is exactly why each one did or didn't replicate in our data, and which ones remain provisional."

## Structure

```
quant-desk/
├── wfpath.yaml                     # Path manifest (slug, version, skill block)
├── README.md                       # This file
└── skill/
    ├── instructions.md             # The main workflow (8-phase pipeline)
    ├── references/                 # Ten deeply-specified rules files:
    │   ├── topic-scope.md          # - narrow-enough topic gate
    │   ├── paper-sources.md        # - allowed academic sources + URL whitelist hints
    │   ├── skeptic-brief.md        # - EXACT agent brief for Phase 3
    │   ├── red-flag-taxonomy.md    # - 14 codified methodology red flags
    │   ├── implementability-rules.md
    │   ├── signal-contract.md      # - Python signal_fn signature
    │   ├── iteration-catalog.md    # - Phase 5b remedial templates A-E
    │   ├── experimental-iteration.md  # - Phase 5c composition templates F1-F5
    │   ├── universe-extension.md   # - Phase 5d D1/D2/D3 protocol
    │   └── reporting-format.md     # - final report + memory format
    └── scripts/
        ├── evaluate_signal.py      # - 3-phase harness, locked thresholds
        └── fetch_ccxt_history.py   # - Binance multi-year OHLCV fetcher (CCXT)
```

## Quickstart

Once installed as a Claude Code skill, invoke via `/quant-desk` or naturally ("replicate realized-skewness papers on crypto"):

```
User: "Test short-term reversal research on BTC/ETH"
→ Skill scaffolds 10-paper discovery, skeptic prune, implementation, backtest, iteration
→ Returns structured report in 15-25 minutes
```

## Prerequisites

- `wayfinder-paths` SDK installed (`poetry install`)
- `config.json` with `system.api_key` set (for Delta Lab Phase 5 data)
- No CCXT credentials needed — `fetch_ohlcv` is a public endpoint
- No API keys needed for academic paper fetching (abstracts are public)

## What the skill does NOT do

- Design new signals (it only replicates published ones)
- Trade blog-post "research" or practitioner writeups
- Deploy strategies to production
- Generate fresh alpha (the value is honest rejection, not signal discovery)

## Data constraints baked in

- **Hyperliquid candle API caps at ~5000 bars** (~200 days hourly) — too short for honest walk-forward. Phase 5d uses **CCXT Binance** which supports 2017+ history for majors.
- **Delta Lab retention is 211 days** — fine for Phase 5 initial test, insufficient for Phase 5d generalization.
- The primary historical test universe is spot Binance pairs (`BTC/USDT`, `ETH/USDT`, etc.), chosen for the longest common history.

## Install

```bash
poetry run wayfinder path install examples/paths/quant-desk
```

Or as a Claude Code skill, copy the skill directory into your `.claude/skills/` location.

## Build + publish (maintainers)

```bash
poetry run wayfinder path doctor --path examples/paths/quant-desk
poetry run wayfinder path fmt --path examples/paths/quant-desk
poetry run wayfinder path build --path examples/paths/quant-desk
poetry run wayfinder path publish --path examples/paths/quant-desk
```

## Historical runs

Each topic run leaves a memory entry at `memory/topic_<slug>.md` summarizing:
- Papers tested + skeptic verdicts
- Phase 5 replication results
- Phase 5b/5c iteration outcomes
- Phase 5d generalization verdict (critical)
- Do-not-retest list for future runs on adjacent topics

Four topic runs were conducted during skill development:
1. Crypto funding mean reversion — all 10 REJECTED
2. Time-series momentum with vol scaling — all failed standalone; I4 composition found (regime-dependent)
3. Realized skewness/kurtosis — C3-filter variant found
4. Variance ratio + autocorrelation — Hurst exponent produced the only Phase-5-PASS standalone signal, but Phase 5d revealed it as regime-dependent

These runs are documented in the harness + rules and inform the iteration catalog's templates.
