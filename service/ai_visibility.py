"""
ai_visibility.py — MEASURED AI visibility, not inferred.

Every audit already generates 4 test queries (context.test_queries) and
crawls 5 competitors — then never uses them. This module closes the loop:
it executes those queries against real answer engines, records who gets
cited and mentioned, computes per-engine inclusion rates and share of
voice vs the competitors, and logs every raw answer permanently in
public.ai_answer_runs (the longitudinal dataset).

Honesty about stochasticity: engines are non-deterministic, so each query
runs K times (AI_VIS_RUNS, default 2) and results are reported as rates
over runs — never as a single-sample binary.

Engines v1 (enabled per available key):
  - openai     — Responses API + web_search tool   (OPENAI_API_KEY)
  - anthropic  — Messages API + web_search server tool (ANTHROPIC_API_KEY)
Adapter registry leaves slots for perplexity / gemini / google_aio when
keys are configured (PERPLEXITY_API_KEY, GEMINI_API_KEY, SERP_API_KEY).

Never raises; stats land in metadata.ai_visibility and the compact result
set in audit['measured_visibility'] (mirrored into metadata for DB
persistence). ADDITIVE: with no keys or AI_VISIBILITY=0 the audit is
untouched.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger('audit.visibility')

ENABLED = os.getenv('AI_VISIBILITY', '1') not in ('0', 'false', 'False')
RUNS = max(1, min(5, int(os.getenv('AI_VIS_RUNS', '2') or 2)))
CALL_TIMEOUT = int(os.getenv('AI_VIS_TIMEOUT', '60') or 60)
MAX_QUERIES = 4
_ANSWER_CAP = 20000

_CORP_STOP = {'inc', 'llc', 'ltd', 'limited', 'private', 'technologies',
              'technology', 'company', 'corp', 'corporation', 'operated',
              'the', 'and', 'group', 'gmbh', 'pvt'}


# ---------------------------------------------------------------------------
# Engine adapters — each returns {'answer': str, 'cited_urls': [str]}
# ---------------------------------------------------------------------------

def _engine_openai(query: str) -> Dict[str, Any]:
    from openai import OpenAI
    client = OpenAI(timeout=CALL_TIMEOUT)
    model = os.getenv('AI_VIS_OPENAI_MODEL', 'gpt-4o-mini')
    if hasattr(client, 'responses'):
        last_err = None
        for tool_type in ('web_search', 'web_search_preview'):
            try:
                r = client.responses.create(
                    model=model,
                    tools=[{'type': tool_type}],
                    input=query,
                )
                urls: List[str] = []
                for item in (getattr(r, 'output', None) or []):
                    for block in (getattr(item, 'content', None) or []):
                        for ann in (getattr(block, 'annotations', None) or []):
                            u = getattr(ann, 'url', None)
                            if u:
                                urls.append(u)
                _u = getattr(r, 'usage', None)
                return {'answer': getattr(r, 'output_text', '') or '',
                        'cited_urls': urls,
                        'usage': {'input_tokens': getattr(_u, 'input_tokens', 0) or 0,
                                  'output_tokens': getattr(_u, 'output_tokens', 0) or 0}}
            except Exception as e:  # noqa: BLE001 — try the older tool name once
                last_err = e
        raise last_err
    # Pre-Responses-API SDK (openai 1.x): use the dedicated search model on
    # chat.completions, which returns url_citation annotations on the message.
    r = client.chat.completions.create(
        model=os.getenv('AI_VIS_OPENAI_SEARCH_MODEL', 'gpt-4o-mini-search-preview'),
        web_search_options={},
        messages=[{'role': 'user', 'content': query}],
    )
    msg = r.choices[0].message
    urls = []
    for ann in (getattr(msg, 'annotations', None) or []):
        uc = getattr(ann, 'url_citation', None)
        u = getattr(uc, 'url', None) if uc else None
        if u:
            urls.append(u)
    _u = getattr(r, 'usage', None)
    return {'answer': msg.content or '', 'cited_urls': urls,
            'usage': {'input_tokens': getattr(_u, 'prompt_tokens', 0) or 0,
                      'output_tokens': getattr(_u, 'completion_tokens', 0) or 0}}


def _engine_anthropic(query: str) -> Dict[str, Any]:
    import anthropic
    client = anthropic.Anthropic(timeout=CALL_TIMEOUT)
    model = os.getenv('AI_VIS_ANTHROPIC_MODEL', 'claude-sonnet-4-6')
    r = client.messages.create(
        model=model,
        max_tokens=1024,
        tools=[{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 3}],
        messages=[{'role': 'user', 'content': query}],
    )
    answer_parts: List[str] = []
    urls: List[str] = []
    for block in (r.content or []):
        btype = getattr(block, 'type', '')
        if btype == 'text':
            answer_parts.append(getattr(block, 'text', '') or '')
            for cit in (getattr(block, 'citations', None) or []):
                u = getattr(cit, 'url', None)
                if u:
                    urls.append(u)
        elif btype == 'web_search_tool_result':
            for item in (getattr(block, 'content', None) or []):
                u = getattr(item, 'url', None)
                if u:
                    urls.append(u)
    _u = getattr(r, 'usage', None)
    _stu = getattr(_u, 'server_tool_use', None) if _u else None
    return {'answer': ' '.join(answer_parts), 'cited_urls': urls,
            'usage': {'input_tokens': getattr(_u, 'input_tokens', 0) or 0,
                      'output_tokens': getattr(_u, 'output_tokens', 0) or 0,
                      'web_searches': (getattr(_stu, 'web_search_requests', 0) or 0)
                      if _stu else 0}}


def _available_engines() -> Dict[str, Callable[[str], Dict[str, Any]]]:
    engines: Dict[str, Callable[[str], Dict[str, Any]]] = {}
    if os.getenv('OPENAI_API_KEY'):
        engines['openai'] = _engine_openai
    if os.getenv('ANTHROPIC_API_KEY'):
        engines['anthropic'] = _engine_anthropic
    # Future slots — wire an adapter and it joins the sweep:
    #   PERPLEXITY_API_KEY -> perplexity (sonar returns citations natively)
    #   GEMINI_API_KEY     -> gemini grounding
    #   SERP_API_KEY       -> google_aio via SERP provider
    return engines


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _norm_domain(url_or_domain: str) -> str:
    s = (url_or_domain or '').strip().lower()
    if '://' in s:
        s = urlparse(s).netloc
    return re.sub(r'^www\.', '', s.split('/')[0])


def _domain_root(domain: str) -> str:
    parts = _norm_domain(domain).split('.')
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else '')


def _brand_tokens(company_name: Optional[str]) -> List[str]:
    toks = re.findall(r'[a-z0-9]{4,}', (company_name or '').lower())
    return [t for t in toks if t not in _CORP_STOP][:6]


def _domain_cited(domain: str, cited_urls: List[str]) -> bool:
    d = _norm_domain(domain)
    return any(_norm_domain(u) == d or _norm_domain(u).endswith('.' + d)
               for u in cited_urls)


def _mentioned(domain: str, brand_toks: List[str], answer: str) -> bool:
    text = (answer or '').lower()
    if _norm_domain(domain) in text or _domain_root(domain) in text:
        return True
    return any(t in text for t in brand_toks)


# ---------------------------------------------------------------------------
# Permanent answer log — the longitudinal dataset. Best-effort.
# ---------------------------------------------------------------------------

_RUNS_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS public.ai_answer_runs (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        audit_id text,
        engine text NOT NULL,
        query_label text NOT NULL,
        query text NOT NULL,
        run_idx int NOT NULL,
        answer_text text,
        cited_urls jsonb,
        target_domain text,
        target_cited boolean,
        target_mentioned boolean,
        created_at timestamptz NOT NULL DEFAULT now()
    )
"""


