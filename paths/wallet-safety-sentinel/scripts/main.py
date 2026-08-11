"""Wallet Safety Sentinel — scan core (Phase 2).

`scan-now` enumerates every ERC-20 `Approval` and ERC-721/1155 `ApprovalForAll`
the wallet has ever emitted (via topic-filtered `eth_getLogs`), re-reads the
*current* on-chain allowance / operator state to drop already-revoked grants,
and reports the live exposure. Read-only. USD pricing + risk scoring land in
later phases; this phase emits raw on-chain facts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from web3 import AsyncWeb3, Web3

from wayfinder_paths.core.config import get_etherscan_api_key, load_config
from wayfinder_paths.core.constants.chains import (
    CHAIN_CODE_TO_ID,
    CHAIN_EXPLORER_URLS,
    ETHERSCAN_V2_API_URL,
)
from wayfinder_paths.core.utils.retry import exponential_backoff_s
from wayfinder_paths.core.utils.units import from_erc20_raw
from wayfinder_paths.core.utils.web3 import web3s_from_chain_id

INPUTS_DIR = Path(__file__).resolve().parent.parent / "inputs"

APPROVAL_SIG = Web3.to_hex(Web3.keccak(text="Approval(address,address,uint256)"))
APPROVAL_FOR_ALL_SIG = Web3.to_hex(Web3.keccak(text="ApprovalForAll(address,address,bool)"))

# An allowance at/above this is treated as effectively unlimited (2**255 covers
# the common uint256-max grant without flagging large-but-finite approvals).
UNLIMITED_THRESHOLD = 1 << 255

# getLogs window controls. The Wayfinder RPC proxy caps eth_getLogs at ~50k blocks
# per query (see chains.yaml `log_span`) and rate-limits per API key. `_MIN_SPAN` is
# the floor we shrink to on a confirmed range cap; concurrency is bounded so a
# rate-limited run throttles instead of fanning out.
_MIN_SPAN = 2000
_RETRIES = 6
_MAX_CONCURRENCY = 4
_BACKOFF_BASE_S = 0.5
_BACKOFF_MAX_S = 8.0

# Explorer logs APIs (Etherscan V2 / Blockscout): discovery via one topic-filtered
# query per event type (paginated), off the RPC quota. Page size is the API max;
# pages are bounded so a pathological address can't run away.
_ETHERSCAN_PAGE = 1000
_ETHERSCAN_MAX_PAGES = 50


def _hx(value: Any) -> str:
    """Normalize web3 topic/bytes outputs to a lowercase 0x-prefixed hex string."""
    raw = value.hex() if hasattr(value, "hex") else str(value)
    raw = raw.lower()
    return raw if raw.startswith("0x") else "0x" + raw


def _addr_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").zfill(64)


def _topic_to_address(topic: Any) -> str:
    return Web3.to_checksum_address("0x" + _hx(topic)[-40:])


def _load_chain_config() -> dict[str, Any]:
    with open(INPUTS_DIR / "chains.yaml") as fh:
        return yaml.safe_load(fh)


class _RangeCapError(Exception):
    """Provider rejected a window because its block range / result set was too large."""


# Substrings that mark a "shrink the window" error, as opposed to a rate limit or a
# forbidden upstream (which must be retried, never split).
_RANGE_HINTS = (
    "block range",
    "range too",
    "exceed maximum block range",
    "too many results",
    "query returned more than",
    "response size",
    "limit exceeded",
)


def _is_range_cap(exc: Exception) -> bool:
    return any(hint in str(exc).lower() for hint in _RANGE_HINTS)


async def _block_number(pool: list[AsyncWeb3]) -> int:
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            return int(await pool[attempt % len(pool)].eth.block_number)
        except Exception as exc:  # noqa: BLE001 - rotate across the pool on transient errors
            last = exc
            await asyncio.sleep(
                exponential_backoff_s(
                    attempt, base_delay_s=_BACKOFF_BASE_S, max_delay_s=_BACKOFF_MAX_S
                )
            )
    raise TimeoutError(f"eth_blockNumber exhausted {_RETRIES} attempts: {last}")


async def _fetch_logs(
    pool: list[AsyncWeb3], params: dict[str, Any], rotate_from: int
) -> list[Any]:
    """One eth_getLogs, rotating across the proxy pool and backing off on transient or
    forbidden upstreams. A genuine range/result cap raises `_RangeCapError` so the caller
    can shrink the window; rate limits (429) and forbidden upstreams (403) are retried
    against other pool members — never split. That split-on-429 feedback loop is what
    exhausted the API key before.
    """
    n = len(pool)
    last: Exception | None = None
    for attempt in range(_RETRIES):
        w3 = pool[(rotate_from + attempt) % n]
        try:
            return list(await w3.eth.get_logs(params))
        except Exception as exc:  # noqa: BLE001 - classify range-cap vs transient below
            if _is_range_cap(exc):
                raise _RangeCapError(str(exc)) from exc
            last = exc
            await asyncio.sleep(
                exponential_backoff_s(
                    attempt, base_delay_s=_BACKOFF_BASE_S, max_delay_s=_BACKOFF_MAX_S
                )
            )
    raise TimeoutError(f"getLogs exhausted {_RETRIES} attempts: {last}")


async def _window_logs(
    pool: list[AsyncWeb3],
    topics: list[Any],
    frm: int,
    to: int,
    errors: list[str],
    sem: asyncio.Semaphore,
) -> list[Any]:
    """Fetch [frm, to], splitting only on a confirmed range cap (never on rate limits)."""
    async with sem:
        try:
            return await _fetch_logs(
                pool, {"fromBlock": frm, "toBlock": to, "topics": topics}, frm % len(pool)
            )
        except _RangeCapError:
            if to - frm <= _MIN_SPAN:
                errors.append(f"getLogs range cap at minimum span {frm}-{to}")
                return []
        except TimeoutError as exc:
            errors.append(f"getLogs skipped {frm}-{to}: {exc}")
            return []
    mid = (frm + to) // 2
    left, right = await asyncio.gather(
        _window_logs(pool, topics, frm, mid, errors, sem),
        _window_logs(pool, topics, mid + 1, to, errors, sem),
    )
    return left + right


async def _enumerate_logs(
    pool: list[AsyncWeb3],
    owner: str,
    frm: int,
    to: int,
    log_span: int,
    errors: list[str],
) -> list[Any]:
    """All Approval + ApprovalForAll logs emitted by `owner`, fanned out in fixed-size
    windows with bounded concurrency."""
    topics = [[APPROVAL_SIG, APPROVAL_FOR_ALL_SIG], _addr_topic(owner)]
    span = max(_MIN_SPAN, int(log_span))
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    chunks = await asyncio.gather(
        *(
            _window_logs(pool, topics, start, min(start + span - 1, to), errors, sem)
            for start in range(frm, to + 1, span)
        )
    )
    return [log for chunk in chunks for log in chunk]


async def _etherscan_logs(
    client: httpx.AsyncClient,
    chain_id: int,
    owner: str,
    topic0: str,
    frm: int,
    to: int,
) -> list[dict[str, Any]]:
    """One event type's logs for `owner` via the Etherscan V2 indexer, paginated.

    Discovery here is O(approvals) and runs off the RPC proxy, vs the O(blocks) RPC
    `getLogs` walk. Returns RPC-log-shaped dicts so `_classify_logs` consumes them
    unchanged. Raises on any API error so the caller can fall back to the RPC walk.
    """
    key = get_etherscan_api_key()
    out: list[dict[str, Any]] = []
    for page in range(1, _ETHERSCAN_MAX_PAGES + 1):
        params = {
            "chainid": str(int(chain_id)),
            "module": "logs",
            "action": "getLogs",
            "fromBlock": str(frm),
            "toBlock": str(to),
            "topic0": topic0,
            "topic1": _addr_topic(owner),
            "topic0_1_opr": "and",
            "page": str(page),
            "offset": str(_ETHERSCAN_PAGE),
            "apikey": key,
        }
        resp = await client.get(ETHERSCAN_V2_API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result")
        if str(data.get("status")) != "1":
            # status 0 with an empty list / "No records found" is just the end of pages.
            if result == [] or "no record" in str(data.get("message", "")).lower():
                break
            raise RuntimeError(f"etherscan getLogs: {result or data.get('message')}")
        if not isinstance(result, list) or not result:
            break
        out.extend(
            {
                "address": Web3.to_checksum_address(r["address"]),
                "topics": r["topics"],
                "data": r.get("data", "0x"),
                "blockNumber": int(r["blockNumber"], 16),
            }
            for r in result
        )
        if len(result) < _ETHERSCAN_PAGE:
            break
    return out


async def _discover_logs(
    pool: list[AsyncWeb3],
    chain_id: int,
    owner: str,
    frm: int,
    to: int,
    log_span: int,
    errors: list[str],
) -> list[Any]:
    """Discover Approval/ApprovalForAll logs: prefer the Etherscan V2 indexer (off the
    RPC proxy), fall back to the bounded RPC `getLogs` walk when no key is set or the
    indexer errors."""
    if get_etherscan_api_key():
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                logs: list[Any] = []
                for sig in (APPROVAL_SIG, APPROVAL_FOR_ALL_SIG):
                    logs.extend(
                        await _etherscan_logs(client, chain_id, owner, sig, frm, to)
                    )
                return logs
        except Exception as exc:  # noqa: BLE001 - any indexer failure → RPC fallback
            errors.append(f"etherscan discovery failed, using RPC getLogs: {exc}")
    return await _enumerate_logs(pool, owner, frm, to, log_span, errors)


async def _blockscout_logs(
    client: httpx.AsyncClient,
    base_url: str,
    owner: str,
    topic0: str,
    frm: int,
    to: int | str,
) -> list[dict[str, Any]]:
    """Approval logs for `owner` via a Blockscout instance's Etherscan-compatible logs
    API — no key, off the RPC quota. Returns RPC-log-shaped dicts for `_classify_logs`.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for page in range(1, _ETHERSCAN_MAX_PAGES + 1):
        params = {
            "module": "logs",
            "action": "getLogs",
            "fromBlock": str(frm),
            "toBlock": str(to),
            "topic0": topic0,
            "topic1": _addr_topic(owner),
            "topic0_1_opr": "and",
            "page": str(page),
            "offset": str(_ETHERSCAN_PAGE),
        }
        resp = await client.get(f"{base_url}/api", params=params)
        resp.raise_for_status()
        result = resp.json().get("result")
        if not isinstance(result, list) or not result:
            break
        added = 0
        for r in result:
            ref = (str(r.get("transactionHash")), str(r.get("logIndex")))
            if ref in seen:  # Blockscout may ignore paging and repeat the first page
                continue
            seen.add(ref)
            added += 1
            out.append(
                {
                    "address": Web3.to_checksum_address(r["address"]),
                    "topics": r["topics"],
                    "data": r.get("data") or "0x",
                    "blockNumber": int(r["blockNumber"], 16),
                    "logIndex": int(r.get("logIndex") or "0x0", 16),
                }
            )
        if added == 0 or len(result) < _ETHERSCAN_PAGE:
            break
    return out


