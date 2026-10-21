"""
Pydantic AI agents for each PRISMA pipeline step.

Each agent is a specialized pydantic-ai Agent with typed output,
system prompt, and optional tool access. They use OpenRouter via
the OpenAIChatModel + OpenRouterProvider.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re as _re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

from pydantic import BaseModel, model_validator
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from .provenance import run_traced
from .models import (
    ReviewProtocol,
    SearchStrategy,
    ScreeningBatchResult,
    ScreeningDecision,
    ScreeningDecisionType,
    RiskOfBiasResult,
    RoBDomainAssessment,
    RoBJudgment,
    StudyDataExtraction,
    SynthesisDivergence,
    GRADEAssessment,
    GRADECertainty,
    Article,
    BatchEvidenceExtraction,
    EvidenceSpan,
    ExtractedEvidenceItem,
    DataChartingRubric,
    PRISMANarrativeRow,
    CriticalAppraisalRubric,
    GroundingValidationResult,
    Abstract,
    Introduction,
    ThematicSynthesisResult,
    Discussion,
    Conclusion,
    QuantitativeAnalysis,
    BiasAssessment,
    Theme,
    PrismaFlow,
    Implications,
    RubricSectionOutput,
    SECTION_FORMAT,
    # Feature 006
    ChartingTemplate,
    ChartingSection,
    FieldDefinition,
    FieldAnswer,
    SectionExtractionResult,
    CriticalAppraisalConfig,
    AppraisalDomainSpec,
    AppraisalItemSpec,
    ItemRating,
    DomainAppraisal,
    CriticalAppraisalResult,
    CONCERN_AGGREGATION_RULE,
    # Search synthesis
    SearchSynthesis,
    GroupSummary,
    PerDisorderSynthesis,
    # Feature 013: per-group analysis (synthesis + Q&A per bucket)
    GroupQuestionAnswer,
    GroupAnalysisEntry,
    PerGroupAnalysis,
)


# ────────────────────── Shared Dependencies ────────────────────────────

@dataclass
class AgentDeps:
    """Shared dependencies injected into all agents via RunContext."""
    protocol: ReviewProtocol
    api_key: str = ""
    model_name: str = "anthropic/claude-sonnet-4"
    model: object = field(default=None, repr=False)
    provenance: object = field(default=None, repr=False)  # ProvenanceCollector | None


def build_model(api_key: str, model_name: str = "anthropic/claude-sonnet-4"):
    """Create a model for pydantic-ai agents.

    When model_name is "test", returns pydantic_ai.models.test.TestModel for
    CI/mock runs that avoid real LLM API calls.
    """
    if model_name == "test":
        from pydantic_ai.models.test import TestModel
        return TestModel()
    provider = OpenRouterProvider(api_key=api_key)
    return OpenAIChatModel(model_name, provider=provider)


# ────────────────────── Map-Reduce Batching ────────────────────────────
#
# Per-article context blocks (title + abstract + full-text excerpt + RoB +
# findings) typically run 1.5–3 KB each. To keep prompts well within any
# major model's context window, the synthesis-style agents shard the corpus
# into char-budgeted batches, run partial generations in parallel, then
# merge. The budgets below leave generous headroom for the system prompt,
# evidence spans, and the model's own response.

SYNTHESIS_BATCH_CHARS = 80_000   # ~25–35 articles per batch in practice
THEMATIC_BATCH_CHARS = 60_000    # smaller — thematic also includes rubrics + spans

# pydantic-ai output-validation retry budget. Each agent retries up to this
# many times when the model returns content that fails Pydantic validation
# of ``output_type``. Bumped from the historical 5 → 10 because complex
# nested schemas (PrismaReview, ThematicSynthesisResult) can require several
# self-correction passes on smaller models. Single source of truth so all
# 23+ Agent constructors stay aligned.
AGENT_VALIDATION_RETRIES = 10


def _chunk_articles_by_budget(
    articles: list,
    blocks: list[str],
    budget: int,
) -> list[list]:
    """Group articles into batches whose article-block char totals stay under budget.

    Each batch contains at least one article; oversized single articles are
    placed alone in their own batch (they will fail downstream if truly
    larger than the model's window — the caller should prefer larger
    models in that case).
    """
    if not articles:
        return []
    batches: list[list] = []
    current: list = []
    current_chars = 0
    for art, block in zip(articles, blocks):
        block_len = len(block)
        if current and current_chars + block_len > budget:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(art)
        current_chars += block_len
    if current:
        batches.append(current)
    return batches


def _format_evidence_spans(spans: list, head_label: str = "EXTRACTED EVIDENCE SPANS") -> str:
    """Render evidence spans as a labelled, numbered prompt section."""
    if not spans:
        return ""
    lines = [
        f'  [{i}] (PMID:{e.paper_pmid}, score:{getattr(e, "relevance_score", 0):.2f}) '
        f'"{e.text[:400]}"'
        for i, e in enumerate(spans)
    ]
    return f"\n\n== {head_label} ==\n" + "\n".join(lines)


# ────────────────────── 1. Search Strategy Agent ───────────────────────

search_strategy_agent = Agent(
    output_type=SearchStrategy,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review methodologist. Given a PRISMA protocol
(research question, PICO, inclusion/exclusion criteria), generate a
multi-provider search strategy. Each provider has its own query list
tuned to that provider's syntax. Emit an empty list when a provider is
unlikely to add coverage for the topic (e.g. arXiv for a clinical
drug-trial review, DOAJ for a bench-science topic with no journal-level
focus). Quality over quantity — empty lists are encouraged when a
provider would just add noise.

Per-provider rules:

- pubmed_queries (2-5):
    Use MeSH terms with [MeSH Terms], field tags ([Title/Abstract], [tw]),
    Boolean operators (AND, OR, NOT), and parenthesised groups. Mix one
    broad MeSH-anchored query with 1-2 narrower keyword combos. Date and
    language filters are added downstream — do NOT include them here.

- biorxiv_queries (2-3):
    Free-text only. The bioRxiv API does not support field tags or Boolean
    operators. 2-3 keywords per query, lowercase. medRxiv reuses these
    queries automatically — no separate list. Avoid MeSH (no MeSH support).

- europe_pmc_queries (1-3):
    Lucene-style with optional EuPMC field tags (TITLE:, ABSTRACT:, AUTH:,
    KW:, SRC:MED for PubMed-overlap suppression). Keep simple unless the
    topic genuinely needs scoping. Boolean operators allowed.

- openalex_queries (1-3):
    Free-text keyword combos. OpenAlex search treats space as AND.
    Phrases in double quotes work. No MeSH, no field tags.

- crossref_queries (1-2):
    Free-text. CrossRef search is metadata-driven (title, abstract,
    container) and ranks by relevance. Keep queries short and
    keyword-dense.

- doaj_queries (0-2):
    Free-text title/abstract search across open-access journals. Often
    redundant with OpenAlex/CrossRef — emit only when the topic
    specifically benefits from open-access journal scoping. Empty list is
    common.

- semantic_scholar_queries (1-2):
    Free-text, semantic-style. S2 ranks by paper embeddings, so natural-
    language phrasing of the research question works as well as keyword
    soup. 1-2 queries cover most topics.

- arxiv_queries (0-2):
    Field operators allowed: cat:cs.AI, ti:"machine learning",
    abs:transformer. Emit only when the topic touches CS / physics /
    math / quant-bio. Empty list for purely clinical biomedical reviews.

- core_queries (0-2):
    Free-text or Lucene-light syntax. CORE aggregates 57M+ full texts
    across 15K repositories — wide net, more noise. Useful for topics
    with strong grey-literature or repository representation. Empty list
    is fine for tightly-scoped clinical reviews.

Cross-cutting:
- Both broad and specific queries within each non-empty list (recall +
  precision).
- Reuse mesh_terms across all relevant providers but only put the
  bracketed [MeSH Terms] form in pubmed_queries.
- Populate mesh_terms (canonical MeSH labels) and key_concepts (free-form
  topical anchors) for downstream filters.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="search_strategy",
    defer_model_check=True,
)


@search_strategy_agent.system_prompt
async def _search_strategy_context(ctx: RunContext[AgentDeps]) -> str:
    p = ctx.deps.protocol
    grouping_note = ""
    if getattr(p, "grouping_dimension", "") and p.grouping_dimension != "disorder_cohort":
        grouping_note = (
            f"\nDownstream stratification: results will later be bucketed by "
            f"`{p.grouping_dimension}`. Make sure your queries collectively "
            f"surface enough variation along that dimension that each likely "
            f"bucket has 2+ studies — otherwise per-group analysis will be "
            f"thin."
        )
    elif getattr(p, "grouping_dimension", "") == "disorder_cohort":
        grouping_note = (
            "\nDownstream stratification: results will later be bucketed by "
            "disorder/cohort. Ensure your PubMed/Europe PMC queries cover the "
            "major disorders likely to satisfy the protocol's PICO so each "
            "cohort has 2+ studies."
        )
    return (
        f"\nResearch Question: {p.title}\n"
        f"Objective: {p.objective}\n"
        f"PICO:\n{p.pico_text}\n"
        f"Inclusion: {p.inclusion_criteria}\n"
        f"Exclusion: {p.exclusion_criteria}\n"
        f"Date range: {p.date_range_start} to {p.date_range_end}"
        f"{grouping_note}"
    )


# ────────────────────── 2. Screening Agent ─────────────────────────────

screening_agent = Agent(
    output_type=ScreeningBatchResult,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review screener. Evaluate each article against
the inclusion/exclusion criteria.

At title_abstract stage: be INCLUSIVE (when in doubt, include). You are
working from title and abstract only, so exclude only on grounds the
abstract makes unambiguous.

At full_text stage: be STRICT, and judge on the report itself. Each article
arrives with either its retrieved full text or an explicit
"FULL TEXT NOT AVAILABLE" marker.
- With full text: base the decision on what the paper actually did —
  population, design, comparator, and whether it reports the review's
  outcome. Check the methods and results, not just the abstract's framing.
  Cite the specific detail that decided it (e.g. "n=12 case series, below
  the 20-participant minimum"; "outcome reported only as a composite").
- With FULL TEXT NOT AVAILABLE: decide on the abstract, and begin the
  reason with "Abstract only — " so the basis is on the record. Exclude on
  this basis only when the abstract itself is decisive; otherwise include
  and let the limitation be reported.

For each article, return a decision with index, include/exclude, reason,
and relevance_score (0-1). The reason is quoted in the PRISMA
exclusion-reasons table, so make it specific and self-contained — never
"does not meet criteria" alone.

You MUST return a decision for EVERY article provided.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="screener",
    defer_model_check=True,
)


@screening_agent.system_prompt
async def _screening_context(ctx: RunContext[AgentDeps]) -> str:
    p = ctx.deps.protocol
    return (
        f"\nResearch Question: {p.question}\n"
        f"Inclusion: {p.inclusion_criteria}\n"
        f"Exclusion: {p.exclusion_criteria}"
    )


# ────────────────────── 3. Risk of Bias Agent ──────────────────────────

rob_agent = Agent(
    output_type=RiskOfBiasResult,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review methodologist assessing risk of bias.
Assess the provided study using the specified risk of bias tool.
For each domain, provide a judgment (Low, Some concerns, High) with
supporting justification from the study text.
Provide an overall judgment and brief summary.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="risk_of_bias",
    defer_model_check=True,
)


ROB_DOMAINS: dict[str, list[str]] = {
    "RoB 2": [
        "Randomization process",
        "Deviations from intended interventions",
        "Missing outcome data",
        "Measurement of the outcome",
        "Selection of the reported result",
    ],
    "Jadad Scale": [
        "Randomization (described and appropriate)",
        "Blinding (described and appropriate)",
        "Withdrawals and dropouts",
    ],
    "ROBINS-I": [
        "Confounding", "Selection of participants",
        "Classification of interventions",
        "Deviations from intended interventions",
        "Missing data", "Measurement of outcomes",
        "Selection of the reported result",
    ],
    "ROBINS-E": [
        "Confounding", "Selection of participants",
        "Classification of exposures",
        "Departures from intended exposures",
        "Missing data", "Measurement of outcomes",
        "Selection of the reported result",
    ],
    "Newcastle-Ottawa Scale": [
        "Selection (representativeness, selection of non-exposed, ascertainment of exposure, outcome not present at start)",
        "Comparability (controls for main factor, controls for additional factor)",
        "Outcome (assessment, follow-up length, adequacy of follow-up)",
    ],
    "QUADAS-2": [
        "Patient selection",
        "Index test",
        "Reference standard",
        "Flow and timing",
    ],
    "CASP Qualitative Checklist": [
        "Clear statement of aims",
        "Appropriate methodology",
        "Appropriate research design",
        "Appropriate recruitment strategy",
        "Data collection addresses research issue",
        "Researcher-participant relationship considered",
        "Ethical issues considered",
        "Sufficiently rigorous data analysis",
        "Clear statement of findings",
        "Value of research",
    ],
    "JBI Critical Appraisal": [
        "Inclusion criteria clearly defined",
        "Study subjects and setting described",
        "Exposure measured validly and reliably",
        "Objective and standard criteria used for condition measurement",
        "Confounding factors identified and strategies stated",
        "Outcomes measured validly and reliably",
        "Appropriate statistical analysis used",
    ],
    "Murad Tool": [
        "Selection (does the patient represent the whole experience?)",
        "Ascertainment (was the exposure adequately ascertained?)",
        "Causality (was the outcome adequately ascertained?)",
        "Reporting (was follow-up long enough?)",
    ],
    "SYRCLE": [
        "Sequence generation", "Baseline characteristics",
        "Allocation concealment", "Random housing",
        "Blinding (caregivers/investigators)", "Random outcome assessment",
        "Blinding (outcome assessors)",
        "Incomplete outcome data", "Selective outcome reporting",
        "Other sources of bias",
    ],
    "MINORS": [
        "Clearly stated aim", "Inclusion of consecutive patients",
        "Prospective data collection", "Appropriate endpoint",
        "Unbiased assessment of endpoint", "Follow-up appropriate",
        "Loss to follow-up <5%", "Prospective sample size calculation",
        "Adequate control group", "Contemporary groups",
        "Baseline equivalence", "Adequate statistical analysis",
    ],
    "ROBIS": [
        "Study eligibility criteria",
        "Identification and selection of studies",
        "Data collection and study appraisal",
        "Synthesis and findings",
    ],
}


@rob_agent.system_prompt
async def _rob_context(ctx: RunContext[AgentDeps]) -> str:
    tool = ctx.deps.protocol.rob_tool.value
    domains = ROB_DOMAINS.get(tool, ["General quality assessment"])
    return f"\nRoB Tool: {tool}\nDomains to assess: {', '.join(domains)}"


# ────────────────────── 4. Data Extraction Agent ───────────────────────

data_extraction_agent = Agent(
    output_type=StudyDataExtraction,
    deps_type=AgentDeps,
    system_prompt="""\
