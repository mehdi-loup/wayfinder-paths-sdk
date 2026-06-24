#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

DOMAIN_SCAN_AGENTS = {
    "geopolitical-analyst",
    "macro-strategist",
    "regulatory-tracker",
    "tech-scout",
    "structural-analyst",
}
HTTPS_RE = re.compile(r"^https?://", re.IGNORECASE)

def _fail(msg: str) -> None:
    raise SystemExit(f'verification_protocol failure: {msg}')

def _validate_domain_scan(payload: dict, *, recency_days: int = 30, min_tool_calls: int = 3) -> None:
    theses = payload.get('candidate_theses')
    if not isinstance(theses, list) or not theses:
        _fail('candidate_theses must be a non-empty array')
    today = date.today()
    recency_cutoff = today - timedelta(days=recency_days)
    for idx, thesis in enumerate(theses):
        if not isinstance(thesis, dict):
            _fail(f'thesis[{idx}] must be an object')
        tid = thesis.get('thesis_id') or f'index_{idx}'
        evidence = thesis.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            _fail(f'{tid}: evidence must be a non-empty array')
        recent_hits = 0
        for ev in evidence:
            url = (ev or {}).get('source_url')
            if not isinstance(url, str) or not HTTPS_RE.match(url):
                _fail(f'{tid}: every evidence entry must carry an https source_url')
            d = (ev or {}).get('date')
            if isinstance(d, str):
                try:
                    if date.fromisoformat(d) >= recency_cutoff:
                        recent_hits += 1
                except ValueError:
                    pass
        if recent_hits < 1:
            _fail(f'{tid}: at least one evidence entry must be within the last {recency_days} days')
        cc = thesis.get('currency_check')
        if not isinstance(cc, dict):
            _fail(f'{tid}: currency_check object is required')
        if cc.get('already_happened') is True:
            _fail(f'{tid}: currency_check.already_happened is true — drop the thesis before writing')
        cc_url = cc.get('evidence_url')
        if not isinstance(cc_url, str) or not HTTPS_RE.match(cc_url):
            _fail(f'{tid}: currency_check.evidence_url must be an https URL')
        queries = thesis.get('verification_queries')
        if not isinstance(queries, list) or len(queries) < min_tool_calls:
            _fail(f'{tid}: verification_queries must list at least {min_tool_calls} tool calls')
        for q in queries:
            q_url = (q or {}).get('url')
            if not isinstance(q_url, str) or not HTTPS_RE.match(q_url):
                _fail(f'{tid}: each verification_queries entry needs an https url')
        for cat in thesis.get('catalysts') or []:
            est = (cat or {}).get('estimated_date')
            if isinstance(est, str):
                try:
                    if date.fromisoformat(est) <= today:
                        _fail(f'{tid}: catalyst estimated_date {est} is not in the future')
                except ValueError:
                    pass
        exec_ = thesis.get('executability')
        if not isinstance(exec_, dict):
            _fail(f'{tid}: executability object is required')
        tier = exec_.get('tier')
        if tier not in ('A', 'B'):
            _fail(f'{tid}: executability.tier must be A or B (got {tier!r}); reject-tier theses must be dropped upstream')
        leg = exec_.get('primary_leg')
        if not isinstance(leg, dict):
            _fail(f'{tid}: executability.primary_leg object is required')
        valid_surfaces = {'swap','perp','lending','vault','lp','pendle','contract','polymarket','ccxt'}
        if leg.get('surface') not in valid_surfaces:
            _fail(f'{tid}: primary_leg.surface must be one of {sorted(valid_surfaces)}')
        for key in ('instrument', 'venue', 'liquidity_check'):
            if not isinstance(leg.get(key), str) or not leg[key].strip():
                _fail(f'{tid}: primary_leg.{key} must be a non-empty string')
        if tier == 'B' and not isinstance(exec_.get('proxy_basis'), str):
            _fail(f'{tid}: Tier B thesis must declare proxy_basis string')

def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit('usage: validate_artifact.py <agent-id> <path>')
    agent_id, path_value = sys.argv[1], sys.argv[2]
    artifact_path = Path(path_value)
    if not artifact_path.exists():
        raise SystemExit(f'missing artifact for {agent_id}: {artifact_path}')
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit('artifact payload must be a JSON object')
    if agent_id in DOMAIN_SCAN_AGENTS:
        _validate_domain_scan(payload)
    print(json.dumps({'ok': True, 'agent_id': agent_id, 'path': str(artifact_path)}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
