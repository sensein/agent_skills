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
- **Authorization is role-based, not just JWT** (see the section below). A `403`
  usually means the user's **role/capability** (or a space access rule) doesn't
  permit the action — not that the API token is invalid. Explain which
  role/capability is needed and that an Admin can grant it.

## Core concepts (so you pick the right call)

- **Space** = an owner-controlled workspace containing named graphs, with
  `visibility` = `private` (members only) or `public` (anyone, even without
  logging in, can read). Two `space_type`s: **individual** (a personal space) and
  **team** (shared). Ingest into a graph requires owner/editor membership of its
  space *and* the ingest capability.
- **Roles govern what a user may do** (JWT is only API access). Roles come from the
  user's account; they map to capabilities like create-space / ingest / admin.
- **Ingestion is submit-and-forget**: it returns a `job_id` and runs in the
  background. Always poll job status rather than assuming it finished.
- **Provenance** lives natively in the graph DB (PROV-O). Every ingest is an
  activity; each job's added triples are a queryable **delta**.

## Authorization (roles & capabilities)

Who can do what is decided by the user's **role** → **capability**, then space
membership, then any per-space **access rule**. Key rules to set expectations:

- **Create a team space**: Admin/SuperAdmin only — or a user an admin has **granted**
  `create_team_space`. Use `space_type="team"`.
- **Create an individual/private space**: any write-capable role (Curator, Lab
  Member, Submitter, Annotator, Mapper, Knowledge Contributor, Admin). Default
  `space_type="individual"`.
- **Ingest / recover**: write-capable role (+ owner/editor of the space).
- **Arbitrary SPARQL**: Admin/SuperAdmin only.
- **No role**: read **public** content only — cannot create/ingest/read private.
- **Delegated upgrades** (Admin only): `brainkb_grant_capability(member, capability)`
  — e.g. let a Lab Member create team spaces. Inspect with
  `brainkb_capabilities(member)`. Admin-only caps (`grant`, `sparql_admin`) are not
  delegatable.
- **Fine-grained per-space rules**: within a space, restrict an action to a role /
  member / space-role (see "Manage & delegate"). Owner + Admin always bypass.

If an action is denied, check the user's roles/capabilities
(`brainkb_capabilities`) and either ask an Admin to grant the needed capability, or
adjust the space's access rules.

## Workflows

### 1. Log in
Call `brainkb_login(email, password, base_url?)`. Confirm with `brainkb_whoami()`.

### 2. Create / choose a workspace (space)
- Individual/private workspace (any write-capable role):
  `brainkb_create_space(slug, name, "private", description)` (space_type defaults to
  "individual").
- Team space (Admin/SuperAdmin, or a user granted `create_team_space`):
  `brainkb_create_space(slug, name, "private", description, space_type="team")`.
  If the user isn't authorized you'll get a 403 — an Admin can grant them
  `create_team_space` (see "Manage & delegate").
- Bind a named graph to it (required before ingesting into that graph):
  `brainkb_add_space_graph(slug, graph_iri, description)` — `graph_iri` like
  `https://brainkb.org/graph/<slug>/`.
- **Always pass a non-empty `description`** when registering a graph (and when
  creating a space). The description is stored in the graph registry and surfaces
  in provenance / listings — leaving it blank leaves the graph undocumented (it
  shows up with an empty description). If the user didn't give one, ask for a short
  description, or derive a sensible one from the space/graph name and the data
  being ingested; don't silently register with an empty description.
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
- Publish: `brainkb_set_space_visibility(slug, "public")` (owner/manager). Warn that
  a public space is readable by **anyone, including unauthenticated clients**.
- Add teammates: `brainkb_add_space_member(slug, email, "editor"|"viewer")`.

### 8. Manage & delegate (RBAC)
- **Admin delegation** (Admin/SuperAdmin only):
  - Inspect a user: `brainkb_capabilities(member)`.
  - Grant: `brainkb_grant_capability(member, "create_team_space")` (or
    `manage_team_space`, `ingest`, etc.); revoke with `brainkb_revoke_capability`.
- **Fine-grained per-space access rules** (space owner/manager): restrict an action
  within a space to a role, a member, or a space-role.
  - List: `brainkb_list_access_rules(slug)`.
  - Add: `brainkb_add_access_rule(slug, action, subject_type, subject_value)` where
    `action` ∈ read|write|manage; `subject_type` ∈ global_role|member|space_role.
    e.g. "only Admins may write here": `("write","global_role","Admin")`; "only this
    lab member may read": `("read","member","alice@lab.org")`.
  - Remove: `brainkb_remove_access_rule(slug, rule_id)`.
  - Owner and Admin/SuperAdmin always bypass rules (no lockout).

