"""ENS Manager — register, renew, transfer, set records, create subnames, set L2 primary name.

Actions:
  lookup        Resolve name→address or address→name (read-only)
  check         Check availability + registration price (read-only)
  register      Register a .eth name (commit → wait → register, mainnet)
  renew         Renew an existing .eth name (mainnet)
  transfer      Transfer .eth name ownership to another address (mainnet)
  set-records   Set text or address records on a name (mainnet)
  create-subname  Create a subname under a name you own (mainnet)
  set-primary   Set your primary ENS name from an L2 wallet (Base / OP / Arb)

Usage:
  poetry run python paths/ens-manager/scripts/main.py --action check --name vitalik
  poetry run python paths/ens-manager/scripts/main.py --action register --name myname --wallet main
  poetry run python paths/ens-manager/scripts/main.py --action set-primary --name myname.eth --wallet main
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import yaml
from eth_utils import keccak

from wayfinder_paths.core.config import load_config, CONFIG
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

# ---------------------------------------------------------------------------
# ABIs
# ---------------------------------------------------------------------------

ETH_REGISTRAR_CONTROLLER_ABI = [
    {"inputs": [{"name": "name", "type": "string"}], "name": "available", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "name", "type": "string"}, {"name": "duration", "type": "uint256"}], "name": "rentPrice", "outputs": [{"components": [{"name": "base", "type": "uint256"}, {"name": "premium", "type": "uint256"}], "name": "price", "type": "tuple"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "name", "type": "string"}, {"name": "owner", "type": "address"}, {"name": "duration", "type": "uint256"}, {"name": "secret", "type": "bytes32"}, {"name": "resolver", "type": "address"}, {"name": "data", "type": "bytes[]"}, {"name": "reverseRecord", "type": "bool"}, {"name": "ownerControlledFuses", "type": "uint16"}], "name": "makeCommitment", "outputs": [{"name": "", "type": "bytes32"}], "stateMutability": "pure", "type": "function"},
    {"inputs": [{"name": "commitment", "type": "bytes32"}], "name": "commit", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "name", "type": "string"}, {"name": "owner", "type": "address"}, {"name": "duration", "type": "uint256"}, {"name": "secret", "type": "bytes32"}, {"name": "resolver", "type": "address"}, {"name": "data", "type": "bytes[]"}, {"name": "reverseRecord", "type": "bool"}, {"name": "ownerControlledFuses", "type": "uint16"}], "name": "register", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "name", "type": "string"}, {"name": "duration", "type": "uint256"}], "name": "renew", "outputs": [], "stateMutability": "payable", "type": "function"},
]

ENS_REGISTRY_ABI = [
    {"inputs": [{"name": "node", "type": "bytes32"}], "name": "owner", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "node", "type": "bytes32"}], "name": "resolver", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]

BASE_REGISTRAR_ABI = [
    {"inputs": [{"name": "id", "type": "uint256"}], "name": "nameExpires", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "id", "type": "uint256"}], "name": "ownerOf", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "tokenId", "type": "uint256"}], "name": "safeTransferFrom", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

NAME_WRAPPER_ABI = [
    {"inputs": [{"name": "id", "type": "uint256"}], "name": "ownerOf", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "id", "type": "uint256"}, {"name": "amount", "type": "uint256"}, {"name": "data", "type": "bytes"}], "name": "safeTransferFrom", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "parentNode", "type": "bytes32"}, {"name": "label", "type": "string"}, {"name": "owner", "type": "address"}, {"name": "fuses", "type": "uint32"}, {"name": "expiry", "type": "uint64"}], "name": "setSubnodeOwner", "outputs": [{"name": "node", "type": "bytes32"}], "stateMutability": "nonpayable", "type": "function"},
]

PUBLIC_RESOLVER_ABI = [
    {"inputs": [{"name": "node", "type": "bytes32"}, {"name": "key", "type": "string"}], "name": "text", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "node", "type": "bytes32"}, {"name": "key", "type": "string"}, {"name": "value", "type": "string"}], "name": "setText", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "node", "type": "bytes32"}, {"name": "a", "type": "address"}], "name": "setAddr", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "node", "type": "bytes32"}], "name": "addr", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]

L2_REVERSE_REGISTRAR_ABI = [
    {"inputs": [{"name": "name", "type": "string"}], "name": "setName", "outputs": [{"name": "", "type": "bytes32"}], "stateMutability": "nonpayable", "type": "function"},
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SECONDS_PER_YEAR = 365 * 24 * 3600


def namehash(name: str) -> bytes:
    node = b"\x00" * 32
    if name:
        for label in reversed(name.split(".")):
            node = keccak(node + keccak(text=label))
    return node


def labelhash(label: str) -> bytes:
    return keccak(text=label)


def labelhash_int(label: str) -> int:
    return int.from_bytes(labelhash(label), "big")


def namehash_int(name: str) -> int:
    return int.from_bytes(namehash(name), "big")


def normalise(name: str) -> str:
    """Strip .eth suffix and lowercase."""
    name = name.lower().strip()
    if name.endswith(".eth"):
        name = name[:-4]
    return name


def wei_to_eth(wei: int) -> float:
    return wei / 1e18


async def _gas_params(w3: Any) -> dict[str, Any]:
    latest = await w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", 0)
    priority = await w3.eth.max_priority_fee
    return {
        "maxFeePerGas": base_fee * 2 + priority,
        "maxPriorityFeePerGas": priority,
        "type": 2,
    }


async def send_tx(w3: Any, account: Any, tx_data: dict[str, Any]) -> tuple[str, Any]:
    nonce, chain_id, gas_params = await asyncio.gather(
        w3.eth.get_transaction_count(account.address),
        w3.eth.chain_id,
        _gas_params(w3),
    )
    tx = {**tx_data, "from": account.address, "nonce": nonce, "chainId": chain_id, **gas_params}
    try:
        tx["gas"] = await w3.eth.estimate_gas(tx)
    except Exception:
        tx["gas"] = 300_000
    signed = account.sign_transaction(tx)
    tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    if receipt["status"] != 1:
        raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
    return tx_hash.hex(), receipt


def get_account(w3: Any, wallet_label: str) -> Any:
    wallets = CONFIG.get("wallets", [])
    wallet = next((w for w in wallets if w["label"] == wallet_label), None)
    if not wallet:
        raise ValueError(f"Wallet '{wallet_label}' not found in config")
    return w3.eth.account.from_key(wallet["private_key_hex"])


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

async def action_lookup(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    """Resolve name → address or address → name (reverse lookup)."""
    chain_id = cfg["chain_eth"]

    async with web3_from_chain_id(chain_id) as w3:
        contracts = cfg["contracts"]
        registry = w3.eth.contract(address=w3.to_checksum_address(contracts["ens_registry"]), abi=ENS_REGISTRY_ABI)

        if name.startswith("0x") and len(name) == 42:
            # Reverse lookup: address → primary name
            reverse_node = namehash(f"{name[2:].lower()}.addr.reverse")
            resolver_addr = await registry.functions.resolver(reverse_node).call()
            zero = "0x" + "0" * 40
            if resolver_addr == zero:
                return {"address": name, "primary_name": None}
            resolver = w3.eth.contract(address=resolver_addr, abi=PUBLIC_RESOLVER_ABI)
            # name() on reverse resolver
            name_abi = [{"inputs": [{"name": "node", "type": "bytes32"}], "name": "name", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"}]
            rev_resolver = w3.eth.contract(address=resolver_addr, abi=name_abi)
            primary = await rev_resolver.functions.name(reverse_node).call()
            return {"address": name, "primary_name": primary or None}
        else:
            # Forward lookup: name → address
            full_name = name if name.endswith(".eth") else f"{name}.eth"
            node = namehash(full_name)
            resolver_addr = await registry.functions.resolver(node).call()
            zero = "0x" + "0" * 40
            if resolver_addr == zero:
                return {"name": full_name, "address": None, "resolver": None}
            resolver = w3.eth.contract(address=resolver_addr, abi=PUBLIC_RESOLVER_ABI)
            addr = await resolver.functions.addr(node).call()
            owner = await registry.functions.owner(node).call()
            return {"name": full_name, "address": addr if addr != zero else None, "owner": owner, "resolver": resolver_addr}


async def action_check(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    """Check availability and registration price for a .eth name."""
    label = normalise(name)
    duration = cfg["defaults"]["duration_years"] * SECONDS_PER_YEAR
    chain_id = cfg["chain_eth"]

    async with web3_from_chain_id(chain_id) as w3:
        contracts = cfg["contracts"]
        controller = w3.eth.contract(
            address=w3.to_checksum_address(contracts["eth_registrar_controller"]),
            abi=ETH_REGISTRAR_CONTROLLER_ABI,
        )
        base_reg = w3.eth.contract(
            address=w3.to_checksum_address(contracts["base_registrar"]),
            abi=BASE_REGISTRAR_ABI,
        )

        available, price_tuple = await asyncio.gather(
            controller.functions.available(label).call(),
            controller.functions.rentPrice(label, duration).call(),
        )

        expires_ts: int | None = None
        if not available:
            try:
                token_id = labelhash_int(label)
                expires_ts = await base_reg.functions.nameExpires(token_id).call()
            except Exception:
                pass

        price_wei = price_tuple[0] + price_tuple[1]  # base + premium
        return {
            "name": f"{label}.eth",
            "available": available,
            "price_eth": round(wei_to_eth(price_wei), 6),
            "price_wei": price_wei,
            "duration_years": cfg["defaults"]["duration_years"],
            "expires": expires_ts,
        }


async def action_register(cfg: dict[str, Any], name: str, wallet_label: str, duration_years: int | None = None, _after_commit_hook: Any = None) -> dict[str, Any]:
    """Register a .eth name: commit → wait 60s → register."""
    label = normalise(name)
    duration = (duration_years or cfg["defaults"]["duration_years"]) * SECONDS_PER_YEAR
    chain_id = cfg["chain_eth"]

    async with web3_from_chain_id(chain_id) as w3:
        account = get_account(w3, wallet_label)
        contracts = cfg["contracts"]
        controller = w3.eth.contract(
            address=w3.to_checksum_address(contracts["eth_registrar_controller"]),
            abi=ETH_REGISTRAR_CONTROLLER_ABI,
        )

        available = await controller.functions.available(label).call()
        if not available:
            return {"error": f"{label}.eth is not available"}

        price_tuple = await controller.functions.rentPrice(label, duration).call()
        price_wei = price_tuple[0] + price_tuple[1]
        # 10% buffer to absorb price fluctuations
        value_wei = int(price_wei * 1.1)

        secret = secrets.token_bytes(32)
        resolver = w3.to_checksum_address(contracts["public_resolver"])

        commitment = await controller.functions.makeCommitment(
            label, account.address, duration, secret, resolver, [], True, 0
        ).call()

        print(f"  [1/3] committing to {label}.eth ...", flush=True)
        commit_hash, _ = await send_tx(w3, account, {
            "to": controller.address,
            "data": controller.encode_abi("commit", [commitment]),
            "value": 0,
        })
        if _after_commit_hook:
            await _after_commit_hook()
        print(f"  [2/3] waiting {cfg['defaults']['commit_wait_seconds']}s ...", flush=True)
        await asyncio.sleep(cfg["defaults"]["commit_wait_seconds"])

        print(f"  [3/3] registering {label}.eth ...", flush=True)
        reg_hash, receipt = await send_tx(w3, account, {
            "to": controller.address,
            "data": controller.encode_abi("register", [label, account.address, duration, secret, resolver, [], True, 0]),
            "value": value_wei,
        })

        return {
            "name": f"{label}.eth",
            "owner": account.address,
            "duration_years": duration_years or cfg["defaults"]["duration_years"],
            "price_eth": round(wei_to_eth(price_wei), 6),
            "commit_tx": commit_hash,
            "register_tx": reg_hash,
            "block": receipt["blockNumber"],
        }


async def action_renew(cfg: dict[str, Any], name: str, wallet_label: str, duration_years: int | None = None) -> dict[str, Any]:
    """Renew an existing .eth name."""
    label = normalise(name)
    duration = (duration_years or cfg["defaults"]["duration_years"]) * SECONDS_PER_YEAR
    chain_id = cfg["chain_eth"]

    async with web3_from_chain_id(chain_id) as w3:
        account = get_account(w3, wallet_label)
        contracts = cfg["contracts"]
        controller = w3.eth.contract(
            address=w3.to_checksum_address(contracts["eth_registrar_controller"]),
            abi=ETH_REGISTRAR_CONTROLLER_ABI,
        )

        price_tuple = await controller.functions.rentPrice(label, duration).call()
        price_wei = price_tuple[0] + price_tuple[1]
        value_wei = int(price_wei * 1.1)

        print(f"  renewing {label}.eth for {duration_years or cfg['defaults']['duration_years']} year(s) ...", flush=True)
        tx_hash, receipt = await send_tx(w3, account, {
            "to": controller.address,
            "data": controller.encode_abi("renew", [label, duration]),
            "value": value_wei,
        })

        return {
            "name": f"{label}.eth",
            "duration_years": duration_years or cfg["defaults"]["duration_years"],
            "price_eth": round(wei_to_eth(price_wei), 6),
            "tx": tx_hash,
            "block": receipt["blockNumber"],
        }


async def action_transfer(cfg: dict[str, Any], name: str, to: str, wallet_label: str) -> dict[str, Any]:
    """Transfer .eth name ownership. Handles both wrapped (NameWrapper ERC-1155) and unwrapped (BaseRegistrar ERC-721)."""
    label = normalise(name)
    full_name = f"{label}.eth"
    # NameWrapper uses namehash as token ID; BaseRegistrar uses labelhash
    nw_token_id = namehash_int(full_name)
    br_token_id = labelhash_int(label)
    chain_id = cfg["chain_eth"]

    async with web3_from_chain_id(chain_id) as w3:
        account = get_account(w3, wallet_label)
        to_addr = w3.to_checksum_address(to)
        contracts = cfg["contracts"]

        name_wrapper = w3.eth.contract(
            address=w3.to_checksum_address(contracts["name_wrapper"]),
            abi=NAME_WRAPPER_ABI,
        )
        base_reg = w3.eth.contract(
            address=w3.to_checksum_address(contracts["base_registrar"]),
            abi=BASE_REGISTRAR_ABI,
        )

        # NameWrapper ownerOf uses the namehash-based token ID
        try:
            zero = "0x" + "0" * 40
            wrapped_owner = await name_wrapper.functions.ownerOf(nw_token_id).call()
            is_wrapped = wrapped_owner.lower() != zero.lower()
        except Exception:
            is_wrapped = False

        print(f"  transferring {full_name} ({'wrapped' if is_wrapped else 'unwrapped'}) to {to_addr} ...", flush=True)

        if is_wrapped:
            # ERC-1155 safeTransferFrom(from, to, id, amount, data)
            tx_hash, receipt = await send_tx(w3, account, {
                "to": name_wrapper.address,
                "data": name_wrapper.encode_abi("safeTransferFrom", [account.address, to_addr, nw_token_id, 1, b""]),
                "value": 0,
            })
        else:
            # ERC-721 safeTransferFrom(from, to, tokenId)
            tx_hash, receipt = await send_tx(w3, account, {
                "to": base_reg.address,
                "data": base_reg.encode_abi("safeTransferFrom", [account.address, to_addr, br_token_id]),
                "value": 0,
            })

        return {
            "name": f"{label}.eth",
            "from": account.address,
            "to": to_addr,
            "wrapped": is_wrapped,
            "tx": tx_hash,
            "block": receipt["blockNumber"],
        }


async def action_set_records(cfg: dict[str, Any], name: str, wallet_label: str, key: str, value: str) -> dict[str, Any]:
    """Set a text record (e.g. url, email, twitter, avatar) on a .eth name."""
    full_name = name if name.endswith(".eth") else f"{name}.eth"
    node = namehash(full_name)
    chain_id = cfg["chain_eth"]

    async with web3_from_chain_id(chain_id) as w3:
        account = get_account(w3, wallet_label)
        resolver_addr = w3.to_checksum_address(cfg["contracts"]["public_resolver"])
        resolver = w3.eth.contract(address=resolver_addr, abi=PUBLIC_RESOLVER_ABI)

        print(f"  setting {key}={value!r} on {full_name} ...", flush=True)
        tx_hash, receipt = await send_tx(w3, account, {
            "to": resolver_addr,
            "data": resolver.encode_abi("setText", [node, key, value]),
            "value": 0,
        })

        return {
            "name": full_name,
            "key": key,
            "value": value,
            "tx": tx_hash,
            "block": receipt["blockNumber"],
        }


async def action_create_subname(cfg: dict[str, Any], parent: str, sublabel: str, owner: str, wallet_label: str) -> dict[str, Any]:
    """Create a subname (e.g. sub.myname.eth) under a name you own (via NameWrapper)."""
    full_parent = parent if parent.endswith(".eth") else f"{parent}.eth"
    parent_node = namehash(full_parent)
    chain_id = cfg["chain_eth"]

    async with web3_from_chain_id(chain_id) as w3:
        account = get_account(w3, wallet_label)
        owner_addr = w3.to_checksum_address(owner)
        contracts = cfg["contracts"]
        name_wrapper = w3.eth.contract(
            address=w3.to_checksum_address(contracts["name_wrapper"]),
            abi=NAME_WRAPPER_ABI,
        )

        full_subname = f"{sublabel}.{full_parent}"
        print(f"  creating subname {full_subname} → {owner_addr} ...", flush=True)
        tx_hash, receipt = await send_tx(w3, account, {
            "to": name_wrapper.address,
            "data": name_wrapper.encode_abi("setSubnodeOwner", [parent_node, sublabel, owner_addr, 0, 0]),
            "value": 0,
        })

        return {
            "subname": full_subname,
            "parent": full_parent,
            "owner": owner_addr,
            "tx": tx_hash,
            "block": receipt["blockNumber"],
        }


async def action_set_primary(cfg: dict[str, Any], name: str, wallet_label: str, chain_id: int | None = None) -> dict[str, Any]:
    """Set primary ENS name from an L2 wallet (Base, Optimism, Arbitrum, Linea, Scroll)."""
    full_name = name if name.endswith(".eth") else f"{name}.eth"
    target_chain = chain_id or cfg["chain_base"]

    async with web3_from_chain_id(target_chain) as w3:
        account = get_account(w3, wallet_label)
        reverse_reg_addr = w3.to_checksum_address(cfg["l2_contracts"]["reverse_registrar"])
        reverse_reg = w3.eth.contract(address=reverse_reg_addr, abi=L2_REVERSE_REGISTRAR_ABI)

        print(f"  setting primary name to {full_name} on chain {target_chain} ...", flush=True)
        tx_hash, receipt = await send_tx(w3, account, {
            "to": reverse_reg_addr,
            "data": reverse_reg.encode_abi("setName", [full_name]),
            "value": 0,
        })

        return {
            "name": full_name,
            "wallet": account.address,
            "chain_id": target_chain,
            "tx": tx_hash,
            "block": receipt["blockNumber"],
        }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def run(config_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    cfg = yaml.safe_load(config_path.read_text())
    wallet = args.wallet or cfg.get("wallet", "main")

    if args.action == "lookup":
        return await action_lookup(cfg, args.name)
    if args.action == "check":
        return await action_check(cfg, args.name)
    if args.action == "register":
        return await action_register(cfg, args.name, wallet, args.duration)
    if args.action == "renew":
        return await action_renew(cfg, args.name, wallet, args.duration)
    if args.action == "transfer":
        if not args.to:
            return {"error": "--to address is required for transfer"}
        return await action_transfer(cfg, args.name, args.to, wallet)
    if args.action == "set-records":
        if not args.key or args.value is None:
            return {"error": "--key and --value are required for set-records"}
        return await action_set_records(cfg, args.name, wallet, args.key, args.value)
    if args.action == "create-subname":
        if not args.sublabel or not args.to:
            return {"error": "--sublabel and --to (owner address) are required for create-subname"}
        return await action_create_subname(cfg, args.name, args.sublabel, args.to, wallet)
    if args.action == "set-primary":
        return await action_set_primary(cfg, args.name, wallet, args.chain)
    return {"error": f"Unknown action: {args.action}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="ENS Manager")
    parser.add_argument("--action", required=True, choices=["lookup", "check", "register", "renew", "transfer", "set-records", "create-subname", "set-primary"])
    parser.add_argument("--name", required=True, help=".eth name or address (for lookup)")
    parser.add_argument("--wallet", default=None, help="Wallet label from config (default: config.wallet)")
    parser.add_argument("--to", default=None, help="Recipient address (transfer / create-subname owner)")
    parser.add_argument("--duration", type=int, default=None, help="Registration/renewal duration in years")
    parser.add_argument("--key", default=None, help="Text record key (set-records)")
    parser.add_argument("--value", default=None, help="Text record value (set-records)")
    parser.add_argument("--sublabel", default=None, help="Sublabel to create (create-subname)")
    parser.add_argument("--chain", type=int, default=None, help="Chain ID for set-primary (default: Base 8453)")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    config_path = args.config or root / "inputs" / "config.yaml"

    load_config()

    result = asyncio.run(run(config_path, args))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
