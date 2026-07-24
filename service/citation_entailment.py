"""
citation_entailment.py — the DISPLAY decision for finding citations moves
from the lexical supports_finding gate to a cached LLM entailment judgment.

WHY: the 206-pair labelled benchmark (build-context 2026-07-19) measured the
lexical gate at 50.4% strict displayed-proof precision with 30.3% of genuine
supports suppressed. Its failure modes — wrong platform, wrong schema entity,
inverted polarity, single-token bridges, tactic-vs-diagnostic — are invisible
to bag-of-words matching but trivial for a judge that reads platform, entity
type and polarity. The lexical gate stays as CANDIDATE annotation
(citation_attach §6, supports_finding); this module stamps every fail/warn
citation with the verdict the renderers act on:

    c['entailment'] = 'supports'   -> shown as proof
                      'related'    -> collapsed "see also" (after proof, smaller)
                      'unrelated'  -> hidden from display, KEPT in JSON
                                      (citations are never dropped)
                      'unjudged'   -> no key / API error / timeout / budget,
                                      or source provenance is unattested:
                                      context only, never proof

One short claude-haiku-4-5 call per (prompt-version, model, rule-kind,
rule-id, check-base, evidence, judged-citation-text) tuple — cached in the
auditor's OWN public.citation_entailment_cache (DDL auto-created, same
pattern as check_query_embeddings) plus an in-process LRU, so repeat
findings across audits cost ~zero API calls, while an in-place rule edit, a
model swap, or a prompt bump (PROMPT_VERSION) re-judges instead of serving
a stale verdict from the TTL-less cache.

FAIL-SAFE by construction: judge_citations never raises and never blocks an
audit — every failure path stamps 'unjudged' and is counted in the stats dict
persisted under metadata.citation_entailment.

Acceptance (scripts/entailment_benchmark.py over the labelled set — needs a
real ANTHROPIC_API_KEY): strict displayed-proof precision >= 95%,
missed-support <= 5%, before any "evidence-grade" language ships.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

log = logging.getLogger('audit.entailment')

VERDICTS = ('supports', 'related', 'unrelated')
UNJUDGED = 'unjudged'

# Default promoted haiku -> sonnet after the self-graded plateau: haiku held
# ~80 strict / 16-21 missed across prompt v1/v2 on the 206-pair benchmark —
# the supports/related boundary needs the stronger judge. Cost stays bounded:
# ~150-token judgments, cached per rule×check×text (~$0.05/audit worst case,
# ~zero once warm). MODEL participates in cache_key, so this swap re-judges.
MODEL = os.getenv('CITATION_ENTAILMENT_MODEL', 'claude-sonnet-4-5')
CALL_TIMEOUT_S = float(os.getenv('CITATION_ENTAILMENT_TIMEOUT_S', '15'))
TOTAL_BUDGET_S = float(os.getenv('CITATION_ENTAILMENT_BUDGET_S', '45'))
# Transient API failures (429 rate limit, 529 overloaded, timeouts) are worth
# retrying: the first sonnet benchmark run lost 158/206 pairs to instant
# rate-limit failures under max_retries=0. The AUDIT path stays fail-fast
# (1 attempt — the unjudged fallback + TOTAL_BUDGET_S protect the audit);
# the BENCHMARK path is patient (it runs in a daemon thread, latency is free).
_RETRY_BACKOFF_S = (2.0, 8.0, 20.0)

# Bump whenever _pair_prompt (or verdict semantics) changes: the version is
# part of cache_key, so a prompt revision re-judges instead of serving the
# old prompt's cached verdicts as if they were the new judge's.
PROMPT_VERSION = 'v3'  # v3: source excerpt is part of the judged input and a
                       # supports verdict also requires the extracted rule to
                       # be entailed by that verbatim source passage.

_JUDGED_STATUSES = ('fail', 'warn')

# ---------------------------------------------------------------------------
# Caches — in-process LRU in front of the auditor's own Postgres table.
# Same auto-create pattern as sieve_brain's public.check_query_embeddings:
# the auditor writes ONLY its own schema, never the sieve brain.
# ---------------------------------------------------------------------------

_LRU_MAX = 4096
_LRU: 'OrderedDict[str, str]' = OrderedDict()

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS public.citation_entailment_cache (
        cache_key     text PRIMARY KEY,
        verdict       text NOT NULL,
        model         text NOT NULL,
        check_id      text,
        citation_kind text,
        citation_id   text,
        created_at    timestamptz NOT NULL DEFAULT now()
    )
"""
_table_ready = False

