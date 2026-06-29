"""Repo-level tests for the stablecoin-yield-rotator path.

These exercise the CLI action handlers with the venue layer mocked, so they don't
require a network or a funded wallet. They assert against the **current** response
shape (`ranked`, `plan`, executes/no-ops/requires_confirmation), not the legacy
fixture shape (`rows`, `proposal`, `requires_adapter_execution`).

Run from repo root:
    poetry run pytest tests/paths/stablecoin-yield-rotator -v
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

PATH_DIR = Path(__file__).resolve().parents[3] / "paths/stablecoin-yield-rotator"
sys.path.insert(0, str(PATH_DIR / "scripts"))

import main as rotator  # noqa: E402
from rotation import DiscoveryGapError  # noqa: E402
from venues import Position, VenueRow  # noqa: E402

CONFIG = {
    "wallet": "main",
    "chains": [8453],
    "assets": ["USDC"],
    "venues": ["aave_v3", "morpho_blue_market"],
    "constraints": {
        "min_apy_delta_bps": 50,
        "gas_amortization_days": 30,
        "max_gas_usd_per_rotation": 25,
        "max_position_pct_per_venue": 100,
        "blocklist_markets": [],
    },
    "slippage_bps": 30,
}

WALLET_ADDRESS = "0x1111111111111111111111111111111111111111"
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _make_row(venue: str, market_id: str, apy: float) -> VenueRow:
    return VenueRow(
        venue=venue, chain_id=8453, asset_symbol="USDC",
        asset_address=USDC_ADDRESS, market_id=market_id, decimals=6,
        supply_apy=apy, utilization=0.5, supply_cap_headroom_raw=None,
        tvl_usd=None,
    )


def _make_position(venue: str, market_id: str, raw: int) -> Position:
    return Position(
        venue=venue, chain_id=8453, asset_symbol="USDC",
        asset_address=USDC_ADDRESS, market_id=market_id, decimals=6,
        supply_raw=raw, supply_usd=raw / 1e6,
    )


@pytest.fixture
def fake_signing_callback():
    async def _fake(label: str):
        return (AsyncMock(), WALLET_ADDRESS)
    return _fake


@pytest.fixture(autouse=True)
def isolated_scan_cache(tmp_path):
    with patch("main.SCAN_CACHE_DIR", tmp_path / "scan_cache"):
        yield


@pytest.fixture(autouse=True)
def funded_gas():
    """Default every chain to well-funded so planning never hits the network;
    gas-specific tests override with their own patches. Top-up pricing is stubbed
    to fail so starved chains use the deterministic flat fallback cost."""
    no_network = AsyncMock(side_effect=RuntimeError("no network in tests"))
    with (
        patch("main._gas_balance_wei", AsyncMock(return_value=10**18)),
        patch.object(rotator.TOKEN_CLIENT, "get_gas_token", no_network),
    ):
        yield


@pytest.fixture(autouse=True)
def block_external_reads():
    """Keep the per-chain balance multicall and the Euler/Delta Lab discovery off the
    network by default; tests that exercise them override these patches. Also resets
    the Euler discovery memo so each test re-discovers against its own mock."""
    import venues as _venues  # noqa: PLC0415
    _venues._euler_discovery_cache["ts"] = 0.0
    _venues._euler_discovery_cache["by_chain"] = {}
    with (
        patch("main._fetch_chain_balances", AsyncMock(return_value={})),
        patch.object(_venues.DELTA_LAB_CLIENT, "screen_lending", AsyncMock(return_value={"data": []})),
    ):
        yield


async def test_scan_returns_ranked_table(fake_signing_callback):
    rows = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    with (
        patch("main.scan_all", AsyncMock(return_value=rows)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_scan(CONFIG)

    assert result["action"] == "scan"
    assert "ranked" in result
    assert "by_asset" in result
    assert result["row_count"] == 2
    # ranked is sorted desc by APY
    assert result["ranked"][0]["venue"] == "morpho_blue_market"
    assert result["ranked"][1]["venue"] == "aave_v3"


async def test_scan_excludes_unsafe_rows_from_ranked_table(fake_signing_callback):
    wild = VenueRow(
        venue="morpho_blue_market", chain_id=8453, asset_symbol="USDC",
        asset_address=USDC_ADDRESS, market_id="0xWILD", decimals=6,
        supply_apy=1000.0, utilization=1.0, supply_cap_headroom_raw=None,
        tvl_usd=5_000_000.0,
    )
    missing_asset = VenueRow(
        venue="euler_v2", chain_id=8453, asset_symbol="USDC",
        asset_address="None", market_id="0xVAULT", decimals=6,
        supply_apy=0.50, utilization=0.0, supply_cap_headroom_raw=None,
        tvl_usd=None,
    )
    low_tvl = VenueRow(
        venue="morpho_blue_market", chain_id=8453, asset_symbol="USDC",
        asset_address=USDC_ADDRESS, market_id="0xLOWTVL", decimals=6,
        supply_apy=0.25, utilization=0.5, supply_cap_headroom_raw=None,
        tvl_usd=25_000.0,
    )
    normal = _make_row("aave_v3", USDC_ADDRESS, 0.040)
    normal.tvl_usd = 500_000.0
    with patch("main.scan_all", AsyncMock(return_value=[wild, missing_asset, normal])):
        result = await rotator.action_scan(CONFIG)

    assert result["row_count"] == 3
    assert result["ranked_count"] == 1
    assert result["excluded_count"] == 2
    assert result["ranked"][0]["market_id"] == USDC_ADDRESS
    assert {row["market_id"] for row in result["excluded"]} == {"0xWILD", "0xVAULT"}

    tvl_config = {
        **CONFIG,
        "constraints": {**CONFIG["constraints"], "min_scan_tvl_usd": 100_000},
    }
    with patch("main.scan_all", AsyncMock(return_value=[wild, missing_asset, low_tvl, normal])):
        result = await rotator.action_scan(tvl_config)

    assert result["row_count"] == 4
    assert result["ranked_count"] == 1
    assert result["excluded_count"] == 3
    excluded_by_market = {row["market_id"]: row["exclude_reason"] for row in result["excluded"]}
    assert "tvl_usd" in excluded_by_market["0xLOWTVL"]


async def test_scan_excludes_zero_apy_rows(fake_signing_callback):
    zero = _make_row("morpho_blue_market", "0xZERO", 0.0)
    dust = _make_row("morpho_vault", "0xDUST", 0.00005)
    normal = _make_row("aave_v3", USDC_ADDRESS, 0.040)
    with patch("main.scan_all", AsyncMock(return_value=[zero, dust, normal])):
        result = await rotator.action_scan(CONFIG)

    assert result["ranked_count"] == 1
    assert result["ranked"][0]["market_id"] == USDC_ADDRESS
    excluded_by_market = {row["market_id"]: row["exclude_reason"] for row in result["excluded"]}
    assert excluded_by_market["0xZERO"] == "supply_apy ~ 0%"
    assert excluded_by_market["0xDUST"] == "supply_apy ~ 0%"


async def test_quote_rotation_excludes_apy_outlier_target(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_vault", "0xOUTLIER", 1338.0),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    config = {
        **CONFIG,
        "venues": ["aave_v3", "morpho_blue_market", "morpho_vault"],
        "constraints": {**CONFIG["constraints"], "max_scan_apy": 0.5},
    }
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_quote_rotation(config)

    assert result["plan"]["legs"]
    assert result["plan"]["legs"][0]["to"] == "morpho_blue_market@8453"


async def test_quote_rotation_reuses_wallet_agnostic_scan_cache(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    fake_scan_all = AsyncMock(return_value=scan)
    fake_positions_all = AsyncMock(return_value=positions)
    with (
        patch("main.scan_all", fake_scan_all),
        patch("main.positions_all", fake_positions_all),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        first = await rotator.action_quote_rotation(CONFIG)
        second = await rotator.action_quote_rotation(CONFIG)

    assert first["plan"]["legs"]
    assert second["plan"]["legs"]
    fake_scan_all.assert_awaited_once()
    assert fake_positions_all.await_count == 2


async def test_euler_scan_discovers_via_delta_lab_then_reads_only_those_vaults():
    from venues import _scan_euler_v2  # noqa: PLC0415

    vault = "0x797dd80692c3b2dAdaBCe8e30C07fDE5307D48a9"
    # Delta Lab discovery returns the one stable Euler vault (apy from Delta Lab).
    discovered = {vault: {"vault": vault, "apy": 0.05, "symbol": "USDC",
                          "is_frozen": False, "is_paused": False, "tvl_usd": 1_000_000.0}}

    adapter = AsyncMock()
    adapter.get_vault_info_full = AsyncMock(return_value=(True, {
        "assetSymbol": "USDC", "asset": USDC_ADDRESS, "assetDecimals": 6,
        "totalCash": 25, "totalBorrowed": 75,
    }))

    with patch("venues._euler_stable_vaults", AsyncMock(return_value=discovered)):
        rows = await _scan_euler_v2(adapter, chain_id=8453, allowed={"USDC"})

    # Only the discovered vault is read on-chain — never the full enumeration.
    adapter.get_all_markets.assert_not_called()
    adapter.get_vault_info_full.assert_awaited_once()
    assert len(rows) == 1
    assert rows[0].market_id == vault
    assert rows[0].asset_address == USDC_ADDRESS
    assert rows[0].asset_symbol == "USDC"
    assert rows[0].supply_apy == pytest.approx(0.05)  # from Delta Lab
    assert rows[0].utilization == pytest.approx(0.75)  # from on-chain cash/borrows
    assert rows[0].tvl_usd == pytest.approx(0.0001)


async def test_euler_stable_vaults_filters_delta_lab_rows():
    import venues  # noqa: PLC0415

    venues._euler_discovery_cache["ts"] = 0.0
    venues._euler_discovery_cache["by_chain"] = {}
    payload = {"data": [
        {"venue_name": "euler-v2-base", "chain_id": 8453, "symbol": "USDC",
         "market_external_id": "0x797dd80692c3b2dadabce8e30c07fde5307d48a9",
         "net_supply_apr_now": 0.05, "supply_tvl_usd": 1_000_000.0},
        {"venue_name": "euler-v2-base", "chain_id": 8453, "symbol": "WETH",  # non-stable: dropped
         "market_external_id": "0x1111111111111111111111111111111111111111",
         "net_supply_apr_now": 0.02},
        {"venue_name": "aave-base", "chain_id": 8453, "symbol": "USDC",  # non-euler: dropped
         "market_external_id": "0x2222222222222222222222222222222222222222"},
    ]}
    with patch.object(venues.DELTA_LAB_CLIENT, "screen_lending", AsyncMock(return_value=payload)):
        found = await venues._euler_stable_vaults(8453, {"USDC"})

    assert len(found) == 1
    (vault, meta), = found.items()
    assert vault.lower() == "0x797dd80692c3b2dadabce8e30c07fde5307d48a9"
    assert meta["symbol"] == "USDC"


async def test_morpho_vault_scan_maps_listed_stable_vaults():
    from venues import _scan_morpho_vault  # noqa: PLC0415

    adapter = AsyncMock()
    adapter.get_all_vaults = AsyncMock(return_value=(True, [{
        "address": "0xeE8F4eC5672F09119b96Ab6fB59C27E1b7e44b61",
        "name": "Gauntlet USDC Prime",
        "symbol": "gtUSDCp",
        "version": "v1",
        "listed": True,
        "warnings": [],
        "asset": {
            "address": USDC_ADDRESS,
            "symbol": "USDC",
            "decimals": 6,
        },
        "state": {
            # net_apy bakes in reward tokens; the rotator ranks on the reward-free
            # figure for consistency with the lending venues, so it must win here.
            "net_apy": 0.052,
            "net_apy_excluding_rewards": 0.041,
            "total_assets_usd": 354_000_000.0,
        },
    }]))

    rows = await _scan_morpho_vault(adapter, chain_id=8453, allowed={"USDC"})

    assert len(rows) == 1
    assert rows[0].venue == "morpho_vault"
    assert rows[0].market_id == "0xeE8F4eC5672F09119b96Ab6fB59C27E1b7e44b61"
    assert rows[0].asset_symbol == "USDC"
    assert rows[0].supply_apy == pytest.approx(0.041)
    assert rows[0].tvl_usd == pytest.approx(354_000_000.0)


async def test_morpho_vault_lend_dispatcher_uses_vault_deposit():
    from venues import lend  # noqa: PLC0415

    fake_adapter = AsyncMock()
    fake_adapter.vault_deposit = AsyncMock(return_value=(True, {"hash": "0xVAULT"}))

    with patch("venues.get_write_adapter", AsyncMock(return_value=fake_adapter)):
        ok, tx = await lend(
            venue="morpho_vault",
            wallet_label="main",
            chain_id=8453,
            market_id="0xeE8F4eC5672F09119b96Ab6fB59C27E1b7e44b61",
            raw_amount=123,
        )

    assert ok
    assert tx == {"hash": "0xVAULT"}
    fake_adapter.vault_deposit.assert_awaited_once_with(
        chain_id=8453,
        vault_address="0xeE8F4eC5672F09119b96Ab6fB59C27E1b7e44b61",
        assets=123,
    )


# ---------------------------------------------------------------------------
# Moonwell (Compound-fork mTokens) — scan dedup + lend/unlend exchange-rate glue
# ---------------------------------------------------------------------------

USDBC_ADDRESS = "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA"
DAI_ADDRESS = "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb"
# 1 mToken == 2 underlying, in the adapter's 1e18 convention.
MW_EXCHANGE_RATE = 2 * 10**18


def _moonwell_market(symbol, underlying, mtoken, supply_apy, tvl_usd, *, mint_paused=False, is_listed=True):
    return {
        "symbol": symbol,
        "underlying": underlying,
        "mtoken": mtoken,
        "supplyApy": supply_apy,
        "totalSupplyUsd": tvl_usd,
        "mintPaused": mint_paused,
        "isListed": is_listed,
        "cash": 50,
        "totalBorrows": 50,
        "totalReserves": 0,
        "exchangeRate": MW_EXCHANGE_RATE,
        "totalSupply": 100,
        "supplyCap": 10**30,
    }


async def test_moonwell_scan_maps_apy_and_freezes_duplicate_stable():
    from venues import _scan_moonwell  # noqa: PLC0415

    adapter = AsyncMock()
    adapter.get_all_markets = AsyncMock(return_value=(True, [
        # Legacy USDbC market that Moonwell still symbols "mUSDC" — the duplicate.
        _moonwell_market("mUSDC", USDBC_ADDRESS, "0xLEGACY", 0.0, 8_000.0),
        # Canonical native-USDC market — deeper TVL, should win.
        _moonwell_market("mUSDC", USDC_ADDRESS, "0xNATIVE", 0.0396, 15_000_000.0),
        _moonwell_market("mDAI", DAI_ADDRESS, "0xDAI", 0.05, 20_000.0),
        # Filtered out entirely: mint paused and unlisted.
        _moonwell_market("mUSDC", USDC_ADDRESS, "0xPAUSED", 0.09, 9_000_000.0, mint_paused=True),
        _moonwell_market("mUSDT", "0xUSDT", "0xUNLISTED", 0.09, 9_000_000.0, is_listed=False),
    ]))

    rows = await _scan_moonwell(adapter, chain_id=8453, allowed={"USDC", "USDT", "DAI"})

    # Both USDC markets are KEPT (not dropped) so a holder of the legacy market can
    # still be rotated out; the lower-TVL duplicate is frozen as a target.
    usdc_rows = {r.market_id: r for r in rows if r.asset_symbol == "USDC"}
    assert set(usdc_rows) == {"0xNATIVE", "0xLEGACY"}
    native, legacy = usdc_rows["0xNATIVE"], usdc_rows["0xLEGACY"]
    assert native.is_frozen is False
    assert native.supply_apy == pytest.approx(0.0396)  # supplyApy key mapped through
    assert native.utilization == pytest.approx(0.5)
    assert legacy.is_frozen is True
    assert "duplicate" in legacy.extra["frozen_reason"]
    # The sole DAI market is not a duplicate -> kept and unfrozen.
    dai = next(r for r in rows if r.asset_symbol == "DAI")
    assert dai.is_frozen is False
    assert dai.decimals == 18


async def test_moonwell_lend_dispatcher_resolves_underlying_for_approval():
    from venues import lend  # noqa: PLC0415

    fake_adapter = AsyncMock()
    fake_adapter.get_pos = AsyncMock(return_value=(True, {
        "underlying_token": USDC_ADDRESS,
        "mtoken_balance": 0,
        "exchange_rate": MW_EXCHANGE_RATE,
    }))
    fake_adapter.lend = AsyncMock(return_value=(True, {"hash": "0xMINT"}))

    with patch("venues.get_write_adapter", AsyncMock(return_value=fake_adapter)):
        ok, tx = await lend(
            venue="moonwell", wallet_label="main", chain_id=8453,
            market_id="0xNATIVE", raw_amount=50_000_000,
        )

    assert ok and tx == {"hash": "0xMINT"}
    fake_adapter.lend.assert_awaited_once_with(
        mtoken="0xNATIVE", underlying_token=USDC_ADDRESS, amount=50_000_000,
    )


async def test_moonwell_unlend_partial_converts_underlying_to_mtoken_units():
    from venues import unlend  # noqa: PLC0415

    fake_adapter = AsyncMock()
    fake_adapter.get_pos = AsyncMock(return_value=(True, {
        "mtoken_balance": 100_000_000,
        "exchange_rate": MW_EXCHANGE_RATE,
        "underlying_token": USDC_ADDRESS,
    }))
    fake_adapter.unlend = AsyncMock(return_value=(True, {"hash": "0xREDEEM"}))

    with patch("venues.get_write_adapter", AsyncMock(return_value=fake_adapter)):
        ok, tx = await unlend(
            venue="moonwell", wallet_label="main", chain_id=8453,
            market_id="0xNATIVE", raw_amount=50_000_000, withdraw_full=False,
        )

    # 50 USDC underlying / (1 mToken = 2 underlying) -> 25_000_000 mToken units.
    assert ok and tx == {"hash": "0xREDEEM"}
    fake_adapter.unlend.assert_awaited_once_with(mtoken="0xNATIVE", amount=25_000_000)


async def test_moonwell_unlend_partial_clamps_to_mtoken_balance():
    from venues import unlend  # noqa: PLC0415

    fake_adapter = AsyncMock()
    fake_adapter.get_pos = AsyncMock(return_value=(True, {
        "mtoken_balance": 10_000_000,
        "exchange_rate": MW_EXCHANGE_RATE,
        "underlying_token": USDC_ADDRESS,
    }))
    fake_adapter.unlend = AsyncMock(return_value=(True, {"hash": "0xREDEEM"}))

    with patch("venues.get_write_adapter", AsyncMock(return_value=fake_adapter)):
        ok, _ = await unlend(
            venue="moonwell", wallet_label="main", chain_id=8453,
            market_id="0xNATIVE", raw_amount=10**18, withdraw_full=False,
        )

    assert ok
    # Requested far more than held; redeem is capped at the full mToken balance.
    fake_adapter.unlend.assert_awaited_once_with(mtoken="0xNATIVE", amount=10_000_000)


async def test_moonwell_unlend_full_redeems_entire_mtoken_balance():
    from venues import unlend  # noqa: PLC0415

    fake_adapter = AsyncMock()
    fake_adapter.get_pos = AsyncMock(return_value=(True, {
        "mtoken_balance": 100_000_000,
        "exchange_rate": MW_EXCHANGE_RATE,
        "underlying_token": USDC_ADDRESS,
    }))
    fake_adapter.unlend = AsyncMock(return_value=(True, {"hash": "0xREDEEM"}))

    with patch("venues.get_write_adapter", AsyncMock(return_value=fake_adapter)):
        ok, _ = await unlend(
            venue="moonwell", wallet_label="main", chain_id=8453,
            market_id="0xNATIVE", raw_amount=0, withdraw_full=True,
        )

    assert ok
    fake_adapter.unlend.assert_awaited_once_with(mtoken="0xNATIVE", amount=100_000_000)


async def test_moonwell_unlend_errors_when_no_position():
    from venues import unlend  # noqa: PLC0415

    fake_adapter = AsyncMock()
    fake_adapter.get_pos = AsyncMock(return_value=(True, {"mtoken_balance": 0}))
    fake_adapter.unlend = AsyncMock()

    with patch("venues.get_write_adapter", AsyncMock(return_value=fake_adapter)):
        ok, payload = await unlend(
            venue="moonwell", wallet_label="main", chain_id=8453,
            market_id="0xNATIVE", raw_amount=0, withdraw_full=True,
        )

    assert not ok
    assert "no Moonwell position" in payload["error"]
    fake_adapter.unlend.assert_not_awaited()


# ---------------------------------------------------------------------------
# Frozen-venue rotation + scan classification (general)
# ---------------------------------------------------------------------------


async def test_position_in_frozen_venue_is_rotated_out_not_ignored():
    """A frozen row blocks *entry* (target) but must not strand funds already there:
    a holder of the frozen venue is still offered a rotation *out*."""
    from rotation import quote_rotation  # noqa: PLC0415

    frozen_source = VenueRow(
        venue="moonwell", chain_id=8453, asset_symbol="USDC",
        asset_address=USDC_ADDRESS, market_id="0xFROZENMW", decimals=6,
        supply_apy=0.02, utilization=0.5, supply_cap_headroom_raw=None,
        tvl_usd=20_000_000.0, is_frozen=True,
    )
    better = _make_row("aave_v3", USDC_ADDRESS, 0.06)
    scan = [frozen_source, better]
    positions = [_make_position("moonwell", "0xFROZENMW", 100_000 * 10**6)]

    plan = quote_rotation(scan=scan, positions=positions, min_apy_delta_bps=50, max_position_pct_per_venue=100)

    assert plan.legs
    assert plan.legs[0].from_venue == "moonwell"  # rotated OUT of the frozen venue
    assert plan.legs[0].to_venue == "aave_v3"


async def test_scan_moves_frozen_rows_to_excluded():
    """Frozen rows (e.g. the deduped Moonwell duplicate) must not appear in the ranked set."""
    frozen = VenueRow(
        venue="moonwell", chain_id=8453, asset_symbol="USDC", asset_address=USDC_ADDRESS,
        market_id="0xFROZEN", decimals=6, supply_apy=0.05, utilization=0.5,
        supply_cap_headroom_raw=None, tvl_usd=5_000_000.0, is_frozen=True,
        extra={"frozen_reason": "duplicate Moonwell market"},
    )
    normal = _make_row("aave_v3", USDC_ADDRESS, 0.04)
    normal.tvl_usd = 5_000_000.0
    with patch("main.scan_all", AsyncMock(return_value=[frozen, normal])):
        result = await rotator.action_scan(CONFIG)

    assert {r["market_id"] for r in result["ranked"]} == {USDC_ADDRESS}
    excluded = {r["market_id"]: r["exclude_reason"] for r in result["excluded"]}
    assert "duplicate Moonwell market" in excluded["0xFROZEN"]

async def test_quote_rotation_returns_plan(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_quote_rotation(CONFIG)

    assert result["action"] == "quote-rotation"
    assert "plan" in result
    assert result["plan"]["legs"]
    leg = result["plan"]["legs"][0]
    assert leg["from"] == "aave_v3@8453"
    assert leg["to"] == "morpho_blue_market@8453"
    assert leg["apy_delta_bps"] == 200


async def test_quote_rotation_sizes_leg_to_diversification_cap():
    from rotation import quote_rotation  # noqa: PLC0415

    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]

    plan = quote_rotation(
        scan=scan,
        positions=positions,
        min_apy_delta_bps=50,
        max_position_pct_per_venue=50,
    )

    assert len(plan.legs) == 1
    assert not plan.skipped
    assert plan.legs[0].raw_amount == 50_000 * 10**6
    assert plan.legs[0].estimated_uplift_usd_30d == pytest.approx(
        50_000 * 0.02 * (30 / 365)
    )


async def test_update_without_confirm_emits_requires_confirmation(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_update(CONFIG, confirmed=False)

    assert result["action"] == "update"
    assert result["status"] == "requires_confirmation"
    assert result["plan"]["legs"]


async def test_update_with_confirm_runs_gas_check_then_executes(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    fake_unlend = AsyncMock(return_value=(True, {"hash": "0xWITHDRAW"}))
    fake_lend = AsyncMock(return_value=(True, {"hash": "0xDEPOSIT"}))
    # Source balance before/after the withdraw — the delta is what actually came back.
    # (Idle balances no longer use get_token_balance; they come from _fetch_chain_balances.)
    fake_balance = AsyncMock(side_effect=[0, 100_000 * 10**6])

    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main._gas_balance_wei", AsyncMock(return_value=10**18)),  # plenty of gas
        patch("main.get_token_balance", fake_balance),
        patch("main.unlend", fake_unlend),
        patch("main.lend", fake_lend),
    ):
        result = await rotator.action_update(CONFIG, confirmed=True)

    assert result["action"] == "update"
    assert result["status"] == "ok"
    assert len(result["executed"]) == 1
    assert fake_unlend.await_count == 1
    fake_unlend.assert_awaited_once_with(
        venue="aave_v3",
        wallet_label="main",
        chain_id=8453,
        market_id=USDC_ADDRESS,
        raw_amount=100_000 * 10**6,
        withdraw_full=False,
    )
    # Deposit spends the measured withdrawn delta, not the planned amount.
    assert fake_lend.await_count == 1
    assert fake_lend.await_args.kwargs["raw_amount"] == 100_000 * 10**6


async def test_update_rechecks_target_before_withdraw(fake_signing_callback):
    initial_scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    stale_target = _make_row("morpho_blue_market", "0xMARKET", 0.060)
    stale_target.utilization = 0.99
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    fake_unlend = AsyncMock(return_value=(True, {"hash": "0xWITHDRAW"}))

    with (
        patch("main.scan_all", AsyncMock(side_effect=[initial_scan, [stale_target]])),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main._check_gas_budget", AsyncMock(return_value={"balances": {8453: 10**18}, "insufficient": []})),
        patch("main.unlend", fake_unlend),
    ):
        result = await rotator.action_update(CONFIG, confirmed=True)

    assert result["status"] == "halted"
    assert "target re-check failed before withdraw" in result["reason"]
    fake_unlend.assert_not_awaited()


async def test_update_halts_when_gas_insufficient(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]

    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main._gas_balance_wei", AsyncMock(return_value=0)),  # no gas anywhere
    ):
        result = await rotator.action_update(CONFIG, confirmed=True)

    assert result["status"] == "halted"
    assert "insufficient" in result["reason"].lower() or result["gas_check"]["insufficient"]


async def test_update_halts_when_unlend_reverts(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    failing_unlend = AsyncMock(return_value=(False, {"error": "RPC revert"}))

    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main._gas_balance_wei", AsyncMock(return_value=10**18)),
        patch("main.get_token_balance", AsyncMock(return_value=0)),
        patch("main.unlend", failing_unlend),
    ):
        result = await rotator.action_update(CONFIG, confirmed=True)

    assert result["status"] == "halted"
    assert "revert" in result["reason"].lower()


async def test_status_returns_blended_apy(fake_signing_callback):
    positions = [
        _make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6),
        _make_position("morpho_blue_market", "0xMARKET", 50_000 * 10**6),
    ]
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_status(CONFIG)

    assert result["action"] == "status"
    assert result["total_supply_usd"] == pytest.approx(150_000.0)
    # blended = (100k*0.04 + 50k*0.06) / 150k = 0.0466...
    assert result["blended_apy"] == pytest.approx((100_000 * 0.04 + 50_000 * 0.06) / 150_000, rel=1e-3)


async def test_quote_rotation_refuses_when_position_market_missing_from_scan(fake_signing_callback):
    # Position lives in a market that scan didn't return — discovery gap.
    scan = [_make_row("morpho_blue_market", "0xMARKET", 0.060)]
    positions = [_make_position("aave_v3", "0xUNKNOWN", 100_000 * 10**6)]
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        pytest.raises(DiscoveryGapError),
    ):
        await rotator.action_quote_rotation(CONFIG)


async def test_sparklend_lend_dispatcher_raises_not_implemented():
    """Bug #13: SparkLend has no lend/unlend; dispatcher must refuse, not crash adapter calls."""
    from venues import lend  # noqa: PLC0415
    with pytest.raises(NotImplementedError, match="not executable"):
        await lend(venue="sparklend", wallet_label="main", chain_id=1, market_id="0xUSDC", raw_amount=100)


