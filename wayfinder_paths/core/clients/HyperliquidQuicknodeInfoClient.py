from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.config import get_api_base_url

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


class HyperliquidQuicknodeInfoClient(WayfinderClient):
    def __init__(self) -> None:
        super().__init__()
        base = get_api_base_url()
        self._qn_proxy_url = f"{base}/blockchain/hyperliquid/qn-info/"
        self._portfolio_state_url = f"{base}/blockchain/hyperliquid/portfolio-state/"

    async def post(self, body: dict[str, Any]) -> Any:
        resp = await self._authed_request("POST", self._qn_proxy_url, json=body)
        resp.raise_for_status()
        return resp.json()

    async def portfolio_state(self, user: str) -> dict[str, Any]:
        resp = await self._authed_request(
            "GET", self._portfolio_state_url, params={"user": user}
        )
        resp.raise_for_status()
        return resp.json()


HYPERLIQUID_QUICKNODE_CLIENT = HyperliquidQuicknodeInfoClient()