### 9. Admin: manage users, roles & activation (usermanagement service)
These act on the **usermanagement service** (a separate service with its own
token) and require the caller to hold an **Admin/SuperAdmin** role. They work when
the MCP has credentials (env auto-login, or `brainkb_login(email, password)` — an
OAuth-only session can't log into usermanagement).
- Onboarding: users are usually created by **first login via Globus/ORCID/GitHub**
  (auto-creates their profile + a default `Curator` role). Admins then adjust.
- Inspect: `brainkb_list_users(q, role)` · `brainkb_available_roles()`.
- Activate/deactivate an account: `brainkb_activate_user(email)` /
  `brainkb_deactivate_user(email)`.
- Assign / remove a role (= permission group: Admin, Lab Member, External, …):
  `brainkb_assign_role(email, role)` / `brainkb_remove_role(email, role)`
  (the user must have a profile — i.e. have signed in once).
- New group: `brainkb_create_role("External", "Community", "External collaborators")`
  then `brainkb_assign_role(email, "External")`.
- KG-specific capabilities (create team spaces, etc.) are granted with the
  query_service admin tools — see §8.

## Typical end-to-end

1. `brainkb_login(...)`
2. `brainkb_create_space("my-lab", "My Lab", "private", "My lab's working data")`
3. `brainkb_add_space_graph("my-lab", "https://brainkb.org/graph/my-lab/", "My lab cell-type annotations")`  ← always give a description
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

## Example SPARQL queries

These are **examples / starting points**, not an exhaustive or fixed set — adapt
them (graph IRIs, filters, terms) to the question at hand, or write your own.
Run them with the `brainkb_sparql(query)` tool (**requires an Admin/SuperAdmin
role**; non-admins use the structured tools like `brainkb_search`,
`brainkb_read_space`, `brainkb_provenance_*` instead). Replace `my-lab` / `JOB_ID`
with real values.

**Fixed graph IRIs**

| Purpose | Named graph |
|---|---|
| Graph registry (catalog) | `https://brainkb.org/metadata/named-graph` |
| Spaces manifest | `https://brainkb.org/metadata/spaces/` |
| Provenance | `https://brainkb.org/provenance/` |
| Per-job delta | `https://brainkb.org/provenance/delta/{job_id}` |
| A data graph | e.g. `https://brainkb.org/graph/my-lab/` |

**Common prefixes**

```sparql
PREFIX prov:    <http://www.w3.org/ns/prov#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX schema:  <https://schema.org/>
PREFIX brainkb: <https://brainkb.org/vocab/>
PREFIX rdfs:    <http://www.w3.org/2000/01/rdf-schema#>
```

1) All named graphs + triple counts

```sparql
SELECT ?g (COUNT(*) AS ?triples) WHERE { GRAPH ?g { ?s ?p ?o } }
GROUP BY ?g ORDER BY DESC(?triples)
```

2) A graph's data

```sparql
SELECT ?s ?p ?o WHERE { GRAPH <https://brainkb.org/graph/my-lab/> { ?s ?p ?o } } LIMIT 200
```

3) Registry — which graphs are registered + by whom

```sparql
SELECT ?graph ?description ?registered_at ?registered_by WHERE {
  GRAPH <https://brainkb.org/metadata/named-graph> {
    ?graph dcterms:description ?description ; prov:generatedAtTime ?registered_at .
    OPTIONAL { ?graph prov:wasAttributedTo ?registered_by }
  }
}
```

4) Spaces manifest — visibility, owner, contained graphs

```sparql
SELECT ?space ?name ?visibility ?owner
       (GROUP_CONCAT(DISTINCT STR(?graph); SEPARATOR=", ") AS ?graphs) WHERE {
  GRAPH <https://brainkb.org/metadata/spaces/> {
    ?space a brainkb:Space ; schema:name ?name ;
           brainkb:visibility ?visibility ; brainkb:owner ?owner .
    OPTIONAL { ?space brainkb:containsGraph ?graph }
  }
} GROUP BY ?space ?name ?visibility ?owner
```

Space members (owner/editor/viewer):

```sparql
SELECT ?space ?role ?agent WHERE {
  GRAPH <https://brainkb.org/metadata/spaces/> {
    VALUES ?role { brainkb:owner brainkb:editor brainkb:viewer }
    ?space ?role ?agent .
  }
}
```

5) Provenance — ingestion activities (who/when/status)

```sparql
SELECT ?activity ?agent ?targetGraph ?status ?start ?end ?success ?fail WHERE {
  GRAPH <https://brainkb.org/provenance/> {
    ?activity a brainkb:IngestionActivity ;
              prov:wasAssociatedWith ?agent ;
              brainkb:targetGraph ?targetGraph ;
              brainkb:jobStatus ?status ;
              prov:startedAtTime ?start .
    OPTIONAL { ?activity prov:endedAtTime ?end }
    OPTIONAL { ?activity brainkb:successCount ?success }
    OPTIONAL { ?activity brainkb:failCount ?fail }
  }
} ORDER BY DESC(?start)
```

6) Change history + deltas for a graph

```sparql
SELECT ?delta ?deltaGraph ?added ?time WHERE {
  GRAPH <https://brainkb.org/provenance/> {
    ?delta a brainkb:IngestionDelta ;
           brainkb:targetGraph <https://brainkb.org/graph/my-lab/> ;
           brainkb:deltaGraph ?deltaGraph ;
           brainkb:addedTripleCount ?added ;
           prov:generatedAtTime ?time .
  }
} ORDER BY DESC(?time)
```

Exact triples one job added:

```sparql
SELECT ?s ?p ?o WHERE { GRAPH <https://brainkb.org/provenance/delta/JOB_ID> { ?s ?p ?o } }
```

7) Per-file results for a job

```sparql
SELECT ?name ?status ?http ?size WHERE {
  GRAPH <https://brainkb.org/provenance/> {
    ?file prov:wasGeneratedBy <https://brainkb.org/prov/activity/JOB_ID> ;
          brainkb:fileName ?name ; brainkb:uploadStatus ?status .
    OPTIONAL { ?file brainkb:httpStatus ?http }
    OPTIONAL { ?file brainkb:sizeBytes ?size }
  }
}
```

8) Find a term across graphs

```sparql
SELECT ?g ?s ?label WHERE {
  GRAPH ?g { ?s rdfs:label ?label . FILTER(CONTAINS(LCASE(STR(?label)), "purkinje")) }
}
```
