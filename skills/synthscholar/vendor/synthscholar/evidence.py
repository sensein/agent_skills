"""
Evidence extraction — delegates to the LLM evidence extraction agent,
then validates every span against its source article (source grounding).

No hardcoded heuristics. The agent intelligently identifies relevant
evidence spans, claims, quantitative results, and contradictions
from article text based on the research question.

Three guarantees on what comes back:

* **Whole-document coverage** — each article's full text is chunked and every
  chunk is read, so results and discussion are mined, not just the opening
  pages (see :mod:`synthscholar.text_windows`).
* **First-party only** — spans the article attributes to another study
  (citation-borne findings, related-work statements) are discarded upstream in
  :func:`synthscholar.agents.run_evidence_extraction`. A review must synthesise
  what each paper found, not what it cites.
* **Source-grounded** — the span text must be traceable to the article's
  abstract or full text at a minimum fuzzy-match score. Ungrounded spans are
  dropped and logged.
"""

from __future__ import annotations

import logging

from .models import Article, EvidenceSpan
from .agents import AgentDeps, run_evidence_extraction
from .validation import filter_grounded, DEFAULT_THRESHOLD

logger = logging.getLogger(__name__)


async def extract_evidence(
    articles: list[Article],
    deps: AgentDeps,
    max_spans: int = 0,
    grounding_threshold: float = DEFAULT_THRESHOLD,
    concurrency: int = 5,
) -> list[EvidenceSpan]:
    """Extract and source-ground first-party evidence spans from articles.

    Runs the LLM extraction agent over the *whole* text of every article, then
    validates each span against the text of its cited article. Spans that
    cannot be matched to source text at or above ``grounding_threshold`` are
    dropped.

    Args:
        articles:             Included articles to extract evidence from.
        deps:                 Agent dependencies (protocol, API key, model).
        max_spans:            Optional cap on total spans returned. 0 (the
                              default) means no corpus-wide cap — per-article
                              breadth is bounded by the protocol's
                              ``evidence_spans_per_article`` instead, so a
                              large corpus doesn't starve later articles.
        grounding_threshold:  Minimum rapidfuzz score (0–100) to keep a span.

    Returns:
        List of source-grounded EvidenceSpan objects sorted by relevance.
        Each span has ``grounded=True`` and a non-zero ``grounding_score``.
    """
    raw_spans = await run_evidence_extraction(articles, deps, concurrency=concurrency)

    verified, report = filter_grounded(raw_spans, articles, threshold=grounding_threshold)

    if report.total > 0 and report.n_grounded < report.total:
        logger.info(
            "Evidence grounding: kept %d/%d spans — %s",
            report.n_grounded, report.total, report.summary(),
        )

    n_articles = len({s.paper_pmid for s in verified})
    logger.info(
        "Evidence: %d grounded first-party spans across %d/%d articles",
        len(verified), n_articles, len(articles),
    )

    return verified[:max_spans] if max_spans > 0 else verified
