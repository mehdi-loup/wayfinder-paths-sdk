"""Concentrated LP Manager - attach helper.

Convenience wrapper around `wayfinder runner add-job` to install monitor.py
as a recurring runner job. Equivalent to running `main.py --action attach`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from main import main as controller_main  # noqa: E402


def main() -> None:
    controller_main(["--action", "attach"])


if __name__ == "__main__":
    main()
