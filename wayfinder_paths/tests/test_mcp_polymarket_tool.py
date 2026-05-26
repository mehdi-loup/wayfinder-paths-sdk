from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.core.constants.polymarket import derive_deposit_wallet
from wayfinder_paths.mcp.tools.polymarket import (
    polymarket_get_state,
    polymarket_place_market_order,
    polymarket_read,
)

_FIND_WALLET = "wayfinder_paths.mcp.utils.find_wallet_by_label"
_GET_SIGN_CB = "wayfinder_paths.mcp.tools.polymarket.get_wallet_signing_callback"
_GET_HASH_CB = "wayfinder_paths.mcp.tools.polymarket.get_wallet_sign_hash_callback"
_GET_TYPED_CB = (
    "wayfinder_paths.mcp.tools.polymarket.get_wallet_sign_typed_data_callback"
)

_ADDR = "0x000000000000000000000000000000000000dEaD"
_WALLET = {"address": _ADDR}
_SIGN_CB = AsyncMock(return_value=b"\x00" * 65)
_HASH_CB = AsyncMock(return_value="0x" + "00" * 65)
_TYPED_CB = AsyncMock(return_value="0x" + "00" * 65)


@pytest.mark.asyncio
async def test_polymarket_get_state_uses_adapter_full_state():
    full_state = AsyncMock(return_value=(True, {"protocol": "polymarket_read"}))
    with (
        patch(_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_GET_SIGN_CB, AsyncMock(return_value=(_SIGN_CB, _ADDR))),
        patch(_GET_HASH_CB, AsyncMock(return_value=(_HASH_CB, _ADDR))),
        patch(_GET_TYPED_CB, AsyncMock(return_value=(_TYPED_CB, _ADDR))),
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_full_user_state",
            new=full_state,
        ),
    ):
        out = await polymarket_get_state(wallet_label="main")
        assert out["ok"] is True
        assert out["result"]["ok"] is True
        assert out["result"]["state"]["protocol"] == "polymarket_read"
        assert out["result"]["account"] == derive_deposit_wallet(_ADDR)
        assert full_state.await_args.kwargs["account"] == derive_deposit_wallet(_ADDR)


@pytest.mark.asyncio
async def test_polymarket_search_uses_adapter_search():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=AsyncMock(
                return_value=(
                    True,
                    [
                        {
                            "slug": "m1",
                            "eventSlug": "e1",
                            "question": "Will BTC rally?",
                            "yesPrice": 0.42,
                            "noPrice": 0.58,
                            "yesTokenId": "tok_yes",
                            "noTokenId": "tok_no",
                            "conditionId": "0xabc",
                            "liquidity": 1234.0,
                            "volume24h": 5678.0,
                            "resolvesAt": "2026-06-01T00:00:00Z",
                        }
                    ],
                )
            ),
        ),
    ):
        out = await polymarket_read("search", query="bitcoin", limit=1)
        assert out["ok"] is True
        assert out["result"]["action"] == "search"
        assert out["result"]["summaryMode"] is True
        assert "markets" not in out["result"]
        candidate = out["result"]["candidates"][0]
        assert candidate["slug"] == "m1"
        assert candidate["outcomes"][0] == {
            "label": "Yes",
            "price": 0.42,
            "tokenId": "tok_yes",
        }
        assert candidate["outcomes"][1]["tokenId"] == "tok_no"
        assert out["result"]["truncation"]["rawAvailableWithSummaryFalse"] is True


@pytest.mark.asyncio
async def test_polymarket_search_summary_false_preserves_raw_markets():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.search_markets",
            new=AsyncMock(return_value=(True, [{"slug": "m1", "raw": True}])),
        ),
    ):
        out = await polymarket_read("search", query="bitcoin", limit=1, summary=False)
        assert out["ok"] is True
        assert "summaryMode" not in out["result"]
        assert out["result"]["markets"] == [{"slug": "m1", "raw": True}]


