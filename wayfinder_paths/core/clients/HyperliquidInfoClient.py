from __future__ import annotations

import asyncio
from functools import cache
from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils import constants

from wayfinder_paths.core.clients.HyperliquidQuicknodeInfoClient import (
    HYPERLIQUID_QUICKNODE_CLIENT,
    QN_PROXIED_TYPES,
)


@cache
def _public_info() -> Info:
    return Info(constants.MAINNET_API_URL, skip_ws=True)


class HyperliquidInfoClient:
    async def post(self, body: dict[str, Any]) -> Any:
        if body["type"] in QN_PROXIED_TYPES:
            return await HYPERLIQUID_QUICKNODE_CLIENT.post(body)
        return await asyncio.to_thread(_public_info().post, "/info", body)


HYPERLIQUID_INFO_CLIENT = HyperliquidInfoClient()
