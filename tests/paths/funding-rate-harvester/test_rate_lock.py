"""Boros lock decision-logic tests for the funding-rate-harvester path.

No network. Run with:
    poetry run pytest tests/paths/funding-rate-harvester -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PATH_DIR = Path(__file__).resolve().parents[3] / "paths/funding-rate-harvester"
sys.path.insert(0, str(PATH_DIR / "scripts"))

from rate_lock import (  # noqa: E402
    HYPE_TOKEN_ID,
    USDT_TOKEN_ID,
    BorosRateLock,
    lock_size_yu,
    required_lock_collateral,
    select_lock_market,
)
from scoring import lock_decision  # noqa: E402


def _quote(market_id: int, tenor_days: float, oi: float = 1_000_000.0) -> dict:
    return {"market_id": market_id, "tenor_days": tenor_days, "notional_oi": oi}


# ---------------------------------------------------------------------------
# Tenor / market selection
# ---------------------------------------------------------------------------

def test_select_lock_market_prefers_shortest_eligible_tenor():
    quotes = [_quote(1, 60.0), _quote(2, 21.0), _quote(3, 9.0), _quote(4, 2.0)]
    picked = select_lock_market(quotes, min_tenor_days=5.0, max_tenor_days=45.0)
    assert picked["market_id"] == 3  # 2d too short, 60d too long, 9d < 21d


def test_select_lock_market_target_tenor_picks_closest():
    quotes = [_quote(1, 9.0), _quote(2, 21.0), _quote(3, 35.0)]
    picked = select_lock_market(quotes, target_tenor_days=25.0)
    assert picked["market_id"] == 2


def test_select_lock_market_filters_thin_oi():
    quotes = [_quote(1, 9.0, oi=1_000.0), _quote(2, 21.0)]
    picked = select_lock_market(quotes)
    assert picked["market_id"] == 2


def test_select_lock_market_none_when_no_eligible():
    assert select_lock_market([_quote(1, 90.0)]) is None
    assert select_lock_market([]) is None


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def test_lock_size_yu_usdt_collateral_is_notional():
    # USDT-collateral markets: 1 YU ≈ $1 → size to the short's notional.
    assert lock_size_yu(
        USDT_TOKEN_ID, short_notional_usd=5_000.0, short_size_units=100.0
    ) == 5_000.0


def test_lock_size_yu_hype_collateral_is_units():
    # HYPE-collateral markets: 1 YU = 1 HYPE → size to the short's units.
    assert lock_size_yu(
        HYPE_TOKEN_ID, short_notional_usd=5_000.0, short_size_units=100.0
    ) == 100.0


def test_required_lock_collateral_never_one_to_one():
    # Margin scales with rate × buffer, NOT size 1:1 (Boros gotcha).
    collateral = required_lock_collateral(5_000.0, 0.04)
    assert collateral == pytest.approx(5_000.0 * 0.04 / 0.6)
    assert collateral < 5_000.0


def test_required_lock_collateral_applies_rate_floor():
    # Near-zero implied rate must not produce near-zero margin.
    assert required_lock_collateral(1_000.0, 0.001) == pytest.approx(1_000.0 * 0.02 / 0.6)
    # Negative rates margin on magnitude.
    assert required_lock_collateral(1_000.0, -0.05) == pytest.approx(1_000.0 * 0.05 / 0.6)


# ---------------------------------------------------------------------------
# Open / roll / unwind decision (scoring.lock_decision drives it)
# ---------------------------------------------------------------------------

def test_lock_lifecycle_open_hold_unwind():
    # Floating rich vs fixed → open
    assert lock_decision(0.20, 0.15, premium_threshold_bps=200, locked=False).action == "open"
    # Premium narrows but stays positive → hold (no churn)
    assert lock_decision(0.16, 0.15, premium_threshold_bps=200, locked=True).action == "hold"
    # Premium inverts → unwind
    assert lock_decision(0.10, 0.15, premium_threshold_bps=200, locked=True).action == "unwind"
    # Not locked and premium thin → nothing to do
    assert lock_decision(0.16, 0.15, premium_threshold_bps=200, locked=False).action == "none"


# ---------------------------------------------------------------------------
# quote_lock uses adapter APRs as-is (they are already annualized)
# ---------------------------------------------------------------------------

class _FakeTenor:
    def __init__(self, market_id: int, tenor_days: float, notional_oi: float) -> None:
        self.market_id = market_id
        self.tenor_days = tenor_days
        self.maturity = 0
        self.mid_apr = 0.09
        self.notional_oi = notional_oi


class _FakeMarket:
    market_id = 164
    maturity_ts = 9_999_999_999.0  # far future; remaining_days must not affect fixed_apr
    best_bid_apr = 0.089
    mid_apr = 0.09
    collateral_token_id = USDT_TOKEN_ID


class _FakeBorosAdapter:
    async def list_tenor_quotes(self, underlying_symbol: str, platform: str):
        return True, [_FakeTenor(164, 23.5, 700_000.0)]

    async def quote_market_by_id(self, market_id: int):
        return True, _FakeMarket()


def test_quote_lock_reports_annualized_fixed_apr_without_reannualizing():
    # Adapter `*_apr` fields are already annualized implied APRs. quote_lock must
    # report the executable bid as-is, NOT re-annualize it by tenor (bid/days*365),
    # which would inflate a 8.9% fixed rate to ~138% for a 23-day tenor.
    lock = BorosRateLock(_FakeBorosAdapter())
    ok, quote = asyncio.run(
        lock.quote_lock("HYPE", short_notional_usd=1_000.0, short_size_units=0.33)
    )
    assert ok
    assert quote.fixed_apr == pytest.approx(0.089)
