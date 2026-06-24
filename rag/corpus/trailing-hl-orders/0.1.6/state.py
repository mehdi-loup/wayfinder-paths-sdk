"""Atomic JSON I/O for trailing configs + runtime state.

Storage lives under `$WAYFINDER_LIBRARY_DIR/hyperliquid/trailing_orders/`
(defaults to `.wayfinder_runs/library/...`) so it survives session scratch
cleanup and runner restarts.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from controller import TrailingConfig, TrailingState

CONFIGS_FILE = "configs.json"
STATE_FILE = "state.json"


def _library_root() -> Path:
    base = os.environ.get("WAYFINDER_LIBRARY_DIR")
    if base:
        return Path(base)
    return Path(".wayfinder_runs") / "library"


def storage_dir() -> Path:
    path = _library_root() / "hyperliquid" / "trailing_orders"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _key(wallet_label: str, coin: str, position_id: str) -> str:
    return f"{wallet_label}::{coin}::{position_id}"


def load_configs() -> dict[str, dict[str, Any]]:
    return _read_json(storage_dir() / CONFIGS_FILE)


def save_configs(configs: dict[str, dict[str, Any]]) -> None:
    _atomic_write_json(storage_dir() / CONFIGS_FILE, configs)


def load_states() -> dict[str, dict[str, Any]]:
    return _read_json(storage_dir() / STATE_FILE)


def save_states(states: dict[str, dict[str, Any]]) -> None:
    _atomic_write_json(storage_dir() / STATE_FILE, states)


def add_config(
    wallet_label: str,
    coin: str,
    position_id: str,
    cfg: TrailingConfig,
) -> str:
    configs = load_configs()
    key = _key(wallet_label, coin, position_id)
    payload = asdict(cfg)
    payload["wallet_label"] = wallet_label
    payload["position_id"] = position_id
    configs[key] = payload
    save_configs(configs)
    return key


def remove_config(key: str) -> None:
    configs = load_configs()
    configs.pop(key, None)
    save_configs(configs)
    states = load_states()
    states.pop(key, None)
    save_states(states)


def get_state(key: str) -> TrailingState:
    raw = load_states().get(key)
    if not raw:
        return TrailingState()
    return TrailingState(
        **{k: v for k, v in raw.items() if k in TrailingState.__dataclass_fields__}
    )


def set_state(key: str, state: TrailingState) -> None:
    states = load_states()
    states[key] = asdict(state)
    save_states(states)


def make_key(wallet_label: str, coin: str, position_id: str) -> str:
    return _key(wallet_label, coin, position_id)
