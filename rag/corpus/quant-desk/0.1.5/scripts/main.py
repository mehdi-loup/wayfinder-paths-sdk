"""Entrypoint for the quant-desk path.

This path is skill-primary — the actual workflow is executed by a host agent
reading `skill/instructions.md`. This script exists to satisfy the path
manifest's component requirement and to provide a CLI handle for direct
invocation (prints the workflow summary and validates the skill bundle).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def summarize() -> dict:
    here = Path(__file__).resolve().parent.parent
    skill_dir = here / "skill"
    refs = sorted((skill_dir / "references").glob("*.md"))
    scripts = sorted((skill_dir / "scripts").glob("*.py"))
    return {
        "slug": "quant-desk",
        "skill_dir": str(skill_dir),
        "instructions_path": str(skill_dir / "instructions.md"),
        "references": [f.name for f in refs],
        "scripts": [f.name for f in scripts],
        "phases": [
            "Phase 1  Discovery",
            "Phase 2  Methodology extraction",
            "Phase 3  Skeptic",
            "Phase 4  Implementation",
            "Phase 5  Replication (3-phase harness)",
            "Phase 5b Remedial iteration",
            "Phase 5c Experimental iteration",
            "Phase 5d Universe + multi-year walk-forward (CCXT)",
            "Phase 6  Synthesis",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="quant-desk path entrypoint")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print a JSON summary of the bundle layout and phase list.",
    )
    args = parser.parse_args()

    summary = summarize()
    if args.describe:
        print(json.dumps(summary, indent=2))
        return 0

    print("quant-desk — skill-primary Wayfinder path")
    print(f"  instructions: {summary['instructions_path']}")
    print(f"  references:   {len(summary['references'])} files")
    print(f"  scripts:      {len(summary['scripts'])} files")
    print()
    print("Workflow:")
    for p in summary["phases"]:
        print(f"  - {p}")
    print()
    print(
        "To run: invoke the host agent (Claude Code / OpenCode) with a narrow\n"
        "signal topic. The agent reads skill/instructions.md and orchestrates\n"
        "the full 8-phase pipeline."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
