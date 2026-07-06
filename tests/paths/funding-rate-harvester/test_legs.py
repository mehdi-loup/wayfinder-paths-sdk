"""Leg-selection + pair-ordering tests for the funding-rate-harvester path.

No network — execution ordering is exercised with fake venues/legs. Run with:
    poetry run pytest tests/paths/funding-rate-harvester -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

PATH_DIR = Path(__file__).resolve().parents[3] / "paths/funding-rate-harvester"
sys.path.insert(0, str(PATH_DIR / "scripts"))

from legs import (  # noqa: E402
    HedgePosition,
    HedgeVenue,
    HlSpotLeg,
    PairExecutor,
    SpotLeg,
    SpotPosition,
    close_pair_steps,
    hl_spot_coin,
    match_pt_markets,
    open_failure_leaves_exposure,
    open_pair_steps,
    pt_market_root,
    select_spot_leg,
)

# ---------------------------------------------------------------------------
# Symbol mapping + PT market matching
# ---------------------------------------------------------------------------

def test_hl_spot_coin_wraps_majors():
    assert hl_spot_coin("BTC") == "UBTC"
    assert hl_spot_coin("eth") == "UETH"
    assert hl_spot_coin("HYPE") == "HYPE"


def test_pt_market_root_strips_yield_wrappers():
    assert pt_market_root("weETH") == "ETH"
    assert pt_market_root("wstETH") == "ETH"
    assert pt_market_root("eETH") == "ETH"
    assert pt_market_root("rsETH") == "ETH"
    assert pt_market_root("sUSDe") == "USDE"
    assert pt_market_root("sKAITO") == "KAITO"
    assert pt_market_root("stHYPE") == "HYPE"
    assert pt_market_root("weETH-26JUN2025") == "ETH"
    assert pt_market_root("HYPE") == "HYPE"


def _market(name: str, apy: float, chain_id: int = 42161) -> dict[str, Any]:
    return {
        "marketName": name,
        "fixedApy": apy,
        "chainId": chain_id,
        "marketAddress": "0x" + "1" * 40,
        "ptAddress": "0x" + "2" * 40,
    }


def test_match_pt_markets_only_price_tracking_markets():
    markets = [
        _market("weETH", 0.04),
        _market("sUSDe", 0.11),
        _market("sKAITO", 0.30),
    ]
    eth = match_pt_markets("ETH", markets)
    assert [m["marketName"] for m in eth] == ["weETH"]
    assert match_pt_markets("BTC", markets) == []
    assert [m["marketName"] for m in match_pt_markets("USDE", markets)] == ["sUSDe"]
    assert [m["marketName"] for m in match_pt_markets("KAITO", markets)] == ["sKAITO"]


def test_match_pt_markets_ranks_by_fixed_apy():
    markets = [_market("weETH", 0.03), _market("rsETH", 0.06), _market("eETH", 0.05)]
    ranked = match_pt_markets("ETH", markets)
    assert [m["marketName"] for m in ranked] == ["rsETH", "eETH", "weETH"]


# ---------------------------------------------------------------------------
# Spot-leg selection
# ---------------------------------------------------------------------------

PRIORITY = ["pendle_pt", "etherfi", "ethena", "hl_spot"]


def test_select_spot_leg_prefers_highest_yield():
    picked = select_spot_leg(
        "ETH", {"pendle_pt": 0.04, "etherfi": 0.031, "hl_spot": 0.0}, PRIORITY
    )
    assert picked == ("pendle_pt", 0.04)


def test_select_spot_leg_excludes_unavailable_yield():
    # etherfi supports ETH but its yield feed is down (None) → must not win
    # by defaulting to 0, and must not be selected at all.
    picked = select_spot_leg("ETH", {"etherfi": None, "hl_spot": 0.0}, PRIORITY)
    assert picked == ("hl_spot", 0.0)


def test_select_spot_leg_tie_uses_priority_order():
    picked = select_spot_leg("ETH", {"hl_spot": 0.0, "ethena": 0.0}, PRIORITY)
    assert picked == ("ethena", 0.0)


def test_select_spot_leg_ignores_legs_not_in_priority():
    picked = select_spot_leg("ETH", {"pendle_pt": 0.9}, ["hl_spot"])
    assert picked is None


# ---------------------------------------------------------------------------
# Pair ordering rules
# ---------------------------------------------------------------------------

def test_open_pair_steps_hedge_first():
    assert open_pair_steps("hyperliquid", "pendle_pt") == ["hedge_short", "spot_open"]
    assert open_pair_steps("binance", "etherfi") == ["hedge_short", "spot_open"]


def test_open_pair_steps_atomic_for_same_venue():
    assert open_pair_steps("hyperliquid", "hl_spot") == ["paired_atomic"]


def test_close_pair_steps_hedge_last():
    assert close_pair_steps("hyperliquid", "ethena") == ["spot_close", "hedge_close"]


# ---------------------------------------------------------------------------
# PairExecutor: hedge-first entry, halt-loudly on spot failure, hedge-last exit
# ---------------------------------------------------------------------------

class FakeHedge(HedgeVenue):
    name = "fake_venue"
    funding_interval_hours = 8.0

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.fail_close = False

    async def perp_snapshot(self):
        return {}

    async def mark_price(self, symbol: str) -> float:
        return 100.0

    async def ensure_leverage(self, symbol: str, leverage: int):
        self.calls.append("ensure_leverage")
        return True, "ok"

    async def open_short(self, symbol: str, notional_usd: float, slippage: float):
        self.calls.append("hedge_open")
        return True, {"size_units": notional_usd / 100.0, "price": 100.0}

    async def close_short(self, symbol: str, size_units, slippage: float):
        self.calls.append("hedge_close")
        if self.fail_close:
            return False, {"error": "boom"}
        return True, {"size_units": 1.0}

    async def short_position(self, symbol: str) -> HedgePosition | None:
        return None

    async def free_margin_usd(self) -> float:
        return 1_000.0


class FakeLeg(SpotLeg):
    name = "fake_leg"

    def __init__(self, calls: list[str], fail_open: bool = False, fail_close: bool = False):
        self.calls = calls
        self.fail_open = fail_open
        self.fail_close = fail_close

    async def supports(self, symbol: str) -> bool:
        return True

    async def yield_apy(self, symbol: str):
        return 0.05

    async def open(self, symbol: str, usd_amount: float):
        self.calls.append("spot_open")
        if self.fail_open:
            return False, {"error": "spot venue down"}
        return True, {"units": usd_amount / 100.0}

    async def close(self, symbol: str, units, lot=None):
        self.calls.append("spot_close")
        if self.fail_close:
            return False, {"error": "spot close down"}
        return True, {"units": 1.0}

    async def position(self, symbol: str, lot=None) -> SpotPosition | None:
        return None


def _executor(fail_open: bool = False, fail_close: bool = False, fail_hedge_close: bool = False):
    calls: list[str] = []
    hedge = FakeHedge(calls)
    hedge.fail_close = fail_hedge_close
    leg = FakeLeg(calls, fail_open=fail_open, fail_close=fail_close)
    return PairExecutor(hedge, {"fake_leg": leg}), calls


def test_open_pair_orders_hedge_before_spot():
    executor, calls = _executor()
    ok, report = asyncio.run(
        executor.open_pair("ETH", "fake_leg", 1_000.0, leverage=3, slippage=0.005)
    )
    assert ok
    assert calls == ["ensure_leverage", "hedge_open", "spot_open"]
    assert [s["step"] for s in report["steps"]] == ["hedge_short", "spot_open"]


def test_open_pair_halts_loudly_when_spot_fails_after_hedge():
    executor, calls = _executor(fail_open=True)
    ok, report = asyncio.run(
        executor.open_pair("ETH", "fake_leg", 1_000.0, leverage=3, slippage=0.005)
    )
    assert not ok
    assert calls == ["ensure_leverage", "hedge_open", "spot_open"]
    # The unhedged short is surfaced with explicit remediation — never silent.
    assert "unhedged_short" in report
    assert report["remediation"]
    assert "spot leg failed after hedge opened" in report["error"]


def test_close_pair_spot_first_hedge_last():
    executor, calls = _executor()
    ok, report = asyncio.run(executor.close_pair("ETH", "fake_leg", slippage=0.005))
    assert ok
    assert calls == ["spot_close", "hedge_close"]


def test_close_pair_keeps_hedge_when_spot_close_fails():
    executor, calls = _executor(fail_close=True)
    ok, report = asyncio.run(executor.close_pair("ETH", "fake_leg", slippage=0.005))
    assert not ok
    assert "hedge_close" not in calls  # short keeps protecting the book
    assert "hedge left open intentionally" in report["error"]


def test_close_pair_reports_hedge_close_failure():
    executor, calls = _executor(fail_hedge_close=True)
    ok, report = asyncio.run(executor.close_pair("ETH", "fake_leg", slippage=0.005))
    assert not ok
    assert calls == ["spot_close", "hedge_close"]
    assert "hedge close failed" in report["error"]


# ---------------------------------------------------------------------------
# Half-open state machine: which open failures leave live exposure
# ---------------------------------------------------------------------------

def test_exposure_when_spot_fails_after_hedge():
    executor, _ = _executor(fail_open=True)
    ok, report = asyncio.run(
        executor.open_pair("ETH", "fake_leg", 1_000.0, leverage=3, slippage=0.005)
    )
    assert not ok
    assert open_failure_leaves_exposure(report)


def test_no_exposure_when_hedge_fails_cleanly():
    # Hedge never filled → nothing live → the pair record can be discarded.
    report = {
        "steps": [{"step": "hedge_short", "ok": False, "result": {"error": "margin"}}],
        "error": "hedge open failed: margin",
    }
    assert not open_failure_leaves_exposure(report)


def test_exposure_on_possible_partial_paired_fill():
    report = {"steps": [], "error": "paired fill failed: timeout", "possible_partial_fill": True}
    assert open_failure_leaves_exposure(report)


# ---------------------------------------------------------------------------
# Configured slippage reaches HL spot orders (not hard-coded)
# ---------------------------------------------------------------------------

class FakeHLAdapter:
    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []

    async def get_spot_asset_id(self, coin: str, quote: str) -> int:
        return 10_042

    async def get_all_mid_prices(self):
        return True, {"UETH": 2_000.0, "ETH": 2_000.0}

    def get_valid_order_size(self, asset_id: int, size: float) -> float:
        return round(size, 4)

    async def place_market_order(self, **kwargs):
        self.orders.append(kwargs)
        return True, {"status": "ok"}

    async def get_spot_user_state(self, address: str):
        return True, {"balances": [{"coin": "UETH", "total": 1.0, "hold": 0.0}]}


def test_hl_spot_leg_uses_configured_slippage():
    adapter = FakeHLAdapter()
    leg = HlSpotLeg(adapter, "0xabc", None, slippage=0.0025)
    ok, _res = asyncio.run(leg.open("ETH", 1_000.0))
    assert ok
    ok, _res = asyncio.run(leg.close("ETH", 0.5))
    assert ok
    assert [o["slippage"] for o in adapter.orders] == [0.0025, 0.0025]
