"""
brain_console.py — the Sieve Brain Console: full visibility into the ruleset
library and the source registry, plus no-CLI source management.

Everything an operator previously needed `python -m sieve_ingest ...` for:
  GET  /brain                      the console page (auth)
  GET  /api/brain/overview         corpus counts, freshness, runs, domains
  GET  /api/brain/sources          registry + health + per-source rule counts
  GET  /api/brain/rules            searchable/paginated browse (3 tables)
  GET  /api/brain/changes          recent ingest activity feed
  POST /api/brain/sources/probe    dry-run pre-flight ("deep crawl suggested")
  POST /api/brain/sources          register a new source (crawls next cycle)
  POST /api/brain/sources/{id}/toggle   enable/disable

Writes are deliberately limited to what the sieve-ingest CLI itself does
(insert-only registration + the enabled flag) — the crawl itself still runs in
the sieve-ingest cron, keeping the auditor read-mostly.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

log = logging.getLogger('audit.brain_console')

router = APIRouter()

_UA = {'User-Agent': 'sieve-brain-console/1.0 (+preflight)'}
# per-kind columns: (title, body1, body2, confidence_col, risk_col)
_TABLES = {'rules': ('name', 'if_condition', 'then_logic', 'confidence_score', None),
           'principles': ('title', 'statement', 'explanation', 'confidence_score', None),
           'anti_patterns': ('title', 'description', None, None, 'risk_level')}
_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,48}$')


def _conn():
    import psycopg2
    import sieve_brain
    if not sieve_brain.SIEVE_DB_URL:
        raise HTTPException(503, 'sieve DB not configured (SIEVE_DB_URL)')
    try:
        conn = psycopg2.connect(sieve_brain.SIEVE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        return conn
    except Exception as e:
        raise HTTPException(503, f'sieve DB unreachable: {type(e).__name__}')


# ---------------------------------------------------------------------------
# Read APIs
# ---------------------------------------------------------------------------

@router.get('/api/brain/overview')
def brain_overview():
    import sieve_brain
    out: Dict[str, Any] = {'tables': {}, 'domains': [], 'runs': [], 'orgs': []}
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for t in _TABLES:
                try:
                    # 'citeable' mirrors the retrieval filter exactly (legacy
                    # statuses like 'candidate' are citeable; only the retire
                    # lifecycle removes rows from circulation).
                    cur.execute(f"SELECT count(*), count(embedding), "
                                f"count(*) FILTER (WHERE coalesce(status,'active') "
                                f"NOT IN ('retired','superseded','rejected')) "
                                f"FROM sieve.{t}")
                    n, emb, act = cur.fetchone()
                    out['tables'][t] = {'rows': n, 'embedded': emb, 'active': act}
                except Exception:
                    conn.rollback()
                    cur.execute(f"SELECT count(*) FROM sieve.{t}")
                    out['tables'][t] = {'rows': cur.fetchone()[0], 'embedded': 0,
                                        'active': None}
            cur.execute("SELECT max(last_verified)::date::text, "
                        "EXTRACT(day FROM now()-max(last_verified))::int FROM sieve.rules")
            vt, sd = cur.fetchone()
            out['verified_through'], out['stale_days'] = vt, sd
            cur.execute("SELECT domain_tag, count(*) FROM sieve.rules "
                        "WHERE domain_tag IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8")
            out['domains'] = [{'tag': t_, 'count': n} for t_, n in cur.fetchall()]
            cur.execute("SELECT source_org, count(*), "
                        "count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>'') "
                        "FROM sieve.rules GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
            out['orgs'] = [{'org': o or '(none)', 'rules': n, 'with_url': wu}
                           for o, n, wu in cur.fetchall()]
            try:
                cur.execute("SELECT run_id, started_at::text, status, sources_checked, "
                            "urls_changed, objects_written FROM sieve.ingest_runs "
                            "ORDER BY run_id DESC LIMIT 6")
                out['runs'] = [{'run_id': r, 'started_at': s[:16], 'status': st,
                                'sources': sc, 'urls': u, 'rules': ob}
                               for r, s, st, sc, u, ob in cur.fetchall()]
            except Exception:
                conn.rollback()
        out['live'] = sieve_brain.live_enabled()
        out['cron'] = 'Mondays 06:00 UTC (weekly sieve-ingest cycle)'
        return out
    finally:
        conn.close()


@router.get('/api/brain/sources')
def brain_sources():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.source_id, s.canonical_org, s.adapter_type, s.tier,
                       s.root_url, s.sitemap_url, s.seed_urls, s.url_filter,
                       s.crawl_cadence_days, s.last_crawled_at::text, s.enabled,
                       s.consecutive_failures, s.last_ok_at::text,
                       left(coalesce(s.last_error,''), 160), s.notes,
                       (SELECT count(*) FROM sieve.rules r
                        WHERE r.source_org = s.canonical_org) AS org_rules
                FROM sieve.source_registry s
                ORDER BY s.tier, s.source_id
            """)
            cols = ['source_id', 'canonical_org', 'adapter_type', 'tier', 'root_url',
                    'sitemap_url', 'seed_urls', 'url_filter', 'crawl_cadence_days',
                    'last_crawled_at', 'enabled', 'consecutive_failures',
                    'last_ok_at', 'last_error', 'notes', 'org_rules']
            return {'sources': [dict(zip(cols, r)) for r in cur.fetchall()]}
    finally:
        conn.close()