You are extracting structured data from a research study for a
systematic review. Extract all available information including study
design, sample size, population, intervention, comparator, outcomes,
key findings, effect measures, follow-up duration, and funding.

Be precise and only report what is stated in the study.
If information is not available, leave the field empty or "Unknown".
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="data_extractor",
    defer_model_check=True,
)


# ────────────────────── 5. Synthesis Agent ─────────────────────────────

synthesis_agent = Agent(
    output_type=str,
    deps_type=AgentDeps,
    system_prompt="""\
You are writing the Results and Discussion sections of a PRISMA 2020
systematic review. Your synthesis must be:

1. **GROUNDED** — every claim must cite specific studies by PMID
2. **EVIDENCE-BACKED** — quote or closely paraphrase source text
3. **STRUCTURED** — use thematic synthesis with clear subsections
4. **CRITICAL** — note contradictions, gaps, and limitations
5. **QUANTITATIVE** — report effect sizes, sample sizes, p-values

Structure:

## Study Selection
Describe the PRISMA flow using the counts provided.

## Study Characteristics
Summarize included studies: designs, populations, settings, outcomes.

## Thematic Synthesis
Organize by themes. For each:
- State finding clearly
- Cite evidence with PMIDs: (Author et al., Year; PMID: XXXXX)
- Note strength of evidence
- Flag contradictions

## Risk of Bias Summary
Discuss quality and biases across studies.

## Heterogeneity
Discuss sources of heterogeneity.

## Gaps and Future Directions
What is missing? What needs further research?

## Key Claims (Evidence Map)
Structured list:
- **Claim**: [statement]
  - **Evidence**: [PMID, quote/paraphrase, strength: strong/moderate/weak]
  - **Contradictions**: [if any]

NEVER fabricate data, PMIDs, or quotes. If uncertain, say so.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="synthesizer",
    defer_model_check=True,
)


@synthesis_agent.system_prompt
async def _synthesis_context(ctx: RunContext[AgentDeps]) -> str:
    p = ctx.deps.protocol
    return (
        f"\nResearch Question: {p.question}\n"
        f"Inclusion: {p.inclusion_criteria}"
    )


# ────────────────────── 6. GRADE Assessment Agent ──────────────────────

grade_agent = Agent(
    output_type=GRADEAssessment,
    deps_type=AgentDeps,
    system_prompt="""\
Perform a GRADE certainty of evidence assessment for the given outcome.
Evaluate five domains: risk_of_bias, inconsistency, indirectness,
imprecision, publication_bias.
For each domain provide a rating (No downgrade, Serious, Very serious)
and explanation.
Provide overall certainty (High, Moderate, Low, Very Low) and a
plain-language summary.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="grade_assessor",
    defer_model_check=True,
)


# ────────────────────── 7. Bias Summary Agent ──────────────────────────

bias_summary_agent = Agent(
    output_type=str,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review methodologist assessing overall risk of bias
across included studies. Provide:
1. Overall assessment of study quality and potential biases
2. Common methodological limitations
3. Publication bias considerations
4. Heterogeneity assessment
5. Confidence in the body of evidence

Be specific and cite study characteristics.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="bias_summary",
    defer_model_check=True,
)


# ────────────────────── 8. Limitations Agent ───────────────────────────

limitations_agent = Agent(
    output_type=str,
    deps_type=AgentDeps,
    system_prompt="""\
Write the Limitations section for this systematic review. Address:
1. Search limitations (databases, language restrictions)
2. Selection bias
3. Methodological heterogeneity
4. Publication bias risk
5. Review-level limitations (AI-assisted screening)
Be concise but thorough. 2-3 paragraphs.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="limitations",
    defer_model_check=True,
)


# ────────────────────── 9. Evidence Extraction Agent ───────────────────

evidence_extraction_agent = Agent(
    output_type=BatchEvidenceExtraction,
    deps_type=AgentDeps,
    system_prompt="""\
You are an evidence extraction specialist for systematic reviews.
You receive one part of one article's text plus the research question, and
extract the evidence spans that part contains.

FIRST-PARTY ONLY — this is the rule that matters most.
Extract only what THIS paper claims about ITS OWN work: results it measured,
outcomes it observed, comparisons it ran, conclusions its authors draw about
their own findings, and limitations they acknowledge about their own study.

REJECT — do not extract, or mark is_own_finding=false and name the source in
attributed_to:
- Findings attributed to other studies: "Smith et al. reported 91% accuracy",
  "previous work has shown…", "accuracy of 0.88 has been reported [12]".
  A number that belongs to another paper is NOT this paper's evidence, even
  though it appears in this paper's text.
- Background and related-work statements, prevalence/burden framing, and
  general domain knowledge ("depression affects 280 million people").
- Aims, hypotheses, and future work with no result attached ("we aim to…",
  "future studies should…").
- Definitions, dataset descriptions from third parties, and citations of
  methods without this paper's own outcome.

A useful test: if the sentence would still be true had this study never been
run, it is not this study's evidence.

For the parts you keep, prioritise:
- The paper's own quantitative results (effect sizes, accuracy / sensitivity /
  specificity / AUC, p-values, confidence intervals, sample sizes)
- Its primary and secondary outcome statements
- Methodological details of THIS study that affect how much its result can be
  trusted (design, validation scheme, blinding, sample composition)
- Contradictions or limitations the authors state about their own results

Rules:
- Quote the article's own words verbatim where possible; a close paraphrase is
  acceptable. Never fabricate, never merge two sentences into one quote, never
  round or restate a number differently from the source.
- is_own_finding=true only for this paper's own claims; false otherwise, with
  attributed_to naming whose statement it is.
- Score relevance 0-1 by how directly the span addresses the research question.
- Flag quantitative spans (is_quantitative=true).
- Name the section the span came from (abstract, methods, results, discussion,
  conclusion, limitations) — use the heading the text itself shows.
- Extract every distinct first-party finding in the part you were given, up to
  6 spans. Do not pad: if this part is references, acknowledgements, or pure
  background, return an empty evidence list for the article.
- Report the article under the exact PMID given in the prompt.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="evidence_extractor",
    defer_model_check=True,
)


@evidence_extraction_agent.system_prompt
async def _evidence_context(ctx: RunContext[AgentDeps]) -> str:
    p = ctx.deps.protocol
    return (
        f"\nResearch Question: {p.question}\n"
        f"PICO: {p.pico_text}\n"
        f"Focus on evidence relevant to: {p.pico_outcome or 'primary outcomes'}"
    )


# ────────────────── Agent Runner Helpers ───────────────────────────────

async def run_search_strategy(deps: AgentDeps, user_feedback: str = "") -> SearchStrategy:
    """Generate search strategy using LLM agent."""
    # api_key from deps; same for both initial and re-generation calls
    model = deps.model or build_model(deps.api_key, deps.model_name)
    prompt = "Generate a comprehensive search strategy for this systematic review."
    if user_feedback:
        prompt += (
            f"\n\nUser feedback on previous strategy: {user_feedback}\n\n"
            "Please revise the strategy accordingly."
        )
    result = await run_traced(
        search_strategy_agent, prompt, deps=deps, model=model,
        step_name="search_strategy_generation",
        iteration_mode="iterative_with_human_feedback",
    )
    return result.output


async def run_screening(
    articles: list[Article],
    deps: AgentDeps,
    stage: str = "title_abstract",
) -> ScreeningBatchResult:
    """Screen a batch of articles.

    At ``stage="full_text"`` each article's **retrieved full text** is included
    in the prompt (windowed head+tail, budget shared across the batch), so the
    eligibility decision is made on the report rather than on a second reading
    of the abstract. Articles whose full text could not be obtained carry an
    explicit ``FULL TEXT NOT AVAILABLE`` marker — the screener is told to
    decide on the abstract and to say so, and the caller records the basis on
    the screening log.
    """
    from .text_windows import DEFAULT_ARTICLE_BUDGET, window_text

    full_text_stage = stage == "full_text"
    per_article_budget = 0
    if full_text_stage:
        budget = getattr(deps.protocol, "article_text_budget", DEFAULT_ARTICLE_BUDGET)
        # Share the prompt budget across the batch, with a floor so a large
        # batch still shows enough of each paper to judge eligibility.
        per_article_budget = max(4000, budget // max(1, len(articles)))

    blocks: list[str] = []
    for i, a in enumerate(articles):
        block = (
            f"[{i}] PMID:{a.pmid}\nTitle: {a.title}\n"
            f"Abstract: {(a.abstract or 'N/A')[:600]}\n"
            f"Year: {a.year} | Journal: {a.journal}"
        )
        if full_text_stage:
            body = window_text(a.full_text or "", per_article_budget)
            if body:
                block += (
                    f"\nFull text (retrieved via {a.full_text_source or 'unknown route'}):"
                    f"\n{body}"
                )
            else:
                block += "\nFULL TEXT NOT AVAILABLE — decide on the abstract."
        blocks.append(block)

    articles_text = "\n\n".join(blocks)
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        screening_agent,
        f"Stage: {stage}\n\n"
        f"=== ARTICLES TO SCREEN ({len(articles)}) ===\n{articles_text}",
        deps=deps,
        model=model,
        step_name="screening",
        iteration_mode="zero_shot",
    )
    return result.output


def _article_body(article: Article, deps: AgentDeps) -> str:
    """Full text for a single-prompt stage, windowed to keep head **and** tail.

    A hard prefix (the old ``full_text[:2000]``) shows the model the title page
    and introduction and hides the results — see
    :mod:`synthscholar.text_windows`.
    """
    from .text_windows import DEFAULT_ARTICLE_BUDGET, window_text

    budget = getattr(deps.protocol, "article_text_budget", DEFAULT_ARTICLE_BUDGET)
    return window_text(article.full_text or "", budget) or "Not available"


async def run_risk_of_bias(article: Article, deps: AgentDeps) -> RiskOfBiasResult:
    """Assess risk of bias for a single study."""
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        rob_agent,
        f"Title: {article.title}\n"
        f"Abstract: {article.abstract[:2000]}\n"
        f"Full text: {_article_body(article, deps)}",
        deps=deps,
        model=model,
        step_name="rob_assessment",
        iteration_mode="zero_shot",
        target_pmid=article.pmid,
    )
    return result.output


async def run_data_extraction(
    article: Article,
    data_items: list[str],
    deps: AgentDeps,
) -> StudyDataExtraction:
    """Extract structured data from a study."""
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        data_extraction_agent,
        f"Title: {article.title}\n"
        f"Abstract: {article.abstract[:2500]}\n"
        f"Full text: {_article_body(article, deps)}\n\n"
        f"Data items to extract: {', '.join(data_items)}",
        deps=deps,
        model=model,
        step_name="data_extraction",
        iteration_mode="zero_shot",
        target_pmid=article.pmid,
    )
    return result.output


async def run_synthesis(
    articles: list[Article],
    evidence_spans: list,
    flow_text: str,
    deps: AgentDeps,
) -> str:
    """Generate grounded narrative synthesis over the full included corpus.

    For corpora that fit ``SYNTHESIS_BATCH_CHARS`` of article-block context, a
    single LLM call produces the synthesis. For larger corpora, articles are
    sharded into char-budgeted batches, partial syntheses run **in parallel**,
    and the partials are merged via :func:`run_synthesis_merge_agent` into one
    coherent narrative covering every included article.
    """
    if not articles:
        return ""

    blocks = [a.to_context_block(i + 1) for i, a in enumerate(articles)]
    model = deps.model or build_model(deps.api_key, deps.model_name)
    total_block_chars = sum(len(b) for b in blocks)

    # Single-pass when the corpus fits the budget.
    if total_block_chars <= SYNTHESIS_BATCH_CHARS:
        prompt = (
            f"PRISMA Flow: {flow_text}\n\n"
            f"=== INCLUDED ARTICLES ({len(articles)}) ===\n"
            + "\n\n".join(blocks)
            + _format_evidence_spans(evidence_spans)
        )
        result = await run_traced(
            synthesis_agent, prompt, deps=deps, model=model,
            step_name="synthesis",
            iteration_mode="zero_shot",  # single-pass: corpus fit budget
        )
        return result.output

    # Map-reduce: shard articles, distribute spans by PMID, run in parallel.
    batches = _chunk_articles_by_budget(articles, blocks, SYNTHESIS_BATCH_CHARS)
    spans_by_pmid: dict[str, list] = {}
    for e in evidence_spans:
        spans_by_pmid.setdefault(e.paper_pmid, []).append(e)

    async def _partial(batch_idx: int, batch_articles: list) -> str:
        batch_blocks = [
            a.to_context_block(i + 1) for i, a in enumerate(batch_articles)
        ]
        batch_spans: list = []
        for a in batch_articles:
            batch_spans.extend(spans_by_pmid.get(a.pmid, []))
        prompt = (
            f"PRISMA Flow: {flow_text}\n\n"
            f"=== INCLUDED ARTICLES (batch {batch_idx + 1} of {len(batches)}, "
            f"{len(batch_articles)} of {len(articles)} total articles) ===\n"
            + "\n\n".join(batch_blocks)
            + _format_evidence_spans(batch_spans)
        )
        partial_result = await run_traced(
            synthesis_agent, prompt, deps=deps, model=model,
            step_name="synthesis",
            iteration_mode="hierarchical_reduce",
            batch_index=batch_idx,
        )
        return partial_result.output

    partials = await asyncio.gather(
        *(_partial(i, b) for i, b in enumerate(batches))
    )
    return await run_synthesis_merge_agent(list(partials), deps)


async def run_grade(
    outcome: str,
    articles: list[Article],
    deps: AgentDeps,
) -> GRADEAssessment:
    """Run GRADE assessment for an outcome."""
    studies_text = "\n".join(
        f"- {a.title} ({a.year}): RoB={a.risk_of_bias.overall if a.risk_of_bias else '?'}"
        for a in articles
    )
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        grade_agent,
        f"Outcome: {outcome}\nStudies ({len(articles)}):\n{studies_text}",
        deps=deps,
        model=model,
        step_name="grade_assessment",
        iteration_mode="zero_shot",
        target_outcome=outcome,
    )
    return result.output


