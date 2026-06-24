from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    manifest = yaml.safe_load((ROOT / "wfpath.yaml").read_text(encoding="utf-8")) or {}
    policy = (
        yaml.safe_load((ROOT / "policy" / "default.yaml").read_text(encoding="utf-8"))
        or {}
    )
    pipeline = manifest.get("pipeline") or {}
    summary = {
        "slug": manifest.get("slug"),
        "archetype": policy.get("archetype"),
        "entry_command": pipeline.get("entry_command"),
        "universe_seed": (policy.get("universe") or {}).get("seed_symbols", []),
        "clustering_method": (policy.get("clustering") or {}).get("method"),
        "scoring_weights": (policy.get("scoring") or {}).get("weights", {}),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
