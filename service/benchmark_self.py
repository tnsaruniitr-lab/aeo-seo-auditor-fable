"""benchmark_self.py — deploy-time self-grading of the entailment display gate.

WHY: the ≥95% strict displayed-proof precision / ≤5% missed-support acceptance
targets must be MEASURED, continuously, without anyone holding credentials
outside production. The labelled 206-pair benchmark ships in-repo
(service/benchmarks/benchmark-pairs.jsonl, gold labels from the 2026-07-19
adversarially-verified labelling run); on boot the service judges every pair
through the real entailment gate (cache-first — a redeploy with an unchanged
judge re-serves cached verdicts at zero API cost) and exposes the scores on a
public, metrics-only endpoint (/benchmark-status, wired in main.py alongside
/healthz). Nothing sensitive is served: numbers, model id, git sha.

Definitions (over gold labels, judge verdicts):
  strict_precision  = of pairs the judge marks 'supports' (i.e. would DISPLAY
                      as proof), the share whose gold label is 'supports'.
  missed_support    = of pairs whose gold label is 'supports', the share the
                      judge does NOT mark 'supports' (real proof hidden).

Fail-safe: any error leaves status='error' with a reason; the audit path is
never touched (this module runs in a daemon thread started after app boot).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

log = logging.getLogger('audit.benchmark')

BENCH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'benchmarks', 'benchmark-pairs.jsonl')

# module-level result the endpoint serves; guarded by _lock
_lock = threading.Lock()
_state: Dict[str, Any] = {'status': 'not-started'}
_thread: Optional[threading.Thread] = None

ACCEPT_STRICT = 95.0
ACCEPT_MISSED = 5.0


def enabled() -> bool:
    """On unless explicitly disabled; needs a key to actually judge."""
    if os.getenv('BENCHMARK_SELF', '1').lower() in ('0', 'false', 'no'):
        return False
    return bool(os.getenv('ANTHROPIC_API_KEY'))


def load_pairs(path: str = BENCH_PATH) -> list:
    pairs = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                pairs.append(json.loads(line))
    return pairs


def run_benchmark(pairs: Optional[list] = None,
                  use_cache: bool = True) -> Dict[str, Any]:
    """Judge every benchmark pair through the real gate; compute the scores.
    Cache-first by default so repeat runs are free. Raises nothing — errors
    are folded into the result dict."""
    import citation_entailment as ce

    if pairs is None:
        pairs = load_pairs()
    judged = cached = errors = 0
    shown = shown_good = gold_supports = gold_supports_shown = 0
    conn = None
    try:
        conn = ce._open_conn()
    except Exception:  # noqa: BLE001 — cache-less judging still works
        conn = None
    t0 = time.time()
    for p in pairs:
        finding = p.get('finding') or {}
        citation = p.get('citation') or {}
        gold = p.get('gold')
        try:
            r = ce.judge_pair(finding, citation, conn=conn, use_cache=use_cache)
        except Exception as e:  # noqa: BLE001 — count and continue
            errors += 1
            log.warning('benchmark judge error on pair %s: %s', p.get('i'), e)
            continue
        judged += 1
        if r.get('cached'):
            cached += 1
        v = r.get('verdict')
        if v == 'supports':
            shown += 1
            if gold == 'supports':
                shown_good += 1
        if gold == 'supports':
            gold_supports += 1
            if v == 'supports':
                gold_supports_shown += 1
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    strict = round(100.0 * shown_good / shown, 1) if shown else None
    missed = round(100.0 * (gold_supports - gold_supports_shown) / gold_supports, 1) \
        if gold_supports else None
    import citation_entailment as ce2
    return {
        'status': 'complete' if judged else 'error',
        'n_pairs': len(pairs), 'judged': judged, 'cached': cached, 'errors': errors,
        'strict_precision': strict, 'missed_support': missed,
        'shown': shown, 'gold_supports': gold_supports,
        'accept_strict': ACCEPT_STRICT, 'accept_missed': ACCEPT_MISSED,
        'passes': (strict is not None and missed is not None
                   and strict >= ACCEPT_STRICT and missed <= ACCEPT_MISSED),
        'model': getattr(ce2, 'MODEL', None),
        'prompt_version': getattr(ce2, 'PROMPT_VERSION', None),
        'git_sha': os.getenv('RAILWAY_GIT_COMMIT_SHA', '')[:9] or None,
        'duration_s': round(time.time() - t0, 1),
        'computed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


def _worker() -> None:
    with _lock:
        _state.clear()
        _state.update({'status': 'running', 'started_at':
                       time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    try:
        result = run_benchmark()
    except Exception as e:  # noqa: BLE001 — belt and braces
        result = {'status': 'error', 'reason': f'{type(e).__name__}: {e}'[:200]}
    with _lock:
        _state.clear()
        _state.update(result)


def start_background(force: bool = False) -> str:
    """Kick the benchmark in a daemon thread. Idempotent per process unless
    force. Returns the current status string."""
    global _thread
    if not enabled():
        with _lock:
            if _state.get('status') == 'not-started':
                _state['status'] = 'disabled'
                _state['reason'] = ('BENCHMARK_SELF=0' if os.getenv('BENCHMARK_SELF', '1')
                                    .lower() in ('0', 'false', 'no')
                                    else 'no ANTHROPIC_API_KEY')
            return _state.get('status', 'disabled')
    with _lock:
        alive = _thread is not None and _thread.is_alive()
        if alive or (_state.get('status') == 'complete' and not force):
            return _state.get('status')
    _thread = threading.Thread(target=_worker, daemon=True,
                               name='entailment-benchmark')
    _thread.start()
    return 'running'


def status() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


# ---------------------------------------------------------------------------
# Self-test (stdlib only; stubs the judge — never calls the API)
# ---------------------------------------------------------------------------

def _selftest() -> None:
    import citation_entailment as ce
    pairs = load_pairs()
    assert len(pairs) >= 200, len(pairs)
    assert all(p.get('gold') in ('supports', 'related', 'unrelated') for p in pairs)
    assert all((p.get('citation') or {}).get('kind') for p in pairs), 'kind required for cache keys'

    # stub judge (all 'supports') + DB unreachable (the designed degradation:
    # _open_conn -> None, cache falls back to LRU-only)
    prev = ce._judge_fn
    ce._judge_fn = lambda f, c: 'supports'
    prev_conn = ce._open_conn
    ce._open_conn = lambda: None
    try:
        r = run_benchmark(pairs=pairs[:20], use_cache=False)
    finally:
        ce._judge_fn = prev
        ce._open_conn = prev_conn
    assert r['judged'] == 20 and r['errors'] == 0, r
    # judge says supports for all 20 -> strict == share of gold supports; no
    # gold-supports pair is hidden, so missed_support == 0
    gold_sup = sum(1 for p in pairs[:20] if p['gold'] == 'supports')
    assert r['shown'] == 20 and r['gold_supports'] == gold_sup, r
    assert r['missed_support'] == 0.0, r
    assert isinstance(r['strict_precision'], float), r
    expected_strict = round(100.0 * gold_sup / 20, 1)
    assert r['strict_precision'] == expected_strict, (r, expected_strict)

    # disabled path
    old = os.environ.pop('ANTHROPIC_API_KEY', None)
    os.environ['BENCHMARK_SELF'] = '1'
    try:
        assert not enabled()
    finally:
        if old is not None:
            os.environ['ANTHROPIC_API_KEY'] = old

    print('BENCHMARK_SELF_OK')


if __name__ == '__main__':
    _selftest()
