import pytest
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.euler_v2_adapter.adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_VAULT = "0x859160DB5841E5cfB8D3f144C6b3381A85A4b410"
BASE_EARN_VAULT = "0x8bF41Ad2b816F7c220b22F4BCD63fC2A35Ab4247"


class TestEulerV2Adapter:
    def test_adapter_type(self):
        assert EulerV2Adapter.adapter_type == "EULER_V2"

    def test_strategy_address_optional(self):
        adapter = EulerV2Adapter(config={})
        assert adapter.strategy_wallet_address is None

    @pytest.mark.asyncio
    async def test_unsupported_chain_returns_error(self):
        adapter = EulerV2Adapter(config={})
        ok, err = await adapter.get_verified_vaults(chain_id=0)
        assert ok is False
        assert isinstance(err, str) and err

    @pytest.mark.asyncio
    async def test_get_protocol_contracts_includes_current_surfaces(self):
        adapter = EulerV2Adapter(config={})

        ok, contracts = await adapter.get_protocol_contracts(chain_id=CHAIN_ID_BASE)

        assert ok is True, contracts
        assert contracts["evc"] == to_checksum_address(
            "0x5301c7dD20bD945D2013b48ed0DEE3A284ca8989"
        )
        assert contracts["lenses"]["vault_lens"] == to_checksum_address(
            "0x601F023CD063324DdbCADa69460e969fb97e98b9"
        )
        assert contracts["lenses"]["euler_earn_vault_lens"] == to_checksum_address(
            "0x0BBf9eE761bFF1c4d64dB608781D5e3beFeed875"
        )
        assert contracts["perspectives"]["euler_earn_governed"] == to_checksum_address(
            "0x08B817C17d84DF89AA371084D910081a5Cc04724"
        )
        assert contracts["swaps"]["euler_swap_v2_periphery"] == to_checksum_address(
            "0xA564dAe65eA7B1ce049AbACFC4Cb1A32C93e127c"
        )
        assert contracts["periphery"]["swap_verifier"] == to_checksum_address(
            "0xF8B2d2BA412E24235eAaDa8d3050202898455455"
        )

        ok, morph = await adapter.get_protocol_contracts(chain_id=2818)
        assert ok is True, morph
        assert morph["network"] == "morph"

    @pytest.mark.asyncio
    async def test_get_indexed_vaults_calls_euler_v3_api(self, monkeypatch):
        adapter = EulerV2Adapter(config={})
        captured = {}

        async def fake_get(endpoint, *, params=None):
            captured["endpoint"] = endpoint
            captured["params"] = params
            return {
                "data": [
                    {
                        "chainId": CHAIN_ID_BASE,
                        "address": BASE_VAULT,
                        "supplyApy": 5.25,
                        "totalAssets": "1000000",
                    }
                ],
                "meta": {"total": 1, "limit": 1},
            }

        monkeypatch.setattr(adapter, "_euler_v3_get", fake_get)

        ok, result = await adapter.get_indexed_vaults(
            chain_id=CHAIN_ID_BASE,
            limit=1,
            fields=["address", "supplyApy"],
            asset=BASE_USDC,
            sort="-totalSupplyUsd",
        )

        assert ok is True, result
        assert captured["endpoint"] == "/evk/vaults"
        assert captured["params"] == {
            "chainId": CHAIN_ID_BASE,
            "limit": 1,
            "offset": 0,
            "fields": "address,supplyApy",
            "sort": "-totalSupplyUsd",
            "asset": to_checksum_address(BASE_USDC),
            "minTvl": None,
            "maxTvl": None,
            "visibility": None,
        }
        assert result["source"] == "euler_v3_api"
        assert result["endpoint"] == "/evk/vaults"
        assert result["data"][0]["address"] == BASE_VAULT
        assert result["meta"]["total"] == 1

    @pytest.mark.asyncio
    async def test_get_euler_earn_vaults_and_resolve_vault_use_v3_api(
        self, monkeypatch
    ):
        adapter = EulerV2Adapter(config={})
        calls = []

        async def fake_get(endpoint, *, params=None):
            calls.append((endpoint, params))
            if endpoint == "/earn/vaults":
                return {
                    "data": [{"chainId": CHAIN_ID_BASE, "address": BASE_EARN_VAULT}],
                    "meta": {"total": 1},
                }
            return {
                "data": {
                    "chainId": CHAIN_ID_BASE,
                    "address": to_checksum_address(BASE_VAULT),
                    "found": True,
                    "vaultType": "evk",
                },
                "meta": {},
            }

        monkeypatch.setattr(adapter, "_euler_v3_get", fake_get)

        ok, earn = await adapter.get_euler_earn_vaults(chain_id=CHAIN_ID_BASE, limit=5)
        assert ok is True, earn
        assert earn["endpoint"] == "/earn/vaults"
        assert earn["data"][0]["address"] == BASE_EARN_VAULT

        ok, resolved = await adapter.resolve_vault(
            chain_id=CHAIN_ID_BASE, address=BASE_VAULT
        )
        assert ok is True, resolved
        assert resolved["data"]["found"] is True
        assert resolved["data"]["vaultType"] == "evk"

        assert calls == [
            (
                "/earn/vaults",
                {"chainId": CHAIN_ID_BASE, "limit": 5, "offset": 0},
            ),
            (
                "/resolve/vaults",
                {"chainId": CHAIN_ID_BASE, "address": to_checksum_address(BASE_VAULT)},
            ),
        ]

    @pytest.mark.asyncio
    async def test_get_offchain_prices_checksums_and_dedupes_addresses(
        self, monkeypatch
    ):
        adapter = EulerV2Adapter(config={})
        captured = {}

        async def fake_get(endpoint, *, params=None):
            captured["endpoint"] = endpoint
            captured["params"] = params
            return {
                "data": [
                    {
                        "chainId": CHAIN_ID_BASE,
                        "address": to_checksum_address(BASE_USDC),
                        "priceUsd": 0.999,
                    }
                ],
                "meta": {"hasMore": False},
            }

        monkeypatch.setattr(adapter, "_euler_v3_get", fake_get)

        ok, prices = await adapter.get_offchain_prices(
            chain_id=CHAIN_ID_BASE,
            addresses=[BASE_USDC.lower(), BASE_USDC],
        )

        assert ok is True, prices
        assert captured["endpoint"] == "/prices"
        assert captured["params"] == {
            "chainId": CHAIN_ID_BASE,
            "addresses": to_checksum_address(BASE_USDC),
        }
        assert prices["data"][0]["priceUsd"] == 0.999

    @pytest.mark.asyncio
    async def test_get_labelled_vaults_reads_products_and_earn_lists(self, monkeypatch):
        adapter = EulerV2Adapter(config={})
        calls = []

        async def fake_labels(*, chain_id, file_name):
            calls.append((chain_id, file_name))
            if file_name == "products.json":
                return {
                    "curator-product": {
                        "name": "Curator Product",
                        "vaults": [BASE_VAULT.lower(), BASE_VAULT],
                    }
                }
            return [BASE_EARN_VAULT.lower(), BASE_EARN_VAULT]

        monkeypatch.setattr(adapter, "_euler_labels_get", fake_labels)

        ok, labels = await adapter.get_labelled_vaults(chain_id=CHAIN_ID_BASE)

        assert ok is True, labels
        assert labels["source"] == "euler_labels"
        assert labels["evk_vaults"] == [to_checksum_address(BASE_VAULT)]
        assert labels["earn_vaults"] == [to_checksum_address(BASE_EARN_VAULT)]
        assert labels["product_count"] == 1
        assert calls == [
            (CHAIN_ID_BASE, "products.json"),
            (CHAIN_ID_BASE, "earn-vaults.json"),
        ]
