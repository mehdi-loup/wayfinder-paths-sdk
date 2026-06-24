"""Entrypoint for the quant-desk-pipeline path.

This is a pipeline path — the workflow is orchestrated by a host agent that reads
`skill/instructions.md` and fans out to the declared worker agents, each writing
one artifact under `.wf-artifacts/$RUN_ID/`. This script satisfies the manifest's
component requirement and prints the pipeline summary for direct invocation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def summarize() -> dict:
    manifest = yaml.safe_load((ROOT / "wfpath.yaml").read_text(encoding="utf-8")) or {}
    policy = yaml.safe_load((ROOT / "policy" / "default.yaml").read_text(encoding="utf-8")) or {}
    pipeline = manifest.get("pipeline") or {}
    runtime = manifest.get("runtime") or {}
    agents = manifest.get("agents") or []
    return {
        "slug": manifest.get("slug"),
        "archetype": pipeline.get("archetype"),
        "entry_command": pipeline.get("entry_command"),
        "graph_nodes": runtime.get("graph_nodes", []),
        "agents": [a.get("id") for a in agents],
        "harness_gates": policy.get("scoring", {}),
        "output_contract": pipeline.get("output_contract", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="quant-desk-pipeline path entrypoint")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print a JSON summary of the pipeline graph, agents, and locked gates.",
    )
    args = parser.parse_args()

    summary = summarize()
    if args.describe:
        print(json.dumps(summary, indent=2))
        return 0

    print("quant-desk-pipeline — Wayfinder pipeline path")
    print(f"  archetype:     {summary['archetype']}")
    print(f"  entry_command: {summary['entry_command']}")
    print(f"  graph nodes:   {len(summary['graph_nodes'])}")
    print(f"  worker agents: {len(summary['agents'])}")
    print()
    print("Run via the host orchestrator (Claude Code / OpenCode) with a narrow")
    print("signal topic. The orchestrator reads skill/instructions.md and fans out")
    print("to the worker agents declared in wfpath.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
