# Alignment via direct HTTP (no Python client needed)

Use this when:
- You have a list of extracted entities/key_terms ready to align.
- The local hybrid mapping service is running and reachable from your runtime
  (verify with the **probe step** below).
- You either don't have the Python `scripts/local_hybrid_map.py` available, or
  you want the lowest-overhead path: just curl.

## Step 0 — runtime reachability probe (DO THIS FIRST)

Before declaring the mapper "unavailable", you MUST attempt at least one probe.
Do not skip this step. Many "no mapper available" messages are wrong because
the model never tried.

```bash
# Adjust the URL if needed: ports 8000/8001/8080/9000 are all common.
# Set MAPPER_URL once and reuse it.
MAPPER_URL=${MAPPER_URL:-http://localhost:8000}

# Probe /docs (every FastAPI service exposes it). HTTP 200 = service is up.
curl -s -o /dev/null -w '%{http_code}\n' "${MAPPER_URL}/docs"
```

| Result | Meaning | Action |
|---|---|---|
| `200` | Service is up; you can proceed. | Continue to Step 1. |
| Any 4xx / 5xx | Service is reachable but `/docs` isn't where expected. | Try `${MAPPER_URL}/health`, or open `${MAPPER_URL}/openapi.json` to inspect routes. |
| Connection refused / timeout | Either the service isn't running OR your runtime can't reach `localhost`. | See **Runtime reachability** below. |

### Runtime reachability — when "localhost" doesn't mean the user's machine

| Runtime | Can it reach the user's localhost? | What to do if not |
|---|---|---|
| **Claude Code (CLI)** | ✅ Yes — Bash runs on the user's machine. | — |
| **Cursor / Codeium with local agents** | ✅ Usually yes. | — |
| **Claude.ai web app** | ❌ No — Claude runs in Anthropic's cloud; cannot dial the user's machine. | Ask the user to run the **MCP bridge** described in `connecting/mcp-server.md`, OR run the pipeline locally and paste the result back. |
| **ChatGPT (custom GPT in web)** | ❌ No. | Same — needs an HTTP Action pointing at a public URL, see `connecting/custom-gpt.md`. |
| **Anthropic Skills hosted runtime** | ❌ No (sandboxed; no outbound network to localhost). | Same — MCP bridge or public URL. |

If the user is in the web app and the mapper is "running on my machine", the
only way to use it is to set up a tunnel (`ngrok`, `cloudflared`, Tailscale)
that exposes `localhost:8000` to a URL the cloud LLM can reach, OR an MCP
bridge.

## Step 1 — call `/map/batch`

The API expects **`text: [{text, context?}]`** — NOT `terms: [...]`. The
`context` field is optional but dramatically improves disambiguation for
acronyms and ambiguous surface forms.

```bash
curl -s -X POST "${MAPPER_URL}/map/batch" \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
    "max_results": 5,
    "text": [
      {"text": "kidney disease", "context": "progressive decline in GFR"},
      {"text": "T2DM",           "context": "type 2 diabetes with insulin resistance"},
      {"text": "astrocyte"}
    ]
  }'
```

**Always provide `context`** when you have it. For NER, the natural choice is
the entity's containing sentence (which the extractor already emits as
`sentence`). For resources, pass `description`.

### Batching

One POST may carry hundreds to thousands of items. Don't loop one-at-a-time.

Practical batch size: 500 items per request is a safe default. If a request
exceeds 30s (`LOCAL_CONCEPT_MAPPING_TIMEOUT`), split in half and retry.

## Step 2 — parse the response

The service returns (verified against the reference deployment):

```jsonc
{
  "query": "...",
  "type":  "batch",
  "results": {
    "kidney disease": [
      {
        "rank": 1,
        "ontology_id":    "http://purl.obolibrary.org/obo/HP_0012622",
        "ontology_label": "Chronic kidney disease",
        "ontology":       "CIDO",
        "original_score": 0.249,
        "llm_score":      0.0,
        "late_interaction_score": 1.0,
        "final_score":    1.0
      },
      // ... up to max_results candidates
    ],
    "T2DM":      [ ... ],
    "astrocyte": [ ... ]
  }
}
```

Important: `results` is a **dict keyed by the input `text`**, not a list. Look
up each input term by name. Use `final_score` (not `score`).

## Step 3 — pick the top candidate per term

