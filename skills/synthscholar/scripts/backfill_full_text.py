#!/usr/bin/env python3
"""Backfill the article_full_text provenance table from legacy article_store rows.

For every article that has a non-empty article_store.full_text but no
article_full_text row, insert one with a Python-computed content_sha256 (so it
matches the RDF slr:content_hash). Historical rows get an empty full_text_source
since their provenance was never recorded. Idempotent.

Usage:
    PRISMA_PG_DSN=postgresql://u:p@host/db python backfill_full_text.py
    python backfill_full_text.py --dsn postgresql://u:p@host/db --batch-size 200

Requires the `synthscholar` package importable and migration 006 applied.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys


async def _run(dsn: str, batch_size: int) -> int:
    from synthscholar.cache.article_store import ArticleStore

    async with ArticleStore(dsn=dsn) as store:
        if not store._has_full_text_table:
            print(
                "article_full_text not found — apply migration 006 first:\n"
                '  psql "$PRISMA_PG_DSN" -f '
                "synthscholar/cache/migrations/006_add_full_text_table.sql",
                file=sys.stderr,
            )
            return 2
        n = await store.backfill_full_text_table(batch_size=batch_size)
        print(f"Backfilled {n} full-text rows into article_full_text.")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=os.getenv("PRISMA_PG_DSN", ""))
    ap.add_argument("--batch-size", type=int, default=100)
    args = ap.parse_args()

    if not args.dsn:
        print("No DSN — pass --dsn or set $PRISMA_PG_DSN", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args.dsn, args.batch_size))
    except ModuleNotFoundError as exc:
        print(f"Cannot import synthscholar ({exc}). Run from the "
              "SynthScholar env or add it to PYTHONPATH.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