async def test_sparklend_unlend_dispatcher_raises_not_implemented():
    from venues import unlend  # noqa: PLC0415
    with pytest.raises(NotImplementedError, match="not executable"):
        await unlend(venue="sparklend", wallet_label="main", chain_id=1, market_id="0xUSDC", raw_amount=0, withdraw_full=True)


async def test_quote_rotation_skips_sparklend_source(fake_signing_callback):
    """Bug #13: positions in non-executable venues should be skipped, not produce broken plans."""
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.080),
    ]
    spark_pos = Position(
        venue="sparklend", chain_id=1, asset_symbol="USDC",
        asset_address=USDC_ADDRESS, market_id=USDC_ADDRESS, decimals=6,
        supply_raw=10_000 * 10**6, supply_usd=10_000.0,
    )
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=[spark_pos])),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_quote_rotation(CONFIG)

    plan = result["plan"]
    assert not plan["legs"]
    assert plan["skipped"]
    assert "not executable" in plan["skipped"][0]["skip_reason"]


async def test_cross_chain_leg_includes_bridge_quote(fake_signing_callback):
    """Bug #15: cross-chain leg must carry a real BRAP quote so user can confirm route + output."""
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        VenueRow(
            venue="morpho_blue_market", chain_id=42161, asset_symbol="USDC",
            asset_address="0xARBUSDC", market_id="0xARBM", decimals=6,
            supply_apy=0.080, utilization=0.5, supply_cap_headroom_raw=None, tvl_usd=None,
        ),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    cross_chain_config = {**CONFIG, "chains": [8453, 42161]}
    fake_brap_quote = {
        "provider": "stargate", "input_amount": 100_000_000_000,
        "output_amount": 99_500_000_000, "from_amount_usd": 100_000.0,
        "to_amount_usd": 99_500.0,
    }
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main._quote_bridge", AsyncMock(return_value=(True, fake_brap_quote))),
    ):
        result = await rotator.action_quote_rotation(cross_chain_config)

    legs = result["plan"]["legs"]
    assert legs and legs[0]["is_cross_chain"]
    assert legs[0]["bridge_quote"] == fake_brap_quote
    assert legs[0]["bridge_from_token"] == USDC_ADDRESS
    assert legs[0]["bridge_to_token"] == "0xARBUSDC"


