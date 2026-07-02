#!/usr/bin/env bash
# run_deterministic_v2.sh — Fixed parallel orchestrator.
#
# Replaces the original scripts/run_deterministic.sh. Fixes:
#
# 1. Per-PID wait with failure isolation.
#    Original used `set -euo pipefail` + `wait $P1 $P2 ...`. If ANY child
#    exited non-zero (e.g., sitemap fetch timeout), `wait` returned non-zero
#    and `set -e` killed the whole script — losing the other 4 scripts'
#    output. Now: `wait` each PID individually, record exit code, continue.
#
# 2. Safe URL quoting.
#    Target URL is passed as a separate argv entry to every child script,
#    never interpolated into a shell command string. Prevents URL fragments
#    like `?q=x&y=z` from being split or re-interpreted by the shell.
#
# 3. Per-script timeout guard.
#    Each background child gets wrapped in `timeout` so a hung child can't
#    block the orchestrator forever.
#
# 4. Clear per-script status reporting in human mode.
#    Shows which children succeeded / failed / timed out.
#
# Usage:
#   bash scripts-v2/run_deterministic_v2.sh <URL>              # JSON
#   bash scripts-v2/run_deterministic_v2.sh <URL> human        # human report

set -uo pipefail  # NOTE: NOT -e — we handle child failures per-PID below

if [ "${1:-}" = "" ]; then
  echo '{"error":"missing URL","usage":"bash run_deterministic_v2.sh <URL> [human]"}'
  exit 1
fi

URL="$1"

# Default to https:// when no scheme is given — children derive the origin
# from this URL and a scheme-less input breaks every one of them. Scheme
# match is case-insensitive (HTTP://x.com must not get a second scheme).
case "$(printf '%s' "$URL" | tr '[:upper:]' '[:lower:]')" in
  http://*|https://*) ;;
  *) URL="https://${URL}" ;;
esac

MODE="${2:-json}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PER_SCRIPT_TIMEOUT=60

# Original-script directory (where the in-tree scripts still live if we
# want to run them alongside v2 scripts)
ORIG_DIR="${SCRIPT_DIR}"

# Portable timeout: prefer GNU timeout / gtimeout when available, otherwise
# fall back to a stdlib-only Python wrapper (_timeout.py). The previous
# version called `timeout` directly, which produced exit 127 on macOS where
# coreutils isn't installed — silently turning every child into
# unparseable_output and running zero checks.
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_CMD=(timeout "$PER_SCRIPT_TIMEOUT")
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_CMD=(gtimeout "$PER_SCRIPT_TIMEOUT")
else
    TIMEOUT_CMD=(python3 "${SCRIPT_DIR}/_timeout.py" "$PER_SCRIPT_TIMEOUT")
fi

# Temp files for per-child output + exit code
TMP=$(mktemp -d "${TMPDIR:-/tmp}/auditor-v2-XXXXXX")
trap 'rm -rf "$TMP"' EXIT

# Helper: run a child in background, record exit code
# args: LABEL COMMAND_WITH_ARGS_AS_STRING
run_child() {
    local label="$1"
    shift
    local out_file="${TMP}/${label}.out"
    local err_file="${TMP}/${label}.err"
    local exit_file="${TMP}/${label}.exit"

    (
        "${TIMEOUT_CMD[@]}" "$@" > "$out_file" 2> "$err_file"
        echo $? > "$exit_file"
    ) &
    eval "PID_${label}=$!"
}

# Decide which scripts to call: v2 where available, originals otherwise.
# This lets users run v2 overlaid on original install without copying files.
BEV_SCRIPT="${ORIG_DIR}/bots_eye_view.sh"
DET_SCRIPT="${ORIG_DIR}/deterministic_checks.py"
ROBOTS_SCRIPT="${SCRIPT_DIR}/check_robots_txt.py"
SITEMAP_SCRIPT="${SCRIPT_DIR}/check_sitemap.py"
SCHEMA_SCRIPT="${ORIG_DIR}/check_schema_completeness.py"

# Allow override via env vars for testing
BEV_SCRIPT="${BEV_SCRIPT_OVERRIDE:-$BEV_SCRIPT}"
DET_SCRIPT="${DET_SCRIPT_OVERRIDE:-$DET_SCRIPT}"
ROBOTS_SCRIPT="${ROBOTS_SCRIPT_OVERRIDE:-$ROBOTS_SCRIPT}"
SITEMAP_SCRIPT="${SITEMAP_SCRIPT_OVERRIDE:-$SITEMAP_SCRIPT}"
SCHEMA_SCRIPT="${SCHEMA_SCRIPT_OVERRIDE:-$SCHEMA_SCRIPT}"