def _log_runs(rows: List[tuple]) -> bool:
    if not rows:
        return True
    db_url = os.getenv('DATABASE_URL') or os.getenv('SIEVE_DB_URL')
    if not db_url:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=10)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(_RUNS_TABLE_DDL)
                cur.executemany(
                    'INSERT INTO public.ai_answer_runs (audit_id, engine, query_label,'
                    ' query, run_idx, answer_text, cited_urls, target_domain,'
                    ' target_cited, target_mentioned)'
                    ' VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', rows)
        finally:
            conn.close()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning('ai_answer_runs logging failed: %s', e)
        return False


# ---------------------------------------------------------------------------
# Usage / cost accounting (shadow-only — never gates anything)
# ---------------------------------------------------------------------------

# Sweep list prices (USD per 1M tokens / per search), env-overridable.
_VIS_ANTH_IN = float(os.getenv('PRICE_INPUT_PER_MTOK', '3.0'))
_VIS_ANTH_OUT = float(os.getenv('PRICE_OUTPUT_PER_MTOK', '15.0'))
_VIS_SEARCH = float(os.getenv('PRICE_PER_WEB_SEARCH', '0.01'))
_VIS_OAI_IN = float(os.getenv('AI_VIS_OPENAI_PRICE_IN', '0.15'))
_VIS_OAI_OUT = float(os.getenv('AI_VIS_OPENAI_PRICE_OUT', '0.60'))


