#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import functools
import hashlib
import html
import importlib
import json
import os
import re
import shlex
import shutil
import socketserver
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

PATH_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PATH_DIR.parents[1] if len(PATH_DIR.parents) > 1 else PATH_DIR
APY_NORMALIZATION_VERSION = "apy-normalization-v1"
SUPPORTED_STABLES = {"USDC", "USDT", "DAI"}
SUPPORTED_COMPARE_USE_CASES = {"stablecoin-lending", "lp", "perps", "restaking"}
ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{16,64}\b")

CHAIN_IDS: dict[str, int] = {
    "ethereum": 1,
    "mainnet": 1,
    "base": 8453,
    "arbitrum": 42161,
    "polygon": 137,
    "bsc": 56,
    "avalanche": 43114,
    "plasma": 9745,
    "hyperevm": 999,
    "optimism": 10,
}

PROTOCOL_DOMAINS: dict[str, str] = {
    "aave_v3_adapter": "aave.com",
    "morpho_adapter": "morpho.org",
    "moonwell_adapter": "moonwell.fi",
    "hyperliquid_adapter": "hyperliquid.xyz",
    "pendle_adapter": "pendle.finance",
    "polymarket_adapter": "polymarket.com",
    "aerodrome_adapter": "aerodrome.finance",
    "aerodrome_slipstream_adapter": "aerodrome.finance",
    "uniswap_adapter": "uniswap.org",
    "euler_v2_adapter": "euler.finance",
    "sparklend_adapter": "spark.fi",
    "hyperlend_adapter": "hyperlend.finance",
    "ethena_vault_adapter": "ethena.fi",
    "etherfi_adapter": "ether.fi",
    "lido_adapter": "lido.fi",
}

PRODUCT_LABELS: dict[str, str] = {
    "how-it-works": "Protocol mechanism",
    "stablecoin-rates": "Stablecoin rates",
    "market-snapshot": "Market snapshot",
    "compare-protocols": "Protocol comparison",
    "preview": "Infographic preview",
}

DENIED_READ_SUBSTRINGS = (
    "execute",
    "deposit",
    "withdraw",
    "lend",
    "unlend",
    "borrow",
    "repay",
    "stake",
    "unstake",
    "claim",
    "vote",
    "mint",
    "burn",
    "increase",
    "decrease",
    "transfer",
    "send",
    "open",
    "close",
    "swap",
    "bridge",
    "authorize",
    "reallocate",
)

READ_METHODS_BY_ACTION: dict[str, set[str]] = {
    "stablecoin-rates": {
        "get_all_markets",
        "get_all_vaults",
        "get_apy",
    },
    "market-snapshot": {
        "get_meta_and_asset_ctxs",
        "get_funding_history",
        "get_markets",
        "get_market",
        "get_orderbook",
        "get_pool",
        "get_vaults",
    },
    "compare-protocols": {
        "get_all_markets",
        "get_all_vaults",
        "get_apy",
        "get_meta_and_asset_ctxs",
        "get_markets",
        "get_pool",
        "get_vaults",
    },
}


JsonMap = dict[str, Any]


@dataclass(frozen=True)
class AdapterInfo:
    slug: str
    protocol: str
    manifest_path: Path
    readme_path: Path | None
    examples_path: Path | None
    entrypoint: str
    capabilities: list[str]
    support: JsonMap
    risks: list[str]


class PathRuntimeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: JsonMap | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class PublishConfig:
    provider: str
    project: str
    account_id: str
    api_token_present: bool
    branch: str
    include_data: bool
    required: bool


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PathRuntimeError("missing_required_arg", message)