# Spawn all 5 children in parallel.
# URL is passed as argv, never interpolated into a shell string.
if [ -f "$BEV_SCRIPT" ]; then
    run_child "bev" bash "$BEV_SCRIPT" "$URL"
else
    echo '{"error":"bev script missing"}' > "${TMP}/bev.out"
    echo 1 > "${TMP}/bev.exit"
fi

if [ -f "$DET_SCRIPT" ]; then
    run_child "det" python3 "$DET_SCRIPT" "$URL"
else
    echo '{"error":"det script missing"}' > "${TMP}/det.out"
    echo 1 > "${TMP}/det.exit"
fi

if [ -f "$ROBOTS_SCRIPT" ]; then
    run_child "robots" python3 "$ROBOTS_SCRIPT" "$URL"
else
    echo '{"error":"robots script missing"}' > "${TMP}/robots.out"
    echo 1 > "${TMP}/robots.exit"
fi

if [ -f "$SITEMAP_SCRIPT" ]; then
    run_child "sitemap" python3 "$SITEMAP_SCRIPT" "$URL"
else
    echo '{"error":"sitemap script missing"}' > "${TMP}/sitemap.out"
    echo 1 > "${TMP}/sitemap.exit"
fi

if [ -f "$SCHEMA_SCRIPT" ]; then
    run_child "schema" python3 "$SCHEMA_SCRIPT" "$URL"
else
    echo '{"error":"schema script missing"}' > "${TMP}/schema.out"
    echo 1 > "${TMP}/schema.exit"
fi

# Wait per-PID so one failure doesn't kill the rest.
for label in bev det robots sitemap schema; do
    pid_var="PID_${label}"
    pid="${!pid_var:-}"
    if [ -n "$pid" ]; then
        wait "$pid" 2>/dev/null || true
    fi
done

# Synthesize combined result via Python (stdin-friendly, no shell-quoting nightmare)
python3 - "$URL" "$MODE" "$TMP" <<'PYEOF'
import json
import os
import sys

url = sys.argv[1]
mode = sys.argv[2]
tmp = sys.argv[3]


def safe_load(label):
    out_path = os.path.join(tmp, f'{label}.out')
    err_path = os.path.join(tmp, f'{label}.err')
    exit_path = os.path.join(tmp, f'{label}.exit')

    exit_code = -1
    if os.path.exists(exit_path):
        try:
            with open(exit_path) as f:
                exit_code = int(f.read().strip() or '-1')
        except Exception:
            pass

    stderr_msg = ''
    if os.path.exists(err_path):
        try:
            with open(err_path) as f:
                stderr_msg = f.read().strip()[:500]
        except Exception:
            pass

    if exit_code == 124:
        return {'_child_status': 'timeout', '_child_exit': 124,
                '_stderr': stderr_msg or 'timeout after 60s'}

    if not os.path.exists(out_path):
        return {'_child_status': 'missing_output', '_child_exit': exit_code,
                '_stderr': stderr_msg}

    try:
        with open(out_path) as f:
            raw = f.read()
        data = json.loads(raw)
        data['_child_status'] = 'ok' if exit_code == 0 else 'nonzero_exit'
        data['_child_exit'] = exit_code
        return data
    except json.JSONDecodeError as e:
        return {
            '_child_status': 'unparseable_output',
            '_child_exit': exit_code,
            '_parse_error': str(e),
            '_stderr': stderr_msg,
        }


bev = safe_load('bev')
det = safe_load('det')
robots = safe_load('robots')
sitemap = safe_load('sitemap')
schema = safe_load('schema')

# Aggregate check results across all 5 scripts
all_checks = {}


def collect(src_name, src_dict):
    if not isinstance(src_dict, dict):
        return
    checks = src_dict.get('checks', {})
    if isinstance(checks, dict):
        for cid, result in checks.items():
            all_checks[f'{src_name}:{cid}'] = result


collect('det_checks', det)
collect('robots', robots)
collect('sitemap', sitemap)
collect('schema', schema)

# Count by status
pass_count = sum(1 for c in all_checks.values() if c.get('status') == 'pass')
fail_count = sum(1 for c in all_checks.values() if c.get('status') == 'fail')
warn_count = sum(1 for c in all_checks.values() if c.get('status') == 'warn')
na_count = sum(1 for c in all_checks.values() if c.get('status') == 'na')

# Collect critical issues + per-child failure reasons
critical_issues = []
if isinstance(bev.get('summary'), dict):
    critical_issues.extend(bev['summary'].get('critical_issues', []))

