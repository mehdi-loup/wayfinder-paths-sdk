from __future__ import annotations

import asyncio
from functools import cache
from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils import constants

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

QUICKNODE_PROXIED_TYPES = frozenset(
    {
        "activeAssetData",
        "clearinghouseState",
        "delegations",
        "delegatorSummary",
        "exchangeStatus",
        "extraAgents",
        "frontendOpenOrders",
        "leadingVaults",
        "liquidatable",
        "maxBuilderFee",
        "maxMarketOrderNtls",
        "meta",
        "openOrders",
        "outcomeMeta",
        "perpDeployAuctionStatus",
        "perpDexs",
        "perpsAtOpenInterestCap",
        "settledOutcome",
        "spotClearinghouseState",
        "spotDeployState",
        "spotMeta",
        "subAccounts",
        "userFees",
        "userRateLimit",
        "userRole",
        "userToMultiSigSigners",
        "userVaultEquities",
        "validatorL1Votes",
        "vaultSummaries",
        "webData2",
    }
)


@cache
def _public_info() -> Info:
    return Info(constants.MAINNET_API_URL, skip_ws=True)


class HyperliquidQuicknodeInfoClient(WayfinderClient):
    def __init__(self) -> None:
        super().__init__()
        base = get_api_base_url()
        self._quicknode_proxy_url = f"{base}/blockchain/hyperliquid/qn-info/"
        self._portfolio_state_url = f"{base}/blockchain/hyperliquid/portfolio-state/"

    async def post(self, body: dict[str, Any]) -> Any:
        if body.get("type") in QUICKNODE_PROXIED_TYPES:
            resp = await self._authed_request(
                "POST", self._quicknode_proxy_url, json=body
            )
            return resp.json()
        return await asyncio.to_thread(_public_info().post, "/info", body)

    async def portfolio_state(self, user: str) -> dict[str, Any]:
        resp = await self._authed_request(
            "GET", self._portfolio_state_url, params={"user": user}
        )
        return resp.json()


HYPERLIQUID_QUICKNODE_INFO_CLIENT = HyperliquidQuicknodeInfoClient()