def read_yaml(path: Path, *, default: Any = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    return loaded if loaded is not None else ({} if default is None else default)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_local_env_file(path: Path | None = None) -> None:
    env_path = path or (PATH_DIR / ".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        separator = "=" if "=" in line else ":" if ":" in line else None
        if separator is None:
            continue
        key, value = line.split(separator, 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def load_request_defaults() -> JsonMap:
    return read_yaml(PATH_DIR / "inputs" / "request.yaml", default={})


def load_style(style_name: str) -> JsonMap:
    styles = read_yaml(PATH_DIR / "inputs" / "style.yaml", default={}).get("styles") or {}
    style = styles.get(style_name)
    if not isinstance(style, dict):
        raise PathRuntimeError(
            "unsupported_use_case",
            f"Unknown style: {style_name}",
            details={"style": style_name, "available": sorted(styles)},
        )
    return style


def load_support() -> JsonMap:
    return read_yaml(PATH_DIR / "data" / "support.yaml", default={})


def load_risks() -> JsonMap:
    return read_yaml(PATH_DIR / "data" / "risks.yaml", default={})


def load_jobs() -> JsonMap:
    return read_yaml(PATH_DIR / "data" / "jobs.yaml", default={})


def load_mechanics() -> JsonMap:
    return read_yaml(PATH_DIR / "data" / "mechanics.yaml", default={})


def locate_adapters_root() -> Path:
    candidates = [
        REPO_ROOT / "wayfinder_paths" / "adapters",
        Path.cwd() / "wayfinder_paths" / "adapters",
        PATH_DIR / "wayfinder_paths" / "adapters",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    try:
        package = importlib.import_module("wayfinder_paths")
        package_path = Path(str(package.__file__)).resolve().parent
    except Exception as exc:  # noqa: BLE001
        raise PathRuntimeError(
            "internal_error",
            "Could not locate wayfinder_paths adapters.",
            details={"error": str(exc)},
        ) from exc

    adapters_root = package_path / "adapters"
    if not adapters_root.exists():
        raise PathRuntimeError(
            "internal_error",
            "Could not locate wayfinder_paths adapters.",
            details={"searched": [str(c) for c in candidates]},
        )
    return adapters_root


def cache_root() -> Path:
    defaults = load_request_defaults()
    root = str(defaults.get("cache_root") or ".wf-cache/infographic-composer")
    return PATH_DIR / root


def artifacts_root() -> Path:
    defaults = load_request_defaults()
    root = str(defaults.get("artifacts_root") or ".wf-artifacts")
    return PATH_DIR / root


def clean_slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-").lower() or "infographic"


def make_run_id(action: str, request: JsonMap) -> str:
    stamp = utc_now().strftime("%Y%m%d-%H%M%S-%f")
    seed = json.dumps(request, sort_keys=True, default=str) + stamp
    suffix = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return f"{clean_slug(action)}-{stamp}-{suffix}"


def list_adapter_inventory() -> list[JsonMap]:
    adapters_root = locate_adapters_root()
    support = load_support().get("adapters") or {}
    risks = load_risks().get("adapters") or {}
    inventory: list[JsonMap] = []
    for manifest_path in sorted(adapters_root.glob("*_adapter/manifest.yaml")):
        manifest = read_yaml(manifest_path, default={})
        slug = manifest_path.parent.name
        support_entry = support.get(slug) or {}
        risk_entry = risks.get(slug) or {}
        readme = manifest_path.parent / "README.md"
        examples = manifest_path.parent / "examples.json"
        inventory.append(
            {
                "slug": slug,
                "protocol": support_entry.get("protocol") or slug_to_protocol(slug),
                "manifest_path": str(manifest_path.relative_to(REPO_ROOT))
                if manifest_path.is_relative_to(REPO_ROOT)
                else str(manifest_path),
                "readme_path": str(readme.relative_to(REPO_ROOT))
                if readme.exists() and readme.is_relative_to(REPO_ROOT)
                else (str(readme) if readme.exists() else None),
                "examples_path": str(examples.relative_to(REPO_ROOT))
                if examples.exists() and examples.is_relative_to(REPO_ROOT)
                else (str(examples) if examples.exists() else None),
                "entrypoint": manifest.get("entrypoint"),
                "capabilities": list(manifest.get("capabilities") or []),
                "dependencies": list(manifest.get("dependencies") or []),
                "support": support_entry,
                "risk_catalog_present": bool(risk_entry.get("risks")),
                "manifest_mtime": manifest_path.stat().st_mtime,
            }
        )
    write_scan_cache(inventory)
    return inventory


def write_scan_cache(inventory: list[JsonMap]) -> None:
    path = cache_root() / "scan_cache.json"
    write_json(
        path,
        {
            "generated_at": iso_now(),
            "adapters_root": str(locate_adapters_root()),
            "manifest_count": len(inventory),
            "adapters": inventory,
        },
    )


def get_adapter_info(slug: str, inventory: list[JsonMap]) -> AdapterInfo:
    support = load_support().get("adapters") or {}
    risks = load_risks().get("adapters") or {}
    found = next((item for item in inventory if item.get("slug") == slug), None)
    if not found:
        raise PathRuntimeError(
            "unsupported_adapter",
            f"Protocol is unsupported in v0.1: {slug}",
            details={"adapter": slug, "available": sorted(i["slug"] for i in inventory)},
        )
    risk_entry = risks.get(slug) or {}
    risk_list = risk_entry.get("risks")
    if not isinstance(risk_list, list) or not risk_list:
        raise PathRuntimeError(
            "risk_catalog_missing",
            f"Risk catalog entry is missing for adapter: {slug}",
            details={"adapter": slug, "file": "data/risks.yaml"},
        )
    manifest_path = resolve_repo_path(str(found["manifest_path"]))
    readme_path = resolve_optional_repo_path(found.get("readme_path"))
    examples_path = resolve_optional_repo_path(found.get("examples_path"))
    return AdapterInfo(
        slug=slug,
        protocol=str((support.get(slug) or {}).get("protocol") or found.get("protocol") or slug_to_protocol(slug)),
        manifest_path=manifest_path,
        readme_path=readme_path,
        examples_path=examples_path,
        entrypoint=str(found.get("entrypoint") or ""),
        capabilities=list(found.get("capabilities") or []),
        support=support.get(slug) or {},
        risks=[str(item) for item in risk_list],
    )


def resolve_repo_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return REPO_ROOT / path


def resolve_optional_repo_path(path_value: Any) -> Path | None:
    if not path_value:
        return None
    path = resolve_repo_path(str(path_value))
    return path if path.exists() else None


def slug_to_protocol(slug: str) -> str:
    name = slug.removesuffix("_adapter").replace("_", " ")
    return " ".join(part.capitalize() for part in name.split())


def validate_read_method(action: str, method: str) -> None:
    lowered = method.lower()
    for token in DENIED_READ_SUBSTRINGS:
        if token in lowered:
            raise PathRuntimeError(
                "unsupported_use_case",
                f"Method is not allowed for this read-only path: {method}",
                details={"action": action, "method": method, "denied_token": token},
            )
    allowed = READ_METHODS_BY_ACTION.get(action) or set()
    if allowed and method not in allowed:
        raise PathRuntimeError(
            "unsupported_use_case",
            f"Method is not allowlisted for action {action}: {method}",
            details={"action": action, "method": method, "allowlist": sorted(allowed)},
        )


def load_source_artifact(path_value: str | None) -> JsonMap | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise PathRuntimeError(
            "missing_required_arg",
            f"Source artifact not found: {path}",
            details={"source_artifact": str(path)},
        )
    if path.suffix.lower() in {".yaml", ".yml"}:
        loaded = read_yaml(path, default={})
    else:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise PathRuntimeError(
            "missing_required_arg",
            "Source artifact must contain a JSON/YAML object.",
            details={"source_artifact": str(path)},
        )
    return loaded


def chain_id_for(chain: str | int | None) -> int | None:
    if chain is None or chain == "":
        return None
    if isinstance(chain, int):
        return chain
    chain_text = str(chain).strip().lower()
    if chain_text.isdigit():
        return int(chain_text)
    if chain_text not in CHAIN_IDS:
        raise PathRuntimeError(
            "unsupported_use_case",
            f"Unsupported chain: {chain}",
            details={"chain": chain, "known": sorted(CHAIN_IDS)},
        )
    return CHAIN_IDS[chain_text]


def chain_ids_for(chain: str | None, *, default: list[int]) -> list[int]:
    cid = chain_id_for(chain)
    return [cid] if cid is not None else default


class DocTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.in_ignored = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag.lower() == "title":
            self.in_title = True
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.in_ignored = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.in_ignored = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text or self.in_ignored:
            return
        if self.in_title:
            self.title_parts.append(text)
        elif len(text) > 20:
            self.text_parts.append(text)


def fetch_doc_metadata(url: str, *, timeout: float = 4.0) -> JsonMap:
    request = Request(
        url,
        headers={
            "User-Agent": "wayfinder-infographic-composer/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(256_000)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "url": url, "error": str(exc)}

    parser = DocTextParser()
    try:
        parser.feed(raw.decode("utf-8", errors="ignore"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc)}

    title = " ".join(parser.title_parts).strip()
    text = " ".join(parser.text_parts)
    text = re.sub(r"\s+", " ", text).strip()
    snippets: list[str] = []
    for part in parser.text_parts:
        cleaned = re.sub(r"\s+", " ", part).strip()
        if len(cleaned) < 50:
            continue
        if any(
            token in cleaned.lower()
            for token in ("cookie", "privacy", "newsletter", "copyright")
        ):
            continue
        snippets.append(cleaned[:180])
        if len(snippets) >= 3:
            break
    return {
        "ok": True,
        "url": url,
        "title": title[:160],
        "snippets": snippets,
        "text_sample_chars": len(text),
    }


async def load_protocol_doc_metadata(
    *,
    urls: list[str],
    docs_mode: str,
) -> tuple[list[JsonMap], list[str]]:
    if docs_mode == "off":
        return [], []
    docs = [{"ok": True, "url": url, "title": "", "mode": "metadata"} for url in urls]
    warnings: list[str] = []
    if docs_mode != "fetch":
        return docs, warnings

    fetched: list[JsonMap] = []
    for url in urls[:3]:
        result = await asyncio.to_thread(fetch_doc_metadata, url)
        fetched.append(result)
        if not result.get("ok"):
            warnings.append(f"Could not fetch protocol docs metadata: {url}")
    return fetched, warnings


def format_rate(value: Any) -> str:
    rate = coerce_float(value)
    if rate is None:
        return "unavailable"
    return f"{rate:.2%}"


def format_usd(value: Any) -> str:
    amount = coerce_float(value)
    if amount is None:
        return "unavailable"
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.2f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.2f}K"
    return f"${amount:,.0f}"


def coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_rate(value: Any) -> tuple[float | None, str | None]:
    raw = coerce_float(value)
    if raw is None:
        return None, None
    if abs(raw) > 1.0 and abs(raw) <= 100.0:
        return raw / 100.0, "Interpreted source value as a percentage because absolute value was greater than 1."
    return raw, None


def get_path_value(record: JsonMap, paths: list[str]) -> Any:
    for path in paths:
        current: Any = record
        for segment in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(segment)
        if current is not None:
            return current
    return None


def flatten_records(value: Any) -> list[JsonMap]:
    records: list[JsonMap] = []
    if isinstance(value, list):
        for item in value:
            records.extend(flatten_records(item))
        return records
    if isinstance(value, dict):
        if any(
            key in value
            for key in (
                "symbol",
                "asset",
                "loan",
                "state",
                "underlying",
                "uniqueKey",
                "address",
                "mtoken",
            )
        ):
            records.append(value)
        for key in ("markets", "vaults", "items", "data", "positions", "rows"):
            child = value.get(key)
            if isinstance(child, (list, dict)):
                records.extend(flatten_records(child))
    return records


def symbol_candidates(record: JsonMap) -> set[str]:
    candidates: set[str] = set()
    raw_values = [
        get_path_value(record, ["symbol", "symbol_canonical", "underlyingSymbol"]),
        get_path_value(record, ["asset.symbol"]),
        get_path_value(record, ["loan.symbol"]),
        get_path_value(record, ["collateral.symbol"]),
        get_path_value(record, ["token.symbol"]),
        get_path_value(record, ["name"]),
    ]
    for raw in raw_values:
        if not raw:
            continue
        text = str(raw).upper()
        candidates.add(text)
        candidates.update(part for part in re.split(r"[^A-Z0-9]+", text) if part)
    return candidates


def matches_asset(record: JsonMap, asset: str) -> bool:
    target = asset.upper()
    candidates = symbol_candidates(record)
    return any(candidate == target or candidate.endswith(target) for candidate in candidates)


def extract_reward_tokens(record: JsonMap) -> list[str]:
    incentives = get_path_value(record, ["incentives", "state.incentives"]) or []
    tokens: list[str] = []
    if isinstance(incentives, list):
        for incentive in incentives:
            if not isinstance(incentive, dict):
                continue
            token = (
                get_path_value(incentive, ["symbol", "token.symbol", "token"])
                or get_path_value(incentive, ["asset.symbol", "asset.address"])
            )
            if token:
                tokens.append(str(token))
    rewards = get_path_value(record, ["rewards", "state.rewards"]) or []
    if isinstance(rewards, list):
        for reward in rewards:
            if isinstance(reward, dict):
                token = get_path_value(reward, ["symbol", "asset.symbol", "token"])
                if token:
                    tokens.append(str(token))
    return sorted(set(tokens))


def normalize_rate_row(
    *,
    adapter: str,
    protocol: str,
    method: str,
    record: JsonMap,
    asset: str,
    chain_id: int | None,
) -> JsonMap | None:
    if not matches_asset(record, asset):
        return None

    gross_source = get_path_value(
        record,
        [
            "supply_apy",
            "state.supply_apy",
            "state.apy",
            "baseSupplyApy",
            "supplyApy",
            "apy",
        ],
    )
    gross_apy, gross_note = normalize_rate(gross_source)
    reward_source = get_path_value(
        record,
        [
            "reward_apy",
            "rewardSupplyApy",
            "state.reward_supply_apr",
            "reward_supply_apr",
        ],
    )
    reward_apy, reward_note = normalize_rate(reward_source)
    if reward_apy is None:
        with_rewards, _ = normalize_rate(
            get_path_value(
                record,
                [
                    "supply_apy_with_rewards",
                    "state.supply_apy_with_rewards",
                    "state.apy_with_rewards",
                    "apy_with_rewards",
                ],
            )
        )
        if gross_apy is not None and with_rewards is not None:
            reward_apy = max(0.0, with_rewards - gross_apy)
    if reward_apy is None:
        reward_apy = 0.0

    net_source = get_path_value(
        record,
        [
            "net_supply_apy",
            "state.net_supply_apy",
            "state.net_apy",
            "net_apy",
            "state.netApy",
        ],
    )
    net_apy, net_note = normalize_rate(net_source)
    fee_apy: float | None = None
    fee_status = "unknown"
    if net_apy is None and gross_apy is not None and adapter in {
        "aave_v3_adapter",
        "moonwell_adapter",
    }:
        fee_apy = 0.0
        fee_status = "included_in_protocol_rate"
        net_apy = gross_apy + reward_apy
    elif net_apy is not None and gross_apy is not None:
        fee_apy = gross_apy + reward_apy - net_apy
        fee_status = "adapter_reported"

    if gross_apy is None and net_apy is None:
        return None

    notes = [
        "Point-in-time protocol read.",
        "Rates are decimals in output JSON.",
    ]
    for note in (gross_note, reward_note, net_note):
        if note:
            notes.append(note)
    if fee_status == "unknown":
        notes.append("Fees were not exposed by the source, so this row is excluded from rankings.")

    market_id = (
        get_path_value(record, ["uniqueKey", "address", "mtoken", "underlying", "a_token"])
        or get_path_value(record, ["symbol", "asset.symbol", "loan.symbol"])
        or "unknown"
    )
    tvl_usd = get_path_value(
        record,
        [
            "tvl_usd",
            "totalSupplyUsd",
            "total_assets_usd",
            "state.total_assets_usd",
            "state.totalAssetsUsd",
            "liquidity_usd",
            "state.liquidity_assets_usd",
        ],
    )
    return {
        "apy_normalization_version": APY_NORMALIZATION_VERSION,
        "venue": adapter,
        "protocol": protocol,
        "chain_id": int(chain_id or get_path_value(record, ["chain_id", "chainId"]) or 0)
        or None,
        "asset": asset,
        "market_id": str(market_id),
        "rate_type": "apy",
        "rate_kind": "supply",
        "gross_apy": gross_apy,
        "net_apy": net_apy,
        "fee_apy": fee_apy,
        "fee_status": fee_status,
        "reward_apy": reward_apy,
        "reward_tokens": extract_reward_tokens(record),
        "compounding": "adapter-reported",
        "sampling_window": "point-in-time",
        "timestamp": iso_now(),
        "source": {
            "adapter": adapter,
            "method": method,
        },
        "normalization_notes": notes,
        "tvl_usd": coerce_float(tvl_usd),
        "raw_symbol_candidates": sorted(symbol_candidates(record)),
    }


def summarize_market_payload(
    *,
    adapter: str,
    protocol: str,
    method: str,
    payload: Any,
    chain_id: int | None,
) -> JsonMap:
    records = flatten_records(payload)
    stable_records = [
        record
        for record in records
        if symbol_candidates(record).intersection(SUPPORTED_STABLES)
    ]
    supply_values: list[float] = []
    tvl_values: list[float] = []
    for record in records:
        supply, _ = normalize_rate(
            get_path_value(
                record,
                [
                    "supply_apy",
                    "state.supply_apy",
                    "state.apy",
                    "baseSupplyApy",
                    "supplyApy",
                    "apy",
                ],
            )
        )
        if supply is not None:
            supply_values.append(supply)
        tvl = coerce_float(
            get_path_value(
                record,
                [
                    "tvl_usd",
                    "totalSupplyUsd",
                    "total_assets_usd",
                    "state.total_assets_usd",
                    "state.totalAssetsUsd",
                    "liquidity_usd",
                    "state.liquidity_assets_usd",
                ],
            )
        )
        if tvl is not None:
            tvl_values.append(tvl)
    return {
        "adapter": adapter,
        "protocol": protocol,
        "method": method,
        "chain_id": chain_id,
        "market_count": len(records),
        "stable_market_count": len(stable_records),
        "best_supply_apy": max(supply_values) if supply_values else None,
        "median_supply_apy": sorted(supply_values)[len(supply_values) // 2]
        if supply_values
        else None,
        "total_tvl_usd": sum(tvl_values) if tvl_values else None,
        "timestamp": iso_now(),
    }


async def fetch_how_it_works_metrics(
    *,
    info: AdapterInfo,
    chain: str | None,
    metrics_mode: str,
) -> tuple[list[JsonMap], list[JsonMap]]:
    if metrics_mode == "off":
        return [], []
    class_paths = {
        "aave_v3_adapter": "wayfinder_paths.adapters.aave_v3_adapter.adapter.AaveV3Adapter",
        "morpho_adapter": "wayfinder_paths.adapters.morpho_adapter.adapter.MorphoAdapter",
        "moonwell_adapter": "wayfinder_paths.adapters.moonwell_adapter.adapter.MoonwellAdapter",
        "hyperliquid_adapter": "wayfinder_paths.adapters.hyperliquid_adapter.adapter.HyperliquidAdapter",
    }
    class_path = class_paths.get(info.slug)
    if not class_path:
        return [], [
            {
                "adapter": info.slug,
                "status": "template-compatible-unverified",
                "message": "Live how-it-works metrics are not implemented for this protocol in v0.1.",
            }
        ]

    calls: list[tuple[str, JsonMap, int | None]] = []
    if info.slug == "aave_v3_adapter":
        cid = chain_id_for(chain) or 8453
        calls.append(("get_all_markets", {"chain_id": cid, "include_rewards": True}, cid))
    elif info.slug == "morpho_adapter":
        cid = chain_id_for(chain) or 8453
        calls.append(("get_all_markets", {"chain_id": cid}, cid))
        calls.append(("get_all_vaults", {"chain_id": cid, "include_v2": True}, cid))
    elif info.slug == "moonwell_adapter":
        cid = chain_id_for(chain) or 8453
        if cid == 8453:
            calls.append(
                (
                    "get_all_markets",
                    {
                        "include_apy": True,
                        "include_rewards": True,
                        "include_usd": True,
                    },
                    cid,
                )
            )
        else:
            return [], [
                {
                    "adapter": info.slug,
                    "method": "get_all_markets",
                    "chain_id": cid,
                    "error": "Moonwell v0.1 live metrics are Base-only.",
                }
            ]
    elif info.slug == "hyperliquid_adapter":
        calls.append(("get_meta_and_asset_ctxs", {}, None))

    metrics: list[JsonMap] = []
    failures: list[JsonMap] = []
    for method_name, kwargs, cid in calls:
        ok, payload = await maybe_call_adapter_method(
            action="how-it-works",
            adapter_slug=info.slug,
            class_path=class_path,
            method_name=method_name,
            timeout=8,
            kwargs=kwargs,
        )
        if not ok:
            failures.append(
                {
                    "adapter": info.slug,
                    "method": method_name,
                    "chain_id": cid,
                    "error": str(payload),
                }
            )
            continue
        if info.slug == "hyperliquid_adapter" and isinstance(payload, list) and len(payload) >= 2:
            meta = payload[0] if isinstance(payload[0], dict) else {}
            ctxs = payload[1] if isinstance(payload[1], list) else []
            metrics.append(
                {
                    "adapter": info.slug,
                    "protocol": info.protocol,
                    "method": method_name,
                    "market_count": len(list(meta.get("universe") or [])),
                    "context_count": len(ctxs),
                    "timestamp": iso_now(),
                }
            )
            continue
        metrics.append(
            summarize_market_payload(
                adapter=info.slug,
                protocol=info.protocol,
                method=method_name,
                payload=payload,
                chain_id=cid,
            )
        )
    return metrics, failures


def build_visual_metrics(
    *,
    info: AdapterInfo,
    profile: JsonMap,
    doc_count: int,
    live_metrics: list[JsonMap],
) -> list[JsonMap]:
    read_count = sum(1 for cap in info.capabilities if is_read_capability(cap))
    execution_count = sum(1 for cap in info.capabilities if not is_read_capability(cap))
    actors = len(list(profile.get("actors") or []))
    state = len(list(profile.get("state") or []))
    out: list[JsonMap] = [
        {
            "label": "Read surfaces",
            "value": str(read_count),
            "subvalue": f"{execution_count} blocked writes",
            "score": min(1.0, read_count / max(1, read_count + execution_count)),
            "kind": "donut",
        },
        {
            "label": "Mechanism map",
            "value": f"{actors}+{state}",
            "subvalue": "actors + state",
            "score": min(1.0, (actors + state) / 10),
            "kind": "bar",
        },
        {
            "label": "Docs linked",
            "value": str(doc_count),
            "subvalue": "official references",
            "score": min(1.0, doc_count / 3),
            "kind": "bar",
        },
    ]
    if live_metrics:
        market_count = sum(int(item.get("market_count") or 0) for item in live_metrics)
        stable_count = sum(int(item.get("stable_market_count") or 0) for item in live_metrics)
        best_rates = [
            float(item["best_supply_apy"])
            for item in live_metrics
            if item.get("best_supply_apy") is not None
        ]
        out.append(
            {
                "label": "Live markets",
                "value": str(market_count),
                "subvalue": f"{stable_count} stable",
                "score": min(1.0, market_count / 40),
                "kind": "bars",
            }
        )
        if best_rates:
            out.append(
                {
                    "label": "Best supply",
                    "value": format_rate(max(best_rates)),
                    "subvalue": "live read",
                    "score": min(1.0, max(best_rates) / 0.15),
                    "kind": "gauge",
                }
            )
    else:
        out.append(
            {
                "label": "Live metrics",
                "value": "unavailable",
                "subvalue": "see live_snapshot",
                "score": 0.12,
                "kind": "bar",
            }
        )
    return out[:5]


async def maybe_call_adapter_method(
    *,
    action: str,
    adapter_slug: str,
    class_path: str,
    method_name: str,
    timeout: float,
    kwargs: JsonMap,
) -> tuple[bool, Any]:
    validate_read_method(action, method_name)
    module_name, class_name = class_path.rsplit(".", 1)
    try:
        module = importlib.import_module(module_name)
        adapter_cls = getattr(module, class_name)
        adapter = adapter_cls(config={})
        method = getattr(adapter, method_name)
        result = await asyncio.wait_for(method(**kwargs), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, {
            "adapter": adapter_slug,
            "method": method_name,
            "error": str(exc),
            "kwargs": kwargs,
        }
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[0], bool)
    ):
        return bool(result[0]), result[1]
    return True, result


async def fetch_stablecoin_rows_for_adapter(
    *,
    info: AdapterInfo,
    asset: str,
    chain: str | None,
    min_tvl: float,
    action: str,
) -> tuple[list[JsonMap], list[JsonMap]]:
    rows: list[JsonMap] = []
    failures: list[JsonMap] = []
    chains_default = [8453, 42161, 1]
    class_paths = {
        "aave_v3_adapter": "wayfinder_paths.adapters.aave_v3_adapter.adapter.AaveV3Adapter",
        "morpho_adapter": "wayfinder_paths.adapters.morpho_adapter.adapter.MorphoAdapter",
        "moonwell_adapter": "wayfinder_paths.adapters.moonwell_adapter.adapter.MoonwellAdapter",
    }
    if info.slug not in class_paths:
        failures.append(
            {
                "adapter": info.slug,
                "status": "template-compatible-unverified",
                "message": "Live stablecoin-rate read is not implemented for this protocol in v0.1.",
            }
        )
        return rows, failures

    method_calls: list[tuple[str, JsonMap, int | None]] = []
    if info.slug == "aave_v3_adapter":
        for cid in chain_ids_for(chain, default=chains_default):
            method_calls.append(("get_all_markets", {"chain_id": cid, "include_rewards": True}, cid))
    elif info.slug == "morpho_adapter":
        for cid in chain_ids_for(chain, default=[8453, 1]):
            method_calls.append(("get_all_markets", {"chain_id": cid}, cid))
            method_calls.append(("get_all_vaults", {"chain_id": cid, "include_v2": True}, cid))
    elif info.slug == "moonwell_adapter":
        cid = chain_id_for(chain)
        if cid is None or cid == 8453:
            method_calls.append(
                (
                    "get_all_markets",
                    {
                        "include_apy": True,
                        "include_rewards": True,
                        "include_usd": True,
                    },
                    8453,
                )
            )
        else:
            failures.append(
                {
                    "adapter": info.slug,
                    "method": "get_all_markets",
                    "error": f"Moonwell v0.1 implementation is Base-only; requested chain_id={cid}.",
                }
            )

    for method_name, kwargs, cid in method_calls:
        ok, payload = await maybe_call_adapter_method(
            action=action,
            adapter_slug=info.slug,
            class_path=class_paths[info.slug],
            method_name=method_name,
            timeout=20,
            kwargs=kwargs,
        )
        if not ok:
            failures.append(
                {
                    "adapter": info.slug,
                    "method": method_name,
                    "chain_id": cid,
                    "error": str(payload),
                }
            )
            continue
        for record in flatten_records(payload):
            row = normalize_rate_row(
                adapter=info.slug,
                protocol=info.protocol,
                method=method_name,
                record=record,
                asset=asset,
                chain_id=cid,
            )
            if row is None:
                continue
            tvl_usd = coerce_float(row.get("tvl_usd"))
            if min_tvl and tvl_usd is not None and tvl_usd < min_tvl:
                continue
            rows.append(row)
    return rows, failures


async def fetch_stablecoin_rates(
    *,
    adapters: list[AdapterInfo],
    asset: str,
    chain: str | None,
    min_tvl: float,
    action: str,
) -> tuple[list[JsonMap], list[JsonMap]]:
    all_rows: list[JsonMap] = []
    failures: list[JsonMap] = []
    for info in adapters:
        rows, adapter_failures = await fetch_stablecoin_rows_for_adapter(
            info=info,
            asset=asset,
            chain=chain,
            min_tvl=min_tvl,
            action=action,
        )
        all_rows.extend(rows)
        failures.extend(adapter_failures)
    return all_rows, failures


async def fetch_hyperliquid_market_snapshot(market: str) -> tuple[JsonMap | None, list[JsonMap]]:
    ok, payload = await maybe_call_adapter_method(
        action="market-snapshot",
        adapter_slug="hyperliquid_adapter",
        class_path="wayfinder_paths.adapters.hyperliquid_adapter.adapter.HyperliquidAdapter",
        method_name="get_meta_and_asset_ctxs",
        timeout=20,
        kwargs={},
    )
    if not ok:
        return None, [{"adapter": "hyperliquid_adapter", "method": "get_meta_and_asset_ctxs", "error": str(payload)}]
    try:
        meta, ctxs = payload
        universe = list((meta or {}).get("universe") or [])
        market_upper = market.upper()
        for index, item in enumerate(universe):
            if str(item.get("name") or "").upper() != market_upper:
                continue
            ctx = ctxs[index] if index < len(ctxs) else {}
            return {
                "market": market_upper,
                "name": item.get("name"),
                "sz_decimals": item.get("szDecimals"),
                "max_leverage": item.get("maxLeverage"),
                "only_isolated": item.get("onlyIsolated"),
                "mark_price": ctx.get("markPx"),
                "oracle_price": ctx.get("oraclePx"),
                "funding": ctx.get("funding"),
                "open_interest": ctx.get("openInterest"),
                "day_volume": ctx.get("dayNtlVlm"),
                "source": {
                    "adapter": "hyperliquid_adapter",
                    "method": "get_meta_and_asset_ctxs",
                },
                "timestamp": iso_now(),
            }, []
    except Exception as exc:  # noqa: BLE001
        return None, [{"adapter": "hyperliquid_adapter", "method": "get_meta_and_asset_ctxs", "error": str(exc)}]
    return None, [{"adapter": "hyperliquid_adapter", "method": "get_meta_and_asset_ctxs", "error": f"Market not found in live payload: {market}"}]


def hyperliquid_info_request(payload: JsonMap, *, timeout: float = 8.0) -> Any:
    request = Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": "wayfinder-infographic-composer/0.1",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


async def fetch_hyperliquid_market_graphs(market: str) -> tuple[JsonMap, list[JsonMap]]:
    market_upper = market.upper()
    end_ms = int(utc_now().timestamp() * 1000)
    start_ms = end_ms - 24 * 60 * 60 * 1000
    failures: list[JsonMap] = []
    out: JsonMap = {"chart_points": [], "book": [], "book_pill": "live depth"}

    try:
        candles = await asyncio.to_thread(
            hyperliquid_info_request,
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": market_upper,
                    "interval": "1h",
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            },
        )
        if isinstance(candles, list):
            out["chart_points"] = [
                {
                    "t": int(item.get("t") or 0),
                    "p": coerce_float(item.get("c")),
                    "v": coerce_float(item.get("v")),
                }
                for item in candles
                if isinstance(item, dict) and coerce_float(item.get("c")) is not None
            ][-32:]
    except Exception as exc:  # noqa: BLE001
        failures.append({"source": "hyperliquid", "read": "candles", "error": str(exc)})

    try:
        book = await asyncio.to_thread(
            hyperliquid_info_request,
            {"type": "l2Book", "coin": market_upper},
        )
        levels = book.get("levels") if isinstance(book, dict) else None
        if isinstance(levels, list) and len(levels) >= 2:
            bids = [lvl for lvl in levels[0] if isinstance(lvl, dict)]
            asks = [lvl for lvl in levels[1] if isinstance(lvl, dict)]
            top_bids = bids[:5]
            top_asks = asks[:5]
            max_size = max(
                [coerce_float(lvl.get("sz")) or 0.0 for lvl in top_bids + top_asks]
                or [1.0]
            )
            rows: list[JsonMap] = []
            for lvl in reversed(top_asks):
                size = coerce_float(lvl.get("sz")) or 0.0
                price = coerce_float(lvl.get("px"))
                rows.append(
                    {
                        "side": "ask",
                        "price": f"{price:,.1f}" if price is not None else "—",
                        "size": f"{size:,.4g}",
                        "total": str(lvl.get("n") or ""),
                        "bg_width": max(8, (size / max(max_size, 0.0001)) * 100),
                    }
                )
            best_bid = coerce_float(top_bids[0].get("px")) if top_bids else None
            best_ask = coerce_float(top_asks[0].get("px")) if top_asks else None
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2
                spread = best_ask - best_bid
                rows.append(
                    {
                        "mid": True,
                        "price": f"{mid:,.1f}",
                        "spread": f"spread {spread:,.2f}",
                    }
                )
            for lvl in top_bids:
                size = coerce_float(lvl.get("sz")) or 0.0
                price = coerce_float(lvl.get("px"))
                rows.append(
                    {
                        "side": "bid",
                        "price": f"{price:,.1f}" if price is not None else "—",
                        "size": f"{size:,.4g}",
                        "total": str(lvl.get("n") or ""),
                        "bg_width": max(8, (size / max(max_size, 0.0001)) * 100),
                    }
                )
            out["book"] = rows
    except Exception as exc:  # noqa: BLE001
        failures.append({"source": "hyperliquid", "read": "orderbook", "error": str(exc)})
        out["book_pill"] = "depth unavailable"

    return out, failures


def infer_adapter_category(info: AdapterInfo) -> str:
    caps = " ".join(info.capabilities).lower()
    use_cases = set(info.support.get("use_cases") or [])
    if "stablecoin-lending" in use_cases or "lending" in caps:
        return "Lending"
    if "lp" in use_cases or "pool" in caps or "liquidity" in caps:
        return "Liquidity"
    if "perps" in use_cases or "funding" in caps or "order" in caps:
        return "Perpetuals"
    if "restaking" in use_cases or "staking" in caps or "vault" in caps:
        return "Staking and Vaults"
    if "prediction-markets" in use_cases:
        return "Prediction Markets"
    return "Protocol"


def describe_protocol(info: AdapterInfo) -> str:
    category = infer_adapter_category(info)
    read_caps = [cap for cap in info.capabilities if is_read_capability(cap)]
    parts = [f"{info.protocol} is a {category.lower()} protocol."]
    if read_caps:
        parts.append(f"This snapshot can summarize {len(read_caps)} protocol state surface(s).")
    return " ".join(parts)


def is_read_capability(capability: str) -> bool:
    lowered = capability.lower()
    if any(token in lowered for token in DENIED_READ_SUBSTRINGS):
        return False
    return any(token in lowered for token in ("read", "list", "meta", "market", "history", "candles", "orderbook", "state", "quote"))


def group_capabilities(capabilities: list[str]) -> JsonMap:
    groups: JsonMap = {"read": [], "market": [], "position": [], "execution": [], "other": []}
    for cap in capabilities:
        lowered = cap.lower()
        if not is_read_capability(cap):
            groups["execution"].append(cap)
        elif "market" in lowered or "funding" in lowered or "pool" in lowered:
            groups["market"].append(cap)
        elif "position" in lowered or "vault" in lowered:
            groups["position"].append(cap)
        elif "read" in lowered or "list" in lowered or "meta" in lowered:
            groups["read"].append(cap)
        else:
            groups["other"].append(cap)
    return groups


def mechanism_profile_for(info: AdapterInfo) -> JsonMap:
    mechanics = load_mechanics()
    adapter_profiles = mechanics.get("adapters") or {}
    category_profiles = mechanics.get("categories") or {}
    profile = adapter_profiles.get(info.slug)
    if isinstance(profile, dict):
        return profile
    fallback = category_profiles.get(infer_adapter_category(info))
    if isinstance(fallback, dict):
        return fallback
    return {
        "primitive": infer_adapter_category(info),
        "mechanism_summary": describe_protocol(info),
        "mechanism_steps": mechanics_for(info),
        "actors": [],
        "state": [],
        "official_docs": [],
        "adapter_boundary": [
            "Protocol surfaces are summarized from checked protocol metadata.",
            "This infographic only uses read-only protocol and market data.",
        ],
    }


def merge_source_mechanism(profile: JsonMap, source_artifact: JsonMap | None) -> JsonMap:
    if not source_artifact:
        return profile
    merged = dict(profile)
    for key in (
        "primitive",
        "mechanism_summary",
        "mechanism_steps",
        "actors",
        "state",
        "official_docs",
        "adapter_boundary",
    ):
        value = source_artifact.get(key)
        if value:
            merged[key] = value
    return merged


def mechanics_for(info: AdapterInfo) -> list[str]:
    category = infer_adapter_category(info)
    if category == "Lending":
        return [
            "Markets expose token supply, borrow demand, liquidity, and risk parameters.",
            "Suppliers receive protocol-reported supply APY and any exposed reward yield.",
            "Borrow and collateral flows depend on protocol collateral rules, oracle pricing, and liquidation thresholds.",
            "Rates are read at a point in time and can change as utilization changes.",
        ]
    if category == "Liquidity":
        return [
            "Pools pair one or more assets and quote available liquidity, fees, or pool state.",
            "LP outcomes depend on trading volume, fee tier, incentives, and price movement.",
            "Concentrated liquidity positions can move out of range when the market moves.",
        ]
    if category == "Perpetuals":
        return [
            "Perp markets expose mark price, oracle price, funding, open interest, and orderbook context when available.",
            "Funding can be positive or negative and can change quickly.",
            "Market snapshots use read-only market data and do not place orders.",
        ]
    if category == "Staking and Vaults":
        return [
            "Vault or staking positions transform deposits into protocol-specific receipt assets.",
            "Yield can come from staking, rewards, incentives, or underlying strategy returns.",
            "Liquidity, cooldowns, withdrawal queues, and slashing terms must be shown when source data exposes them.",
        ]
    if category == "Prediction Markets":
        return [
            "Markets expose outcomes, prices, spread, liquidity, and settlement context when available.",
            "Prices reflect market consensus and liquidity, not guaranteed probabilities.",
            "Prediction markets are available for how-it-works and market-snapshot in v0.1, not compare-protocols.",
        ]
    return [
        "Protocol surfaces are summarized from checked protocol metadata.",
        "Protocol notes are reviewed before rendering.",
        "Live values are used only when a read-only protocol or market source is available.",
    ]


def protocol_domain_for_slug(slug: str) -> str:
    return PROTOCOL_DOMAINS.get(slug, "")


def protocol_homepage_for_slug(slug: str) -> str | None:
    domain = protocol_domain_for_slug(slug)
    return f"https://{domain}" if domain else None


def protocol_logo_url_for_slug(slug: str) -> str | None:
    domain = protocol_domain_for_slug(slug)
    if not domain:
        return None
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"


def protocol_public_sources(info: AdapterInfo, docs: list[str] | None = None) -> list[str]:
    sources: list[str] = []
    homepage = protocol_homepage_for_slug(info.slug)
    if homepage:
        sources.append(homepage)
    for url in docs or []:
        if url and url not in sources:
            sources.append(url)
    return sources


def doc_note_rows(doc_metadata: list[JsonMap], official_docs: list[str]) -> list[JsonMap]:
    rows: list[JsonMap] = []
    for item in doc_metadata:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        url = str(item.get("url") or "")
        title = str(item.get("title") or "") or compact_source_label(url, max_len=44)
        snippets = [str(s) for s in list(item.get("snippets") or [])[:2]]
        rows.append(
            {
                "title": title,
                "url": url,
                "snippets": snippets,
            }
        )
    if rows:
        return rows[:3]
    return [
        {"title": compact_source_label(url, max_len=44), "url": url, "snippets": []}
        for url in official_docs[:3]
    ]


def build_model(
    *,
    kind: str,
    title: str,
    subtitle: str,
    summary: str,
    metrics: list[JsonMap],
    sections: list[JsonMap],
    risks: list[str],
    sources: list[str],
    warnings: list[str],
    style: JsonMap,
    flow_steps: list[str] | None = None,
    chips: list[str] | None = None,
    visual_metrics: list[JsonMap] | None = None,
) -> JsonMap:
    return {
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "summary": summary,
        "metrics": metrics,
        "sections": sections,
        "risks": risks,
        "sources": sources,
        "warnings": warnings,
        "flow_steps": flow_steps or [],
        "chips": chips or [],
        "visual_metrics": visual_metrics or [],
        "style": {
            "name": style.get("title", "Protocol Infographic"),
            "canvas": style.get("canvas") or {},
            "colors": style.get("colors") or {},
            "typography": style.get("typography") or {},
        },
    }


def wrap_text(text: str, max_chars: int) -> list[str]:
    words = str(text).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            for start in range(0, len(word), max_chars):
                lines.append(word[start : start + max_chars])
            continue
        if not current:
            current = word
            continue
        if len(current) + len(word) + 1 <= max_chars:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def shorten_address(address: str) -> str:
    return f"{address[:5]}...{address[-4:]}"


def shorten_addresses_in_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return ADDRESS_RE.sub(lambda match: shorten_address(match.group(0)), text)


def svg_text(
    *,
    x: int,
    y: int,
    text: str,
    size: int,
    color: str,
    weight: int = 400,
    max_chars: int = 60,
    line_height: int | None = None,
) -> tuple[str, int]:
    line_height = line_height or int(size * 1.35)
    parts: list[str] = []
    cursor = y
    for line in wrap_text(text, max_chars):
        display_line = shorten_addresses_in_text(line)
        parts.append(
            f'<text x="{x}" y="{cursor}" fill="{html.escape(color)}" '
            f'font-size="{size}" font-weight="{weight}">{html.escape(display_line)}</text>'
        )
        cursor += line_height
    return "\n".join(parts), cursor


# ──────────────────────────────────────────────────────────────────────────
# Wayfinder design system
# Derived from the design handoff at wayfinder/colors_and_type.css and the
# per-product HTML mocks under ui_kits/infographic-composer/.
# Dark theme · lavender accent · Inter Tight + JetBrains Mono + Source Serif 4.
# ──────────────────────────────────────────────────────────────────────────

WF_FONTS_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Inter+Tight:wght@400;500;600;700&'
    'family=JetBrains+Mono:wght@400;500;600&'
    'family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&display=swap">'
)

WF_CSS_TOKENS = (
    ":root{"
    "--wf-bg:#0b0c0f;--wf-surface:#13151a;--wf-surface-2:#181b22;--wf-surface-3:#1f232c;"
    "--wf-border:rgba(255,255,255,0.06);--wf-border-2:rgba(255,255,255,0.10);--wf-border-3:rgba(255,255,255,0.18);"
    "--wf-fg:#e8e9ec;--wf-fg-dim:rgba(232,233,236,0.62);--wf-fg-faint:rgba(232,233,236,0.42);--wf-fg-ghost:rgba(232,233,236,0.24);"
    "--wf-brand:#c8a8ff;--wf-brand-deep:#7c5cff;--wf-brand-tint:rgba(200,168,255,0.12);"
    "--wf-up:#3ecf8e;--wf-down:#ff5b6a;--wf-warn:#ffcb5b;"
    "--wf-font-sans:'Inter Tight','Inter',ui-sans-serif,system-ui,sans-serif;"
    "--wf-font-mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;"
    "--wf-font-serif:'Source Serif 4',ui-serif,Georgia,serif;"
    "}"
)

WF_CSS_BASE = """
*,*::before,*::after{box-sizing:border-box;}
html,body{margin:0;padding:0;background:#06070a;}
body{font-family:var(--wf-font-sans);color:var(--wf-fg);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;}
.wf-mono{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum","ss01";}
.wf-up{color:var(--wf-up);}.wf-down{color:var(--wf-down);}.wf-warn{color:var(--wf-warn);}
.wf-dim{color:var(--wf-fg-dim);}.wf-faint{color:var(--wf-fg-faint);}.wf-brand-fg{color:var(--wf-brand);}
.sheet{width:1280px;min-height:800px;background:var(--wf-bg);color:var(--wf-fg);font-family:var(--wf-font-sans);padding:48px 56px 40px;box-sizing:border-box;position:relative;margin:0 auto;}
.doc-head{display:grid;grid-template-columns:1fr auto;align-items:flex-start;border-bottom:1px solid var(--wf-border);padding-bottom:18px;gap:32px;}
.logo-row{display:flex;align-items:center;gap:10px;}
.logo{width:22px;height:22px;border-radius:50%;position:relative;background:conic-gradient(from 210deg,#c8a8ff,#7c5cff,#c8a8ff);}
.logo::before{content:'';position:absolute;inset:4px;border-radius:50%;background:var(--wf-bg);}
.logo::after{content:'';position:absolute;inset:8px;border-radius:50%;background:#c8a8ff;}
.wordmark{font-size:13px;font-weight:600;letter-spacing:-0.01em;}
.doc-meta{color:var(--wf-fg-faint);font-family:var(--wf-font-mono);font-size:10px;letter-spacing:0.06em;text-transform:uppercase;}
.doc-meta .sep{color:var(--wf-fg-ghost);margin:0 8px;}
.run-id{color:var(--wf-fg-dim);font-family:var(--wf-font-mono);font-size:11px;text-align:right;line-height:1.6;}
.run-id .k{color:var(--wf-fg-faint);}
.title-block{padding:32px 0 24px;display:grid;grid-template-columns:1.4fr 1fr;gap:48px;align-items:flex-end;border-bottom:1px solid var(--wf-border);}
.title-main{display:flex;align-items:flex-end;gap:18px;min-width:0;}
.title-logo{width:56px;height:56px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;background:var(--wf-surface-2);border:1px solid var(--wf-border-2);overflow:hidden;color:var(--wf-fg);font-family:var(--wf-font-mono);font-size:20px;font-weight:700;box-shadow:0 0 0 6px rgba(255,255,255,0.02);}
.title-logo img{width:100%;height:100%;object-fit:cover;background:#fff;display:block;}
.title-logo img + span{display:none;}
.eyebrow{font-size:10px;font-weight:600;letter-spacing:0.12em;text-transform:uppercase;color:var(--wf-brand);margin-bottom:14px;display:inline-flex;align-items:center;gap:10px;}
.eyebrow::before{content:'';width:18px;height:1px;background:currentColor;}
.eyebrow.up{color:var(--wf-up);}
h1.editorial{font-family:var(--wf-font-serif);font-size:52px;font-weight:600;line-height:1.0;letter-spacing:-0.02em;margin:0;}
h1.editorial em{font-style:italic;color:var(--wf-brand);font-weight:600;}
h1.editorial .num{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;color:var(--wf-up);font-weight:500;font-style:normal;letter-spacing:-0.04em;}
.deck{color:var(--wf-fg-dim);font-size:14px;line-height:1.55;max-width:480px;}
.deck b,.deck strong{color:var(--wf-fg);font-weight:500;}
.deck .wf-mono,.deck code{color:var(--wf-fg);font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum","ss01";}
.pill{font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px;background:rgba(255,255,255,0.06);color:var(--wf-fg-dim);letter-spacing:0.04em;display:inline-block;}
.pill.brand{background:var(--wf-brand-tint);color:var(--wf-brand);}
.pill.warn{background:rgba(255,203,91,0.14);color:var(--wf-warn);}
.live{display:inline-flex;align-items:center;gap:6px;font-size:10px;font-weight:600;letter-spacing:0.08em;color:var(--wf-up);}
.live::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--wf-up);box-shadow:0 0 8px var(--wf-up);}
.card{background:var(--wf-surface);border:1px solid var(--wf-border);border-radius:10px;overflow:hidden;}
.card-head{padding:14px 18px;border-bottom:1px solid var(--wf-border);display:flex;align-items:center;justify-content:space-between;}
.card-title{font-size:13px;font-weight:600;}
.doc-foot{margin-top:32px;padding-top:16px;border-top:1px solid var(--wf-border);display:flex;justify-content:space-between;align-items:center;font-family:var(--wf-font-mono);font-size:10px;color:var(--wf-fg-faint);letter-spacing:0.06em;flex-wrap:wrap;gap:12px;}
.doc-foot .src{display:flex;gap:14px;flex-wrap:wrap;}
.warnings{margin:24px 0 0;border:1px solid var(--wf-border);border-radius:10px;background:rgba(255,203,91,0.04);padding:14px 18px;}
.warnings .label{font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--wf-warn);margin-bottom:8px;}
.warnings ul{margin:0;padding-left:18px;font-size:12px;color:var(--wf-fg-dim);line-height:1.5;}
.warnings ul li{margin-bottom:4px;}
"""

WF_CSS_HIW = """
.sheet.kind-how-it-works{background-image:radial-gradient(ellipse 800px 400px at 80% -10%,rgba(124,92,255,0.10),transparent 60%),radial-gradient(ellipse 600px 300px at -10% 110%,rgba(62,207,142,0.05),transparent 60%);}
.kind-how-it-works .title-block{grid-template-columns:1fr 380px;}
.kind-how-it-works .deck{max-width:380px;}
.kind-how-it-works .stats{display:grid;grid-template-columns:repeat(5,1fr);margin:24px 0 32px;border:1px solid var(--wf-border);border-radius:10px;background:var(--wf-surface);}
.kind-how-it-works .stat{padding:14px 18px;border-left:1px solid var(--wf-border);}
.kind-how-it-works .stat:first-child{border-left:0;}
.kind-how-it-works .stat .k{font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--wf-fg-faint);margin-bottom:6px;}
.kind-how-it-works .stat .v{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:22px;font-weight:500;letter-spacing:-0.01em;}
.kind-how-it-works .stat .sub{font-size:10px;color:var(--wf-fg-dim);margin-top:4px;font-family:var(--wf-font-mono);}
.kind-how-it-works .wizard-flow{position:relative;margin-bottom:32px;padding:4px 0 0;}
.kind-how-it-works .wizard-track{position:absolute;left:10%;right:10%;top:27px;height:2px;background:linear-gradient(90deg,var(--wf-brand),var(--wf-up));opacity:0.45;border-radius:2px;}
.kind-how-it-works .wizard-steps{display:grid;gap:0;position:relative;}
.kind-how-it-works .wizard-step{padding:0 16px 4px;text-align:center;display:flex;flex-direction:column;align-items:center;min-width:0;}
.kind-how-it-works .wizard-dot{width:52px;height:52px;border-radius:50%;background:var(--wf-bg);border:2px solid var(--wf-fg-faint);color:var(--wf-fg-faint);font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:14px;font-weight:700;display:flex;align-items:center;justify-content:center;box-shadow:0 0 0 8px var(--wf-bg);margin-bottom:18px;}
.kind-how-it-works .wizard-step.brand .wizard-dot{border-color:var(--wf-brand);color:var(--wf-brand);}
.kind-how-it-works .wizard-step.up .wizard-dot{border-color:var(--wf-up);color:var(--wf-up);}
.kind-how-it-works .wizard-label{font-family:var(--wf-font-mono);font-size:10px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:var(--wf-brand);margin-bottom:10px;}
.kind-how-it-works .wizard-step.up .wizard-label{color:var(--wf-up);}
.kind-how-it-works .wizard-head{font-family:var(--wf-font-serif);font-size:20px;font-weight:600;line-height:1.15;letter-spacing:-0.01em;color:var(--wf-fg);margin-bottom:10px;max-width:220px;}
.kind-how-it-works .wizard-body{font-size:12px;line-height:1.55;color:var(--wf-fg-dim);max-width:230px;margin-bottom:14px;}
.kind-how-it-works .wizard-badge{display:inline-flex;gap:6px;align-items:center;font-size:10px;font-family:var(--wf-font-mono);color:var(--wf-fg);padding:5px 9px;border-radius:4px;background:var(--wf-surface-2);border:1px solid var(--wf-border-2);letter-spacing:0.04em;margin-top:auto;}
.kind-how-it-works .wizard-value{margin-top:auto;border-top:1px dotted var(--wf-border-3);padding-top:10px;width:100%;max-width:210px;display:flex;justify-content:space-between;align-items:baseline;font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:10px;color:var(--wf-fg-faint);letter-spacing:0.06em;text-transform:uppercase;}
.kind-how-it-works .wizard-value b{font-size:14px;font-weight:500;color:var(--wf-brand);letter-spacing:-0.01em;text-transform:none;}
.kind-how-it-works .lower{display:grid;grid-template-columns:1.4fr 1fr;gap:24px;}
.kind-how-it-works .risk-list{padding:0;}
.kind-how-it-works .risk-row{padding:12px 18px;border-top:1px solid var(--wf-border);display:grid;grid-template-columns:16px 1fr;gap:12px;}
.kind-how-it-works .risk-row:first-child{border-top:0;}
.kind-how-it-works .risk-row .dot{width:8px;height:8px;border-radius:2px;margin-top:5px;background:var(--wf-warn);}
.kind-how-it-works .risk-row .dot.low{background:var(--wf-up);}
.kind-how-it-works .risk-row .dot.med{background:var(--wf-warn);}
.kind-how-it-works .risk-row .dot.high{background:var(--wf-down);}
.kind-how-it-works .risk-row .t{font-size:12px;font-weight:500;margin-bottom:4px;line-height:1.4;}
.kind-how-it-works .risk-row .meta{margin-top:6px;font-family:var(--wf-font-mono);font-size:10px;color:var(--wf-fg-faint);letter-spacing:0.04em;text-transform:uppercase;}
.kind-how-it-works .boundary{margin-top:18px;padding:14px 18px;border:1px solid var(--wf-border);border-radius:8px;background:var(--wf-surface);font-size:12px;color:var(--wf-fg-dim);line-height:1.55;}
.kind-how-it-works .boundary .k{display:inline-block;color:var(--wf-brand);font-family:var(--wf-font-mono);font-size:10px;letter-spacing:0.08em;text-transform:uppercase;margin-right:8px;}
"""

WF_CSS_RATES = """
.sheet.kind-stablecoin-rates{background-image:radial-gradient(ellipse 700px 400px at 110% 0%,rgba(62,207,142,0.06),transparent 60%);}
.kind-stablecoin-rates .eyebrow{color:var(--wf-up);}
.kind-stablecoin-rates .eyebrow::before{background:var(--wf-up);}
.kind-stablecoin-rates h1.editorial{font-size:56px;line-height:0.98;letter-spacing:-0.025em;}
.kind-stablecoin-rates .deck{max-width:480px;font-size:13px;}
.kind-stablecoin-rates .body{display:grid;grid-template-columns:1.4fr 1fr;gap:28px;margin-top:28px;}
.kind-stablecoin-rates .rates{background:var(--wf-surface);border:1px solid var(--wf-border);border-radius:10px;overflow:hidden;}
.kind-stablecoin-rates .col-head{display:grid;grid-template-columns:1.6fr 1fr 0.9fr 0.9fr 1.4fr;padding:10px 18px;font-size:9px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--wf-fg-faint);border-bottom:1px solid var(--wf-border);}
.kind-stablecoin-rates .col-head > *:nth-child(n+2){text-align:right;}
.kind-stablecoin-rates .row{display:grid;grid-template-columns:1.6fr 1fr 0.9fr 0.9fr 1.4fr;padding:16px 18px;border-top:1px solid var(--wf-border);align-items:center;}
.kind-stablecoin-rates .row:first-of-type{border-top:0;}
.kind-stablecoin-rates .row .a{display:flex;align-items:center;gap:12px;}
.kind-stablecoin-rates .tok{width:28px;height:28px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#fff;flex-shrink:0;position:relative;overflow:hidden;}
.kind-stablecoin-rates .tok img{width:100%;height:100%;object-fit:cover;border-radius:50%;background:#fff;display:block;}
.kind-stablecoin-rates .tok img + span{display:none;}
.kind-stablecoin-rates .row .name{font-size:13px;font-weight:500;}
.kind-stablecoin-rates .row .sub{font-size:10px;color:var(--wf-fg-faint);font-family:var(--wf-font-mono);margin-top:2px;letter-spacing:0.04em;text-transform:uppercase;}
.kind-stablecoin-rates .row .apy{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:20px;font-weight:500;text-align:right;color:var(--wf-up);letter-spacing:-0.01em;}
.kind-stablecoin-rates .row .delta{text-align:right;font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:12px;color:var(--wf-fg-dim);}
.kind-stablecoin-rates .row .util{text-align:right;font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:12px;color:var(--wf-fg-dim);}
.kind-stablecoin-rates .row .tvl{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:11px;color:var(--wf-fg-dim);text-align:left;margin-top:4px;}
.kind-stablecoin-rates .row .bar{width:100%;height:4px;background:rgba(62,207,142,0.10);border-radius:2px;overflow:hidden;}
.kind-stablecoin-rates .row .bar > i{display:block;height:100%;background:linear-gradient(90deg,var(--wf-up),#c8a8ff);border-radius:2px;}
.kind-stablecoin-rates .row.unavail .apy{color:var(--wf-fg-faint);font-style:italic;letter-spacing:0;font-size:12px;font-weight:400;}
.kind-stablecoin-rates .row.unavail{background:rgba(255,91,106,0.03);}
.kind-stablecoin-rates .row.unavail .name{color:var(--wf-fg-dim);}
.kind-stablecoin-rates .row.unavail .sub{color:var(--wf-down);}
.kind-stablecoin-rates .row.best{background:linear-gradient(90deg,rgba(200,168,255,0.05),transparent 60%);position:relative;}
.kind-stablecoin-rates .row.best::before{content:'BEST';position:absolute;left:0;top:50%;transform:translateY(-50%);writing-mode:vertical-rl;font-family:var(--wf-font-mono);font-size:9px;letter-spacing:0.08em;color:var(--wf-brand);font-weight:700;padding-left:3px;}
.kind-stablecoin-rates .side-card{background:var(--wf-surface);border:1px solid var(--wf-border);border-radius:10px;padding:18px;}
.kind-stablecoin-rates .side-card + .side-card{margin-top:18px;}
.kind-stablecoin-rates .side-card .label{font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--wf-fg-faint);margin-bottom:12px;}
.kind-stablecoin-rates .side-card .big{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:38px;font-weight:500;letter-spacing:-0.02em;line-height:1;}
.kind-stablecoin-rates .side-card .sub{font-size:11px;color:var(--wf-fg-dim);margin-top:6px;line-height:1.5;}
.kind-stablecoin-rates .side-card .body-text{font-size:11px;color:var(--wf-fg-dim);line-height:1.55;}
.kind-stablecoin-rates .kv{display:flex;align-items:baseline;font-size:11px;padding:4px 0;}
.kind-stablecoin-rates .kv .k{color:var(--wf-fg-faint);white-space:nowrap;}
.kind-stablecoin-rates .kv .leader{flex:1;border-bottom:1px dotted var(--wf-fg-ghost);margin:0 6px;transform:translateY(-3px);}
.kind-stablecoin-rates .kv .v{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;color:var(--wf-fg);}
.kind-stablecoin-rates .head-row{padding:14px 18px;border-bottom:1px solid var(--wf-border);display:flex;justify-content:space-between;align-items:center;}
.kind-stablecoin-rates .head-row .t{font-size:13px;font-weight:600;}
"""

WF_CSS_SNAPSHOT = """
.sheet.kind-market-snapshot{background-image:radial-gradient(ellipse 600px 400px at 100% 0%,rgba(247,147,26,0.08),transparent 60%),radial-gradient(ellipse 600px 300px at 0% 100%,rgba(124,92,255,0.06),transparent 60%);}
.kind-market-snapshot .market-hero{margin-top:28px;display:grid;grid-template-columns:auto 1fr auto;gap:28px;align-items:center;padding-bottom:24px;border-bottom:1px solid var(--wf-border);}
.kind-market-snapshot .mark-id{display:flex;align-items:center;gap:16px;}
.kind-market-snapshot .mark-id .tok{width:64px;height:64px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:32px;font-weight:700;color:#fff;flex-shrink:0;overflow:hidden;border:1px solid var(--wf-border-2);}
.kind-market-snapshot .mark-id .tok img{width:100%;height:100%;object-fit:cover;background:#fff;display:block;}
.kind-market-snapshot .mark-id .tok img + span{display:none;}
.kind-market-snapshot .mark-id .name{font-family:var(--wf-font-sans);font-size:32px;font-weight:600;letter-spacing:-0.02em;}
.kind-market-snapshot .mark-id .sub{font-family:var(--wf-font-mono);font-size:11px;color:var(--wf-fg-faint);letter-spacing:0.06em;text-transform:uppercase;margin-top:4px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
.kind-market-snapshot .mark-id .sub .pill{padding:2px 6px;border-radius:3px;background:rgba(255,255,255,0.06);color:var(--wf-fg-dim);}
.kind-market-snapshot .mark-price{text-align:center;}
.kind-market-snapshot .mark-price .price{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:88px;font-weight:500;line-height:0.95;letter-spacing:-0.04em;}
.kind-market-snapshot .mark-price .price .cents{font-size:36px;color:var(--wf-fg-dim);letter-spacing:-0.02em;}
.kind-market-snapshot .mark-price .price.unavail{font-size:42px;color:var(--wf-fg-faint);font-style:italic;}
.kind-market-snapshot .mark-price .delta{display:inline-flex;align-items:center;gap:12px;margin-top:12px;flex-wrap:wrap;justify-content:center;}
.kind-market-snapshot .mark-price .delta .pct{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:18px;font-weight:500;}
.kind-market-snapshot .mark-price .delta .abs{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:14px;}
.kind-market-snapshot .mark-price .delta .since{font-size:11px;color:var(--wf-fg-faint);letter-spacing:0.06em;text-transform:uppercase;}
.kind-market-snapshot .mark-state{text-align:right;}
.kind-market-snapshot .mark-state .state{display:inline-flex;gap:6px;align-items:center;font-size:10px;font-weight:600;letter-spacing:0.08em;color:var(--wf-up);padding:6px 10px;background:rgba(62,207,142,0.10);border:1px solid rgba(62,207,142,0.30);border-radius:4px;}
.kind-market-snapshot .mark-state .state::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--wf-up);box-shadow:0 0 8px var(--wf-up);}
.kind-market-snapshot .mark-state .state.fail{color:var(--wf-down);background:rgba(255,91,106,0.10);border-color:rgba(255,91,106,0.30);}
.kind-market-snapshot .mark-state .state.fail::before{background:var(--wf-down);box-shadow:0 0 8px var(--wf-down);}
.kind-market-snapshot .mark-state .read{font-family:var(--wf-font-mono);font-size:10px;color:var(--wf-fg-faint);margin-top:10px;letter-spacing:0.04em;line-height:1.4;}
.kind-market-snapshot .stats{display:grid;grid-template-columns:repeat(8,1fr);background:var(--wf-surface);border:1px solid var(--wf-border);border-radius:10px;margin:24px 0;}
.kind-market-snapshot .stat{padding:14px 12px;border-left:1px solid var(--wf-border);min-width:0;}
.kind-market-snapshot .stat:first-child{border-left:0;}
.kind-market-snapshot .stat .k{font-size:9px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--wf-fg-faint);margin-bottom:6px;}
.kind-market-snapshot .stat .v{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:15px;font-weight:500;letter-spacing:-0.01em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.kind-market-snapshot .stat .v.unavail{color:var(--wf-fg-faint);font-style:italic;}
.kind-market-snapshot .body{display:grid;grid-template-columns:1.7fr 1fr;gap:24px;}
.kind-market-snapshot .book-cols{display:grid;grid-template-columns:1fr 1fr 1fr;padding:8px 14px;font-size:9px;color:var(--wf-fg-faint);letter-spacing:0.08em;text-transform:uppercase;font-weight:600;}
.kind-market-snapshot .book-cols span:nth-child(2),.kind-market-snapshot .book-cols span:nth-child(3){text-align:right;}
.kind-market-snapshot .brow{position:relative;display:grid;grid-template-columns:1fr 1fr 1fr;padding:4px 14px;font-family:var(--wf-font-mono);font-size:11px;font-variant-numeric:tabular-nums;align-items:center;}
.kind-market-snapshot .brow .bg{position:absolute;right:0;top:0;bottom:0;}
.kind-market-snapshot .brow.a .bg{background:rgba(255,91,106,0.14);}
.kind-market-snapshot .brow.b .bg{background:rgba(62,207,142,0.14);}
.kind-market-snapshot .brow .p{position:relative;font-weight:500;}
.kind-market-snapshot .brow.a .p{color:var(--wf-down);}
.kind-market-snapshot .brow.b .p{color:var(--wf-up);}
.kind-market-snapshot .brow .s,.kind-market-snapshot .brow .t{text-align:right;position:relative;color:var(--wf-fg);}
.kind-market-snapshot .brow .t{color:var(--wf-fg-dim);}
.kind-market-snapshot .mid-row{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--wf-surface-2);border-top:1px solid var(--wf-border);border-bottom:1px solid var(--wf-border);}
.kind-market-snapshot .mid-row .price{font-family:var(--wf-font-mono);font-size:15px;font-weight:600;color:var(--wf-up);font-variant-numeric:tabular-nums;}
.kind-market-snapshot .mid-row .sp{font-size:10px;color:var(--wf-fg-faint);letter-spacing:0.06em;}
.kind-market-snapshot .empty-card{padding:24px;color:var(--wf-fg-faint);font-size:12px;line-height:1.55;font-style:italic;}
"""

WF_CSS_COMPARE = """
.sheet.kind-compare-protocols{background-image:radial-gradient(ellipse 800px 400px at 30% -10%,rgba(124,92,255,0.08),transparent 60%);}
.kind-compare-protocols .matrix{margin-top:28px;background:var(--wf-surface);border:1px solid var(--wf-border);border-radius:10px;overflow:hidden;}
.kind-compare-protocols .proto-head{display:grid;border-bottom:1px solid var(--wf-border);}
.kind-compare-protocols .proto-head > div{padding:22px 22px;border-left:1px solid var(--wf-border);}
.kind-compare-protocols .proto-head > div:first-child{border-left:0;background:var(--wf-surface-2);}
.kind-compare-protocols .proto-name{display:flex;align-items:center;gap:12px;}
.kind-compare-protocols .proto-tok{width:36px;height:36px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:#fff;flex-shrink:0;overflow:hidden;border:1px solid var(--wf-border-2);}
.kind-compare-protocols .proto-tok img{width:100%;height:100%;object-fit:cover;background:#fff;display:block;}
.kind-compare-protocols .proto-tok img + span{display:none;}
.kind-compare-protocols .proto-name .t{font-family:var(--wf-font-serif);font-size:22px;font-weight:600;letter-spacing:-0.01em;}
.kind-compare-protocols .proto-meta{font-family:var(--wf-font-mono);font-size:10px;color:var(--wf-fg-faint);letter-spacing:0.06em;text-transform:uppercase;margin-top:8px;}
.kind-compare-protocols .proto-best{margin-top:14px;display:inline-flex;align-items:center;gap:6px;font-size:10px;font-weight:700;letter-spacing:0.08em;color:var(--wf-brand);padding:4px 8px;border-radius:4px;background:var(--wf-brand-tint);}
.kind-compare-protocols .proto-best.hidden{visibility:hidden;}
.kind-compare-protocols .matrix-question{font-family:var(--wf-font-serif);font-size:18px;font-weight:600;line-height:1.2;letter-spacing:-0.01em;}
.kind-compare-protocols .matrix-question .sub{display:block;font-family:var(--wf-font-sans);font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--wf-fg-faint);margin-bottom:8px;}
.kind-compare-protocols .matrix-row{display:grid;border-top:1px solid var(--wf-border);}
.kind-compare-protocols .matrix-row > div{padding:18px 22px;border-left:1px solid var(--wf-border);min-width:0;}
.kind-compare-protocols .matrix-row > div:first-child{border-left:0;background:var(--wf-surface-2);}
.kind-compare-protocols .cell-value{font-family:var(--wf-font-mono);font-variant-numeric:tabular-nums;font-size:22px;font-weight:500;letter-spacing:-0.01em;}
.kind-compare-protocols .cell-value.up{color:var(--wf-up);}
.kind-compare-protocols .cell-value.brand{color:var(--wf-brand);}
.kind-compare-protocols .cell-value.unavail{font-style:italic;color:var(--wf-fg-faint);font-size:14px;font-weight:400;letter-spacing:0;}
.kind-compare-protocols .cell-note{font-size:11px;color:var(--wf-fg-dim);margin-top:8px;line-height:1.5;}
.kind-compare-protocols .cell-note .mono{font-family:var(--wf-font-mono);color:var(--wf-fg);}
.kind-compare-protocols .tag-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;}
.kind-compare-protocols .tag{font-size:10px;padding:3px 7px;border-radius:4px;background:rgba(255,255,255,0.05);color:var(--wf-fg-dim);font-family:var(--wf-font-mono);letter-spacing:0.04em;}
.kind-compare-protocols .tag.up{background:rgba(62,207,142,0.10);color:var(--wf-up);}
.kind-compare-protocols .tag.down{background:rgba(255,91,106,0.10);color:var(--wf-down);}
.kind-compare-protocols .tag.warn{background:rgba(255,203,91,0.10);color:var(--wf-warn);}
.kind-compare-protocols .risk-meter{display:flex;gap:4px;align-items:center;margin-top:8px;}
.kind-compare-protocols .risk-meter i{width:14px;height:6px;border-radius:2px;background:rgba(255,255,255,0.10);}
.kind-compare-protocols .risk-meter i.on{background:var(--wf-warn);}
.kind-compare-protocols .risk-meter.low i.on{background:var(--wf-up);}
.kind-compare-protocols .risk-meter.high i.on{background:var(--wf-down);}
.kind-compare-protocols .risk-label{display:inline-flex;align-items:center;gap:8px;font-family:var(--wf-font-mono);font-size:11px;color:var(--wf-fg-dim);letter-spacing:0.04em;text-transform:uppercase;}
.kind-compare-protocols .legend{display:flex;gap:14px;padding:14px 22px;font-family:var(--wf-font-mono);font-size:10px;color:var(--wf-fg-faint);letter-spacing:0.06em;border-top:1px solid var(--wf-border);background:var(--wf-surface-2);}
.kind-compare-protocols .legend-item{display:inline-flex;align-items:center;gap:6px;}
"""


# ──────────────────────────────────────────────────────────────────────────
# Shared HTML builders
# ──────────────────────────────────────────────────────────────────────────

PROTOCOL_TOKEN_COLORS: dict[str, str] = {
    "aave_v3_adapter": "#7c5cff",
    "morpho_adapter": "linear-gradient(135deg,#b6509e,#2eb6ea)",
    "moonwell_adapter": "#4cb6a7",
    "compound_v3_adapter": "#41d1a7",
    "sparklend_adapter": "#0052ff",
    "spark_adapter": "#0052ff",
    "fluid_adapter": "#1a5fff",
    "euler_v2_adapter": "#1a8b5a",
    "hyperlend_adapter": "#ec6f3a",
    "ethena_vault_adapter": "#9266ff",
    "pendle_adapter": "#1f8f6f",
    "hyperliquid_adapter": "#97f9d8",
    "uniswap_adapter": "#ff3e7a",
    "aerodrome_adapter": "#1d6dff",
    "aerodrome_slipstream_adapter": "#1d6dff",
    "projectx_adapter": "#9d4dff",
    "lido_adapter": "#00a3ff",
    "etherfi_adapter": "#7c5cff",
    "eigencloud_adapter": "#1e3a8a",
    "polymarket_adapter": "#1052ff",
    "avantis_adapter": "#fb7a3c",
    "boros_adapter": "#7c5cff",
}

CHAIN_LABEL: dict[int, str] = {
    1: "ethereum",
    8453: "base",
    42161: "arbitrum",
    137: "polygon",
    56: "bsc",
    43114: "avalanche",
    9745: "plasma",
    999: "hyperevm",
    10: "optimism",
}


def _esc(value: Any) -> str:
    return html.escape(shorten_addresses_in_text(value))


def _protocol_token_color(slug: str) -> str:
    color = PROTOCOL_TOKEN_COLORS.get(slug)
    if color:
        return color
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()
    hue = int(digest[:2], 16) * 360 // 256
    return f"hsl({hue},60%,55%)"


def _protocol_monogram(name: str) -> str:
    name = (name or "").strip()
    for ch in name:
        if ch.isalpha():
            return ch.upper()
    return "?"


def _logo_inner(slug: str, name: str) -> str:
    logo_url = protocol_logo_url_for_slug(slug)
    monogram = _protocol_monogram(name or slug)
    if logo_url:
        return (
            f'<img src="{_esc(logo_url)}" alt="" loading="lazy">'
            f'<span>{_esc(monogram)}</span>'
        )
    return _esc(monogram)


def step_label_from_text(text: str) -> str:
    lowered = text.lower()
    labels = [
        ("supply", ("supply", "supplies", "deposit", "deposits", "add liquidity")),
        ("borrow", ("borrow", "borrower", "borrowers", "debt", "draw")),
        ("accrue", ("accrue", "interest", "yield", "apy", "rate", "utilization")),
        ("liquidate", ("liquidator", "liquidators", "liquidation", "unhealthy")),
        ("withdraw", ("withdraw", "exit", "redeem", "burn")),
        ("price", ("price", "oracle", "mark")),
        ("funding", ("funding",)),
        ("depth", ("orderbook", "spread", "depth", "liquidity")),
        ("allocate", ("allocate", "allocation", "curator")),
        ("settle", ("resolve", "resolution", "settle", "settlement")),
    ]
    for label, needles in labels:
        if any(needle in lowered for needle in needles):
            return label.upper()
    first = re.sub(r"[^A-Za-z0-9]+", "", text.split(" ", 1)[0] if text else "")
    return (first or "STEP").upper()[:12]


def _chain_slug(chain_id: int | None) -> str:
    if chain_id is None:
        return ""
    return CHAIN_LABEL.get(int(chain_id), str(chain_id))


def _format_percent(value: Any, *, signed: bool = False) -> str:
    rate = coerce_float(value)
    if rate is None:
        return "—"
    if signed:
        prefix = "+" if rate >= 0 else "−"
        return f"{prefix}{abs(rate) * 100:.2f}%"
    return f"{rate * 100:.2f}%"


def _format_usd_compact(value: Any) -> str:
    amount = coerce_float(value)
    if amount is None:
        return "—"
    abs_amount = abs(amount)
    sign = "−" if amount < 0 else ""
    if abs_amount >= 1_000_000_000_000:
        return f"{sign}${abs_amount / 1_000_000_000_000:.2f}T"
    if abs_amount >= 1_000_000_000:
        return f"{sign}${abs_amount / 1_000_000_000:.2f}B"
    if abs_amount >= 1_000_000:
        return f"{sign}${abs_amount / 1_000_000:.1f}M"
    if abs_amount >= 1_000:
        return f"{sign}${abs_amount / 1_000:.1f}K"
    return f"{sign}${abs_amount:,.0f}"


def _shorten(value: Any, *, max_len: int = 28) -> str:
    text = shorten_addresses_in_text(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _wf_css() -> str:
    css = WF_CSS_TOKENS + WF_CSS_BASE + WF_CSS_HIW + WF_CSS_RATES + WF_CSS_SNAPSHOT + WF_CSS_COMPARE
    return css


def _wf_html_head(title: str) -> str:
    css = _wf_css()
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=1280, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"{WF_FONTS_LINK}\n"
        f"<style>{css}</style>\n"
        "</head>\n"
    )


def _render_doc_head(action_slug: str, run_meta: list) -> str:
    parts = ['<div class="doc-head"><div>']
    parts.append('<div class="logo-row">')
    parts.append('<div class="logo"></div>')
    parts.append('<div class="wordmark">Protocol Brief</div>')
    parts.append(
        '<span class="doc-meta">'
        f'<span class="sep">/</span>{_esc(PRODUCT_LABELS.get(action_slug, action_slug))}'
        "</span>"
    )
    parts.append("</div></div>")
    rows = []
    for item in run_meta:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        k, v = item
        rows.append(
            f'<div><span class="k">{_esc(k)}</span> &nbsp; {_esc(v)}</div>'
        )
    if rows:
        parts.append(f'<div class="run-id">{"".join(rows)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _render_title_block(model: JsonMap, *, eyebrow_class: str = "") -> str:
    eyebrow = model.get("eyebrow") or ""
    title = model.get("title") or ""
    em = model.get("headline_em")
    title_post = model.get("title_post") or ""
    deck = shorten_addresses_in_text(model.get("deck") or model.get("summary") or "")
    title_parts: list[str] = []
    if title:
        title_parts.append(_esc(title))
    if em:
        if title_parts and not title_parts[-1].endswith(" "):
            title_parts.append(" ")
        title_parts.append(f"<em>{_esc(em)}</em>")
    if title_post:
        title_parts.append(" " + _esc(title_post))
    eyebrow_attr = f"eyebrow {eyebrow_class}".strip()
    logo_url = model.get("protocol_logo_url")
    logo_slug = str(model.get("protocol_slug") or "")
    logo_name = str(model.get("protocol_name") or model.get("headline_em") or title or "")
    logo_html = ""
    if logo_url:
        logo_html = (
            '<div class="title-logo">'
            f'<img src="{_esc(logo_url)}" alt="" loading="lazy">'
            f'<span>{_esc(_protocol_monogram(logo_name or logo_slug))}</span>'
            "</div>"
        )
    return (
        '<div class="title-block"><div>'
        '<div class="title-main">'
        f"{logo_html}"
        "<div>"
        f'<div class="{eyebrow_attr}">{_esc(eyebrow)}</div>'
        f'<h1 class="editorial">{"".join(title_parts)}</h1>'
        "</div>"
        "</div>"
        "</div>"
        f'<div class="deck">{deck}</div>'
        "</div>"
    )


def _render_doc_foot(model: JsonMap) -> str:
    sources = list(model.get("sources") or [])
    src_html = "".join(
        f"<span>{_esc(compact_source_label(str(s), max_len=58))}</span>"
        for s in sources[:6]
    )
    return (
        '<div class="doc-foot">'
        f'<div class="src">{src_html}</div>'
        "<div>Point-in-time protocol snapshot</div>"
        "</div>"
    )


def _render_warnings(model: JsonMap) -> str:
    warnings = list(model.get("warnings") or [])
    if not warnings:
        return ""
    items = "".join(f"<li>{_esc(w)}</li>" for w in warnings)
    return (
        '<div class="warnings">'
        '<div class="label">Warnings</div>'
        f"<ul>{items}</ul>"
        "</div>"
    )


def _render_alt_caption(alt_text: str) -> str:
    sanitized = _esc(alt_text)
    return (
        '<div style="position:absolute;width:1px;height:1px;padding:0;'
        'margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;'
        f'border:0;">{sanitized}</div>'
    )


# ──────────────────────────────────────────────────────────────────────────
# Product builders
# ──────────────────────────────────────────────────────────────────────────


def _render_how_it_works_html(model: JsonMap, alt_text: str) -> str:
    extras = model.get("extras") or {}
    stats = list(extras.get("stat_strip") or [])
    steps = list(extras.get("mechanism_steps") or [])
    risks = list(extras.get("risk_rows") or [])
    doc_notes = list(extras.get("doc_notes") or [])
    title = model.get("title") or "How it works"

    stats_html_parts: list[str] = []
    for stat in stats[:5]:
        if not isinstance(stat, dict):
            continue
        k = _esc(stat.get("k") or stat.get("label") or "")
        v_raw = stat.get("v") if "v" in stat else stat.get("value")
        v_class = "v"
        if stat.get("v_class") == "up":
            v_class = "v wf-up"
        elif stat.get("v_class") == "down":
            v_class = "v wf-down"
        elif stat.get("v_class") == "warn":
            v_class = "v wf-warn"
        if v_raw is None or v_raw == "":
            v_html = '<span class="wf-faint" style="font-style:italic;">Unavailable</span>'
        elif stat.get("live"):
            v_html = f'<span class="live">{_esc(v_raw)}</span>'
        else:
            v_html = _esc(v_raw)
        sub = stat.get("sub") or ""
        stats_html_parts.append(
            '<div class="stat">'
            f'<div class="k">{k}</div>'
            f'<div class="{v_class}">{v_html}</div>'
            f'<div class="sub">{_esc(sub)}</div>'
            "</div>"
        )
    stats_html = (
        f'<div class="stats">{"".join(stats_html_parts)}</div>'
        if stats_html_parts
        else ""
    )

    steps_html_parts: list[str] = []
    visible_steps = [step for step in steps[:5] if isinstance(step, (dict, str))]
    for i, step in enumerate(visible_steps):
        cls_parts = ["wizard-step"]
        if i == 0:
            cls_parts.append("brand")
        elif i == len(visible_steps) - 1:
            cls_parts.append("up")
        if isinstance(step, dict):
            n_label = step.get("n") or f"{i + 1:02d}"
            label = step.get("label") or "STEP"
            head = step.get("h") or ""
            body = step.get("b") or ""
            badge = step.get("badge")
            value = step.get("value")
            caption = step.get("caption") or "value"
        else:
            n_label = f"{i + 1:02d}"
            label = "STEP"
            head = ""
            body = str(step)
            badge = None
            value = None
            caption = "value"
        badge_html = ""
        if isinstance(badge, dict):
            left = _esc(badge.get("from") or "")
            right = _esc(badge.get("to") or "")
            badge_html = (
                '<div class="wizard-badge">'
                f"<span>{left}</span>"
                '<span class="arrow">→</span>'
                f"<span>{right}</span>"
                "</div>"
            )
        elif badge:
            badge_html = f'<div class="wizard-badge">{_esc(badge)}</div>'
        value_html = ""
        if value:
            value_html = (
                '<div class="wizard-value">'
                f"<span>{_esc(caption)}</span>"
                f"<b>{_esc(value)}</b>"
                "</div>"
            )
        steps_html_parts.append(
            f'<div class="{" ".join(cls_parts)}">'
            f'<div class="wizard-dot">{_esc(n_label)}</div>'
            f'<div class="wizard-label">{_esc(label)}</div>'
            f'<div class="wizard-head">{_esc(head)}</div>'
            f'<div class="wizard-body">{_esc(body)}</div>'
            f"{badge_html}"
            f"{value_html}"
            "</div>"
        )
    mechanism_html = (
        '<div class="wizard-flow">'
        '<div class="wizard-track"></div>'
        f'<div class="wizard-steps" style="grid-template-columns:repeat({len(steps_html_parts)},1fr);">{"".join(steps_html_parts)}</div>'
        "</div>"
        if steps_html_parts
        else ""
    )

    actors_text = ", ".join(_esc(a) for a in list(extras.get("actors") or [])[:6])
    state_text = ", ".join(_esc(s) for s in list(extras.get("state_surfaces") or [])[:6])
    summary_card_parts = []
    if actors_text:
        summary_card_parts.append(
            '<div style="display:flex;align-items:baseline;font-size:11px;padding:6px 0;">'
            '<span class="wf-faint" style="font-family:var(--wf-font-mono);letter-spacing:0.04em;text-transform:uppercase;width:88px;">Actors</span>'
            f'<span class="wf-dim">{actors_text}</span>'
            "</div>"
        )
    if state_text:
        summary_card_parts.append(
            '<div style="display:flex;align-items:baseline;font-size:11px;padding:6px 0;">'
            '<span class="wf-faint" style="font-family:var(--wf-font-mono);letter-spacing:0.04em;text-transform:uppercase;width:88px;">State</span>'
            f'<span class="wf-dim">{state_text}</span>'
            "</div>"
        )

    docs_html = ""
    if doc_notes:
        note_parts: list[str] = []
        for note in doc_notes[:3]:
            if not isinstance(note, dict):
                continue
            title_text = note.get("title") or note.get("url") or ""
            snippets = list(note.get("snippets") or [])
            snippet = snippets[0] if snippets else compact_source_label(str(note.get("url") or ""), max_len=80)
            note_parts.append(
                '<div class="boundary">'
                '<span class="k">Protocol docs</span>'
                f'<b style="color:var(--wf-fg);font-weight:500;">{_esc(title_text)}</b>'
                f'<br>{_esc(snippet)}'
                "</div>"
            )
        docs_html = "".join(note_parts)

    left_card_inner = ""
    if summary_card_parts:
        left_card_inner = (
            '<div style="padding:18px;">'
            f"{''.join(summary_card_parts)}"
            "</div>"
        )
    if not left_card_inner:
        left_card_inner = (
            '<div class="empty-card" style="padding:24px;color:var(--wf-fg-faint);'
            'font-size:12px;line-height:1.55;font-style:italic;">'
            "Protocol mechanism details were not available for this run."
            "</div>"
        )

    left_card_html = (
        '<div class="card">'
        '<div class="card-head">'
        '<div class="card-title">Mechanism surface</div>'
        '<span class="pill">protocol docs</span>'
        "</div>"
        f"{left_card_inner}"
        f"{docs_html}"
        "</div>"
    )

    risks_html_parts: list[str] = []
    for risk in risks[:4]:
        if isinstance(risk, dict):
            severity = (risk.get("severity") or "med").lower()
            title_text = risk.get("title") or risk.get("text") or ""
            meta = risk.get("meta") or ""
        else:
            severity = "med"
            title_text = str(risk)
            meta = ""
        sev_class = severity if severity in {"low", "med", "high"} else "med"
        meta_html = (
            f'<div class="meta">{_esc(meta)}</div>' if meta else ""
        )
        risks_html_parts.append(
            '<div class="risk-row">'
            f'<span class="dot {sev_class}"></span>'
            f'<div><div class="t">{_esc(title_text)}</div>{meta_html}</div>'
            "</div>"
        )
    risks_html = ""
    if risks_html_parts:
        risks_html = (
            '<div class="card">'
            '<div class="card-head">'
            '<div class="card-title">Material risks</div>'
            f'<span class="pill brand">{len(risks_html_parts)} protocol notes</span>'
            "</div>"
            f'<div class="risk-list">{"".join(risks_html_parts)}</div>'
            "</div>"
        )

    lower_html = f'<div class="lower">{left_card_html}{risks_html}</div>'

    body = (
        f'<div class="sheet kind-how-it-works" role="img" aria-label="{_esc(alt_text)}">'
        + _render_doc_head("how-it-works", list(model.get("run_meta") or []))
        + _render_title_block(model)
        + stats_html
        + mechanism_html
        + lower_html
        + _render_warnings(model)
        + _render_doc_foot(model)
        + _render_alt_caption(alt_text)
        + "</div>"
    )
    return _wf_html_head(title) + f"<body>{body}</body></html>"


def _render_stablecoin_rates_html(model: JsonMap, alt_text: str) -> str:
    extras = model.get("extras") or {}
    asset = extras.get("asset") or "USDC"
    rows = list(extras.get("rate_rows") or [])
    unavail = list(extras.get("unavail_rows") or [])
    best = extras.get("best") or {}
    summary_kv = list(extras.get("summary_kv") or [])
    caveats = extras.get("caveats") or ""
    head_pill = extras.get("head_pill") or "LIVE READ"
    title = model.get("title") or f"{asset} Stablecoin Rates"

    rate_rows_html_parts: list[str] = []
    if rows:
        top_apy = max(
            (coerce_float(row.get("net_apy")) or 0.0) for row in rows
        ) if rows else 0.0
    else:
        top_apy = 0.0

    for index, row in enumerate(rows[:8]):
        if not isinstance(row, dict):
            continue
        is_best = bool(row.get("is_best") or index == 0)
        cls = "row best" if is_best else "row"
        protocol = row.get("protocol") or row.get("venue") or ""
        venue_slug = str(row.get("venue") or "")
        sub = row.get("sub") or _shorten(venue_slug, max_len=40)
        logo_url = row.get("logo_url")
        color = _protocol_token_color(venue_slug)
        token_bg = (
            f'background:{color};'
            if color.startswith("hsl")
            or color.startswith("#")
            else f"background:{color};"
        )
        if color.startswith("linear-gradient"):
            token_bg = f"background:{color};"
        monogram = _protocol_monogram(str(protocol))
        token_inner = (
            f'<img src="{_esc(logo_url)}" alt="" loading="lazy">'
            f'<span>{_esc(monogram)}</span>'
            if logo_url
            else _esc(monogram)
        )
        net_apy = row.get("net_apy")
        apy_html = _format_percent(net_apy)
        delta = row.get("delta_30d")
        if delta is None:
            delta_html = '<span class="wf-faint">—</span>'
        else:
            delta_class = "wf-up" if (coerce_float(delta) or 0) >= 0 else "wf-down"
            delta_html = f'<span class="{delta_class}">{_format_percent(delta, signed=True)}</span>'
        util = row.get("utilization")
        util_html = _format_percent(util) if util is not None else "—"
        tvl_value = row.get("tvl_usd")
        tvl_text = _format_usd_compact(tvl_value)
        net_value = coerce_float(net_apy) or 0.0
        bar_pct = max(8.0, min(100.0, (net_value / max(top_apy, 0.0001)) * 100)) if rows else 0.0
        rate_rows_html_parts.append(
            f'<div class="{cls}">'
            '<div class="a">'
            f'<div class="tok" style="{token_bg}">{token_inner}</div>'
            f'<div><div class="name">{_esc(protocol)}</div><div class="sub">{_esc(sub)}</div></div>'
            "</div>"
            f'<div class="apy">{apy_html}</div>'
            f'<div class="delta">{delta_html}</div>'
            f'<div class="util">{_esc(util_html)}</div>'
            f'<div><div class="bar"><i style="width:{bar_pct:.0f}%;"></i></div>'
            f'<div class="tvl">{_esc(tvl_text)}</div></div>'
            "</div>"
        )

    for row in unavail[:4]:
        if not isinstance(row, dict):
            continue
        protocol = row.get("protocol") or row.get("venue") or ""
        venue_slug = str(row.get("venue") or "")
        reason = row.get("reason") or "Read failed"
        retry = row.get("retry") or ""
        monogram = _protocol_monogram(str(protocol))
        logo_url = row.get("logo_url")
        token_inner = (
            f'<img src="{_esc(logo_url)}" alt="" loading="lazy">'
            f'<span>{_esc(monogram)}</span>'
            if logo_url
            else _esc(monogram)
        )
        rate_rows_html_parts.append(
            '<div class="row unavail">'
            '<div class="a">'
            f'<div class="tok" style="background:#2f3038;opacity:0.6;">{token_inner}</div>'
            f'<div><div class="name">{_esc(protocol)}</div>'
            f'<div class="sub">{_esc(reason)}</div></div>'
            "</div>"
            '<div class="apy">Unavailable</div>'
            '<div class="delta">—</div>'
            '<div class="util">—</div>'
            f'<div class="tvl">{_esc(retry) if retry else ""}</div>'
            "</div>"
        )

    empty_rate_rows = '<div class="empty-card">No rate rows fetched in this run.</div>'
    table_html = (
        '<div class="rates">'
        '<div class="head-row">'
        f'<div class="t">Supply APY · {_esc(asset)} · point-in-time</div>'
        f'<span class="live">{_esc(head_pill)}</span>'
        "</div>"
        '<div class="col-head">'
        "<span>Protocol</span><span>Supply APY</span><span>30d Δ</span><span>Util.</span><span>TVL</span>"
        "</div>"
        + ("".join(rate_rows_html_parts) or empty_rate_rows)
        + "</div>"
    )

    best_apy = best.get("apy")
    best_apy_html = _format_percent(best_apy)
    if coerce_float(best_apy) is not None:
        best_html_value = (
            f'<div class="big wf-up">{best_apy_html.rstrip("%")}'
            '<span style="font-size:18px;color:var(--wf-fg-faint);">%</span>'
            "</div>"
        )
    else:
        best_html_value = (
            '<div class="big" style="color:var(--wf-fg-faint);font-style:italic;">'
            "Unavailable"
            "</div>"
        )
    best_card_html = (
        '<div class="side-card">'
        '<div class="label">Best in class · live</div>'
        f"{best_html_value}"
        f'<div class="sub">{best.get("sub") or "No rankable rate available."}</div>'
        "</div>"
    )

    kv_html_parts: list[str] = []
    for kv in summary_kv[:6]:
        if not isinstance(kv, (list, tuple)) or len(kv) < 2:
            continue
        k, v = kv[0], kv[1]
        v_class = ""
        if len(kv) > 2:
            tone = str(kv[2])
            if tone == "up":
                v_class = " wf-up"
            elif tone == "down":
                v_class = " wf-down"
        kv_html_parts.append(
            '<div class="kv">'
            f'<span class="k">{_esc(k)}</span>'
            '<span class="leader"></span>'
            f'<span class="v{v_class}">{_esc(v)}</span>'
            "</div>"
        )
    summary_card_html = (
        '<div class="side-card">'
        '<div class="label">Run summary</div>'
        f"{''.join(kv_html_parts)}"
        "</div>"
    )

    caveats_html = ""
    if caveats:
        caveats_html = (
            '<div class="side-card">'
            '<div class="label">Caveats</div>'
            f'<div class="body-text">{caveats}</div>'
            "</div>"
        )

    body_html = (
        '<div class="body">'
        f"{table_html}"
        f'<div>{best_card_html}{summary_card_html}{caveats_html}</div>'
        "</div>"
    )

    body = (
        f'<div class="sheet kind-stablecoin-rates" role="img" aria-label="{_esc(alt_text)}">'
        + _render_doc_head("stablecoin-rates", list(model.get("run_meta") or []))
        + _render_title_block(model, eyebrow_class="up")
        + body_html
        + _render_warnings(model)
        + _render_doc_foot(model)
        + _render_alt_caption(alt_text)
        + "</div>"
    )
    return _wf_html_head(title) + f"<body>{body}</body></html>"


def _render_market_snapshot_html(model: JsonMap, alt_text: str) -> str:
    extras = model.get("extras") or {}
    market = extras.get("market") or "—"
    protocol = extras.get("protocol") or model.get("protocol_name") or ""
    venue_slug = str(extras.get("venue") or "")
    chain_label = extras.get("chain_label") or ""
    token_color = _protocol_token_color(venue_slug) if venue_slug else "#f7931a"
    token_glyph = extras.get("token_glyph") or _protocol_monogram(market)
    logo_inner = _logo_inner(venue_slug, str(protocol or market)) if venue_slug else _esc(token_glyph)

    price = extras.get("mark_price")
    delta_pct = extras.get("delta_pct")
    delta_abs = extras.get("delta_abs")
    delta_since = extras.get("delta_since") or "24h"
    live_ok = bool(extras.get("live_ok"))
    failure_msg = extras.get("failure_msg") or ""
    read_meta = extras.get("read_meta") or ""

    price_html: str
    price_value = coerce_float(price)
    if price_value is None:
        price_html = '<div class="price unavail">Unavailable</div>'
    else:
        whole = int(price_value)
        cents = price_value - whole
        if cents:
            cents_text = f"{cents:.4f}".split(".")[1].rstrip("0") or "00"
            cents_text = cents_text[:4]
            price_html = (
                f'<div class="price">{whole:,}'
                f'<span class="cents">.{cents_text}</span></div>'
            )
        else:
            price_html = f'<div class="price">{whole:,}</div>'

    delta_pct_val = coerce_float(delta_pct)
    delta_abs_val = coerce_float(delta_abs)
    if delta_pct_val is None and delta_abs_val is None:
        delta_html = ""
    else:
        pct_class = "wf-up" if (delta_pct_val or 0) >= 0 else "wf-down"
        abs_class = pct_class
        triangle = "▲" if (delta_pct_val or 0) >= 0 else "▼"
        pct_text = _format_percent(delta_pct_val, signed=True) if delta_pct_val is not None else ""
        abs_text = ""
        if delta_abs_val is not None:
            sign = "+" if delta_abs_val >= 0 else "−"
            abs_text = f"{sign}${abs(delta_abs_val):,.2f}"
        parts = []
        if abs_text:
            parts.append(f'<span class="abs {abs_class}">{abs_text}</span>')
        if pct_text:
            parts.append(f'<span class="pct {pct_class}">{triangle} {pct_text}</span>')
        if delta_since:
            parts.append(f'<span class="since">{_esc(delta_since)}</span>')
        delta_html = f'<div class="delta">{"".join(parts)}</div>'

    state_class = "state" if live_ok else "state fail"
    state_label = "LIVE READ" if live_ok else "READ FAILED"
    state_html = (
        f'<span class="{state_class}">{_esc(state_label)}</span>'
        f'<div class="read">{read_meta}</div>'
    )

    hero_html = (
        '<div class="market-hero">'
        '<div class="mark-id">'
        f'<div class="tok" style="background:{token_color};">{logo_inner}</div>'
        "<div>"
        f'<div class="name">{_esc(market)}</div>'
        '<div class="sub">'
        f'<span class="pill">{_esc(protocol or "—")}</span>'
        f'<span style="color:var(--wf-fg-faint);">{_esc(chain_label)}</span>'
        "</div>"
        "</div>"
        "</div>"
        f'<div class="mark-price">{price_html}{delta_html}</div>'
        f'<div class="mark-state">{state_html}</div>'
        "</div>"
    )

    stats = list(extras.get("stats") or [])
    stats_html_parts: list[str] = []
    for stat in stats[:8]:
        if not isinstance(stat, dict):
            continue
        k = _esc(stat.get("k") or "")
        v = stat.get("v")
        tone = stat.get("tone")
        if v is None or v == "":
            v_html = '<span class="v unavail">Unavailable</span>'
        else:
            tone_class = ""
            if tone == "up":
                tone_class = " wf-up"
            elif tone == "down":
                tone_class = " wf-down"
            elif tone == "warn":
                tone_class = " wf-warn"
            v_html = f'<span class="v{tone_class}">{_esc(v)}</span>'
        stats_html_parts.append(
            f'<div class="stat"><div class="k">{k}</div>{v_html}</div>'
        )
    stats_html = (
        f'<div class="stats">{"".join(stats_html_parts)}</div>'
        if stats_html_parts
        else ""
    )

    chart_data = list(extras.get("chart_points") or [])
    chart_html = ""
    if chart_data:
        max_p = max(coerce_float(p.get("p")) or 0 for p in chart_data if isinstance(p, dict))
        min_p = min(coerce_float(p.get("p")) or 0 for p in chart_data if isinstance(p, dict))
        span = max_p - min_p if max_p > min_p else 1
        n = len(chart_data)
        path_pts: list[str] = []
        for i, pt in enumerate(chart_data):
            if not isinstance(pt, dict):
                continue
            p = coerce_float(pt.get("p")) or 0
            x = (i / max(1, n - 1)) * 720
            y = 220 - ((p - min_p) / span) * 200
            path_pts.append(f"{x:.1f},{y:.1f}")
        polyline = " ".join(path_pts)
        chart_html = (
            '<div class="card">'
            '<div class="card-head">'
            f'<div class="card-title">Price · {_esc(market)} · history</div>'
            '<span class="pill brand">live</span>'
            "</div>"
            '<div style="padding:18px 18px 12px;">'
            '<svg viewBox="0 0 720 240" width="100%" height="240" preserveAspectRatio="none" style="display:block;">'
            '<g stroke="rgba(255,255,255,0.05)" stroke-width="1">'
            '<line x1="0" x2="720" y1="60" y2="60"/>'
            '<line x1="0" x2="720" y1="120" y2="120"/>'
            '<line x1="0" x2="720" y1="180" y2="180"/>'
            "</g>"
            f'<polyline fill="none" stroke="#c8a8ff" stroke-width="2" points="{polyline}"/>'
            "</svg>"
            "</div>"
            "</div>"
        )
    else:
        chart_html = (
            '<div class="card">'
            '<div class="card-head">'
            f'<div class="card-title">Price · {_esc(market)} · history</div>'
            '<span class="pill">unavailable</span>'
            "</div>"
            '<div class="empty-card">'
            "Live price history was not available for this market snapshot."
            "</div>"
            "</div>"
        )

    book_html = ""
    book_levels = list(extras.get("book") or [])
    if book_levels:
        rows_html: list[str] = []
        for lvl in book_levels:
            if not isinstance(lvl, dict):
                continue
            if lvl.get("mid"):
                rows_html.append(
                    '<div class="mid-row">'
                    f'<span class="price">{_esc(lvl.get("price") or "")}</span>'
                    f'<span class="sp">{_esc(lvl.get("spread") or "")}</span>'
                    "</div>"
                )
                continue
            side = "a" if lvl.get("side") == "ask" else "b"
            width = float(lvl.get("bg_width") or 50)
            rows_html.append(
                f'<div class="brow {side}">'
                f'<span class="bg" style="width:{width:.0f}%;"></span>'
                f'<span class="p">{_esc(lvl.get("price") or "")}</span>'
                f'<span class="s">{_esc(lvl.get("size") or "")}</span>'
                f'<span class="t">{_esc(lvl.get("total") or "")}</span>'
                "</div>"
            )
        book_html = (
            '<div class="card">'
            '<div class="card-head">'
            '<div class="card-title">Order book · top levels</div>'
            f'<span class="pill">{_esc(extras.get("book_pill") or "live")}</span>'
            "</div>"
            '<div class="book-cols">'
            "<span>Price</span><span>Size</span><span>Total</span>"
            "</div>"
            f"{''.join(rows_html)}"
            "</div>"
        )
    else:
        book_html = (
            '<div class="card">'
            '<div class="card-head">'
            '<div class="card-title">Order book · top levels</div>'
            '<span class="pill">unavailable</span>'
            "</div>"
            '<div class="empty-card">'
            "Live orderbook depth was not available for this market snapshot."
            "</div>"
            "</div>"
        )
        if failure_msg:
            book_html = book_html.replace(
                "Live orderbook depth was not available",
                f"Live orderbook depth was not available · {_esc(failure_msg)}",
            )

    body_html = (
        hero_html
        + stats_html
        + f'<div class="body">{chart_html}{book_html}</div>'
    )
    title = model.get("title") or f"{protocol} {market} Snapshot"
    body = (
        f'<div class="sheet kind-market-snapshot" role="img" aria-label="{_esc(alt_text)}">'
        + _render_doc_head("market-snapshot", list(model.get("run_meta") or []))
        + body_html
        + _render_warnings(model)
        + _render_doc_foot(model)
        + _render_alt_caption(alt_text)
        + "</div>"
    )
    return _wf_html_head(title) + f"<body>{body}</body></html>"


def _render_compare_protocols_html(model: JsonMap, alt_text: str) -> str:
    extras = model.get("extras") or {}
    protocols = list(extras.get("protocols") or [])
    rows = list(extras.get("matrix_rows") or [])
    question_label = extras.get("question_label") or "Comparison"
    question_sub = extras.get("question_sub") or ""
    legend_text = extras.get("legend_text") or "all live reads · apy-normalization-v1"
    title = model.get("title") or "Compare protocols"

    col_count = max(2, min(4, len(protocols))) if protocols else 2
    grid_template = f"240px {' '.join(['1fr'] * col_count)}"

    proto_head_parts: list[str] = []
    proto_head_parts.append(
        '<div>'
        f'<div style="font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--wf-fg-faint);">{_esc(question_sub or "Question")}</div>'
        f'<div style="font-family:var(--wf-font-serif);font-size:22px;font-weight:600;margin-top:6px;letter-spacing:-0.01em;">{_esc(question_label)}</div>'
        "</div>"
    )
    for proto in protocols[:col_count]:
        if not isinstance(proto, dict):
            continue
        slug = str(proto.get("slug") or "")
        name = proto.get("name") or slug
        meta = proto.get("meta") or ""
        is_best = bool(proto.get("is_best"))
        best_reason = proto.get("best_reason") or "best fit"
        color = _protocol_token_color(slug)
        bg_style = f"background:{color};"
        logo_inner = _logo_inner(slug, str(name))
        best_html = (
            f'<span class="proto-best">BEST FIT · {_esc(best_reason)}</span>'
            if is_best
            else '<span class="proto-best hidden">BEST FIT</span>'
        )
        proto_head_parts.append(
            "<div>"
            '<div class="proto-name">'
            f'<span class="proto-tok" style="{bg_style}">{logo_inner}</span>'
            f'<div><div class="t">{_esc(name)}</div></div>'
            "</div>"
            f'<div class="proto-meta">{_esc(meta)}</div>'
            f"{best_html}"
            "</div>"
        )

    matrix_rows_html_parts: list[str] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        row_label = row.get("label") or ""
        row_sub = row.get("sub") or ""
        cells = list(row.get("cells") or [])
        cell_parts: list[str] = []
        for cell in cells[:col_count]:
            if not isinstance(cell, dict):
                cell_parts.append(
                    '<div><div class="cell-value unavail">Unavailable</div></div>'
                )
                continue
            value = cell.get("value")
            tone = cell.get("tone")
            note = shorten_addresses_in_text(cell.get("note") or "")
            tag_row = list(cell.get("tags") or [])
            risk_meter = cell.get("risk_meter")
            if value is None or value == "":
                value_html = '<div class="cell-value unavail">Unavailable</div>'
            else:
                tone_class = ""
                if tone == "up":
                    tone_class = " up"
                elif tone == "brand":
                    tone_class = " brand"
                value_html = f'<div class="cell-value{tone_class}">{_esc(value)}</div>'
            tags_html = ""
            if tag_row:
                tag_html_inner = "".join(
                    f'<span class="tag {_esc(t.get("tone") or "")}">{_esc(t.get("text") or "")}</span>'
                    for t in tag_row
                    if isinstance(t, dict)
                )
                tags_html = f'<div class="tag-row">{tag_html_inner}</div>'
            risk_html = ""
            if isinstance(risk_meter, dict):
                level = (risk_meter.get("level") or "med").lower()
                fill = int(risk_meter.get("fill") or 0)
                fill = max(0, min(5, fill))
                pips = "".join(
                    '<i class="on"></i>' if i < fill else "<i></i>"
                    for i in range(5)
                )
                risk_label = risk_meter.get("label") or ""
                risk_class = (
                    "risk-meter low" if level == "low"
                    else "risk-meter high" if level == "high"
                    else "risk-meter"
                )
                risk_html = (
                    f'<div class="risk-label">{_esc(risk_label)}</div>'
                    f'<div class="{risk_class}">{pips}</div>'
                )
                value_html = ""
            note_html = (
                f'<div class="cell-note">{note}</div>' if note else ""
            )
            cell_parts.append(
                f'<div>{risk_html}{value_html}{note_html}{tags_html}</div>'
            )
        while len(cell_parts) < col_count:
            cell_parts.append(
                '<div><div class="cell-value unavail">Unavailable</div></div>'
            )
        matrix_rows_html_parts.append(
            f'<div class="matrix-row" style="grid-template-columns:{grid_template};">'
            f'<div class="matrix-question"><span class="sub">{_esc(row_sub)}</span>{_esc(row_label)}</div>'
            f"{''.join(cell_parts)}"
            "</div>"
        )

    legend_html = (
        '<div class="legend">'
        '<span class="legend-item"><span style="width:10px;height:6px;background:var(--wf-up);border-radius:2px;"></span>low</span>'
        '<span class="legend-item"><span style="width:10px;height:6px;background:var(--wf-warn);border-radius:2px;"></span>medium</span>'
        '<span class="legend-item"><span style="width:10px;height:6px;background:var(--wf-down);border-radius:2px;"></span>high</span>'
        f'<span style="margin-left:auto;">{_esc(legend_text)}</span>'
        "</div>"
    )

    matrix_html = (
        '<div class="matrix">'
        f'<div class="proto-head" style="grid-template-columns:{grid_template};">{"".join(proto_head_parts)}</div>'
        f"{''.join(matrix_rows_html_parts)}"
        f"{legend_html}"
        "</div>"
    )

    body = (
        f'<div class="sheet kind-compare-protocols" role="img" aria-label="{_esc(alt_text)}">'
        + _render_doc_head("compare-protocols", list(model.get("run_meta") or []))
        + _render_title_block(model)
        + matrix_html
        + _render_warnings(model)
        + _render_doc_foot(model)
        + _render_alt_caption(alt_text)
        + "</div>"
    )
    return _wf_html_head(title) + f"<body>{body}</body></html>"


def _render_default_html(model: JsonMap, alt_text: str) -> str:
    title = model.get("title") or "Protocol Infographic"
    summary = model.get("summary") or ""
    sections = list(model.get("sections") or [])
    section_html_parts: list[str] = []
    for section in sections[:4]:
        if not isinstance(section, dict):
            continue
        items_html = "".join(
            f"<li>{_esc(item if isinstance(item, str) else json.dumps(item))}</li>"
            for item in list(section.get("items") or [])[:6]
        )
        section_html_parts.append(
            '<div class="card" style="padding:18px;">'
            f'<div class="card-title">{_esc(section.get("title") or "")}</div>'
            f'<ul style="margin:12px 0 0;padding-left:18px;color:var(--wf-fg-dim);font-size:12px;line-height:1.55;">{items_html}</ul>'
            "</div>"
        )
    body_html = "".join(section_html_parts) or (
        f'<div class="empty-card">{_esc(summary)}</div>'
    )
    body = (
        f'<div class="sheet" role="img" aria-label="{_esc(alt_text)}">'
        + _render_doc_head(str(model.get("kind") or "infographic"), list(model.get("run_meta") or []))
        + _render_title_block(model)
        + f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:28px;">{body_html}</div>'
        + _render_warnings(model)
        + _render_doc_foot(model)
        + _render_alt_caption(alt_text)
        + "</div>"
    )
    return _wf_html_head(title) + f"<body>{body}</body></html>"


# ──────────────────────────────────────────────────────────────────────────
# Public entry points
# ──────────────────────────────────────────────────────────────────────────


def _extract_body_inner(html_text: str) -> str:
    match = re.search(r"<body>(.*)</body>\s*</html>\s*$", html_text, flags=re.S)
    if match:
        return match.group(1)
    return html_text


def _svg_xhtml_safe(html_text: str) -> str:
    # foreignObject content is parsed as XHTML, so void elements must self-close.
    text = html_text.replace("&nbsp;", "&#160;").replace("<br>", "<br/>")
    return re.sub(r"(<img\b[^>]*?)\s*/?>", r"\1/>", text)


def _svg_canvas_height(model: JsonMap) -> int:
    kind = str(model.get("kind") or "")
    extras = model.get("extras") or {}
    if kind == "stablecoin-rates":
        rows = len(list(extras.get("opportunities") or []))
        return max(920, 780 + rows * 48)
    if kind == "compare-protocols":
        cards = len(list(extras.get("protocol_cards") or []))
        rows = len(list(extras.get("comparison_rows") or []))
        return max(1040, 860 + cards * 72 + rows * 46)
    if kind == "market-snapshot":
        return 980
    return 900


def render_svg(model: JsonMap, alt_text: str | None = None) -> str:
    """SVG companion rendered from the same design-system body as HTML."""
    width = 1280
    height = _svg_canvas_height(model)
    title_text = build_alt_text(model) if alt_text is None else alt_text
    html_text = render_html(model, "", title_text)
    body = _svg_xhtml_safe(_extract_body_inner(html_text))
    css = _wf_css() + (
        "\nhtml,body{margin:0;width:1280px;min-height:100%;background:#0b0c0f;}"
        "\n.sheet{margin:0;}"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title_text)}">\n'
        f"<title>{_esc(title_text)}</title>\n"
        f'<rect width="{width}" height="{height}" fill="#0b0c0f"/>\n'
        f'<foreignObject x="0" y="0" width="{width}" height="{height}">\n'
        '<div xmlns="http://www.w3.org/1999/xhtml">\n'
        f"<style>{css}</style>\n"
        f"{body}\n"
        "</div>\n"
        "</foreignObject>\n"
        "</svg>\n"
    )


def _svg_wrap(text: str, max_chars: int) -> list[str]:
    return wrap_text(text, max_chars)


def render_html(model: JsonMap, svg: str, alt_text: str) -> str:
    """Render the design-system HTML for one of the four product kinds.
    `svg` is ignored — the SVG is written to its sibling file; the HTML
    is the primary surface and uses its own inline SVG charts."""
    _ = svg
    kind = str(model.get("kind") or "")
    renderers = {
        "how-it-works": _render_how_it_works_html,
        "stablecoin-rates": _render_stablecoin_rates_html,
        "market-snapshot": _render_market_snapshot_html,
        "compare-protocols": _render_compare_protocols_html,
    }
    renderer = renderers.get(kind, _render_default_html)
    return renderer(model, alt_text)


def build_alt_text(model: JsonMap) -> str:
    eyebrow = str(model.get("eyebrow") or "")
    title = str(model.get("title") or "")
    em = str(model.get("headline_em") or "")
    post = str(model.get("title_post") or "")
    headline = " ".join(p for p in [title, em, post] if p).strip()
    deck = re.sub(r"<[^>]+>", "", str(model.get("deck") or model.get("summary") or ""))
    deck = re.sub(r"\s+", " ", deck).strip()
    parts: list[str] = []
    if eyebrow:
        parts.append(eyebrow + ".")
    if headline:
        parts.append(headline)
    if deck:
        parts.append(deck)
    metrics = list(model.get("metrics") or [])
    if metrics and not deck:
        parts.append(
            "Metrics: "
            + "; ".join(f"{item.get('label')}: {item.get('value')}" for item in metrics[:4])
        )
    warnings = list(model.get("warnings") or [])
    if warnings:
        parts.append("Warnings: " + "; ".join(str(item) for item in warnings[:3]))
    return " ".join(parts)


def validate_model(model: JsonMap) -> JsonMap:
    errors: list[JsonMap] = []
    warnings: list[str] = [str(item) for item in list(model.get("warnings") or [])]
    if not model.get("title"):
        errors.append({"code": "layout_validation_failed", "message": "Missing title."})
    if not model.get("summary"):
        errors.append({"code": "layout_validation_failed", "message": "Missing summary."})
    if len(str(model.get("title") or "")) > 90:
        warnings.append("Title is long and may wrap to more than two lines.")
    if not model.get("sources"):
        warnings.append("No sources were attached to this infographic.")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "title_present": bool(model.get("title")),
            "summary_present": bool(model.get("summary")),
            "sources_present": bool(model.get("sources")),
            "risk_notes_present": bool(model.get("risks")),
        },
    }