# Seams for tests: inject a stub judge / fake DB connection factory so the
# suite never needs the API or Postgres (same pattern as citation_attach's
# _query_brain_fn).
_judge_fn = None    # (finding, citation) -> verdict str
_connect_fn = None  # () -> conn | None


def _open_conn():
    """Short-lived autocommit connection to the auditor's DATABASE_URL
    (db._connect), or None when unconfigured/unreachable — cache degrades
    to LRU-only, never blocks."""
    if _connect_fn is not None:
        return _connect_fn()
    try:
        from db import _connect
        return _connect()
    except Exception:  # noqa: BLE001
        return None


def _lru_get(key: str) -> Optional[str]:
    v = _LRU.get(key)
    if v is not None:
        _LRU.move_to_end(key)
    return v


def _lru_put(key: str, verdict: str) -> None:
    _LRU[key] = verdict
    _LRU.move_to_end(key)
    while len(_LRU) > _LRU_MAX:
        _LRU.popitem(last=False)


def _db_get(conn, key: str) -> Optional[str]:
    global _table_ready
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            if not _table_ready:
                cur.execute(_TABLE_DDL)
                _table_ready = True
            cur.execute('SELECT verdict FROM public.citation_entailment_cache'
                        ' WHERE cache_key = %s', (key,))
            row = cur.fetchone()
        v = row[0] if row else None
        return v if v in VERDICTS else None
    except Exception as e:  # noqa: BLE001
        log.info('entailment cache read unavailable (%s)', e)
        return None


def _db_put(conn, key: str, verdict: str, finding: Dict, citation: Dict) -> None:
    global _table_ready
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            if not _table_ready:
                cur.execute(_TABLE_DDL)
                _table_ready = True
            cur.execute(
                'INSERT INTO public.citation_entailment_cache'
                ' (cache_key, verdict, model, check_id, citation_kind, citation_id)'
                ' VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (cache_key) DO NOTHING',
                (key, verdict, MODEL, str(finding.get('check_id') or '')[:120],
                 str(citation.get('kind') or '')[:40],
                 str(citation.get('id') if citation.get('id') is not None else '')[:80]))
    except Exception as e:  # noqa: BLE001
        log.info('entailment cache write unavailable (%s)', e)


# ---------------------------------------------------------------------------
# Cache key — sha256(prompt-version:model:rule-kind:rule-id:check-base:
# evidence-hash:citation-text-hash).
# check BASE (A1, D12b, ...) not the full id, so aliased/renamed ids that
# canonicalize to the same slot share judgments; evidence prefix-hashed so a
# different observation on the same check is re-judged. The JUDGED citation
# text participates too: when a rule's if/then is edited in-place in the
# brain, the regrounded text the judge reads changes, and the stale verdict
# must not be served forever (the DB cache has no TTL). MODEL and
# PROMPT_VERSION participate so a judge/prompt revision re-judges instead of
# reporting the OLD judge's cached verdicts — the acceptance benchmark
# (entailment_benchmark.py) depends on this.
# ---------------------------------------------------------------------------

