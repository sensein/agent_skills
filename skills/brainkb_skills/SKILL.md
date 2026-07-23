---
name: brainkb
description: >-
  Work with a BrainKB knowledge base on the user's behalf: log in with their
  credentials, ingest RDF into their workspace (space), check the status of
  ingest jobs, read/search the knowledge graphs, and query W3C PROV-O
  provenance (including triple-level deltas). Use whenever the user asks to
  ingest/upload triples or RDF to BrainKB, create or share a workspace/space  (private/public), search or read BrainKB graphs, check an ingest job's status, or ask "who/when/what changed" (provenance) about a graph.
  
---

# BrainKB Skills
This skill provides tools for ingesting, querying, and exploring **BrainKB (Brain Knowledgebase)**. It preferentially uses the **`brainkb_mcp`** server (`brainkb_mcp/`) when available, enabling direct MCP-based interaction. If the MCP server is unavailable, equivalent operations are performed through the BrainKB REST API using `curl` (see below).

## Connectivity — READ FIRST

The base URL must be reachable **from wherever this code runs**:

- `http://localhost:8010` (the current default) only works when the caller runs
  on the **same machine** as the BrainKB Docker stack. That means the **`brainkb`
  MCP server must be running locally (stdio) on that machine**, and you must use
  the **MCP tools** — a local stack is reached through the local MCP process, not
  from a cloud/sandbox session.