def artifact_paths(run_id: str) -> JsonMap:
    root = artifacts_root() / run_id
    return {
        "root": root,
        "publish": root / "publish.json",
        "adapter_inventory": root / "adapter_inventory.json",
        "request": root / "request.json",
        "data_snapshot": root / "data_snapshot.json",
        "live_snapshot": root / "live_snapshot.json",
        "design_spec": root / "design_spec.json",
        "html": root / "infographic.html",
        "svg": root / "infographic.svg",
        "alt_text": root / "alt_text.md",
        "validation": root / "validation.json",
    }


def write_artifacts(
    *,
    run_id: str,
    request: JsonMap,
    inventory: list[JsonMap],
    data_snapshot: JsonMap,
    live_snapshot: JsonMap,
    model: JsonMap,
) -> tuple[JsonMap, JsonMap]:
    paths = artifact_paths(run_id)
    validation = validate_model(model)
    if not validation["ok"]:
        raise PathRuntimeError(
            "layout_validation_failed",
            "Generated model failed validation.",
            details=validation,
        )
    alt_text = build_alt_text(model)
    svg = render_svg(model, alt_text)
    html_text = render_html(model, svg, alt_text)
    write_json(paths["adapter_inventory"], {"generated_at": iso_now(), "adapters": inventory})
    write_json(paths["request"], request)
    write_json(paths["data_snapshot"], data_snapshot)
    write_json(paths["live_snapshot"], live_snapshot)
    write_json(paths["design_spec"], model)
    write_text(paths["svg"], svg)
    write_text(paths["html"], html_text)
    write_text(paths["alt_text"], alt_text + "\n")
    write_json(paths["validation"], validation)
    return paths, validation


