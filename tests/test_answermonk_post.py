#!/usr/bin/env python3
"""Test the AnswerMonk sync (persistence.post_to_answermonk) against a
mocked ingest endpoint (stdlib http.server).

Covers:
  - happy path: correct endpoint path, x-auditor-key header, payload mapping
    (required fields + brand_name from classification.company_name, jsonb
    blocks passed through, non-dict findings filtered)
  - findings capped at 500 (receiver's zod schema rejects more)
  - retry: one 500 then success -> posted on attempt 2
  - no retry on 4xx (a client error won't change)
  - unconfigured env -> silent no-op, no request sent
  - unreachable endpoint -> returns posted=False, never raises

Run from the service dir (imports persistence directly):
    cd service && python3 ../tests/test_answermonk_post.py
Prints ANSWERMONK_OK on success, exits non-zero on failure.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'service'))

import persistence

persistence.ANSWERMONK_RETRY_DELAY_S = 0.0  # no sleeps in tests

RECEIVED = []          # (path, headers-dict, body-dict) per request
FAIL_STATUSES = []     # queue of statuses to serve before succeeding


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        RECEIVED.append((self.path, dict(self.headers), json.loads(body)))
        status = FAIL_STATUSES.pop(0) if FAIL_STATUSES else 200
        payload = json.dumps({'ok': status == 200}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep test output clean
        pass


def make_audit(**overrides):
    audit = {
        'audit_id': 'test-audit-123',
        'url': 'https://example.com/',
        'domain': 'example.com',
        'date': '2026-07-07',
        'classification': {'company_name': 'Example GmbH', 'industry': 'saas'},
        'scoring': {'overall_score': 72, 'overall_grade': 'B'},
        'findings': [{'check_id': 'A1', 'status': 'fail'}, 'not-a-dict',
                     {'check_id': 'B2', 'status': 'pass'}],
        'narrative': {'executive_diagnosis': 'fine'},
        'performance': {'ttfb_ms': 120},
        'bots_eye_view': {'ssr': True},
        'gates': {'gate_1': 'pass'},
        'metadata': {'version': 'test'},
    }
    audit.update(overrides)
    return audit


failures = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ok: {name}')
    else:
        failures.append(name)
        print(f'  FAIL: {name} {detail}')


server = HTTPServer(('127.0.0.1', 0), _Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

os.environ['ANSWERMONK_BASE_URL'] = f'http://127.0.0.1:{port}/'  # trailing / on purpose
os.environ['EXTERNAL_AUDITOR_KEY'] = 'secret-key-1'

# --- happy path -------------------------------------------------------------
RECEIVED.clear()
res = persistence.post_to_answermonk(make_audit())
check('posted on first attempt', res.get('posted') is True and res.get('attempts') == 1, res)
path, headers, body = RECEIVED[0]
check('endpoint path', path == '/api/external-audits', path)
check('auth header', headers.get('x-auditor-key') == 'secret-key-1')
check('required fields', body['audit_id'] == 'test-audit-123'
      and body['url'] == 'https://example.com/' and body['domain'] == 'example.com')
check('brand_name from classification', body.get('brand_name') == 'Example GmbH')
check('jsonb blocks mapped', body.get('scoring', {}).get('overall_grade') == 'B'
      and body.get('gates') == {'gate_1': 'pass'}
      and body.get('bots_eye_view') == {'ssr': True}
      and body.get('performance') == {'ttfb_ms': 120}
      and body.get('narrative') == {'executive_diagnosis': 'fine'}
      and body.get('date') == '2026-07-07')
check('non-dict findings filtered', body.get('findings') ==
      [{'check_id': 'A1', 'status': 'fail'}, {'check_id': 'B2', 'status': 'pass'}])

# --- findings capped at 500 --------------------------------------------------
RECEIVED.clear()
big = make_audit(findings=[{'check_id': f'C{i}'} for i in range(600)])
persistence.post_to_answermonk(big)
check('findings capped at 500', len(RECEIVED[0][2]['findings']) == 500)

# --- retry: 500 then success -------------------------------------------------
RECEIVED.clear()
FAIL_STATUSES[:] = [500]
res = persistence.post_to_answermonk(make_audit())
check('retries after 5xx', res.get('posted') is True and res.get('attempts') == 2, res)
check('two requests sent', len(RECEIVED) == 2, len(RECEIVED))

# --- no retry on 4xx ----------------------------------------------------------
RECEIVED.clear()
FAIL_STATUSES[:] = [400, 400]
res = persistence.post_to_answermonk(make_audit())
check('4xx not retried, reported', res.get('posted') is False
      and len(RECEIVED) == 1, (res, len(RECEIVED)))
FAIL_STATUSES.clear()

# --- missing required field -> skipped, nothing sent ---------------------------
RECEIVED.clear()
res = persistence.post_to_answermonk(make_audit(url=None))
check('missing url skipped', res.get('posted') is False and not RECEIVED, res)

# --- unconfigured env -> no-op -------------------------------------------------
del os.environ['ANSWERMONK_BASE_URL']
res = persistence.post_to_answermonk(make_audit())
check('unconfigured is a no-op', res.get('posted') is False and 'note' in res, res)

# --- unreachable endpoint -> best-effort failure, no exception ------------------
server.shutdown()
os.environ['ANSWERMONK_BASE_URL'] = f'http://127.0.0.1:{port}'
res = persistence.post_to_answermonk(make_audit())
check('unreachable endpoint never raises', res.get('posted') is False
      and res.get('error'), res)

if failures:
    print(f'{len(failures)} FAILED: {failures}')
    sys.exit(1)
print('ANSWERMONK_OK payload mapped, header sent, 5xx retried once, '
      '4xx/unconfigured/unreachable degrade safely')
