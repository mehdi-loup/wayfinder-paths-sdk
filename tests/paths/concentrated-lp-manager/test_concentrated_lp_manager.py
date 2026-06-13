from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
PATH_DIR = ROOT / "paths" / "concentrated-lp-manager"


def _load_script(name: str):
    script_path = PATH_DIR / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"clp_{name}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def clp_main():
    return _load_script("main")


@pytest.fixture()
def clp_monitor():
    return _load_script("monitor")


class FakeHandle:
    def __init__(self, module: Any) -> None:
        self.venue = module.VENUE_UNI
        self.chain_id = 8453
        self.pool_address = "0x00000000000000000000000000000000000000aa"
        self.wallet_address = "0x00000000000000000000000000000000000000bb"
        self._state_cache = None
        self.mints: list[dict[str, int]] = []
        self.increases: list[dict[str, int]] = []
        self.removed: list[int] = []
        self.collected: list[int] = []
        self.state = {
            "pool": self.pool_address,
            "token0": "0x0000000000000000000000000000000000000001",
            "token1": "0x0000000000000000000000000000000000000002",
            "decimals0": 18,
            "decimals1": 18,
            "sqrt_price_x96": module.sqrt_price_x96_from_tick(0),
            "tick": 0,
            "tick_spacing": 1,
            "liquidity": 10**24,
            "fee_pips": 500,
            "fee_pct": 0.0005,
        }
        self.positions = [
            {
                "token_id": 123,
                "tick_lower": -100,
                "tick_upper": 100,
                "liquidity": 10**18,
            }
        ]

    async def pool_state(self) -> dict[str, Any]:
        return dict(self.state)

    async def list_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)

    async def mint(self, **kwargs: int) -> dict[str, Any]:
        self.mints.append(kwargs)
        return {"tx_hash": "0xmint", "token_id": 456}

    async def increase(self, **kwargs: int) -> dict[str, Any]:
        self.increases.append(kwargs)
        return {"tx_hash": "0xincrease"}

    async def collect(self, token_id: int) -> dict[str, Any]:
        self.collected.append(token_id)
        return {"tx_hash": "0xcollect"}

    async def remove_all(
        self, token_id: int, *, burn: bool, slippage_bps: int
    ) -> dict[str, Any]:
        self.removed.append(token_id)
        return {"tx_hashes": ["0xdecrease"]}

    async def get_uncollected_fees(self, token_id: int) -> dict[str, int]:
        return {"amount0": 20 * 10**18, "amount1": 20 * 10**18}


def _pool_config(module: Any, handle: FakeHandle) -> dict[str, Any]:
    return {
        "positions": [
            {
                "pool": handle.pool_address,
                "venue": module.VENUE_UNI,
                "chain": 8453,
                "pair": ["TOKEN0", "TOKEN1"],
                "target_usd": 100,
                "strategy": {"range_width_pct": 1},
            }
        ]
    }


