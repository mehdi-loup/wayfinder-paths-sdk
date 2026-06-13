"""Concentrated LP Manager - background monitor.

Polls all configured positions on `monitor.poll_interval_seconds` and surfaces
band-exit candidates as JSON snapshots. **Never executes a rebalance** — alert-only.

Designed to be invoked by the wayfinder runner (one-shot per tick) or directly.
When invoked directly, runs forever until `monitor.max_runtime_hours` elapses.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PATH_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PATH_DIR / "scripts"
CONFIG_PATH = PATH_DIR / "inputs" / "config.yaml"
POOLS_PATH = PATH_DIR / "inputs" / "pools.yaml"

# Reuse controller helpers without circular imports.
sys.path.insert(0, str(SCRIPTS_DIR))
from main import (  # noqa: E402
    _erc20_symbol,
    _human_price,
    cooldown_check,
    load_yaml,
    make_handle,
    merged_strategy,
)

from wayfinder_paths.core.config import load_config  # noqa: E402


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


async def _scan_once(config: dict[str, Any], pools: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []

    for entry in pools.get("positions") or []:
        try:
            handle = await make_handle(entry, str(config.get("wallet", "main")))
            state = await handle.pool_state()
            positions = await handle.list_positions()
        except Exception as exc:  # noqa: BLE001
            snapshots.append({"pool": entry.get("pool"), "error": str(exc)})
            continue

        strategy = merged_strategy(config, entry)
        cooldown_min = int(strategy.get("rebalance_cooldown_minutes") or 60)
        daily_cap = int(strategy.get("max_rebalances_per_day") or 4)
        sym0 = await _erc20_symbol(state["token0"], handle.chain_id)
        sym1 = await _erc20_symbol(state["token1"], handle.chain_id)
        price = _human_price(state)

        if not positions:
            snapshots.append(
                {
                    "pool": handle.pool_address,
                    "venue": handle.venue,
                    "pair": [sym0, sym1],
                    "current_tick": int(state["tick"]),
                    "current_price_token1_per_token0": price,
                    "positions": 0,
                }
            )
            continue

        for pos in positions:
            tick_lower = int(pos.get("tick_lower") or pos.get("tickLower") or 0)
            tick_upper = int(pos.get("tick_upper") or pos.get("tickUpper") or 0)
            current = int(state["tick"])
            in_range = tick_lower <= current < tick_upper
            band_exit_pct = None
            if not in_range:
                if current < tick_lower:
                    band_exit_pct = (tick_lower - current) * 0.0001
                else:
                    band_exit_pct = (current - tick_upper) * 0.0001

            snap = {
                "pool": handle.pool_address,
                "venue": handle.venue,
                "pair": [sym0, sym1],
                "current_tick": current,
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "in_range": in_range,
                "band_exit_pct_estimate": band_exit_pct,
            }
            snapshots.append(snap)

            if not in_range:
                ok, reason = cooldown_check(handle.pool_address, cooldown_min, daily_cap)
                candidates.append(
                    {
                        **snap,
                        "rebalance_eligible": ok,
                        "rebalance_blocked_by": None if ok else reason,
                        "suggested_action": (
                            "rebalance" if ok else "wait (cooldown/cap active)"
                        ),
                    }
                )

    return {
        "as_of": datetime.now(UTC).isoformat(),
        "snapshots": snapshots,
        "candidates": candidates,
    }


async def _try_mcp_notify(message: str) -> None:
    """Best-effort MCP notify; silent if not available (e.g. running outside MCP)."""
    try:
        from wayfinder_paths.mcp.tools.notify import notify  # type: ignore

        result = await notify(
            title="Concentrated LP band exit",
            message=message,
        )
        if not isinstance(result, dict) or not result.get("ok", False):
            print(json.dumps({"mcp_notify_fallback": message, "notify_result": result}))
    except Exception:
        # Fallback: write to stdout under a sentinel so runner logs surface it.
        print(json.dumps({"mcp_notify_fallback": message}))


async def _run_loop(config: dict[str, Any], pools: dict[str, Any]) -> int:
    monitor_cfg = config.get("monitor") or {}
    interval = int(monitor_cfg.get("poll_interval_seconds") or 300)
    max_hours = float(monitor_cfg.get("max_runtime_hours") or 168)
    deadline = time.time() + max_hours * 3600

    while time.time() < deadline:
        snapshot = await _scan_once(config, pools)
        emit(snapshot)
        for c in snapshot["candidates"]:
            await _try_mcp_notify(
                f"[concentrated-lp-manager] band-exit on {c['venue']} "
                f"{'/'.join(c['pair'])}: tick {c['current_tick']} outside "
                f"[{c['tick_lower']}, {c['tick_upper']}]; suggested={c['suggested_action']}"
            )
        await asyncio.sleep(max(10, interval))
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Concentrated LP Manager monitor")
    parser.add_argument("--once", action="store_true", help="Single scan + exit")
    parser.add_argument("--config-path", default=str(CONFIG_PATH))
    parser.add_argument("--pools-path", default=str(POOLS_PATH))
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    config = load_yaml(Path(args.config_path))
    pools = load_yaml(Path(args.pools_path))

    if args.once or os.environ.get("WAYFINDER_RUNNER_SCRIPT_ONESHOT"):
        snapshot = await _scan_once(config, pools)
        emit(snapshot)
        for c in snapshot["candidates"]:
            await _try_mcp_notify(
                f"[concentrated-lp-manager] band-exit on {c['venue']} "
                f"{'/'.join(c['pair'])}: tick {c['current_tick']} outside "
                f"[{c['tick_lower']}, {c['tick_upper']}]; suggested={c['suggested_action']}"
            )
        return 0
    return await _run_loop(config, pools)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    load_config("config.json")
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