def _cite_text(citation: Dict) -> str:
    """The citation text the judge actually reads — same field fallbacks and
    truncations as _pair_prompt, so the cache key tracks the judged input."""
    name = str(citation.get('name') or citation.get('title') or '(unnamed)')[:200]
    cond = str(citation.get('if_condition') or citation.get('statement')
               or citation.get('description') or '')[:300]
    act = str(citation.get('then_action') or citation.get('explanation') or '')[:300]
    excerpt = str(citation.get('source_excerpt') or '')[:500]
    return f'{name}\n{cond}\n{act}\n{excerpt}'


def cache_key(finding: Dict, citation: Dict) -> str:
    try:
        from rule_eval import check_base
        base = check_base(finding.get('check_id'))
    except Exception:  # noqa: BLE001
        base = None
    base = base or str(finding.get('check_id') or '?')
    kind = str(citation.get('kind') or '?').lower()
    cid = citation.get('id')
    cid = str(cid) if cid is not None else str(citation.get('name') or '?')
    ev = str(finding.get('evidence') or '').strip().lower()
    ev_hash = hashlib.sha256(ev.encode('utf-8')).hexdigest()[:16]
    ct_hash = hashlib.sha256(_cite_text(citation).encode('utf-8')).hexdigest()[:16]
    return hashlib.sha256(
        f'{PROMPT_VERSION}:{MODEL}:{kind}:{cid}:{base}:{ev_hash}:{ct_hash}'
        .encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# The judgment — ONE short claude-haiku-4-5 call, temperature 0, strict JSON.
# The five measured failure modes are explicit checks in the prompt.
# ---------------------------------------------------------------------------

def _pair_prompt(finding: Dict, citation: Dict) -> str:
    ev = str(finding.get('evidence') or '(no evidence recorded)')[:400]
    name = str(citation.get('name') or citation.get('title') or '(unnamed)')[:200]
    cond = str(citation.get('if_condition') or citation.get('statement')
               or citation.get('description') or '')[:300]
    act = str(citation.get('then_action') or citation.get('explanation') or '')[:300]
    excerpt = str(citation.get('source_excerpt') or '')[:500]
    return (
        'You judge whether a cited guideline proves a website-audit finding.\n\n'
        f"FINDING check: {finding.get('check_id')}\n"
        f'EVIDENCE: {ev}\n\n'
        f"CITED {citation.get('kind') or 'item'}: {name}\n"
        f'IF: {cond}\n'
        f'THEN: {act}\n'
        f'VERBATIM SOURCE EXCERPT: {excerpt}\n\n'
        'Decide in two steps.\n\n'
        'STEP 1 — eliminate "unrelated". It is unrelated if ANY of: '
        '(1) different platform — a LinkedIn/YouTube/social rule never proves a '
        'website finding; (2) different schema.org entity type; (3) inverted '
        'polarity — the rule endorses what the finding faults, or triggers on '
        'the inverse condition; (4) the overlap is a single shared word; (5) it '
        'is an outreach/marketing tactic offered against a site-state '
        'diagnostic.\n\n'
        'STEP 2 — "supports" requires ALL THREE: '
        '(a) the IF-condition describes the same condition class the EVIDENCE '
        'observed — same feature AND same aspect of it. A more GENERAL rule '
        'still passes when the observed evidence is an instance of its '
        'condition (evidence "LCP 4.1s" is an instance of "IF LCP exceeds '
        '2.5s"). (b) the THEN prescribes fixing or avoiding exactly what the '
        'finding flags; and (c) the VERBATIM SOURCE EXCERPT itself supports '
        'the cited IF/THEN guidance without adding a requirement the excerpt '
        'does not state. Same feature but a DIFFERENT aspect fails (a): '
        'evidence "title lacks the brand name" vs rule "IF title exceeds 60 '
        'characters THEN shorten" is the same feature (title tag) but a '
        'different aspect (branding vs length) — that is "related", never '
        '"supports". Likewise a rule that presupposes a state the evidence '
        'contradicts (rule about cleaning 404s FROM a sitemap when the '
        'evidence is "sitemap missing") is "related".\n'
        'Everything that survives STEP 1 but fails STEP 2 is "related".\n\n'
        'Calibration examples:\n'
        '- EVIDENCE "measured LCP 4.1s" / IF "LCP exceeds 2.5s" THEN "reduce '
        'render-blocking resources" -> supports (instance of the condition).\n'
        '- EVIDENCE "FAQ-style content carries no FAQPage markup" / IF "page '
        'answers common questions" THEN "add FAQPage structured data" -> '
        'supports (general rule, observed instance).\n'
        '- EVIDENCE "title lacks brand name" / IF "title exceeds 60 chars" '
        'THEN "shorten it" -> related (same feature, different aspect).\n'
        '- EVIDENCE "sitemap file missing" / IF "sitemap lists 404 URLs" THEN '
        '"remove them" -> related (presupposes the missing state).\n\n'
        'Reply with ONLY this JSON: {"verdict":"supports"|"related"|"unrelated"}'
    )


def _parse_verdict(text: str) -> Optional[str]:
    """Strict JSON first; degrade to a single unambiguous verdict word."""
    try:
        start, end = text.index('{'), text.rindex('}') + 1
        v = json.loads(text[start:end]).get('verdict')
        if v in VERDICTS:
            return v
    except Exception:  # noqa: BLE001
        pass
    # Word-boundary match ('related' is a substring of 'unrelated'); only
    # trust the bare-word fallback when exactly one verdict is mentioned.
    found = {m.group(0) for m in re.finditer(r'\b(supports|related|unrelated)\b', text)}
    return next(iter(found)) if len(found) == 1 else None


def _transient(exc: Exception) -> bool:
    status = getattr(exc, 'status_code', None)
    if isinstance(status, int) and status in (429, 500, 502, 503, 529):
        return True
    return type(exc).__name__ in (
        'RateLimitError', 'OverloadedError', 'APITimeoutError',
        'APIConnectionError', 'InternalServerError', 'ServiceUnavailableError',
    )


def _call_api(finding: Dict, citation: Dict, retries: int = 0) -> str:
    """One classification call (+ up to `retries` backoff retries on transient
    API failures). Raises on missing SDK/key, terminal API error, timeout or
    unparseable output — the caller decides what a failure means."""
    from anthropic import Anthropic
    client = Anthropic(timeout=CALL_TIMEOUT_S, max_retries=0)
    attempt = 0
    while True:
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=64,
                temperature=0,
                messages=[{'role': 'user', 'content': _pair_prompt(finding, citation)}],
            )
            break
        except Exception as e:  # noqa: BLE001
            if attempt >= retries or not _transient(e):
                raise
            time.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])
            attempt += 1
    text = ''.join(getattr(b, 'text', '') for b in resp.content
                   if getattr(b, 'type', '') == 'text')
    v = _parse_verdict(text)
    if v is None:
        raise ValueError(f'unparseable verdict: {text[:120]!r}')
    return v


