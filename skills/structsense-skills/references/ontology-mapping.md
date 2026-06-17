# Ontology mapping (concept alignment)

Map free-text terms to ontology IRIs + labels. Four backends are useful; pick by cost, quality, and infra constraints.

| Backend | Setup | Quality | Speed | Cost |
|---|---|---|---|---|
| **BioPortal REST API** | API key | High (curated) | ~1 req/sec (rate-limited) | Free + your time |
| **OLS REST API** (EBI) | None | High (EBI-curated) | Fast | Free |
| **Local hybrid BM25 + dense retrieval** | Self-host a service (e.g. [search_hybrid](https://github.com/sensein/search_hybrid)) | Tunable, very high if re-ranked | Fastest (batched) | Infra |
| **LLM-only (no tool)** | Just prompting | Hallucinates IRIs | Fast | LLM tokens |

**Rule of thumb:** prefer **tool-based** mapping (BioPortal/OLS/local) and use the LLM only to choose between candidates or fill obvious gaps. Mark every output with `concept_mapping_provenance`: `"tool"` or `"llm_knowledge"`.

## Recommended cascade (production default)

The reference pipeline (`scripts/pipeline.py`) uses this order by default:

1. **Local hybrid service** at `http://localhost:8000` (the [search_hybrid](https://github.com/sensein/search_hybrid) reference implementation). Verify it's up by visiting **`http://localhost:8000/docs`** — every FastAPI-based deployment serves the interactive OpenAPI page there. The pipeline health-checks `/health` then `/docs`.
2. **BioPortal** (if `BIOPORTAL_API_KEY` is set). Triggered automatically if the local service is unreachable.
3. **Ask the user** for an alternative local URL. Deployments often use non-default ports (8001, 9000, behind a reverse proxy at `/concept-map/`, etc.) — the cascade prompts for an override and retries the local backend with the user-provided URL.
4. **Skip alignment entirely** with `concept_mapping_provenance: "skipped"` on every item, only if the user declines to provide a URL. The run still completes; only ontology fields are unpopulated.

When this skill is used inside an LLM agent (Claude Code, GPT custom action, etc.), the agent should **ask the user via natural language** if the cascade exhausts its defaults — the port/host varies enough across deployments that a default-only check is not enough. Example: "I couldn't reach a concept-mapping service at http://localhost:8000. What URL is your local service running on, or should I fall back to BioPortal?"

The cascade builder is `scripts/pipeline.py::build_mapper_with_cascade`. Use `ask_user=stdin_ask_callback` for CLI use, or pass your own `ask_user(prompt: str) -> Optional[str]` callable to route the prompt through your UI.

## Output format (every backend produces this)

For each extracted term:

```jsonc
{
  "term": "hippocampus",
  "ontology_id": "http://purl.obolibrary.org/obo/UBERON_0002421",
  "ontology_label": "hippocampal formation",
  "ontology": "UBERON",
  "concept_mapping_provenance": "tool"   // or "llm_knowledge" or "skipped"
}
```

Keep this shape constant across backends. If you change backend, downstream code doesn't change.

## Backend 1: BioPortal REST

Docs: https://data.bioontology.org/documentation
Get a key at https://bioportal.bioontology.org/account.

### Endpoint

`GET https://data.bioontology.org/search?q={term}&apikey={key}&display_context=false&include=prefLabel,definition`

Optional filters:
- `ontologies=UBERON,CL,NCBITAXON` to constrain to specific ontologies.
- `require_exact_match=true` for strict matching.
- `pagesize=10` for top-N candidates.

### Throttling

BioPortal rate-limits aggressive callers. Use:

- `BIOPORTAL_REQUEST_INTERVAL=0.7` seconds between requests (raise to 1.0+ if you see 429s).
- Exponential backoff on 429: `BIOPORTAL_BACKOFF_AFTER_429=2.0` seconds initial; double on each retry.
- A small in-memory LRU cache by `term` (e.g. 2000 entries). Many papers re-mention the same terms.

See `scripts/bioportal_map.py` for a ready-to-use client.

## Backend 2: OLS (EBI Ontology Lookup Service)

Docs: https://www.ebi.ac.uk/ols4/help

### Endpoint

`GET https://www.ebi.ac.uk/ols4/api/search?q={term}&exact=false&rows=10`

Optional filters:
- `ontology=uberon,cl,ncbitaxon` (lowercase, comma-separated).
- `exact=true` for strict matching.
- `type=class` to skip properties/individuals.

OLS is free and well-rate-limited; usable in batch without API keys.

See `scripts/ols_map.py`.

## Backend 3: Local hybrid retrieval

For high throughput (thousands of terms in seconds), self-host a service that combines:

1. **BM25** over ontology labels + synonyms (lexical).
2. **Dense embedding retrieval** (e.g. `nomic-embed-text`, `intfloat/e5-base`) over the same.
3. **Re-ranking** with a cross-encoder or an LLM.

The reference implementation is at https://github.com/sensein/search_hybrid (branch `dev`). It exposes (verified against a live deployment):

```
POST /map/batch
Content-Type: application/json
{
  "max_results": 5,
  "text": [
    { "text": "kidney disease", "context": "progressive decline in GFR" },
    { "text": "T2DM",           "context": "type 2 diabetes with insulin resistance" },
    { "text": "astrocyte" }
  ]
}
```

The top-level field is **`text`**, not `terms`. Each item is `{text, context?}`. The optional `context` field dramatically improves disambiguation — for NER, pass the entity's containing sentence as `context`.

Returns:

```jsonc
{
  "query": "...",
  "type":  "batch",
  "results": {
    "kidney disease": [
      { "rank": 1, "ontology_id": "http://purl.obolibrary.org/obo/HP_0012622",
        "ontology_label": "Chronic kidney disease", "ontology": "CIDO",
        "final_score": 1.0, "original_score": 0.249, "llm_score": 0.0,
        "late_interaction_score": 1.0 },
      // ... up to max_results candidates, pre-ranked by `rank`
    ],
    "T2DM":      [ /* ... */ ],
    "astrocyte": [ /* ... */ ]
  }
}
```

Important: `results` is a **dict keyed by the input `text`**, not a list parallel to inputs. Use `final_score` (not `score`).

**One HTTP call, thousands of terms.** This is the dominant cost optimization for large-scale runs.

For a direct, Python-free workflow (just `curl` + `jq`), see [`prompts/alignment-via-http.md`](../prompts/alignment-via-http.md) — it gives the LLM an exact bash template.

**Interactive docs:** open `http://localhost:8000/docs` in a browser to see the OpenAPI schema and try the endpoints. This is also the most reliable "service is up" probe — the pipeline's health check tries `/health` first and falls back to `/docs`.

**The URL varies.** Most users run on `localhost:8000`, but ports `8001`, `8080`, `9000`, and reverse-proxied paths like `https://internal.example.com/concept-map/` are all common. The pipeline's cascade asks the user for an override when the default doesn't reach the service — see "Recommended cascade" above.

See `scripts/local_hybrid_map.py`.

### Runtime reachability — "localhost" doesn't mean the same thing everywhere

The most common cause of "no mapper available" is the LLM giving up without probing. Before you tell the user the mapper is unreachable, **you must attempt at least one HTTP probe** in whatever runtime you have:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/docs
# 200 = service reachable. Connection refused = either the service is down,
# OR your runtime can't reach the user's machine (see table below).
```

| Runtime | Can reach the user's `localhost`? | What to do if not |
|---|---|---|
| **Claude Code (CLI)** | ✅ Yes — Bash runs on the user's machine. | — |
| **Cursor / local agent IDEs** | ✅ Usually yes. | — |
| **claude.ai web app** | ❌ No — Claude runs in Anthropic's cloud. | Ask the user to expose the service via an MCP bridge (`connecting/mcp-server.md`) or a tunnel (`ngrok`, `cloudflared`, Tailscale). |
| **ChatGPT custom GPT** | ❌ No. | Wire a server-side Action with a public URL (`connecting/custom-gpt.md`). |
| **Anthropic Skills hosted runtime** | ❌ No (no outbound localhost). | Same — MCP bridge or public URL. |

**Symptom you've seen:** the user has the service running and verified it with curl, but the LLM still says "not available". This always means one of:

1. The LLM's runtime can't reach the user's machine (web app, hosted runtime). → Set up a bridge.
2. The LLM never probed at all. → Strict instruction: "you MUST run `curl ${MAPPER_URL}/docs` before declaring the mapper unavailable."
3. The LLM probed but used the wrong API schema (e.g. `terms:` instead of `text:`). → See the corrected schema above.

## Backend 4: LLM-only (fallback)

When you have no tool, prompt the LLM:

```
For each term, return the most likely ontology IRI, label, and ontology shortname.
If you are not at least 80% confident, return null.
Output strict JSON keyed by term.
```

Mark every returned mapping as `"concept_mapping_provenance": "llm_knowledge"`. Treat these as **hints, not ground truth.** Always validate IRIs against an actual ontology before trusting them downstream — LLMs invent plausible-looking IRIs.

### IRI validation

```python
import re

OBO_RE = re.compile(r"^http://purl\.obolibrary\.org/obo/[A-Z]+_\d+$")
NCBITAXON_RE = re.compile(r"^NCBITaxon:\d+$|^http://purl\.obolibrary\.org/obo/NCBITaxon_\d+$")

def looks_real(iri: str) -> bool:
    return any(p.match(iri) for p in (OBO_RE, NCBITAXON_RE, ...))
```

Ideally, resolve the IRI (HEAD request) before accepting it.

## Combining backends (recommended)

Most production setups combine two backends:

1. **Fast path:** local hybrid retrieval for every term in one batch call.
2. **Fallback:** for terms the local service marks as low-confidence (score < threshold), call BioPortal or OLS for a second opinion.
3. **LLM re-rank:** if you have multiple candidates, pass them + the original sentence to an LLM and ask which is correct.

Pseudocode:

```python
local = local_hybrid.batch(terms, max_results=5)

low_conf = [t for t, hits in zip(terms, local) if hits[0].score < 0.7]
fallback = bioportal.batch(low_conf, max_results=3)

# Re-rank top candidates with LLM
final = []
for term, candidates, sentence in zip(terms, merged_candidates, sentences):
    if len(candidates) == 1:
        final.append(candidates[0])
    else:
        choice = llm_pick_best(term, candidates, sentence)
        final.append(choice)
```

## Choosing the right ontology

For biomedical text, a good default routing:

| Term type | First-choice ontology |
|---|---|
| Species / organism | NCBITaxon |
| Anatomy / brain region | UBERON |
| Cell type | CL (Cell Ontology) |
| Gene | NCBIGene / HGNC |
| Protein | UniProt / PR (Protein Ontology) |
| Disease | MONDO / DOID |
| Phenotype | HP (Human Phenotype) / MP (Mouse Phenotype) |
| Chemical / drug | CHEBI / DrON |
| Tissue / cell line | BTO (BRENDA Tissue Ontology) |
| Method / assay | OBI / EFO |

The mapping tool should accept an `ontologies=` filter; pre-filter by entity `label` to avoid noisy cross-domain matches.

## Provenance discipline

Every mapped item must carry **two** provenance markers:

- `concept_mapping_provenance`: where the mapping came from (`tool` / `llm_knowledge` / `skipped`).
- `alignment_method`: how the alignment stage was run (`direct_tool_call` / `llm_agent` / `skipped`).

This is the single most useful piece of metadata for debugging weird outputs months later.

## When to skip alignment

Skip the alignment stage entirely when:

- The downstream consumer doesn't need IRIs (e.g. just feeding the entity list to a UI).
- The text is in a domain with no good ontology coverage (e.g. computational methods names, dataset names).
- You're prototyping and want to validate the extraction step in isolation.

Mark every item with `concept_mapping_provenance: "skipped"` so the absence is explicit, not implicit.