@pytest.mark.asyncio
async def test_polymarket_get_event_summary_returns_compact_candidates():
    event = {
        "slug": "world-cup-winner",
        "title": "World Cup winner",
        "description": "Pick the tournament winner.",
        "markets": [
            {
                "slug": "closed-world-cup-market",
                "question": "Closed market",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [1.0, 0.0],
                "clobTokenIds": ["tok_closed_yes", "tok_closed_no"],
                "enableOrderBook": True,
                "acceptingOrders": False,
                "active": True,
                "closed": True,
                "liquidityNum": "999999.99",
                "volume24hr": "999999.99",
            },
            {
                "slug": "world-cup-winner-2026",
                "question": "Who will win the 2026 World Cup?",
                "outcomes": ["Brazil", "France", "Spain"],
                "outcomePrices": [0.25, 0.2, 0.12],
                "clobTokenIds": ["tok_brazil", "tok_france", "tok_spain"],
                "conditionId": "0xcond1",
                "enableOrderBook": True,
                "acceptingOrders": True,
                "active": True,
                "closed": False,
                "liquidityNum": "12345.67",
                "volume24hr": "987.65",
                "endDate": "2026-07-19T00:00:00Z",
                "rawLargeField": {"should": "not appear"},
            },
            {
                "slug": "other-world-cup-market",
                "question": "Another market",
                "outcomes": ["Yes", "No"],
                "outcomePrices": [0.5, 0.5],
                "clobTokenIds": ["tok_yes", "tok_no"],
            },
        ],
    }
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_event_by_slug",
            new=AsyncMock(return_value=(True, event)),
        ),
    ):
        out = await polymarket_read(
            "get_event", event_slug="world-cup-winner", candidate_limit=1
        )

    assert out["ok"] is True
    result = out["result"]
    assert result["summaryMode"] is True
    assert result["event"] == {
        "slug": "world-cup-winner",
        "title": "World Cup winner",
        "description": "Pick the tournament winner.",
        "startDate": None,
        "endDate": None,
        "active": None,
        "closed": None,
    }
    assert "markets" not in result["event"]
    assert result["truncation"] == {
        "totalAvailable": 3,
        "returnedCandidates": 1,
        "truncated": True,
        "rawAvailableWithSummaryFalse": True,
    }
    candidate = result["candidates"][0]
    assert candidate["slug"] == "world-cup-winner-2026"
    assert candidate["eventSlug"] == "world-cup-winner"
    assert candidate["outcomes"] == [
        {"label": "Brazil", "price": 0.25, "tokenId": "tok_brazil"},
        {"label": "France", "price": 0.2, "tokenId": "tok_france"},
        {"label": "Spain", "price": 0.12, "tokenId": "tok_spain"},
    ]
    assert candidate["liquidity"] == 12345.67
    assert candidate["tradable"] is True
    assert "rawLargeField" not in candidate


@pytest.mark.asyncio
async def test_polymarket_get_event_summary_false_preserves_raw_event():
    event = {"slug": "event", "markets": [{"slug": "m1", "raw": True}]}
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_event_by_slug",
            new=AsyncMock(return_value=(True, event)),
        ),
    ):
        out = await polymarket_read("get_event", event_slug="event", summary=False)
    assert out["ok"] is True
    assert out["result"]["event"] == event


@pytest.mark.asyncio
async def test_polymarket_get_market_summary_and_raw_modes():
    market = {
        "slug": "market",
        "question": "Will it happen?",
        "description": "Resolution text " * 80,
        "resolutionSource": "https://example.com/rules",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [0.4, 0.6],
        "clobTokenIds": ["tok_yes", "tok_no"],
        "conditionId": "0xcond",
        "raw": {"nested": True},
    }
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.get_market_by_slug",
            new=AsyncMock(return_value=(True, market)),
        ),
    ):
        summary = await polymarket_read("get_market", market_slug="market")
        raw = await polymarket_read("get_market", market_slug="market", summary=False)

    assert summary["ok"] is True
    assert summary["result"]["summaryMode"] is True
    assert summary["result"]["market"]["outcomes"][0]["tokenId"] == "tok_yes"
    assert len(summary["result"]["market"]["description"]) < len(market["description"])
    assert "raw" not in summary["result"]["market"]
    assert raw["ok"] is True
    assert raw["result"]["market"] == market


