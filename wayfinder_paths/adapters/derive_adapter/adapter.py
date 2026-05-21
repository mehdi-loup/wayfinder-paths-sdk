from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from eth_account.messages import defunct_hash_message

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter

DERIVE_MAINNET_API_BASE_URL = "https://api.lyra.finance"
DERIVE_TESTNET_API_BASE_URL = "https://api-demo.lyra.finance"
DERIVE_MAINNET_WS_URL = "wss://api.lyra.finance/ws"
DERIVE_TESTNET_WS_URL = "wss://api-demo.lyra.finance/ws"

InstrumentType = Literal["option", "perp", "erc20"]

DERIVE_ORDER_REQUIRED_FIELDS = frozenset(
    {
        "amount",
        "direction",
        "instrument_name",
        "limit_price",
        "max_fee",
        "nonce",
        "signature",
        "signature_expiry_sec",
        "signer",
        "subaccount_id",
    }
)

DERIVE_ORDERBOOK_GROUPS = frozenset({"1", "10", "100"})
DERIVE_ORDERBOOK_DEPTHS = frozenset({"1", "10", "20", "100"})


class DeriveAdapter(BaseAdapter):
    adapter_type = "DERIVE"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Callable[[dict[str, Any]], Awaitable[bytes]] | None = None,
        sign_hash_callback: Callable[[str], Awaitable[str]] | None = None,
        sign_message_callback: Callable[[str], Awaitable[str]] | None = None,
        wallet_address: str | None = None,
        derive_wallet_address: str | None = None,
        api_base_url: str | None = None,
        ws_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        http_timeout_s: float = 30.0,
    ) -> None:
        super().__init__("derive_adapter", config)

        derive_config = self.config.get("derive") or {}
        if not isinstance(derive_config, dict):
            derive_config = {}

        network = str(derive_config.get("network", "mainnet")).lower()
        use_testnet = network in {"testnet", "demo", "api-demo"}

        self.api_base_url = (
            api_base_url
            or derive_config.get("api_base_url")
            or (
                DERIVE_TESTNET_API_BASE_URL
                if use_testnet
                else DERIVE_MAINNET_API_BASE_URL
            )
        )
        self.ws_url = (
            ws_url
            or derive_config.get("ws_url")
            or (DERIVE_TESTNET_WS_URL if use_testnet else DERIVE_MAINNET_WS_URL)
        )
        self.wallet_address = wallet_address or derive_config.get("wallet_address")
        self.derive_wallet_address = (
            derive_wallet_address
            or derive_config.get("derive_wallet_address")
            or self.wallet_address
        )
        self.sign_callback = sign_callback
        self.sign_hash_callback = sign_hash_callback
        self.sign_message_callback = sign_message_callback
        self._http = http_client or httpx.AsyncClient(
            base_url=self.api_base_url,
            timeout=httpx.Timeout(http_timeout_s),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        private: bool = False,
    ) -> tuple[bool, Any]:
        headers: dict[str, str] | None = None
        if private:
            ok, auth_headers = await self._auth_headers()
            if not ok:
                return False, auth_headers
            headers = auth_headers

        try:
            response = await self._http.post(path, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        if not isinstance(data, dict):
            return False, f"Unexpected Derive response: {type(data).__name__}"
        if data.get("error") is not None:
            return False, data["error"]
        if "result" not in data:
            return False, f"Derive response missing result: {data}"
        return True, data["result"]

    async def _auth_headers(self) -> tuple[bool, dict[str, str] | str]:
        if not self.derive_wallet_address:
            return (
                False,
                "derive_wallet_address is required for Derive private endpoints",
            )

        timestamp = str(int(time.time() * 1000))
        if self.sign_message_callback is not None:
            signature = await self.sign_message_callback(timestamp)
        elif self.sign_hash_callback is not None:
            digest = defunct_hash_message(text=timestamp)
            signature = await self.sign_hash_callback(f"0x{bytes(digest).hex()}")
        else:
            return (
                False,
                "sign_message_callback or sign_hash_callback is required for Derive private endpoints",
            )

        return (
            True,
            {
                "X-LyraWallet": str(self.derive_wallet_address),
                "X-LyraTimestamp": timestamp,
                "X-LyraSignature": str(signature),
            },
        )

    async def get_time(self) -> tuple[bool, int | str]:
        ok, result = await self._post("/public/get_time", {})
        if not ok:
            return False, result
        if not isinstance(result, int):
            return False, f"Unexpected get_time result: {result}"
        return True, result

    async def get_instruments(
        self,
        *,
        currency: str,
        instrument_type: InstrumentType = "option",
        expired: bool = False,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        ok, result = await self._post(
            "/public/get_instruments",
            {
                "currency": currency.upper(),
                "instrument_type": instrument_type,
                "expired": expired,
            },
        )
        if not ok:
            return False, result
        if not isinstance(result, list):
            return False, f"Unexpected get_instruments result: {type(result).__name__}"
        return True, result

    async def list_options(
        self, *, currency: str, expired: bool = False
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        return await self.get_instruments(
            currency=currency,
            instrument_type="option",
            expired=expired,
        )

    async def list_option_expiries(
        self, *, currency: str, expired: bool = False
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        ok, instruments = await self.list_options(currency=currency, expired=expired)
        if not ok:
            return False, instruments

        expiries: dict[int, int] = {}
        for instrument in instruments:
            details = instrument.get("option_details") or {}
            expiry = details.get("expiry")
            if isinstance(expiry, int):
                expiries[expiry] = expiries.get(expiry, 0) + 1

        return True, [
            {
                "expiry": expiry,
                "expiry_date": self.expiry_date(expiry),
                "instrument_count": count,
            }
            for expiry, count in sorted(expiries.items())
        ]

    async def get_tickers(
        self,
        *,
        instrument_type: InstrumentType,
        currency: str | None = None,
        expiry_date: str | int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        if instrument_type == "option" and (currency is None or expiry_date is None):
            return False, "currency and expiry_date are required for option tickers"

        payload: dict[str, Any] = {"instrument_type": instrument_type}
        if currency is not None:
            payload["currency"] = currency.upper()
        if expiry_date is not None:
            payload["expiry_date"] = str(expiry_date)

        ok, result = await self._post("/public/get_tickers", payload)
        if not ok:
            return False, result
        if not isinstance(result, dict):
            return False, f"Unexpected get_tickers result: {type(result).__name__}"
        tickers = result.get("tickers")
        if not isinstance(tickers, dict):
            return False, f"Unexpected get_tickers tickers: {type(tickers).__name__}"
        return True, tickers

    async def get_option_tickers(
        self,
        *,
        currency: str,
        expiry_date: str | int,
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self.get_tickers(
            instrument_type="option",
            currency=currency,
            expiry_date=expiry_date,
        )

    async def get_ticker(
        self, *, instrument_name: str
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, result = await self._post(
            "/public/get_ticker",
            {"instrument_name": instrument_name},
        )
        if not ok:
            return False, result
        if not isinstance(result, dict):
            return False, f"Unexpected get_ticker result: {type(result).__name__}"
        return True, result

    async def get_subaccounts(
        self, *, wallet: str | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        target_wallet = wallet or self.derive_wallet_address
        if not target_wallet:
            return False, "wallet or derive_wallet_address is required"
        return await self._post(
            "/private/get_subaccounts",
            {"wallet": target_wallet},
            private=True,
        )

    async def get_subaccount(
        self, *, subaccount_id: int
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self._post(
            "/private/get_subaccount",
            {"subaccount_id": subaccount_id},
            private=True,
        )

    async def get_positions(
        self, *, subaccount_id: int
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self._post(
            "/private/get_positions",
            {"subaccount_id": subaccount_id},
            private=True,
        )

    async def get_open_orders(
        self, *, subaccount_id: int
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self._post(
            "/private/get_open_orders",
            {"subaccount_id": subaccount_id},
            private=True,
        )

    async def get_margin(
        self,
        *,
        subaccount_id: int,
        simulated_position_changes: list[dict[str, Any]] | None = None,
        simulated_collateral_changes: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        payload: dict[str, Any] = {"subaccount_id": subaccount_id}
        if simulated_position_changes is not None:
            payload["simulated_position_changes"] = simulated_position_changes
        if simulated_collateral_changes is not None:
            payload["simulated_collateral_changes"] = simulated_collateral_changes
        return await self._post("/private/get_margin", payload, private=True)

    async def debug_order(
        self, order: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, error = self._validate_signed_order(order)
        if not ok:
            return False, error
        return await self._post("/private/order_debug", dict(order), private=True)

    async def submit_order(
        self,
        order: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        ok, error = self._validate_signed_order(order)
        if not ok:
            return False, error

        path = "/private/order_debug" if dry_run else "/private/order"
        return await self._post(path, dict(order), private=True)

    async def cancel_order(
        self,
        *,
        instrument_name: str,
        order_id: str,
        subaccount_id: int,
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self._post(
            "/private/cancel",
            {
                "instrument_name": instrument_name,
                "order_id": order_id,
                "subaccount_id": subaccount_id,
            },
            private=True,
        )

    @staticmethod
    def _validate_signed_order(order: dict[str, Any]) -> tuple[bool, str]:
        missing = sorted(DERIVE_ORDER_REQUIRED_FIELDS.difference(order))
        if missing:
            return False, f"missing signed Derive order fields: {', '.join(missing)}"
        return True, ""

    @staticmethod
    def expiry_date(expiry_timestamp: int) -> str:
        return datetime.fromtimestamp(expiry_timestamp, UTC).strftime("%Y%m%d")

    @staticmethod
    def new_order_nonce(now_ms: int | None = None) -> int:
        timestamp_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return timestamp_ms * 1000 + secrets.randbelow(1000)

    @staticmethod
    def orderbook_channel(
        instrument_name: str,
        *,
        group: str = "1",
        depth: str = "10",
    ) -> str:
        if group not in DERIVE_ORDERBOOK_GROUPS:
            raise ValueError("group must be one of: 1, 10, 100")
        if depth not in DERIVE_ORDERBOOK_DEPTHS:
            raise ValueError("depth must be one of: 1, 10, 20, 100")
        return f"orderbook.{instrument_name}.{group}.{depth}"