async def run_bias_summary(articles: list[Article], deps: AgentDeps) -> str:
    """Generate overall bias assessment."""
    articles_text = "\n".join(
        f"- {a.title} ({a.year}, {a.journal}) [PMID:{a.pmid}]"
        for a in articles
    )
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        bias_summary_agent,
        f"Included studies:\n{articles_text}",
        deps=deps,
        model=model,
        step_name="bias_summary",
        iteration_mode="zero_shot",
    )
    return result.output


async def run_limitations(
    flow_text: str,
    articles: list[Article],
    deps: AgentDeps,
) -> str:
    """Generate limitations section."""
    p = deps.protocol
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        limitations_agent,
        f"Question: {p.question}\n"
        f"Databases: {', '.join(p.databases)}\n"
        f"Flow summary: {flow_text}\n"
        f"Study types: {', '.join(set(a.journal for a in articles))}",
        deps=deps,
        model=model,
        step_name="limitations",
        iteration_mode="zero_shot",
    )
    return result.output


# Openers that mark a sentence as another study's claim regardless of how the
# model labelled it. Deliberately narrow — only phrasings that cannot be the
# paper's own result — so first-party evidence is never dropped by accident.
_THIRD_PARTY_LEAD_RE = _re.compile(
    r"^\W*(?:"
    r"previous(?:ly)?\s+(?:studies|work|research|reports?|findings?)"
    r"|prior\s+(?:studies|work|research|reports?)"
    r"|earlier\s+(?:studies|work|reports?|research)"
    r"|(?:several|many|most|other|numerous)\s+(?:studies|authors|works|papers)"
    r"|it\s+(?:has|had)\s+been\s+(?:shown|reported|demonstrated|suggested|observed|found)"
    r"|(?:has|have)\s+been\s+(?:shown|reported|demonstrated|suggested)\s+"
    r"|according\s+to\s+"
    r"|the\s+literature\s+(?:suggests|reports|shows)"
    r"|existing\s+(?:studies|work|approaches|methods)"
    r")",
    _re.I,
)


def _is_first_party(item: "ExtractedEvidenceItem") -> tuple[bool, str]:
    """Is this span the article's own claim about its own work?

    Returns ``(keep, reason_when_dropped)``. Two independent gates: the
    model's own ``is_own_finding`` label, then a conservative regex on
    attribution openers that the label sometimes misses.
    """
    if not item.is_own_finding:
        who = item.attributed_to or "another source"
        return False, f"attributed to {who}"
    if _THIRD_PARTY_LEAD_RE.match(item.quote or ""):
        return False, "third-party attribution phrasing"
    return True, ""


async def run_evidence_extraction(
    articles: list[Article],
    deps: AgentDeps,
    concurrency: int = 5,
    *,
    chunk_chars: int | None = None,
    chunk_overlap: int | None = None,
    max_chars: int | None = None,
    spans_per_article: int | None = None,
) -> list[EvidenceSpan]:
    """Extract first-party evidence spans, reading each article in full.

    Every article's stored full text is split into sequential overlapping
    chunks (:func:`synthscholar.text_windows.chunk_text`) and **every chunk is
    processed**, so Results, Discussion and Conclusion are read rather than
    just the opening pages. One agent call handles one chunk of one article,
    which keeps the PMID attribution exact and the prompt focused.

    Spans the model marks as belonging to another study — or that open with an
    unmistakable third-party attribution — are discarded: a systematic review
    must synthesise what each paper found, not what it cites.

    Budgets default to the protocol's ``evidence_chunk_chars`` /
    ``evidence_chunk_overlap`` / ``evidence_max_chars`` /
    ``evidence_spans_per_article``; pass them explicitly to override.
    """
    import asyncio as _asyncio

    from .text_windows import (
        DEFAULT_CHUNK_CHARS, DEFAULT_CHUNK_OVERLAP, chunk_text,
    )

    proto = deps.protocol
    chunk_chars = chunk_chars if chunk_chars is not None else getattr(
        proto, "evidence_chunk_chars", DEFAULT_CHUNK_CHARS)
    chunk_overlap = chunk_overlap if chunk_overlap is not None else getattr(
        proto, "evidence_chunk_overlap", DEFAULT_CHUNK_OVERLAP)
    max_chars = max_chars if max_chars is not None else getattr(
        proto, "evidence_max_chars", 0)
    spans_per_article = spans_per_article if spans_per_article is not None else getattr(
        proto, "evidence_spans_per_article", 8)

    model = deps.model or build_model(deps.api_key, deps.model_name)

    # One work item per (article, chunk). Articles without full text still get
    # one item so their abstract is mined.
    work: list[tuple[Article, int, int, str]] = []
    for art in articles:
        chunks = chunk_text(
            art.full_text or "",
            chunk_chars=chunk_chars,
            overlap=chunk_overlap,
            max_chars=max_chars,
        ) or [""]
        for ci, chunk in enumerate(chunks):
            work.append((art, ci, len(chunks), chunk))

    n_items = len(work)
    item_spans: list[list[EvidenceSpan]] = [[] for _ in work]
    dropped = {"third_party": 0, "unlabelled_pmid": 0}
    sem = _asyncio.Semaphore(concurrency)
    _done = [0]
    logger.info(
        "evidence_extraction_start articles=%d chunks=%d chunk_chars=%d",
        len(articles), n_items, chunk_chars,
    )

    async def _run_item(idx: int, art: Article, ci: int, n_ch: int, chunk: str) -> None:
        async with sem:
            part = (
                f"Part {ci + 1} of {n_ch} of the article's full text"
                if n_ch > 1 else "The article's full text"
            )
            body = chunk or "Not available — use the abstract only."
            prompt = (
                f"=== Article (PMID:{art.pmid}) ===\n"
                f"Title: {art.title}\n"
                f"Authors: {art.authors} ({art.year})\n"
                f"Journal: {art.journal}\n"
                f"Abstract: {(art.abstract or 'N/A')[:2000]}\n\n"
                f"--- {part} ---\n{body}\n\n"
                f"Extract this article's OWN evidence from the part above. "
                f"Report it under PMID:{art.pmid}."
            )
            try:
                result = await run_traced(
                    evidence_extraction_agent,
                    prompt,
                    deps=deps,
                    model=model,
                    step_name="evidence_extraction",
                    iteration_mode="validated_against_source",
                    target_pmid=art.pmid,
                    batch_index=idx,
                )
                spans: list[EvidenceSpan] = []
                for art_ev in result.output.articles:
                    # Single-article prompt: trust the article we sent, not the
                    # PMID echoed back (models occasionally reformat it).
                    if art_ev.pmid and art_ev.pmid != art.pmid:
                        dropped["unlabelled_pmid"] += 1
                    for ev in art_ev.evidence:
                        keep, why = _is_first_party(ev)
                        if not keep:
                            dropped["third_party"] += 1
                            logger.debug(
                                "evidence_span_dropped pmid=%s reason=%s quote=%.80s",
                                art.pmid, why, ev.quote,
                            )
                            continue
                        spans.append(EvidenceSpan(
                            text=ev.quote,
                            paper_pmid=art.pmid,
                            paper_title=art.title,
                            section=ev.section,
                            relevance_score=ev.relevance,
                            claim=ev.claim,
                            doi=art.doi,
                        ))
                item_spans[idx] = spans
            except Exception as exc:
                logger.warning(
                    "evidence extraction failed for %s chunk %d/%d: %s",
                    art.pmid, ci + 1, n_ch, exc,
                )
            finally:
                _done[0] += 1
                logger.info(
                    "evidence_chunk_done %d/%d pmid=%s part=%d/%d spans=%d",
                    _done[0], n_items, art.pmid, ci + 1, n_ch,
                    len(item_spans[idx]),
                )

    await _asyncio.gather(*[_run_item(i, a, ci, n, c) for i, (a, ci, n, c) in enumerate(work)])

    # Keep the strongest spans per article, then drop near-duplicates that the
    # chunk overlap can produce.
    per_article: dict[str, list[EvidenceSpan]] = {}
    for spans in item_spans:
        for s in spans:
            per_article.setdefault(s.paper_pmid, []).append(s)

    all_spans: list[EvidenceSpan] = []
    for pmid, spans in per_article.items():
        spans.sort(key=lambda x: x.relevance_score, reverse=True)
        # 0.85 rather than the default 0.7: the only duplicates expected here
        # come from the chunk overlap and are near-identical, while two genuine
        # findings from one paper ("sensitivity was 0.91…" / "specificity was
        # 0.88…") share enough function words to trip a looser threshold.
        all_spans.extend(_deduplicate_spans(spans, threshold=0.85)[:spans_per_article])

    all_spans.sort(key=lambda x: x.relevance_score, reverse=True)
    logger.info(
        "evidence_extraction_done spans=%d articles_with_evidence=%d/%d "
        "dropped_third_party=%d",
        len(all_spans), len(per_article), len(articles), dropped["third_party"],
    )
    return all_spans


def _deduplicate_spans(spans: list[EvidenceSpan], threshold: float = 0.7) -> list[EvidenceSpan]:
    """Remove near-duplicate spans using word overlap."""
    kept: list[EvidenceSpan] = []
    for span in spans:
        words = set(span.text.lower().split())
        is_dup = False
        for existing in kept:
            ex_words = set(existing.text.lower().split())
            if not words or not ex_words:
                continue
            overlap = len(words & ex_words) / min(len(words), len(ex_words))
            if overlap > threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(span)
    return kept


# ────────────────────── Steps 13–18: Charting, Appraisal, Narrative, Grounding ──

data_charting_agent = Agent(
    output_type=DataChartingRubric,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review data charter. Extract structured information
from the provided research article into the Data Charting Rubric format.

Complete all sections (A-G) based on the article content. Use "Not Reported"
for missing information. Be precise and only report what is explicitly stated.

Section A: Publication metadata
Section B: Study design details
Section C: Disordered group participants
Section D: Healthy controls (if applicable)
Section E: Data collection methods
Section F: Features, models, and performance
Section G: Synthesis and reviewer notes (summarize key findings)

If the prompt includes additional protocol-specific questions, answer each one
in the custom_fields dict with a concise extracted answer as the value. Key it
by the question's identifier when the question starts with one ("RQ1.1 What
groups were compared?" → "RQ1.1"), otherwise by the question text verbatim. Use
"Not Reported" when the article does not address the question.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="data_charter",
    defer_model_check=True,
)


narrative_row_agent = Agent(
    output_type=PRISMANarrativeRow,
    deps_type=AgentDeps,
    system_prompt="""\
You are creating a condensed PRISMA-style narrative row from the detailed
charting data. Generate a six-cell summary:

1. Study design / sample / dataset (from Sections B, C, D, E)
2. Methods (from Sections E, F: feature extraction, model, validation)
3. Outcomes (from Section F: key performance results, Section G: summary)
4. Key limitations (from Section G notes + appraisal domains)
5. Relevance notes (from Section A: disorder cohort/focus, Section G)
6. Review-specific questions (customized per protocol)

Keep each cell concise (1-2 sentences) but informative.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="narrative_summarizer",
    defer_model_check=True,
)


critical_appraisal_agent = Agent(
    output_type=CriticalAppraisalRubric,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review critical appraiser. Complete the four-domain
critical appraisal rubric for the provided study.

For each domain, evaluate each item using the rating definitions:
- Yes: criterion fully met and clearly reported
- Partial: partially met or partially reported
- No: addressed but not met
- Not Reported: not addressed at all
- N/A: not applicable

Then assign overall concern for each domain:
- Low: all/nearly all items Yes, minor Partial ratings
- Some: one+ Partial/No items affecting validity but not undermining
- High: multiple No/Not Reported items, or critical item No compromising conclusions

Complete all four domains with item-level ratings and justifications.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="critical_appraiser",
    defer_model_check=True,
)


introduction_agent = Agent(
    output_type=str,
    deps_type=AgentDeps,
    system_prompt="""\
Write the Introduction section (10–15% of review length) for a PRISMA 2020 systematic review.
Cover these four subsections in order:

1.1 Background and Context — define the problem, its significance, and the domain landscape.
1.2 Theoretical/Conceptual Framework (if applicable) — guiding models or taxonomies; omit if not relevant.
1.3 Rationale for the Review — why a systematic review is needed now: conflicting evidence,
    gap in existing reviews, new evidence since last review, or absence of any synthesis.
1.4 Objectives and Research Questions — state in PICO or equivalent and number each:
    RQ1, RQ2, …

Style: past tense for completed work; present for established knowledge. First person plural.
End with the numbered RQ list.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="introduction_writer",
    defer_model_check=True,
)


@introduction_agent.system_prompt
async def _introduction_context(ctx: RunContext[AgentDeps]) -> str:
    p = ctx.deps.protocol
    return (
        f"\nReview Title: {p.title}\n"
        f"Research Question: {p.question}\n"
        f"PICO:\n{p.pico_text}\n"
        f"Inclusion: {p.inclusion_criteria}\n"
        f"Exclusion: {p.exclusion_criteria}\n"
        f"Target Audience: {getattr(p, 'target_audience', '') or 'academic journal'}\n"
        f"Word Count Target: {getattr(p, 'word_count_target', 8000)}"
    )


conclusions_agent = Agent(
    output_type=str,
    deps_type=AgentDeps,
    system_prompt="""\
Write the Conclusions section (1–2 paragraphs, 150–250 words) for a PRISMA 2020 systematic review.
Requirements:
- Directly answer each research question (RQ1, RQ2, …).
- Do NOT overstate certainty beyond what GRADE / the synthesis supports.
- Do NOT introduce new data or references.
- Summarise what the evidence shows, its certainty level, and key gaps.
- Close with one forward-looking sentence on priority future research.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="conclusions_writer",
    defer_model_check=True,
)


@conclusions_agent.system_prompt
async def _conclusions_context(ctx: RunContext[AgentDeps]) -> str:
    p = ctx.deps.protocol
    return (
        f"\nResearch Question: {p.question}\n"
        f"PICO Outcome: {p.pico_outcome or 'Not specified'}"
    )


abstract_agent = Agent(
    output_type=str,
    deps_type=AgentDeps,
    system_prompt="""\
Write a structured abstract (250–300 words) for a PRISMA 2020 systematic review.
Follow the PRISMA-Abstract 12-item checklist. Use these exact labelled sub-headings:

**Background:** 1–2 sentences on why the review matters and the knowledge gap.
**Objectives:** The research question(s) and specific aims.
**Methods:** Eligibility criteria, information sources (with latest search date),
  risk-of-bias tool, synthesis method.
**Results:** Number of studies included, key study/participant characteristics,
  main findings, certainty of evidence (GRADE level).
**Conclusions:** Primary interpretation, one implication, one limitation.
**Registration:** Protocol registry and ID, or "not registered".
**Keywords:** 5–8 comma-separated indexing terms.

Total: ≤300 words. Do not cite individual papers by name.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="abstract_writer",
    defer_model_check=True,
)