def path_string(path: Path) -> str:
    return str(path.resolve())


def truncate_text(value: str, *, max_len: int = 1600) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def publish_disabled_result() -> JsonMap:
    return {
        "enabled": False,
        "published": False,
        "provider": None,
        "artifact_url": None,
    }


def publish_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "publish", False))


def first_env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def publish_config_from_args(args: argparse.Namespace, run_id: str) -> PublishConfig:
    load_local_env_file()
    provider = str(getattr(args, "publish_provider", "cloudflare-pages") or "")
    if provider not in {"cloudflare-pages", "cloudflare_pages"}:
        raise PathRuntimeError(
            "unsupported_publish_provider",
            f"Unsupported publish provider: {provider}",
            details={"provider": provider, "supported": ["cloudflare-pages"]},
        )
    project = first_env_value(
        "CLOUDFLARE_PAGES_PROJECT",
        "CLOUDFLARE_PAGE_PROJECT",
        "PAGES_PROJECT",
        "PAGE_PROJECT",
        "pages_project",
        "page_project",
    )
    account_id = first_env_value("CLOUDFLARE_ACCOUNT_ID", "ACCOUNT_ID", "account_id")
    api_token = first_env_value("CLOUDFLARE_API_TOKEN", "API_TOKEN", "api_token")
    branch_value = str(getattr(args, "publish_branch", "") or "").strip()
    branch = clean_slug(branch_value or run_id)
    return PublishConfig(
        provider="cloudflare_pages",
        project=project,
        account_id=account_id,
        api_token_present=bool(api_token),
        branch=branch,
        include_data=bool(getattr(args, "publish_data", False)),
        required=bool(getattr(args, "publish_required", False)),
    )


