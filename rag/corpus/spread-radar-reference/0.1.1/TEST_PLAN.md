# Spread Radar Reference — Test Plan

This path ships three pipeline fixtures and four evals that validate the spread-radar output contract (`signal_snapshot`, `selected_playbook`, `candidate_expressions`, `null_state`, `risk_checks`, `job`, `next_invalidation`) and the generated host skill exports.

## How to run

From the path root (`examples/paths/spread-radar-reference/`):

```bash
# Lint/validate manifest + policy + graph + output contract
poetry run wayfinder path doctor --path .

# Run all fixture and host_render evals
poetry run wayfinder path eval --path .
```

The `eval` command loads every file under `tests/evals/*.{yaml,yml,json}`, resolves each declared `fixture:` from `tests/fixtures/`, checks the fixture output contains every field required by the archetype's output contract, then evaluates each `assert:` entry against the fixture output (dotted-path lookup, exact match).

`host_render` evals invoke the skill renderer for the declared hosts and verify that each `expected_files` path is written under the rendered build output.

## Fixtures (`tests/fixtures/`)

Each fixture is a static YAML file with a top-level `output:` block that mirrors one plausible `spread-radar` pipeline run. Fixtures are never executed against the live pipeline — they are frozen reference outputs used by evals to pin expected shape and semantics.

| Fixture | Scenario | What it pins |
|---|---|---|
| `base_case.yaml` | SOL/SUI spread clears the scoring threshold; armed job returned | Full armed-path output contract (playbook id, scores, z-score, monitor job, invalidation string) |
| `beta_rejection.yaml` | Top candidate is hidden directional beta (β = 0.82); skeptic rejects | Null-state response when `risk_checks.beta_to_market` exceeds the hidden-beta gate |
| `null_state.yaml` | Universe produces no cointegrated pair above the confidence floor | Null-state response when no candidate is surfaced (scoreboard empty, playbook id = `null-state`) |

## Evals (`tests/evals/`)

| Eval | Type | Fixture | Verifies |
|---|---|---|---|
| `output_shape.yaml` | fixture | `base_case` | Armed path writes `null_state.selected=false`, `job.mode=armed`, `risk_checks.mode=armed`, correct playbook id |
| `beta_rejection.yaml` | fixture | `beta_rejection` | Hidden-beta detection collapses the output to null-state and preserves the measured β for downstream invalidation |
| `null_state.yaml` | fixture | `null_state` | Empty scoreboard resolves to null-state playbook with `job.mode=null` |
| `host_render.yaml` | host_render | — | Rendering the skill for `claude` and `opencode` produces the expected `SKILL.md` install paths |

## Adding new fixtures or evals

1. Write the frozen output to `tests/fixtures/<name>.yaml` under a top-level `output:` block. Include every field the archetype's output contract requires (doctor enforces the same list).
2. Add an eval under `tests/evals/<name>.yaml` with `type: fixture`, `fixture: <name>`, and an `assert:` map of dotted paths → expected values.
3. Re-run `poetry run wayfinder path eval --path .` and confirm every case reports `"passed": true`.