The candidates are pre-ranked. The first one (`rank: 1`) is usually correct.
For high-confidence pipelines, also keep candidates 2–3 in case `judge` later
needs to reconsider.

## Step 4 — merge into the extraction

For each entity in the extraction's `entities[]` (and `key_terms[]`), set:

```jsonc
{
  // existing fields preserved
  "entity": "kidney disease",
  "label":  "Disease",
  "sentence": "...",
  "start": 42, "end": 56,

  // ADD these four
  "ontology_id":    "http://purl.obolibrary.org/obo/HP_0012622",
  "ontology_label": "Chronic kidney disease",
  "ontology":       "CIDO",
  "concept_mapping_provenance": "tool",
  "alignment_method":           "direct_tool_call"
}
```

Do not modify any pre-existing fields. Do not drop items that came back
unmapped — instead set:

```jsonc
{
  "ontology_id": null, "ontology_label": null, "ontology": null,
  "concept_mapping_provenance": "unmapped",
  "alignment_method": "direct_tool_call"
}
```

## Step 5 — preserve provenance throughout

`concept_mapping_provenance: "tool"` is the signal downstream consumers use
to trust an ontology mapping. The only legal values are:

- `"tool"`     — tool returned a match for this term
- `"unmapped"` — tool returned no candidates
- `"skipped"`  — alignment stage was explicitly disabled

**Never `"llm_knowledge"`.** The skill's policy is tool-backed mappings
only. The post-processor (`scripts/normalize_result.py` →
`scripts/iri_validation.py`) demotes any item with `llm_knowledge`
provenance to `unmapped` and labels it with `alignment_method:
"validation_failed"` so the audit trail is clear.

Do not paper over a missing/unreachable mapper by inventing IRIs from
prior knowledge. If the mapper is unreachable, the right action is to
SURFACE that fact to the user (see "When the service is unreachable" below)
and let them decide between (a) bringing it up, (b) configuring BioPortal,
or (c) explicitly opting out of alignment.

## When the service is unreachable

Decide between three fallbacks in this order:

1. **Ask the user for an alternative URL.** Ports often differ from the default.
   ```
   I tried http://localhost:8000/docs — connection refused. What URL is
   your local mapping service running on? (e.g. http://localhost:8001,
   https://internal.example.com/concept-map/, or skip alignment.)
   ```
2. **Try BioPortal as a fallback** (requires `BIOPORTAL_API_KEY`). See
   `prompts/alignment.md` and `scripts/bioportal_map.py`.
3. **Skip alignment** with `concept_mapping_provenance: "skipped"` on every
   item. The pipeline still completes; only ontology fields are unpopulated.

Do not silently invent IRIs. Hallucinated `http://purl.obolibrary.org/obo/...`
strings have done real damage to downstream datasets. The post-processor
will catch them via `scripts/iri_validation.py` and demote them, but you
should not produce them in the first place.

## Worked example — full alignment pass from a bash session

```bash
set -e
MAPPER_URL=${MAPPER_URL:-http://localhost:8000}

# Probe
test "$(curl -s -o /dev/null -w '%{http_code}' "${MAPPER_URL}/docs")" = "200"

# Build the payload from extraction.json (entities + their sentence as context)
jq '{
  max_results: 5,
  text: [.entities[] | {text: .entity, context: .sentence}]
}' extraction.json > /tmp/batch.json

# Call /map/batch
curl -s -X POST "${MAPPER_URL}/map/batch" \
  -H 'Content-Type: application/json' \
  -d @/tmp/batch.json > /tmp/mappings.json

# Merge mappings back into the extraction
jq '
  .entities |= map(
    . as $e
    | (input.results[$e.entity] // [])[0] as $top
    | if $top
      then . + {
        ontology_id:    $top.ontology_id,
        ontology_label: $top.ontology_label,
        ontology:       $top.ontology,
        concept_mapping_provenance: "tool",
        alignment_method:           "direct_tool_call"
      }
      else . + {
        ontology_id: null, ontology_label: null, ontology: null,
        concept_mapping_provenance: "unmapped",
        alignment_method:           "direct_tool_call"
      }
      end
  )
' extraction.json /tmp/mappings.json > aligned.json
```

This whole flow takes ~1–3 seconds on a well-provisioned service for ~1000
entities. Compare to ~30+ minutes if you run alignment through an LLM agent
loop.