@pytest.mark.asyncio
async def test_polymarket_quote_uses_adapter_quote_by_token_id():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.quote_market_order",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "token_id": "tok_yes",
                        "side": "BUY",
                        "average_price": 0.42,
                        "shares": 10.0,
                    },
                )
            ),
        ),
    ):
        out = await polymarket_read(
            "quote",
            token_id="tok_yes",
            side="BUY",
            amount_collateral=4.2,
        )
        assert out["ok"] is True
        assert out["result"]["action"] == "quote"
        assert out["result"]["token_id"] == "tok_yes"
        assert out["result"]["quote"]["average_price"] == 0.42


@pytest.mark.asyncio
async def test_polymarket_quote_uses_adapter_quote_by_market_slug():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.quote_prediction",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "token_id": "tok_yes",
                        "side": "SELL",
                        "average_price": 0.61,
                        "shares": 3.0,
                    },
                )
            ),
        ),
    ):
        out = await polymarket_read(
            "quote",
            market_slug="market-slug",
            outcome="YES",
            side="SELL",
            shares=3.0,
        )
        assert out["ok"] is True
        assert out["result"]["action"] == "quote"
        assert out["result"]["side"] == "SELL"
        assert out["result"]["quote"]["average_price"] == 0.61


@pytest.mark.asyncio
async def test_polymarket_quote_buy_requires_amount_collateral():
    with patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}):
        out = await polymarket_read("quote", token_id="tok_yes", side="BUY")
        assert out["ok"] is False
        assert out["error"]["code"] == "error"


@pytest.mark.asyncio
async def test_polymarket_quote_sell_requires_shares():
    with patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}):
        out = await polymarket_read("quote", token_id="tok_yes", side="SELL")
        assert out["ok"] is False
        assert out["error"]["code"] == "error"


@pytest.mark.asyncio
async def test_polymarket_quote_surfaces_adapter_failure():
    with (
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.quote_market_order",
            new=AsyncMock(return_value=(False, "order book unavailable")),
        ),
    ):
        out = await polymarket_read(
            "quote",
            token_id="tok_yes",
            side="BUY",
            amount_collateral=4.2,
        )
        assert out["ok"] is False
        assert out["error"]["code"] == "error"
        assert "order book unavailable" in out["error"]["message"]


@pytest.mark.asyncio
async def test_polymarket_place_market_order(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))

    with (
        patch(_FIND_WALLET, AsyncMock(return_value=_WALLET)),
        patch(_GET_SIGN_CB, AsyncMock(return_value=(_SIGN_CB, _ADDR))),
        patch(_GET_HASH_CB, AsyncMock(return_value=(_HASH_CB, _ADDR))),
        patch(_GET_TYPED_CB, AsyncMock(return_value=(_TYPED_CB, _ADDR))),
        patch("wayfinder_paths.mcp.tools.polymarket.CONFIG", {}),
        patch(
            "wayfinder_paths.mcp.tools.polymarket.PolymarketAdapter.place_prediction",
            new=AsyncMock(return_value=(True, {"status": "matched"})),
        ),
    ):
        out = await polymarket_place_market_order(
            wallet_label="main",
            market_slug="bitcoin-above-70k-on-february-9",
            outcome="YES",
            side="BUY",
            amount_collateral=2.0,
        )
        assert out["ok"] is True
        assert out["result"]["status"] == "confirmed"
        effects = out["result"]["effects"]
        assert effects and effects[0]["label"] == "place_market_order"
