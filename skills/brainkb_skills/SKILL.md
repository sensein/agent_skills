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

- **How to authenticate — pick in this order. Do NOT ask the user for a password
  by default; BrainKB is a Globus/OAuth-first system and password login is being
  retired.**
  1. **Already configured?** On the hosted remote each caller authenticates via an
     `Authorization: Bearer <token>` header, or a `BRAINKB_TOKEN` (PAT) set in the
     MCP config — in that case no login step is needed at all. Check with
     `brainkb_whoami()` before asking for anything.
  2. **Personal Access Token (PAT)** — the recommended browser-free credential.
     If the user has one, `brainkb_use_token("brainkb_pat_…")` (or they set it as
     `BRAINKB_TOKEN`). If they don't, mint one after step 3 with
     `brainkb_create_token(name, days)`.
  3. **Globus / ORCID / GitHub (browser)** — the normal way to sign in as a real
     user. Call `brainkb_globus_login()`; it returns a URL for the user to open,
     they sign in, the browser shows a short code, and you finish with
     `brainkb_finish_login("<code>")`. **This is what you should do when the user
     says "log in as <their account>" — start the Globus flow, do not ask for a
     password.**
  4. **Password (legacy, discouraged)** — only if the user *explicitly* has a
     password account and asks to use it: `brainkb_login(email, password)`. Never
     volunteer this path or prompt for a password unprompted.
- **Base URL**: default `http://localhost:8010`; ask only if it's not already set
  and the user hasn't said where the deployment is.
- **Never print, echo, or store the password or the JWT token.** Pass them
  straight to the login step. When using curl, read the token into a shell
  variable — do not paste it into chat. (The one exception is the PAT returned by
  `brainkb_create_token`, which is shown to the user **once** so they can copy it
  into their config — never re-display it afterward.)
- A PAT is **revocable instantly** (`brainkb_revoke_token`) and its roles are
  re-checked live on every use, so a ban/demotion takes effect at once.
- **Never print, echo, or store the password or the JWT token.** Pass them
  straight to the login step. When using curl, read the token into a shell
  variable — do not paste it into chat. (The one exception is the PAT returned by
  `brainkb_create_token`, which is shown to the user **once** so they can copy it
  into their config — never re-display it afterward.)
- **Identity must be verified, never assumed — this is the #1 correctness rule.**
  Mutations (create space, ingest, add member, grant) are **attributed to the
  authenticated identity permanently** (provenance records who did what). A login
  step reporting "Logged in as X" is **not proof** the next call runs as X: the MCP
  silently falls back to a configured `BRAINKB_EMAIL`/`BRAINKB_PASSWORD`,
  `BRAINKB_TOKEN`, or an `Authorization` header when the session login isn't
  carried forward (common on the hosted `streamable-http` transport). So:
  1. After **every** login, call `brainkb_whoami()` and confirm `email` == the
     intended user.
  2. **Immediately before any write/mutation**, call `brainkb_whoami()` again and
     confirm it still matches. Only proceed if it does.
  3. If it shows a **different/unexpected** account (e.g. a shared `test@…`),
     **STOP** — report the mismatch, don't write. The durable fix is a per-call
     credential that can't be shadowed: a **PAT** (`BRAINKB_TOKEN` /
     `brainkb_use_token`) or an `Authorization: Bearer` header. A leftover
     `BRAINKB_EMAIL`/`BRAINKB_PASSWORD` in the MCP config is the usual culprit and
     should be removed on a shared/multi-user MCP.
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
**First run `brainkb_whoami()`** — if it already reports `authenticated: true`
(header token or `BRAINKB_TOKEN` PAT is configured), you're done; don't ask for
anything. Otherwise pick a method — **default to Globus, never prompt for a
password unprompted:**

- **Globus / ORCID / GitHub (browser) — the default for "log in as <account>":**
  1. `brainkb_globus_login()` (or `brainkb_globus_login("orcid")` / `("github")`)
     → returns a URL; give it to the user to open and sign in.
  2. The browser then shows a short one-time **code** — ask the user to paste it.
  3. `brainkb_finish_login("<code>")` → completes login for this session.
  The browser sign-in is unavoidable (only the user can consent at the provider),
  but the rest stays in the skill. First OAuth login auto-creates/links the
  profile + a default `Curator` role. **When a user asks to sign in as a specific
  account and no PAT/header is configured, start THIS flow — do not ask for a
  password.**