@pytest.mark.asyncio
async def test_open_caps_mint_to_requested_size_not_wallet_balance(
    clp_main, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = FakeHandle(clp_main)
    wallet_balance = 1_000 * 10**18

    async def fake_make_handle(*_: Any, **__: Any) -> FakeHandle:
        return handle

    async def fake_balance(*_: Any, **__: Any) -> int:
        return wallet_balance

    async def no_gas_error(*_: Any, **__: Any) -> None:
        return None

    async def no_ledger(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(clp_main, "make_handle", fake_make_handle)
    monkeypatch.setattr(clp_main, "_erc20_balance", fake_balance)
    monkeypatch.setattr(clp_main, "_gas_reserve_error", no_gas_error)
    monkeypatch.setattr(clp_main, "compute_range_ticks", lambda *_: (-100, 100))
    monkeypatch.setattr(clp_main, "_maybe_ledger", no_ledger)

    result = await clp_main.action_open(
        SimpleNamespace(pool=handle.pool_address, size=100.0),
        {"wallet": "main", "slippage_bps": 30, "gas_reserve_native_eth": 0},
        _pool_config(clp_main, handle),
    )

    assert result["ok"] is True
    assert handle.mints
    minted = handle.mints[0]
    assert minted["amount0"] < wallet_balance
    assert minted["amount1"] < wallet_balance
    assert result["amount0"] + result["amount1"] < 110 * 10**18


@pytest.mark.asyncio
async def test_compound_uses_only_collected_fee_balance_delta(
    clp_main, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = FakeHandle(clp_main)
    before = 1_000 * 10**18
    fee_delta = 20 * 10**18
    calls_by_token = {
        handle.state["token0"].lower(): [before, before + fee_delta],
        handle.state["token1"].lower(): [before, before + fee_delta],
    }

    async def fake_make_handle(*_: Any, **__: Any) -> FakeHandle:
        return handle

    async def fake_balance(token: str, *_: Any, **__: Any) -> int:
        return calls_by_token[token.lower()].pop(0)

    async def no_gas_error(*_: Any, **__: Any) -> None:
        return None

    async def no_ledger(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(clp_main, "make_handle", fake_make_handle)
    monkeypatch.setattr(clp_main, "_erc20_balance", fake_balance)
    monkeypatch.setattr(clp_main, "_gas_reserve_error", no_gas_error)
    monkeypatch.setattr(clp_main, "_maybe_ledger", no_ledger)

    result = await clp_main.action_compound(
        SimpleNamespace(pool=handle.pool_address),
        {"wallet": "main", "slippage_bps": 30, "gas_reserve_native_eth": 0},
        _pool_config(clp_main, handle),
    )

    assert "increase_tx" in result["results"][0]
    assert handle.increases[0]["amount0"] == fee_delta
    assert handle.increases[0]["amount1"] == fee_delta


@pytest.mark.asyncio
async def test_rebalance_remints_only_removed_position_proceeds(
    clp_main, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = FakeHandle(clp_main)
    before = 1_000 * 10**18
    proceeds = 50 * 10**18
    calls_by_token = {
        handle.state["token0"].lower(): [before, before + proceeds],
        handle.state["token1"].lower(): [before, before + proceeds],
    }

    async def fake_make_handle(*_: Any, **__: Any) -> FakeHandle:
        return handle

    async def fake_balance(token: str, *_: Any, **__: Any) -> int:
        return calls_by_token[token.lower()].pop(0)

    async def no_gas_error(*_: Any, **__: Any) -> None:
        return None

    async def no_ledger(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(clp_main, "make_handle", fake_make_handle)
    monkeypatch.setattr(clp_main, "_erc20_balance", fake_balance)
    monkeypatch.setattr(clp_main, "_gas_reserve_error", no_gas_error)
    monkeypatch.setattr(clp_main, "compute_range_ticks", lambda *_: (-100, 100))
    monkeypatch.setattr(clp_main, "cooldown_check", lambda *_: (True, None))
    monkeypatch.setattr(clp_main, "record_rebalance", lambda *_: None)
    monkeypatch.setattr(clp_main, "_maybe_ledger", no_ledger)

    result = await clp_main.action_rebalance(
        SimpleNamespace(pool=handle.pool_address),
        {"wallet": "main", "slippage_bps": 30, "gas_reserve_native_eth": 0},
        _pool_config(clp_main, handle),
    )

    assert result["ok"] is True
    assert handle.removed == [123]
    assert handle.mints[0]["amount0"] <= proceeds
    assert handle.mints[0]["amount1"] <= proceeds


@pytest.mark.asyncio
async def test_monitor_notify_awaits_notify_with_title_and_message(
    clp_monitor, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, str]] = []

    async def fake_notify(*, title: str, message: str) -> dict[str, Any]:
        calls.append({"title": title, "message": message})
        return {"ok": True}

    import wayfinder_paths.mcp.tools.notify as notify_module

    monkeypatch.setattr(notify_module, "notify", fake_notify)

    await clp_monitor._try_mcp_notify("tick outside range")

    assert calls == [
        {
            "title": "Concentrated LP band exit",
            "message": "tick outside range",
        }
    ]
    assert capsys.readouterr().out == ""