def _judge(finding: Dict, citation: Dict, retries: int = 0) -> str:
    if _judge_fn is not None:
        return _judge_fn(finding, citation)
    return _call_api(finding, citation, retries=retries)


def judge_pair(finding: Dict, citation: Dict, conn: Any = None,
               use_cache: bool = True, retries: int = 0) -> Dict[str, Any]:
    """Judge one finding<->citation pair. Returns
    {'verdict': 'supports'|'related'|'unrelated', 'cached': bool}.

    Cache order: LRU -> public.citation_entailment_cache -> ONE API call.
    use_cache=False skips both reads (the result is still written back) —
    the acceptance benchmark's --no-cache mode, so a run always exercises
    the LIVE judge rather than reporting cached verdicts.
    Raises on judge failure (no key, timeout, bad output) — fail-safety lives
    in judge_citations, and the benchmark harness wants real errors.
    When conn is None a short-lived connection is opened just for this call;
    judge_citations shares one across the batch."""
    key = cache_key(finding, citation)
    if use_cache:
        v = _lru_get(key)
        if v is not None:
            return {'verdict': v, 'cached': True}
    own = conn is None
    if own:
        conn = _open_conn()
    try:
        v = _db_get(conn, key) if use_cache else None
        if v is not None:
            _lru_put(key, v)
            return {'verdict': v, 'cached': True}
        v = _judge(finding, citation, retries=retries)
        if v not in VERDICTS:
            raise ValueError(f'judge returned invalid verdict: {v!r}')
        _lru_put(key, v)
        _db_put(conn, key, v, finding, citation)
        return {'verdict': v, 'cached': False}
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Judge-at-selection (Lane A) — citation_attach retrieves a candidate POOL
# and judges it HERE, with the same warm cache, before cutting to the
# displayed top-3. Measured on the labelled set: the right rule sat in the
# candidate pool for 74.2% of findings but in the displayed slots for only
# 48.5% — judging at selection closes 59% of those misses (+25.7pt).
# Because every pool verdict lands in the shared LRU+DB cache, the display
# gate (judge_citations, post-regrounding) re-reads the selected citations'
# verdicts as cache hits: judged-at-selection citations render pre-judged
# for free, and repeat audits cost ~zero API calls.
# ---------------------------------------------------------------------------

