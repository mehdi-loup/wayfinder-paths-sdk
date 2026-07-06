"""Risk-monotonicity + accounting tests for the pair-reduce guard.

No network: hedge/leg are fakes and notifications are stubbed. Run with:
    poetry run pytest tests/paths/funding-rate-harvester -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PATH_DIR = Path(__file__).resolve().parents[3] / "paths/funding-rate-harvester"
sys.path.insert(0, str(PATH_DIR / "scripts"))

import main as harvester  # noqa: E402
from legs import HedgePosition, SpotPosition  # noqa: E402


class FakeHedge:
    def __init__(self, fail_close: bool = False) -> None:
        self.fail_close = fail_close
        self.closed_units: list[float] = []

    async def short_position(self, symbol: str) -> HedgePosition:
        return HedgePosition(
            symbol=symbol,
            size_units=1.0,
            notional_usd=1_000.0,
            entry_price=1_000.0,
            mark_price=1_000.0,
            liq_price=1_300.0,
            unrealized_pnl_usd=0.0,
            margin_used_usd=333.0,
        )

    async def close_short(self, symbol: str, size_units, slippage: float):
        if self.fail_close:
            return False, {"error": "venue rejected"}
        self.closed_units.append(size_units)
        return True, {"size_units": size_units}


class FakeLeg:
    def __init__(self, fail_close: bool = False) -> None:
        self.fail_close = fail_close
        self.closed_units: list[float] = []

    async def position(self, symbol: str, lot=None) -> SpotPosition:
        return SpotPosition("fake_leg", symbol, 1.0, 1_000.0)

    async def close(self, symbol: str, units, lot=None):
        if self.fail_close:
            return False, {"error": "spot venue down"}
        self.closed_units.append(units)
        return True, {"units": units}


def _ctx(hedge: FakeHedge, leg: FakeLeg):
    ctx = harvester.Ctx()
    ctx.hedge = hedge
    ctx.legs = {"fake_leg": leg}
    ctx.state = harvester._default_state()
    ctx.state["reference_value_usd"] = 1_333.0
    return ctx


def _pair() -> dict:
    return {
        "spot_leg": "fake_leg",
        "status": "open",
        "entry_notional_usd": 1_000.0,
        "entry_value_usd": 1_333.0,
        "last_rebalance_ts": None,
        "spot_lot": {"units": 1.0},
    }


@pytest.fixture(autouse=True)
def _mute_notify(monkeypatch):
    async def _noop(title: str, message: str) -> None:
        pass

    monkeypatch.setattr(harvester, "_notify", _noop)


def test_reduce_aborts_before_spot_when_hedge_fails():
    hedge, leg = FakeHedge(fail_close=True), FakeLeg()
    ctx, pair = _ctx(hedge, leg), _pair()
    ok, res = asyncio.run(
        harvester._reduce_pair(ctx, "ETH", pair, fraction=0.25, slippage=0.005)
    )
    assert not ok
    assert leg.closed_units == []  # spot untouched → pair stays symmetric
    assert pair["status"] == "open"  # full-size symmetric pair is not impaired
    assert pair["entry_notional_usd"] == 1_000.0  # accounting unchanged
    assert ctx.state["reference_value_usd"] == 1_333.0


def test_reduce_marks_pair_impaired_when_spot_fails_after_hedge():
    hedge, leg = FakeHedge(), FakeLeg(fail_close=True)
    ctx, pair = _ctx(hedge, leg), _pair()
    ok, res = asyncio.run(
        harvester._reduce_pair(ctx, "ETH", pair, fraction=0.25, slippage=0.005)
    )
    assert not ok
    assert hedge.closed_units == [0.25]
    assert pair["status"] == "impaired"  # asymmetric now → carry actions suspend
    assert "impaired" in res["error"]
    assert pair["entry_notional_usd"] == 1_000.0  # no accounting until resolved


def test_successful_reduce_scales_accounting():
    hedge, leg = FakeHedge(), FakeLeg()
    ctx, pair = _ctx(hedge, leg), _pair()
    ok, res = asyncio.run(
        harvester._reduce_pair(ctx, "ETH", pair, fraction=0.25, slippage=0.005)
    )
    assert ok
    assert hedge.closed_units == [0.25]
    assert leg.closed_units == [0.25]
    # De-risking 25% must not read as a 25% drawdown: basis and reference shrink together.
    assert pair["entry_notional_usd"] == pytest.approx(750.0)
    assert pair["entry_value_usd"] == pytest.approx(1_333.0 * 0.75)
    assert ctx.state["reference_value_usd"] == pytest.approx(1_333.0 * 0.75)
    assert res["released_reference_usd"] == pytest.approx(1_333.0 * 0.25, abs=0.01)
    assert pair["status"] == "open"


def test_reduce_refuses_open_pair_without_lot():
    hedge, leg = FakeHedge(), FakeLeg()
    ctx, pair = _ctx(hedge, leg), _pair()
    pair["spot_lot"] = None
    ok, res = asyncio.run(
        harvester._reduce_pair(ctx, "ETH", pair, fraction=0.25, slippage=0.005)
    )
    assert not ok
    assert hedge.closed_units == [] and leg.closed_units == []  # nothing touched
    assert "no lot record" in res["error"]


def test_reduce_non_open_pair_is_hedge_only():
    # A half-open/impaired pair has no trusted matched spot: de-risking the
    # short must never sell wallet spot balances.
    hedge, leg = FakeHedge(), FakeLeg()
    ctx, pair = _ctx(hedge, leg), _pair()
    pair["status"] = "half_open"
    ok, res = asyncio.run(
        harvester._reduce_pair(ctx, "ETH", pair, fraction=0.25, slippage=0.005)
    )
    assert ok
    assert hedge.closed_units == [0.25]
    assert leg.closed_units == []
    assert res["note"] == "non-open pair: hedge-only de-risk"
    assert pair["entry_notional_usd"] == 1_000.0  # no accounting on recovery pairs


def test_reduce_spot_capped_at_lot_units():
    # Wallet holds 1.0 units but only 0.4 belong to this pair.
    hedge, leg = FakeHedge(), FakeLeg()
    ctx, pair = _ctx(hedge, leg), _pair()
    pair["spot_lot"] = {"units": 0.4}
    ok, _res = asyncio.run(
        harvester._reduce_pair(ctx, "ETH", pair, fraction=0.5, slippage=0.005)
    )
    assert ok
    assert leg.closed_units == [0.2]  # 0.5 × lot, not 0.5 × wallet balance
    assert pair["spot_lot"]["units"] == 0.2
