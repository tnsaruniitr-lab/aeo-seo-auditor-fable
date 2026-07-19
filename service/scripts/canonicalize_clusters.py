"""
canonicalize_clusters.py — Lane C: cluster near-duplicate brain rows and elect
ONE canonical citeable per cluster (6-13 near-duplicate rows scattering
retrieval = 41% of measured recall misses).

ANALYSIS + EXECUTION tool in the prodops shape: the agent builds and reviews
the READ-ONLY plan; prod WRITES are user-run by design (the auto-mode
classifier blocks agents from wielding prod credentials — see
docs/HANDOVER-2026-07-19-evidence-loop.md §6).

RUNBOOK (user terminal):
    TOKEN=$(security find-generic-password -s railway.app -a qisto -w)

    # 1) READ-ONLY plan (connects via DATABASE_PUBLIC_URL fetched with the
    #    token; nothing is written, no secret is printed):
    python3 service/scripts/canonicalize_clusters.py "$TOKEN" plan \
        --out /tmp/canon-plan.json          # add --limit 25 for a pilot
                                            # (largest clusters first)

    # 2) review /tmp/canon-plan.json, then EXECUTE (double-gated):
    SIEVE_WRITE_OK=1 python3 service/scripts/canonicalize_clusters.py \
        "$TOKEN" apply --plan /tmp/canon-plan.json --apply

    # local/dev: pass '-' as the token to use SIEVE_DB_URL / DATABASE_URL.
    # selftest (no DB, no network): python3 canonicalize_clusters.py --selftest

WHAT IT DOES
  plan  — pulls CITEABLE rows (mirrors sieve_brain._trust_filter: status not
          in deprecated/rejected/retired/superseded AND superseded_by IS NULL)
          for each kind (table), blocks them by (kind, canonical topic =
          normalized domain_tag), clusters near-duplicates:
            * rows sharing a non-empty rule_key (when the column exists)
              cluster together directly;
            * rows WITHOUT a rule_key cluster by name-similarity
              (normalized token Jaccard >= 0.6) AND embedding cosine >= 0.92
              when BOTH rows have embeddings (missing embeddings -> Jaccard
              alone decides; playbooks have no embedding column).
          Per cluster it elects ONE canonical — best of:
            tier ASC (sieve_brain.tier_of on source_org — retrieval parity),
            has source_url first, url_provenance method 'extracted' first,
            last_verified newest first, confidence DESC, id ASC (numeric-as-
            text aware). Everything else goes on the demote list with its
            PRIOR status recorded (full reversibility).
  apply — executes a reviewed plan file transactionally with per-batch
          commits. Demote = status='superseded' + superseded_by=<canonical id>
          — REUSES the existing supersession semantics that
          sieve_brain._trust_filter already excludes; no new status invented.
          Guarded per row (only flips rows still in their planned prior
          state; drifted rows are skipped and counted). Records the reversal
          tag in a sieve ledger table when one exists, else prints the exact
          reversal UPDATEs. Requires BOTH --apply and env SIEVE_WRITE_OK=1.

SAFETY
  * Never deletes — status flips only, every one reversible via the plan's
    recorded prior state + the tag ('cluster-canon-2026-07-19').
  * sieve ids are TEXT and COLLIDE across kinds — everything is keyed
    (kind, id); clustering never crosses kinds; superseded_by is a same-table
    reference.
  * Deterministic: stable sort keys everywhere, ties broken by existing sort
    order then id — the same corpus always yields the byte-identical plan.
  * No LLM calls, no OpenAI — pure SQL + stdlib math (psycopg2 only for DB).
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..')))

# Retrieval-parity org tiering + provenance parsing (no DB needed to import).
from sieve_brain import canon_org, tier_of, _prov_method  # noqa: E402

DEFAULT_TAG = 'cluster-canon-2026-07-19'
DEFAULT_JACCARD = 0.60
DEFAULT_COSINE = 0.92
BATCH = int(os.getenv('CANON_BATCH', '200'))

# Railway coordinates for the sieve DB (Postgres-pxlu in AEO-SEO-fable).
# Env-overridable so the tool survives an infra move without a code change.
RAILWAY_PROJECT_ID = os.getenv('RAILWAY_PROJECT_ID',
                               'a0937b30-50da-4b38-be40-c8a664219dab')
RAILWAY_ENV_ID = os.getenv('RAILWAY_ENV_ID',
                           '8c6867da-a516-4b48-b474-7fff75dcaaee')
RAILWAY_PG_SERVICE_ID = os.getenv('RAILWAY_PG_SERVICE_ID',
                                  '75f2971f-285e-4c95-8ef4-73931d83c0a7')
_BACKBOARD = 'https://backboard.railway.com/graphql/v2'

# kind (= table name; the (kind, id) key) -> title column used for
# name-similarity. Mirrors sieve_brain._TABLE_CFG title columns.
KINDS = {
    'rules': 'name',
    'principles': 'title',
    'anti_patterns': 'title',
    'playbooks': 'name',
}

# The same DEAD-status set _trust_filter excludes — citeable = not in here
# AND superseded_by IS NULL.
_DEAD = ('deprecated', 'rejected', 'retired', 'superseded')

_STOP = {'a', 'an', 'the', 'of', 'for', 'in', 'on', 'to', 'and', 'or',
         'with', 'is', 'are', 'be', 'it', 'as', 'at', 'by'}


# ---------------------------------------------------------------------------
# Pure functions (selftest-covered, no DB)
# ---------------------------------------------------------------------------

def norm_tokens(name: Optional[str]) -> frozenset:
    """Normalized token set for name-similarity: lowercase, alnum-only,
    minimal stopword strip. Deterministic."""
    if not name:
        return frozenset()
    toks = re.findall(r'[a-z0-9]+', str(name).lower())
    return frozenset(t for t in toks if t not in _STOP)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def parse_vec(text: Optional[str]) -> Optional[List[float]]:
    """pgvector ::text is '[0.1,0.2,...]' — valid JSON."""
    if not text:
        return None
    try:
        v = json.loads(text)
        return v if isinstance(v, list) and v else None
    except (ValueError, TypeError):
        return None


def cosine(a: Optional[List[float]], b: Optional[List[float]]) -> Optional[float]:
    """None when either side is missing/mismatched — 'no embedding' must be
    distinguishable from 'orthogonal'."""
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return None
    return dot / (na * nb)


def _id_key(rid: Any) -> Tuple[int, int, str]:
    """Stable ASC ordering for TEXT ids: numeric-as-text sorts numerically
    ('9' before '10'), non-numeric lexicographically after."""
    s = str(rid)
    if s.isdigit():
        return (0, int(s), s)
    return (1, 0, s)


def _prov_rank_raw(raw: Any) -> int:
    """0 = extracted, 1 = unknown/legacy, 2 = neighbor-inferred. Accepts the
    JSON shape ({'method': ...}, via sieve_brain._prov_method) AND legacy
    plain-string values."""
    method = _prov_method(raw) or (raw if isinstance(raw, str) else None)
    if method == 'extracted':
        return 0
    if method == 'neighbor-inferred':
        return 2
    return 1


def _conf_of(row: Dict[str, Any]) -> float:
    """confidence_score when present; anti_patterns map risk_level like
    sieve_brain._conf_expr (high .9 / low .65 / else .8)."""
    v = row.get('confidence_score')
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    risk = str(row.get('risk_level') or '').lower()
    if risk:
        return {'high': 0.9, 'low': 0.65}.get(risk, 0.8)
    return 0.0


def _lv_key(row: Dict[str, Any]) -> int:
    """last_verified newest-first under an ASC composite sort: negative
    YYYYMMDD; missing/unparseable -> 0 (sorts last)."""
    lv = str(row.get('last_verified') or '')[:10]
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', lv)
    if not m:
        return 0
    return -int(m.group(1) + m.group(2) + m.group(3))


def election_key(row: Dict[str, Any]) -> Tuple:
    """The canonical wins the ASC sort. Order (spec): tier ASC via source_org,
    has source_url first, url_provenance 'extracted' first, last_verified
    newest, confidence DESC, id ASC."""
    return (
        tier_of(row.get('source_org')),
        0 if row.get('source_url') else 1,
        _prov_rank_raw(row.get('url_provenance')),
        _lv_key(row),
        -round(_conf_of(row), 4),
        _id_key(row.get('id')),
    )


def elect(members: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    ordered = sorted(members, key=election_key)
    return ordered[0], ordered[1:]


class _UnionFind:
    def __init__(self):
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic root choice: smaller id-key wins.
            if _id_key(rb) < _id_key(ra):
                ra, rb = rb, ra
            self.parent[rb] = ra


def candidate_pairs(rows: List[Dict[str, Any]], jaccard_min: float) -> List[Tuple[str, str, float]]:
    """Jaccard-passing pairs among rows (inverted token index — only pairs
    sharing >= 1 token are examined, which Jaccard >= 0.6 guarantees)."""
    index: Dict[str, List[int]] = {}
    for i, r in enumerate(rows):
        for t in r['_tokens']:
            index.setdefault(t, []).append(i)
    seen = set()
    out: List[Tuple[str, str, float]] = []
    for _, idxs in sorted(index.items()):
        for ai in range(len(idxs)):
            for bi in range(ai + 1, len(idxs)):
                pair = (idxs[ai], idxs[bi]) if idxs[ai] < idxs[bi] else (idxs[bi], idxs[ai])
                if pair in seen:
                    continue
                seen.add(pair)
                a, b = rows[pair[0]], rows[pair[1]]
                j = jaccard(a['_tokens'], b['_tokens'])
                if j >= jaccard_min:
                    out.append((str(a['id']), str(b['id']), j))
    out.sort(key=lambda p: (_id_key(p[0]), _id_key(p[1])))
    return out


def cluster_block(rows: List[Dict[str, Any]], jaccard_min: float, cosine_min: float,
                  embeddings: Dict[str, Optional[List[float]]]) -> List[Dict[str, Any]]:
    """Cluster one (kind, topic) block. Returns [{'method', 'rule_key'?,
    'members': [row, ...]}] with >= 2 members each, deterministically ordered.

    rule_key rows group by rule_key; the rest by name-similarity gated on
    cosine when BOTH embeddings exist (spec: 'when embeddings exist')."""
    rows = [r if r.get('_tokens') is not None
            else dict(r, _tokens=norm_tokens(r.get('_title'))) for r in rows]
    uf = _UnionFind()
    by_id = {str(r['id']): r for r in rows}
    edge_method: Dict[str, str] = {}

    keyed: Dict[str, List[str]] = {}
    for r in rows:
        rk = r.get('rule_key')
        if rk is not None and str(rk).strip():
            keyed.setdefault(str(rk).strip(), []).append(str(r['id']))
    for rk, ids in sorted(keyed.items()):
        ids.sort(key=_id_key)
        for other in ids[1:]:
            uf.union(ids[0], other)
        if len(ids) > 1:
            for i in ids:
                edge_method[i] = 'rule_key'

    plain = [r for r in rows
             if not (r.get('rule_key') is not None and str(r['rule_key']).strip())]
    for a_id, b_id, _j in candidate_pairs(plain, jaccard_min):
        cos = cosine(embeddings.get(a_id), embeddings.get(b_id))
        if cos is not None and cos < cosine_min:
            continue  # embeddings exist and disagree -> not a duplicate
        uf.union(a_id, b_id)
        edge_method.setdefault(a_id, 'name_similarity')
        edge_method.setdefault(b_id, 'name_similarity')

    groups: Dict[str, List[str]] = {}
    for rid in sorted(by_id, key=_id_key):
        if rid in uf.parent:
            groups.setdefault(uf.find(rid), []).append(rid)

    clusters = []
    for root in sorted(groups, key=_id_key):
        ids = groups[root]
        if len(ids) < 2:
            continue
        members = [by_id[i] for i in sorted(ids, key=_id_key)]
        method = ('rule_key' if all(edge_method.get(i) == 'rule_key' for i in ids)
                  else 'name_similarity')
        c: Dict[str, Any] = {'method': method, 'members': members}
        if method == 'rule_key':
            rk = members[0].get('rule_key')
            if rk is not None:
                c['rule_key'] = str(rk).strip()
        clusters.append(c)
    return clusters


def _topic_of(row: Dict[str, Any]) -> str:
    return str(row.get('domain_tag') or 'general').strip().lower() or 'general'


def _member_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'id': str(row.get('id')),
        'name': row.get('_title'),
        'status': row.get('status'),
        'source_org': row.get('source_org'),
        'canon_org': canon_org(row.get('source_org')) or None,
        'tier': tier_of(row.get('source_org')),
        'source_url': row.get('source_url'),
        'url_provenance_method': _prov_method(row.get('url_provenance'))
        or (row.get('url_provenance') if isinstance(row.get('url_provenance'), str) else None),
        'last_verified': (str(row.get('last_verified'))[:10]
                          if row.get('last_verified') else None),
        'confidence': round(_conf_of(row), 4),
        'domain_tag': row.get('domain_tag'),
        'rule_key': row.get('rule_key'),
    }


def build_plan(rows_by_kind: Dict[str, List[Dict[str, Any]]],
               jaccard_min: float = DEFAULT_JACCARD,
               cosine_min: float = DEFAULT_COSINE,
               embeddings_by_kind: Optional[Dict[str, Dict[str, Optional[List[float]]]]] = None,
               limit: Optional[int] = None,
               topic_block: bool = True,
               tag: str = DEFAULT_TAG) -> Dict[str, Any]:
    """Pure planner: rows in, deterministic plan dict out. Each row dict needs
    'id' + the kind's title under '_title' (+ optional status/source_org/
    source_url/url_provenance/last_verified/confidence_score/risk_level/
    domain_tag/rule_key). Never mutates inputs."""
    embeddings_by_kind = embeddings_by_kind or {}
    clusters_out: List[Dict[str, Any]] = []
    counts_kind: Dict[str, Dict[str, int]] = {}

    for kind in sorted(rows_by_kind):
        rows = []
        for r in rows_by_kind[kind]:
            r = dict(r)
            r['_tokens'] = norm_tokens(r.get('_title'))
            rows.append(r)
        blocks: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            blocks.setdefault(_topic_of(r) if topic_block else '*', []).append(r)

        k_clusters = 0
        k_rows = 0
        k_demote = 0
        emb = embeddings_by_kind.get(kind, {})
        for topic in sorted(blocks):
            for c in cluster_block(blocks[topic], jaccard_min, cosine_min, emb):
                canonical, losers = elect(c['members'])
                entry: Dict[str, Any] = {
                    'kind': kind,
                    'topic': topic,
                    'cluster_id': f'{kind}:{canonical["id"]}',
                    'method': c['method'],
                    'size': len(c['members']),
                    'canonical': _member_public(canonical),
                    'members': [_member_public(m) for m in c['members']],
                    'demote': [{
                        'id': str(m['id']),
                        'name': m.get('_title'),
                        'prior_status': m.get('status'),
                        'prior_superseded_by': m.get('superseded_by'),
                    } for m in losers],
                }
                if 'rule_key' in c:
                    entry['rule_key'] = c['rule_key']
                clusters_out.append(entry)
                k_clusters += 1
                k_rows += len(c['members'])
                k_demote += len(losers)
        counts_kind[kind] = {'citeable_rows': len(rows), 'clusters': k_clusters,
                             'rows_in_clusters': k_rows, 'demotions': k_demote}

    # Pilot ordering: biggest scatterers first, then (kind, canonical id).
    clusters_out.sort(key=lambda c: (-c['size'], c['kind'], _id_key(c['canonical']['id'])))
    if limit is not None:
        clusters_out = clusters_out[:limit]

    return {
        'tool': 'canonicalize_clusters',
        'tag': tag,
        'params': {'jaccard_min': jaccard_min, 'cosine_min': cosine_min,
                   'limit': limit, 'topic_block': topic_block},
        'counts': {
            'per_kind': counts_kind,
            'clusters_emitted': len(clusters_out),
            'demotions_emitted': sum(len(c['demote']) for c in clusters_out),
        },
        'clusters': clusters_out,
    }


def validate_plan(plan: Dict[str, Any]) -> List[str]:
    """Structural safety before any write: no self-supersession, no id both
    canonical and demoted (within a kind — ids collide ACROSS kinds and that
    is fine), no duplicate demotes, demote rows must look citeable."""
    errors: List[str] = []
    canon_ids = set()
    demote_ids = set()
    for c in plan.get('clusters', []):
        kind = c.get('kind')
        if kind not in KINDS:
            errors.append(f'unknown kind {kind!r} in cluster {c.get("cluster_id")}')
            continue
        cid = str(c.get('canonical', {}).get('id'))
        canon_ids.add((kind, cid))
        for d in c.get('demote', []):
            did = str(d.get('id'))
            if did == cid:
                errors.append(f'{kind}:{did} demoted to itself in {c.get("cluster_id")}')
            if (kind, did) in demote_ids:
                errors.append(f'{kind}:{did} demoted twice')
            demote_ids.add((kind, did))
            if d.get('prior_status') in _DEAD or d.get('prior_superseded_by'):
                errors.append(f'{kind}:{did} was not citeable at plan time')
    both = canon_ids & demote_ids
    for kind, rid in sorted(both):
        errors.append(f'{kind}:{rid} is canonical in one cluster and demoted in another')
    return errors


def reversal_sql(plan: Dict[str, Any]) -> List[str]:
    """The exact UPDATEs that undo an applied plan, grouped per (kind,
    prior_status). Guarded on status='superseded' + the canonical target so a
    row later touched by someone else is left alone."""
    stmts: List[str] = []
    groups: Dict[Tuple[str, Optional[str], str], List[str]] = {}
    for c in plan.get('clusters', []):
        canon = str(c.get('canonical', {}).get('id'))
        for d in c.get('demote', []):
            groups.setdefault((c['kind'], d.get('prior_status'), canon), []).append(str(d['id']))
    for (kind, prior, canon), ids in sorted(
            groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]), _id_key(kv[0][2]))):
        ids = sorted(set(ids), key=_id_key)
        id_list = ', '.join(f"'{i}'" for i in ids)
        restore = 'NULL' if prior is None else f"'{prior}'"
        stmts.append(
            f"-- {plan.get('tag')}: restore {len(ids)} {kind} rows"
            f" (prior status {prior!r}, canonical {canon})\n"
            f"UPDATE sieve.{kind} SET status = {restore}, superseded_by = NULL"
            f" WHERE id IN ({id_list}) AND status = 'superseded'"
            f" AND superseded_by = '{canon}';")
    return stmts


# ---------------------------------------------------------------------------
# DB plumbing (plan reads, apply writes; token never printed)
# ---------------------------------------------------------------------------

def _fetch_db_url(token: str) -> str:
    """DATABASE_PUBLIC_URL for Postgres-pxlu via Railway backboard GraphQL.
    '-' / 'env' -> SIEVE_DB_URL / DATABASE_URL (local runs). The token and the
    URL are never printed."""
    if token in ('-', 'env', ''):
        url = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')
        if not url:
            raise SystemExit("token '-' needs SIEVE_DB_URL or DATABASE_URL set")
        return url
    import urllib.request
    q = ('query($p:String!,$e:String!,$s:String!)'
         '{variables(projectId:$p,environmentId:$e,serviceId:$s)}')
    body = json.dumps({'query': q, 'variables': {
        'p': RAILWAY_PROJECT_ID, 'e': RAILWAY_ENV_ID,
        's': RAILWAY_PG_SERVICE_ID}}).encode()
    req = urllib.request.Request(_BACKBOARD, data=body, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        # backboard 403s the python-urllib UA (HANDOVER §6)
        'User-Agent': 'curl/8.4.0',
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    if data.get('errors'):
        raise SystemExit(f"railway API error: {data['errors'][0].get('message')}")
    variables = (data.get('data') or {}).get('variables') or {}
    url = variables.get('DATABASE_PUBLIC_URL')
    if not url:
        raise SystemExit('DATABASE_PUBLIC_URL not in service variables '
                         f'(got {sorted(variables)[:8]}...)')
    return url


def _connect(db_url: str):
    import psycopg2
    return psycopg2.connect(db_url, connect_timeout=15)


def _table_cols(conn, table: str) -> set:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='sieve' AND table_name=%s", (table,))
        return {r[0] for r in cur.fetchall()}


def _citeable_where(have: set) -> str:
    clauses = []
    if 'status' in have:
        dead = ', '.join(f"'{s}'" for s in _DEAD)
        clauses.append(f"coalesce(t.status,'active') NOT IN ({dead})")
    if 'superseded_by' in have:
        clauses.append('t.superseded_by IS NULL')
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else ''


def _fetch_citeable(conn, kind: str) -> List[Dict[str, Any]]:
    """Citeable rows for one kind with the columns the planner needs.
    source_url resolution mirrors sieve_brain._select_head / export_snapshots:
    playbooks carry their own; core kinds COALESCE own -> documents join."""
    from psycopg2.extras import RealDictCursor
    have = _table_cols(conn, kind)
    title_col = KINDS[kind]
    sel = ['t.id::text AS id', f't.{title_col} AS _title']
    for col in ('status', 'superseded_by', 'source_org', 'url_provenance',
                'domain_tag', 'rule_key', 'confidence_score', 'risk_level'):
        if col in have:
            sel.append(f't.{col} AS {col}')
    if 'last_verified' in have:
        sel.append('t.last_verified::text AS last_verified')

    join = ''
    if kind == 'playbooks' or not ({'document_id', 'source_refs_json'} & have):
        sel.append("NULLIF(t.source_url,'') AS source_url"
                   if 'source_url' in have else 'NULL AS source_url')
    else:
        on = ('d.id = t.document_id' if 'document_id' in have else
              "d.id = NULLIF(substring(t.source_refs_json from '\\d+'), '')")
        join = f' LEFT JOIN sieve.documents d ON {on}'
        dcols = _table_cols(conn, 'documents')
        own = "NULLIF(t.source_url,'')" if 'source_url' in have else 'NULL'
        durl = 'd.source_url' if 'source_url' in dcols else 'NULL'
        sel.append(f'COALESCE({own}, {durl}) AS source_url')

    sql = (f"SELECT {', '.join(sel)} FROM sieve.{kind} t{join}"
           f"{_citeable_where(have)} ORDER BY t.id")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def _fetch_embeddings(conn, kind: str, ids: List[str]) -> Dict[str, Optional[List[float]]]:
    """embedding::text for the requested ids (chunked); {} when the table has
    no embedding column (playbooks) — cosine gate then simply doesn't apply."""
    if 'embedding' not in _table_cols(conn, kind):
        return {}
    out: Dict[str, Optional[List[float]]] = {}
    with conn.cursor() as cur:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            cur.execute(
                f"SELECT id::text, embedding::text FROM sieve.{kind}"
                f" WHERE id::text = ANY(%s) AND embedding IS NOT NULL", (chunk,))
            for rid, emb in cur.fetchall():
                out[rid] = parse_vec(emb)
    return out