def validate_publish_config(config: PublishConfig) -> None:
    missing = []
    if not config.project:
        missing.append("CLOUDFLARE_PAGES_PROJECT")
    if not config.account_id:
        missing.append("CLOUDFLARE_ACCOUNT_ID")
    if not config.api_token_present:
        missing.append("CLOUDFLARE_API_TOKEN")
    if missing:
        raise PathRuntimeError(
            "publish_config_missing",
            "Cloudflare Pages publish requires environment variables.",
            details={"missing_env": missing, "provider": config.provider},
        )


def stage_publish_directory(paths: JsonMap, *, include_data: bool) -> Path:
    stage = Path(paths["root"]) / "_publish"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    public_keys = ["html", "svg", "alt_text"]
    if include_data:
        public_keys.extend(
            [
                "adapter_inventory",
                "request",
                "data_snapshot",
                "live_snapshot",
                "design_spec",
                "validation",
            ]
        )
    for key in public_keys:
        source = Path(paths[key])
        if source.exists():
            shutil.copy2(source, stage / source.name)
    shutil.copy2(Path(paths["html"]), stage / "index.html")
    return stage


def wrangler_command() -> list[str]:
    override = os.environ.get("CLOUDFLARE_WRANGLER_CMD", "").strip()
    if override:
        return shlex.split(override)
    return ["npx", "--yes", "wrangler"]


