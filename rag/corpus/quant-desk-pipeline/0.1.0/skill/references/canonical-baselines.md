# Canonical baselines

A curated topic → seminal-paper map. Phase 1 (discovery) **must seed its pool with the matching canonical papers before searching for novelty.** Phase 4 (implementation) **must implement the canonical baseline for the topic as Step 0**, before any paper-specific signals.

The list grows as memory entries land. New runs that uncover a paper which clearly outperformed novel academic peers should add it here.

## Why this exists

Novelty-biased discovery (arxiv "latest", filtering for recent years) systematically misses the canonical work that *actually replicates*. The Moskowitz-Ooi-Pedersen (2012) / Lempérière (2014) trend run revealed this: 18 fresh papers, none of the two that worked. We now anchor every topic with the canonical baselines first, then layer novelty on top.

## Format

Each topic block lists the seminal papers in priority order. The discovery agent prepends them to the candidate pool (subject to feasibility pruning like any other paper). The implementer treats the topmost entry as the Step-0 baseline signal.

---

## Time-series momentum / trend following

- Moskowitz, Ooi, Pedersen (2012) — "Time Series Momentum" — Journal of Financial Economics 104(2). DOI: `https://doi.org/10.1016/j.jfineco.2011.11.003` (resolves to ScienceDirect)
- Lempérière, Deremble, Seager, Potters, Bouchaud (2014) — "Two Centuries of Trend Following" — `https://arxiv.org/abs/1404.3274`
- Baz, Granger, Harvey, Le Roux, Rattray (2015) — "Dissecting Investment Strategies in the Cross Section and Time Series" — `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2695101` (use arxiv mirror if SSRN blocked)

Step-0 implementation: plain EWMA-trend signal at the paper's spec (typically 30d half-life, 24h forward horizon). This has empirically been the strongest replicator in the topic family.

## Variance ratio / autocorrelation / mean reversion

- Lo, MacKinlay (1988) — "Stock Market Prices Do Not Follow Random Walks: Evidence from a Simple Specification Test" — `https://www.nber.org/papers/w2168`

Step-0 implementation: the classic variance ratio test signal at multiple lookbacks.

## Funding-rate mean reversion (crypto-native)

*No canonical baseline established yet.* Prior memory: 10 papers tested 2026-04-20, all REJECTED. Do not seed canonical anchors here until a replicating mechanism is found.

## Realized moments (skewness / kurtosis)

*No canonical baseline established yet.* Prior memory shows zhao_realized_kurtosis as the closest to passing but it failed Phase 2 direction inversion at the raw spec; not canonical-grade.

---

## Adding to this list

After a successful run, if a paper materially outperformed novel academic peers AND the win was robust across walk-forward, add it here under the matching topic. Keep entries terse: title, authors, year, URL, and one-line note on which spec to use as Step-0.
