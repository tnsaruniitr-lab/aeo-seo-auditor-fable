"""Lane B offline tests — numeric-aware query enrichment.

Evidence states measurements in probe units ('LCP: 12,417ms'); rules state
thresholds in prose units ('under 3 seconds', 'load time'). _numeric_hints
bridges the two deterministically (pure lexical, no LLM, no I/O) and
_query_for appends the hints AFTER the check topic, capped, de-duplicated
against words already in the query. Covers:
  (a) table of evidence strings -> exact expected hint tokens per metric class
  (b) canonical-unit conversion (ms->s, min->s, bytes/kb/mb/gb, %, counts)
  (c) cap + kill-switch (max_tokens=0) + hard cap
  (d) no measurement -> byte-identical legacy query (both query builds share
      _query_for: live via live_citations, snapshot via tools.query_brain and
      retrieve_batch via _spec_query_text)
  (e) determinism + URL/identifier immunity (no hints from 'page-2s', 'v2')

Prints NUMERIC_HINTS_OK on success (run_tests.sh contract)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))

import sieve_brain  # noqa: E402

NH = sieve_brain._numeric_hints
QF = sieve_brain._query_for


def test_hint_table():
    # (a)+(b) evidence string -> exact added tokens (order is part of the contract)
    table = [
        # time: ms -> canonical seconds + threshold vocabulary
        ('LCP: 12,417ms on mobile',
         ['12.4', 'seconds', 'load', 'time', 'speed']),
        ('TTFB was 900 ms', ['0.9', 'seconds', 'load', 'time', 'speed']),
        ('render took 4.2 s', ['4.2', 'seconds', 'load', 'time', 'speed']),
        ('blocked for 2 minutes', ['120', 'seconds', 'load', 'time', 'speed']),
        # size: canonical mb when >= 1 MB, kb below; bytes/gb converted
        ('page weight 2048 kb', ['2', 'mb', 'megabytes', 'page', 'size', 'weight']),
        ('total transfer 3,145,728 bytes',
         ['3', 'mb', 'megabytes', 'page', 'size', 'weight']),
        ('412 kb of javascript', ['412', 'kb', 'kilobytes', 'page', 'size', 'weight']),
        ('payload is 1.5 gb', ['1536', 'mb', 'megabytes', 'page', 'size', 'weight']),
        # percent
        ('62% of pages missing meta', ['62', 'percent', 'percentage', 'ratio']),
        ('coverage 3.5 percent', ['3.5', 'percent', 'percentage', 'ratio']),
        # counts: conceptual vocabulary for the counted noun
        ('title is 812 characters', ['character', 'count', 'length']),
        ('only 120 words of content', ['word', 'count', 'content', 'length']),
        ('6 redirects before final URL', ['redirect', 'chain', 'count']),
        ('47 links found', ['link', 'count']),
        # no measured quantity -> no hints (bare numbers are not measurements)
        ('title tag missing entirely', []),
        ('404 error on 12 pages', []),
        ('', []),
        (None, []),
        # URL/identifier immunity: numbers glued to identifiers never fire
        ('see https://x.example/page-2s and the 3-step guide near v2', []),
    ]
    for ev, want in table:
        got = NH(ev)
        assert got == want, (ev, got, want)


def test_cap_and_kill_switch():
    # (c) multi-class evidence fills classes in fixed order (time, size,
    # percent, count) and stops exactly at the cap.
    ev = '12417 ms load, 2.5 mb page, 61% images, 900 words'
    got = NH(ev)  # default cap 8
    assert got == ['12.4', 'seconds', 'load', 'time', 'speed',
                   '2.5', 'mb', 'megabytes'], got
    assert len(got) <= 8
    # explicit cap + kill switch + hard cap clamp
    assert NH(ev, max_tokens=3) == ['12.4', 'seconds', 'load'], NH(ev, 3)
    assert NH(ev, max_tokens=0) == []
    assert len(NH(ev, max_tokens=999)) <= sieve_brain._NUM_HINT_HARD_CAP
    assert sieve_brain.SIEVE_NUM_HINT_TOKENS <= sieve_brain._NUM_HINT_HARD_CAP


def test_query_wiring_and_legacy_identity():
    # (d) numberless evidence -> byte-identical legacy construction
    base = QF('A2_title_tag')
    assert QF('A2_title_tag', 'title tag missing entirely') == \
        'title tag missing entirely ' + base
    # measured evidence -> evidence still LEADS, topic anchors, hints trail
    q = QF('B2_page_load', 'LCP: 12,417ms on mobile')
    assert q.startswith('lcp: 12,417ms on mobile'), q
    # 'load' (check body) and 'speed' (B-section hint) are already in the
    # query, so only the genuinely-new tokens are appended.
    assert q.endswith('12.4 seconds time'), q
    assert 'performance core web vitals' in q  # B-section anchor kept
    # de-dup: hint words already present in evidence/base are not re-appended
    q2 = QF('A9_mobile', 'ttfb 900 ms load time')
    assert q2.endswith('0.9 seconds speed'), q2
    assert q2.count(' load ') == 1 and ' time ' in q2, q2
    # (e) determinism: same inputs -> same query, case/whitespace folded
    assert QF('B2_page_load', ' LCP:  12,417MS  on mobile ') == q
    # retrieve_batch spec path composes via the same function (contract)
    assert sieve_brain._spec_query_text(
        {'check_id': 'B2_page_load', 'evidence': 'LCP: 12,417ms on mobile'}) == q


if __name__ == '__main__':
    test_hint_table()
    test_cap_and_kill_switch()
    test_query_wiring_and_legacy_identity()
    print('NUMERIC_HINTS_OK')