grounding_validation_agent = Agent(
    None,  # model supplied per-call via model= kwarg; avoids requiring OPENAI_API_KEY at import time
    deps_type=AgentDeps,
    output_type=GroundingValidationResult,
    system_prompt="""\
You are a Grounding Validator for systematic review text produced in the PRISMA 2020 tradition.
Your job is to determine, clause by clause, whether each assertion in AI-generated review
excerpts is faithfully grounded in the provided source corpus.

Core Principles:
- Text-to-text fidelity only: Ground claims against the provided corpus documents
- Atomic decomposition: Break excerpts into single verifiable propositions
- Exact matching: Numbers, citations, and facts must match sources digit-for-digit
- No hallucinations: Reject any claim not directly supported by cited sources

Return a complete GroundingValidationResult with atomic claim decomposition,
verdicts, and scoring. Be meticulous in identifying every discrepancy.
""",
)


_DEFAULT_APPRAISAL_DOMAINS = [
    "Participant and Sample Quality",
    "Data Collection Quality",
    "Feature and Model Quality",
    "Bias and Transparency",
]


_SECTION_KEY_FIELDS: dict[str, list[str]] = {
    "A": ["title", "authors", "year", "journal_conference", "doi", "database_retrieved", "disorder_cohort", "primary_focus"],
    "B": ["primary_goal", "study_design", "duration_frequency", "subject_model", "task_type", "study_setting", "country_region"],
    "C": ["disorder_diagnosis", "diagnosis_assessment", "n_disordered", "age_mean_sd", "age_range", "gender_distribution", "comorbidities_included_excluded", "medications_therapies", "severity_levels"],
    "D": ["healthy_controls_included", "healthy_status_confirmed", "n_controls", "age_mean_sd_controls", "age_range_controls", "gender_distribution_controls", "age_matched", "gender_matched", "neurodevelopmentally_typical"],
    "E": ["data_types", "tasks_performed", "equipment_tools", "new_dataset_contributed", "dataset_openly_available", "dataset_available_request", "sensitive_data_anonymized"],
    "F": ["feature_types", "specific_features", "feature_extraction_tools", "feature_importance_reported", "importance_method", "top_features_identified", "feature_change_direction", "model_category", "specific_algorithms", "validation_methodology", "performance_metrics", "key_performance_results"],
    "G": ["summary_key_findings", "features_associated_disorder", "future_directions_recommended", "reviewer_notes"],
}

_FORMAT_PATTERNS: dict[str, str] = {
    "table": r"^\|",           # starts with | (Markdown table)
    "bullet_list": r"^- ",     # starts with "- "
    "numeric": r"^[\d\.]",     # starts with digit or decimal
    "yes_no": r"^(Yes|No)\b",  # starts with Yes or No
}


def _apply_concern_rule(
    ratings: list[str],
    rule: CONCERN_AGGREGATION_RULE,
) -> str:
    """Derive domain concern level from item ratings using the specified aggregation rule.

    Returns "Low", "Some", or "High". Applied deterministically in Python — not delegated to LLM.
    """
    if not ratings:
        return "High"

    _positive = {"yes"}
    _negative = {"no", "not reported"}

    positive_count = sum(1 for r in ratings if r.strip().lower() in _positive)
    negative_count = sum(1 for r in ratings if r.strip().lower() in _negative)
    total = len(ratings)

    if rule == "majority_yes":
        if positive_count > total / 2:
            return "Low"
        if negative_count > total / 2:
            return "High"
        return "Some"

    if rule == "strict":
        if positive_count == total:
            return "Low"
        if negative_count >= 2:
            return "High"
        return "Some"

    # lenient
    if positive_count > 0:
        return "Low"
    if negative_count == total:
        return "High"
    return "Some"


def _extract_section_text(rubric: DataChartingRubric, section_key: str, display_title: str) -> str:
    """Extract the raw text for a section from the rubric fields."""
    import re
    fields = _SECTION_KEY_FIELDS.get(section_key.upper(), [])
    if fields:
        parts = [
            str(getattr(rubric, f, "") or "")
            for f in fields
            if str(getattr(rubric, f, "") or "").strip()
        ]
        return "\n".join(parts) if parts else ""
    # Custom question — look in custom_fields, which is keyed by the question's
    # id when it has one ("RQ1.1 What groups …" → "RQ1.1") and by its text
    # otherwise, so try both before giving up.
    if display_title in rubric.custom_fields:
        return rubric.custom_fields[display_title]
    from .research_questions import _split_id

    qid, rest = _split_id(display_title)
    for candidate in (qid, rest):
        if candidate and candidate in rubric.custom_fields:
            return rubric.custom_fields[candidate]
    lowered = {k.strip().lower(): v for k, v in rubric.custom_fields.items()}
    return lowered.get(display_title.strip().lower(), "")


def _validate_format(text: str, fmt: str) -> bool:
    """Return True if text appears to match the expected format."""
    import re
    pattern = _FORMAT_PATTERNS.get(fmt)
    if pattern is None:
        return True  # descriptive: always valid
    return bool(re.search(pattern, text, re.MULTILINE))


def _is_schema_too_complex_error(exc: Exception) -> bool:
    """Return True for provider 400 errors caused by overly-complex JSON schemas (e.g. Gemini)."""
    msg = str(exc)
    return "too much branching" in msg or (
        "INVALID_ARGUMENT" in msg and "constraint" in msg.lower()
    )


async def _run_with_text_fallback(
    agent: "Any", prompt: str, target_type: "Any", deps: "Any", model: "Any",
    *, step_name: str = "structured_output", iteration_mode: "str" = "self_check_retry",
    target_pmid: str = "",
) -> "Any":
    """Try structured-output agent.run(); on schema-complexity 400, retry as plain text + JSON parse."""
    import logging as _lg
    import re as _re2

    try:
        r = await run_traced(
            agent, prompt, deps=deps, model=model,
            step_name=step_name, iteration_mode=iteration_mode,
            target_pmid=target_pmid,
        )
        return r.output
    except Exception as _exc:
        if not _is_schema_too_complex_error(_exc):
            raise
        _log2 = _lg.getLogger(__name__)
        _log2.warning(
            "Schema-complexity error (%s); retrying agent '%s' in text mode",
            type(_exc).__name__, getattr(agent, "name", "?"),
        )
        _fields = [k for k in target_type.model_fields if k != "section_outputs"]
        _hint = ", ".join(_fields)
        _fp = (
            prompt
            + f"\n\nReturn ONLY a JSON object. Required fields: {_hint}. "
            "Use empty string for unknown text fields, [] for list fields, "
            "{} for dict fields. No markdown fences."
        )
        _tr = await run_traced(
            agent, _fp, deps=deps, model=model,
            step_name=f"{step_name}_text_fallback",
            iteration_mode="self_check_retry",
            target_pmid=target_pmid,
            output_type=str,
        )
        _raw = _tr.output.strip()
        if _raw.startswith("```"):
            _lines = _raw.split("\n")
            _raw = "\n".join(_lines[1:-1] if _lines[-1].strip() == "```" else _lines[1:])
        # LLMs frequently emit malformed JSON: control chars in strings,
        # truncated output, single quotes, unquoted keys, trailing commas.
        # _validate_json_safe layers strict pydantic → stdlib strict=False →
        # json_repair to handle each case.
        from ._json_safe import model_validate_json_safe as _validate_json_safe
        _agent_name = getattr(agent, "name", "?")
        try:
            return _validate_json_safe(target_type, _raw, source=f"agent:{_agent_name}")
        except Exception as _pe:
            _m = _re2.search(r"\{.*\}", _raw, _re2.DOTALL)
            if _m:
                try:
                    return _validate_json_safe(
                        target_type, _m.group(),
                        source=f"agent:{_agent_name}:regex_extracted",
                    )
                except Exception:
                    pass
            _log2.warning(
                "Text-fallback JSON parse failed for agent '%s' (using defaults): %s",
                _agent_name, _pe,
            )
            return target_type()


async def run_data_charting(
    article: Article,
    deps: AgentDeps,
    charting_questions: list[str] | None = None,
    resolved_section_config: list[tuple[str, str, str]] | None = None,
    charting_template: ChartingTemplate | None = None,
) -> DataChartingRubric:
    """Extract data charting rubric from a single article.

    When resolved_section_config is provided, also populates rubric.section_outputs
    with per-section RubricSectionOutput entries using the configured format types.

    When charting_template is provided, injects per-field constraints into the prompt
    and validates/re-prompts out-of-vocabulary enumerated values.
    """
    import logging as _logging
    import re as _re
    model = deps.model or build_model(deps.api_key, deps.model_name)

    custom_block = ""
    if charting_questions:
        q_lines = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(charting_questions))
        custom_block = (
            f"\n\nAdditional protocol-specific questions to answer in custom_fields.\n"
            f"Key each answer by the question's identifier when it starts with one "
            f"(e.g. 'RQ1.1 What groups were compared?' → key 'RQ1.1'); otherwise use "
            f"the question text verbatim as the key:\n{q_lines}"
        )

    format_block = ""
    if resolved_section_config:
        fmt_lines = "\n".join(
            f"  {i+1}. {title} → format: {fmt}"
            for i, (_, title, fmt) in enumerate(resolved_section_config)
        )
        format_block = (
            "\n\nPer-section extraction format requirements:\n"
            + fmt_lines
            + "\n\nFormat types:\n"
            "  - descriptive: free-form narrative prose\n"
            "  - yes_no: exactly 'Yes' or 'No' optionally followed by one-sentence justification\n"
            "  - table: Markdown table with header row (e.g. '| Col | Val |')\n"
            "  - bullet_list: bulleted list, each item on own line starting with '- '\n"
            "  - numeric: numeric value only (e.g. '42' or '0.87 ± 0.03')\n"
            "For table, bullet_list, and numeric sections also produce a 1–3 sentence "
            "prose summary of that section's content.\n"
            "If the requested format cannot be produced, use descriptive prose."
        )

    field_constraint_block = ""
    if charting_template:
        constraint_lines = [
            "\n\nField-level extraction constraints:",
            "For fields with enumerated or yes_no_extended answer type you MUST select a value",
            "from the declared options list EXACTLY as written. Do not paraphrase, abbreviate,",
            "or translate the option text. If insufficient information is available, select the",
            "option that most closely means 'unknown' or 'not reported' from the list and set",
            "confidence to 'low'. Fields marked reviewer_only are excluded — do not fill them.\n",
        ]
        for section in charting_template.sections:
            extractable = [f for f in section.fields if not f.reviewer_only]
            if not extractable:
                continue
            constraint_lines.append(f"Section {section.section_key}: {section.section_title}")
            for field in extractable:
                opts = ""
                if field.options:
                    opts = ": " + " / ".join(field.options)
                constraint_lines.append(
                    f"  - {field.field_name} ({field.answer_type}{opts})"
                    f"\n    {field.description}"
                )
        field_constraint_block = "\n".join(constraint_lines)

    _charting_prompt = (
        f"Article PMID: {article.pmid}\n"
        f"Title: {article.title}\n"
        f"Authors: {article.authors}\n"
        f"Year: {article.year}\n"
        f"Journal: {article.journal}\n"
        f"DOI: {article.doi}\n"
        f"Abstract: {article.abstract[:2500]}\n"
        f"Full text: {_article_body(article, deps)}"
        + custom_block
        + format_block
        + field_constraint_block
    )
    rubric = await _run_with_text_fallback(
        data_charting_agent, _charting_prompt, DataChartingRubric, deps, model,
        step_name="data_charting", iteration_mode="zero_shot",
        target_pmid=article.pmid,
    )
    rubric.source_id = f"M-{article.pmid[-3:]}" if article.pmid.startswith("biorxiv_") else f"R-{article.pmid[-3:]}"

    # Populate section_outputs when config is provided
    if resolved_section_config:
        _log = _logging.getLogger(__name__)
        for section_key, display_title, requested_fmt in resolved_section_config:
            raw_text = _extract_section_text(rubric, section_key, display_title)
            if not raw_text.strip():
                raw_text = "Not available"

            # Validate format; fall back to descriptive if mismatch
            if requested_fmt != "descriptive" and not _validate_format(raw_text, requested_fmt):
                _log.warning(
                    "section '%s' requested '%s' but content did not match expected format; "
                    "falling back to 'descriptive'",
                    display_title, requested_fmt,
                )
                actual_fmt: SECTION_FORMAT = "descriptive"
            else:
                actual_fmt = requested_fmt  # type: ignore[assignment]

            # Build section_summary for structured formats
            summary: str | None = None
            if actual_fmt in {"table", "bullet_list", "numeric"}:
                # Use a compact prose description derived from raw_text
                lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
                summary = " ".join(lines[:3])[:300] or "See formatted answer above."

            try:
                rubric.section_outputs[display_title] = RubricSectionOutput(
                    format_used=actual_fmt,
                    formatted_answer=raw_text,
                    section_summary=summary,
                )
            except Exception as exc:
                _log.warning("Failed to build RubricSectionOutput for '%s': %s", display_title, exc)
                rubric.section_outputs[display_title] = RubricSectionOutput(
                    format_used="descriptive",
                    formatted_answer=raw_text or "Not available",
                    section_summary=None,
                )

    return rubric


async def run_narrative_row(charting: DataChartingRubric, appraisal: CriticalAppraisalRubric, deps: AgentDeps) -> PRISMANarrativeRow:
    """Generate narrative row from charting data and appraisal."""
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        narrative_row_agent,
        f"Data Charting Rubric:\n{charting.model_dump_json(indent=2)}\n\n"
        f"Critical Appraisal:\n{appraisal.model_dump_json(indent=2)}",
        deps=deps,
        model=model,
        step_name="narrative_row",
        iteration_mode="zero_shot",
        target_pmid=charting.source_id,
    )
    row = result.output
    row.source_id = charting.source_id
    return row