for cid, result in all_checks.items():
    if result.get('status') == 'fail':
        ev = str(result.get('evidence', ''))[:200]
        critical_issues.append(f'[{cid}] {ev}')

# Per-child health summary (new in v2 — was silent in v1)
child_health = {
    'bev': {'status': bev.get('_child_status', 'unknown'),
            'exit': bev.get('_child_exit')},
    'det': {'status': det.get('_child_status', 'unknown'),
            'exit': det.get('_child_exit')},
    'robots': {'status': robots.get('_child_status', 'unknown'),
               'exit': robots.get('_child_exit')},
    'sitemap': {'status': sitemap.get('_child_status', 'unknown'),
                'exit': sitemap.get('_child_exit')},
    'schema': {'status': schema.get('_child_status', 'unknown'),
               'exit': schema.get('_child_exit')},
}
any_child_failed = any(
    v['status'] not in ('ok',) for v in child_health.values()
)

combined = {
    'url': url,
    'bots_eye_view': bev,
    'deterministic_checks': det,
    'robots_txt_analysis': robots,
    'sitemap_analysis': sitemap,
    'schema_completeness': schema,
    'overall_summary': {
        'classification': bev.get('classification'),
        'total_checks_run': len(all_checks),
        'pass': pass_count,
        'fail': fail_count,
        'warn': warn_count,
        'na': na_count,
        'all_critical_issues': critical_issues,
        'child_health': child_health,
        'any_child_degraded': any_child_failed,
    },
    'all_checks': all_checks,
}

if mode == 'human':
    print('=' * 75)
    print(f'DETERMINISTIC AUDIT (v2) — {url}')
    print('=' * 75)
    print()

    print('CHILD HEALTH')
    print('-' * 75)
    for name, h in child_health.items():
        ic = '✓' if h['status'] == 'ok' else '✗'
        print(f'  {ic} {name}: {h["status"]} (exit {h["exit"]})')
    print()

    print('PHASE 1 — Bot\'s Eye View')
    print('-' * 75)
    if isinstance(bev.get('summary'), dict):
        s = bev['summary']
        dprobe = (bev.get('probes') or {}).get('default', {})
        print(f'Classification: {bev.get("classification")}')
        print(f'HTTP (default UA): {s.get("http_code_default")} '
              f'after {s.get("redirects_followed", 0)} redirect(s) '
              f'→ {s.get("final_url") or bev.get("url")}')
        print(f'First H1: {dprobe.get("h1_first")}')
        print(f'Visible words (raw HTML): {s.get("visible_words_default")}')
        print(f'FAQ visible: {s.get("faq_visible")} pairs — '
              f'in schema: {s.get("faq_schema")} ({s.get("faq_integrity")})')
        print(f'Same HTML as 404: {s.get("same_html_as_404_url")}')
        print(f'Cloaking: {s.get("cloaking_detected")} — '
              f'Bot blocking: {s.get("bot_blocking_detected")}')
    elif '_child_status' in bev:
        print(f'Unavailable: child_status={bev["_child_status"]}')
    print()

    for phase_label, data in [
        ('PHASE 2 — Targeted Deterministic Checks', det),
        ('PHASE 3 — robots.txt Analysis', robots),
        ('PHASE 4 — Sitemap Validity', sitemap),
        ('PHASE 5 — Schema Completeness', schema),
    ]:
        print(phase_label)
        print('-' * 75)
        if not isinstance(data, dict):
            print('  (unavailable)')
        elif data.get('_child_status') not in ('ok', 'nonzero_exit', None):
            print(f'  Unavailable: {data.get("_child_status")}')
        else:
            checks = data.get('checks', {})
            for cid, result in checks.items():
                icons = {'pass': '✓', 'fail': '✗', 'warn': '⚠', 'na': '—'}
                ic = icons.get(result.get('status'), '?')
                ev = str(result.get('evidence', ''))[:150]
                print(f'  {ic} {cid}: {ev}')
        print()

    print('OVERALL SUMMARY')
    print('-' * 75)
    os_dict = combined['overall_summary']
    print(f'Total checks: {os_dict["total_checks_run"]} — '
          f'{os_dict["pass"]} pass, {os_dict["fail"]} fail, '
          f'{os_dict["warn"]} warn, {os_dict["na"]} n/a')
    print(f'Degraded children: {os_dict["any_child_degraded"]}')
    critical = os_dict['all_critical_issues']
    if critical:
        print(f'\nCRITICAL ISSUES ({len(critical)}):')
        for i, issue in enumerate(critical[:15], 1):
            print(f'  {i}. {issue[:200]}')
    else:
        print('No critical issues detected.')
else:
    print(json.dumps(combined, indent=2, ensure_ascii=False))
PYEOF
