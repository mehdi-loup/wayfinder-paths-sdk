"""Pendle partial-close + lot-isolation tests.

No network — the Pendle adapter is faked. Run with:
    poetry run pytest tests/paths/funding-rate-harvester -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

PATH_DIR = Path(__file__).resolve().parents[3] / "paths/funding-rate-harvester"
sys.path.insert(0, str(PATH_DIR / "scripts"))

from legs import PendlePtLeg  # noqa: E402

PT_A = "0x" + "a" * 40  # the pair's recorded lot
PT_B = "0x" + "b" * 40  # unrelated pre-existing PT of the same symbol root
MARKET_A = "0x" + "1" * 40
RAW = 10**18


class FakePendleAdapter:
    def __init__(self) -> None:
        self.swaps: list[dict[str, Any]] = []
        self.converts: list[dict[str, Any]] = []

    async def list_active_pt_yt_markets(self, **_kw: Any) -> list[dict[str, Any]]:
        return [
            {
                "marketName": "weETH",
                "chainId": 42161,
                "marketAddress": MARKET_A,
                "ptAddress": PT_A,
                "fixedApy": 0.05,
            }
        ]

    async def get_full_user_state_per_chain(self, *, chain: int, account: str, include_prices: bool = True):
        if chain != 42161:
            return True, {"positions": []}
        return True, {
            "positions": [
                {
                    "marketName": "rsETH",  # unrelated holding, also roots to ETH
                    "pt": PT_B,
                    "underlying": "0x" + "c" * 40,
                    "balances": {"pt": {"raw": 5 * RAW, "formatted": 5.0}},
                },
                {
                    "marketName": "weETH",
                    "pt": PT_A,
                    "underlying": "0x" + "d" * 40,
                    "balances": {"pt": {"raw": 2 * RAW, "formatted": 2.0}},
                },
            ]
        }

    async def execute_swap(self, **kwargs: Any):
        self.swaps.append(kwargs)
        return True, {"tx_hash": "0x1"}

    async def execute_convert(self, **kwargs: Any):
        self.converts.append(kwargs)
        return True, {"tx_hash": "0x2"}


def _leg(adapter: FakePendleAdapter) -> PendlePtLeg:
    return PendlePtLeg(adapter, "0xwallet", chains=[42161])


def test_partial_close_swaps_only_requested_units():
    adapter = FakePendleAdapter()
    ok, res = asyncio.run(
        _leg(adapter).close("ETH", 0.5, lot={"pt_address": PT_A, "chain_id": 42161})
    )
    assert ok
    assert len(adapter.swaps) == 1
    # 0.5 of 2.0 PT units → a quarter of the 2e18 raw balance, not all of it.
    assert adapter.swaps[0]["amount_in"] == str(int(2 * RAW * 0.25))
    assert adapter.swaps[0]["token_in"] == PT_A
    assert res["units"] == 0.5


def test_lot_isolation_targets_recorded_pt_not_symbol_root():
    # Without the lot, symbol-root matching would find the wallet's unrelated
    # rsETH PT first (both root to ETH). The lot pins the exact token.
    adapter = FakePendleAdapter()
    ok, _res = asyncio.run(
        _leg(adapter).close("ETH", None, lot={"pt_address": PT_A, "chain_id": 42161})
    )
    assert ok
    assert [s["token_in"] for s in adapter.swaps] == [PT_A]


def test_lot_for_absent_pt_closes_nothing():
    # The recorded PT is gone (already redeemed): nothing else may be sold.
    adapter = FakePendleAdapter()
    ok, res = asyncio.run(
        _leg(adapter).close("ETH", None, lot={"pt_address": "0x" + "e" * 40})
    )
    assert ok
    assert adapter.swaps == [] and adapter.converts == []
    assert res["note"] == "no PT position"


def test_full_close_without_units_swaps_lot_balance():
    adapter = FakePendleAdapter()
    ok, _res = asyncio.run(
        _leg(adapter).close("ETH", None, lot={"pt_address": PT_A, "chain_id": 42161})
    )
    assert ok
    assert adapter.swaps[0]["amount_in"] == str(2 * RAW)


def test_position_binds_to_lot_pt_not_first_symbol_root():
    # Without the lot, position() finds the wallet's unrelated rsETH PT first
    # (5 units); the lot pins valuation to the pair's weETH PT (2 units).
    adapter = FakePendleAdapter()
    pos_no_lot = asyncio.run(_leg(adapter).position("ETH"))
    pos_lot = asyncio.run(
        _leg(adapter).position("ETH", lot={"pt_address": PT_A, "chain_id": 42161})
    )
    assert pos_no_lot.units == 5.0
    assert pos_lot.units == 2.0
    assert pos_lot.meta["pt"] == PT_A