async def run_critical_appraisal(
    article: Article,
    charting: DataChartingRubric,
    deps: AgentDeps,
    appraisal_domains: list[str] | None = None,
    appraisal_config: CriticalAppraisalConfig | None = None,
) -> tuple[CriticalAppraisalRubric, CriticalAppraisalResult]:
    """Perform critical appraisal of a study.

    Returns both the legacy CriticalAppraisalRubric (backward compat) and the new
    CriticalAppraisalResult (list-based, stored in Methods.critical_appraisal_results).
    When appraisal_config is provided, uses its domain/item structure and derives
    domain_concern via _apply_concern_rule() in Python (not delegated to LLM).
    """
    import logging as _logging
    import json as _json
    _log = _logging.getLogger(__name__)
    model = deps.model or build_model(deps.api_key, deps.model_name)

    domain_labels = list(_DEFAULT_APPRAISAL_DOMAINS)
    if appraisal_domains:
        for i, name in enumerate(appraisal_domains[:4]):
            domain_labels[i] = name

    # Build legacy domain label block (always produced)
    domains_block = "\n".join(f"  Domain {i+1}: {name}" for i, name in enumerate(domain_labels))

    # Build structured per-domain/item prompt block when config is provided
    config_block = ""
    if appraisal_config:
        config_lines = [
            "\n\nStructured appraisal instrument — rate each item using ONLY the allowed ratings:",
        ]
        for domain in appraisal_config.domains:
            config_lines.append(f"\nDomain: {domain.domain_name} (aggregation: {domain.concern_aggregation_rule})")
            for item in domain.items:
                config_lines.append(
                    f"  - {item.item_text}\n"
                    f"    Allowed ratings: {' / '.join(item.allowed_ratings)}"
                )
        config_lines.append(
            "\nAfter rating all items, also provide an 'overall_concern' per domain "
            "(Low / Some / High) as your initial estimate — it will be overridden by "
            "the aggregation rule in post-processing."
        )
        config_block = "\n".join(config_lines)

    _appraisal_prompt = (
        f"Article: {article.title} ({article.year})\n"
        f"Study Design: {charting.study_design}\n"
        f"Sample: {charting.n_disordered} disordered, {charting.n_controls} controls\n"
        f"Data Collection: {charting.data_types} via {charting.tasks_performed}\n"
        f"Features/Models: {charting.feature_types} → {charting.model_category}\n"
        f"Performance: {charting.key_performance_results}\n"
        f"Limitations noted: {charting.reviewer_notes}\n"
        f"\nAppraisal domains to use:\n{domains_block}"
        + config_block
    )
    rubric = await _run_with_text_fallback(
        critical_appraisal_agent, _appraisal_prompt, CriticalAppraisalRubric, deps, model,
        step_name="critical_appraisal", iteration_mode="zero_shot",
        target_pmid=charting.source_id,
    )
    rubric.source_id = charting.source_id
    for domain_field, label in zip(
        [
            rubric.domain_1_participant_quality,
            rubric.domain_2_data_collection_quality,
            rubric.domain_3_feature_model_quality,
            rubric.domain_4_bias_transparency,
        ],
        domain_labels,
    ):
        domain_field.domain_name = label

    # Build CriticalAppraisalResult from appraisal_config + rubric domain items
    resolved_config = appraisal_config if appraisal_config is not None else default_appraisal_config()
    domain_appraisals: list[DomainAppraisal] = []
    legacy_domains = [
        rubric.domain_1_participant_quality,
        rubric.domain_2_data_collection_quality,
        rubric.domain_3_feature_model_quality,
        rubric.domain_4_bias_transparency,
    ]
    for idx, domain_spec in enumerate(resolved_config.domains):
        legacy_dom = legacy_domains[idx] if idx < len(legacy_domains) else None

        # Collect item ratings from legacy rubric items (best-effort match by position)
        item_ratings: list[ItemRating] = []
        if legacy_dom and legacy_dom.items:
            for item_spec, legacy_item in zip(domain_spec.items, legacy_dom.items):
                item_ratings.append(
                    ItemRating(item_text=item_spec.item_text, rating=legacy_item.rating or "Not Reported")
                )
        else:
            for item_spec in domain_spec.items:
                item_ratings.append(ItemRating(item_text=item_spec.item_text, rating="Not Reported"))

        # Apply concern rule deterministically in Python
        raw_ratings = [ir.rating for ir in item_ratings]
        concern = _apply_concern_rule(raw_ratings, domain_spec.concern_aggregation_rule)

        domain_appraisals.append(
            DomainAppraisal(
                domain_name=domain_spec.domain_name,
                item_ratings=item_ratings,
                domain_concern=concern,
            )
        )

    appraisal_result = CriticalAppraisalResult(
        source_id=charting.source_id,
        domains=domain_appraisals,
    )
    return rubric, appraisal_result


async def run_grounding_validation(
    target_excerpt: str,
    corpus_documents: dict[str, str],
    citation_map: dict[str, str],
    deps: AgentDeps,
) -> GroundingValidationResult:
    """Validate grounding of AI-generated systematic review text against source corpus."""
    model = deps.model or build_model(deps.api_key, deps.model_name)

    corpus_summary = "\n\n".join([
        f"=== {key} ===\n{text[:2000]}..."
        for key, text in list(corpus_documents.items())[:10]
    ])
    citation_list = "\n".join([f"- {key}: {desc}" for key, desc in citation_map.items()])

    prompt = (
        f"TARGET_EXCERPT to validate:\n{target_excerpt}\n\n"
        f"CITATION_MAP:\n{citation_list}\n\n"
        f"CORPUS_DOCUMENTS (excerpts):\n{corpus_summary}\n\n"
        "Validate each atomic claim in the TARGET_EXCERPT against the CORPUS_DOCUMENTS."
    )

    result = await run_traced(
        grounding_validation_agent, prompt, deps=deps, model=model,
        step_name="grounding_validation",
        iteration_mode="validated_against_source",
    )
    return result.output


async def run_introduction(deps: AgentDeps) -> str:
    """Generate Introduction section."""
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        introduction_agent, "Write the Introduction section.", deps=deps, model=model,
        step_name="introduction", iteration_mode="zero_shot",
    )
    return result.output


async def run_conclusions(synthesis: str, grade_summary: str, deps: AgentDeps) -> str:
    """Generate Conclusions section."""
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        conclusions_agent,
        f"Synthesis summary:\n{synthesis[:2000]}\n\nGRADE certainty:\n{grade_summary}",
        deps=deps,
        model=model,
        step_name="conclusions", iteration_mode="zero_shot",
    )
    return result.output


async def run_abstract(flow_text: str, synthesis: str, deps: AgentDeps) -> str:
    """Generate structured abstract."""
    p = deps.protocol
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        abstract_agent,
        f"Review: {p.title}\n"
        f"PICO: {p.pico_text}\n"
        f"Databases: {', '.join(p.databases)}\n"
        f"RoB Tool: {p.rob_tool.value}\n"
        f"Registration: {p.registration_number or 'not registered'}\n"
        f"Flow: {flow_text}\n"
        f"Key synthesis:\n{synthesis[:2000]}",
        deps=deps,
        model=model,
        step_name="structured_abstract", iteration_mode="zero_shot",
    )
    return result.output


# ────────────────────── Rich Synthesis Agents (005-rich-synthesis-output) ──────


abstract_section_agent = Agent(
    output_type=Abstract,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review writer. Generate a five-part structured abstract for a PRISMA 2020 systematic review.

Produce exactly these five fields as coherent prose paragraphs (no bullet points within fields):
- background: 1-2 sentences on the domain and why the review matters
- objective: the research question(s) and specific aims
- methods: eligibility criteria, databases searched, synthesis method — MUST mention the exact number of included studies
- results: key findings summarized from the provided themes and flow counts
- conclusion: primary interpretation and one implication, without introducing new claims

Do not fabricate statistics or citations not provided in the context.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="abstract_section",
    defer_model_check=True,
)


async def run_abstract_section(
    deps: AgentDeps,
    protocol: ReviewProtocol,
    themes: list,
    flow: PrismaFlow,
    bias_summary: str,
) -> Abstract:
    """Generate five-part structured abstract from review outcomes."""
    model = deps.model or build_model(deps.api_key, deps.model_name)
    theme_block = "\n".join(
        f"- {t.theme_name}: {'; '.join(t.key_findings[:2])}" for t in themes[:5]
    )
    result = await run_traced(
        abstract_section_agent,
        f"Review Title: {protocol.title}\n"
        f"Objective: {protocol.objective}\n"
        f"PICO: {protocol.pico_text}\n"
        f"Inclusion: {protocol.inclusion_criteria}\n"
        f"Exclusion: {protocol.exclusion_criteria}\n"
        f"Databases: {', '.join(protocol.databases)}\n"
        f"Studies included: {flow.final_included}\n"
        f"PRISMA Flow: identified={flow.total_identified}, screened={flow.screened}, "
        f"full-text reviewed={flow.full_text_reviewed}, included={flow.final_included}\n"
        f"Key themes:\n{theme_block}\n"
        f"Overall bias: {bias_summary}\n",
        deps=deps,
        model=model,
        step_name="abstract_section",
        iteration_mode="zero_shot",
    )
    return result.output


introduction_section_agent = Agent(
    output_type=Introduction,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review writer. Generate a four-section introduction for a PRISMA 2020 systematic review.

Produce exactly these four fields as coherent prose paragraphs:
- background: 1-2 paragraphs on the domain, its significance, and existing knowledge
- problem_statement: the specific gap or challenge that motivates this review
- research_gap: what is currently unknown or under-synthesized in the literature
- objectives: the specific aims of this review — must align with the provided protocol objective

Style: past tense for completed work; present for established knowledge.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="introduction_section",
    defer_model_check=True,
)


async def run_introduction_section(deps: AgentDeps, protocol: ReviewProtocol) -> Introduction:
    """Generate four-section introduction from review protocol."""
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        introduction_section_agent,
        f"Review Title: {protocol.title}\n"
        f"Objective: {protocol.objective}\n"
        f"PICO:\n{protocol.pico_text}\n"
        f"Inclusion criteria: {protocol.inclusion_criteria}\n"
        f"Exclusion criteria: {protocol.exclusion_criteria}\n"
        f"Target audience: {getattr(protocol, 'target_audience', '') or 'academic journal'}\n",
        deps=deps,
        model=model,
        step_name="introduction_section",
        iteration_mode="zero_shot",
    )
    return result.output


thematic_synthesis_agent = Agent(
    output_type=ThematicSynthesisResult,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review analyst. Perform thematic synthesis across the provided included studies.

You MUST produce:
1. themes: A list of cross-study analytical themes (minimum 2). Each theme must have:
   - theme_name: concise label
   - description: 2-3 sentence narrative
   - supporting_studies: list of source_id values from the provided articles (use ONLY provided IDs)
   - key_findings: list of 2-5 specific findings

2. bias_assessment: Cross-study risk-of-bias summary with:
   - overall_quality: prose assessment of evidence quality
   - common_biases: list of methodological limitations observed across studies
   - risk_level: exactly one of "low", "moderate", or "high"

Additionally, based on the output_style instruction at the end of the context:
- If "paragraph": populate paragraph_summary as ParagraphBlock list (each block has optional heading + text)
- If "question_answer": populate question_answer_summary as QAItem list (minimum 3 Q&A pairs covering distinct aspects)
- Otherwise: populate paragraph_summary

Do NOT fabricate source_id values. Use only the IDs present in the article list.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="thematic_synthesis",
    defer_model_check=True,
)


def _merge_thematic_results(
    partials: list[ThematicSynthesisResult],
) -> ThematicSynthesisResult:
    """Deterministically combine partial thematic syntheses into one result.

    Strategy:
      * **themes** — dedupe by ``theme_name`` (case-insensitive). When the
        same theme appears in multiple partials, union the
        ``supporting_studies`` and ``key_findings`` lists (preserving order
        of first occurrence).
      * **paragraph_summary** / **question_answer_summary** — concatenated
        in batch order.
      * **bias_assessment** — common_biases unioned, overall_quality joined
        as paragraphs, risk_level promoted to the most severe across
        partials (``high`` > ``moderate`` > ``low``).
    """
    if not partials:
        # Surface an empty-but-valid skeleton rather than failing.
        return ThematicSynthesisResult(
            themes=[],
            bias_assessment=BiasAssessment(
                overall_quality="No articles synthesised.",
                common_biases=[],
                risk_level="moderate",
            ),
        )
    if len(partials) == 1:
        return partials[0]

    # Themes — dedupe by lowercased name, union sub-lists.
    theme_index: dict[str, Theme] = {}
    for p in partials:
        for t in p.themes:
            key = t.theme_name.strip().lower()
            if key not in theme_index:
                theme_index[key] = Theme(
                    theme_name=t.theme_name,
                    description=t.description,
                    supporting_studies=list(t.supporting_studies),
                    key_findings=list(t.key_findings),
                )
                continue
            existing = theme_index[key]
            seen_studies = set(existing.supporting_studies)
            for s in t.supporting_studies:
                if s not in seen_studies:
                    existing.supporting_studies.append(s)
                    seen_studies.add(s)
            seen_findings = set(existing.key_findings)
            for f in t.key_findings:
                if f not in seen_findings:
                    existing.key_findings.append(f)
                    seen_findings.add(f)

    # Paragraph / Q&A summaries — concatenate.
    paragraphs: list = []
    qa_items: list = []
    for p in partials:
        if p.paragraph_summary:
            paragraphs.extend(p.paragraph_summary)
        if p.question_answer_summary:
            qa_items.extend(p.question_answer_summary)

    # Bias assessment — promote risk level, union biases, join quality prose.
    severity = {"low": 0, "moderate": 1, "high": 2}
    quality_lines: list[str] = []
    biases_seen: set[str] = set()
    biases_merged: list[str] = []
    worst_severity = -1
    worst_label = "moderate"
    for p in partials:
        ba = p.bias_assessment
        if ba.overall_quality:
            quality_lines.append(ba.overall_quality.strip())
        for b in ba.common_biases:
            key = b.strip().lower()
            if key and key not in biases_seen:
                biases_seen.add(key)
                biases_merged.append(b)
        sev = severity.get(ba.risk_level.strip().lower(), 1)
        if sev > worst_severity:
            worst_severity = sev
            worst_label = ba.risk_level.strip().lower()

    merged_bias = BiasAssessment(
        overall_quality="\n\n".join(quality_lines) or "Cross-batch bias summary unavailable.",
        common_biases=biases_merged,
        risk_level=worst_label if worst_label in {"low", "moderate", "high"} else "moderate",
    )

    return ThematicSynthesisResult(
        themes=list(theme_index.values()),
        paragraph_summary=paragraphs or None,
        question_answer_summary=qa_items or None,
        bias_assessment=merged_bias,
    )