def _find_ledger_table(conn) -> Optional[Tuple[str, set]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='sieve' AND (table_name LIKE '%ledger%'"
            " OR table_name LIKE '%ops_log%') ORDER BY table_name")
        rows = [r[0] for r in cur.fetchall()]
    if not rows:
        return None
    name = rows[0]
    return name, _table_cols(conn, name)


def _ledger_record(conn, tag: str, kind: str, ids: List[str]) -> bool:
    """Best-effort tag row in a sieve ledger table when one exists with a
    tag-ish column. Failure NEVER affects the applied demotes (separate
    transaction); caller falls back to printing the reversal UPDATEs."""
    try:
        found = _find_ledger_table(conn)
        if not found:
            return False
        table, cols = found
        tag_col = next((c for c in ('tag', 'op_tag', 'operation_tag') if c in cols), None)
        if not tag_col:
            return False
        fields = {tag_col: tag}
        for c, v in (('action', 'canonicalize-demote'), ('operation', 'canonicalize-demote'),
                     ('kind', kind), ('table_name', kind),
                     ('payload', json.dumps({'kind': kind, 'ids': ids})),
                     ('details', json.dumps({'kind': kind, 'ids': ids}))):
            if c in cols and c not in fields:
                fields[c] = v
        names = ', '.join(fields)
        ph = ', '.join(['%s'] * len(fields))
        with conn.cursor() as cur:
            cur.execute(f'INSERT INTO sieve."{table}" ({names}) VALUES ({ph})',
                        list(fields.values()))
        conn.commit()
        print(f'  ledger: tagged {len(ids)} {kind} ids in sieve.{table}')
        return True
    except Exception as e:  # noqa: BLE001 — ledger is best-effort by contract
        try:
            conn.rollback()
        except Exception:
            pass
        print(f'  ledger: unavailable ({type(e).__name__}) — reversal SQL printed below')
        return False


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_plan(token: str, args) -> int:
    db_url = _fetch_db_url(token)
    conn = _connect(db_url)
    try:
        host = re.search(r'@([^/:]+)', db_url)
        print(f'connected (host={host.group(1) if host else "?"}) — READ-ONLY plan')
        kinds = [k.strip() for k in args.kinds.split(',') if k.strip() in KINDS]
        rows_by_kind: Dict[str, List[Dict[str, Any]]] = {}
        embeddings_by_kind: Dict[str, Dict[str, Optional[List[float]]]] = {}
        for kind in kinds:
            rows = _fetch_citeable(conn, kind)
            rows_by_kind[kind] = rows
            print(f'  {kind}: {len(rows)} citeable rows')
            # Embeddings only for ids that pass the Jaccard prefilter — the
            # cosine gate never needs the rest.
            probe = []
            for r in rows:
                r['_tokens'] = norm_tokens(r.get('_title'))
            blocks: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                blocks.setdefault(_topic_of(r) if args.topic_block else '*', []).append(r)
            need: set = set()
            for _topic, brows in sorted(blocks.items()):
                plain = [r for r in brows
                         if not (r.get('rule_key') is not None and str(r['rule_key']).strip())]
                for a, b, _j in candidate_pairs(plain, args.jaccard):
                    need.add(a)
                    need.add(b)
            probe = sorted(need, key=_id_key)
            embeddings_by_kind[kind] = _fetch_embeddings(conn, kind, probe)
            got = sum(1 for v in embeddings_by_kind[kind].values() if v)
            print(f'  {kind}: {len(probe)} pair-candidate ids, embeddings for {got}')
    finally:
        conn.close()

    plan = build_plan(rows_by_kind, jaccard_min=args.jaccard, cosine_min=args.cosine,
                      embeddings_by_kind=embeddings_by_kind,
                      limit=args.limit, topic_block=args.topic_block, tag=args.tag)
    errors = validate_plan(plan)
    if errors:
        print('PLAN INVALID:')
        for e in errors:
            print(f'  - {e}')
        return 2
    with open(args.out, 'w') as f:
        json.dump(plan, f, indent=1, ensure_ascii=False, default=str)
    c = plan['counts']
    print(f"plan: {c['clusters_emitted']} clusters, "
          f"{c['demotions_emitted']} demotions -> {args.out}")
    for kind, kc in sorted(c['per_kind'].items()):
        print(f"  {kind}: {kc['clusters']} clusters over {kc['rows_in_clusters']} rows "
              f"({kc['demotions']} demotions) of {kc['citeable_rows']} citeable")
    print(f"tag: {plan['tag']} — review the plan file, then run apply "
          f"(needs --apply + SIEVE_WRITE_OK=1)")
    return 0


def cmd_apply(token: str, args) -> int:
    with open(args.plan) as f:
        plan = json.load(f)
    errors = validate_plan(plan)
    if errors:
        print('REFUSING to apply an invalid plan:')
        for e in errors:
            print(f'  - {e}')
        return 2
    if not args.apply or os.getenv('SIEVE_WRITE_OK') != '1':
        print('DRY RUN (writes need BOTH --apply and SIEVE_WRITE_OK=1).')
        total = sum(len(c['demote']) for c in plan['clusters'])
        print(f"would demote {total} rows across {len(plan['clusters'])} clusters "
              f"(tag {plan.get('tag')}). Reversal SQL that apply would print:")
        for s in reversal_sql(plan):
            print(s)
        return 0

    db_url = _fetch_db_url(token)
    conn = _connect(db_url)
    tag = plan.get('tag') or DEFAULT_TAG
    applied = 0
    skipped = 0
    ids_by_kind: Dict[str, List[str]] = {}
    try:
        # Per-kind column safety: a kind without superseded_by cannot be
        # demoted reversibly — refuse it, apply the rest.
        cols_by_kind = {k: _table_cols(conn, k) for k in KINDS}
        pending = 0
        with conn.cursor() as cur:
            for c in plan['clusters']:
                kind = c['kind']
                have = cols_by_kind[kind]
                if 'superseded_by' not in have or 'status' not in have:
                    print(f"  SKIP cluster {c['cluster_id']}: sieve.{kind} lacks "
                          f"status/superseded_by — not reversible, not applied")
                    skipped += len(c['demote'])
                    continue
                cid = str(c['canonical']['id'])
                for d in c['demote']:
                    prior = d.get('prior_status')
                    cur.execute(
                        f"UPDATE sieve.{kind} SET status='superseded', superseded_by=%s"
                        f" WHERE id::text = %s AND superseded_by IS NULL"
                        f" AND coalesce(status,'active') = coalesce(%s,'active')",
                        (cid, str(d['id']), prior))
                    if cur.rowcount == 1:
                        applied += 1
                        ids_by_kind.setdefault(kind, []).append(str(d['id']))
                    else:
                        skipped += 1  # drifted since plan time — left alone
                    pending += 1
                    if pending >= BATCH:
                        conn.commit()
                        pending = 0
        conn.commit()
        print(f'applied: {applied} demotions, skipped (drift/missing): {skipped} '
              f'[tag {tag}]')
        if ids_by_kind:
            ledgered = True
            for kind, ids in sorted(ids_by_kind.items()):
                if not _ledger_record(conn, tag, kind, ids):
                    ledgered = False
            if not ledgered:
                print('-- REVERSAL (keep with the plan file):')
                for s in reversal_sql(plan):
                    print(s)
        else:
            print('nothing applied — no reversal needed')
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# Selftest — pure functions on fixtures, no DB / no network
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import random

    def row(rid, title, kind_extra=None, **kw):
        r = {'id': rid, '_title': title, 'status': kw.pop('status', 'active'),
             'superseded_by': None}
        r.update(kw)
        return r

    # 1) normalization + jaccard
    a = norm_tokens('Use ISO dates for freshness!')
    b = norm_tokens('use iso DATES freshness')
    assert jaccard(a, b) == 1.0, (a, b)
    assert jaccard(norm_tokens('alpha beta gamma'), norm_tokens('alpha beta delta')) == 0.5
    assert jaccard(frozenset(), norm_tokens('x1')) == 0.0
    print('  ✓ token normalization + jaccard')

    # 2) cosine incl. missing-embedding honesty
    assert abs(cosine([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(cosine([1, 0], [0, 1])) < 1e-9
    assert cosine(None, [1, 0]) is None and cosine([1], [1, 2]) is None
    assert parse_vec('[0.5,-0.25]') == [0.5, -0.25] and parse_vec('junk') is None
    print('  ✓ cosine + pgvector text parse (missing embedding -> None, never 0)')

    # 3) clustering: jaccard alone is not enough when embeddings disagree;
    #    passes when they agree; passes when either embedding is missing.
    rows = [
        row('1', 'publish dates on articles', domain_tag='seo'),
        row('2', 'publish dates on articles pages', domain_tag='seo'),
        row('3', 'publish dates on articles page', domain_tag='seo'),
        row('4', 'schema sameas links', domain_tag='seo'),
    ]
    emb_agree = {'1': [1.0, 0.0], '2': [0.99, 0.14], '3': None}
    cl = cluster_block(rows, 0.6, 0.92, emb_agree)
    assert len(cl) == 1 and [m['id'] for m in cl[0]['members']] == ['1', '2', '3'], cl
    assert cl[0]['method'] == 'name_similarity'
    emb_disagree = {'1': [1.0, 0.0], '2': [0.0, 1.0], '3': [0.0, 1.0]}
    cl2 = cluster_block(rows, 0.6, 0.92, emb_disagree)
    assert len(cl2) == 1 and [m['id'] for m in cl2[0]['members']] == ['2', '3'], cl2
    print('  ✓ similarity clustering: cosine gate vetoes, missing embedding degrades to jaccard')

    # 4) rule_key groups regardless of names; keyed rows never similarity-merge
    rows_rk = [
        row('10', 'completely different words', rule_key='dates.freshness'),
        row('11', 'unrelated title here', rule_key='dates.freshness'),
        row('12', 'unrelated title here today', rule_key=None),
    ]
    cl3 = cluster_block(rows_rk, 0.6, 0.92, {})
    assert len(cl3) == 1 and cl3[0]['method'] == 'rule_key'
    assert [m['id'] for m in cl3[0]['members']] == ['10', '11']
    assert cl3[0]['rule_key'] == 'dates.freshness'
    print('  ✓ rule_key grouping wins when present; keyed rows never similarity-merge')

    # 5) election order: tier, then has-url, then provenance method, then
    #    last_verified (newest), then confidence, then numeric-as-text id
    members = [
        row('9', 'r', source_org='someblog.com', source_url='https://x.com/a',
            url_provenance='{"method": "extracted"}', last_verified='2026-07-01',
            confidence_score='0.99'),
        row('10', 'r', source_org='Google', source_url=None,
            confidence_score='0.10'),
        row('11', 'r', source_org='Google', source_url='https://g.com/doc',
            url_provenance='{"method": "neighbor-inferred"}',
            last_verified='2026-06-01', confidence_score='0.50'),
        row('12', 'r', source_org='Google', source_url='https://g.com/doc2',
            url_provenance='{"method": "extracted"}', last_verified='2026-01-01',
            confidence_score='0.50'),
    ]
    canonical, losers = elect(members)
    assert canonical['id'] == '12', canonical  # tier-1 + url + extracted beats newer neighbor-inferred
    assert [m['id'] for m in losers] == ['11', '10', '9'], losers
    # id tiebreak is numeric-aware
    tie = [row('10', 'same', source_org='Google'), row('9', 'same', source_org='Google')]
    assert elect(tie)[0]['id'] == '9'
    print('  ✓ deterministic election: tier > url > extracted > freshest > confidence > id (numeric-as-text)')

    # 6) build_plan: topic blocking, prior-state capture, deterministic under shuffle
    fix = {
        'rules': [
            row('1', 'publish dates on articles', domain_tag='seo',
                source_org='Google', source_url='https://g.com/d',
                url_provenance='{"method": "extracted"}', confidence_score='0.9'),
            row('2', 'publish dates on articles pages', domain_tag='seo',
                source_org='someblog.com', status='candidate', confidence_score='0.8'),
            row('3', 'publish dates on articles', domain_tag='aeo',  # other topic block
                source_org='Moz', source_url='https://moz.com/x', confidence_score='0.7'),
            row('4', 'entity sameas consistency', domain_tag='seo', confidence_score='0.5'),
        ],
        'anti_patterns': [
            row('1', 'thin doorway pages', risk_level='high'),   # id collides with rules:1 — fine
            row('2', 'thin doorway pages everywhere', risk_level='low'),
        ],
    }
    emb = {'rules': {'1': [1.0, 0.0], '2': [0.995, 0.0999]},
           'anti_patterns': {}}
    plan = build_plan(fix, embeddings_by_kind=emb)
    assert plan['tag'] == DEFAULT_TAG
    assert plan['counts']['clusters_emitted'] == 2, plan['counts']
    kinds_seen = {c['kind'] for c in plan['clusters']}
    assert kinds_seen == {'rules', 'anti_patterns'}
    rc = next(c for c in plan['clusters'] if c['kind'] == 'rules')
    assert rc['canonical']['id'] == '1' and rc['demote'][0]['id'] == '2'
    assert rc['demote'][0]['prior_status'] == 'candidate'  # prior state captured for reversal
    assert rc['topic'] == 'seo' and rc['size'] == 2        # aeo twin blocked apart
    ac = next(c for c in plan['clusters'] if c['kind'] == 'anti_patterns')
    assert ac['canonical']['id'] == '1'                    # risk high(.9) beats low(.65)
    assert validate_plan(plan) == []
    shuffled = {k: random.Random(7).sample(v, len(v)) for k, v in fix.items()}
    plan2 = build_plan(shuffled, embeddings_by_kind=emb)
    assert json.dumps(plan, sort_keys=True) == json.dumps(plan2, sort_keys=True)
    print('  ✓ plan: topic blocks isolate, prior status recorded, byte-identical under shuffle')

    # 7) --limit pilot keeps the biggest clusters first
    lim = build_plan(fix, embeddings_by_kind=emb, limit=1)
    assert lim['counts']['clusters_emitted'] == 1
    assert lim['clusters'][0]['size'] == 2
    print('  ✓ --limit pilot: deterministic largest-first cut')

    # 8) validation catches self-supersession + canonical-demoted-elsewhere
    bad = json.loads(json.dumps(plan))
    bad['clusters'][0]['demote'].append(
        {'id': bad['clusters'][0]['canonical']['id'], 'prior_status': 'active',
         'prior_superseded_by': None})
    errs = validate_plan(bad)
    assert any('demoted to itself' in e for e in errs), errs
    chain = {'tag': DEFAULT_TAG, 'clusters': [
        {'kind': 'rules', 'cluster_id': 'rules:1', 'canonical': {'id': '1'},
         'demote': [{'id': '2', 'prior_status': 'active', 'prior_superseded_by': None}]},
        {'kind': 'rules', 'cluster_id': 'rules:2', 'canonical': {'id': '2'},
         'demote': [{'id': '3', 'prior_status': 'active', 'prior_superseded_by': None}]},
    ]}
    errs2 = validate_plan(chain)
    assert any('canonical in one cluster and demoted in another' in e for e in errs2), errs2
    # cross-KIND id reuse is legal (ids collide across kinds by design) — the
    # fixture plan above already contains rules:1 and anti_patterns:1.
    print('  ✓ validate_plan: self-supersession + supersession chains rejected; cross-kind id reuse legal')

    # 9) reversal SQL restores exact prior status per group, tag included
    stmts = reversal_sql(plan)
    joined = '\n'.join(stmts)
    assert DEFAULT_TAG in joined
    assert "SET status = 'candidate', superseded_by = NULL" in joined, joined
    assert "status = 'superseded'" in joined  # guard: only rows we flipped
    print('  ✓ reversal SQL: prior status restored per group, guarded, tagged')

    # 10) provenance rank accepts JSON and legacy plain strings
    assert _prov_rank_raw('{"method": "extracted"}') == 0
    assert _prov_rank_raw('extracted') == 0
    assert _prov_rank_raw('neighbor-inferred') == 2
    assert _prov_rank_raw(None) == 1
    print('  ✓ url_provenance: JSON method + legacy string both ranked')

    print('CANON_OK — clustering + election + plan/reversal verified on fixtures (no DB)')
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == '--selftest':
        return _selftest()
    if len(argv) < 2:
        print(__doc__)
        print('usage: canonicalize_clusters.py <railway-token|-> plan|apply [options]\n'
              '       canonicalize_clusters.py --selftest')
        return 2
    token, rest = argv[0], argv[1:]

    import argparse
    p = argparse.ArgumentParser(prog='canonicalize_clusters.py <railway-token|->')
    sub = p.add_subparsers(dest='cmd', required=True)
    pp = sub.add_parser('plan', help='READ-ONLY: build the cluster/demotion plan')
    pp.add_argument('--out', default='canon-plan.json')
    pp.add_argument('--kinds', default=','.join(sorted(KINDS)),
                    help='comma list of tables (default: all four kinds)')
    pp.add_argument('--limit', type=int, default=None,
                    help='pilot: emit only the N largest clusters')
    pp.add_argument('--jaccard', type=float, default=DEFAULT_JACCARD)
    pp.add_argument('--cosine', type=float, default=DEFAULT_COSINE)
    pp.add_argument('--tag', default=DEFAULT_TAG)
    pp.add_argument('--no-topic-block', dest='topic_block', action='store_false',
                    help='cluster within kind only (default blocks by domain_tag)')
    pa = sub.add_parser('apply', help='execute a reviewed plan file (gated)')
    pa.add_argument('--plan', required=True)
    pa.add_argument('--apply', action='store_true',
                    help='actually write (also needs env SIEVE_WRITE_OK=1)')
    args = p.parse_args(rest)

    if args.cmd == 'plan':
        return cmd_plan(token, args)
    return cmd_apply(token, args)


if __name__ == '__main__':
    sys.exit(main())
