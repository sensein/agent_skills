#!/usr/bin/env python3
"""Run full-text-inclusion SQL queries against the PostgreSQL article store.

Usage:
    python query_postgres.py included-full-text
    python query_postgres.py source-breakdown --dsn postgresql://u:p@host/db
    python query_postgres.py get-content --pmid 39012345
    python query_postgres.py full-text-search --q "sickle cell" --limit 10
    python query_postgres.py --list

DSN resolution order: --dsn, then $PRISMA_PG_DSN. Requires `psycopg` (v3).
Query definitions mirror references/sql_queries.md.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

# name -> (sql, needs_params)
QUERIES: dict[str, str] = {
    "included-full-text": """
        SELECT s.pmid, s.doi, s.title, aft.full_text_source,
               aft.content_size_bytes, aft.content_sha256, aft.retrieved_at
        FROM article_store s
        JOIN article_full_text aft USING (pmid)
        WHERE aft.content <> ''
        ORDER BY s.title
    """,
    "abstract-only": """
        SELECT s.pmid, s.doi, s.title
        FROM article_store s
        LEFT JOIN article_full_text aft USING (pmid)
        WHERE aft.pmid IS NULL AND s.abstract <> ''
        ORDER BY s.title
    """,
    "status-all": """
        SELECT s.pmid, s.title,
               CASE WHEN aft.pmid IS NOT NULL AND aft.content <> ''
                    THEN 'full_text' ELSE 'abstract_only' END AS status,
               aft.content_size_bytes
        FROM article_store s
        LEFT JOIN article_full_text aft USING (pmid)
        ORDER BY status, s.title
    """,
    "counts": """
        SELECT
          (SELECT count(*) FROM article_store)                         AS articles,
          (SELECT count(*) FROM article_full_text WHERE content <> '') AS full_text,
          (SELECT count(*) FROM article_store s
             LEFT JOIN article_full_text aft USING (pmid)
             WHERE aft.pmid IS NULL AND s.abstract <> '')              AS abstract_only
    """,
    "source-breakdown": """
        SELECT COALESCE(NULLIF(full_text_source, ''), '(unknown/backfilled)') AS source,
               count(*) AS n,
               pg_size_pretty(sum(content_size_bytes)::bigint) AS total_bytes
        FROM article_full_text
        WHERE content <> ''
        GROUP BY 1 ORDER BY n DESC
    """,
    "hash-dupes": """
        SELECT content_sha256, count(*) AS copies, array_agg(pmid) AS pmids
        FROM article_full_text
        WHERE content_sha256 <> ''
        GROUP BY content_sha256 HAVING count(*) > 1
        ORDER BY copies DESC
    """,
    "get-content": """
        SELECT pmid, full_text_source, content_size_bytes, content_sha256, content
        FROM article_full_text WHERE pmid = %(pmid)s
    """,
    "full-text-search": """
        SELECT pmid, ts_rank(search_vector, plainto_tsquery('english', %(q)s)) AS rank
        FROM article_full_text
        WHERE search_vector @@ plainto_tsquery('english', %(q)s)
        ORDER BY rank DESC LIMIT %(limit)s
    """,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", choices=sorted(QUERIES), help="named query")
    ap.add_argument("--dsn", default=os.getenv("PRISMA_PG_DSN", ""))
    ap.add_argument("--pmid", help="PMID (for get-content)")
    ap.add_argument("--q", help="search text (for full-text-search)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--format", choices=["table", "csv"], default="table")
    ap.add_argument("--list", action="store_true", help="list named queries and exit")
    args = ap.parse_args()

    if args.list:
        for name in sorted(QUERIES):
            print(name)
        return 0
    if not args.query:
        ap.error("query name is required (unless --list)")
    if not args.dsn:
        print("No DSN — pass --dsn or set $PRISMA_PG_DSN", file=sys.stderr)
        return 2
    if args.query == "get-content" and not args.pmid:
        ap.error("get-content requires --pmid")
    if args.query == "full-text-search" and not args.q:
        ap.error("full-text-search requires --q")

    try:
        import psycopg
    except ImportError:
        print("psycopg (v3) is required: pip install 'psycopg[binary]>=3.1'",
              file=sys.stderr)
        return 2

    params = {"pmid": args.pmid, "q": args.q, "limit": args.limit}
    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        cur.execute(QUERIES[args.query], params)
        cols = [d.name for d in cur.description] if cur.description else []
        rows = [[("" if v is None else str(v)) for v in row] for row in cur.fetchall()]

    if args.format == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(cols)
        w.writerows(rows)
    else:
        _print_table(cols, rows)
    return 0


def _print_table(cols: list[str], rows: list[list[str]]) -> None:
    def trunc(s: str, n: int = 60) -> str:
        s = s.replace("\n", " ")
        return s if len(s) <= n else s[: n - 1] + "…"

    disp = [[trunc(c) for c in r] for r in rows]
    widths = [max(len(cols[i]), *(len(r[i]) for r in disp)) if disp else len(cols[i])
              for i in range(len(cols))]
    print("  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    print("  ".join("-" * widths[i] for i in range(len(cols))))
    for r in disp:
        print("  ".join(r[i].ljust(widths[i]) for i in range(len(cols))))
    print(f"\n({len(rows)} rows)")


if __name__ == "__main__":
    raise SystemExit(main())