async def run_thematic_synthesis(
    deps: AgentDeps,
    articles: list,
    evidence_spans: list,
    charting_rubrics: list,
    output_style: str = "paragraph",
) -> ThematicSynthesisResult:
    """Generate thematic synthesis over the full included corpus.

    Single-pass when the corpus fits ``THEMATIC_BATCH_CHARS`` of article-block
    context. For larger corpora, articles + their associated charting rubrics
    + evidence spans are sharded into char-budgeted batches, partial thematic
    syntheses run **in parallel**, and the structured partials are merged
    deterministically via :func:`_merge_thematic_results`.
    """
    if not articles:
        return _merge_thematic_results([])

    model = deps.model or build_model(deps.api_key, deps.model_name)

    pmid_to_source_id = {
        a.pmid: (
            f"M-{a.pmid[-3:]}" if a.pmid.startswith("biorxiv_")
            else f"R-{a.pmid[-3:]}"
        )
        for a in articles
    }

    def _build_prompt(
        batch_articles: list,
        batch_rubrics: list,
        batch_spans: list,
        batch_idx: int = 0,
        n_batches: int = 1,
    ) -> str:
        article_blocks = "\n\n".join(
            a.to_context_block(i) for i, a in enumerate(batch_articles)
        )
        rubric_block = "\n".join(
            f"[{r.source_id}] {r.title[:60]} — Design: {r.study_design} — "
            f"Key findings: {r.summary_key_findings[:200]}"
            for r in batch_rubrics
        )
        evidence_block = "\n".join(
            f"- {pmid_to_source_id.get(e.paper_pmid, f'PMID:{e.paper_pmid}')}: {e.text[:150]}"
            for e in batch_spans
        )
        header = (
            f"INCLUDED ARTICLES ({len(batch_articles)})"
            if n_batches == 1
            else f"INCLUDED ARTICLES (batch {batch_idx + 1} of {n_batches}, "
                 f"{len(batch_articles)} of {len(articles)} total)"
        )
        return (
            f"Research Question: {deps.protocol.question}\n\n"
            f"{header}:\n{article_blocks}\n\n"
            + (f"CHARTING SUMMARIES:\n{rubric_block}\n\n" if rubric_block else "")
            + (f"EVIDENCE SPANS:\n{evidence_block}\n\n" if evidence_block else "")
            + f"OUTPUT_STYLE: {output_style}"
        )

    blocks = [a.to_context_block(i) for i, a in enumerate(articles)]
    total_block_chars = sum(len(b) for b in blocks)

    # Single-pass when the corpus fits the budget.
    if total_block_chars <= THEMATIC_BATCH_CHARS:
        prompt = _build_prompt(articles, charting_rubrics, evidence_spans)
        result = await run_traced(
            thematic_synthesis_agent, prompt, deps=deps, model=model,
            step_name="thematic_synthesis", iteration_mode="zero_shot",
        )
        return result.output

    # Map-reduce: shard articles, distribute rubrics + spans by source_id /
    # PMID, run partial thematic syntheses in parallel, merge structurally.
    batches = _chunk_articles_by_budget(articles, blocks, THEMATIC_BATCH_CHARS)
    rubrics_by_source_id = {r.source_id: r for r in charting_rubrics}
    spans_by_pmid: dict[str, list] = {}
    for e in evidence_spans:
        spans_by_pmid.setdefault(e.paper_pmid, []).append(e)

    async def _partial(batch_idx: int, batch_articles: list) -> ThematicSynthesisResult:
        batch_source_ids = {pmid_to_source_id.get(a.pmid) for a in batch_articles}
        batch_rubrics = [
            rubrics_by_source_id[sid] for sid in batch_source_ids
            if sid and sid in rubrics_by_source_id
        ]
        batch_spans: list = []
        for a in batch_articles:
            batch_spans.extend(spans_by_pmid.get(a.pmid, []))
        prompt = _build_prompt(
            batch_articles, batch_rubrics, batch_spans,
            batch_idx=batch_idx, n_batches=len(batches),
        )
        partial_result = await run_traced(
            thematic_synthesis_agent, prompt, deps=deps, model=model,
            step_name="thematic_synthesis",
            iteration_mode="hierarchical_reduce",
            batch_index=batch_idx,
        )
        return partial_result.output

    partials = await asyncio.gather(
        *(_partial(i, b) for i, b in enumerate(batches))
    )
    return _merge_thematic_results(list(partials))


discussion_section_agent = Agent(
    output_type=Discussion,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review writer. Generate the Discussion section for a PRISMA 2020 systematic review.

Produce exactly these five fields as coherent prose paragraphs:
- summary_of_findings: concise restatement of main results from the provided themes
- interpretation: what the findings mean in context of the research question
- comparison_with_literature: how findings relate to existing knowledge (cite only sources in the review corpus)
- implications: three distinct implications — clinical, policy, and research (each a complete sentence)
- limitations: methodological and evidence limitations from the provided limitations text and themes
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="discussion_section",
    defer_model_check=True,
)


async def run_discussion_section(
    deps: AgentDeps,
    protocol: ReviewProtocol,
    themes: list,
    limitations_text: str,
) -> Discussion:
    """Generate interpretive discussion from themes and protocol context."""
    model = deps.model or build_model(deps.api_key, deps.model_name)
    theme_block = "\n".join(
        f"- {t.theme_name}: {t.description} Key findings: {'; '.join(t.key_findings[:3])}"
        for t in themes[:6]
    )
    result = await run_traced(
        discussion_section_agent,
        f"Research Question: {protocol.question}\n"
        f"PICO: {protocol.pico_text}\n\n"
        f"THEMES FROM SYNTHESIS:\n{theme_block}\n\n"
        f"LIMITATIONS: {limitations_text or 'Not specified'}\n",
        deps=deps,
        model=model,
        step_name="discussion_section", iteration_mode="zero_shot",
    )
    return result.output


conclusion_section_agent = Agent(
    output_type=Conclusion,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review writer. Generate the Conclusion section for a PRISMA 2020 systematic review.

Produce exactly these three fields as coherent prose paragraphs:
- key_takeaways: 2-3 concise sentences summarizing the main findings
- recommendations: actionable guidance derived from the findings — MUST contain action verbs such as "should", "must", or "recommend"
- future_research: specific open questions and next steps for future investigation

Do not introduce new claims not supported by the provided themes. Do not overstate certainty.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="conclusion_section",
    defer_model_check=True,
)


async def run_conclusion_section(
    deps: AgentDeps,
    protocol: ReviewProtocol,
    themes: list,
) -> Conclusion:
    """Generate terminal synthesis from themes and protocol objectives."""
    model = deps.model or build_model(deps.api_key, deps.model_name)
    theme_block = "\n".join(
        f"- {t.theme_name}: {'; '.join(t.key_findings[:2])}" for t in themes[:6]
    )
    result = await run_traced(
        conclusion_section_agent,
        f"Research Question: {protocol.question}\n"
        f"Objectives: {protocol.objective}\n\n"
        f"KEY THEMES AND FINDINGS:\n{theme_block}\n",
        deps=deps,
        model=model,
        step_name="conclusion_section", iteration_mode="zero_shot",
    )
    return result.output


quantitative_analysis_agent = Agent(
    output_type=QuantitativeAnalysis,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review methodologist. Synthesize numeric outcome data from the provided studies.

Produce these three fields — set to null if data is insufficient or unavailable:
- effect_size: describe the pooled or representative effect size(s) with direction and magnitude
- confidence_intervals: the confidence interval(s) for the key effect(s)
- heterogeneity: between-study variability (I², tau², or qualitative description)

IMPORTANT: Do NOT fabricate numeric values. Only report what is directly derivable from the provided data.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="quantitative_analysis",
    defer_model_check=True,
)


async def run_quantitative_analysis(
    deps: AgentDeps,
    articles: list,
) -> QuantitativeAnalysis | None:
    """Generate quantitative synthesis when ≥3 articles have numeric outcome data."""
    quantitative = [
        a for a in articles
        if a.extracted_data and a.extracted_data.effect_measures
    ]
    if len(quantitative) < 3:
        return None

    model = deps.model or build_model(deps.api_key, deps.model_name)
    data_block = "\n".join(
        f"[{a.pmid}] {a.title[:60]}: effects={'; '.join(a.extracted_data.effect_measures[:3])}; "
        f"findings={'; '.join(a.extracted_data.key_findings[:2])}"
        for a in quantitative[:10]
    )
    result = await run_traced(
        quantitative_analysis_agent,
        f"Research Question: {deps.protocol.question}\n\n"
        f"NUMERIC OUTCOME DATA ({len(quantitative)} studies):\n{data_block}\n",
        deps=deps,
        model=model,
        step_name="quantitative_analysis", iteration_mode="zero_shot",
    )
    return result.output


def build_quality_checklist(result) -> dict[str, bool]:
    """Run the pre-delivery quality checklist."""
    p = result.protocol
    f = result.flow
    title_lower = (p.title or "").lower()
    return {
        "title_identifies_as_sr": "systematic review" in title_lower,
        "abstract_structured": bool(result.structured_abstract),
        "research_question_stated": bool(p.question),
        "pico_complete": bool(p.pico_population and p.pico_outcome),
        "prisma_flow_numbers_present": f.total_identified > 0,
        "flow_reconciles": (
            f.after_dedup == f.total_identified - f.duplicates_removed
            and f.included_synthesis
            == f.after_dedup - f.excluded_title_abstract - f.excluded_eligibility
        ),
        "every_study_charted": len(result.data_charting_rubrics) == f.included_synthesis,
        "every_study_appraised": len(result.critical_appraisals) == f.included_synthesis,
        "rob_tool_matches_design": bool(p.rob_tool),
        "grade_reported": len(result.grade_assessments) > 0,
        "limitations_two_levels": bool(result.limitations),
        "conclusions_present": bool(result.conclusions_text),
        "registration_declared": True,
        "funding_declared": True,
        "introduction_present": bool(result.introduction_text),
        "citation_style_set": bool(getattr(p, "citation_style", "")),
    }


# ──────────────────── Feature 006: Bridge2AI Factory Functions ────────────────


def default_charting_template() -> ChartingTemplate:
    """Return the full Bridge2AI data charting template (Sections A–G, 60 fields).

    Pure and deterministic — same output on every call.
    """
    def _f(name: str, desc: str, atype: str, opts=None, reviewer_only: bool = False) -> FieldDefinition:
        return FieldDefinition(
            field_name=name, description=desc, answer_type=atype,
            options=opts, reviewer_only=reviewer_only,
        )

    YNR = ["Yes", "No", "Not Reported"]
    YNA = ["Yes", "No", "N/A"]

    sections = [
        ChartingSection(
            section_key="A",
            section_title="Publication Information",
            fields=[
                _f("Source ID", "Unique ID assigned by reviewers (e.g., M-001, R-001, N-001)", "free_text"),
                _f("Title", "Full title of the paper", "free_text"),
                _f("Authors", "Last name, First initial", "free_text"),
                _f("Year", "Publication year", "numeric"),
                _f("Journal / Conference", "Full publication venue name", "free_text"),
                _f("DOI", "Digital Object Identifier", "free_text"),
                _f("Database Retrieved From", "Where source was found (e.g. PubMed, Scopus)", "free_text"),
                _f("Disorder Cohort", "Which of the five Bridge2AI cohorts", "free_text"),
                _f("Primary Focus", "Disorder-focused or technology-focused", "enumerated",
                   opts=["Disorder-focused", "Technology-focused"]),
            ],
        ),
        ChartingSection(
            section_key="B",
            section_title="Study Design",
            fields=[
                _f("Primary Study Goal", "What the study set out to do", "enumerated",
                   opts=["Classification", "Severity assessment", "Feature identification", "Other"]),
                _f("Study Design", "Overall design", "enumerated",
                   opts=["Cross-sectional", "Longitudinal", "Not Reported"]),
                _f("If Longitudinal: Duration", "How long participants were followed (e.g. 6 months)", "free_text"),
                _f("If Longitudinal: Frequency", "How often data were collected (e.g. weekly)", "free_text"),
                _f("Subject Model", "How comparisons were structured", "enumerated",
                   opts=["Within-subjects", "Between-subjects", "Mixed", "Not Reported"]),
                _f("Task Type", "Nature of the modeling task", "enumerated",
                   opts=["Classification", "Regression", "Both", "Not Reported"]),
                _f("Study Setting", "Where data were collected", "enumerated",
                   opts=["Clinical", "Lab", "Remote/home", "Other", "Not Reported"]),
                _f("Country / Region", "Where the study was conducted", "free_text"),
            ],
        ),
        ChartingSection(
            section_key="C",
            section_title="Participants: Disordered Group",
            fields=[
                _f("Disorder / Diagnosis", "Specific condition studied", "free_text"),
                _f("How Diagnosis Was Assessed", "Criteria or tool used (e.g. MDS-UPDRS, DSM-5)", "free_text"),
                _f("N (Disordered)", "Number of participants in disordered group", "numeric"),
                _f("Age — Mean (SD)", "Mean age and standard deviation", "free_text"),
                _f("Age — Range", "Age range", "free_text"),
                _f("Gender Distribution", "Gender distribution (e.g. 60% female)", "free_text"),
                _f("Comorbidities — Included or Excluded",
                   "Whether comorbid conditions were included or excluded from the study",
                   "enumerated", opts=["Included", "Excluded", "Not Reported"]),
                _f("Which Comorbidities Addressed",
                   "Which comorbidities were included or excluded, if reported", "free_text"),
                _f("Medications / Therapies",
                   "Were participants on treatment at time of data collection?",
                   "yes_no_extended", opts=YNR),
                _f("If Yes — What Medications / Therapies",
                   "Details of medications or therapies if reported", "free_text"),
                _f("Severity Levels Included",
                   "Range of disorder severity represented in the sample",
                   "enumerated", opts=["Mild", "Moderate", "Severe", "Mixed", "Not Reported"]),
            ],
        ),
        ChartingSection(
            section_key="D",
            section_title="Participants: Healthy Controls",
            fields=[
                _f("Healthy Controls Included",
                   "Were healthy control participants included?",
                   "yes_no_extended", opts=YNR),
                _f("How Healthy Status Was Confirmed",
                   "Criteria or method to confirm no relevant diagnosis", "free_text"),
                _f("N (Healthy)", "Number of healthy control participants", "numeric"),
                _f("Age — Mean (SD)", "Mean age and standard deviation for controls", "free_text"),
                _f("Age — Range", "Age range for controls", "free_text"),
                _f("Gender Distribution", "Gender distribution for controls", "free_text"),
                _f("Age-Matched to Disordered Group",
                   "Were healthy controls matched on age?",
                   "yes_no_extended", opts=YNR),
                _f("Gender-Matched to Disordered Group",
                   "Were healthy controls matched on gender?",
                   "yes_no_extended", opts=YNR),
                _f("Neurodevelopmentally Typical",
                   "Were healthy controls confirmed as neurodevelopmentally typical?",
                   "yes_no_extended", opts=YNR),
            ],
        ),
        ChartingSection(
            section_key="E",
            section_title="Data Collection",
            fields=[
                _f("Data Types Collected",
                   "Modalities used (audio, video, text, physiological, etc.)", "free_text"),
                _f("Tasks Performed",
                   "What participants were asked to do", "free_text"),
                _f("Equipment / Tools Used",
                   "Hardware or software (e.g. specific microphone, app)", "free_text"),
                _f("New Dataset Contributed",
                   "Does the paper introduce a new dataset?",
                   "yes_no_extended", opts=YNR),
                _f("Dataset Openly Available",
                   "Is the dataset openly available?",
                   "yes_no_extended", opts=YNR),
                _f("Dataset Available on Request",
                   "Is the dataset available on request?",
                   "yes_no_extended", opts=YNR),
                _f("Sensitive Data Anonymized",
                   "Was sensitive data anonymized?",
                   "yes_no_extended", opts=YNR),
            ],
        ),
        ChartingSection(
            section_key="F",
            section_title="Features and Models",
            fields=[
                _f("Feature Types Extracted",
                   "Broad category of features", "enumerated",
                   opts=["Acoustic", "Linguistic", "Articulatory", "DNN embeddings", "Combination"]),
                _f("Specific Features Reported",
                   "List key features (e.g. MFCCs, jitter, shimmer, F0, HNR)", "free_text"),
                _f("Feature Extraction Tools / Libraries",
                   "Software used (e.g. openSMILE, torchaudio, librosa)", "free_text"),
                _f("Feature Importance Reported",
                   "Did the paper identify top features?",
                   "yes_no_extended", opts=YNR),
                _f("Feature Importance Method",
                   "If yes, what method was used (e.g. SHAP, permutation importance)", "free_text"),
                _f("Top Features Identified",
                   "If reported, list the top features", "free_text"),
                _f("Direction of Feature Change",
                   "Increase or decrease relative to healthy controls or severity",
                   "enumerated", opts=["Increase", "Decrease", "Mixed", "Not Reported"]),
                _f("Model Category",
                   "Broad model type", "enumerated",
                   opts=["Statistical", "Classical ML", "Deep learning", "Not Reported"]),
                _f("Specific Algorithms Used",
                   "e.g. SVM, Random Forest, LSTM, wav2vec", "free_text"),
                _f("Validation Methodology",
                   "How model was evaluated", "enumerated",
                   opts=["Train/test split", "k-fold CV", "LOOCV", "Held-out test set", "Not Reported"]),
                _f("Performance Metrics Reported",
                   "e.g. Accuracy, AUC, F1, RMSE, R²", "free_text"),
                _f("Key Performance Results",
                   "Headline numbers as reported (e.g. AUC = 0.89)", "free_text"),
            ],
        ),
        ChartingSection(
            section_key="G",
            section_title="Synthesis Fields",
            fields=[
                _f("Summary of Key Findings",
                   "1–2 sentence summary of the study's main contribution",
                   "free_text", reviewer_only=True),
                _f("Features Most Associated With Disorder",
                   "Key features identified as most relevant",
                   "free_text", reviewer_only=True),
                _f("Future Directions Recommended by Authors",
                   "What the authors suggest for future work",
                   "free_text", reviewer_only=True),
                _f("Reviewer Notes",
                   "Flags or observations for team discussion",
                   "free_text", reviewer_only=True),
            ],
        ),
    ]
    return ChartingTemplate(sections=sections)


