# Connecting structsense-skills to Pi (pi.dev)

[Pi](https://pi.dev) is an open-source CLI coding agent by Earendil Inc. (Mario Zechner) — terminal-based, runs locally, no SaaS backend. It natively supports the **Agent Skills** standard (the same `SKILL.md` layout this repo uses) and has built-in `read`/`write`/`edit`/`bash` tools, so it can both discover the skill and run `scripts/pipeline.py` directly. In practice this is the same story as Claude Code: drop the folder in a directory Pi searches, and it loads.

> **Versions move fast.** Pi is young and actively developed. The skill-search paths, slash commands, and env-var names below are accurate as of writing but may drift — confirm against `pi --help`, the in-session `/help`, and the [docs](https://pi.dev/docs/latest/) for your installed version.

## 0. Install Pi (if you haven't)

```bash
# macOS / Linux
curl -fsSL https://pi.dev/install.sh | sh

# or via npm (pnpm / yarn / bun also work)
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
```

Then, from any project directory:

```bash
pi            # start an interactive session
/login        # first-time auth (or set an env var — see §5)
/model        # pick model + provider
```

## 1. Install the skill

Pi searches these locations for skills, in order (first match wins):

1. `~/.pi/agent/skills/` — user-global, Pi-specific
2. `~/.agents/skills/` — user-global, shared across Agent-Skills-compatible tools
3. `.pi/skills/` — per-project, Pi-specific
4. `.agents/skills/` — per-project, shared (cwd walking upward)
5. bundled Pi packages

Pick a scope:

### User-global (every project on this machine)

```bash
mkdir -p ~/.pi/agent/skills
cp -r /path/to/structsense-skills ~/.pi/agent/skills/
```

Or, if you want the skill available to **both Pi and other Agent-Skills tools** from one copy, use the shared location instead:

```bash
mkdir -p ~/.agents/skills
cp -r /path/to/structsense-skills ~/.agents/skills/
```

### Per-project (versioned with the repo)

```bash
cd /path/to/your/project
mkdir -p .pi/skills
cp -r /path/to/structsense-skills .pi/skills/
git add .pi/skills/structsense-skills && git commit -m "Add structsense-skills"
```

Project-scoped skills take precedence over user-global ones with the same name.

## 2. Verify it loaded

In a Pi session, list available skills (the exact command may vary by version — try `/help` if `/skills` isn't recognized):

```
/skills
```

You should see `structsense-skills` with its description. If you don't:

- The folder must be `structsense-skills/` with `SKILL.md` at its root (not nested in a subfolder).
- `SKILL.md` frontmatter MUST have both `name:` and `description:` (three dashes, key/value lines, three dashes).
- Make sure it's in one of the search paths in §1 — a typo like `~/.pi/skills/` (missing `agent/`) won't be found.
- Restart Pi if it cached the registry.

## 3. Invoke

Pi lets you invoke a skill explicitly by name:

```
/skill:structsense-skills
```

…or implicitly, by describing the task and letting Pi pick it up:

> "Extract all the gene mentions from this paper and map them to ontology IDs."

> "Pull the tools and datasets out of these three READMEs as a structured table."

You can also point Pi at a file directly when launching:

```bash
pi @paper.txt "Use structsense-skills to extract entities and write the result as paper_final.json."
```

## 4. What Pi will do

When invoked, Pi reads `SKILL.md`, then loads only the reference / prompt files relevant to the task (progressive disclosure). For NER on a biomedical paper, that's typically:

- `references/ner-extraction.md`
- `prompts/extractor-ner-neuroscience.md` (or `extractor-ner-cns-cells.md`)
- `prompts/mask-recall-pass.md`
- `prompts/judge.md`
- `references/ontology-mapping.md`
- `references/ner-models.md`

For the Python pipeline driver, Pi calls `scripts/pipeline.py` via its built-in `bash` tool. Make sure your project's working directory has Python + dependencies available (`pip install requests openai anthropic json-repair jsonschema` — install the providers you actually use). For the optional HF NER ensemble, also `pip install transformers torch`.

## 5. API keys

Pi reads provider keys from your shell environment, or you can authenticate in-session with `/login` (which also supports Claude Pro/Max, ChatGPT, and Copilot subscription auth and stores credentials at `~/.pi/agent/auth.json`).

Set keys in the shell **before** launching Pi, or in a `.env` you source:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export GEMINI_API_KEY=...
export BIOPORTAL_API_KEY=...
```

Pi inherits the env from the shell that launched it. **Do not paste API keys into chat.**

Note: the pipeline itself reads `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `BIOPORTAL_API_KEY` from the environment when it shells out — Pi's own `/login` auth (in `auth.json`) covers Pi's conversation model, but `scripts/pipeline.py` still needs the keys in the env to call its extractor/judge models. Set both if you want Pi-the-agent and the pipeline to use different providers.

## 6. Picking models inside Pi

When Pi calls `scripts/pipeline.py`, it can pass model strings just like any other CLI invocation:

```bash
python -m scripts.pipeline \
    --task ner --input paper.txt \
    --extractor openrouter/anthropic/claude-sonnet-4-6 \
    --judge openrouter/openai/gpt-4o-mini \
    --mapper local --mapper-url http://localhost:8000 \
    --ner-profile biomedical_broad
```

Tell Pi in plain English — *"use claude-sonnet-4-6 for extraction and gpt-4o-mini for the judge, with the cns_cells NER profile"* — and it translates that into the right flags. The pipeline is model-agnostic, so Pi (the conversation model) and the pipeline's extractor/judge can be entirely different providers.

## 7. Working with the local concept-mapping service

Because Pi runs `bash` on your machine (no cloud sandbox in between), the skill's local hybrid mapper at `http://localhost:8000` is reachable directly — same as Claude Code, unlike the Claude Desktop / claude.ai sandbox case (see [`claude-desktop.md`](claude-desktop.md)).

Verify the [search_hybrid](https://github.com/sensein/search_hybrid) service is up before invoking the skill:

```bash
curl -s http://localhost:8000/docs > /dev/null && echo OK
```

If it's unreachable, the pipeline cascades local → BioPortal → asks you for an alternate URL → hard-stops (it will not silently skip alignment).

## 8. Project-level instructions (optional)

If you want Pi to *always* follow certain conventions when this skill runs (e.g. preferred default models, or "always write the canonical shape"), add them to an `AGENTS.md` at your project root — Pi reads `AGENTS.md` (and `CLAUDE.md`) walking from cwd up through parent dirs, plus a global `~/.pi/agent/AGENTS.md`. Example:

```markdown
# Project conventions

When using the structsense-skills skill:
- Default extractor: openrouter/anthropic/claude-sonnet-4-6; default judge: gpt-4o-mini.
- Always emit the canonical shape (top-level source_metadata, entities[] one per
  occurrence, entities_grouped[], stats). Never put paper_title/doi on each entity.
- For biomedical papers, run mask-recall after pass-1.
```

This is optional — the skill's own `SKILL.md` already encodes these rules. Use `AGENTS.md` only for per-project overrides.

## 9. MCP (optional, not built in)

Pi ships with **no native MCP support** — by design ("build CLI tools with READMEs, or an extension that adds MCP"). For this skill that doesn't matter: the Agent Skills path above is the first-class integration and is all you need.

If you specifically want to expose the pipeline as an MCP tool (e.g. to share one running instance across Pi, Cursor, and Claude Code), you can:

- Build the MCP server from [`mcp-server.md`](mcp-server.md), then
- Add MCP support to Pi via the community **`pi-mcp-adapter`** extension (third-party, by nicobailon — install separately; config paths and options are version-dependent, so check its README), and configure the server in the adapter's `mcp.json`.

Treat this as an ecosystem add-on, not core Pi behavior. For a single-machine workflow, the Skills path (§1) is simpler and recommended.

## 10. Updating the skill

When you pull a new version:

```bash
cd ~/.pi/agent/skills/structsense-skills    # or .pi/skills/structsense-skills, or ~/.agents/skills/...
git pull                                    # if it's a checkout
# OR
rm -rf structsense-skills/ && cp -r /path/to/new/structsense-skills .
```

Check the version:

```bash
head -5 ~/.pi/agent/skills/structsense-skills/SKILL.md   # look for version: 0.4.0 or higher
```

If you have output JSON from an older version, bring it up to the canonical shape (idempotent):

```bash
python -m scripts.normalize_result old_output.json --input paper.txt \
       --llm-model openrouter/anthropic/claude-sonnet-4-6
```

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Skill not listed | Wrong folder location or missing frontmatter | Confirm it's under a §1 search path; check `name:` + `description:` in `SKILL.md`. |
| `/skills` not recognized | Command name differs by version | Run `/help` to find the skill-listing / `/skill:` command for your build. |
| Pi ignores the skill in conversation | No trigger detected | Invoke explicitly with `/skill:structsense-skills`, or name the task ("extract entities", "map to ontologies"). |
| Pipeline can't find the mapping service | `search_hybrid` not running on the expected port | Start it, verify with `curl http://localhost:8000/docs`, then re-run. |
| HF NER models fail ("transformers not installed") | `transformers` missing | `pip install transformers torch`, or omit `--ner-profile` to skip the ensemble. |
| Pipeline auth fails even after `/login` | `/login` auths Pi's model, not the pipeline subprocess | Export `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` etc. in the shell that launched Pi (§5). |
| Output has `paper_title`/`doi` on every entity | Legacy shape | `python -m scripts.normalize_result <file> --input <text> --llm-model <model>` — idempotent. |
</content>
</invoke>
