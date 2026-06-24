# Pipeline

This path discovers emerging macro narratives and maps them to tradeable instruments.

Ordered phases:
1. `intake` — load scan config and previous inventory
2. parallel fan-out: `geopolitical_scan`, `macro_scan`, `regulatory_scan`, `tech_scan`, `structural_scan`
3. `thesis_synthesis` — merge domain outputs, deduplicate, update existing inventory
4. `novelty_gate` — kill theses that are already mainstream
5. `pre_mortem` — assume each thesis is wrong, find the most likely failure mode
6. `consensus_audit` — find the strongest counter-argument for each thesis
7. `historical_analog` — find historical parallels and base rates
8. `portfolio_strategy` — map surviving theses to instruments and trade structures
9. `compile_inventory` — persist updated inventory for next run AND write `trade_book.md` as the primary human-readable artifact (summary table + one short section per retained trade)
10. `finalize` — emit the standard response envelope including `trade_book` pointer

Failure policy:
- retry any domain scan once on retryable errors
- if novelty gate kills all theses, skip to finalize with null state
- if adversarial chain rejects all theses, compile inventory with no portfolio actions
- if no instruments found, compile inventory without trade structures

Artifact rule:
- every worker owns exactly one JSON artifact under `.wf-artifacts/$RUN_ID/`
- the orchestrator reads artifacts and owns final synthesis

State persistence:
- `inputs/inventory.json` carries the thesis inventory across runs
- each run updates confidence scores based on new evidence
- `compile_inventory` writes the updated inventory back