def default_appraisal_config() -> CriticalAppraisalConfig:
    """Return the four-domain Bridge2AI critical appraisal instrument.

    Pure and deterministic — same output on every call.
    """
    def _item(text: str, ratings: list[str]) -> AppraisalItemSpec:
        return AppraisalItemSpec(item_text=text, allowed_ratings=ratings)

    _YPNR = ["Yes", "Partial", "No", "Not Reported"]
    _YPNA = ["Yes", "Partial", "No", "N/A"]

    return CriticalAppraisalConfig(
        domains=[
            AppraisalDomainSpec(
                domain_name="Sample Quality",
                concern_aggregation_rule="majority_yes",
                items=[
                    _item("Was sample size adequate and reported?", _YPNR),
                    _item("Were demographic characteristics reported?", _YPNR),
                    _item("Was diagnosis verified by a validated clinical measure?", _YPNR),
                    _item("Were comorbidities assessed and reported?", _YPNR),
                    _item("Were healthy controls included and appropriately matched?", _YPNA),
                ],
            ),
            AppraisalDomainSpec(
                domain_name="Data Collection Quality",
                concern_aggregation_rule="majority_yes",
                items=[
                    _item("Was the recording setup described in sufficient detail?", _YPNR),
                    _item("Were tasks clearly defined and standardized?", _YPNR),
                    _item("Was data collected in a controlled or described setting?", _YPNR),
                ],
            ),
            AppraisalDomainSpec(
                domain_name="Feature and Model Quality",
                concern_aggregation_rule="majority_yes",
                items=[
                    _item("Were features clearly defined and justified?", _YPNR),
                    _item("Was feature selection methodology described?", _YPNR),
                    _item("Was the model clearly described and reproducible?", _YPNR),
                    _item("Was an appropriate validation strategy used?", _YPNR),
                    _item("Were performance metrics appropriate for the task?", _YPNR),
                ],
            ),
            AppraisalDomainSpec(
                domain_name="Bias and Transparency",
                concern_aggregation_rule="majority_yes",
                items=[
                    _item("Was class imbalance acknowledged and addressed?", _YPNA),
                    _item("Were study limitations discussed by authors?", _YPNR),
                    _item("Was feature importance or interpretability reported?", _YPNR),
                    _item("Is the dataset or code publicly available?", _YPNR),
                ],
            ),
        ]
    )


# ────────────────── Feature 007: Consensus Synthesis Agent ─────────────


class ConsensusSynthesisOutput(BaseModel):
    """Typed output from the consensus synthesis agent."""
    consensus_text: str
    divergences: list[SynthesisDivergence]

    @model_validator(mode="after")
    def _filter_incomplete_divergences(self) -> "ConsensusSynthesisOutput":
        self.divergences = [d for d in self.divergences if len(d.positions) >= 2]
        return self


consensus_synthesis_agent = Agent(
    output_type=ConsensusSynthesisOutput,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review expert synthesising findings from multiple AI models.
Given per-model synthesis texts for the same review protocol, identify:
1. Claims present across ALL models (agreed findings) — summarise in flowing prose.
2. Significant divergences where models reach materially different conclusions — list each as a topic with per-model positions.

Rules:
- Be specific: quote or closely paraphrase when noting divergences.
- Only include divergences that would change a reviewer's interpretation of the evidence.
- consensus_text should read as coherent prose, not a list.
- divergences list may be empty if models substantially agree.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="consensus_synthesis",
    defer_model_check=True,
)


async def run_consensus_synthesis(
    syntheses: dict[str, str],
    deps: AgentDeps,
) -> ConsensusSynthesisOutput:
    """Generate consensus synthesis from {model_name: synthesis_text} dict."""
    lines = ["## Per-Model Synthesis Texts\n"]
    for model_name, text in syntheses.items():
        lines.append(f"### {model_name}\n{text[:3000]}\n")
    lines.append(
        "\n## Task\n"
        "Synthesise the above into agreed findings (consensus_text) and "
        "list material divergences (divergences)."
    )
    prompt = "\n".join(lines)
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        consensus_synthesis_agent, prompt, deps=deps, model=model,
        step_name="consensus_synthesis",
        iteration_mode="hierarchical_reduce",
    )
    return result.output


# ────────────────── Feature 010: Synthesis Merge Agent ──────────────────────


class MergedSynthesisOutput(BaseModel):
    """Merged output from the synthesis merge agent."""
    synthesis_text: str


_synthesis_merge_agent = Agent(
    output_type=MergedSynthesisOutput,
    deps_type=AgentDeps,
    system_prompt="""\
You are a systematic review expert. You receive multiple partial synthesis texts,
each covering a different subset of included articles from the same review.

Your task: merge them into one coherent, deduplicated narrative synthesis that:
- Covers all articles across all partial texts
- Eliminates redundancy (do not repeat the same finding multiple times)
- Maintains academic prose appropriate for a PRISMA 2020 systematic review
- Preserves all unique findings, effect estimates, and conclusions from every partial text
- Does NOT invent findings not present in the partial texts

Return the merged text in synthesis_text. Length should reflect the total evidence,
not just one chunk.
""",
    retries=AGENT_VALIDATION_RETRIES,
    name="synthesis_merge",
    defer_model_check=True,
)


async def run_synthesis_merge_agent(
    partial_syntheses: list[str],
    deps: AgentDeps,
) -> str:
    """Merge N partial synthesis texts into one coherent narrative.

    Used by the iterative pipeline when synthesis is split across multiple chunks.
    Returns the merged synthesis as a plain string (same type as run_synthesis output).
    """
    if not partial_syntheses:
        return ""
    if len(partial_syntheses) == 1:
        return partial_syntheses[0]

    lines = [f"## {len(partial_syntheses)} Partial Synthesis Texts to Merge\n"]
    for i, text in enumerate(partial_syntheses, 1):
        lines.append(f"### Chunk {i} of {len(partial_syntheses)}\n{text}\n")
    lines.append(
        "\n## Task\n"
        "Merge the above partial synthesis texts into one comprehensive, "
        "deduplicated narrative synthesis covering all chunks."
    )
    prompt = "\n".join(lines)
    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        _synthesis_merge_agent, prompt, deps=deps, model=model,
        step_name="synthesis_merge",
        iteration_mode="hierarchical_reduce",
    )
    return result.output.synthesis_text



# ────────────────────── 17. Search-Result Synthesis Agent ────────────────────
#
# Used by the standalone search surface (CLI / FastAPI) to produce a
# stratified summary of search hits. Distinct from `synthesis_agent` (which
# is corpus-wide and PRISMA-flow-anchored) — this one operates on a
# pre-filtered shortlist returned by ArticleStore.search_*().

search_synthesis_agent = Agent(
    output_type=SearchSynthesis,
    deps_type=AgentDeps,
    system_prompt="""\
You are a clinical/biomedical synthesis assistant. You receive (a) a free-text
search query and (b) a shortlist of articles returned for that query. Each
article block contains the title and abstract; many also carry a full-text
excerpt and pre-extracted key findings — use those richer fields when present
to ground numeric claims. Produce a structured stratified summary.

Rules:
  1. Detect the most informative grouping dimension yourself — typically
     condition / disorder / disease when results span multiple disease
     areas, otherwise population subgroup, intervention type, study design,
     or outcome metric. State the chosen dimension implicitly through the
     group labels.
  2. Each group's `aggregate_finding` MUST cite specific quantitative values
     when present in the source articles: effect sizes (with units),
     accuracy / sensitivity / specificity / AUC, sample sizes, ranges,
     confidence intervals. Prefer numbers from the full-text excerpt over
     the abstract when both are present. Do NOT invent numbers.
  3. `n_studies` per group should reflect actual articles in the shortlist
     that fall in that group. Sum across groups should equal
     n_articles_synthesized (allow a small overflow when an article spans
     two groups — note such overlaps in `caveats`).
  4. `representative_pmids` lists 1–3 PMIDs from the shortlist that are most
     representative of the group's conclusion.
  5. `overview` is 1–2 sentences framing the corpus: predominant study
     designs, settings, sample size range, data collection era. No findings.
  6. `overall_caveats` summarises cross-group heterogeneity, missing
     comparators, demographic narrowness, etc. Concise; use only when
     genuinely warranted.

Never fabricate findings beyond what the shortlist supplies. When a group has
insufficient evidence to summarise quantitatively, say so explicitly.
""",
    retries=3,
    name="search_synthesis",
    defer_model_check=True,
)


# Per-article full-text excerpt budget for the search-synthesis prompt block.
# 2 KB captures Methods + key Results paragraphs for most papers without
# blowing the per-call context budget when a bucket has 10–25 articles.
_SEARCH_FULL_TEXT_CHARS = 2000


def _summarise_article_for_search(art: "Article", index: int) -> str:
    """Compact one-article block used as input to the search-synthesis agent.

    Renders whatever is most informative for aggregate findings, in order:
    title + year, abstract, full-text excerpt (when resolved), and any
    pre-extracted key findings. Abstract-only articles (e.g. fresh search
    hits that have not been through the full-text resolver) degrade
    gracefully — the helper just omits the missing pieces.
    """
    parts = [
        f"--- [{index}] PMID:{art.pmid} ---",
        f"Title: {art.title}",
    ]
    if art.year:
        parts.append(f"Year: {art.year}")
    if art.abstract:
        parts.append(f"Abstract: {art.abstract[:1500]}")
    if art.full_text:
        parts.append(
            f"Full-text excerpt: {art.full_text[:_SEARCH_FULL_TEXT_CHARS]}"
        )
    findings = getattr(getattr(art, "extracted_data", None), "key_findings", None)
    if findings:
        joined = "; ".join(findings[:5])
        parts.append(f"Pre-extracted findings: {joined}")
    return "\n".join(parts)


async def run_search_synthesis(
    query: str,
    articles: "list[Article]",
    deps: AgentDeps,
    top_k: int | None = None,
) -> SearchSynthesis:
    """Produce a stratified summary of a search-result shortlist.

    Single LLM call. Use ``top_k`` to cap the corpus when search returned
    a long list (default: use whatever is supplied). The agent's output
    length grows roughly linearly with article count, so 10–25 is a
    practical sweet spot for the CLI / REST surface.
    """
    if not articles:
        return SearchSynthesis(
            query=query,
            n_articles_synthesized=0,
            overview="No matching articles in the corpus.",
            groups=[],
        )

    if top_k is not None and top_k > 0:
        articles = articles[:top_k]

    blocks = [_summarise_article_for_search(a, i + 1) for i, a in enumerate(articles)]
    prompt = (
        f"Search query: {query}\n\n"
        f"=== SHORTLIST ({len(articles)} articles, ranked by relevance) ===\n\n"
        + "\n\n".join(blocks)
    )

    model = deps.model or build_model(deps.api_key, deps.model_name)
    result = await run_traced(
        search_synthesis_agent, prompt, deps=deps, model=model,
        step_name="search_synthesis",
        iteration_mode="zero_shot",
    )
    return result.output


