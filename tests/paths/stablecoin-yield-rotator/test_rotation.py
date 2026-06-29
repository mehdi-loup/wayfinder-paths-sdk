"""Constraint engine tests for the stablecoin-yield-rotator path.

These tests are pure logic — they don't hit the network. Run with:
    poetry run pytest tests/paths/stablecoin-yield-rotator -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PATH_DIR = Path(__file__).resolve().parents[3] / "paths/stablecoin-yield-rotator"
sys.path.insert(0, str(PATH_DIR / "scripts"))

from rotation import quote_rotation  # noqa: E402
from venues import Position, VenueRow  # noqa: E402


def _row(venue: str, chain_id: int, asset: str, market_id: str, apy: float, **kw) -> VenueRow:
    return VenueRow(
        venue=venue,
        chain_id=chain_id,
        asset_symbol=asset,
        asset_address="0x" + "0" * 40,
        market_id=market_id,
        decimals=6,
        supply_apy=apy,
        utilization=kw.get("utilization", 0.5),
        supply_cap_headroom_raw=kw.get("headroom"),
        tvl_usd=kw.get("tvl_usd"),
    )


def _position(venue: str, chain_id: int, asset: str, market_id: str, raw_amount: int) -> Position:
    return Position(
        venue=venue, chain_id=chain_id, asset_symbol=asset,
        asset_address="0x" + "0" * 40, market_id=market_id,
        decimals=6, supply_raw=raw_amount, supply_usd=raw_amount / 1e6,
    )


def test_no_rotation_when_uplift_below_min_delta():
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA", 0.040),
        _row("morpho_blue_market",  8453, "USDC", "0xM", 0.043),
    ]
    positions = [_position("aave_v3", 8453, "USDC", "0xA", 1_000 * 10**6)]
    plan = quote_rotation(scan=scan, positions=positions, min_apy_delta_bps=50)
    assert not plan.legs
    assert plan.skipped and "min" in plan.skipped[0].skip_reason


def test_rotation_emitted_when_uplift_clears_min_delta_and_payback():
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA", 0.040),
        _row("morpho_blue_market",  8453, "USDC", "0xM", 0.060),
    ]
    positions = [_position("aave_v3", 8453, "USDC", "0xA", 100_000 * 10**6)]
    plan = quote_rotation(scan=scan, positions=positions, min_apy_delta_bps=50, gas_amortization_days=30, max_position_pct_per_venue=100)
    assert plan.legs
    leg = plan.legs[0]
    assert leg.from_venue == "aave_v3"
    assert leg.to_venue == "morpho_blue_market"
    assert leg.apy_delta_bps == 200
    assert leg.payback_days < 30


def test_utilization_spike_falls_through_to_second_best():
    # Position lives in 0xA1; ranked targets are morpho_blue_market (spiked) then aave_v3 0xA2.
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA1", 0.040),  # current location
        _row("morpho_blue_market",  8453, "USDC", "0xM",  0.080, utilization=0.99),
        _row("aave_v3", 8453, "USDC", "0xA2", 0.060),
    ]
    positions = [_position("aave_v3", 8453, "USDC", "0xA1", 100_000 * 10**6)]
    plan = quote_rotation(scan=scan, positions=positions, min_apy_delta_bps=50, max_position_pct_per_venue=100)
    assert plan.legs
    assert plan.legs[0].to_venue == "aave_v3"
    assert plan.legs[0].to_market_id == "0xA2"


def test_supply_cap_headroom_skips_target():
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA1", 0.040),
        _row("morpho_blue_market",  8453, "USDC", "0xM",  0.080, headroom=10),
        _row("aave_v3", 8453, "USDC", "0xA2", 0.060),
    ]
    positions = [_position("aave_v3", 8453, "USDC", "0xA1", 100_000 * 10**6)]
    plan = quote_rotation(scan=scan, positions=positions, min_apy_delta_bps=50, max_position_pct_per_venue=100)
    assert plan.legs
    assert plan.legs[0].to_market_id == "0xA2"


def test_apy_outlier_falls_through_to_second_best():
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA1", 0.040),
        _row("morpho_vault", 8453, "USDC", "0xOUTLIER", 1338.0),
        _row("morpho_blue_market", 8453, "USDC", "0xM", 0.060),
    ]
    positions = [_position("aave_v3", 8453, "USDC", "0xA1", 100_000 * 10**6)]
    plan = quote_rotation(
        scan=scan,
        positions=positions,
        min_apy_delta_bps=50,
        max_position_pct_per_venue=100,
        max_target_apy=0.5,
    )
    assert plan.legs
    assert plan.legs[0].to_venue == "morpho_blue_market"
    assert plan.legs[0].to_market_id == "0xM"


def test_payback_floor_blocks_tiny_uplift():
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA", 0.040),
        _row("morpho_blue_market",  8453, "USDC", "0xM", 0.046),
    ]
    # Position too small for the gas to pay back within window.
    positions = [_position("aave_v3", 8453, "USDC", "0xA", 100 * 10**6)]
    plan = quote_rotation(scan=scan, positions=positions, min_apy_delta_bps=50, gas_amortization_days=30, same_chain_gas_usd=4.0)
    assert not plan.legs
    assert plan.skipped and "payback" in plan.skipped[0].skip_reason


def test_cross_chain_bridge_gate():
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA", 0.040),
        _row("morpho_blue_market",  42161, "USDC", "0xM", 0.060),
    ]
    # Position sized so payback fits inside the window but the bridge gate fails:
    # uplift_30d ≈ 33 USD; bridge gate requires uplift × (window/30) > bridge × 2 → 99 > 120 (no).
    positions = [_position("aave_v3", 8453, "USDC", "0xA", 20_000 * 10**6)]
    plan = quote_rotation(
        scan=scan, positions=positions, min_apy_delta_bps=50,
        gas_amortization_days=90, cross_chain_gas_usd=12.0, bridge_fee_usd=60.0,
        max_position_pct_per_venue=100,
    )
    assert not plan.legs
    assert plan.skipped and "cross_chain" in plan.skipped[0].skip_reason


def test_blocklist_excludes_market():
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA", 0.040),
        _row("morpho_blue_market",  8453, "USDC", "0xBANNED", 0.080),
        _row("euler_v2", 8453, "USDC", "0xE", 0.055),
    ]
    positions = [_position("aave_v3", 8453, "USDC", "0xA", 100_000 * 10**6)]
    plan = quote_rotation(
        scan=scan, positions=positions, min_apy_delta_bps=50,
        blocklist_markets=["0xBANNED"], max_position_pct_per_venue=100,
    )
    assert plan.legs and plan.legs[0].to_venue == "euler_v2"


def test_diversification_cap():
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA", 0.040),
        _row("morpho_blue_market",  8453, "USDC", "0xM", 0.080),
    ]
    # 100% would land in morpho_blue_market if fully rotated; cap of 50% sizes it down.
    positions = [_position("aave_v3", 8453, "USDC", "0xA", 100_000 * 10**6)]
    plan = quote_rotation(
        scan=scan, positions=positions, min_apy_delta_bps=50, max_position_pct_per_venue=50,
    )
    assert plan.legs
    assert plan.legs[0].raw_amount == 50_000 * 10**6
    assert not plan.skipped


def test_diversification_cap_across_multiple_source_positions():
    # Two separate positions of the same asset both want to rotate into the single
    # top venue. Each would be sized to the 50% cap in isolation; combined they must
    # still not exceed 50% of the asset total (regression: per-pass inflow tracking).
    scan = [
        _row("aave_v3", 8453, "USDC", "0xA", 0.040),
        _row("hyperlend", 999, "USDC", "0xH", 0.040),
        _row("morpho_blue_market", 8453, "USDC", "0xM", 0.090),
    ]
    positions = [
        _position("aave_v3", 8453, "USDC", "0xA", 50_000 * 10**6),
        _position("hyperlend", 999, "USDC", "0xH", 50_000 * 10**6),
    ]
    plan = quote_rotation(
        scan=scan, positions=positions, min_apy_delta_bps=50, max_position_pct_per_venue=50,
    )
    into_morpho = sum(
        leg.raw_amount for leg in plan.legs
        if (leg.to_venue, leg.to_chain_id) == ("morpho_blue_market", 8453)
    )
    # 50% of the 100k USDC total — not 100% from both legs stacking.
    assert into_morpho <= 50_000 * 10**6
    assert any("diversification_cap" in (s.skip_reason or "") for s in plan.skipped)


@pytest.mark.parametrize("asset,decimals", [("USDC", 6), ("USDT", 6), ("DAI", 18)])
def test_per_asset_decimal_handling(asset, decimals):
    scan = [
        VenueRow(
            venue="aave_v3", chain_id=8453, asset_symbol=asset,
            asset_address="0x" + "0" * 40, market_id="0xA",
            decimals=decimals, supply_apy=0.04, utilization=0.5,
            supply_cap_headroom_raw=None, tvl_usd=None,
        ),
        VenueRow(
            venue="morpho_blue_market", chain_id=8453, asset_symbol=asset,
            asset_address="0x" + "0" * 40, market_id="0xM",
            decimals=decimals, supply_apy=0.07, utilization=0.5,
            supply_cap_headroom_raw=None, tvl_usd=None,
        ),
    ]
    positions = [Position(
        venue="aave_v3", chain_id=8453, asset_symbol=asset,
        asset_address="0x" + "0" * 40, market_id="0xA",
        decimals=decimals, supply_raw=100_000 * (10 ** decimals),
        supply_usd=100_000.0,
    )]
    plan = quote_rotation(scan=scan, positions=positions, min_apy_delta_bps=50, max_position_pct_per_venue=100)
    assert plan.legs
    # Uplift should be ~3% of 100k for ~30/365 of a year.
    assert plan.legs[0].estimated_uplift_usd_30d == pytest.approx(100_000 * 0.03 * 30 / 365, rel=1e-6)