def parse_pages_url(output: str, *, project: str, branch: str) -> str:
    urls = re.findall(r"https://[^\s\"']+?\.pages\.dev", output)
    preferred_suffix = f"{branch}.{project}.pages.dev"
    for url in urls:
        if preferred_suffix in url:
            return url
    if urls:
        return urls[-1]
    return f"https://{branch}.{project}.pages.dev"


def publish_cloudflare_pages(paths: JsonMap, run_id: str, config: PublishConfig) -> JsonMap:
    validate_publish_config(config)
    stage = stage_publish_directory(paths, include_data=config.include_data)
    cmd = wrangler_command() + [
        "pages",
        "deploy",
        str(stage),
        "--project-name",
        config.project,
        "--branch",
        config.branch,
    ]
    timeout = float(os.environ.get("CLOUDFLARE_PAGES_TIMEOUT_SECONDS", "180") or "180")
    env = os.environ.copy()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(PATH_DIR),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PathRuntimeError(
            "publish_tool_missing",
            "Cloudflare Pages publish requires npx or a CLOUDFLARE_WRANGLER_CMD override.",
            details={"provider": config.provider, "tool": cmd[0]},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PathRuntimeError(
            "publish_timeout",
            "Cloudflare Pages publish timed out.",
            details={"provider": config.provider, "timeout_seconds": timeout},
        ) from exc

    combined_output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0:
        raise PathRuntimeError(
            "publish_failed",
            "Cloudflare Pages publish failed.",
            details={
                "provider": config.provider,
                "returncode": completed.returncode,
                "stdout": truncate_text(completed.stdout),
                "stderr": truncate_text(completed.stderr),
            },
        )

    deployment_url = parse_pages_url(combined_output, project=config.project, branch=config.branch).rstrip("/")
    return {
        "enabled": True,
        "published": True,
        "provider": config.provider,
        "project": config.project,
        "branch": config.branch,
        "run_id": run_id,
        "deployment_url": deployment_url,
        "artifact_url": f"{deployment_url}/infographic.html",
        "files": {
            "html": f"{deployment_url}/infographic.html",
            "svg": f"{deployment_url}/infographic.svg",
            "alt_text": f"{deployment_url}/alt_text.md",
            "root": deployment_url,
        },
        "include_data": config.include_data,
    }


def publish_artifacts_if_requested(args: argparse.Namespace, run_id: str, paths: JsonMap) -> JsonMap:
    if not publish_requested(args):
        return publish_disabled_result()
    config = publish_config_from_args(args, run_id)
    try:
        result = publish_cloudflare_pages(paths, run_id, config)
    except PathRuntimeError as exc:
        result = {
            "enabled": True,
            "published": False,
            "provider": config.provider,
            "project": config.project or None,
            "branch": config.branch,
            "artifact_url": None,
            "include_data": config.include_data,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        }
        write_json(Path(paths["publish"]), result)
        if config.required:
            raise
        return result
    write_json(Path(paths["publish"]), result)
    return result


def compact_source_label(value: str, *, max_len: int = 48) -> str:
    text = str(value)
    if text.startswith("https://"):
        text = text.removeprefix("https://")
    elif text.startswith("http://"):
        text = text.removeprefix("http://")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def base_success_envelope(
    *,
    action: str,
    kind: str,
    run_id: str,
    paths: JsonMap,
    validation: JsonMap,
    preview_url: str | None,
    publish: JsonMap | None = None,
) -> JsonMap:
    publish_result = publish or publish_disabled_result()
    files = {
        "adapter_inventory": path_string(paths["adapter_inventory"]),
        "request": path_string(paths["request"]),
        "data_snapshot": path_string(paths["data_snapshot"]),
        "live_snapshot": path_string(paths["live_snapshot"]),
        "design_spec": path_string(paths["design_spec"]),
        "html": path_string(paths["html"]),
        "svg": path_string(paths["svg"]),
        "alt_text": path_string(paths["alt_text"]),
        "validation": path_string(paths["validation"]),
    }
    if Path(paths["publish"]).exists():
        files["publish"] = path_string(paths["publish"])
    return {
        "ok": True,
        "action": action,
        "kind": kind,
        "run_id": run_id,
        "render_mode": "static_html",
        "artifacts_dir": path_string(paths["root"]),
        "primary_artifact": path_string(paths["html"]),
        "artifact_url": publish_result.get("artifact_url"),
        "files": files,
        "preview_url": preview_url,
        "publish": publish_result,
        "validation": validation,
    }


async def handle_how_it_works(args: argparse.Namespace) -> JsonMap:
    style = load_style(args.style)
    source_artifact = load_source_artifact(args.source_artifact)
    inventory = list_adapter_inventory()
    info = get_adapter_info(args.adapter, inventory)
    profile = merge_source_mechanism(mechanism_profile_for(info), source_artifact)
    official_docs = [str(url) for url in list(profile.get("official_docs") or [])]
    doc_metadata, doc_warnings = await load_protocol_doc_metadata(
        urls=official_docs,
        docs_mode=str(args.docs_mode),
    )
    live_metrics, metric_failures = await fetch_how_it_works_metrics(
        info=info,
        chain=args.chain,
        metrics_mode=str(args.metrics_mode),
    )
    metric_warnings = (
        ["Some live protocol metrics were unavailable for this snapshot."]
        if metric_failures
        else []
    )
    grouped = group_capabilities(info.capabilities)
    sources = protocol_public_sources(info, official_docs[:3])

    actors = [str(item) for item in list(profile.get("actors") or [])]
    state = [str(item) for item in list(profile.get("state") or [])]
    flow_steps = [str(item) for item in list(profile.get("mechanism_steps") or [])]
    docs_ok = sum(1 for item in doc_metadata if item.get("ok"))
    doc_notes = doc_note_rows(doc_metadata, official_docs)
    primitive = str(profile.get("primitive") or infer_adapter_category(info))
    primitive_metric = primitive if len(primitive) <= 22 else primitive[:19] + "..."
    metrics = [
        {"label": "Primitive", "value": primitive_metric},
        {"label": "Core State", "value": str(len(state))},
        {"label": "Docs", "value": str(docs_ok or len(official_docs))},
    ]
    if live_metrics:
        metrics.append(
            {
                "label": "Live Markets",
                "value": str(sum(int(item.get("market_count") or 0) for item in live_metrics)),
            }
        )
    elif metric_failures:
        metrics.append({"label": "Live Metrics", "value": "unavailable"})
    sections = [
        {"title": "Actors", "items": actors or ["Protocol docs did not declare actors."]},
        {"title": "State", "items": state or ["Protocol docs did not declare state variables."]},
        {
            "title": "Protocol Docs",
            "items": [
                str(note.get("title") or note.get("url"))
                for note in doc_notes
                if isinstance(note, dict)
            ]
            or ["No official protocol docs URL is registered yet."],
        },
        {
            "title": "Docs Used",
            "items": [
                compact_source_label(str(item.get("title") or item.get("url")), max_len=40)
                for item in doc_metadata
                if item.get("ok")
            ]
            or [compact_source_label(url, max_len=40) for url in official_docs[:3]]
            or ["No official protocol docs URL is registered yet."],
        },
    ]
    model = build_model(
        kind="how-it-works",
        title=f"{info.protocol}: How It Works",
        subtitle=primitive,
        summary=str(profile.get("mechanism_summary") or describe_protocol(info)),
        metrics=metrics,
        sections=sections,
        risks=info.risks,
        sources=sources,
        warnings=doc_warnings + metric_warnings,
        style=style,
        flow_steps=flow_steps or mechanics_for(info),
        visual_metrics=build_visual_metrics(
            info=info,
            profile=profile,
            doc_count=docs_ok or len(official_docs),
            live_metrics=live_metrics,
        ),
        chips=[primitive, f"{len(actors)} actors", f"{len(state)} state surfaces"],
    )
    request = {
        "action": "how-it-works",
        "adapter": info.slug,
        "docs_mode": args.docs_mode,
        "metrics_mode": args.metrics_mode,
        "chain": args.chain,
        "style": args.style,
        "source_artifact_loaded": bool(source_artifact),
    }
    run_id = make_run_id("how-it-works", request)
    data_snapshot = {
        "request": request,
        "protocol": info.protocol,
        "category": infer_adapter_category(info),
        "mechanism_profile": profile,
        "docs": doc_metadata,
        "live_metrics": live_metrics,
        "metric_failures": metric_failures,
        "capabilities": grouped,
        "risks": info.risks,
        "support": info.support,
        "source_artifact": source_artifact,
    }
    live_snapshot: JsonMap = {
        "attempted": args.docs_mode == "fetch" or args.metrics_mode == "live",
        "docs_mode": args.docs_mode,
        "docs": doc_metadata,
        "warnings": doc_warnings + metric_warnings,
        "metrics_attempted": args.metrics_mode == "live",
        "metrics": live_metrics,
        "metric_failures": metric_failures,
    }
    chain_label = _chain_slug(chain_id_for(args.chain))
    if not chain_label and live_metrics:
        first = live_metrics[0] if isinstance(live_metrics[0], dict) else {}
        first_chain = first.get("chain_id")
        if first_chain:
            chain_label = _chain_slug(int(first_chain))
    run_meta_lines = [
        ("PROTOCOL", info.protocol),
        ("CHAIN", chain_label.title() if chain_label else "Cross-market"),
        ("UPDATED", iso_now()[:10]),
    ]
    deck_html = (
        "Mechanism map built from protocol docs, market state surfaces, and live public reads"
        + (
            f' on <span class="wf-mono">{_esc(chain_label)}</span>'
            if chain_label
            else ""
        )
        + "."
    )
    stat_strip_extras: list[JsonMap] = []
    if live_metrics:
        summary = live_metrics[0] if isinstance(live_metrics[0], dict) else {}
        if summary.get("best_supply_apy") is not None:
            stat_strip_extras.append(
                {
                    "k": "Best supply APY",
                    "v": _format_percent(summary.get("best_supply_apy")),
                    "v_class": "up",
                    "sub": "live read",
                }
            )
        if summary.get("median_supply_apy") is not None:
            stat_strip_extras.append(
                {
                    "k": "Median supply APY",
                    "v": _format_percent(summary.get("median_supply_apy")),
                    "sub": "across markets",
                }
            )
        if summary.get("market_count") is not None:
            stat_strip_extras.append(
                {
                    "k": "Markets",
                    "v": str(summary.get("market_count")),
                    "sub": f"{summary.get('stable_market_count') or 0} stable",
                }
            )
        if summary.get("total_tvl_usd") is not None:
            stat_strip_extras.append(
                {
                    "k": "Total TVL",
                    "v": _format_usd_compact(summary.get("total_tvl_usd")),
                    "sub": "supplied",
                }
            )
        stat_strip_extras.append(
            {"k": "Read", "v": "LIVE", "live": True, "sub": "best-effort"}
        )
    if not stat_strip_extras:
        stat_strip_extras = [
            {"k": "Primitive", "v": primitive_metric, "sub": infer_adapter_category(info)},
            {"k": "Actors", "v": str(len(actors)), "sub": "protocol roles"},
            {"k": "State surfaces", "v": str(len(state)), "sub": "market variables"},
            {"k": "Docs", "v": str(docs_ok or len(official_docs)), "sub": "official references"},
            {"k": "Live metrics", "v": None, "sub": "unavailable"},
        ]
    mechanism_steps_extras: list[JsonMap] = []
    base_steps = list(flow_steps or mechanics_for(info))
    for index, step in enumerate(base_steps[:5]):
        text = str(step)
        first_period = text.find(".")
        if 8 < first_period < 80:
            head_text = text[: first_period + 1].strip()
            body_text = text[first_period + 1 :].strip()
        elif len(text) > 80:
            head_text = text[:60].rstrip() + "…"
            body_text = text
        else:
            head_text = text
            body_text = ""
        mechanism_steps_extras.append(
            {
                "n": f"{index + 1:02d}",
                "label": step_label_from_text(text),
                "h": head_text,
                "b": body_text,
            }
        )
    risk_rows_extras = [
        {"severity": "med", "title": str(r)} for r in info.risks[:4]
    ]
    model["eyebrow"] = f"How it works · {primitive}"
    model["title"] = "How"
    model["headline_em"] = info.protocol
    model["title_post"] = (
        f"works{f' on {chain_label}' if chain_label else ''}."
    )
    model["deck"] = deck_html
    model["protocol_logo_url"] = protocol_logo_url_for_slug(info.slug)
    model["protocol_slug"] = info.slug
    model["protocol_name"] = info.protocol
    model["run_meta"] = run_meta_lines
    model["extras"] = {
        "stat_strip": stat_strip_extras,
        "mechanism_steps": mechanism_steps_extras,
        "risk_rows": risk_rows_extras,
        "actors": actors,
        "state_surfaces": state,
        "doc_notes": doc_notes,
    }
    paths, validation = write_artifacts(
        run_id=run_id,
        request=request,
        inventory=inventory,
        data_snapshot=data_snapshot,
        live_snapshot=live_snapshot,
        model=model,
    )
    publish_result = publish_artifacts_if_requested(args, run_id, paths)
    envelope = base_success_envelope(
        action="how-it-works",
        kind="how-it-works",
        run_id=run_id,
        paths=paths,
        validation=validation,
        preview_url=preview_url(paths["root"]) if args.serve else None,
        publish=publish_result,
    )
    envelope.update({"adapter": info.slug, "protocol_name": info.protocol})
    return envelope


def stablecoin_candidates(
    *,
    inventory: list[JsonMap],
    venue: str | None,
) -> list[str]:
    support = load_support().get("adapters") or {}
    if venue:
        return [venue]
    candidates: list[str] = []
    verified = []
    unverified = []
    for slug, entry in support.items():
        if "stablecoin-lending" not in set(entry.get("use_cases") or []):
            continue
        status = (entry.get("verification") or {}).get("stablecoin-rates", "")
        if status == "verified":
            verified.append(slug)
        elif "template-compatible" in str(status):
            unverified.append(slug)
    adapter_slugs = {item["slug"] for item in inventory}
    candidates.extend(slug for slug in sorted(verified) if slug in adapter_slugs)
    candidates.extend(slug for slug in sorted(unverified) if slug in adapter_slugs)
    return candidates


async def handle_stablecoin_rates(args: argparse.Namespace) -> JsonMap:
    asset = str(args.asset).upper()
    if asset not in SUPPORTED_STABLES:
        raise PathRuntimeError(
            "unsupported_use_case",
            f"Unsupported stablecoin for v0.1: {asset}",
            details={"asset": asset, "supported": sorted(SUPPORTED_STABLES)},
        )
    style = load_style(args.style)
    source_artifact = load_source_artifact(args.source_artifact)
    inventory = list_adapter_inventory()
    candidate_slugs = stablecoin_candidates(inventory=inventory, venue=args.venue)
    adapters = [get_adapter_info(slug, inventory) for slug in candidate_slugs]
    verified = [
        info.slug
        for info in adapters
        if ((info.support.get("verification") or {}).get("stablecoin-rates") == "verified")
    ]
    unverified = [info.slug for info in adapters if info.slug not in verified]
    live_rows, live_failures = await fetch_stablecoin_rates(
        adapters=[info for info in adapters if info.slug in verified or args.venue],
        asset=asset,
        chain=args.chain,
        min_tvl=float(args.min_tvl or 0),
        action="stablecoin-rates",
    )
    if source_artifact and isinstance(source_artifact.get("normalized_rates"), list):
        live_rows.extend(
            row for row in source_artifact["normalized_rates"] if isinstance(row, dict)
        )
    ranked = sorted(
        [row for row in live_rows if row.get("net_apy") is not None],
        key=lambda row: float(row.get("net_apy") or 0.0),
        reverse=True,
    )
    top_rows = ranked[:5]
    warnings = []
    if unverified:
        warnings.append(f"{len(unverified)} additional protocol venues were not included in the ranked set.")
    if live_failures:
        warnings.append("Some protocol venues did not return a current stablecoin rate.")
    if not ranked:
        warnings.append("No rankable APY rows were fetched. Values are unavailable rather than estimated.")

    metrics = [
        {"label": "Asset", "value": asset},
        {"label": "Live Rows", "value": str(len(ranked))},
        {"label": "Protocol Venues", "value": str(len(verified))},
    ]
    if top_rows:
        metrics.append({"label": "Best Net APY", "value": format_rate(top_rows[0].get("net_apy"))})
    rate_items = [
        f"{index + 1}. {row['protocol']}: net {format_rate(row.get('net_apy'))}, gross {format_rate(row.get('gross_apy'))}, rewards {format_rate(row.get('reward_apy'))}, TVL {format_usd(row.get('tvl_usd'))}"
        for index, row in enumerate(top_rows)
    ]
    if not rate_items:
        rate_items = ["No live rankable rates were available from the verified protocols in this run."]
    verified_protocols = [info.protocol for info in adapters if info.slug in verified]
    unverified_protocols = [info.protocol for info in adapters if info.slug in unverified]
    sections = [
        {"title": "Best Available Rates", "items": rate_items},
        {
            "title": "Normalization",
            "items": [
                f"Version: {APY_NORMALIZATION_VERSION}",
                "Gross APY is the protocol base supply APY when exposed.",
                "Net APY uses protocol net values when present; otherwise base supply APY plus exposed rewards is used only when fees are known.",
                "Rows with unknown fees are excluded from rankings.",
            ],
        },
        {
            "title": "Coverage",
            "items": [
                f"Verified: {', '.join(verified_protocols) or 'none'}",
                f"Template-compatible, unverified: {', '.join(unverified_protocols) or 'none'}",
            ],
        },
    ]
    risks = []
    for info in adapters[:3]:
        risks.extend(info.risks[:1])
    model = build_model(
        kind="stablecoin-rates",
        title=f"{asset} Stablecoin Rates",
        subtitle=f"Protocol supply markets{f' on {args.chain}' if args.chain else ''}",
        summary="Best-effort APY infographic using current protocol lending market data and explicit APY normalization.",
        metrics=metrics,
        sections=sections,
        risks=risks[:3],
        sources=[
            src
            for info in adapters
            for src in [protocol_homepage_for_slug(info.slug)]
            if src
        ],
        warnings=warnings,
        style=style,
    )
    request = {
        "action": "stablecoin-rates",
        "asset": asset,
        "chain": args.chain,
        "venue": args.venue,
        "min_tvl": float(args.min_tvl or 0),
        "style": args.style,
        "apy_normalization_version": APY_NORMALIZATION_VERSION,
        "source_artifact_loaded": bool(source_artifact),
    }
    run_id = make_run_id("stablecoin-rates", request)
    data_snapshot = {
        "request": request,
        "verified_adapters": verified,
        "template_compatible_unverified": unverified,
        "normalized_rates": live_rows,
        "ranked_rates": ranked,
    }
    live_snapshot = {
        "attempted": True,
        "failures": live_failures,
        "row_count": len(live_rows),
        "rankable_row_count": len(ranked),
    }
    chain_label = _chain_slug(chain_id_for(args.chain)) or (args.chain or "")
    # Dedupe by venue: keep highest-APY market per protocol so the table
    # reads as a venue-vs-venue comparison, not per-market noise.
    venue_top: dict[str, JsonMap] = {}
    for row in ranked:
        venue = str(row.get("venue") or "")
        if not venue:
            continue
        if venue not in venue_top:
            venue_top[venue] = row
    venue_rows = sorted(
        venue_top.values(),
        key=lambda r: float(coerce_float(r.get("net_apy")) or 0.0),
        reverse=True,
    )
    rate_rows_extras: list[JsonMap] = []
    for index, row in enumerate(venue_rows[:6]):
        venue = str(row.get("venue") or "")
        protocol = str(row.get("protocol") or venue or "")
        chain_id_value = row.get("chain_id")
        chain_extra = _chain_slug(int(chain_id_value)) if chain_id_value else ""
        sub_parts = [chain_extra.title()] if chain_extra else []
        market_id = str(row.get("market_id") or "")
        if market_id and market_id != "unknown":
            sub_parts.append(_shorten(market_id, max_len=18))
        rate_rows_extras.append(
            {
                "protocol": protocol,
                "venue": venue,
                "sub": " · ".join(sub_parts) or "current supply market",
                "net_apy": row.get("net_apy"),
                "delta_30d": None,
                "utilization": None,
                "tvl_usd": row.get("tvl_usd"),
                "is_best": index == 0,
                "logo_url": protocol_logo_url_for_slug(venue),
            }
        )
    failure_lookup: dict[str, JsonMap] = {}
    for failure in live_failures:
        slug = str(failure.get("adapter") or "")
        if slug and slug not in failure_lookup:
            failure_lookup[slug] = failure
    successful_slugs = set(venue_top.keys())
    unavail_rows_extras: list[JsonMap] = []
    for info in adapters:
        if info.slug in successful_slugs:
            continue
        failure = failure_lookup.get(info.slug)
        reason = "read returned no rows"
        if failure:
            reason = "No current protocol row"
        unavail_rows_extras.append(
            {
                "protocol": info.protocol,
                "venue": info.slug,
                "reason": reason if not reason.lower().startswith("adapter") else "No current protocol row",
                "retry": "not ranked",
                "logo_url": protocol_logo_url_for_slug(info.slug),
            }
        )
    best_extras: JsonMap = {}
    rank_summary_rows = venue_rows or ranked
    if rank_summary_rows:
        top = rank_summary_rows[0]
        gap = ""
        if len(rank_summary_rows) >= 2:
            next_row = rank_summary_rows[1]
            delta = (coerce_float(top.get("net_apy")) or 0) - (coerce_float(next_row.get("net_apy")) or 0)
            gap = (
                f"{delta * 100:+.2f}pp over {next_row.get('protocol') or 'next venue'}"
                if delta
                else ""
            )
        best_extras = {
            "apy": top.get("net_apy"),
            "sub": (
                f"{top.get('protocol')}"
                + (f". {gap}." if gap else ".")
            ),
        }
    else:
        best_extras = {"apy": None, "sub": "No rankable supply APY in this run."}
    summary_kv_extras = [
        ["ranking method", "net supply APY"],
        ["protocols checked", str(len(adapters))],
        ["live venues ok", str(len(venue_top)), "up" if venue_top else ""],
        ["unavailable venues", str(len(live_failures)), "down" if live_failures else ""],
        ["ranked venues", str(len(venue_rows))],
        ["snapshot · " + (chain_label or "—"), iso_now()[:10]],
    ]
    caveats_html = (
        "Rates are point-in-time protocol supply APYs. "
        "Rows without enough fee information are excluded from the ranking. "
        "Unavailable venues render as <span class=\"wf-mono\">Unavailable</span>, never zero."
    )
    deck_html = (
        f"{len(adapters)} lending venues polled for "
        f"<span class=\"wf-mono\">{_esc(asset)}</span>"
        + (f" on <span class=\"wf-mono\">{_esc(chain_label)}</span>" if chain_label else "")
        + ", ranked by net supply APY. "
        "Protocol logos are loaded from public web sources. "
        "Missing rates render as Unavailable — never inferred."
    )
    run_meta_lines = [
        (
            "ASSET",
            f"{asset}{' · chain · ' + chain_label if chain_label else ''}",
        ),
        ("PROTOCOLS", f"{len(venue_top)}/{len(adapters)} live"),
        ("UPDATED", iso_now()[:10]),
    ]
    model["eyebrow"] = (
        f"Stablecoin rates · {asset}"
        + (f" · {chain_label}" if chain_label else "")
    )
    model["title"] = "Top supply APYs for"
    model["headline_em"] = asset
    model["title_post"] = (
        f"on {chain_label}, today."
        if chain_label
        else "right now."
    )
    model["deck"] = deck_html
    model["run_meta"] = run_meta_lines
    model["extras"] = {
        "asset": asset,
        "rate_rows": rate_rows_extras,
        "unavail_rows": unavail_rows_extras,
        "best": best_extras,
        "summary_kv": summary_kv_extras,
        "caveats": caveats_html,
        "head_pill": "LIVE READ" if ranked else "READ FAILED",
    }
    paths, validation = write_artifacts(
        run_id=run_id,
        request=request,
        inventory=inventory,
        data_snapshot=data_snapshot,
        live_snapshot=live_snapshot,
        model=model,
    )
    publish_result = publish_artifacts_if_requested(args, run_id, paths)
    envelope = base_success_envelope(
        action="stablecoin-rates",
        kind="stablecoin-rates",
        run_id=run_id,
        paths=paths,
        validation=validation,
        preview_url=preview_url(paths["root"]) if args.serve else None,
        publish=publish_result,
    )
    envelope.update(
        {
            "asset": asset,
            "venues": [info.slug for info in adapters],
            "verified_adapters": verified,
            "template_compatible_unverified": unverified,
            "apy_normalization_version": APY_NORMALIZATION_VERSION,
        }
    )
    return envelope


async def handle_market_snapshot(args: argparse.Namespace) -> JsonMap:
    style = load_style(args.style)
    source_artifact = load_source_artifact(args.source_artifact)
    inventory = list_adapter_inventory()
    info = get_adapter_info(args.adapter, inventory)
    snapshot: JsonMap | None = None
    live_failures: list[JsonMap] = []
    graph_data: JsonMap = {"chart_points": [], "book": [], "book_pill": "depth unavailable"}
    if info.slug == "hyperliquid_adapter":
        snapshot, live_failures = await fetch_hyperliquid_market_snapshot(str(args.market))
        fetched_graphs, graph_failures = await fetch_hyperliquid_market_graphs(str(args.market))
        graph_data.update(fetched_graphs)
        live_failures.extend(graph_failures)
    else:
        live_failures.append(
            {
                "adapter": info.slug,
                "status": "template-compatible-unverified",
                "message": "Current market data is not available for this protocol in v0.1.",
            }
        )
    if source_artifact and isinstance(source_artifact.get("market_snapshot"), dict):
        snapshot = dict(source_artifact["market_snapshot"])

    warnings = []
    if live_failures:
        warnings.append("Some live market data was unavailable for this snapshot.")
    if snapshot is None:
        warnings.append("No live market snapshot was available.")

    metrics = [
        {"label": "Market", "value": str(args.market).upper()},
        {"label": "Venue", "value": info.protocol},
    ]
    if snapshot:
        if "mark_price" in snapshot:
            metrics.append({"label": "Mark", "value": str(snapshot.get("mark_price"))})
        if "funding" in snapshot:
            metrics.append({"label": "Funding", "value": str(snapshot.get("funding"))})
        if "open_interest" in snapshot:
            metrics.append({"label": "Open Interest", "value": str(snapshot.get("open_interest"))})

    snapshot_items = (
        [f"{key}: {value}" for key, value in snapshot.items() if key not in {"source"}][:8]
        if snapshot
        else ["Live market data unavailable for this run."]
    )
    sections = [
        {"title": "Market Snapshot", "items": snapshot_items},
        {"title": "Market Mechanics", "items": mechanics_for(info)},
    ]
    model = build_model(
        kind="market-snapshot",
        title=f"{info.protocol} {str(args.market).upper()} Snapshot",
        subtitle=f"{str(args.market).upper()} market state",
        summary="One-market infographic built from current protocol market data when available.",
        metrics=metrics,
        sections=sections,
        risks=info.risks,
        sources=protocol_public_sources(info),
        warnings=warnings,
        style=style,
    )
    request = {
        "action": "market-snapshot",
        "adapter": info.slug,
        "market": args.market,
        "chain": args.chain,
        "style": args.style,
        "source_artifact_loaded": bool(source_artifact),
    }
    run_id = make_run_id("market-snapshot", request)
    data_snapshot = {
        "request": request,
        "market_snapshot": snapshot,
        "risks": info.risks,
        "support": info.support,
    }
    live_snapshot = {"attempted": True, "failures": live_failures, "snapshot": snapshot, "graphs": graph_data}
    market_upper = str(args.market).upper()
    chain_label = _chain_slug(chain_id_for(args.chain)) or ""
    live_ok = bool(snapshot)
    failure_msg = ""
    if live_failures and not live_ok:
        first_failure = live_failures[0] if isinstance(live_failures[0], dict) else {}
        failure_msg = str(first_failure.get("error") or first_failure.get("message") or "read failed")[:80]
    funding_hourly = None
    funding_apr = None
    if snapshot:
        funding_raw = coerce_float(snapshot.get("funding"))
        if funding_raw is not None:
            funding_hourly = funding_raw
            funding_apr = funding_raw * 24 * 365
    token_glyphs = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "DOGE": "Ð"}
    token_color = {
        "BTC": "#f7931a",
        "ETH": "#627eea",
        "SOL": "#14f195",
        "DOGE": "#c2a633",
    }.get(market_upper, "#f7931a")
    stats_extras: list[JsonMap] = []
    if snapshot:
        stats_extras = [
            {
                "k": "Mark",
                "v": (
                    f"{coerce_float(snapshot.get('mark_price')) or 0:,.1f}"
                    if snapshot.get("mark_price") is not None
                    else None
                ),
            },
            {
                "k": "Oracle",
                "v": (
                    f"{coerce_float(snapshot.get('oracle_price')) or 0:,.1f}"
                    if snapshot.get("oracle_price") is not None
                    else None
                ),
            },
            {
                "k": "24h Vol",
                "v": _format_usd_compact(snapshot.get("day_volume")),
            },
            {
                "k": "Open Int.",
                "v": _format_usd_compact(snapshot.get("open_interest")),
            },
            {
                "k": "Funding · 1h",
                "v": _format_percent(funding_hourly, signed=True) if funding_hourly is not None else None,
                "tone": "up"
                if (funding_hourly or 0) >= 0
                else "down",
            },
            {
                "k": "Funding · APR",
                "v": _format_percent(funding_apr, signed=True) if funding_apr is not None else None,
                "tone": "up"
                if (funding_apr or 0) >= 0
                else "down",
            },
            {
                "k": "Max Lev.",
                "v": (
                    f"{int(snapshot.get('max_leverage'))}×"
                    if snapshot.get("max_leverage") is not None
                    else None
                ),
            },
            {
                "k": "Isolated",
                "v": "Yes" if snapshot.get("only_isolated") else "No",
            },
        ]
    else:
        stats_extras = [{"k": k, "v": None} for k in [
            "Mark", "Oracle", "24h Vol", "Open Int.", "Funding · 1h",
            "Funding · APR", "Max Lev.", "Isolated",
        ]]
    run_meta_lines = [
        ("MARKET", market_upper),
        ("VENUE", info.protocol),
        ("UPDATED", iso_now()[:10]),
    ]
    deck_html = (
        "A one-shot read of mark, funding, open interest, and orderbook depth"
        + (
            f" for <span class=\"wf-mono\">{_esc(market_upper)}</span>"
            if market_upper
            else ""
        )
        + f" on <span class=\"wf-mono\">{_esc(info.protocol)}</span>. "
        "Missing data shows as <span class=\"wf-mono\">Unavailable</span>, never inferred."
    )
    read_meta_text = ""
    if snapshot:
        ts = _esc(str(snapshot.get("timestamp") or iso_now()))
        oracle_val = snapshot.get("oracle_price")
        oracle_extra = (
            f"<br>oracle {_esc(str(oracle_val))}"
            if oracle_val is not None
            else ""
        )
        read_meta_text = f"snapshot at {ts}{oracle_extra}"
    elif failure_msg:
        read_meta_text = _esc(failure_msg)
    model["eyebrow"] = (
        f"Market snapshot · {market_upper}"
        + (f" · {info.protocol}" if info.protocol else "")
    )
    model["title"] = "Live state of"
    model["headline_em"] = market_upper
    model["title_post"] = f"on {info.protocol}."
    model["deck"] = deck_html
    model["run_meta"] = run_meta_lines
    model["extras"] = {
        "market": market_upper,
        "protocol": info.protocol,
        "venue": info.slug,
        "chain_label": chain_label or "current market",
        "protocol_logo_url": protocol_logo_url_for_slug(info.slug),
        "token_glyph": token_glyphs.get(market_upper, market_upper[:1].upper()),
        "token_color": token_color,
        "mark_price": snapshot.get("mark_price") if snapshot else None,
        "delta_pct": None,
        "delta_abs": None,
        "delta_since": "since open" if snapshot else "",
        "live_ok": live_ok,
        "failure_msg": failure_msg,
        "read_meta": read_meta_text,
        "stats": stats_extras,
        "chart_points": list(graph_data.get("chart_points") or []),
        "book": list(graph_data.get("book") or []),
        "book_pill": str(graph_data.get("book_pill") or ("live" if snapshot else "depth unavailable")),
    }
    paths, validation = write_artifacts(
        run_id=run_id,
        request=request,
        inventory=inventory,
        data_snapshot=data_snapshot,
        live_snapshot=live_snapshot,
        model=model,
    )
    publish_result = publish_artifacts_if_requested(args, run_id, paths)
    envelope = base_success_envelope(
        action="market-snapshot",
        kind="market-snapshot",
        run_id=run_id,
        paths=paths,
        validation=validation,
        preview_url=preview_url(paths["root"]) if args.serve else None,
        publish=publish_result,
    )
    envelope.update({"adapter": info.slug, "protocol_name": info.protocol, "market": args.market})
    return envelope