# ────────────────── Per-Disorder Synthesis (deterministic bucketing) ───
#
# Unlike ``search_synthesis_agent`` — which lets the LLM pick a grouping
# dimension — this flow buckets articles deterministically by a
# caller-supplied disorder label and produces one summary per non-empty
# bucket. Use this when you already have charted ``disorder_cohort`` values
# (or any externally-curated disorder mapping) and want strict, reproducible
# per-disorder strata rather than soft, model-chosen groups.

disorder_group_summary_agent = Agent(
    output_type=GroupSummary,
    deps_type=AgentDeps,
    system_prompt="""\
You are a clinical/biomedical synthesis assistant. You receive (a) a single
disorder/cohort label and (b) a shortlist of articles whose disorder cohort
matches that label. Each article block contains the title and abstract; many
also carry a full-text excerpt and pre-extracted key findings — use those
richer fields when present to ground numeric claims. Produce ONE structured
summary (a single GroupSummary) covering this disorder.

Rules:
  1. The output `label` MUST equal the disorder label provided in the prompt
     (verbatim). Do not relabel, abbreviate, or expand it.
  2. `n_studies` MUST equal the number of articles in the shortlist.
  3. `aggregate_finding` is 1–3 sentences synthesising what the shortlist
     reports for this disorder. Cite specific quantitative values when the
     source articles supply them: effect sizes (with units), accuracy /
     sensitivity / specificity / AUC, sample sizes, ranges, confidence
     intervals. Prefer numbers from the full-text excerpt over the abstract
     when both are present. Never invent numbers.
  4. `representative_pmids` lists 1–3 PMIDs from the shortlist that are most
     representative of the conclusion.
  5. `caveats` summarises within-disorder limitations (small N, narrow
     demographics, methodological heterogeneity, missing comparator).
     Concise; use only when genuinely warranted.

Do not introduce sub-groups or alternative grouping dimensions — this call
covers exactly one disorder.
""",
    retries=3,
    name="disorder_group_summary",
    defer_model_check=True,
)


def _normalize_disorder_label(label: str) -> str:
    """Canonicalise a disorder label for bucketing: trim + collapse whitespace + casefold."""
    return " ".join((label or "").split()).casefold()


def labels_from_rubrics(
    rubrics: "list[DataChartingRubric]",
    label_attr: str,
    *,
    pmid_attr: str = "source_id",
) -> dict[str, str]:
    """Build a ``{pmid: <rubric.label_attr>}`` mapping from charted rubrics.

    Empty / whitespace-only attribute values are omitted so they surface
    as ``unlabeled_count`` in the downstream per-group driver. Raises
    ``AttributeError`` if ``label_attr`` does not exist on the first
    non-empty rubric — protects callers from silently mistyped dimensions.
    """
    if not rubrics:
        return {}
    if not hasattr(rubrics[0], label_attr):
        raise AttributeError(
            f"DataChartingRubric has no attribute {label_attr!r}; "
            "supply a valid rubric field name (e.g. 'disorder_cohort', "
            "'study_design', 'country_region', 'task_type')."
        )
    out: dict[str, str] = {}
    for r in rubrics:
        key = getattr(r, pmid_attr, "") or ""
        raw = (getattr(r, label_attr, "") or "").strip()
        if key and raw:
            out[key] = raw
    return out


def disorder_labels_from_rubrics(
    rubrics: "list[DataChartingRubric]",
    *,
    pmid_attr: str = "source_id",
) -> dict[str, str]:
    """Back-compat shim — equivalent to ``labels_from_rubrics(rubrics, 'disorder_cohort')``."""
    return labels_from_rubrics(rubrics, "disorder_cohort", pmid_attr=pmid_attr)


async def run_per_disorder_synthesis(
    articles: "list[Article]",
    disorder_labels: dict[str, str],
    deps: AgentDeps,
    *,
    topic: str = "",
    min_articles_per_disorder: int = 1,
) -> PerDisorderSynthesis:
    """Strict per-disorder stratified summary via deterministic bucketing.

    For each distinct disorder label in ``disorder_labels``, articles whose
    ``pmid`` maps to that label are grouped together (case-insensitive,
    whitespace-collapsed match) and summarised in a single LLM call. Buckets
    smaller than ``min_articles_per_disorder`` are skipped — the articles
    still count toward ``n_articles_synthesized`` but produce no group.
    Articles missing from ``disorder_labels`` (or mapped to an empty label)
    are excluded from synthesis and counted in ``unlabeled_count``.

    The output preserves the *original-cased* label string from the first
    article encountered for that bucket — normalisation is used only for
    grouping, never for display.
    """
    if not articles:
        return PerDisorderSynthesis(
            topic=topic,
            n_articles_synthesized=0,
            n_disorders=0,
            unlabeled_count=0,
            groups=[],
        )

    buckets: dict[str, list["Article"]] = {}
    display_label: dict[str, str] = {}
    unlabeled = 0
    for art in articles:
        raw = (disorder_labels.get(art.pmid) or "").strip()
        if not raw:
            unlabeled += 1
            continue
        key = _normalize_disorder_label(raw)
        if not key:
            unlabeled += 1
            continue
        buckets.setdefault(key, []).append(art)
        display_label.setdefault(key, raw)

    eligible_keys = [
        k for k, arts in buckets.items() if len(arts) >= min_articles_per_disorder
    ]

    model = deps.model or build_model(deps.api_key, deps.model_name)

    async def _summarise_one(key: str) -> GroupSummary:
        bucket = buckets[key]
        label = display_label[key]
        blocks = [_summarise_article_for_search(a, i + 1) for i, a in enumerate(bucket)]
        prompt = (
            f"Disorder label: {label}\n"
            f"Topic context: {topic or '(none)'}\n\n"
            f"=== SHORTLIST ({len(bucket)} articles for this disorder) ===\n\n"
            + "\n\n".join(blocks)
        )
        result = await run_traced(
            disorder_group_summary_agent, prompt, deps=deps, model=model,
            step_name="per_disorder_synthesis",
            iteration_mode="zero_shot",
        )
        gs: GroupSummary = result.output
        # Normalise label and counts deterministically — ignore any LLM drift.
        return gs.model_copy(update={"label": label, "n_studies": len(bucket)})

    summaries: list[GroupSummary] = []
    if eligible_keys:
        summaries = list(
            await asyncio.gather(*(_summarise_one(k) for k in eligible_keys))
        )
        summaries.sort(key=lambda g: (-g.n_studies, g.label.casefold()))

    return PerDisorderSynthesis(
        topic=topic,
        n_articles_synthesized=len(articles),
        n_disorders=len(summaries),
        unlabeled_count=unlabeled,
        groups=summaries,
    )


# ────────────────── Per-Group Analysis (synthesis + Q&A per bucket) ──────
#
# Generalises ``run_per_disorder_synthesis`` into a configurable bucket-by-
# any-rubric-field flow with optional per-group Q&A. The dimension is a
# string naming a ``DataChartingRubric`` attribute (e.g. ``disorder_cohort``,
# ``study_design``, ``country_region``). Questions can be supplied
# per-group (override) or as a default list applied to every group without
# explicit overrides. ``run_per_disorder_synthesis`` becomes a thin shim
# over this when called via ``run_per_group_analysis(dimension="disorder_cohort", ...)``.

# Hard cap mirrored from ReviewProtocol's validator. Safety belt at the
# driver level so callers using the agent directly (without going through
# ReviewProtocol validation) still get the bound.
_MAX_QUESTIONS_PER_GROUP = 10

group_question_answer_agent = Agent(
    output_type=GroupQuestionAnswer,
    deps_type=AgentDeps,
    system_prompt="""\
You are a clinical/biomedical synthesis assistant. You receive (a) a single
group label, (b) one specific question to answer for this group, and (c) a
shortlist of articles whose group attribute matches this label. Each article
block contains the title and abstract; many also carry a full-text excerpt
and pre-extracted key findings — use those richer fields when present to
ground your answer.

Rules:
  1. The output `question` MUST equal the question text from the prompt
     (verbatim — do not paraphrase or expand it).
  2. `answer` is a 1–4 sentence response grounded in the shortlist. Cite
     specific quantitative values when the source articles supply them
     (effect sizes with units, accuracy / sensitivity / specificity / AUC,
     sample sizes, ranges, confidence intervals). Prefer numbers from the
     full-text excerpt over the abstract when both are present. If the
     shortlist genuinely cannot answer the question, the answer field MUST
     start with "Insufficient evidence" and briefly say what is missing
     (e.g. "Insufficient evidence — none of the 3 included studies report
     gender breakdown.").
  3. `supporting_pmids` lists 0–5 PMIDs from the shortlist that ground the
     answer. Empty list is acceptable when the answer is "Insufficient
     evidence".
  4. `caveats` is a short note when the answer applies only to a subset of
     the group's articles, or when the evidence is mixed (e.g. "Two of five
     studies reported conflicting effect directions.").

Never invent numbers. Never speculate beyond the shortlist.
""",
    retries=3,
    name="group_question_answer",
    defer_model_check=True,
)


async def run_per_group_analysis(
    articles: "list[Article]",
    rubrics: "list[DataChartingRubric]",
    deps: AgentDeps,
    *,
    dimension: str = "disorder_cohort",
    default_questions: "list[str] | None" = None,
    per_group_questions: "dict[str, list[str]] | None" = None,
    topic: str = "",
    min_per_group: int = 1,
) -> PerGroupAnalysis:
    """Run synthesis + per-question Q&A for every non-empty group bucket.

    ``dimension`` names a ``DataChartingRubric`` string-typed attribute —
    articles are bucketed deterministically by it (case-insensitive,
    whitespace-collapsed). Each non-empty bucket produces one
    ``GroupAnalysisEntry`` containing (a) a ``GroupSummary`` from the
    existing per-disorder synthesis agent and (b) one ``GroupQuestionAnswer``
    per question. Questions resolve as
    ``per_group_questions.get(label, default_questions)`` (case-insensitive
    label match), each bound to ``_MAX_QUESTIONS_PER_GROUP`` to keep cost
    predictable. Synthesis + all answers run concurrently across all
    eligible buckets.
    """
    default_questions = list(default_questions or [])
    per_group_questions = dict(per_group_questions or {})

    if len(default_questions) > _MAX_QUESTIONS_PER_GROUP:
        raise ValueError(
            f"default_questions: max {_MAX_QUESTIONS_PER_GROUP} questions "
            f"(got {len(default_questions)})"
        )
    for k, qs in per_group_questions.items():
        if len(qs) > _MAX_QUESTIONS_PER_GROUP:
            raise ValueError(
                f"per_group_questions[{k!r}]: max {_MAX_QUESTIONS_PER_GROUP} "
                f"questions (got {len(qs)})"
            )

    if not articles:
        return PerGroupAnalysis(
            dimension=dimension, topic=topic,
            n_articles_synthesized=0, n_groups=0,
            unlabeled_count=0, groups=[],
        )

    labels = labels_from_rubrics(rubrics, dimension)

    # Deterministic bucketing — same logic as run_per_disorder_synthesis.
    buckets: dict[str, list["Article"]] = {}
    display_label: dict[str, str] = {}
    unlabeled = 0
    for art in articles:
        raw = (labels.get(art.pmid) or "").strip()
        if not raw:
            unlabeled += 1
            continue
        key = _normalize_disorder_label(raw)
        if not key:
            unlabeled += 1
            continue
        buckets.setdefault(key, []).append(art)
        display_label.setdefault(key, raw)

    eligible_keys = [
        k for k, arts in buckets.items() if len(arts) >= min_per_group
    ]

    # Case-insensitive lookup for per-group overrides (callers may write
    # the label in any casing — match it against the bucket's normalised key).
    per_group_lookup: dict[str, list[str]] = {
        _normalize_disorder_label(k): list(qs)
        for k, qs in per_group_questions.items()
    }

    model = deps.model or build_model(deps.api_key, deps.model_name)

    async def _summarise_bucket(key: str) -> GroupSummary:
        bucket = buckets[key]
        label = display_label[key]
        blocks = [_summarise_article_for_search(a, i + 1) for i, a in enumerate(bucket)]
        prompt = (
            f"Disorder label: {label}\n"
            f"Topic context: {topic or '(none)'}\n\n"
            f"=== SHORTLIST ({len(bucket)} articles for this group) ===\n\n"
            + "\n\n".join(blocks)
        )
        result = await run_traced(
            disorder_group_summary_agent, prompt, deps=deps, model=model,
            step_name="per_group_summary",
            iteration_mode="zero_shot",
        )
        gs: GroupSummary = result.output
        return gs.model_copy(update={"label": label, "n_studies": len(bucket)})

    async def _answer_question(key: str, question: str) -> GroupQuestionAnswer:
        bucket = buckets[key]
        label = display_label[key]
        blocks = [_summarise_article_for_search(a, i + 1) for i, a in enumerate(bucket)]
        prompt = (
            f"Group label: {label}\n"
            f"Question: {question}\n"
            f"Topic context: {topic or '(none)'}\n\n"
            f"=== SHORTLIST ({len(bucket)} articles for this group) ===\n\n"
            + "\n\n".join(blocks)
        )
        result = await run_traced(
            group_question_answer_agent, prompt, deps=deps, model=model,
            step_name="per_group_question_answer",
            iteration_mode="zero_shot",
        )
        qa: GroupQuestionAnswer = result.output
        # Force the question text to match what we asked — prevents the LLM
        # from paraphrasing it back (which breaks downstream UI keying).
        return qa.model_copy(update={"question": question})

    async def _build_entry(key: str) -> GroupAnalysisEntry:
        questions = per_group_lookup.get(key, default_questions)
        # Run synthesis + every Q in parallel within this bucket.
        summary_task = asyncio.create_task(_summarise_bucket(key))
        qa_tasks = [
            asyncio.create_task(_answer_question(key, q)) for q in questions
        ]
        summary = await summary_task
        answers = list(await asyncio.gather(*qa_tasks)) if qa_tasks else []
        return GroupAnalysisEntry(
            label=display_label[key],
            n_studies=len(buckets[key]),
            summary=summary,
            answers=answers,
        )

    entries: list[GroupAnalysisEntry] = []
    if eligible_keys:
        entries = list(
            await asyncio.gather(*(_build_entry(k) for k in eligible_keys))
        )
        entries.sort(key=lambda e: (-e.n_studies, e.label.casefold()))

    return PerGroupAnalysis(
        dimension=dimension,
        topic=topic,
        n_articles_synthesized=len(articles),
        n_groups=len(entries),
        unlabeled_count=unlabeled,
        groups=entries,
    )
