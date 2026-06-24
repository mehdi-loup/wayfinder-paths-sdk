"""Register a trailing-order config and ensure the background monitor runs.

The skill invokes this via `mcp__wayfinder__run_script` immediately after the
user's entry order fires. It appends the config to library storage, registers
a runner job if needed, and prints a one-line confirmation back to Claude.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from controller import TrailingConfig  # noqa: E402
from state import add_config  # noqa: E402

from wayfinder_paths.runner.client import RunnerControlClient  # noqa: E402
from wayfinder_paths.runner.constants import JOB_TYPE_SCRIPT  # noqa: E402
from wayfinder_paths.runner.lifecycle import ensure_daemon_started  # noqa: E402
from wayfinder_paths.runner.paths import get_runner_paths  # noqa: E402

RUNNER_JOB_NAME = "trailing-hl-monitor"
DEFAULT_INTERVAL = 300  # seconds


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attach a trailing order to a Hyperliquid position"
    )
    parser.add_argument("--wallet", default="main")
    parser.add_argument("--coin", required=True)
    parser.add_argument("--side", required=True, choices=("long", "short"))
    parser.add_argument(
        "--kind",
        required=True,
        choices=("trailing_sl", "trailing_tp", "trailing_entry"),
    )
    parser.add_argument("--offset-pct", required=True, type=float)
    parser.add_argument("--mode", choices=("resting", "monitor"), default="resting")
    parser.add_argument("--activation-pct", type=float, default=None)
    parser.add_argument("--oco-peer", default=None)
    parser.add_argument(
        "--position-id",
        required=True,
        help="Unique tag for this position (entry cloid or user-supplied id).",
    )
    parser.add_argument(
        "--entry-size",
        type=float,
        default=None,
        help="Coin units to buy/sell on FIRE_ENTRY (trailing_entry only).",
    )
    parser.add_argument("--cadence", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument(
        "--skip-runner",
        action="store_true",
        help="Only write the config; don't register or start the runner.",
    )
    return parser.parse_args()


# ---------- runner control (pure Python; no subprocess) --------------------
#
# All runner interaction goes through RunnerControlClient, which speaks JSON
# over a local Unix socket. No shell, no argv construction, no user- or
# config-sourced strings ever reach a process command line. ensure_daemon_started
# handles the one-time daemon fork internally (core SDK code, not this path).


def _status(client: RunnerControlClient) -> dict[str, Any]:
    resp = client.call("status")
    if not resp.get("ok"):
        return {}
    result = resp.get("result")
    return result if isinstance(result, dict) else {}


def _find_job(status: dict[str, Any], name: str) -> dict[str, Any] | None:
    jobs = status.get("jobs") or []
    for job in jobs:
        if isinstance(job, dict) and str(job.get("name")) == name:
            return job
    return None


def _job_status(job: dict[str, Any]) -> str:
    state = job.get("state")
    if isinstance(state, dict):
        return str(state.get("status") or "")
    return str(job.get("status") or "")


def _ensure_runner_job(script_path: Path, interval: int) -> str:
    # Idempotent: start the daemon, ensure the job is registered and ACTIVE.
    paths = get_runner_paths()
    started_ok, start_info = ensure_daemon_started(
        paths=paths,
        tick_seconds=1.0,
        max_workers=4,
        max_failures=5,
        default_timeout_seconds=20 * 60,
        log_level="INFO",
    )
    if not started_ok:
        return f"runner start failed: {start_info}"

    client = RunnerControlClient(sock_path=paths.sock_path)
    status = _status(client)
    if not status:
        return "runner status failed: no response"

    existing = _find_job(status, RUNNER_JOB_NAME)
    if existing is None:
        resp = client.call(
            "add_job",
            {
                "name": RUNNER_JOB_NAME,
                "type": JOB_TYPE_SCRIPT,
                "payload": {"script_path": str(script_path)},
                "interval_seconds": int(interval),
            },
        )
        if not resp.get("ok"):
            return f"add_job failed: {resp.get('error')!r}"
        return "runner job registered"

    # Job already exists. If the previous monitor paused itself when configs
    # emptied, resume it so the new config we just wrote actually ticks.
    if _job_status(existing).upper() == "PAUSED":
        resp = client.call("resume_job", {"name": RUNNER_JOB_NAME})
        if not resp.get("ok"):
            return f"resume_job failed: {resp.get('error')!r}"
        return "runner job resumed"
    return "runner job already registered"


def _stage_runner_wrapper(skill_path_dir: Path) -> Path:
    # The local runner daemon only accepts script paths inside `.wayfinder_runs/`,
    # but the skill's `monitor.py` lives in the installed skill tree. Stage a thin
    # wrapper in the library dir that sys.path-inserts the skill and delegates.
    library_root = Path(
        os.environ.get("WAYFINDER_LIBRARY_DIR")
        or Path.cwd() / ".wayfinder_runs" / "library"
    )
    wrapper_dir = library_root / "hyperliquid" / "trailing_orders"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_dir / "monitor.py"
    wrapper.write_text(
        '"""Auto-generated by trailing-hl-orders attach.py."""\n'
        "from __future__ import annotations\n\n"
        "import asyncio\n"
        "import sys\n\n"
        f"sys.path.insert(0, {str(skill_path_dir)!r})\n\n"
        "from monitor import main as _skill_main  # noqa: E402\n\n\n"
        'if __name__ == "__main__":\n'
        "    asyncio.run(_skill_main())\n",
        encoding="utf-8",
    )
    return wrapper


def main() -> int:
    args = _parse_args()
    cfg = TrailingConfig(
        coin=args.coin,
        side=args.side,
        kind=args.kind,
        offset_pct=args.offset_pct,
        mode=args.mode,
        activation_pct=args.activation_pct,
        oco_peer=args.oco_peer,
    )
    key = add_config(args.wallet, args.coin, args.position_id, cfg)
    # Entry-size metadata lives alongside the config (only meaningful for trailing_entry).
    if args.entry_size is not None:
        from state import load_configs, save_configs

        all_cfgs = load_configs()
        entry = all_cfgs.get(key, {})
        entry["entry_size"] = args.entry_size
        all_cfgs[key] = entry
        save_configs(all_cfgs)

    skill_path_dir = Path(__file__).resolve().parent
    monitor_path = _stage_runner_wrapper(skill_path_dir)
    runner_note = (
        "skipped (--skip-runner)"
        if args.skip_runner
        else _ensure_runner_job(monitor_path, args.cadence)
    )

    print(
        json.dumps(
            {
                "status": "attached",
                "key": key,
                "config": {
                    "coin": args.coin,
                    "side": args.side,
                    "kind": args.kind,
                    "offset_pct": args.offset_pct,
                    "mode": args.mode,
                    "activation_pct": args.activation_pct,
                    "oco_peer": args.oco_peer,
                    "cadence_s": args.cadence,
                },
                "runner": runner_note,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