def selection_judge_available() -> bool:
    """True when judge-at-selection can produce verdicts: a stub judge is
    injected (tests / future local model) or an API key is present. The
    CITATION_ENTAILMENT kill-switch disables selection judging too — one
    switch turns off every LLM citation judgment."""
    if os.getenv('CITATION_ENTAILMENT', '1') in ('0', 'false', 'False'):
        return False
    return _judge_fn is not None or bool(os.getenv('ANTHROPIC_API_KEY'))


def judge_selection_pool(finding: Dict, citations: List[Any], conn: Any = None,
                         deadline: Optional[float] = None):
    """Judge every candidate in a retrieval pool against one finding.

    Returns (verdicts, info): verdicts aligned 1:1 with `citations` (each
    'supports'|'related'|'unrelated'|'unjudged'); info counts cache_hits /
    api_calls / errors / budget_exhausted.

    NEVER raises. The per-pair path is identical to judge_citations
    (LRU -> public.citation_entailment_cache -> ONE API call), so pool
    judgments warm the SAME cache the display gate reads later. Cache reads
    stay allowed past the deadline (a hit costs nothing) — only API calls
    stop, and the remaining pairs degrade to 'unjudged', which the selector
    ranks exactly where legacy retrieval order would put them."""
    verdicts: List[str] = []
    info: Dict[str, Any] = {'cache_hits': 0, 'api_calls': 0, 'errors': 0,
                            'source_unattested': 0,
                            'budget_exhausted': False}
    has_judge = selection_judge_available()
    for c in citations:
        v = None
        if isinstance(c, dict):
            # This must precede both cache lookup and API judgment. Otherwise a
            # legacy cached "supports" verdict can promote a rule whose source
            # excerpt was never verified, displacing a source-faithful rule
            # before the later display gate gets a chance to demote it.
            if c.get('source_faithful') is not True:
                c['provenance_blocked'] = True
                info['source_unattested'] += 1
                verdicts.append(UNJUDGED)
                continue
            try:
                key = cache_key(finding, c)
                v = _lru_get(key)
                if v is None:
                    v = _db_get(conn, key)
                    if v is not None:
                        _lru_put(key, v)
                if v is not None:
                    info['cache_hits'] += 1
                elif not has_judge:
                    pass                                    # -> unjudged
                elif deadline is not None and time.monotonic() > deadline:
                    info['budget_exhausted'] = True         # -> unjudged
                else:
                    v = _judge(finding, c)
                    if v not in VERDICTS:
                        raise ValueError(f'invalid verdict {v!r}')
                    info['api_calls'] += 1
                    _lru_put(key, v)
                    _db_put(conn, key, v, finding, c)
            except Exception as e:  # noqa: BLE001 — one bad pair never stops selection
                info['errors'] += 1
                v = None
                log.warning('selection judge failed for %s / %s#%s: %s',
                            finding.get('check_id'), c.get('kind'), c.get('id'), e)
        verdicts.append(v if v in VERDICTS else UNJUDGED)
    return verdicts, info


