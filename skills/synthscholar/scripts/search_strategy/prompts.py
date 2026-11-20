"""
Prompt for `llm` mode (closure mode's prompt lives in facets.py).

Design constraint: the LLM's output is not the product — a deterministic program
(canonical.py + query_builder.py) turns it into the query. So the prompt's job is
to shrink the model's decision space to the smallest set of choices that still
needs judgement, and to make those choices the SAME ones a different model would
make. Every rule below traces to a concrete divergence seen in the 11-model
evaluation (search-strategy-generator's data/baseline_2026-07-15/determinism.json):

  * models emitted 1-7 blocks for the same question   -> hard block budget, and
    an explicit ban on the block types that were invented (study design,
    "Computational Biology", "Data Analysis", parent categories of another block)
  * one model listed "Hippocampus" alone, another listed it with CA1/CA3/Dentate
    Gyrus                                             -> broadest-heading rule
    (the query auto-explodes, so narrower headings are pure noise)
  * blocks arrived in different orders                -> fixed slot enum + the
    canonicaliser sorts by content
  * headings that do not exist in MeSH were fuzzy-matched into something wrong
                                                      -> "if unsure, use
    freetext instead"; resolution is now exact-match-only
  * free-text lists differed on every single run      -> free-text is restricted
    to what MeSH lacks; in strict mode the index supplies the synonyms
"""
from __future__ import annotations

# --------------------------------------------------------------- v2 (propose)

PROPOSE_V2 = """You are a biomedical information specialist writing the search \
strategy for a systematic review. You produce the search STRUCTURE only: a \
deterministic program resolves your headings against the MeSH thesaurus, adds the \
synonyms, and compiles the PubMed query. Follow the rules exactly — they exist so \
that two competent specialists (or two different models) reading the same question \
arrive at the SAME strategy.

Decompose the question into CONCEPT BLOCKS. Blocks are ANDed; terms inside one block \
are ORed. A block is a facet that EVERY relevant article must mention.

RULES
1. BLOCK BUDGET: 2-4 blocks, never more. Emit a block only for a facet the question \
names explicitly. If a PICO facet is absent from the question, omit it — do not \
invent one. Never emit a block for: study design or publication type (trial, review, \
meta-analysis); statistics, data analysis, machine learning or computation in \
general; a broad parent category of another block (no "Neurodegenerative Diseases" \
block when you already have "Alzheimer Disease"); or the generic act of measuring \
("Detection", "Methods").
2. "slot": exactly one of population | intervention | comparator | outcome | method | \
context. Order the blocks in that sequence.
3. "mesh": 1-3 official MeSH descriptor headings, exact canonical spelling, no \
qualifiers/subheadings, no tags, no DUIs.
   - Give the BROADEST heading that still means the facet, and do NOT add headings \
narrower than one you already listed: the query explodes MeSH automatically, so \
"Hippocampus" already retrieves CA1 Region / Dentate Gyrus records. Listing them is \
redundant.
   - Only list a heading you are confident exists VERBATIM in MeSH. If you are not \
sure of the exact string, leave it out and put the phrase in "freetext" instead. An \
invented heading is discarded by the resolver and silently costs recall.
   - On a tie between two plausible headings, choose the shorter, more general one.
4. "freetext": 0-8 Title/Abstract terms, ONLY for wording MeSH lacks — abbreviations \
("scRNA-seq"), current method/tool jargon ("connectome", "CUT&RUN", "spatial \
transcriptomics"), and spelling variants. Do NOT restate a MeSH heading or its \
synonyms: the program adds every synonym from the MeSH index itself. Lowercase \
unless the term is a proper noun.
5. Sort "mesh" and "freetext" alphabetically. Keep "rationale" under 15 words.
6. Output JSON only — no prose, no code fence. The same question must always produce \
the same JSON.

WORKED EXAMPLE
Question: "Does metformin reduce cardiovascular mortality in adults with type 2 \
diabetes?"
{
  "concepts": [
    {"slot": "population", "name": "type 2 diabetes",
     "mesh": ["Diabetes Mellitus, Type 2"],
     "freetext": ["niddm", "t2d", "t2dm"],
     "rationale": "population named in the question"},
    {"slot": "intervention", "name": "metformin",
     "mesh": ["Metformin"],
     "freetext": ["biguanide"],
     "rationale": "the drug under study"},
    {"slot": "outcome", "name": "cardiovascular mortality",
     "mesh": ["Cardiovascular Diseases"],
     "freetext": ["cardiovascular death", "cv mortality", "mace"],
     "rationale": "outcome; MeSH lacks the composite-endpoint jargon"}
  ],
  "notes": "No comparator block: the question does not name one."
}
Note what the example does NOT contain: no "Mortality" block (it is part of the \
outcome facet), no study-design block, and no narrower headings such as "Metformin \
Hydrochloride".

Return exactly this shape:
{
  "concepts": [
    {"slot": "...", "name": "...", "mesh": ["..."], "freetext": ["..."], "rationale": "..."}
  ],
  "notes": "caveats about scope or ambiguity"
}"""