- A **cloud / sandbox** Claude session (or the `curl` fallback running there)
  **cannot reach a `localhost` deployment** on the user's laptop — no base URL
  will fix that. In that case either (a) run this in **Claude Code on the same
  machine** with the MCP registered, or (b) use a **publicly reachable** base URL
  (the hosted remote, once it's up).
- If login/any call returns a **connection error / HTTP 000 / connection refused**,
  do **not** keep guessing URLs. It almost always means the caller can't reach the
  deployment. Check: is the `brainkb` MCP running locally? Is the stack up
  (`http://localhost:8010/openapi.json` returns 200 on that machine)? Ask the user
  to confirm rather than retrying different hosts.

**For local testing now: run the `brainkb` MCP locally and keep the base URL at
`http://localhost:8010`.** Do not fall back to curl from a non-local session.

## Credentials & safety

- BrainKB uses JWT auth. Ask the user for their **email**, **password**, and the
  **base URL** (default `http://localhost:8010`) if not already provided.
- The MCP is **multi-user**: on the hosted remote each caller authenticates with
  their own token via an `Authorization: Bearer <token>` header (configured on the
  MCP client), so `brainkb_login` may be unnecessary there. Locally, use
  `brainkb_login(email, password)` — it scopes the token to that session only.
- **Never print, echo, or store the password or the JWT token.** Pass them
  straight to the login step. When using curl, read the token into a shell
  variable — do not paste it into chat.
- Confirm before mutating actions (creating a space, ingesting, changing
  visibility, adding members). Reads are safe.
- Scopes: reads need `read`, ingest / space changes need `write`, arbitrary
  SPARQL needs `admin`. If a call returns 403, the user's account lacks the scope
  — tell them which scope is required.

## Core concepts (so you pick the right call)

- **Space** = an owner-controlled workspace containing named graphs, with
  `visibility` = `private` (members only) or `public` (anyone, even without
  logging in, can read). Ingest into a graph requires owner/editor membership of
  its space.
- **Ingestion is submit-and-forget**: it returns a `job_id` and runs in the
  background. Always poll job status rather than assuming it finished.
- **Provenance** lives natively in the graph DB (PROV-O). Every ingest is an
  activity; each job's added triples are a queryable **delta**.

## Workflows

### 1. Log in
Call `brainkb_login(email, password, base_url?)`. Confirm with `brainkb_whoami()`.

### 2. Create / choose a workspace (space)
- New private workspace: `brainkb_create_space(slug, name, "private")`.
- Bind a named graph to it (required before ingesting into that graph):
  `brainkb_add_space_graph(slug, graph_iri)` — `graph_iri` like
  `https://brainkb.org/graph/<slug>/`.
- List what the user can see: `brainkb_list_spaces()`.

### 3. Ingest
- Raw text: `brainkb_ingest_text(graph_iri, data)` (Turtle/N-Triples/JSON-LD).
- Files: `brainkb_ingest_files(graph_iri, [paths])`.
- Both return a `job_id`. Tell the user ingestion is running in the background.

### 4. Check ingest status
- `brainkb_job_status(job_id)` → status (`pending`/`running`/`done`/`partial`/
  `failed`/`error`), progress %, current file/stage, and per-file failures.
- Poll every few seconds until terminal; report success/failure counts. Use
  `brainkb_list_jobs()` to show recent jobs. If a job is stuck/errored, offer
  `brainkb_recover_job(job_id)`.

### 5. Read / search
- Search (access-filtered): `brainkb_search(q, space?, limit?)`. Omit `space` for
  a full search across everything the user may read.
- Read a whole space's RDF: `brainkb_read_space(slug)`.
- List registered graphs: `brainkb_list_registered_graphs()`.
- Arbitrary SPARQL (admin only): `brainkb_sparql(query)`.

### 6. Provenance
- Whole job: `brainkb_provenance_job(job_id)`.
- A graph's history: `brainkb_provenance_graph(graph_iri)` and
  `brainkb_delta_history(graph_iri)`.
- Exactly what a job added: `brainkb_delta(job_id)`.
- Compare two ingests: `brainkb_delta_compare(job_id_a, job_id_b)`.

### 7. Share publicly / manage a workspace
- Publish: `brainkb_set_space_visibility(slug, "public")` (owner). Warn that a
  public space is readable by **anyone, including unauthenticated clients**.
- Add teammates: `brainkb_add_space_member(slug, email, "editor"|"viewer")`.

## Typical end-to-end

1. `brainkb_login(...)`
2. `brainkb_create_space("my-lab", "My Lab", "private")`
3. `brainkb_add_space_graph("my-lab", "https://brainkb.org/graph/my-lab/")`
4. `brainkb_ingest_text("https://brainkb.org/graph/my-lab/", "<ttl>")` → job_id
5. poll `brainkb_job_status(job_id)` until `done`
6. `brainkb_search("<term>", space="my-lab")` / `brainkb_provenance_graph(iri)`
7. (optional) `brainkb_set_space_visibility("my-lab", "public")`

## Fallback: curl (no MCP)

Only use this when running **on the same machine/network as the deployment**
(so `localhost:8010` resolves to the real stack). From a cloud/sandbox session,
curl to `localhost` will fail — use the local MCP instead.

Base = the deployment URL (default `http://localhost:8010`).

```bash
# login (capture token into a variable — never print it)
TOKEN=$(curl -s -X POST "$BASE/api/token" -H 'Content-Type: application/json' \
  -d '{"email":"'"$EMAIL"'","password":"'"$PASSWORD"'"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# create space + bind a graph
curl -s -X POST "$BASE/api/spaces" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug":"my-lab","name":"My Lab","visibility":"private"}'
curl -s -X POST "$BASE/api/spaces/my-lab/graphs" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"named_graph_url":"https://brainkb.org/graph/my-lab/","description":"lab data"}'

# ingest (URL-encode user_id and named_graph_iri)
curl -s -X POST "$BASE/api/insert/raw/knowledge-graph-triples?user_id=$EMAIL&named_graph_iri=https%3A%2F%2Fbrainkb.org%2Fgraph%2Fmy-lab%2F" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: text/plain' --data-binary @data.ttl

# job status
curl -s "$BASE/api/insert/user/jobs/detail?user_id=$EMAIL&job_id=<JOB_ID>" -H "Authorization: Bearer $TOKEN"

# search / provenance
curl -s "$BASE/api/search?q=<term>" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/api/provenance/named-graph?iri=https%3A%2F%2Fbrainkb.org%2Fgraph%2Fmy-lab%2F" -H "Authorization: Bearer $TOKEN"
```

The `user_id` query parameter must equal the logged-in user's email — the server
rejects acting on another user's behalf.
