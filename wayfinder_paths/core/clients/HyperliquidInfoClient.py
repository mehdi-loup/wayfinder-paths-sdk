from __future__ import annotations

import asyncio
from functools import cache
from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils import constants

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

# /info types where vault-backend proxies to QuickNode. Anything outside
# this set goes straight to api.hyperliquid.xyz via the SDK Info client —
# no point paying the proxy hop when QN doesn't serve the type anyway.
QN_PROXIED_TYPES = frozenset(
    {
        "clearinghouseState",
        "spotClearinghouseState",
        "frontendOpenOrders",
        "maxBuilderFee",
        "meta",
        "openOrders",
        "outcomeMeta",
        "perpDexs",
        "spotMeta",
    }
)


@cache
def _public_info() -> Info:
    return Info(constants.MAINNET_API_URL, skip_ws=True)


class HyperliquidInfoClient(WayfinderClient):
    def __init__(self) -> None:
        super().__init__()
        base = get_api_base_url()
        self._qn_proxy_url = f"{base}/blockchain/hyperliquid/qn-info/"
        self._portfolio_state_url = f"{base}/blockchain/hyperliquid/portfolio-state/"

    async def post(self, body: dict[str, Any]) -> Any:
        if body["type"] in QN_PROXIED_TYPES:
            resp = await self._authed_request("POST", self._qn_proxy_url, json=body)
            resp.raise_for_status()
            return resp.json()
        return await asyncio.to_thread(_public_info().post, "/info", body)

    async def portfolio_state(self, user: str) -> dict[str, Any]:
        resp = await self._authed_request(
            "GET", self._portfolio_state_url, params={"user": user}
        )
        resp.raise_for_status()
        return resp.json()


HYPERLIQUID_INFO_CLIENT = HyperliquidInfoClient()
