#!/usr/bin/env python3
"""Structural checks for pipeline artifacts — use instead of throwaway `python -c`.

Two subcommands, both reading `.wf-artifacts/<run_id>/<file>`:

  verify <run_id> <file> [--keys K ...] [--array ARR] [--fields F ...]
      Confirm <file> is valid JSON, has the required top-level keys, and that
      every item in array ARR has the required fields.

  cross-check <run_id> <file_a> <a_array> <a_id> <file_b> <b_array> <b_id> [--filter field=value]
      Confirm id sets line up between two artifacts (e.g. KEEP papers in
      discovery vs papers carried into methodology). Reports missing/extra ids.

Prints a JSON result and exits non-zero when a check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ARTIFACTS_DIR = ".wf-artifacts"


def _load(run_id: str, file: str) -> dict | list:
    path = Path(ARTIFACTS_DIR) / run_id / file
    if not path.exists():
        _fail({"ok": False, "error": "artifact_missing", "path": str(path)})
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail({"ok": False, "error": "invalid_json", "path": str(path), "detail": str(exc)})


def _pick_array(data, key: str | None) -> list | None:
    arr = data.get(key) if key and isinstance(data, dict) else data
    return arr if isinstance(arr, list) else None


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2))
    sys.exit(0 if payload.get("ok") else 1)


def _fail(payload: dict) -> None:
    print(json.dumps(payload, indent=2))
    sys.exit(1)


def cmd_verify(args: argparse.Namespace) -> None:
    data = _load(args.run_id, args.file)
    missing_keys = [k for k in (args.keys or []) if not (isinstance(data, dict) and k in data)]
    item_issues: list[dict] = []
    if args.array or args.fields:
        arr = _pick_array(data, args.array)
        if arr is None:
            _fail({"ok": False, "error": "not_an_array", "array": args.array})
        for i, item in enumerate(arr):
            miss = [f for f in (args.fields or []) if not (isinstance(item, dict) and f in item)]
            if miss:
                ident = (item or {}).get("id") or (item or {}).get("paper_id")
                item_issues.append({"index": i, "id": ident, "missing": miss})
        _emit({"ok": not missing_keys and not item_issues, "count": len(arr),
               "missing_keys": missing_keys, "item_issues": item_issues})
    _emit({"ok": not missing_keys, "missing_keys": missing_keys})


def cmd_cross_check(args: argparse.Namespace) -> None:
    a = _load(args.run_id, args.file_a)
    b = _load(args.run_id, args.file_b)
    arr_a, arr_b = _pick_array(a, args.a_array), _pick_array(b, args.b_array)
    if arr_a is None or arr_b is None:
        _fail({"ok": False, "error": "not_an_array"})
    items_a = arr_a
    if args.filter:
        field, _, value = args.filter.partition("=")
        items_a = [x for x in arr_a if str((x or {}).get(field)) == value]
    ids_a = [x[args.a_id] for x in items_a if isinstance(x, dict) and x.get(args.a_id) is not None]
    ids_b = [x[args.b_id] for x in arr_b if isinstance(x, dict) and x.get(args.b_id) is not None]
    set_a, set_b = {str(v) for v in ids_a}, {str(v) for v in ids_b}
    missing_in_b = [v for v in ids_a if str(v) not in set_b]
    extra_in_b = [v for v in ids_b if str(v) not in set_a]
    _emit({"ok": not missing_in_b and not extra_in_b, "a_count": len(ids_a),
           "b_count": len(ids_b), "missing_in_b": missing_in_b, "extra_in_b": extra_in_b})


def main() -> None:
    p = argparse.ArgumentParser(description="Verify Wayfinder pipeline artifacts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify")
    v.add_argument("run_id")
    v.add_argument("file")
    v.add_argument("--keys", nargs="*")
    v.add_argument("--array")
    v.add_argument("--fields", nargs="*")
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("cross-check")
    c.add_argument("run_id")
    c.add_argument("file_a")
    c.add_argument("a_array")
    c.add_argument("a_id")
    c.add_argument("file_b")
    c.add_argument("b_array")
    c.add_argument("b_id")
    c.add_argument("--filter", help="field=value filter on file_a items (e.g. verdict=KEEP)")
    c.set_defaults(func=cmd_cross_check)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