@router.get('/api/brain/segments')
def brain_segments():
    """The library map: every way to slice the corpus, with counts — domains,
    source orgs (tiered), statuses, types. Each segment is a clickable entry
    point into the filtered browser."""
    import sieve_brain
    out: Dict[str, Any] = {'kinds': [], 'domains': [], 'orgs': [], 'statuses': []}
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for t in _TABLES:
                cur.execute(f"SELECT count(*) FROM sieve.{t}")
                out['kinds'].append({'kind': t, 'count': cur.fetchone()[0]})
            cur.execute("SELECT coalesce(domain_tag,'(untagged)'), count(*) "
                        "FROM sieve.rules GROUP BY 1 ORDER BY 2 DESC")
            out['domains'] = [{'tag': t, 'count': n} for t, n in cur.fetchall()]
            cur.execute("SELECT coalesce(source_org,'(unknown)'), count(*), "
                        "count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>''), "
                        "max(last_verified)::date::text "
                        "FROM sieve.rules GROUP BY 1 ORDER BY 2 DESC LIMIT 24")
            out['orgs'] = [{'org': o, 'count': n, 'with_url': wu, 'verified': v,
                            'tier': sieve_brain.tier_of(o)}
                           for o, n, wu, v in cur.fetchall()]
            cur.execute("SELECT coalesce(status,'active'), count(*) FROM sieve.rules "
                        "GROUP BY 1 ORDER BY 2 DESC")
            out['statuses'] = [{'status': s, 'count': n} for s, n in cur.fetchall()]
        return out
    finally:
        conn.close()


@router.get('/api/brain/rules')
def brain_rules(q: str = '', kind: str = 'rules', org: str = '', domain: str = '',
                status_f: str = '', offset: int = 0):
    if kind not in _TABLES:
        raise HTTPException(400, f'kind must be one of {list(_TABLES)}')
    title, t1, t2, confcol, riskcol = _TABLES[kind]
    offset = max(0, min(offset, 100000))
    where, params = ['TRUE'], []
    if q:
        like = f'%{q}%'
        parts = [f"{title} ILIKE %s", f"{t1} ILIKE %s"]
        params += [like, like]
        if t2:
            parts.append(f"{t2} ILIKE %s")
            params.append(like)
        where.append('(' + ' OR '.join(parts) + ')')
    if org:
        where.append('source_org = %s'); params.append(org)
    if domain:
        where.append('domain_tag = %s'); params.append(domain)
    if status_f:
        where.append("coalesce(status,'active') = %s"); params.append(status_f)
    t2sel = t2 or "''"
    conf = confcol or "NULL"          # anti_patterns has no confidence column
    risk = riskcol or "NULL"
    conn = _conn()
    try:
        with conn.cursor() as cur:
            # optional deep-dive columns (arrive via ingest migrations)
            import sieve_brain
            have = sieve_brain._optional_cols(conn).get(kind, set())
            prov = 'url_provenance' if 'url_provenance' in have else 'NULL'
            cur.execute(f"SELECT count(*) FROM sieve.{kind} WHERE {' AND '.join(where)}",
                        params)
            total = cur.fetchone()[0]
            cur.execute(f"""
                SELECT id, {title} AS title, {t1} AS body1, {t2sel} AS body2,
                       domain_tag, source_org, source_url,
                       coalesce(status,'active'), last_verified::date::text,
                       {conf} AS confidence, {risk} AS risk,
                       {prov} AS url_provenance, document_id,
                       extracted_at::date::text, created_at::text
                FROM sieve.{kind} WHERE {' AND '.join(where)}
                ORDER BY last_verified DESC NULLS LAST, id DESC
                LIMIT 25 OFFSET %s
            """, params + [offset])
            cols = ['id', 'title', 'body1', 'body2', 'domain_tag', 'source_org',
                    'source_url', 'status', 'last_verified', 'confidence', 'risk',
                    'url_provenance', 'document_id', 'extracted_at', 'created_at']
            return {'total': total, 'offset': offset,
                    'items': [dict(zip(cols, r)) for r in cur.fetchall()]}
    finally:
        conn.close()