async def _blockscout_balances(
    client: httpx.AsyncClient, base_url: str, owner: str
) -> dict[str, tuple[str, int, int]]:
    """All current ERC-20 balances + metadata for `owner` in one Blockscout call
    (off-quota). Maps checksummed token address -> (symbol, decimals, raw balance)."""
    resp = await client.get(f"{base_url}/api/v2/addresses/{owner}/token-balances")
    resp.raise_for_status()
    data = resp.json()
    items = data if isinstance(data, list) else data.get("items", [])
    out: dict[str, tuple[str, int, int]] = {}
    for it in items:
        tok = it.get("token") or {}
        if tok.get("type") not in ("ERC-20", None):
            continue
        addr = tok.get("address_hash") or tok.get("address")
        if not addr:
            continue
        try:
            out[Web3.to_checksum_address(addr)] = (
                tok.get("symbol") or "?",
                int(tok.get("decimals") or 18),
                int(it.get("value") or 0),
            )
        except (ValueError, TypeError):
            continue
    return out


def _classify_logs(
    logs: list[Any],
) -> tuple[
    dict[tuple[str, str], tuple[int, int, int]],
    dict[tuple[str, str], tuple[int, int, bool]],
]:
    """Reduce raw logs to the latest ERC-20 (token,spender)->allowance and NFT
    (token,operator)->approved state. The latest `Approval` event's value *is* the
    current set allowance (a revoke is an `Approval(owner,spender,0)` event), so no
    on-chain `allowance()`/`isApprovedForAll()` read is needed — only logs.
    """
    erc20: dict[tuple[str, str], tuple[int, int, int]] = {}  # (block, logindex, value)
    nft: dict[tuple[str, str], tuple[int, int, bool]] = {}  # (block, logindex, approved)
    for log in logs:
        topic0 = _hx(log["topics"][0])
        token = Web3.to_checksum_address(log["address"])
        block = int(log["blockNumber"])
        logindex = int(log.get("logIndex") or 0)
        counterparty = _topic_to_address(log["topics"][2])
        key = (token, counterparty)
        data = _hx(log["data"])
        value = int(data, 16) if data not in ("0x", "0x0", "") else 0
        if topic0 == APPROVAL_SIG:
            prev = erc20.get(key)
            if prev is None or (block, logindex) >= (prev[0], prev[1]):
                erc20[key] = (block, logindex, value)
        elif topic0 == APPROVAL_FOR_ALL_SIG:
            prevn = nft.get(key)
            if prevn is None or (block, logindex) >= (prevn[0], prevn[1]):
                nft[key] = (block, logindex, value != 0)
    return erc20, nft


