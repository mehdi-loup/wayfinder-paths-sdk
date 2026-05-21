from __future__ import annotations

from typing import Any, Required, TypedDict

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url


class FundingHistoryEntry(TypedDict):
    time: Required[int]
    fundingRate: Required[str]


class CandleEntry(TypedDict):
    t: Required[int]
    T: Required[int]
    o: Required[str | None]
    h: Required[str | None]
    l: Required[str | None]  # noqa: E741
    c: Required[str | None]


class MarginSummary(TypedDict):
    accountValue: Required[str]
    totalNtlPos: Required[str]
    totalRawUsd: Required[str]
    totalMarginUsed: Required[str]


class ClearinghouseState(TypedDict, total=False):
    marginSummary: MarginSummary
    crossMarginSummary: MarginSummary
    crossMaintenanceMarginUsed: str
    withdrawable: str
    assetPositions: list[dict[str, Any]]


class SpotBalance(TypedDict):
    coin: Required[str]
    token: Required[int]
    total: Required[str]
    hold: Required[str]
    entryNtl: Required[str]


class SpotClearinghouseState(TypedDict):
    balances: Required[list[SpotBalance]]


class PortfolioState(TypedDict):
    clearinghouseState: Required[dict[str, ClearinghouseState]]
    spotClearinghouseState: Required[SpotClearinghouseState]
    userAbstraction: Required[str]


class HyperliquidDataClient(WayfinderClient):
    def __init__(self) -> None:
        super().__init__()
        self.api_base_url = f"{get_api_base_url()}/blockchain/hyperliquid"

    async def get_funding_history(
        self, coin: str, start_ms: int, end_ms: int
    ) -> list[FundingHistoryEntry]:
        url = f"{self.api_base_url}/funding/"
        params = {"coin": coin, "start_ms": start_ms, "end_ms": end_ms}
        resp = await self._authed_request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()["rows"]

    async def get_candles(
        self, coin: str, start_ms: int, end_ms: int, interval: str = "1h"
    ) -> list[CandleEntry]:
        url = f"{self.api_base_url}/candles/"
        params = {
            "coin": coin,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "interval": interval,
        }
        resp = await self._authed_request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()["rows"]

    async def get_portfolio_state(self, user: str) -> PortfolioState:
        """Full per-dex perp + spot + user-abstraction state in one call.

        Backed by vault-backend's hl_portfolioState (QN) → public fallback —
        collapses what would be ~6-8 sequential per-dex /info POSTs into one
        round-trip.
        """
        url = f"{self.api_base_url}/portfolio-state/"
        params = {"user": user.lower()}
        resp = await self._authed_request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()


HYPERLIQUID_DATA_CLIENT = HyperliquidDataClient()