def parse_adapter_list(value: str) -> list[str]:
    adapters = [item.strip() for item in value.split(",") if item.strip()]
    if len(adapters) < 2 or len(adapters) > 4:
        raise PathRuntimeError(
            "missing_required_arg",
            "compare-protocols requires 2-4 protocols.",
            details={"adapters": adapters},
        )
    return adapters


def compare_row_for_adapter(info: AdapterInfo, row: JsonMap) -> JsonMap:
    methods = set(info.support.get("live_reads") or [])
    patterns = set(row.get("method_patterns") or [])
    matched_methods = sorted(
        method
        for method in methods
        if not any(token in method.lower() for token in DENIED_READ_SUBSTRINGS)
        and (not patterns or any(re.fullmatch(pattern.replace("*", ".*"), method) for pattern in patterns))
    )
    preferred_fields = list(row.get("preferred_fields") or [])
    capabilities = [cap for cap in info.capabilities if is_read_capability(cap)]
    return {
        "adapter": info.slug,
        "protocol": info.protocol,
        "row_id": row.get("id"),
        "label": row.get("label"),
        "supported": bool(matched_methods or capabilities),
        "matched_methods": matched_methods,
        "preferred_fields": preferred_fields,
        "read_capabilities": capabilities,
        "verification": (info.support.get("verification") or {}).get("compare-protocols", "unverified"),
    }