def _resolve_from_logs(
    erc20: dict[tuple[str, str], tuple[int, int, int]],
    nft: dict[tuple[str, str], tuple[int, int, bool]],
    balances: dict[str, tuple[str, int, int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the approval report from classified logs + a token-balance map — no RPC.
    Allowance is the latest Approval event's value; balances come from one explorer
    call (empty map => balance unknown, reported as 0)."""
    approvals: list[dict[str, Any]] = []
    for (token, spender), (block, _li, value) in erc20.items():
        if value <= 0:  # current allowance revoked/zero
            continue
        symbol, decimals, balance = balances.get(token, ("?", 18, 0))
        reachable = min(value, balance)
        approvals.append(
            {
                "token": token,
                "symbol": symbol,
                "decimals": decimals,
                "spender": spender,
                "allowance_raw": str(value),
                "is_unlimited": value >= UNLIMITED_THRESHOLD,
                "balance_raw": str(balance),
                "exposure_tokens": from_erc20_raw(reachable, decimals),
                "approved_at_block": block,
            }
        )
    approvals.sort(key=lambda a: a["exposure_tokens"], reverse=True)
    nft_approvals = [
        {"token": token, "operator": operator, "approved": True, "approved_at_block": block}
        for (token, operator), (block, _li, approved) in nft.items()
        if approved
    ]
    return approvals, nft_approvals


async def _scan_chain(
    code: str,
    owner: str,
    window_days: int,
    deep: bool,
    chain_cfg: dict[str, Any],
) -> dict[str, Any]:
    chain_id = CHAIN_CODE_TO_ID[code]
    params = chain_cfg["chains"][code]
    errors: list[str] = []
    blockscout = params.get("blockscout_url")
    if blockscout:
        # Off-quota path: Blockscout serves full-history logs + all balances with no
        # key, so the entire scan touches zero Wayfinder RPC calls.
        async with httpx.AsyncClient(timeout=40) as client:
            logs: list[Any] = []
            for sig in (APPROVAL_SIG, APPROVAL_FOR_ALL_SIG):
                logs.extend(
                    await _blockscout_logs(client, blockscout, owner, sig, 0, "latest")
                )
            balances = await _blockscout_balances(client, blockscout, owner)
        erc20, nft = _classify_logs(logs)
        approvals, nft_approvals = _resolve_from_logs(erc20, nft, balances)
        scanned: list[Any] = [0, "latest"]
    else:
        # No Blockscout instance (e.g. BSC): discover via RPC getLogs, still derive the
        # allowance from logs (no allowance() reads). Balances aren't available off-quota.
        async with web3s_from_chain_id(chain_id) as pool:
            latest = await _block_number(pool)
            frm = 0 if deep else max(0, latest - window_days * int(params["blocks_per_day"]))
            logs = await _discover_logs(
                pool, chain_id, owner, frm, latest, int(params["log_span"]), errors
            )
        erc20, nft = _classify_logs(logs)
        approvals, nft_approvals = _resolve_from_logs(erc20, nft, {})
        errors.append("balances unavailable (no Blockscout instance for this chain)")
        scanned = [frm, latest]
    return {
        "chain_id": chain_id,
        "explorer": CHAIN_EXPLORER_URLS.get(chain_id),
        "scanned_blocks": scanned,
        "approvals": approvals,
        "nft_approvals": nft_approvals,
        "errors": errors,
    }


async def scan_now(
    owner: str, chains: list[str], window_days: int, deep: bool
) -> dict[str, Any]:
    owner = Web3.to_checksum_address(owner)
    chain_cfg = _load_chain_config()
    results: dict[str, Any] = {}
    for code in chains:
        if code not in CHAIN_CODE_TO_ID or code not in chain_cfg["chains"]:
            results[code] = {"error": f"unsupported chain '{code}'"}
            continue
        results[code] = await _scan_chain(code, owner, window_days, deep, chain_cfg)

    total = sum(len(c.get("approvals", [])) for c in results.values())
    unlimited = sum(
        1
        for c in results.values()
        for a in c.get("approvals", [])
        if a.get("is_unlimited")
    )
    nft_ops = sum(len(c.get("nft_approvals", [])) for c in results.values())
    return {
        "schema": "wallet-safety-sentinel/scan/0.1",
        "address": owner,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {"chains": chains, "window_days": window_days, "deep": deep},
        "chains": results,
        "summary": {
            "active_approvals": total,
            "unlimited_approvals": unlimited,
            "nft_operators": nft_ops,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="wallet-safety-sentinel")
    sub = parser.add_subparsers(dest="action", required=True)
    scan = sub.add_parser("scan-now", help="One-shot read-only approval scan.")
    scan.add_argument("--address", required=True, help="Wallet address (0x...).")
    scan.add_argument("--chains", help="Comma-separated chain codes (default from chains.yaml).")
    scan.add_argument("--window-days", type=int, default=547, help="Lookback window (default ~18mo).")
    scan.add_argument("--deep", action="store_true", help="Scan full history from block 0.")
    scan.add_argument("--config", help="Path to config.json.")
    args = parser.parse_args()

    load_config(args.config or os.environ.get("WAYFINDER_CONFIG_PATH", "config.json"))

    if args.address.endswith(".eth"):
        print("ENS names are not supported yet — pass a 0x address.", file=sys.stderr)
        raise SystemExit(2)

    chain_cfg = _load_chain_config()
    chains = (
        [c.strip() for c in args.chains.split(",") if c.strip()]
        if args.chains
        else chain_cfg["default_chains"]
    )
    report = asyncio.run(scan_now(args.address, chains, args.window_days, args.deep))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