def _usage_summary(usage_totals: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    """Aggregate per-engine token/search totals into a stats block with an
    estimated dollar cost, so the sweep's spend is visible in metadata and
    METRIC lines (it was previously invisible to all accounting)."""
    cost = 0.0
    for eng, tot in usage_totals.items():
        i, o = tot.get('input_tokens', 0), tot.get('output_tokens', 0)
        if eng == 'anthropic':
            cost += i / 1e6 * _VIS_ANTH_IN + o / 1e6 * _VIS_ANTH_OUT
            cost += tot.get('web_searches', 0) * _VIS_SEARCH
        elif eng == 'openai':
            cost += i / 1e6 * _VIS_OAI_IN + o / 1e6 * _VIS_OAI_OUT
    return {'per_engine': usage_totals, 'est_cost_usd': round(cost, 4)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def measure_visibility(audit: Dict[str, Any],
                       engines: Optional[Dict[str, Callable]] = None,
                       runs: Optional[int] = None,
                       ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Execute the audit's test queries against real answer engines.
    Mutates and returns (audit, stats). Never raises."""
    stats: Dict[str, Any] = {'applied': False}
    try:
        if not ENABLED:
            stats['reason'] = 'AI_VISIBILITY=0'
            return audit, stats
        engines = engines if engines is not None else _available_engines()
        if not engines:
            stats['reason'] = 'no engine keys configured'
            return audit, stats

        tq = ((audit.get('context') or {}).get('test_queries')) or {}
        queries = [(k, v) for k, v in tq.items()
                   if isinstance(v, str) and v.strip()][:MAX_QUERIES]
        if not queries:
            stats['reason'] = 'no test_queries in audit'
            return audit, stats

        target = audit.get('domain') or _norm_domain(audit.get('url', ''))
        brand_toks = _brand_tokens(
            (audit.get('classification') or {}).get('company_name'))
        competitors = [_norm_domain(c.get('domain'))
                       for c in (audit.get('competitor_comparison') or [])
                       if isinstance(c, dict) and c.get('domain')
                       and _norm_domain(c.get('domain')) != _norm_domain(target)][:5]
        k = runs if runs is not None else RUNS

        # Fan out: engines x queries x k runs, bounded concurrency.
        jobs = [(eng, label, q, i)
                for eng in engines for (label, q) in queries for i in range(k)]
        raw: List[Dict[str, Any]] = []
        errors = 0
        usage_totals: Dict[str, Dict[str, int]] = {}
        with ThreadPoolExecutor(max_workers=min(6, len(jobs) or 1)) as pool:
            futs = {pool.submit(engines[eng], q): (eng, label, q, i)
                    for (eng, label, q, i) in jobs}
            for fut in as_completed(futs):
                eng, label, q, i = futs[fut]
                try:
                    r = fut.result()
                    raw.append({'engine': eng, 'label': label, 'query': q,
                                'run': i, 'answer': (r.get('answer') or '')[:_ANSWER_CAP],
                                'cited_urls': [u for u in (r.get('cited_urls') or [])
                                               if isinstance(u, str)][:40]})
                    for key, val in (r.get('usage') or {}).items():
                        if isinstance(val, int):
                            tot = usage_totals.setdefault(eng, {})
                            tot[key] = tot.get(key, 0) + val
                except Exception as e:  # noqa: BLE001 — one bad call must not stop the sweep
                    errors += 1
                    log.warning('%s query "%s" run %d failed: %s', eng, label, i, e)

        if not raw:
            stats.update({'reason': 'all engine calls failed', 'errors': errors})
            return audit, stats

        # Aggregate.
        per_engine: Dict[str, Any] = {}
        sov_counts: Dict[str, int] = {}
        total_runs = 0
        for r in raw:
            eng = per_engine.setdefault(r['engine'], {})
            cell = eng.setdefault(r['label'], {
                'query': r['query'], 'runs': 0, 'target_cited': 0,
                'target_mentioned': 0, 'competitors_cited': {c: 0 for c in competitors}})
            cell['runs'] += 1
            total_runs += 1
            cited = _domain_cited(target, r['cited_urls'])
            mentioned = _mentioned(target, brand_toks, r['answer'])
            r['target_cited'], r['target_mentioned'] = cited, mentioned
            if cited:
                cell['target_cited'] += 1
            if mentioned:
                cell['target_mentioned'] += 1
            seen_domains = {_norm_domain(u) for u in r['cited_urls']}
            for d in seen_domains:
                if d:
                    sov_counts[d] = sov_counts.get(d, 0) + 1
            for c in competitors:
                if _domain_cited(c, r['cited_urls']):
                    cell['competitors_cited'][c] += 1

        inclusion = {}
        for eng, cells in per_engine.items():
            n = sum(c['runs'] for c in cells.values())
            cited_runs = sum(c['target_cited'] for c in cells.values())
            mention_runs = sum(c['target_mentioned'] for c in cells.values())
            inclusion[eng] = {
                'runs': n,
                'cited_rate': round(cited_runs / n, 2) if n else 0,
                'mentioned_rate': round(mention_runs / n, 2) if n else 0,
            }

        watched = [_norm_domain(target)] + competitors
        sov_table = [{'domain': d,
                      'citations': sov_counts.get(d, 0),
                      'sov': round(sov_counts.get(d, 0) / total_runs, 2) if total_runs else 0,
                      'is_target': d == _norm_domain(target)}
                     for d in watched]
        top_cited = sorted(((d, n) for d, n in sov_counts.items()),
                           key=lambda x: (-x[1], x[0]))[:10]

        mv = {
            'measured': True,
            'engines': sorted(per_engine.keys()),
            'runs_per_query': k,
            'total_runs': total_runs,
            'inclusion': inclusion,
            'share_of_voice': sov_table,
            'top_cited_domains': [{'domain': d, 'citations': n} for d, n in top_cited],
            'per_engine': per_engine,
            'measured_at': audit.get('date'),
        }
        audit['measured_visibility'] = mv
        # metadata jsonb is what persists to the DB row — mirror the result.
        audit.setdefault('metadata', {})['measured_visibility'] = mv

        logged = _log_runs([
            (str(audit.get('audit_id') or ''), r['engine'], r['label'], r['query'],
             r['run'], r['answer'], json.dumps(r['cited_urls']),
             _norm_domain(target), r.get('target_cited'), r.get('target_mentioned'))
            for r in raw])

        stats.update({'applied': True, 'engines': sorted(per_engine.keys()),
                      'queries': len(queries), 'runs_total': total_runs,
                      'errors': errors, 'answers_logged': logged,
                      'target_inclusion': inclusion,
                      'usage': _usage_summary(usage_totals)})
    except Exception as e:  # noqa: BLE001
        log.error('ai visibility measurement failed: %s', e)
        stats = {'applied': False, 'error': f'{type(e).__name__}: {e}'}
    return audit, stats


# ---------------------------------------------------------------------------
# Self-test (stdlib only, stubbed engines — no network/DB)
# ---------------------------------------------------------------------------

def _selftest() -> None:
    calls = {'n': 0}

    def stub_engine(query):
        calls['n'] += 1
        if 'variant' in query:
            raise RuntimeError('engine hiccup')          # per-call fault tolerance
        return {'answer': 'Example Domain is an IANA reserved placeholder. '
                          'See also iana.org for details.',
                'cited_urls': ['https://www.iana.org/domains',
                               'https://example.com/', 'https://w3.org/spec']}

    audit = {
        'audit_id': 'selftest', 'domain': 'example.com', 'url': 'https://example.com',
        'date': '2026-07-04',
        'classification': {'company_name': 'IANA / ICANN (Internet Assigned Numbers Authority)'},
        'context': {'test_queries': {
            'primary': 'what is example.com used for',
            'variant': 'variant query that will fail',
            'category': 'IANA reserved documentation domains',
        }},
        'competitor_comparison': [{'domain': 'iana.org'}, {'domain': 'w3.org'},
                                  {'domain': 'example.com'}],  # self is excluded
    }
    audit, stats = measure_visibility(
        audit, engines={'stub': stub_engine}, runs=2)

    assert stats['applied'] is True, stats
    assert stats['errors'] == 2, stats                      # 2 runs of the failing query
    mv = audit['measured_visibility']
    assert mv['total_runs'] == 4, mv['total_runs']          # 2 queries x 2 runs succeeded
    inc = mv['inclusion']['stub']
    assert inc['cited_rate'] == 1.0 and inc['mentioned_rate'] == 1.0, inc
    sov = {r['domain']: r for r in mv['share_of_voice']}
    assert sov['example.com']['is_target'] and sov['example.com']['citations'] == 4, sov
    assert sov['iana.org']['citations'] == 4 and not sov['iana.org']['is_target'], sov
    assert 'example.com' not in [c for c in
                                 mv['per_engine']['stub']['primary']['competitors_cited']], \
        'target must not appear as its own competitor'
    assert audit['metadata']['measured_visibility'] is mv, 'metadata mirror missing'

    # Disabled / missing inputs must be safe no-ops.
    a2, s2 = measure_visibility({'domain': 'x.com'}, engines={'stub': stub_engine})
    assert s2['applied'] is False and 'measured_visibility' not in a2, (a2, s2)
    a3, s3 = measure_visibility({'context': {'test_queries': {'p': 'q'}}}, engines={})
    assert s3['applied'] is False, s3

    print('VISIBILITY_OK')


if __name__ == '__main__':
    _selftest()