async def handle_compare_protocols(args: argparse.Namespace) -> JsonMap:
    use_case = str(args.use_case)
    if use_case == "prediction-markets":
        raise PathRuntimeError(
            "unsupported_use_case",
            "prediction-markets is not supported by compare-protocols in v0.1.",
            details={"use_case": use_case, "supported": sorted(SUPPORTED_COMPARE_USE_CASES)},
        )
    if use_case not in SUPPORTED_COMPARE_USE_CASES:
        raise PathRuntimeError(
            "unsupported_use_case",
            f"Unsupported compare-protocols use case: {use_case}",
            details={"use_case": use_case, "supported": sorted(SUPPORTED_COMPARE_USE_CASES)},
        )
    style = load_style(args.style)
    source_artifact = load_source_artifact(args.source_artifact)
    jobs = load_jobs().get("use_cases") or {}
    job = jobs.get(use_case)
    if not isinstance(job, dict) or not job.get("row_taxonomy"):
        raise PathRuntimeError(
            "job_taxonomy_missing",
            f"Missing row taxonomy for use case: {use_case}",
            details={"use_case": use_case, "file": "data/jobs.yaml"},
        )
    inventory = list_adapter_inventory()
    adapter_slugs = parse_adapter_list(args.adapters)
    infos = [get_adapter_info(slug, inventory) for slug in adapter_slugs]
    rows = []
    for row in list(job.get("row_taxonomy") or []):
        rows.extend(compare_row_for_adapter(info, row) for info in infos)

    live_rows: list[JsonMap] = []
    live_failures: list[JsonMap] = []
    compare_min_tvl = float(load_request_defaults().get("min_tvl_usd") or 0)
    if use_case == "stablecoin-lending" and args.asset:
        live_rows, live_failures = await fetch_stablecoin_rates(
            adapters=infos,
            asset=str(args.asset).upper(),
            chain=args.chain,
            min_tvl=compare_min_tvl,
            action="compare-protocols",
        )
    if source_artifact and isinstance(source_artifact.get("comparison_rows"), list):
        rows.extend(row for row in source_artifact["comparison_rows"] if isinstance(row, dict))

    warnings = []
    unsupported = [row for row in rows if not row.get("supported")]
    if unsupported:
        warnings.append("Some comparison rows are unavailable for the current protocol data.")
    unverified = [
        info.slug
        for info in infos
        if "unverified" in str((info.support.get("verification") or {}).get("compare-protocols", "")).lower()
        or "template-compatible" in str((info.support.get("verification") or {}).get("compare-protocols", "")).lower()
    ]
    if unverified:
        warnings.append(f"{len(unverified)} protocol venues have partial comparison coverage.")
    if live_failures:
        warnings.append("Some live protocol comparison values were unavailable.")

    metrics = [
        {"label": "Use Case", "value": str(job.get("label") or use_case)},
        {"label": "Protocols", "value": str(len(infos))},
        {"label": "Rows", "value": str(len(job.get("row_taxonomy") or []))},
    ]
    row_items = []
    for taxonomy_row in list(job.get("row_taxonomy") or []):
        supported_protocols = [
            row["protocol"]
            for row in rows
            if row.get("row_id") == taxonomy_row.get("id") and row.get("supported")
        ]
        row_items.append(
            f"{taxonomy_row.get('label')}: {', '.join(supported_protocols) if supported_protocols else 'unavailable'}"
        )
    if live_rows:
        ranked = sorted(
            [row for row in live_rows if row.get("net_apy") is not None],
            key=lambda row: float(row.get("net_apy") or 0.0),
            reverse=True,
        )
        live_items = [
            f"{row['protocol']}: net {format_rate(row.get('net_apy'))} on {row.get('asset')}"
            for row in ranked[:4]
        ]
    else:
        live_items = ["No live comparison values were available for this run."]
    sections = [
        {"title": "Comparison Rows", "items": row_items},
        {"title": "Live Values", "items": live_items},
        {
            "title": "Scope",
            "items": [
                "compare-protocols v0.1 supports stablecoin-lending, lp, perps, and restaking.",
                "prediction-markets remains available for how-it-works and market-snapshot only.",
                "Rows represent user-facing protocol questions for the selected use case.",
            ],
        },
    ]
    risks = []
    for info in infos:
        risks.extend(info.risks[:1])
    model = build_model(
        kind="compare-protocols",
        title=f"{job.get('label') or use_case} Comparison",
        subtitle=", ".join(info.protocol for info in infos),
        summary=str(job.get("description") or "Side-by-side protocol comparison using checked-in row taxonomy."),
        metrics=metrics,
        sections=sections,
        risks=risks[:3],
        sources=[
            src
            for info in infos
            for src in [protocol_homepage_for_slug(info.slug)]
            if src
        ],
        warnings=warnings,
        style=style,
    )
    request = {
        "action": "compare-protocols",
        "adapters": adapter_slugs,
        "use_case": use_case,
        "asset": args.asset,
        "chain": args.chain,
        "min_tvl": compare_min_tvl,
        "style": args.style,
        "source_artifact_loaded": bool(source_artifact),
    }
    run_id = make_run_id("compare-protocols", request)
    data_snapshot = {
        "request": request,
        "job_taxonomy": job,
        "comparison_rows": rows,
        "live_rows": live_rows,
    }
    live_snapshot = {"attempted": bool(args.asset and use_case == "stablecoin-lending"), "failures": live_failures, "rows": live_rows}
    chain_label = _chain_slug(chain_id_for(args.chain)) or (args.chain or "")
    asset_text = (str(args.asset).upper() if args.asset else "")
    by_slug_apy: dict[str, float] = {}
    by_slug_row: dict[str, JsonMap] = {}
    for row in live_rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("venue") or "")
        if not slug:
            continue
        apy_val = coerce_float(row.get("net_apy")) or 0.0
        if apy_val > by_slug_apy.get(slug, -float("inf")):
            by_slug_apy[slug] = apy_val
            by_slug_row[slug] = row
    best_slug = max(by_slug_apy, key=by_slug_apy.get) if by_slug_apy else None
    protocols_extras: list[JsonMap] = []
    use_case_label = str(job.get("label") or use_case)
    for info in infos:
        chain_meta = chain_label or ""
        meta_parts = []
        if chain_meta:
            meta_parts.append(chain_meta)
        is_best = info.slug == best_slug
        best_reason = ""
        if is_best and best_slug is not None:
            best_reason = f"highest live {asset_text or 'apy'}".strip()
        protocols_extras.append(
            {
                "slug": info.slug,
                "name": info.protocol,
                "meta": " · ".join(meta_parts) or use_case_label,
                "is_best": is_best,
                "best_reason": best_reason or "best fit",
            }
        )
    matrix_rows_extras: list[JsonMap] = []
    taxonomy = list(job.get("row_taxonomy") or [])
    for index, taxonomy_row in enumerate(taxonomy[:6]):
        row_id = str(taxonomy_row.get("id") or "")
        row_label = str(taxonomy_row.get("label") or row_id)
        cells: list[JsonMap] = []
        for info in infos:
            live_row = by_slug_row.get(info.slug)
            cell: JsonMap = {}
            if row_id in {"supply_rate", "net_rate"} and live_row is not None:
                cell["value"] = _format_percent(live_row.get("net_apy"))
                cell["tone"] = "brand" if info.slug == best_slug else "up"
                gross = live_row.get("gross_apy")
                reward = live_row.get("reward_apy")
                note_parts = []
                if gross is not None:
                    note_parts.append(
                        f"gross <span class=\"mono\">{_format_percent(gross)}</span>"
                    )
                if reward is not None and (coerce_float(reward) or 0) > 0:
                    note_parts.append(
                        f"reward <span class=\"mono\">{_format_percent(reward)}</span>"
                    )
                cell["note"] = ", ".join(note_parts) if note_parts else "variable, utilization-driven"
            elif row_id == "rewards":
                if live_row is not None and (coerce_float(live_row.get("reward_apy")) or 0) > 0:
                    cell["value"] = _format_percent(live_row.get("reward_apy"))
                    cell["tone"] = "up"
                    tokens = live_row.get("reward_tokens") or []
                    cell["tags"] = [
                        {"text": shorten_addresses_in_text(t), "tone": "warn"} for t in tokens[:3]
                    ]
                    cell["note"] = "reward APY layered on top of base supply"
                else:
                    cell["value"] = "None reported"
                    cell["note"] = "no reward APY exposed by protocol"
            elif row_id == "liquidity":
                if live_row is not None:
                    cell["value"] = _format_usd_compact(live_row.get("tvl_usd"))
                    cell["note"] = f"TVL · <span class=\"mono\">{_esc(asset_text or 'asset')}</span>"
                else:
                    cell["value"] = None
                    cell["note"] = "current market did not return TVL"
            elif row_id == "constraints":
                chain_id_val = live_row.get("chain_id") if live_row else None
                ch_label = _chain_slug(int(chain_id_val)) if chain_id_val else chain_label
                cell["value"] = ch_label.title() if ch_label else "—"
                market_id = live_row.get("market_id") if live_row else None
                cell["note"] = (
                    f"market · <span class=\"mono\">{_esc(shorten_addresses_in_text(market_id or '—'))}</span>"
                )
            else:
                comparison_row = next(
                    (
                        r
                        for r in rows
                        if r.get("adapter") == info.slug
                        and r.get("row_id") == row_id
                    ),
                    None,
                )
                if comparison_row and comparison_row.get("supported"):
                    cell["value"] = "Supported"
                    cell["note"] = "current protocol data available"
                else:
                    cell["value"] = None
                    cell["note"] = "not available for this snapshot"
            cells.append(cell)
        # Risk meter row when no other data — synthesise from risks.yaml count
        if row_id == "constraints" and not any(c.get("value") for c in cells):
            cells = []
            for info in infos:
                level = "med"
                if len(info.risks) <= 2:
                    level = "low"
                elif len(info.risks) >= 5:
                    level = "high"
                fill = max(1, min(5, len(info.risks)))
                cells.append(
                    {
                        "risk_meter": {
                            "level": level,
                            "fill": fill,
                            "label": "Risk surface",
                        },
                        "note": f"{len(info.risks)} protocol risk notes",
                    }
                )
        matrix_rows_extras.append(
            {
                "label": row_label,
                "sub": f"Q{index + 1} · {row_id.replace('_', ' ')}",
                "cells": cells,
            }
        )
    question_label_map = {
        "stablecoin-lending": "For a stable supplier.",
        "lp": "For a liquidity provider.",
        "perps": "For a perps trader.",
        "restaking": "For a restaker.",
    }
    question_label = question_label_map.get(use_case, "For this use case.")
    if use_case == "stablecoin-lending" and asset_text:
        question_label = f"For a {asset_text} supplier" + (
            f" on {chain_label.title()}." if chain_label else "."
        )
    legend_text = f"{len(by_slug_apy)} current protocol reads · point-in-time"
    run_meta_lines = [
        (
            "USE CASE",
            " · ".join(
                p
                for p in [use_case, asset_text, chain_label]
                if p
            ),
        ),
        ("PROTOCOLS", ", ".join(info.protocol for info in infos)),
        ("UPDATED", iso_now()[:10]),
    ]
    n_protocols = len(infos)
    n_rows = len(taxonomy[:6])
    deck_html = (
        f"A side-by-side answer for the <span class=\"wf-mono\">{_esc(use_case)}</span> job. "
        "Values come from current protocol market data when available. "
        "Risk surfaces summarize protocol-level considerations."
    )
    model["eyebrow"] = (
        f"Compare · {use_case_label}"
        + (f" · {asset_text}" if asset_text else "")
        + (f" · {chain_label}" if chain_label else "")
    )
    model["title"] = f"{n_protocols} protocols."
    model["headline_em"] = "One use case."
    model["title_post"] = f"{n_rows} questions."
    model["deck"] = deck_html
    model["run_meta"] = run_meta_lines
    model["extras"] = {
        "protocols": protocols_extras,
        "matrix_rows": matrix_rows_extras,
        "question_label": question_label,
        "question_sub": use_case_label,
        "legend_text": legend_text,
    }
    paths, validation = write_artifacts(
        run_id=run_id,
        request=request,
        inventory=inventory,
        data_snapshot=data_snapshot,
        live_snapshot=live_snapshot,
        model=model,
    )
    publish_result = publish_artifacts_if_requested(args, run_id, paths)
    envelope = base_success_envelope(
        action="compare-protocols",
        kind="compare-protocols",
        run_id=run_id,
        paths=paths,
        validation=validation,
        preview_url=preview_url(paths["root"]) if args.serve else None,
        publish=publish_result,
    )
    envelope.update(
        {
            "adapters": adapter_slugs,
            "venues": adapter_slugs,
            "use_case": use_case,
            "asset": args.asset,
            "apy_normalization_version": APY_NORMALIZATION_VERSION
            if use_case == "stablecoin-lending"
            else None,
        }
    )
    return envelope


def find_run_dir(run_id: str) -> Path:
    run_dir = artifacts_root() / run_id
    if not run_dir.exists():
        raise PathRuntimeError(
            "missing_required_arg",
            f"Run artifacts not found: {run_id}",
            details={"run_id": run_id, "artifacts_root": str(artifacts_root())},
        )
    html_path = run_dir / "infographic.html"
    if not html_path.exists():
        raise PathRuntimeError(
            "missing_required_arg",
            f"Run is missing infographic.html: {run_id}",
            details={"run_id": run_id, "run_dir": str(run_dir)},
        )
    return run_dir


async def handle_preview(args: argparse.Namespace) -> JsonMap:
    run_dir = find_run_dir(args.run_id)
    validation_path = run_dir / "validation.json"
    validation = (
        json.loads(validation_path.read_text(encoding="utf-8"))
        if validation_path.exists()
        else {"ok": True, "warnings": ["validation.json was not found."]}
    )
    return {
        "ok": True,
        "action": "preview",
        "kind": "preview",
        "run_id": args.run_id,
        "artifacts_dir": path_string(run_dir),
        "primary_artifact": path_string(run_dir / "infographic.html"),
        "files": {
            "html": path_string(run_dir / "infographic.html"),
            "svg": path_string(run_dir / "infographic.svg"),
            "validation": path_string(validation_path),
        },
        "preview_url": preview_url(run_dir) if args.serve else None,
        "validation": validation,
    }


def preview_url(run_dir: Path, *, host: str = "127.0.0.1", port: int = 8765) -> str:
    _ = run_dir
    return f"http://{host}:{port}/infographic.html"


def serve_directory(run_dir: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(run_dir))
    with socketserver.TCPServer((host, port), handler) as httpd:
        httpd.serve_forever()


def add_publish_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--publish-required", action="store_true")
    parser.add_argument("--publish-provider", default="cloudflare-pages")
    parser.add_argument("--publish-branch")
    parser.add_argument("--publish-data", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    defaults = load_request_defaults()
    parser = JsonArgumentParser(prog="infographic-composer")
    subparsers = parser.add_subparsers(dest="action", required=True)

    how = subparsers.add_parser("how-it-works")
    how.add_argument("--adapter", required=True)
    how.add_argument("--docs-mode", choices=["off", "metadata", "fetch"], default="fetch")
    how.add_argument("--metrics-mode", choices=["off", "live"], default="live")
    how.add_argument("--chain")
    how.add_argument("--source-artifact")
    how.add_argument("--style", default=defaults.get("default_style") or "default")
    how.add_argument("--serve", action="store_true")
    add_publish_args(how)

    rates = subparsers.add_parser("stablecoin-rates")
    rates.add_argument("--asset", required=True)
    rates.add_argument("--chain")
    rates.add_argument("--venue")
    rates.add_argument("--min-tvl", type=float, default=float(defaults.get("min_tvl_usd") or 0))
    rates.add_argument("--source-artifact")
    rates.add_argument("--style", default=defaults.get("default_style") or "default")
    rates.add_argument("--serve", action="store_true")
    add_publish_args(rates)

    market = subparsers.add_parser("market-snapshot")
    market.add_argument("--adapter", required=True)
    market.add_argument("--market", required=True)
    market.add_argument("--chain")
    market.add_argument("--source-artifact")
    market.add_argument("--style", default=defaults.get("default_style") or "default")
    market.add_argument("--serve", action="store_true")
    add_publish_args(market)

    compare = subparsers.add_parser("compare-protocols")
    compare.add_argument("--adapters", required=True)
    compare.add_argument("--use-case", required=True)
    compare.add_argument("--asset")
    compare.add_argument("--chain")
    compare.add_argument("--source-artifact")
    compare.add_argument("--style", default=defaults.get("default_style") or "default")
    compare.add_argument("--serve", action="store_true")
    add_publish_args(compare)

    preview = subparsers.add_parser("preview")
    preview.add_argument("--run-id", required=True)
    preview.add_argument("--serve", action="store_true")
    return parser


async def dispatch(args: argparse.Namespace) -> JsonMap:
    if args.action == "how-it-works":
        return await handle_how_it_works(args)
    if args.action == "stablecoin-rates":
        return await handle_stablecoin_rates(args)
    if args.action == "market-snapshot":
        return await handle_market_snapshot(args)
    if args.action == "compare-protocols":
        return await handle_compare_protocols(args)
    if args.action == "preview":
        return await handle_preview(args)
    raise PathRuntimeError(
        "unsupported_use_case",
        f"Unsupported action: {args.action}",
        details={"action": args.action},
    )


def failure_envelope(error: PathRuntimeError) -> JsonMap:
    return {
        "ok": False,
        "action": None,
        "kind": "error",
        "run_id": None,
        "artifacts_dir": None,
        "primary_artifact": None,
        "files": {},
        "preview_url": None,
        "errors": [
            {
                "code": error.code,
                "message": error.message,
                "details": error.details,
            }
        ],
    }


def internal_failure(exc: Exception) -> JsonMap:
    return failure_envelope(
        PathRuntimeError(
            "internal_error",
            str(exc),
            details={"type": exc.__class__.__name__},
        )
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        result = asyncio.run(dispatch(args))
        print(json.dumps(result, sort_keys=True), flush=True)
        if bool(getattr(args, "serve", False)):
            run_dir = Path(str(result["artifacts_dir"]))
            serve_directory(run_dir)
        return 0
    except PathRuntimeError as exc:
        print(json.dumps(failure_envelope(exc), sort_keys=True), flush=True)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps(internal_failure(exc), sort_keys=True), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
