#!/usr/bin/env bash
# run_tests.sh — stdlib-only test runner for the fable auditor.
#
# Exercises what the clean layout actually ships:
#   - the deterministic script selftests (FAQ true/false, SSR classification)
#   - a real XML-parser fixture (entity/CDATA handling)
#   - the new deterministic scoring module (recompute + XSS-value neutralization)
#   - the new delta/re-score engine
#   - py_compile across every service module + script
#
# Usage: bash tests/run_tests.sh    (exit 0 = all pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIX_DIR="${SCRIPT_DIR}/fixtures"
SERVICE_DIR="${SCRIPT_DIR}/../service"
SCRIPTS_DIR="${SERVICE_DIR}/scripts"

PASS=0
FAIL=0
FAILURES=()

ok()   { PASS=$((PASS+1)); echo "  ✓ $1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); echo "  ✗ $1"; [[ -n "${2:-}" ]] && echo "    $2"; }

assert_contains() {
    if [[ "$2" == *"$3"* ]]; then ok "$1"; else bad "$1 — expected '$3'" "${2:0:200}"; fi
}

echo "=============================================="
echo "AEO/SEO/GEO auditor — test suite"
echo "=============================================="

# ----------------------------------------------------------------------
echo ""
echo "[1] Bot's-Eye-View selftest (FAQ detection + SSR classification)"
OUT=$(cd "${SCRIPTS_DIR}" && python3 _bev_analyze.py --selftest 2>&1)
RC=$?
if [[ $RC -eq 0 ]]; then
    assert_contains "country accordion is NOT counted as FAQ" "$OUT" "country_accordion_not_faq.html: faq_count=0"
    assert_contains "real FAQ accordion counts 6"            "$OUT" "real_faq_accordion.html: faq_count=6"
else
    bad "bev selftest exited non-zero ($RC)" "$OUT"
fi

# ----------------------------------------------------------------------
echo ""
echo "[2] Sitemap XML parser — entity + CDATA handling"
OUT=$(python3 -c "
import xml.etree.ElementTree as ET
tree = ET.parse('${FIX_DIR}/sitemap_with_entities.xml')
ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
urls = [u.find('sm:loc', ns).text.strip() for u in tree.getroot().findall('sm:url', ns)]
print('COUNT=' + str(len(urls)))
for u in urls: print('URL=' + u)
")
assert_contains "URL count correct" "$OUT" "COUNT=4"
assert_contains "entity-encoded ampersand preserved" "$OUT" "URL=https://example.com/search?q=hello&lang=en"

# ----------------------------------------------------------------------
echo ""
echo "[3] Deterministic scoring — recompute + malformed-value neutralization"
OUT=$(cd "${SERVICE_DIR}" && python3 -c "
from scoring import finalize_scoring, grade_for, VALID_GRADES
audit = {
  'bots_eye_view': {'classification': 'fully_accessible'},
  'scoring': {'overall_score': '99\"><script>', 'overall_grade': 'A+', 'section_scores': {'A_technical':'BOGUS'}},
  'findings': [
    {'check_id':'A1','section':'A','status':'pass'}, {'check_id':'A2','section':'A','status':'fail'},
    {'check_id':'D1','section':'D','status':'pass'}, {'check_id':'D2','section':'D','status':'warn'},
  ],
}
sc = finalize_scoring(audit)['scoring']
assert isinstance(sc['section_scores']['A_technical'], float), 'forged section score not neutralized'
assert sc['overall_grade'] in VALID_GRADES, 'grade out of enum'
assert sc['computed_by'] == 'runtime-deterministic', 'not runtime-computed'
# Determinism: same input -> same output
sc2 = finalize_scoring({**audit, 'scoring': {}})['scoring']
assert sc['overall_score'] == sc2['overall_score'], 'non-deterministic recompute'
print('SCORING_OK score=%s grade=%s' % (sc['overall_score'], sc['overall_grade']))
" 2>&1)
assert_contains "scoring is deterministic + neutralizes forged values" "$OUT" "SCORING_OK"

# ----------------------------------------------------------------------
echo ""
echo "[4] Delta engine — resolved / regressed / new / persisting"
OUT=$(cd "${SERVICE_DIR}" && python3 -c "
from delta import compute_delta
prior   = {'scoring':{'overall_score':62},'findings':[{'check_id':'A1','status':'fail'},{'check_id':'A2','status':'pass'},{'check_id':'D1','status':'warn'}]}
current = {'scoring':{'overall_score':81},'findings':[{'check_id':'A1','status':'pass'},{'check_id':'A2','status':'fail'},{'check_id':'D1','status':'warn'},{'check_id':'G1','status':'fail'}]}
d = compute_delta(prior, current)
assert d['resolved']==['A1'] and d['regressed']==['A2'] and d['new_issues']==['G1'] and d['persisting']==['D1'], d
assert d['score_delta']['change']==19, d['score_delta']
print('DELTA_OK ' + d['summary'])
" 2>&1)
assert_contains "delta classifies check transitions + score change" "$OUT" "DELTA_OK"

# ----------------------------------------------------------------------
echo ""
echo "[5] Citation re-grounding — verbatim-by-construction quotes"
OUT=$(cd "${SERVICE_DIR}" && python3 citation_grounding.py 2>&1)
assert_contains "citations re-fetched from brain by id; paraphrase overwritten; degrades safely" "$OUT" "GROUNDING_OK"

# ----------------------------------------------------------------------
echo ""
echo "[6] Check-id vocabulary — semantic guard + canonicalization against brain-mappings"
OUT=$(cd "${SERVICE_DIR}" && python3 check_vocab.py 2>&1)
assert_contains "renames need semantic agreement (alias table / token overlap); cross-topic squatters stay foreign; provenance stamped; collision-safe" "$OUT" "VOCAB_OK"

# ----------------------------------------------------------------------
echo ""
echo "[7] Deterministic citation attachment — Python cites every fail/warn (+ judge-at-selection pool)"
OUT=$(cd "${SERVICE_DIR}" && python3 citation_attach.py 2>&1)
assert_contains "fail/warn findings get top-3 brain citations; LLM picks replaced; supports_finding annotated; foreign/renamed go evidence-led; fault-tolerant; judged 12-pool promotes rank-9 supports over rank-1 related, degrades byte-identical without a judge, deterministic + warm-cache on rerun, budget-capped" "$OUT" "ATTACH_OK"

# ----------------------------------------------------------------------
echo ""
echo "[8] Measured AI visibility — engine sweep, SOV, fault tolerance"
OUT=$(cd "${SERVICE_DIR}" && python3 ai_visibility.py 2>&1)
assert_contains "queries executed k-times per engine; inclusion + SOV computed; per-call faults tolerated" "$OUT" "VISIBILITY_OK"

# ----------------------------------------------------------------------
echo ""
echo "[9] AnswerMonk sync — POST persisted audit to a mocked ingest endpoint"
OUT=$(cd "${SERVICE_DIR}" && python3 "${SCRIPT_DIR}/test_answermonk_post.py" 2>&1)
assert_contains "audit POSTed with key header; 5xx retried once; 4xx/unconfigured/unreachable degrade safely" "$OUT" "ANSWERMONK_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10] Wired deterministic checks (A5/A1/B9/A3/C10/E4/E12) + evidence tiers"
OUT=$(python3 "${SCRIPT_DIR}/test_new_checks.py" 2>&1)
assert_contains "noindex/HTTPS/mixed-content/meta-desc/OG/snippet checks + measured|llm-judged tiers; PCR weights unchanged" "$OUT" "NEWCHECKS_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10b] Site context seam — lenient sanitation + measured/narrative-only prompt block + request acceptance"
OUT=$(cd "${SERVICE_DIR}" && python3 "${SCRIPT_DIR}/test_site_context.py" 2>&1)
assert_contains "siteContext sanitized leniently; prompt CONTEXT measured + narrative-only; start request accepted with and without it" "$OUT" "SITE_CONTEXT_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10b2] Cost tier-0/1: true-cost shadow accounting + pause_turn cache-breakpoint fix + Phase-13 runtime citations + skip_visibility seam"
OUT=$(cd "${SERVICE_DIR}" && python3 "${SCRIPT_DIR}/test_cost_tier01.py" 2>&1)
assert_contains "cache fix marks dictified blocks + never non-cacheable types; ceiling metric frozen; Phase 13 mandate gone; skipVisibility accepted" "$OUT" "COST_TIER01_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10c] Mobile parity + honest CWV labels + deterministic E-E-A-T subset (G1/G2/G7b/G7c)"
OUT=$(python3 "${SCRIPT_DIR}/test_mobile_eeat.py" 2>&1)
assert_contains "mobile flag + parity comparator + lab-CWV/INP honesty + byline/about-contact/editorial/schema-author checks; PCR weights unchanged" "$OUT" "MOBILE_EEAT_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10d] Brain freshness: status filter + provenance rank + strict mode + disclosure"
OUT=$(python3 "${SCRIPT_DIR}/test_brain_freshness.py" 2>&1)
assert_contains "retired rules unciteable; provenance-ranked URLs; SIEVE_STRICT typed error; report disclosure + evidence_tier column" "$OUT" "BRAIN_FRESHNESS_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10e] Brain Console: routes + central auth gate + probe validation + page markers"
OUT=$(python3 "${SCRIPT_DIR}/test_brain_console.py" 2>&1)
assert_contains "console routes registered, auth centralized at include, SourceSpec validation, probe spec errors, HTML markers" "$OUT" "BRAIN_CONSOLE_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10f] Sieve binding Phase 1: evidence query + cosine floor + relevance-first sort + embed pinning"
OUT=$(python3 "${SCRIPT_DIR}/test_sieve_binding.py" 2>&1)
assert_contains "evidence query, off-topic floor, relevance beats authority across bands, DB cosine floor, embed guard" "$OUT" "SIEVE_BINDING_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10g] Sieve binding Phase 2: BM25 snapshot retrieval over all 3 kinds + neutral AP confidence + freshness"
OUT=$(python3 "${SCRIPT_DIR}/test_sieve_snapshot.py" 2>&1)
assert_contains "principles reachable, empty-mapping checks covered, AP confidence neutral not risk-derived, snapshot freshness stamped" "$OUT" "SIEVE_SNAPSHOT_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10h] Sieve binding Phase 3: deterministic rule_eval (mode C) + binding_gate verifier (mode B)"
OUT=$(cd "${SERVICE_DIR}" && python3 rule_eval.py 2>&1)
assert_contains "measured checks bound to a verbatim rule; unresolved/non-measured never mis-bound" "$OUT" "RULE_EVAL_OK"
OUT=$(cd "${SERVICE_DIR}" && python3 binding_gate.py 2>&1)
assert_contains "binding gate: existence + candidacy + support; hallucinated/off-topic ids rejected not stamped" "$OUT" "BINDING_GATE_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10i] Sieve binding Phase 4: rule-weighted scoring — byte-identical default, bounded + symmetric"
OUT=$(python3 "${SCRIPT_DIR}/test_sieve_scoring.py" 2>&1)
assert_contains "LAMBDA=0 byte-identical to unweighted; verified binding reweights bounded+symmetric; unverified never moves; status never flipped" "$OUT" "SIEVE_SCORING_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10j] Sieve binding Phase 5: compact API emits citations + boundRule + brain-mode disclosure"
OUT=$(python3 "${SCRIPT_DIR}/test_compact_citations.py" 2>&1)
assert_contains "compact issues carry slim citations + verified boundRule; sourcesMode live/snapshot/mixed/none; snapshotDate surfaced" "$OUT" "COMPACT_CITATIONS_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10k] Evidence identity: observed-join honesty + method labels + evidence-led retrieval + support surfacing"
OUT=$(cd "${SERVICE_DIR}" && python3 "${SCRIPT_DIR}/test_evidence_identity.py" 2>&1)
assert_contains "observed only on a real script match (script's words, honest method); evidence_led skips curated mapping; vocabStatus/originalCheckId/supportsFinding surfaced compact+HTML" "$OUT" "EVIDENCE_IDENTITY_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10l] Fix chain: per-finding fix backstop + compact fix/https + brain-retrieve evidence + deprecation guard"
OUT=$(cd "${SERVICE_DIR}" && python3 "${SCRIPT_DIR}/test_fix_chain.py" 2>&1)
assert_contains "fix backstop joins narrative by check_id (never invents); compact prefers finding.fix + https fullReportUrl; retrieve evidence-led (truncate 400); deprecated guidance excluded+counted in attach/ranker/fix-sources" "$OUT" "FIX_CHAIN_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10m] Shadow dual-score: classic byte-identity + observed-gated evidence math + null paths + surfaces"
OUT=$(cd "${SERVICE_DIR}" && python3 "${SCRIPT_DIR}/test_shadow_scoring.py" 2>&1)
assert_contains "classic scoring byte-identical (golden); shadow counts only runtime-observed findings (forgeable tier/check_id closed), renormalized, GEO excluded from coverage; null with reason when no evidence; clamp_shadow on both copies (validate + compact metadata fallback); hero line gated on non-null" "$OUT" "SHADOW_SCORING_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10n] Citation entailment: cached LLM display verdict + fail-safe + render branches"
OUT=$(cd "${SERVICE_DIR}" && python3 "${SCRIPT_DIR}/test_citation_entailment.py" 2>&1)
assert_contains "entailment verdict cached (LRU+DB) per rule×check-base×evidence; no-key/error/budget stamp unjudged never block; wired after re-grounding; supports/related/unrelated render branches + compact entailment field" "$OUT" "ENTAILMENT_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10o] Corpus wiring: playbooks retrievable + rendered, canon-org tier parity, snapshot freshness fields, status-gate parity, NORM tier-4 default"
OUT=$(cd "${SERVICE_DIR}" && python3 "${SCRIPT_DIR}/test_corpus_wiring.py" 2>&1)
assert_contains "playbook kind reachable via BM25 + curated mapping + kind badge; canon_org before tier lookup fixes name drift (shared org-tiers.json, live=snapshot); last_verified/status carried when exported; deprecated rows unciteable from snapshot; retrieve default includes practitioner tier 4, never tier 5" "$OUT" "CORPUS_WIRING_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10p] Self-benchmark: labelled pairs ship in-repo; scores computed via the real gate (stubbed judge); disabled path honest"
OUT=$(cd "${SERVICE_DIR}" && python3 benchmark_self.py 2>&1)
assert_contains "206 labelled pairs load with kind/id; stubbed all-supports judge yields exact strict/missed math under DB-less degradation; enabled() honest without a key" "$OUT" "BENCHMARK_SELF_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10q] Cluster canonicalization tooling: blocking + rule_key/similarity clustering + deterministic election + reversible demote plan"
OUT=$(cd "${SCRIPTS_DIR}" && python3 canonicalize_clusters.py --selftest 2>&1)
assert_contains "near-dup clustering (jaccard+cosine gate, rule_key groups, topic blocks); deterministic election (tier>url>extracted>freshest>confidence>id); prior-status capture + guarded reversal SQL; plan validation rejects self/cross supersession" "$OUT" "CANON_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10r] Numeric-aware query enrichment: canonical units + conceptual vocab, capped, numberless evidence byte-identical"
OUT=$(python3 "${SCRIPT_DIR}/test_numeric_hints.py" 2>&1)
assert_contains "measured quantities canonicalized (ms/min->seconds, bytes/kb/mb/gb, %, counted nouns) + threshold vocab appended after the check topic; capped at 8 + kill switch; deduped vs query words; URL/identifier numbers never fire; live+snapshot+retrieve builds share _query_for" "$OUT" "NUMERIC_HINTS_OK"

# ----------------------------------------------------------------------
echo ""
echo "[10s] Cite-readiness Phase 2: calibrated config consumer + tier-0 gates + E14 llms.txt + per-bot robots verdicts"
OUT=$(cd "${SERVICE_DIR}" && python3 "${SCRIPT_DIR}/test_cite_readiness.py" 2>&1)
assert_contains "aeo-scoring-model consumed (null-tolerant); renormalized factor math; gate-zero distinct from INCONCLUSIVE-null (unknown fails open); product default on low confidence; E14 pass/soft-200-reject; GPTBot/Google-Extended tier-0 verdicts; clamp on both compact copies" "$OUT" "CITE_READINESS_OK"

# ----------------------------------------------------------------------
echo ""
echo "[11] py_compile — every service module + script parses"
COMPILE_OK=1
for f in "${SERVICE_DIR}"/*.py "${SCRIPTS_DIR}"/*.py; do
    if ! python3 -m py_compile "$f" 2>/dev/null; then
        COMPILE_OK=0; bad "py_compile failed: $(basename "$f")"
    fi
done
[[ $COMPILE_OK -eq 1 ]] && ok "all modules + scripts compile"

# ----------------------------------------------------------------------
echo ""
echo "=============================================="
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "=============================================="
if [[ $FAIL -gt 0 ]]; then
    printf '  - %s\n' "${FAILURES[@]}"
    exit 1
fi
exit 0