@router.get('/api/brain/changes')
def brain_changes():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT detected_at::text, source_id, change_type, signal,
                           coalesce(extract_status,'detected'), coalesce(rules_new,0), url
                    FROM sieve.ingest_changes ORDER BY change_id DESC LIMIT 60
                """)
                cols = ['at', 'source_id', 'change_type', 'signal', 'outcome',
                        'rules_new', 'url']
                return {'changes': [dict(zip(cols, (r[0][:16], *r[1:]))) for r in cur.fetchall()]}
            except Exception:
                conn.rollback()
                return {'changes': []}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Probe + write APIs
# ---------------------------------------------------------------------------

class SourceSpec(BaseModel):
    source_id: str = Field(min_length=2, max_length=48)
    canonical_org: str = Field(min_length=1, max_length=120)
    root_url: str = Field(min_length=8, max_length=500)
    adapter_type: str = 'sitemap'
    sitemap_url: Optional[str] = None
    seed_urls: Optional[List[str]] = None
    url_filter: Optional[str] = None
    tier: int = Field(default=3, ge=1, le=5)
    crawl_cadence_days: int = Field(default=30, ge=1, le=120)
    notes: Optional[str] = None


_SKIP_EXT = re.compile(r'\.(pdf|jpg|jpeg|png|gif|svg|webp|mp4|mp3|zip|gz|css|js|xml)([?#]|$)', re.I)


def _probe_spec(spec: SourceSpec) -> Dict[str, Any]:
    """Dry-run pre-flight: what WOULD a crawl of this source process? No DB
    writes, no LLM. This is the 'deep crawl suggested' evidence."""
    warnings: List[str] = []
    timeout = httpx.Timeout(20, connect=10)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=_UA) as client:
            if spec.adapter_type == 'url_list':
                seeds = (spec.seed_urls or [])[:10]
                if not seeds:
                    return {'ok': False, 'error': 'url_list needs seed_urls'}
                pages = []
                for u in seeds:
                    try:
                        r = client.get(u)
                        text_len = len(re.sub(r'<[^>]+>', ' ', r.text)) if r.status_code == 200 else 0
                        pages.append({'url': u, 'status': r.status_code, 'text_chars': text_len})
                        if r.status_code != 200:
                            warnings.append(f'{u} returned {r.status_code}')
                        elif text_len < 500:
                            warnings.append(f'{u} has very little text ({text_len} chars) — JS shell?')
                    except Exception as e:
                        pages.append({'url': u, 'status': None, 'text_chars': 0})
                        warnings.append(f'{u}: {type(e).__name__}')
                ok = any(p['status'] == 200 and p['text_chars'] > 500 for p in pages)
                return {'ok': ok, 'mode': 'url_list', 'pages': pages, 'warnings': warnings,
                        'crawl_estimate': sum(1 for p in pages
                                              if p['status'] == 200 and p['text_chars'] > 500)}
            # sitemap mode
            sm = spec.sitemap_url
            if not sm:
                return {'ok': False, 'error': 'sitemap adapter needs sitemap_url'}
            r = client.get(sm)
            if r.status_code != 200:
                return {'ok': False, 'error': f'sitemap returned {r.status_code}'}
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(r.text)
            except Exception:
                return {'ok': False, 'error': 'sitemap is not parseable XML'}
            ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
            children = [c.text for c in root.findall('.//sm:sitemap/sm:loc', ns) if c.text]
            locs: List[str] = []
            if children:
                for child in children[:3]:
                    try:
                        cr = client.get(child)
                        if cr.status_code == 200:
                            croot = ET.fromstring(cr.text)
                            locs += [u.text for u in croot.findall('.//sm:url/sm:loc', ns)
                                     if u.text]
                    except Exception:
                        warnings.append(f'child sitemap unreadable: {child}')
                if len(children) > 3:
                    warnings.append(f'index has {len(children)} child sitemaps; probed first 3')
            else:
                locs = [u.text for u in root.findall('.//sm:url/sm:loc', ns) if u.text]
            total_raw = len(locs)
            flt = None
            if spec.url_filter:
                try:
                    flt = re.compile(spec.url_filter)
                except re.error:
                    warnings.append('url_filter is not a valid regex — ignored in probe')
            kept = []
            from urllib.parse import urlsplit
            for u in locs:
                path = urlsplit(u).path
                if _SKIP_EXT.search(u):
                    continue
                if flt and not flt.search(path):
                    continue
                kept.append(u)
            if not kept:
                warnings.append('0 URLs pass the filter — check url_filter')
            return {'ok': bool(kept), 'mode': 'sitemap',
                    'sitemap_children': len(children),
                    'urls_found': total_raw, 'urls_after_filter': len(kept),
                    'sample': kept[:8], 'warnings': warnings,
                    'crawl_estimate': min(len(kept), 20),
                    'note': 'crawler processes up to 20 pages/cycle, rotating '
                            'through the rest on later cycles; its own hygiene '
                            'gate (chrome/junk/locale filters + relevance '
                            'screen) drops more than this preview shows'}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


@router.post('/api/brain/sources/probe')
def brain_probe(spec: SourceSpec):
    return _probe_spec(spec)


@router.post('/api/brain/sources')
def brain_add_source(spec: SourceSpec):
    if not _ID_RE.match(spec.source_id):
        raise HTTPException(400, 'source_id must be a lowercase slug (a-z, 0-9, -)')
    if spec.adapter_type not in ('sitemap', 'url_list', 'github_release'):
        raise HTTPException(400, 'adapter_type must be sitemap | url_list | github_release')
    if not spec.root_url.startswith(('http://', 'https://')):
        raise HTTPException(400, 'root_url must be an http(s) URL')
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM sieve.source_registry WHERE source_id=%s",
                        (spec.source_id,))
            if cur.fetchone():
                raise HTTPException(409, f'{spec.source_id} already exists')
            cur.execute("""
                INSERT INTO sieve.source_registry
                    (source_id, canonical_org, adapter_type, tier, root_url,
                     sitemap_url, seed_urls, url_filter, crawl_cadence_days,
                     enabled, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s)
            """, (spec.source_id, spec.canonical_org, spec.adapter_type, spec.tier,
                  spec.root_url, spec.sitemap_url,
                  json.dumps(spec.seed_urls) if spec.seed_urls else None,
                  spec.url_filter, spec.crawl_cadence_days,
                  (spec.notes or 'added via Brain Console')))
        log.info('brain console: source %s registered', spec.source_id)
        return {'ok': True, 'source_id': spec.source_id,
                'message': 'Registered and immediately due — the next weekly '
                           'cycle (Mon 06:00 UTC) begins its deep crawl.'}
    finally:
        conn.close()


@router.post('/api/brain/sources/{source_id}/toggle')
def brain_toggle(source_id: str):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE sieve.source_registry SET enabled = NOT enabled "
                        "WHERE source_id=%s RETURNING enabled", (source_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f'no such source {source_id}')
            return {'ok': True, 'source_id': source_id, 'enabled': row[0]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The console page (Luminous tokens, matches the auditor's report styling)
# ---------------------------------------------------------------------------

BRAIN_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sieve Brain Console</title>
<script>
  const saved = localStorage.getItem('aeo-theme');
  document.documentElement.dataset.theme = saved ||
    (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
</script>
<style>
:root{--brand:#6366f1;--brand-ink:#4f46e5;--teal:#14b8a6;--bg:#f7f9fc;--card:#ffffff;
--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;
--shadow:0 1px 2px rgba(15,23,42,.06),0 8px 24px rgba(15,23,42,.07)}
:root[data-theme=dark]{--bg:#0b1020;--card:#111731;--ink:#e2e8f0;--mut:#94a3b8;
--line:#26304f;--brand-ink:#a5b4fc;--shadow:0 1px 2px rgba(0,0,0,.4)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 20px 80px}
h1{font-size:22px;margin:6px 0 2px}h2{font-size:16px;margin:26px 0 10px}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 16px;box-shadow:var(--shadow)}
.card .k{font-size:12px;color:var(--mut)}.card .v{font-size:22px;font-weight:700;margin-top:2px}
.card .n{font-size:11px;color:var(--mut);margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}
th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);
text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13px}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;font-size:11px;font-weight:600;border-radius:999px;padding:1px 9px}
.b-ok{background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)}
.b-warn{background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn)}
.b-bad{background:color-mix(in srgb,var(--bad) 14%,transparent);color:var(--bad)}
.b-mut{background:color-mix(in srgb,var(--mut) 14%,transparent);color:var(--mut)}
.tabs{display:flex;gap:6px;margin:18px 0 14px;flex-wrap:wrap}
.tab{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;
padding:6px 16px;cursor:pointer;font-size:13px;font-weight:600}
.tab.on{background:var(--brand);border-color:var(--brand);color:#fff}
input,select,textarea{background:var(--card);color:var(--ink);border:1px solid var(--line);
border-radius:9px;padding:8px 10px;font:inherit;width:100%}
textarea{min-height:70px;font-family:ui-monospace,monospace;font-size:12px}
label{font-size:12px;color:var(--mut);display:block;margin:10px 0 4px}
.btn{background:var(--brand);color:#fff;border:none;border-radius:9px;padding:9px 18px;
font-weight:600;cursor:pointer;font-size:13px}.btn[disabled]{opacity:.5;cursor:default}
.btn.ghost{background:transparent;color:var(--brand-ink);border:1px solid var(--line)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
.probe{border-left:3px solid var(--teal);background:color-mix(in srgb,var(--teal) 7%,transparent);
border-radius:0 10px 10px 0;padding:12px 14px;margin:14px 0;font-size:13px}
.probe.bad{border-color:var(--bad);background:color-mix(in srgb,var(--bad) 7%,transparent)}
a{color:var(--brand-ink);text-decoration:none}a:hover{text-decoration:underline}
.rule-if{color:var(--mut)}.mono{font-family:ui-monospace,monospace;font-size:12px}
.toolbar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.toolbar>*{width:auto;flex:0 1 auto}
#themeBtn{position:fixed;right:18px;top:16px;border-radius:999px;border:1px solid var(--line);
background:var(--card);width:36px;height:36px;cursor:pointer;color:var(--ink)}
.section{display:none}.section.on{display:block}
.load{color:var(--mut);padding:20px;text-align:center}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);
background:var(--card);color:var(--ink);border-radius:10px;padding:8px 13px;cursor:pointer;
font:inherit;font-size:13px;box-shadow:var(--shadow)}
.chip:hover{border-color:var(--brand)}.chip b{color:var(--brand-ink)}
.rrow{cursor:pointer}.rrow:hover td{background:color-mix(in srgb,var(--brand) 4%,transparent)}
.detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
gap:12px;padding:6px 4px 10px;font-size:13px}
</style></head><body>
<button id="themeBtn" title="theme">◐</button>
<div class="wrap">
<h1>Sieve Brain Console</h1>
<div class="sub">The living ruleset library: what we know, where it came from, how fresh it is — and where new sources enter.</div>
<div class="cards" id="ov"></div>
<div class="tabs">
  <button class="tab on" data-s="map">Library</button>
  <button class="tab" data-s="sources">Sources</button>
  <button class="tab" data-s="add">Add a source</button>
  <button class="tab" data-s="browse">Browse rules</button>
  <button class="tab" data-s="activity">Activity</button>
</div>
<div class="section on" id="s-map">
  <div class="sub">Every segment is clickable — drill into the filtered rules behind any number.</div>
  <div id="segMap" class="load">loading library map…</div>
</div>
<div class="section" id="s-sources"><div class="load">loading sources…</div></div>
<div class="section" id="s-add">
  <h2>Register a new source</h2>
  <div class="sub">Probe first — the console shows exactly what a deep crawl would process before anything is registered or any token is spent.</div>
  <div class="grid2">
    <div><label>Source id (slug)</label><input id="f-id" placeholder="e.g. searchpilot-blog"></div>
    <div><label>Organization (canonical name)</label><input id="f-org" placeholder="e.g. SearchPilot"></div>
    <div><label>Root URL</label><input id="f-root" placeholder="https://…"></div>
    <div><label>Adapter</label><select id="f-adapter">
      <option value="sitemap">sitemap — discover pages from sitemap.xml</option>
      <option value="url_list">url_list — exact pages I specify</option>
      <option value="github_release">github_release — watch release notes</option></select></div>
    <div id="f-sm-w"><label>Sitemap URL</label><input id="f-sm" placeholder="https://…/sitemap.xml"></div>
    <div><label>Authority tier (1 = first-party primary … 5)</label><select id="f-tier">
      <option>1</option><option>2</option><option selected>3</option><option>4</option><option>5</option></select></div>
    <div id="f-seeds-w" style="display:none"><label>Seed URLs (one per line)</label><textarea id="f-seeds"></textarea></div>
    <div><label>URL path filter (optional regex, e.g. ^/blog/)</label><input id="f-filter"></div>
    <div><label>Re-crawl cadence (days)</label><input id="f-cad" type="number" value="30" min="1" max="120"></div>
  </div>
  <div style="margin-top:16px;display:flex;gap:10px">
    <button class="btn ghost" id="btnProbe">1 · Probe (dry run)</button>
    <button class="btn" id="btnAdd" disabled>2 · Register — crawl begins next cycle</button>
  </div>
  <div id="probeOut"></div>
</div>
<div class="section" id="s-browse">
  <div class="toolbar">
    <input id="q" placeholder="search rules…" style="flex:1 1 220px">
    <select id="q-kind"><option value="rules">rules</option>
      <option value="principles">principles</option><option value="anti_patterns">anti-patterns</option></select>
    <select id="q-domain"><option value="">all domains</option></select>
    <select id="q-org"><option value="">all sources</option></select>
    <select id="q-status"><option value="">any status</option><option>active</option>
      <option>candidate</option><option>retired</option></select>
    <button class="btn ghost" id="qGo">Search</button>
  </div>
  <div id="rules"></div>
  <div style="margin-top:10px"><button class="btn ghost" id="qMore" style="display:none">Load more</button></div>
</div>
<div class="section" id="s-activity"><div class="load">loading activity…</div></div>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x===t));
  document.querySelectorAll('.section').forEach(x=>x.classList.toggle('on',x.id==='s-'+t.dataset.s));
});
$('#themeBtn').onclick=()=>{const r=document.documentElement;
  r.dataset.theme=r.dataset.theme==='dark'?'light':'dark';
  localStorage.setItem('aeo-theme',r.dataset.theme)};
async function j(url,opts){const r=await fetch(url,opts);
  if(!r.ok){let d='';try{d=(await r.json()).detail||''}catch(e){}
    throw new Error(d||('HTTP '+r.status))}return r.json()}

async function loadOverview(){
  try{const o=await j('/api/brain/overview');
    const t=o.tables||{};const cards=[];
    const fmt=n=>n==null?'—':Number(n).toLocaleString();
    cards.push({k:'Rules',v:fmt(t.rules?.rows),n:fmt(t.rules?.active)+' citeable · '+fmt(t.rules?.embedded)+' embedded'});
    cards.push({k:'Principles',v:fmt(t.principles?.rows),n:'distilled guidance'});
    cards.push({k:'Anti-patterns',v:fmt(t.anti_patterns?.rows),n:'what not to do'});
    cards.push({k:'Verified through',v:esc(o.verified_through||'—'),n:(o.stale_days??'?')+' day(s) old'});
    const lr=(o.runs||[])[0];
    cards.push({k:'Last ingest run',v:lr?('#'+lr.run_id+' '+esc(lr.status)):'—',
                n:lr?esc(lr.started_at)+' · +'+lr.rules+' rules':''});
    cards.push({k:'Next crawl',v:'Mon 06:00 UTC',n:'weekly cycle'});
    $('#ov').innerHTML=cards.map(c=>'<div class="card"><div class="k">'+c.k+
      '</div><div class="v">'+c.v+'</div><div class="n">'+c.n+'</div></div>').join('');
    const dsel=$('#q-domain');
    (o.domains||[]).forEach(d=>{const op=document.createElement('option');
      op.value=d.tag;op.textContent=d.tag+' ('+d.count+')';dsel.appendChild(op)});
  }catch(e){$('#ov').innerHTML='<div class="card"><div class="k">error</div><div class="v" style="font-size:14px">'+esc(e.message)+'</div></div>'}
}
async function loadSources(){
  const el=$('#s-sources');
  try{const d=await j('/api/brain/sources');
    let h='<table><thead><tr><th></th><th>Source</th><th>Adapter</th><th>Tier</th>'+
      '<th>Cadence</th><th>Last crawl</th><th>Health</th><th>Library rules</th><th></th></tr></thead><tbody>';
    for(const s of d.sources){
      const health=s.consecutive_failures>=3?'<span class="badge b-bad">'+s.consecutive_failures+' fails</span>'
        :s.consecutive_failures>0?'<span class="badge b-warn">'+s.consecutive_failures+' fail</span>'
        :'<span class="badge b-ok">ok</span>';
      h+='<tr><td>'+(s.enabled?'🟢':'⚪')+'</td>'+
        '<td><b>'+esc(s.source_id)+'</b><br><span class="mono">'+esc(s.canonical_org)+'</span>'+
        (s.last_error?'<br><span class="rule-if">'+esc(s.last_error)+'</span>':'')+'</td>'+
        '<td>'+esc(s.adapter_type)+(s.url_filter?'<br><span class="mono rule-if">'+esc(s.url_filter)+'</span>':'')+'</td>'+
        '<td>T'+s.tier+'</td><td>'+s.crawl_cadence_days+'d</td>'+
        '<td>'+esc((s.last_crawled_at||'never').slice(0,16))+'</td>'+
        '<td>'+health+'</td><td>'+Number(s.org_rules).toLocaleString()+'</td>'+
        '<td><button class="btn ghost" style="padding:4px 12px" onclick="tgl(\''+esc(s.source_id)+'\')">'+
        (s.enabled?'disable':'enable')+'</button></td></tr>';
    }
    el.innerHTML=h+'</tbody></table>';
  }catch(e){el.innerHTML='<div class="load">'+esc(e.message)+'</div>'}
}
window.tgl=async id=>{try{await j('/api/brain/sources/'+encodeURIComponent(id)+'/toggle',{method:'POST'});loadSources()}catch(e){alert(e.message)}};

$('#f-adapter').onchange=e=>{const v=e.target.value;
  $('#f-sm-w').style.display=v==='sitemap'?'':'none';
  $('#f-seeds-w').style.display=v==='url_list'?'':'none'};
function spec(){const seeds=$('#f-seeds').value.trim();
  return{source_id:$('#f-id').value.trim(),canonical_org:$('#f-org').value.trim(),
    root_url:$('#f-root').value.trim(),adapter_type:$('#f-adapter').value,
    sitemap_url:$('#f-sm').value.trim()||null,
    seed_urls:seeds?seeds.split('\n').map(s=>s.trim()).filter(Boolean):null,
    url_filter:$('#f-filter').value.trim()||null,
    tier:parseInt($('#f-tier').value),crawl_cadence_days:parseInt($('#f-cad').value)||30}}
$('#btnProbe').onclick=async()=>{
  const out=$('#probeOut');out.innerHTML='<div class="load">probing…</div>';
  $('#btnAdd').disabled=true;
  try{const p=await j('/api/brain/sources/probe',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(spec())});
    if(!p.ok){out.innerHTML='<div class="probe bad"><b>Probe failed:</b> '+esc(p.error||'no crawlable pages')+
      (p.warnings?.length?'<br>'+p.warnings.map(esc).join('<br>'):'')+'</div>';return}
    let h='<div class="probe"><b>Deep crawl suggested ✓</b><br>';
    if(p.mode==='sitemap'){h+=esc(p.urls_found)+' URLs in sitemap'+
      (p.sitemap_children?' ('+p.sitemap_children+' child sitemaps)':'')+
      ' → <b>'+esc(p.urls_after_filter)+'</b> pass filters. ~'+esc(p.crawl_estimate)+
      ' pages/cycle, rotating until covered.<br><span class="mono">'+
      (p.sample||[]).map(esc).join('<br>')+'</span>'}
    else{h+=(p.pages||[]).map(pg=>esc(pg.url)+' — '+pg.status+' ('+pg.text_chars+' chars)').join('<br>')}
    if(p.warnings?.length)h+='<br><b>Notes:</b> '+p.warnings.map(esc).join(' · ');
    h+='</div>';out.innerHTML=h;$('#btnAdd').disabled=false;
  }catch(e){out.innerHTML='<div class="probe bad">'+esc(e.message)+'</div>'}};
$('#btnAdd').onclick=async()=>{
  try{const r=await j('/api/brain/sources',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(spec())});
    $('#probeOut').innerHTML='<div class="probe"><b>Registered ✓</b> '+esc(r.message)+'</div>';
    $('#btnAdd').disabled=true;loadSources();
  }catch(e){$('#probeOut').innerHTML='<div class="probe bad">'+esc(e.message)+'</div>'}};

async function loadSegments(){
  try{const s=await j('/api/brain/segments');
    const kindLabel={rules:'Rules — testable if/then directives',
      principles:'Principles — the "why" behind them',
      anti_patterns:'Anti-patterns — what not to do'};
    const chip=(label,count,extra,click)=>'<button class="chip" onclick="'+click+'">'+
      '<span>'+label+'</span><b>'+Number(count).toLocaleString()+'</b>'+(extra||'')+'</button>';
    let h='<h2>By type</h2><div class="chips">';
    for(const k of s.kinds)h+=chip(esc(kindLabel[k.kind]||k.kind),k.count,'',
      "goBrowse({kind:'"+esc(k.kind)+"'})");
    h+='</div><h2>By domain</h2><div class="chips">';
    for(const d of s.domains)h+=chip(esc(d.tag),d.count,'',
      "goBrowse({domain:'"+esc(d.tag)+"'})");
    h+='</div><h2>By source (authority-tiered)</h2><div class="chips">';
    for(const o of s.orgs)h+=chip(esc(o.org),o.count,
      ' <span class="badge b-mut">T'+o.tier+'</span>'+
      (o.verified?' <span class="rule-if">'+esc(o.verified)+'</span>':''),
      "goBrowse({org:'"+esc(o.org).replace(/'/g,"\\'")+"'})");
    h+='</div><h2>By lifecycle status</h2><div class="chips">';
    for(const st of s.statuses)h+=chip(esc(st.status),st.count,'',
      "goBrowse({status:'"+esc(st.status)+"'})");
    h+='</div>';
    $('#segMap').innerHTML=h;
    const osel=$('#q-org');
    s.orgs.forEach(o=>{const op=document.createElement('option');
      op.value=o.org;op.textContent=o.org+' ('+o.count+')';osel.appendChild(op)});
  }catch(e){$('#segMap').innerHTML='<div class="load">'+esc(e.message)+'</div>'}}
window.goBrowse=f=>{
  if(f.kind)$('#q-kind').value=f.kind;
  if(f.domain)$('#q-domain').value=f.domain==='(untagged)'?'':f.domain;
  if(f.org)$('#q-org').value=f.org==='(unknown)'?'':f.org;
  if(f.status)$('#q-status').value=f.status;
  document.querySelector('.tab[data-s="browse"]').click();
  loadRules(false)};

let qOffset=0,qItems=[];
function detailRow(r){
  const kv=(k,v)=>v?'<div><span class="rule-if">'+k+'</span><br>'+v+'</div>':'';
  return '<div class="detail-grid">'+
    kv('IF (condition)',esc(r.body1||'—'))+
    kv('THEN (action)',esc(r.body2||''))+
    kv('Source',esc(r.source_org||'—')+(r.source_url
      ?' — <a target="_blank" rel="noopener" href="'+esc(r.source_url)+'">'+esc(r.source_url)+'</a>':''))+
    kv('Link provenance',esc(r.url_provenance||'legacy import'))+
    kv('Domain',esc(r.domain_tag||'—'))+
    kv('Confidence',esc(r.confidence||''))+kv('Risk',esc(r.risk||''))+
    kv('Status',esc(r.status))+
    kv('Verified',esc(r.last_verified||'never'))+
    kv('Extracted',esc(r.extracted_at||'—'))+
    kv('Ids',(r.id!=null?'#'+esc(r.id):'')+(r.document_id?' · doc '+esc(r.document_id):''))+
    '</div>';}
window.toggleDetail=i=>{
  const el=document.getElementById('det-'+i);
  if(el.style.display==='none'){el.style.display='';el.innerHTML='<td colspan="4">'+detailRow(qItems[i])+'</td>'}
  else el.style.display='none'};
async function loadRules(more){
  if(!more){qOffset=0;qItems=[]}
  const p=new URLSearchParams({q:$('#q').value.trim(),kind:$('#q-kind').value,
    domain:$('#q-domain').value,org:$('#q-org').value,
    status_f:$('#q-status').value,offset:qOffset});
  try{const d=await j('/api/brain/rules?'+p);
    let h=more?$('#rules').dataset.rows||'':'';
    for(const r of d.items){
      const i=qItems.length;qItems.push(r);
      h+='<tr class="rrow" onclick="toggleDetail('+i+')"><td><b>'+esc(r.title)+'</b>'+
        '<div class="rule-if">IF '+esc((r.body1||'').slice(0,160))+'</div>'+
        (r.body2?'<div>THEN '+esc(r.body2.slice(0,160))+'</div>':'')+'</td>'+
        '<td>'+esc(r.domain_tag||'—')+'</td>'+
        '<td>'+esc(r.source_org||'—')+(r.source_url?'<br><a target="_blank" rel="noopener" onclick="event.stopPropagation()" href="'+
          esc(r.source_url)+'">source ↗</a>':'')+'</td>'+
        '<td>'+(r.status==='active'?'<span class="badge b-ok">active</span>'
          :'<span class="badge b-mut">'+esc(r.status)+'</span>')+
        (r.last_verified?'<br><span class="rule-if">'+esc(r.last_verified)+'</span>':'')+'</td></tr>'+
        '<tr id="det-'+i+'" style="display:none"></tr>';
    }
    $('#rules').dataset.rows=h;
    $('#rules').innerHTML='<div class="sub">'+Number(d.total).toLocaleString()+
      ' matching — click a row for the deep dive</div><table><thead><tr><th>Rule</th><th>Domain</th><th>Source</th>'+
      '<th>Status</th></tr></thead><tbody>'+h+'</tbody></table>';
    qOffset=d.offset+25;
    $('#qMore').style.display=qOffset<d.total?'':'none';
  }catch(e){$('#rules').innerHTML='<div class="load">'+esc(e.message)+'</div>'}}
$('#qGo').onclick=()=>loadRules(false);
$('#q').addEventListener('keydown',e=>{if(e.key==='Enter')loadRules(false)});
$('#qMore').onclick=()=>loadRules(true);

async function loadActivity(){
  const el=$('#s-activity');
  try{const d=await j('/api/brain/changes');
    if(!d.changes.length){el.innerHTML='<div class="load">no ingest activity recorded yet</div>';return}
    let h='<table><thead><tr><th>When</th><th>Source</th><th>Change</th><th>Outcome</th><th>URL</th></tr></thead><tbody>';
    for(const c of d.changes){
      const b=c.outcome==='extracted'?'b-ok':c.outcome==='failed'?'b-bad'
        :c.outcome==='irrelevant'||c.outcome==='empty'?'b-mut':'b-warn';
      h+='<tr><td class="mono">'+esc(c.at)+'</td><td>'+esc(c.source_id)+'</td>'+
        '<td>'+esc(c.change_type)+'</td><td><span class="badge '+b+'">'+esc(c.outcome)+
        (c.rules_new?' +'+c.rules_new:'')+'</span></td>'+
        '<td class="mono" style="word-break:break-all">'+esc((c.url||'').slice(0,90))+'</td></tr>';
    }
    el.innerHTML=h+'</tbody></table>';
  }catch(e){el.innerHTML='<div class="load">'+esc(e.message)+'</div>'}}

loadOverview();loadSegments();loadSources();loadRules(false);loadActivity();
</script></body></html>"""


@router.get('/brain', response_class=HTMLResponse)
def brain_page():
    return HTMLResponse(BRAIN_HTML)