async def test_cross_chain_skipped_when_brap_quote_fails(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        VenueRow(
            venue="morpho_blue_market", chain_id=42161, asset_symbol="USDC",
            asset_address="0xARBUSDC", market_id="0xARBM", decimals=6,
            supply_apy=0.080, utilization=0.5, supply_cap_headroom_raw=None, tvl_usd=None,
        ),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    cross_chain_config = {**CONFIG, "chains": [8453, 42161]}
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main._quote_bridge", AsyncMock(return_value=(False, "brap api down"))),
    ):
        result = await rotator.action_quote_rotation(cross_chain_config)

    assert not result["plan"]["legs"]
    assert any("bridge quote failed" in s["skip_reason"] for s in result["plan"]["skipped"])


async def test_deposit_halts_when_gas_insufficient(fake_signing_callback):
    """Bug #16: deposit must precheck native gas on the target chain."""
    scan = [_make_row("aave_v3", USDC_ADDRESS, 0.040)]
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main._gas_balance_wei", AsyncMock(return_value=0)),
    ):
        result = await rotator.action_deposit(CONFIG, asset="USDC", human_amount=100.0)

    assert result["status"] == "halted"
    assert "gas" in result["reason"].lower()


async def test_withdraw_halts_when_gas_insufficient(fake_signing_callback):
    """Bug #16: withdraw must precheck native gas on every chain it touches."""
    positions = [_make_position("aave_v3", USDC_ADDRESS, 1_000 * 10**6)]
    with (
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main._gas_balance_wei", AsyncMock(return_value=0)),
    ):
        result = await rotator.action_withdraw(CONFIG, human_amount=None)

    assert result["status"] == "halted"
    assert "gas" in result["reason"].lower()


