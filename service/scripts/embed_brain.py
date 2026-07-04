"""
embed_brain.py — one-time / maintenance batch: generate OpenAI embeddings for the
Sieve brain so the auditor can do SEMANTIC citation retrieval (not just keyword FTS).

The loaded brain has NO rule embeddings (original Sieve embedded document_chunks,
not rules), so we generate them here with text-embedding-3-small (1536-dim) — the
same space the query is embedded in at audit time (see sieve_brain.py).

Idempotent + resumable: only embeds rows whose `embedding` is NULL. Safe to re-run.

Usage:
    OPENAI_API_KEY=... SIEVE_DB_URL=<railway pg>  python3 scripts/embed_brain.py
    # optional: EMBED_TABLES=rules,principles,anti_patterns (default all three)
"""

from __future__ import annotations

import os
import sys
import time

import psycopg2
from psycopg2.extras import execute_values

MODEL = "text-embedding-3-small"
DIM = 1536
API_BATCH = 500      # texts per OpenAI call
DB_BATCH = 500       # rows per UPDATE

DB_URL = os.getenv("SIEVE_DB_URL") or os.getenv("DATABASE_URL")

# table -> (text columns to concatenate for the embedding)
TABLES = {
    "rules":        ("name", "if_condition", "then_logic"),
    "principles":   ("title", "statement", "explanation"),
    "anti_patterns": ("title", "description"),
}


def _client():
    from openai import OpenAI
    return OpenAI()


def _embed(client, texts):
    """Embed a list of texts with one retry on transient error."""
    for attempt in range(3):
        try:
            resp = client.embeddings.create(model=MODEL, input=texts)
            return [d.embedding for d in resp.data]
        except Exception as e:
            if attempt == 2:
                raise
            print(f"    openai retry {attempt+1}: {e}")
            time.sleep(2 * (attempt + 1))


def _vec_literal(v):
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def ensure_column_and_index(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE sieve.{table} ADD COLUMN IF NOT EXISTS embedding vector({DIM})")
    # HNSW index (created after backfill for speed, but IF NOT EXISTS makes re-run safe)


def build_text(cols, row):
    parts = [str(row[i]) for i in range(len(cols)) if row[i]]
    return " ".join(parts)[:6000] or "(empty)"


def ensure_index(conn, table):
    """Build an ANN index, best-effort. Serial build (parallel workers caused a
    shared-memory DiskFull on the small Railway PG). HNSW → IVFFlat → skip."""
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_maintenance_workers = 0")
        cur.execute("SET maintenance_work_mem = '128MB'")
        cur.execute(f"SELECT count(*) FROM sieve.{table} WHERE embedding IS NOT NULL")
        n = cur.fetchone()[0]
    if not n:
        return
    for kind, ddl in (
        ("HNSW", f"CREATE INDEX IF NOT EXISTS {table}_embedding_hnsw "
                 f"ON sieve.{table} USING hnsw (embedding vector_cosine_ops)"),
        ("IVFFlat", f"CREATE INDEX IF NOT EXISTS {table}_embedding_ivf "
                    f"ON sieve.{table} USING ivfflat (embedding vector_cosine_ops) "
                    f"WITH (lists = {max(10, n // 1000)})"),
    ):
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            print(f"    [{table}] {kind} index ready ({n:,} vectors)")
            return
        except Exception as e:
            conn.rollback()
            print(f"    [{table}] {kind} index failed ({str(e)[:70]}); trying next")
    print(f"    [{table}] no ANN index — exact search (slower)")


def embed_table(conn, client, table, cols):
    col_list = ", ".join(cols)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM sieve.{table} WHERE embedding IS NULL")
        todo = cur.fetchone()[0]
    print(f"\n[{table}] {todo:,} rows to embed")
    if not todo:
        ensure_index(conn, table)   # embeddings already present — just (re)build index
        return
    done = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, {col_list} FROM sieve.{table} "
                f"WHERE embedding IS NULL LIMIT {DB_BATCH}"
            )
            rows = cur.fetchall()
        if not rows:
            break
        ids = [r[0] for r in rows]
        texts = [build_text(cols, r[1:]) for r in rows]
        # OpenAI in sub-batches
        vectors = []
        for i in range(0, len(texts), API_BATCH):
            vectors.extend(_embed(client, texts[i:i + API_BATCH]))
        payload = [(i, _vec_literal(v)) for i, v in zip(ids, vectors)]
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"UPDATE sieve.{table} AS t SET embedding = d.emb::vector "
                f"FROM (VALUES %s) AS d(id, emb) WHERE t.id = d.id",
                payload,
            )
        conn.commit()
        done += len(rows)
        print(f"    {done:,}/{todo:,}", end="\r", flush=True)
    print(f"    {done:,}/{todo:,} — done")
    ensure_index(conn, table)


def main():
    if not DB_URL:
        sys.exit("SIEVE_DB_URL / DATABASE_URL not set")
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY not set")
    which = os.getenv("EMBED_TABLES", "rules,principles,anti_patterns").split(",")
    client = _client()
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        for t in which:
            t = t.strip()
            if t in TABLES:
                ensure_column_and_index(conn, t)
                conn.commit()
                embed_table(conn, client, t, TABLES[t])
    finally:
        conn.close()
    print("\nAll requested tables embedded.")


if __name__ == "__main__":
    main()
