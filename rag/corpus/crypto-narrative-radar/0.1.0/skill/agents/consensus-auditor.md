# consensus-auditor

Before scanning, read `references/common-rules.md`. The pipeline is hunting asymmetrically skewed upside — bring theses where the best-case outcome is genuinely large, and don't pre-skeptic yourself.

Find the strongest counter-argument to each thesis from crypto-native research desks AND from on-chain/cross-protocol data. R3 learning: crypto-native research silence ≠ edge; it often means "too small to matter." Quantify both sides.

Read:
- the novelty gate artifact (surviving theses) and the pre-mortem artifact
- the thesis synthesis artifact for full context
- `policy/default.yaml` — consensus audit prompt
- `../references/data-sources.md` — authoritative data-source inventory

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/consensus_audit.json`
- include for each thesis: `strongest_counter_argument`, `counter_source` {name, url, date}, `consensus_score` (0-1, 1 = strong counter from top desk), `on_chain_contradiction` (or null), `confidence_delta` ∈ [-0.20, +0.05], `relaxed_leg_rescope` (if headline is crowded but a specific leg is uncrowded)
- include a `verdict` per thesis: `pass`, `downgrade`, or `reject`

**Primary data sources:**
1. `ALPHA_LAB_CLIENT.search(scan_type="defi_llama_overview", min_score=0.5)` + `scan_type="defi_llama_protocol"` + `scan_type="defi_llama_chain_flow"` — where is capital ACTUALLY flowing? If capital is flowing opposite the thesis, that is an on-chain contradiction.
2. `ALPHA_LAB_CLIENT.search(scan_type="delta_lab_top_apy")` + `scan_type="delta_lab_best_delta_neutral"` — quant consensus screening picks. If your thesis is the REVERSE of a currently high-score quant pick, note the tension explicitly.
3. `DELTA_LAB_CLIENT.get_best_delta_neutral_pairs(basis_symbol)` — is the hedged expression of this thesis on the Pareto frontier, or is a competing pair dominating? A competing pair at higher net_apy with lower erisk_proxy is strong counter-evidence.
4. `DELTA_LAB_CLIENT.screen_lending / screen_perp` for the affected asset — is the rate-of-change of relevant rates contradicting the thesis? (e.g. thesis predicts "supply APR will spike" but APR has been flat 30d.)
5. Protocol adapter spot reads — `EthenaVaultAdapter` for reserve-embedded APY (thesis of "Ethena will break" fails if the APY-embedded reserve ratio is still rising), `MorphoAdapter` market warnings (thesis of "Morpho market X will blow" fails if warnings flag is clean).
6. WebSearch for crypto-native research desk publications: Delphi Digital, Kaiko, Chaos Labs, Steakhouse Financial, Galaxy Research, Blockworks Research, Messari. Quantify: `research_coverage_count` and `top_desk_consensus_direction`.

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Record the source in `evidence[].tool` — one of `alpha_lab`, `delta_lab`, `<protocol>_adapter`, `WebSearch`, `WebFetch`.
- **Absence of crypto-native desk coverage is NOT positive edge.** If Delphi/Kaiko/Chaos Labs/Steakhouse/Galaxy have published nothing on this topic in 60 days, the thesis may be too small-cap or too far off-platform to matter at scale. Flag this as a warning, not a positive consensus-score.
- If credible experts disagree across desks, that is MORE interesting than universal agreement.
- **Relaxed mode:** if the HEADLINE is crowded but a specific leg of the trade structure (e.g. a pair's short leg, a specific Pendle tenor, a Polymarket binary on the rule outcome rather than the token) is uncrowded — PASS the thesis and rescope the trade to the uncrowded leg only. Record the uncrowded leg in `relaxed_leg_rescope` and do not downgrade purely on headline crowdedness.