async def test_scan_surfaces_failures_in_response(fake_signing_callback):
    """Bug #17: scan with partial discovery must report failures, not just empty rows."""
    async def fake_scan_all(*, failure_log=None, **_):
        if failure_log is not None:
            failure_log.append({"venue": "aave_v3", "chain_id": 8453, "error": "RPC timeout"})
        return [_make_row("morpho_blue_market", "0xMARKET", 0.060)]

    with patch("main.scan_all", fake_scan_all):
        result = await rotator.action_scan(CONFIG)

    assert result["status"] == "partial"
    assert result["failure_count"] == 1
    assert result["failures"][0]["venue"] == "aave_v3"
    assert result["failures"][0]["error"] == "RPC timeout"


async def test_execute_bridge_uses_signed_brap_adapter():
    """Bug #18: _execute_bridge must call get_adapter(BRAPAdapter, wallet_label) so swap_from_quote has a signer."""
    fresh_quote = {"output_amount": 99_500_000_000, "provider": "stargate"}
    fake_brap = AsyncMock()
    fake_brap.best_quote = AsyncMock(return_value=(True, fresh_quote))
    fake_brap.swap_from_quote = AsyncMock(return_value=(True, {"tx_hash": "0xBRIDGE"}))
    fake_get_adapter = AsyncMock(return_value=fake_brap)

    fake_token = {"address": "0xT", "chain": {"id": 8453}, "id": "id", "decimals": 6}
    with (
        patch("main.get_adapter", fake_get_adapter),
        patch("main.TOKEN_CLIENT.get_token_details", AsyncMock(return_value=fake_token)),
    ):
        ok, payload = await rotator._execute_bridge(
            wallet_label="main",
            from_chain_id=8453, to_chain_id=42161,
            from_token_address="0xSRC", to_token_address="0xDST",
            raw_amount=100_000_000_000,
            sender="0xSENDER", slippage_bps=30,
            locked_quote={"output_amount": 99_500_000_000},
        )

    assert ok
    fake_get_adapter.assert_awaited_once()
    args, _kwargs = fake_get_adapter.call_args
    # First positional arg is the adapter class; second is the wallet label.
    assert args[1] == "main"
    fake_brap.swap_from_quote.assert_awaited_once()


async def test_execute_bridge_camel_case_locked_quote_blocks_degradation():
    """Bug #19: locked quote using `outputAmount` (camelCase) must still trigger the degradation guard."""
    locked_camel = {"outputAmount": 100_000_000_000}
    fresh_snake = {"output_amount": 50_000_000_000}  # 50% — well below 95% floor
    fake_brap = AsyncMock()
    fake_brap.best_quote = AsyncMock(return_value=(True, fresh_snake))
    fake_brap.swap_from_quote = AsyncMock()  # should NOT be called
    fake_token = {"address": "0xT", "chain": {"id": 8453}, "id": "id", "decimals": 6}

    with (
        patch("main.get_adapter", AsyncMock(return_value=fake_brap)),
        patch("main.TOKEN_CLIENT.get_token_details", AsyncMock(return_value=fake_token)),
    ):
        ok, payload = await rotator._execute_bridge(
            wallet_label="main",
            from_chain_id=8453, to_chain_id=42161,
            from_token_address="0xSRC", to_token_address="0xDST",
            raw_amount=100_000_000_000,
            sender="0xSENDER", slippage_bps=30,
            locked_quote=locked_camel,
        )

    assert not ok
    assert "materially worse" in payload["error"]
    assert payload["locked_output"] == 100_000_000_000
    assert payload["fresh_output"] == 50_000_000_000
    fake_brap.swap_from_quote.assert_not_awaited()


