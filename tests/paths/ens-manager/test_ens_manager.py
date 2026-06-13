"""Test all ENS Manager write actions on Gorlami forks.

Mainnet fork: register, renew, transfer, set-records, create-subname
Base fork:    set-primary

Run from repo root:
    poetry run python tests/paths/ens-manager/test_ens_manager.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml
from web3.exceptions import Web3RPCError

from wayfinder_paths.core.config import get_rpc_urls, load_config, set_rpc_urls
from wayfinder_paths.core.utils.gorlami import gorlami_fork
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.testing.gorlami import gorlami_configured

ENS_PATH = Path(__file__).resolve().parents[3] / "paths/ens-manager"
CONFIG_PATH = ENS_PATH / "inputs/config.yaml"

sys.path.insert(0, str(ENS_PATH / "scripts"))
import main as ens  # noqa: E402

MAIN_WALLET = "0x081dc2947d97ef59Dd422f1c8750D9990eBaEC3f"
ONE_ETH = 10**18
TEST_NAME = "wayfinder-ens-fork-test"
TRANSFER_TO = "0x000000000000000000000000000000000000dEaD"


def load_ens_cfg() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    # Speed up tests: skip real sleep, advance fork time instead
    cfg["defaults"]["commit_wait_seconds"] = 1
    return cfg


def ok(label: str, result: dict) -> None:
    if "error" in result:
        print(f"  ❌ {label}: {result['error']}")
        sys.exit(1)
    print(f"  ✅ {label}: {result}")


def is_gorlami_backend_panic(exc: Exception) -> bool:
    return isinstance(exc, Web3RPCError) and "panicked" in str(exc)


async def run_mainnet_actions(fork_id: str, client, cfg: dict) -> None:
    print("\n[mainnet fork]")

    result = await ens.action_check(cfg, TEST_NAME)
    ok("check (available)", result)
    assert result["available"], "Expected test name to be available on fork"

    # Advance fork time AFTER commit so minCommitmentAge (60s) is satisfied
    async def advance_after_commit() -> None:
        await client.send_rpc(fork_id, "evm_increaseTime", [70])
        await client.send_rpc(fork_id, "evm_mine", [])

    print(f"  registering {TEST_NAME}.eth ...")
    result = await ens.action_register(
        cfg, TEST_NAME, "main", _after_commit_hook=advance_after_commit
    )
    ok("register", result)
    assert result.get("register_tx"), "Expected register_tx in result"

    await client.send_rpc(fork_id, "evm_mine", [])

    result = await ens.action_check(cfg, TEST_NAME)
    ok("check (after register)", result)
    assert not result["available"], "Expected name to be taken after register"

    result = await ens.action_renew(cfg, TEST_NAME, "main", duration_years=1)
    ok("renew", result)
    assert result.get("tx"), "Expected tx in renew result"

    result = await ens.action_set_records(
        cfg,
        f"{TEST_NAME}.eth",
        "main",
        "avatar",
        "https://wayfinder.ai/avatar.png",
    )
    ok("set-records (avatar)", result)
    assert result.get("tx"), "Expected tx in set-records result"

    result = await ens.action_set_records(cfg, f"{TEST_NAME}.eth", "main", "url", "https://wayfinder.ai")
    ok("set-records (url)", result)

    result = await ens.action_create_subname(
        cfg, f"{TEST_NAME}.eth", "alice", MAIN_WALLET, "main"
    )
    ok("create-subname", result)
    assert result.get("tx"), "Expected tx in create-subname result"

    result = await ens.action_lookup(cfg, f"{TEST_NAME}.eth")
    ok("lookup (after register)", result)

    result = await ens.action_transfer(cfg, TEST_NAME, TRANSFER_TO, "main")
    ok("transfer", result)
    assert result.get("tx"), "Expected tx in transfer result"


async def run_base_actions(_fork_id: str, _client, cfg: dict) -> None:
    print("\n[base fork]")

    result = await ens.action_set_primary(
        cfg, f"{TEST_NAME}.eth", "main", chain_id=8453
    )
    ok("set-primary (Base)", result)
    assert result.get("tx"), "Expected tx in set-primary result"


async def assert_base_set_primary_proxy_probe(cfg: dict) -> None:
    """Prove Base supports the call outside the Gorlami fork backend."""
    old_rpc_urls = get_rpc_urls().copy()
    try:
        proxy_rpcs = {
            key: value
            for key, value in old_rpc_urls.items()
            if str(key) not in {"8453", "base"}
        }
        set_rpc_urls(proxy_rpcs)

        async with web3_from_chain_id(8453) as w3:
            account = ens.get_account(w3, "main")
            reverse_reg_addr = w3.to_checksum_address(
                cfg["l2_contracts"]["reverse_registrar"]
            )
            reverse_reg = w3.eth.contract(
                address=reverse_reg_addr, abi=ens.L2_REVERSE_REGISTRAR_ABI
            )
            data = reverse_reg.encode_abi("setName", [f"{TEST_NAME}.eth"])
            code = await w3.eth.get_code(reverse_reg_addr)
            assert len(code) > 0

            tx = {
                "from": account.address,
                "to": reverse_reg_addr,
                "data": data,
                "value": 0,
            }
            await w3.eth.call(tx)
            assert await w3.eth.estimate_gas(tx) > 0
    finally:
        set_rpc_urls(old_rpc_urls)


async def run() -> None:
    load_config()
    cfg = load_ens_cfg()

    print("=== ENS Manager — full action test suite ===\n")
    print("Read-only actions (no fork needed):")

    result = await ens.action_lookup(cfg, "vitalik.eth")
    ok("lookup (forward)", result)

    result = await ens.action_lookup(cfg, "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    ok("lookup (reverse)", result)

    result = await ens.action_check(cfg, "vitalik")
    ok("check (taken)", result)
    assert not result["available"]

    result = await ens.action_check(cfg, "wahootest12345xyz")
    ok("check (available)", result)
    assert result["available"]

    print("\nWrite actions — Ethereum mainnet fork:")
    async with gorlami_fork(1, native_balances={MAIN_WALLET: ONE_ETH}) as (client, fork_info):
        fork_id = str(fork_info["fork_id"])
        print(f"  fork: {fork_id}  rpc: {fork_info['rpc_url']}")
        await run_mainnet_actions(fork_id, client, cfg)

    print("\nWrite actions — Base fork:")
    async with gorlami_fork(8453, native_balances={MAIN_WALLET: ONE_ETH}) as (client, fork_info):
        fork_id = str(fork_info["fork_id"])
        print(f"  fork: {fork_id}  rpc: {fork_info['rpc_url']}")
        try:
            await run_base_actions(fork_id, client, cfg)
        except Exception as exc:
            if not is_gorlami_backend_panic(exc):
                raise
            print(
                "  ⚠️ set-primary (Base): xfail — Gorlami Base fork backend "
                f"panicked: {exc}",
                flush=True,
            )

    print("\n=== ENS Manager checks complete ✅ ===")


@pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for Gorlami fork proxy)",
)
async def test_ens_manager_mainnet_gorlami_actions() -> None:
    load_config()
    cfg = load_ens_cfg()

    async with gorlami_fork(1, native_balances={MAIN_WALLET: ONE_ETH}) as (
        client,
        fork_info,
    ):
        await run_mainnet_actions(str(fork_info["fork_id"]), client, cfg)


@pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for Wayfinder RPC proxy)",
)
async def test_ens_manager_base_set_primary_proxy_probe() -> None:
    load_config()
    cfg = load_ens_cfg()

    await assert_base_set_primary_proxy_probe(cfg)


@pytest.mark.skipif(
    not gorlami_configured(),
    reason="api_key not configured (needed for Gorlami fork proxy)",
)
async def test_ens_manager_base_gorlami_set_primary() -> None:
    load_config()
    cfg = load_ens_cfg()

    async with gorlami_fork(8453, native_balances={MAIN_WALLET: ONE_ETH}) as (
        client,
        fork_info,
    ):
        try:
            await run_base_actions(str(fork_info["fork_id"]), client, cfg)
        except Exception as exc:
            if is_gorlami_backend_panic(exc):
                pytest.xfail(
                    "Gorlami Base fork backend panics on ENS L2 reverse "
                    f"registrar setName transaction: {exc}"
                )
            raise


if __name__ == "__main__":
    asyncio.run(run())