- **Personal Access Token (PAT) — recommended for repeated use, no browser:**
  1. `brainkb_use_token("brainkb_pat_…")` if the user already has one — done.
  2. To mint one: log in once (Globus, above), then
     `brainkb_create_token(name="laptop", days=3)` → returns a `brainkb_pat_…`
     token **once**. Give it to the user to copy.
  3. The user sets it as `BRAINKB_TOKEN` in the MCP config (or calls
     `brainkb_use_token("<pat>")` per session). No login/browser afterward until
     it expires (default 3 days; pass `days` up to the server cap for longer).
  - Manage: `brainkb_list_tokens()` (metadata only), `brainkb_revoke_token(id)`
    (instant). PATs are the cleanest way to avoid re-authenticating every session.
- **Password (legacy, discouraged — being retired):**
  `brainkb_login(email, password, base_url?)`. Use **only** if the user explicitly
  says they have a password account and want to use it. Never prompt for a password
  otherwise.

> **⚠️ ALWAYS verify identity after logging in — do not trust the login message.**
> Immediately after `brainkb_finish_login` / `brainkb_login` / `brainkb_use_token`,
> call **`brainkb_whoami()`** and confirm the returned `email` is the account you
> intended. A login step can report "Logged in as X" yet subsequent calls run as a
> **different** account — because the MCP falls back, silently, to a configured
> `BRAINKB_EMAIL`/`BRAINKB_PASSWORD` (or `BRAINKB_TOKEN`, or an `Authorization`
> header) when the just-established session isn't carried into the next call (this
> is common on the hosted `streamable-http` transport, where per-session login may
> not persist between tool calls). **If `whoami` shows a different or unexpected
> account, STOP — the login did not take effect. Do NOT create/ingest/mutate**
> (see the identity rule in "Credentials & safety"). The reliable fix is a
> per-call credential that can't be shadowed: a **PAT** via `BRAINKB_TOKEN` /
> `brainkb_use_token`, or an `Authorization: Bearer` header — not an in-memory
> session login on a multi-user remote.

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
- **Before ingesting, verify identity: call `brainkb_whoami()` and confirm the
  `email` is the intended user.** Ingest is **attributed** — the job's `user_id`
  and the provenance (`prov:wasAssociatedWith` / `prov:wasAttributedTo`) are set to
  the authenticated identity, permanently. If `whoami` shows the wrong account
  (e.g. a fallback `test@…` from a stale `BRAINKB_EMAIL`), the data will be
  **mis-attributed and cannot be silently reassigned** — STOP and fix the login
  (use a PAT / header) before ingesting. Never ingest "hoping" the earlier login
  stuck.
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
- **Admin vs SuperAdmin:** **SuperAdmin** is bootstrapped at deployment (env
  allowlist), is protected (can't be banned/deleted/demoted), and is the **only**
  role that can create/remove **Admins** or ban an Admin. **Admin** is granted by a
  SuperAdmin; same KG powers but itself manageable. Both manage **all** team spaces.
- **`manage_team_space` is scoped:** a non-admin with it manages **only** team
  spaces they **created (own)** or were **assigned to** (a member of / matched by a
  per-space `manage` rule) — never all team spaces. Only Admin/SuperAdmin manage all.
- **See the options first:** `brainkb_list_capabilities()` returns the catalog —
  all KG capabilities, which are delegatable (`grantable`), which are admin-only
  (`grant`, `sparql_admin`), and what each means.
- **Grant to an INDIVIDUAL** (Admin/SuperAdmin): inspect with
  `brainkb_capabilities(member)`; grant with
  `brainkb_grant_capability(member, "create_team_space")` (or `ingest`,
  `manage_team_space`, …); revoke with `brainkb_revoke_capability`.
- **Grant to a GROUP/role** (Admin/SuperAdmin): give a capability to *every* member
  of a role/group — including a custom group you created:
  `brainkb_grant_role_capability("uk_collaborator", "ingest")`;
  inspect with `brainkb_role_capabilities(role)`; revoke with
  `brainkb_revoke_role_capability`. Only `grantable` caps may be delegated
  (`grant`/`sparql_admin` are never delegatable — no escalation).
- **Custom groups**: create with `brainkb_create_role("uk_collaborator",
  "Community", "UK collaborators")`, assign with `brainkb_assign_role(email,
  "uk_collaborator")`, then grant it capabilities (above) and/or per-space access.
  A **new group starts with no powers** (read only) until you grant capabilities —
  creating/assigning it is not enough by itself.

  **Worked example — a group that can ingest, end to end:**
  ```
  brainkb_create_role("uk_collaborator", "Community", "UK collaborators")
  brainkb_grant_role_capability("uk_collaborator", "ingest")   # give the group the power
  brainkb_assign_role("alice@uk.org", "uk_collaborator")       # put a user in it
  brainkb_capabilities("alice@uk.org")                         # verify → includes "ingest"
  ```
  Global capability (ingest anywhere they have space write) vs per-space: to scope a
  group to ONE space instead, skip grant_role_capability and add a space write rule —
  `brainkb_add_access_rule("lab-space","write","global_role","uk_collaborator")`.

  Example prompt: *"Create a group uk_collaborator, let it ingest, and add alice@uk.org to it."*
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
These act on the **usermanagement service** and require the caller to hold an
**Admin/SuperAdmin** role. With single sign-on, one `brainkb_login(email,
password)` (or env auto-login) is enough — the MCP exchanges your session for a
usermanagement-scoped token automatically; **no separate admin login**. (A caller
authenticating by header should pass a refresh token so admin tools work too; a
query_service-only access token won't authorize usermanagement.)
- **Onboarding = first login via Globus/ORCID/GitHub** — there is **no separate
  register step**. Signing in (`brainkb_globus_login` → `brainkb_finish_login`, §1)
  auto-creates and links the profile + a default `Curator` role on first login;
  the account is active immediately. Admins then adjust roles.
- Inspect: `brainkb_list_users(q, role)` · `brainkb_available_roles()`.
- Deactivate/reactivate login access: `brainkb_deactivate_user(email)` /
  `brainkb_activate_user(email)`.
- Assign / remove a role (= permission group: Lab Member, External, custom, …):
  `brainkb_assign_role(email, role)` / `brainkb_remove_role(email, role)`
  (the user must have a profile — i.e. have signed in once).
- New group/category: `brainkb_create_role("uk_collaborator", "Community",
  "UK collaborators")` then `brainkb_assign_role(email, "uk_collaborator")`, then
  grant it KG capabilities via §8 (`brainkb_grant_role_capability`).
- **Permissions catalog**: `brainkb_list_permissions()` lists usermanagement
  permissions (resource/action, used for page-access); add new ones with
  `brainkb_create_permission(name, resource, action, description)`. (KG action
  capabilities are separate — see `brainkb_list_capabilities` in §8.)
- **SuperAdmin > Admin**: assigning/removing the `Admin` role, and banning an
  Admin, are **SuperAdmin-only**. The `SuperAdmin` role is protected (can't be
  removed/banned).
- **No deletion — ban instead**: accounts are never hard-deleted. Remove access
  with `brainkb_ban_user(email, reason)` (reversible; preserves history); lift with
  `brainkb_unban_user(email)`. `brainkb_deactivate_user` toggles login access.
- KG-specific capabilities (create team spaces, ingest, etc.) are granted with the
  query_service admin tools — see §8.

## Typical end-to-end

1. `brainkb_login(...)`
2. `brainkb_create_space("my-lab", "My Lab", "private", "My lab's working data")`
3. `brainkb_add_space_graph("my-lab", "https://brainkb.org/graph/my-lab/", "My lab cell-type annotations")`  ← always give a description
4. `brainkb_ingest_text("https://brainkb.org/graph/my-lab/", "<ttl>")` → job_id
5. poll `brainkb_job_status(job_id)` until `done`
6. `brainkb_search("<term>", space="my-lab")` / `brainkb_provenance_graph(iri)`
7. (optional) `brainkb_set_space_visibility("my-lab", "public")`

## Deployment smoke test (is the deploy done & healthy?)

Use this to confirm a fresh deployment is up and the auth stack (SSO + PAT +
Globus) works. Two ways: **curl** (run on the deploy host, or against the public
URLs) and **MCP tools** (from a session with the `brainkb` MCP registered). Run
the layers in order and stop at the first failure — a later layer can't pass if
an earlier one didn't.

Set the two base URLs first (defaults are the unified local stack):

```bash
BASE="${BRAINKB_URL:-http://localhost:8010}"                 # query_service
UM="${USERMANAGEMENT_URL:-http://localhost:8004}"            # usermanagement
# Deployment example:
#   BASE=https://query.brainkb.org   UM=https://usermanagement.brainkb.org
```

### Layer 1 — services reachable (no auth)

```bash
# Each should print 200. Anything else (000/connection refused) = not reachable.
curl -s -o /dev/null -w "query_service   openapi : %{http_code}\n" "$BASE/openapi.json"
curl -s -o /dev/null -w "usermanagement  jwks    : %{http_code}\n" "$UM/.well-known/jwks.json"
curl -s -o /dev/null -w "usermanagement  openapi : %{http_code}\n" "$UM/openapi.json"
# JWKS must contain a key (RS256 issuer live):
curl -s "$UM/.well-known/jwks.json" | python3 -c 'import sys,json;k=json.load(sys.stdin).get("keys",[]);print("jwks keys:",len(k),"OK" if k else "MISSING")'
```

### Layer 2 — Globus/OAuth configured + redirect correct

```bash
# Globus should show "configured": true. If false, GLOBUS_CLIENT_ID/SECRET aren't set.
curl -s "$UM/api/auth/providers" | python3 -m json.tool
```
The server builds the redirect as `${USERMANAGEMENT_PUBLIC_BASE_URL}/api/auth/globus/callback`.
Confirm `USERMANAGEMENT_PUBLIC_BASE_URL` is the **public** host (e.g.
`https://usermanagement.brainkb.org`), not `localhost` — and that this exact
callback URL is registered in the Globus app. A `localhost` value here is the
usual cause of a Globus "redirect mismatch" after deploy.

### Layer 3 — SSO login → per-service exchange (needs a password account)

```bash
# 1) login -> refresh token
REFRESH=$(curl -s -X POST "$UM/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"'"$EMAIL"'","password":"'"$PASSWORD"'"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("refresh_token",""))')
[ -n "$REFRESH" ] && echo "login OK" || echo "login FAILED"
# 2) exchange -> query_service access token
QTOK=$(curl -s -X POST "$UM/api/auth/exchange" -H "Authorization: Bearer $REFRESH" \
  -H 'Content-Type: application/json' -d '{"audience":"query_service"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
# 3) use it against query_service (expect 200)
curl -s -o /dev/null -w "query_service /api/spaces : %{http_code}\n" "$BASE/api/spaces" -H "Authorization: Bearer $QTOK"
```

### Layer 4 — Personal Access Token round-trip

Needs a **session token** (`$STOK`) — the usermanagement JWT for a logged-in
user. If you have a password account, mint one from the refresh token above:
`STOK=$(curl -s -X POST "$UM/api/auth/exchange" -H "Authorization: Bearer $REFRESH" -H 'Content-Type: application/json' -d '{"audience":"usermanagement"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')`

```bash
# create (default lifetime is 3 days)
CREATE=$(curl -s -X POST "$UM/api/auth/tokens" -H "Authorization: Bearer $STOK" \
  -H 'Content-Type: application/json' -d '{"name":"smoke-test","days":3}')
echo "$CREATE" | python3 -m json.tool
PAT=$(echo "$CREATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
PID=$(echo "$CREATE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
# exchange PAT -> query_service token, then use it (expect 200)
PQ=$(curl -s -X POST "$UM/api/auth/pat/exchange" -H 'Content-Type: application/json' \
  -d '{"token":"'"$PAT"'","audience":"query_service"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
curl -s -o /dev/null -w "PAT -> query_service : %{http_code}  (expect 200)\n" "$BASE/api/spaces" -H "Authorization: Bearer $PQ"
# revoke, then confirm the PAT no longer exchanges (expect 401)
curl -s -X DELETE "$UM/api/auth/tokens/$PID" -H "Authorization: Bearer $STOK" >/dev/null
curl -s -o /dev/null -w "PAT after revoke     : %{http_code}  (expect 401)\n" -X POST "$UM/api/auth/pat/exchange" \
  -H 'Content-Type: application/json' -d '{"token":"'"$PAT"'","audience":"query_service"}'
```

A healthy deploy prints: layer-1 all `200` + `jwks keys: N OK`; layer-2 Globus
`configured: true`; layer-3 `login OK` + `200`; layer-4 create returns a
`brainkb_pat_…` token, `PAT -> query_service : 200`, `PAT after revoke : 401`.

### Via the MCP (same checks, no curl)

From a session with the `brainkb` MCP registered (base URL pointed at the
deploy):

1. `brainkb_login(email, password)` **or** `brainkb_globus_login()` →
   `brainkb_finish_login(code)`. Then `brainkb_whoami()` → should report
   `authenticated: true` and the right `base_url`/email (proves login + SSO
   exchange).
2. `brainkb_list_spaces()` → returns without error (proves query_service auth via
   the exchanged token).
3. **PAT:** `brainkb_create_token(name="smoke-test", days=3)` → returns a
   `brainkb_pat_…` once; `brainkb_list_tokens()` → shows it `active: true`;
   `brainkb_revoke_token(<id>)` → `revoked: true`. To prove end-to-end, set that
   PAT as `BRAINKB_TOKEN` (or `brainkb_use_token("<pat>")`) and re-run
   `brainkb_whoami()` / `brainkb_list_spaces()` — they should still work with **no
   password/browser**.
4. Admin reachability (if Admin/SuperAdmin): `brainkb_list_users(limit=1)` returns
   without error (proves the `usermanagement` audience exchange too).

If step 1 errors with a connection failure, re-read **Connectivity** above — the
session can't reach the deploy (localhost from a cloud session, or stack down).

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