async def test_quote_output_amount_helper_handles_both_cases():
    from main import _quote_output_amount  # noqa: PLC0415
    assert _quote_output_amount({"output_amount": 123}) == 123
    assert _quote_output_amount({"outputAmount": 456}) == 456
    assert _quote_output_amount(None) == 0
    assert _quote_output_amount({}) == 0
    assert _quote_output_amount({"output_amount": "789"}) == 789  # str -> int


async def test_confirmed_update_reuses_scan_cache_but_refreshes_positions(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    fake_scan_all = AsyncMock(return_value=scan)
    fake_positions_all = AsyncMock(return_value=positions)

    with (
        patch("main.scan_all", fake_scan_all),
        patch("main.positions_all", fake_positions_all),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main._check_gas_budget", AsyncMock(return_value={"balances": {8453: 10**18}, "insufficient": []})),
        patch("main._execute_leg", AsyncMock(return_value={"withdraw": {"hash": "0xW"}, "deposit": {"hash": "0xD"}})),
    ):
        quote = await rotator.action_quote_rotation(CONFIG)
        update = await rotator.action_update(CONFIG, confirmed=True)

    assert quote["plan"]["legs"]
    assert update["status"] == "ok"
    fake_scan_all.assert_awaited_once()
    assert fake_positions_all.await_count == 2


async def test_gorlami_scenario_runs_base_usdc_deposit_withdraw(fake_signing_callback):
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_gorlami_fork(chain_id, **kwargs):
        captured["chain_id"] = chain_id
        captured["kwargs"] = kwargs
        yield None, {"fork_id": "fork-1", "rpc_url": "http://gorlami.local"}

    status_after_deposit = {"action": "status", "positions": [{"venue": "aave_v3"}]}
    status_after_withdraw = {"action": "status", "positions": []}

    with (
        patch("main.get_wallet_signing_callback", fake_signing_callback),
        patch("main.gorlami_fork", fake_gorlami_fork),
        patch("main.action_scan", AsyncMock(return_value={"action": "scan", "status": "ok"})),
        patch("main.action_deposit", AsyncMock(return_value={"action": "deposit", "status": "ok"})) as deposit,
        patch("main.action_withdraw", AsyncMock(return_value={"action": "withdraw", "status": "ok"})) as withdraw,
        patch("main.action_status", AsyncMock(side_effect=[status_after_deposit, status_after_withdraw])),
    ):
        result = await rotator.action_gorlami_scenario(CONFIG, asset="USDC", human_amount=10.0)

    assert result["action"] == "gorlami-scenario"
    assert result["status"] == "ok"
    assert result["fork"]["fork_id"] == "fork-1"
    assert result["scenario_config"] == {
        "chains": [8453],
        "assets": ["USDC"],
        "venues": ["aave_v3"],
    }
    assert captured["chain_id"] == 8453
    assert captured["kwargs"]["native_balances"][WALLET_ADDRESS] > 0
    assert captured["kwargs"]["erc20_balances"][0][1] == WALLET_ADDRESS
    deposit.assert_awaited_once()
    deposit_config = deposit.await_args.args[0]
    assert deposit_config["chains"] == [8453]
    assert deposit_config["venues"] == ["aave_v3"]
    withdraw.assert_awaited_once()


# ---------------------------------------------------------------------------
# Expanded token / chain universe (USDS, USDe, GHO; Polygon, Morpho on Arbitrum,
# Euler on HyperEVM)
# ---------------------------------------------------------------------------

import venues  # noqa: E402


def test_new_stables_allowed_with_18_decimals():
    assert {"USDS", "USDE", "GHO"} <= venues.ALLOWED_STABLES
    for asset in ("USDS", "USDE", "GHO", "DAI"):
        assert rotator._decimals_for(asset) == 18
    for asset in ("USDC", "USDT"):
        assert rotator._decimals_for(asset) == 6


def test_matches_asset_handles_mixed_case_and_excludes_wrappers():
    assert venues._matches_asset("USDe", venues.ALLOWED_STABLES)
    assert venues._matches_asset("usds", venues.ALLOWED_STABLES)
    assert venues._matches_asset("GHO", venues.ALLOWED_STABLES)
    assert not venues._matches_asset("sUSDe", venues.ALLOWED_STABLES)
    assert not venues._matches_asset("sDAI", venues.ALLOWED_STABLES)


def test_venue_chain_support_covers_expanded_networks():
    assert 137 in venues.VENUE_CHAIN_SUPPORT["aave_v3"]
    assert {137, 42161} <= venues.VENUE_CHAIN_SUPPORT["morpho_blue_market"]
    assert {137, 42161} <= venues.VENUE_CHAIN_SUPPORT["morpho_vault"]
    assert 999 in venues.VENUE_CHAIN_SUPPORT["euler_v2"]


async def test_deposit_rejects_unsupported_asset():
    with pytest.raises(ValueError, match="unsupported asset"):
        await rotator.action_deposit(CONFIG, asset="FRAX", human_amount=10.0)


# ---------------------------------------------------------------------------
# auto-rotate (unattended runner action)
# ---------------------------------------------------------------------------

def _ok_update_result(deposit_amount: int = 1_000 * 10**6, gas_topup_usd: float = 0.0):
    return {
        "action": "update",
        "status": "ok",
        "executed": [{
            "leg": {
                "asset_symbol": "USDC",
                "from": "aave_v3@8453",
                "to": "morpho_blue_market@8453",
                "human_amount": 1000.0,
                "decimals": 6,
                "apy_delta_bps": 120,
                "estimated_uplift_usd_30d": 9.86,
                "gas_topup_usd": gas_topup_usd,
            },
            "receipts": {"deposit": {"hash": "0xDEP"}, "deposit_amount": deposit_amount},
        }],
        "gas_check": {"insufficient": []},
    }


@pytest.fixture
def monitor_state(tmp_path):
    store: dict[str, dict] = {}

    def read(name, default=None):
        return store.get(name, dict(default or {}))

    def write(name, payload):
        store[name] = payload
        return tmp_path / f"{name}.json"

    with (
        patch("main.read_monitor_state", side_effect=read),
        patch("main.write_monitor_state", side_effect=write),
    ):
        yield store


async def test_auto_rotate_notifies_on_execution(monitor_state):
    notify = AsyncMock(return_value={"ok": True})
    with (
        patch("main.action_update", AsyncMock(return_value=_ok_update_result())),
        patch.object(rotator.NOTIFY_CLIENT, "notify", notify),
    ):
        result = await rotator.action_auto_rotate(CONFIG)

    assert result["action"] == "auto-rotate"
    assert result["status"] == "ok"
    assert result["notified"] is True
    notify.assert_awaited_once()
    title, body = notify.await_args.args
    assert "1 rotation leg" in title
    assert "USDC" in body and "aave_v3@8453" in body
    assert monitor_state["auto_rotate"]["last_status"] == "ok"


async def test_auto_rotate_dedupes_repeated_halts(monitor_state):
    halted = {"action": "update", "status": "halted", "reason": "insufficient native gas", "executed": []}
    notify = AsyncMock(return_value={"ok": True})
    with (
        patch("main.action_update", AsyncMock(return_value=halted)),
        patch.object(rotator.NOTIFY_CLIENT, "notify", notify),
    ):
        first = await rotator.action_auto_rotate(CONFIG)
        second = await rotator.action_auto_rotate(CONFIG)

    assert first["notified"] is True
    assert second["notified"] is False
    notify.assert_awaited_once()


async def test_auto_rotate_silent_on_noop(monitor_state):
    noop = {"action": "update", "status": "no-op", "reason": "no legs passed constraints", "skipped": []}
    notify = AsyncMock(return_value={"ok": True})
    with (
        patch("main.action_update", AsyncMock(return_value=noop)),
        patch.object(rotator.NOTIFY_CLIENT, "notify", notify),
    ):
        result = await rotator.action_auto_rotate(CONFIG)

    assert result["notified"] is False
    notify.assert_not_awaited()
    assert monitor_state["auto_rotate"]["last_status"] == "no-op"


async def test_auto_rotate_survives_notify_failure(monitor_state):
    notify = AsyncMock(side_effect=RuntimeError("notify backend down"))
    with (
        patch("main.action_update", AsyncMock(return_value=_ok_update_result())),
        patch.object(rotator.NOTIFY_CLIENT, "notify", notify),
    ):
        result = await rotator.action_auto_rotate(CONFIG)

    assert result["status"] == "ok"
    assert result["notified"] is False
    assert "notify backend down" in result["notify_error"]


# ---------------------------------------------------------------------------
# idle wallet balances → deposit legs
# ---------------------------------------------------------------------------

def _chain_balances(native=10**18, **tokens_by_chain):
    # tokens_by_chain: chain_id (as str like "c8453") -> {addr_lower: balance}
    out = {}
    for k, toks in tokens_by_chain.items():
        out[int(k[1:])] = {"native": native, "tokens": toks}
    return out


async def test_idle_wallet_balance_produces_deposit_leg(fake_signing_callback):
    scan = [_make_row("aave_v3", USDC_ADDRESS, 0.040)]
    # Large enough that 400 bps of uplift pays back gas within gas_amortization_days.
    balances = _chain_balances(c8453={USDC_ADDRESS.lower(): 100_000 * 10**6})

    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=[])),
        patch("main._fetch_chain_balances", AsyncMock(return_value=balances)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_quote_rotation(CONFIG)

    legs = result["plan"]["legs"]
    assert len(legs) == 1
    assert legs[0]["from"] == "wallet@8453"
    assert legs[0]["to"] == "aave_v3@8453"
    assert legs[0]["current_apy"] == 0.0
    assert legs[0]["raw_amount"] == 100_000 * 10**6
    assert legs[0]["from_asset_address"] == USDC_ADDRESS


async def test_idle_dust_balance_is_ignored(fake_signing_callback):
    scan = [_make_row("aave_v3", USDC_ADDRESS, 0.040)]
    balances = _chain_balances(c8453={USDC_ADDRESS.lower(): int(0.5 * 10**6)})  # below IDLE_DUST_HUMAN

    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=[])),
        patch("main._fetch_chain_balances", AsyncMock(return_value=balances)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_quote_rotation(CONFIG)

    assert result["plan"]["legs"] == []


async def test_idle_balance_read_failure_does_not_block_planning(fake_signing_callback):
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.040),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]

    # Multicall failed for the chain → empty balances; venue rotation must still plan.
    with (
        patch("main.scan_all", AsyncMock(return_value=scan)),
        patch("main.positions_all", AsyncMock(return_value=positions)),
        patch("main._fetch_chain_balances", AsyncMock(return_value={8453: {"native": None, "tokens": {}}})),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_quote_rotation(CONFIG)

    # The venue rotation still plans; the idle sweep is just absent.
    assert len(result["plan"]["legs"]) == 1
    assert result["plan"]["legs"][0]["from"] == "aave_v3@8453"


async def test_execute_leg_wallet_source_spends_live_balance_without_unlend():
    leg = rotator.RotationLeg(
        asset_symbol="USDC", from_venue="wallet", from_chain_id=8453,
        from_market_id="wallet", to_venue="aave_v3", to_chain_id=8453,
        to_market_id=USDC_ADDRESS, raw_amount=1_000 * 10**6, decimals=6,
        current_apy=0.0, target_apy=0.04, apy_delta_bps=400,
        estimated_uplift_usd_30d=3.29, estimated_gas_usd=4.0,
        estimated_bridge_fee_usd=0.0, payback_days=36.5, is_cross_chain=False,
        from_asset_address=USDC_ADDRESS,
    )
    lend_mock = AsyncMock(return_value=(True, {"tx": "0xdeadbeef"}))
    unlend_mock = AsyncMock()
    # Live balance below plan: spend what's actually there.
    balance_mock = AsyncMock(return_value=800 * 10**6)
    recheck = AsyncMock(return_value={"ok": True})
    with (
        patch("main.lend", lend_mock),
        patch("main.unlend", unlend_mock),
        patch("main.get_token_balance", balance_mock),
        patch("main._recheck_target_before_deposit", recheck),
    ):
        receipts = await rotator._execute_leg(
            leg, wallet_label="main", sender_address=WALLET_ADDRESS,
            slippage_bps=30, config=CONFIG,
        )

    unlend_mock.assert_not_awaited()
    lend_mock.assert_awaited_once()
    assert lend_mock.await_args.kwargs["raw_amount"] == 800 * 10**6
    assert receipts["idle_source"] == {
        "planned": 1_000 * 10**6, "available": 800 * 10**6, "spending": 800 * 10**6,
    }


# ---------------------------------------------------------------------------
# out-of-gas handling
# ---------------------------------------------------------------------------

def _gas_leg(chain_id: int, market: str) -> rotator.RotationLeg:
    return rotator.RotationLeg(
        asset_symbol="USDC", from_venue="aave_v3", from_chain_id=chain_id,
        from_market_id=market, to_venue="morpho_blue_market", to_chain_id=chain_id,
        to_market_id="0xTARGET", raw_amount=100 * 10**6, decimals=6,
        current_apy=0.03, target_apy=0.05, apy_delta_bps=200,
        estimated_uplift_usd_30d=1.64, estimated_gas_usd=4.0,
        estimated_bridge_fee_usd=0.0, payback_days=73.0, is_cross_chain=False,
        from_asset_address=USDC_ADDRESS,
    )


async def test_update_skips_gasless_legs_and_executes_the_rest(fake_signing_callback):
    plan = rotator.RotationPlan(legs=[_gas_leg(8453, "0xBASE"), _gas_leg(1, "0xMAINNET")])
    gas_check = {"insufficient": [{"chain_id": 1, "balance_wei": 0, "min_required_wei": 1}], "checked": []}
    with (
        patch("main._build_typed_plan", AsyncMock(return_value=(plan, [], []))),
        patch("main._check_gas_budget", AsyncMock(return_value=gas_check)),
        patch("main._execute_leg", AsyncMock(return_value={"deposit": "0xtx"})),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_update(CONFIG, confirmed=True, skip_gasless_legs=True)

    assert result["status"] == "ok"
    assert len(result["executed"]) == 1
    assert result["executed"][0]["leg"]["from"] == "aave_v3@8453"
    assert len(result["gas_skipped"]) == 1
    assert "insufficient native gas" in result["gas_skipped"][0]["skip_reason"]


async def test_update_halts_when_every_leg_is_gasless(fake_signing_callback):
    plan = rotator.RotationPlan(legs=[_gas_leg(1, "0xMAINNET")])
    gas_check = {"insufficient": [{"chain_id": 1, "balance_wei": 0, "min_required_wei": 1}], "checked": []}
    with (
        patch("main._build_typed_plan", AsyncMock(return_value=(plan, [], []))),
        patch("main._check_gas_budget", AsyncMock(return_value=gas_check)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_update(CONFIG, confirmed=True, skip_gasless_legs=True)

    assert result["status"] == "halted"
    assert "insufficient native gas blocked every planned leg" in result["reason"]
    assert len(result["gas_skipped"]) == 1


async def test_update_default_still_halts_on_gasless_chain(fake_signing_callback):
    plan = rotator.RotationPlan(legs=[_gas_leg(8453, "0xBASE"), _gas_leg(1, "0xMAINNET")])
    gas_check = {"insufficient": [{"chain_id": 1, "balance_wei": 0, "min_required_wei": 1}], "checked": []}
    with (
        patch("main._build_typed_plan", AsyncMock(return_value=(plan, [], []))),
        patch("main._check_gas_budget", AsyncMock(return_value=gas_check)),
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_update(CONFIG, confirmed=True)

    assert result["status"] == "halted"
    assert "insufficient native gas" in result["reason"]


# ---------------------------------------------------------------------------
# gas top-up planning + execution
# ---------------------------------------------------------------------------

from rotation import quote_rotation  # noqa: E402


def _remote_row(chain_id: int, apy: float) -> VenueRow:
    return VenueRow(
        venue="hyperlend", chain_id=chain_id, asset_symbol="USDC",
        asset_address="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        market_id="0xREMOTE", decimals=6,
        supply_apy=apy, utilization=0.5, supply_cap_headroom_raw=None,
        tvl_usd=None,
    )


def test_quote_rotation_counts_topup_cost_in_payback():
    # 30k position, +100bps cross-chain: $24.66/30d uplift. Base cost 12+6=18 →
    # payback ~21.9d (passes 30d gate). Adding a $15 destination top-up → 33 →
    # payback ~40.1d (fails). The top-up cost must flip the decision.
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.030),
        _remote_row(999, 0.040),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 30_000 * 10**6)]

    without_topup = quote_rotation(
        scan=scan, positions=positions,
        min_apy_delta_bps=50, gas_amortization_days=30,
        max_gas_usd_per_rotation=50, max_position_pct_per_venue=100,
    )
    assert len(without_topup.legs) == 1
    assert without_topup.legs[0].gas_topup_usd == 0.0

    with_topup = quote_rotation(
        scan=scan, positions=positions,
        min_apy_delta_bps=50, gas_amortization_days=30,
        max_gas_usd_per_rotation=50, max_position_pct_per_venue=100,
        gas_topup_usd_by_chain={999: 15.0},
    )
    assert with_topup.legs == []
    assert any("payback" in (leg.skip_reason or "") for leg in with_topup.skipped)

    # A big enough position amortizes the top-up and keeps the leg, cost included.
    big = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    big_plan = quote_rotation(
        scan=scan, positions=big,
        min_apy_delta_bps=50, gas_amortization_days=30,
        max_gas_usd_per_rotation=50, max_position_pct_per_venue=100,
        gas_topup_usd_by_chain={999: 15.0},
    )
    assert len(big_plan.legs) == 1
    assert big_plan.legs[0].gas_topup_usd == 15.0
    assert big_plan.legs[0].estimated_gas_usd == 12.0 + 15.0


def test_quote_rotation_skips_gasless_source_chains():
    scan = [
        _make_row("aave_v3", USDC_ADDRESS, 0.030),
        _make_row("morpho_blue_market", "0xMARKET", 0.060),
    ]
    positions = [_make_position("aave_v3", USDC_ADDRESS, 100_000 * 10**6)]
    plan = quote_rotation(
        scan=scan, positions=positions,
        min_apy_delta_bps=50, gas_amortization_days=30,
        max_gas_usd_per_rotation=25, max_position_pct_per_venue=100,
        gasless_source_chains={8453},
    )
    assert plan.legs == []
    assert len(plan.skipped) == 1
    assert "no native gas on source chain 8453" in plan.skipped[0].skip_reason


async def test_update_executes_topup_covered_legs(fake_signing_callback):
    leg = _gas_leg(8453, "0xBASE")
    leg.to_chain_id = 999
    leg.is_cross_chain = True
    leg.gas_topup_usd = 15.0
    leg.gas_topup = {"chain_id": 999, "stable_raw": 15 * 10**6, "native_token": {"address": "0xEee"}, "quote": {}}
    plan = rotator.RotationPlan(legs=[leg])
    gas_check = {"insufficient": [{"chain_id": 999, "balance_wei": 0, "min_required_wei": 1}], "checked": []}
    with (
        patch("main._build_typed_plan", AsyncMock(return_value=(plan, [], []))),
        patch("main._check_gas_budget", AsyncMock(return_value=gas_check)),
        patch("main._execute_leg", AsyncMock(return_value={"deposit": "0xtx"})) as exec_leg,
        patch("main.get_wallet_signing_callback", fake_signing_callback),
    ):
        result = await rotator.action_update(CONFIG, confirmed=True, skip_gasless_legs=True)

    assert result["status"] == "ok"
    assert len(result["executed"]) == 1
    assert "gas_skipped" not in result
    exec_leg.assert_awaited_once()


async def test_execute_leg_runs_topup_before_main_bridge():
    raw = 1_000 * 10**6
    stable_raw = 15 * 10**6
    leg = rotator.RotationLeg(
        asset_symbol="USDC", from_venue="aave_v3", from_chain_id=8453,
        from_market_id=USDC_ADDRESS, to_venue="hyperlend", to_chain_id=999,
        to_market_id="0xREMOTE", raw_amount=raw, decimals=6,
        current_apy=0.03, target_apy=0.06, apy_delta_bps=300,
        estimated_uplift_usd_30d=24.66, estimated_gas_usd=27.0,
        estimated_bridge_fee_usd=6.0, payback_days=20.0, is_cross_chain=True,
        from_asset_address=USDC_ADDRESS,
        bridge_from_token=USDC_ADDRESS, bridge_to_token="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        bridge_quote={"output_amount": str(raw - stable_raw)},
        gas_topup_usd=15.0,
        gas_topup={
            "chain_id": 999, "needed_wei": 2 * 10**15, "stable_raw": stable_raw,
            "native_token": {"address": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE", "decimals": 18},
            "quote": {"output_amount": str(3 * 10**15)},
        },
    )
    bridge_calls = []

    async def fake_bridge(**kwargs):
        bridge_calls.append(kwargs)
        return True, {"bridge_result": "0xtx"}

    # Balance reads: src before/after withdraw, then dest stable before/after main bridge.
    balances = AsyncMock(side_effect=[0, raw, 0, raw - stable_raw])
    with (
        patch("main.unlend", AsyncMock(return_value=(True, {"hash": "0xW"}))),
        patch("main.lend", AsyncMock(return_value=(True, {"hash": "0xD"}))) as lend_mock,
        patch("main.get_token_balance", balances),
        patch("main._execute_bridge", side_effect=fake_bridge),
        patch("main._gas_balance_wei", AsyncMock(return_value=2_000_000_000_000_000)),
        patch("main._recheck_target_before_deposit", AsyncMock(return_value={"ok": True})),
    ):
        receipts = await rotator._execute_leg(
            leg, wallet_label="main", sender_address=WALLET_ADDRESS,
            slippage_bps=30, config=CONFIG,
        )

    assert len(bridge_calls) == 2
    topup_call, main_call = bridge_calls
    assert topup_call["to_token_address"] == "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
    assert topup_call["raw_amount"] == stable_raw
    assert main_call["to_token_address"] == "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    assert main_call["raw_amount"] == raw - stable_raw
    assert receipts["gas_topup"]["stable_raw_spent"] == stable_raw
    lend_mock.assert_awaited_once()
    assert lend_mock.await_args.kwargs["raw_amount"] == raw - stable_raw


# ---------------------------------------------------------------------------
# wallet resolution: prefer the connected (remote/session) wallet
# ---------------------------------------------------------------------------

def _w(label, kind="local"):
    return {"label": label, "address": "0x" + label.replace("-", "")[:1].ljust(40, "0"), "type": kind}


async def test_resolve_prefers_connected_wallet_over_local_main():
    # config pins "main" (the shipped/dev default) but a session wallet is connected.
    wallets = [_w("main", "local"), _w("strat-1", "local"), _w("session-abc", "remote")]
    with patch("main.load_wallets", AsyncMock(return_value=wallets)):
        label = await rotator._resolve_wallet_label({"wallet": "main"})
    assert label == "session-abc"


async def test_resolve_blank_config_uses_sole_connected_wallet():
    wallets = [_w("main", "local"), _w("strat-1", "local"), _w("session-abc", "remote")]
    with patch("main.load_wallets", AsyncMock(return_value=wallets)):
        label = await rotator._resolve_wallet_label({"wallet": None})
    assert label == "session-abc"


async def test_resolve_explicit_pin_to_a_connected_wallet_is_honored():
    # Two connected wallets, config names one of them → honor the pin.
    wallets = [_w("session-a", "remote"), _w("session-b", "remote"), _w("main", "local")]
    with patch("main.load_wallets", AsyncMock(return_value=wallets)):
        label = await rotator._resolve_wallet_label({"wallet": "session-b"})
    assert label == "session-b"


async def test_resolve_local_dev_falls_back_to_configured_when_no_remote():
    # Pure local dev (no remote wallets): explicit "main" still works among many.
    wallets = [_w("main", "local"), _w("strat-1", "local"), _w("strat-2", "local")]
    with patch("main.load_wallets", AsyncMock(return_value=wallets)):
        label = await rotator._resolve_wallet_label({"wallet": "main"})
    assert label == "main"


async def test_resolve_ambiguous_no_remote_blank_config_raises():
    wallets = [_w("main", "local"), _w("strat-1", "local")]
    with patch("main.load_wallets", AsyncMock(return_value=wallets)):
        with pytest.raises(SystemExit):
            await rotator._resolve_wallet_label({"wallet": None})


# ---------------------------------------------------------------------------
# 429 handling: rate-limit detection, bounded+retried reads, scan_all retry/cap
# ---------------------------------------------------------------------------

import asyncio as _asyncio  # noqa: E402


def test_is_rate_limit_error_detects_429_variants():
    assert venues.is_rate_limit_error(RuntimeError("429, message='Too Many Requests'"))
    assert venues.is_rate_limit_error(RuntimeError("aave_v3 get_all_markets failed: 429"))
    assert venues.is_rate_limit_error(Exception("rate limit exceeded"))
    assert not venues.is_rate_limit_error(RuntimeError("boom: revert"))
    assert not venues.is_rate_limit_error(ValueError("no liquidity"))


async def test_run_bounded_retries_rate_limit_then_succeeds():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429, message='Too Many Requests'")
        return "ok"

    sem = _asyncio.Semaphore(2)
    with patch("venues.asyncio.sleep", AsyncMock()):  # don't actually wait
        result = await venues.run_bounded(sem, flaky)
    assert result == "ok"
    assert calls["n"] == 3


async def test_run_bounded_does_not_retry_non_rate_limit():
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("hard revert")

    sem = _asyncio.Semaphore(1)
    with pytest.raises(RuntimeError, match="hard revert"):
        await venues.run_bounded(sem, boom)
    assert calls["n"] == 1  # no retry


async def test_scan_all_retries_transient_429(monkeypatch):
    # One venue/chain 429s once then succeeds; scan_all should return its rows.
    attempts = {"n": 0}
    usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    async def flaky_scan(adapter, chain_id, allowed):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("aave_v3 get_all_markets failed on chain 8453: 429, message='Too Many Requests'")
        return [VenueRow(venue="aave_v3", chain_id=chain_id, asset_symbol="USDC",
                         asset_address=usdc, market_id=usdc, decimals=6, supply_apy=0.04,
                         utilization=0.5, supply_cap_headroom_raw=None, tvl_usd=None)]

    monkeypatch.setitem(venues._SCAN_FNS, "aave_v3", flaky_scan)
    with (
        patch("venues.get_read_adapter", AsyncMock(return_value=object())),
        patch("venues.asyncio.sleep", AsyncMock()),
    ):
        rows = await venues.scan_all(["aave_v3"], [8453], ["USDC"], strict=True)
    assert len(rows) == 1
    assert attempts["n"] == 2  # failed once, retried, succeeded


async def test_scan_all_caps_concurrency(monkeypatch):
    # Track peak concurrent scan-fn executions; must not exceed the cap.
    state = {"cur": 0, "peak": 0}

    async def slow_scan(adapter, chain_id, allowed):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        await _asyncio.sleep(0)  # yield so others can start
        state["cur"] -= 1
        return []

    monkeypatch.setitem(venues._SCAN_FNS, "aave_v3", slow_scan)
    with patch("venues.get_read_adapter", AsyncMock(return_value=object())):
        await venues.scan_all(["aave_v3"], [1, 137, 8453, 42161], ["USDC"], strict=True, concurrency=2)
    assert state["peak"] <= 2


# ---------------------------------------------------------------------------
# request-volume redesign: euler de-enumeration, batched balances, gas reuse
# ---------------------------------------------------------------------------

async def test_euler_positions_use_user_state_without_enumeration():
    from venues import _euler_positions  # noqa: PLC0415

    vault = "0x797dd80692c3b2dAdaBCe8e30C07fDE5307D48a9"
    adapter = AsyncMock()
    adapter.get_full_user_state = AsyncMock(return_value=(True, {"positions": [
        {"vault": vault, "underlying": USDC_ADDRESS, "assets": 1_000 * 10**6},
    ]}))
    discovered = {vault: {"vault": vault, "apy": 0.05, "symbol": "USDC",
                          "is_frozen": False, "is_paused": False, "tvl_usd": 1e6}}

    @asynccontextmanager
    async def fake_w3(chain_id):
        w = AsyncMock()
        w.to_checksum_address = lambda a: a
        yield w

    with (
        patch("venues._euler_stable_vaults", AsyncMock(return_value=discovered)),
        patch("venues.web3_from_chain_id", fake_w3),
    ):
        positions = await _euler_positions(adapter, 8453, {"USDC"}, "0xACC")

    adapter.get_all_markets.assert_not_called()  # never enumerate all vaults
    # held vault is in the discovered set → no per-vault info read needed either
    adapter.get_vault_info_full.assert_not_called()
    assert len(positions) == 1
    assert positions[0].market_id == vault
    assert positions[0].asset_symbol == "USDC"
    assert positions[0].supply_raw == 1_000 * 10**6
    assert positions[0].decimals == 6


def test_idle_positions_from_balances_is_pure():
    scan = [_make_row("aave_v3", USDC_ADDRESS, 0.04)]
    cb = {8453: {"native": 10**18, "tokens": {USDC_ADDRESS.lower(): 100_000 * 10**6}}}
    out = rotator._idle_positions_from_balances(scan, cb)
    assert len(out) == 1
    assert out[0].asset_symbol == "USDC"
    assert out[0].supply_raw == 100_000 * 10**6
    # dust is ignored
    dust = {8453: {"native": 10**18, "tokens": {USDC_ADDRESS.lower(): int(0.5 * 10**6)}}}
    assert rotator._idle_positions_from_balances(scan, dust) == []
    # missing chain → no idle
    assert rotator._idle_positions_from_balances(scan, {}) == []


async def test_gas_topup_state_reuses_multicall_native_balance():
    # chain 8453 native came from the multicall; chain 1 didn't → one direct fallback read.
    chain_balances = {8453: {"native": 10**18, "tokens": {}}, 1: {"native": None, "tokens": {}}}
    direct = AsyncMock(return_value=10**14)  # below the gas floor → chain 1 is starved
    with patch("main._gas_balance_wei", direct):
        state = await rotator._gas_topup_state(
            [8453, 1], rotator.DEFAULT_MIN_GAS_WEI, chain_balances, address="0xACC",
        )
    assert state["balances"][8453] == 10**18
    assert state["balances"][1] == 10**14
    direct.assert_awaited_once()  # only the chain the multicall couldn't read
    assert 1 in state["starved"] and 8453 not in state["starved"]


async def test_erc4626_share_positions_batches_balance_then_convert():
    import venues  # noqa: PLC0415

    from wayfinder_paths.adapters.multicall_adapter.adapter import (
        MulticallResult,  # noqa: PLC0415
    )

    rows = [
        _make_row("morpho_vault", "0x" + "a" * 40, 0.05),
        _make_row("morpho_vault", "0x" + "b" * 40, 0.06),
    ]
    u256 = lambda n: n.to_bytes(32, "big")  # noqa: E731
    agg_calls: list = []

    class FakeMC:
        decode_uint256 = staticmethod(lambda d: int.from_bytes(d, "big"))

        def __init__(self, **kw):
            self.web3 = kw["web3"]

        def encode_erc20_balance(self, token, acct):
            return ("bal", token)

        def build_call(self, addr, data):
            return ("conv", addr)

        async def aggregate(self, calls):
            agg_calls.append(list(calls))
            if len(agg_calls) == 1:  # balanceOf batch: vault A=0, B=500 shares
                return MulticallResult(block_number=1, return_data=(u256(0), u256(500)))
            return MulticallResult(block_number=1, return_data=(u256(1000),))  # convertToAssets(B)

    class FakeEth:
        def contract(self, address=None, abi=None):
            c = AsyncMock()
            c.encode_abi = lambda fn, args: b""
            return c

    class FakeWeb3:
        eth = FakeEth()

        def to_checksum_address(self, a):
            return a

    @asynccontextmanager
    async def fake_w3(chain_id):
        yield FakeWeb3()

    with (
        patch("venues.MulticallAdapter", FakeMC),
        patch("venues.web3_from_chain_id", fake_w3),
    ):
        out = await venues._erc4626_share_positions(8453, "0xACC", rows, venue="morpho_vault")

    assert len(agg_calls) == 2          # one balanceOf batch + one convertToAssets batch (not 2N reads)
    assert len(agg_calls[0]) == 2       # both vaults in a single balanceOf aggregate
    assert len(agg_calls[1]) == 1       # only the held vault converted
    assert len(out) == 1
    assert out[0].market_id == "0x" + "b" * 40
    assert out[0].supply_raw == 1000


async def test_moonwell_positions_batched_no_get_pos():
    from venues import _moonwell_positions  # noqa: PLC0415

    from wayfinder_paths.adapters.multicall_adapter.adapter import (
        MulticallResult,  # noqa: PLC0415
    )

    adapter = AsyncMock()
    adapter.get_all_markets = AsyncMock(return_value=(True, [
        {"symbol": "mUSDC", "mtoken": "0x" + "c" * 40, "underlying": USDC_ADDRESS},
        {"symbol": "mWETH", "mtoken": "0x" + "e" * 40, "underlying": "0x" + "f" * 40},  # non-stable → skipped
    ]))
    u256 = lambda n: n.to_bytes(32, "big")  # noqa: E731

    class FakeMC:
        decode_uint256 = staticmethod(lambda d: int.from_bytes(d, "big"))

        def __init__(self, **kw):
            pass

        def encode_erc20_balance(self, token, acct):
            return ("bal", token)

        def build_call(self, addr, data):
            return ("er", addr)

        async def aggregate(self, calls):
            # one stable mToken → [balanceOf=200 mtokens, exchangeRate=2e18] → underlying 400
            return MulticallResult(block_number=1, return_data=(u256(200), u256(2 * 10**18)))

    class FakeEth:
        def contract(self, address=None, abi=None):
            c = AsyncMock()
            c.encode_abi = lambda fn, args: b""
            return c

    class FakeWeb3:
        eth = FakeEth()

        def to_checksum_address(self, a):
            return a

    @asynccontextmanager
    async def fake_w3(chain_id):
        yield FakeWeb3()

    with (patch("venues.MulticallAdapter", FakeMC), patch("venues.web3_from_chain_id", fake_w3)):
        positions = await _moonwell_positions(adapter, 8453, {"USDC"}, "0xACC")

    adapter.get_pos.assert_not_called()  # batched, not per-mToken get_pos
    assert len(positions) == 1
    assert positions[0].asset_symbol == "USDC"
    assert positions[0].market_id == "0x" + "c" * 40
    assert positions[0].supply_raw == 400  # 200 * 2e18 // 1e18


def test_leg_summary_reports_actual_deposited_not_planned():
    # Top-up leg: planned 1000, but only 985 reached the venue after the gas slice.
    entry = {
        "leg": {
            "asset_symbol": "USDC", "from": "aave_v3@42161", "to": "hyperlend@999",
            "human_amount": 1000.0, "decimals": 6, "apy_delta_bps": 150,
            "estimated_uplift_usd_30d": 12.3, "gas_topup_usd": 15.0,
        },
        "receipts": {"deposit_amount": 985 * 10**6},
    }
    line = rotator._leg_summary_line(entry)
    assert "985.00 deposited" in line          # actual, not the planned 1,000
    assert "1,000.00:" not in line
    assert "gas top-up" in line


def test_leg_summary_falls_back_to_planned_without_receipt():
    entry = {"leg": {
        "asset_symbol": "USDC", "from": "aave_v3@8453", "to": "morpho_blue_market@8453",
        "human_amount": 1000.0, "decimals": 6, "apy_delta_bps": 120,
        "estimated_uplift_usd_30d": 9.86,
    }}  # errored leg: no receipts
    line = rotator._leg_summary_line(entry)
    assert "1,000.00:" in line