# ---------------------------------------------------------------------------
# Batch pass — post-loop, AFTER reground_citations (judgments must read the
# final verbatim text). Stamps c['entailment'] on every fail/warn citation.
# ---------------------------------------------------------------------------

def judge_citations(findings: Any) -> Dict[str, Any]:
    """Stamp c['entailment'] on every citation of every fail/warn finding.
    NEVER raises; every failure path stamps 'unjudged' and is counted."""
    stats: Dict[str, Any] = {
        'applied': True, 'model': MODEL,
        'eligible_findings': 0, 'citations_seen': 0,
        'judged': 0, 'cache_hits': 0, 'api_calls': 0,
        'supports': 0, 'related': 0, 'unrelated': 0, 'unjudged': 0,
        'source_unattested': 0,
        'errors': 0, 'budget_exhausted': False, 'no_api_key': False,
    }
    conn = None
    try:
        if not isinstance(findings, list):
            return stats
        # A stub judge (tests / future local model) needs no key.
        has_judge = _judge_fn is not None or bool(os.getenv('ANTHROPIC_API_KEY'))
        if not has_judge:
            stats['no_api_key'] = True
        start = time.monotonic()
        conn = _open_conn()
        for f in findings:
            if not isinstance(f, dict) or f.get('status') not in _JUDGED_STATUSES:
                continue
            cites = f.get('citations')
            if not isinstance(cites, list) or not cites:
                continue
            stats['eligible_findings'] += 1
            for c in cites:
                if not isinstance(c, dict):
                    continue
                stats['citations_seen'] += 1
                # A cached or newly generated supports verdict can never
                # rescue a rule whose source excerpt was not verified by the
                # ingestion pipeline. This gate precedes cache lookup to stop
                # legacy cached verdicts leaking through after migration.
                if c.get('source_faithful') is not True:
                    c['entailment'] = UNJUDGED
                    c['provenance_blocked'] = True
                    stats['source_unattested'] += 1
                    stats['unjudged'] += 1
                    continue
                verdict = None
                try:
                    key = cache_key(f, c)
                    # Cache reads stay allowed with no key and past the
                    # budget — a hit costs nothing.
                    v = _lru_get(key)
                    if v is None:
                        v = _db_get(conn, key)
                        if v is not None:
                            _lru_put(key, v)
                    if v is not None:
                        verdict = v
                        stats['cache_hits'] += 1
                    elif not has_judge:
                        pass  # -> unjudged
                    elif time.monotonic() - start > TOTAL_BUDGET_S:
                        # Early stop: no more API calls this audit; the rest
                        # of the citations are stamped unjudged (cache hits
                        # above still land).
                        stats['budget_exhausted'] = True
                    else:
                        verdict = _judge(f, c)
                        if verdict not in VERDICTS:
                            raise ValueError(f'invalid verdict {verdict!r}')
                        stats['api_calls'] += 1
                        _lru_put(key, verdict)
                        _db_put(conn, key, verdict, f, c)
                except Exception as e:  # noqa: BLE001 — one bad pair never stops the pass
                    stats['errors'] += 1
                    verdict = None
                    log.warning('entailment judge failed for %s / %s#%s: %s',
                                f.get('check_id'), c.get('kind'), c.get('id'), e)
                if verdict in VERDICTS:
                    c['entailment'] = verdict
                    stats['judged'] += 1
                    stats[verdict] += 1
                else:
                    c['entailment'] = UNJUDGED
                    stats['unjudged'] += 1
    except Exception as e:  # noqa: BLE001
        stats['applied'] = False
        stats['error'] = f'{type(e).__name__}: {e}'
        log.error('citation entailment pass failed: %s', e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return stats
