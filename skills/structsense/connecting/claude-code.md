# Connecting structsense to Claude Code

Claude Code (the CLI tool) auto-discovers skills via the `name` + `description` in `SKILL.md`'s YAML frontmatter. There's nothing to configure — drop the folder in the right place and Claude Code will load it.

## 1. Install

Pick a scope:

### User-global (auto-loaded on every project)

```bash
mkdir -p ~/.claude/skills
cp -r /path/to/structsense ~/.claude/skills/
```

After this, every Claude Code session on this machine has the skill available.

### Per-project (versioned with the repo)

```bash
cd /path/to/your/project
mkdir -p .claude/skills
cp -r /path/to/structsense .claude/skills/
git add .claude/skills/structsense && git commit -m "Add structsense"
```

Per-project takes precedence over user-global with the same name.

## 2. Verify it loaded

```
/skills
```

You should see `structsense` listed with its description. If you don't:

- The folder must be `structsense/` containing `SKILL.md` at its root (not `SKILL.md` inside a subfolder).
- The frontmatter MUST have both `name:` and `description:` (and they MUST match the YAML format — three dashes, key/value lines, three dashes).
- Restart Claude Code (some installations cache the skill registry).

## 3. Invoke

Either implicitly (Claude picks it up from your message):

> "Extract all the gene mentions from this paper and map them to ontology IDs."

> "Pull the tools and datasets out of these three READMEs as a structured table."

Or explicitly:

> "Use the structsense skill to extract entities from this PDF and write the result as paper_final.json."

## 4. What Claude will do

When invoked, Claude reads `SKILL.md`, then loads only the reference / prompt files relevant to the current task (progressive disclosure). For NER on a biomedical paper, that's typically:

- `references/ner-extraction.md`
- `prompts/extractor-ner-neuroscience.md` (or `extractor-ner-cns-cells.md`)
- `prompts/mask-recall-pass.md`
- `prompts/judge.md`
- `references/ontology-mapping.md`
- `references/ner-models.md`

Claude does the extraction and judging **itself** — it is the model, so those stages
need no external call. It shells out only for the deterministic helpers
(`mask_pass.py`, `group_by_entity.py`, `normalize_result.py`, `stats.py`,
`iri_validation.py`), none of which call an LLM. For those, make sure the working
directory has Python available: `pip install requests json-repair jsonschema`.

Install the provider SDKs (`openai`, `anthropic`) only if you also want the headless
path in §6.

## 5. API keys — you almost certainly need none

**Running the skill in Claude Code does not require an LLM API key.** Claude *is* the
extractor and the judge. There is no OpenRouter/OpenAI/Anthropic call in the loop, so
`OPENROUTER_API_KEY` and friends do nothing. If Claude asks you for one, that is a bug
in the run, not a missing prerequisite — point it at rule 16 in `SKILL.md`.

Two keys can matter, and neither is an LLM key:

| Variable | What actually needs it | Required? |
|---|---|---|
| `BIOPORTAL_API_KEY` | concept mapping, **only** if the local hybrid mapper at `http://localhost:8000` is unreachable (the rule-15 cascade). Free from [bioportal.bioontology.org](https://bioportal.bioontology.org/account) | only as a fallback |
| `SEMANTIC_SCHOLAR_API_KEY` | lifts Semantic Scholar's 1 req/s public limit | no |

LLM keys are needed **only** for the headless path in §6 — a scheduled or batch run
where no agent is present, or when you deliberately want a different model than
Claude. If that is what you want:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...     # only for §6
```

Claude Code inherits the env from the shell that launched it. **Do not paste keys into
chat** — Claude will warn you.

## 6. Using a different model instead of Claude

Skip this section unless you specifically want it. The reason to reach for it is cost
(a small open model for a 200-page corpus), a local model for data that can't leave
the machine, or reproducibility of a scheduled run — not capability.

```bash
python -m scripts.pipeline \
    --task ner --input paper.txt \
    --extractor openrouter/anthropic/claude-sonnet-4-6 \
    --judge openrouter/openai/gpt-4o-mini \
    --mapper local --mapper-url http://localhost:8000 \
    --ner-profile cns_cells
```

`--extractor` is **required** by `pipeline.py`, which is exactly why this path needs a
key and the default path doesn't. Tell Claude in plain English — *"run it headless with
claude-sonnet-4-6 for extraction and gpt-4o-mini for the judge"* — and it will
translate that into the flags.

Note `--ner-profile` is orthogonal to all of this: it runs local HuggingFace NER
models alongside the extractor and needs `pip install transformers torch`, **not** an
API key. It works the same in host-model mode.

## 7. Working with the local concept-mapping service

If you run the [search_hybrid](https://github.com/sensein/search_hybrid) service locally for ontology mapping, **make sure it's reachable before invoking the skill**. Claude will tell you if the cascade falls through to BioPortal or asks you for an alternate URL.

```bash
# Verify the service is up before running the skill
curl -s http://localhost:8000/docs > /dev/null && echo OK
```

## 8. Updating the skill

When you pull a new version of the skill:

```bash
cd ~/.claude/skills/structsense    # or .claude/skills/structsense
git pull                                  # if it's a checkout
# OR
rm -rf structsense/ && cp -r /path/to/new/structsense .
```

Check the version with:

```bash
head -5 ~/.claude/skills/structsense/SKILL.md
```

Look for `version: 0.3.0` (or higher). If you have output JSON from an older version, run:

```bash
python -m scripts.normalize_result old_output.json --input paper.txt \
       --llm-model openrouter/anthropic/claude-sonnet-4-6
```

to bring it up to the current canonical shape (idempotent).

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| **Claude asks you for an OpenRouter / OpenAI / Anthropic key** | It picked framework mode when it should be the extractor itself | None needed — say "you are the model, no API key". See §5 and `SKILL.md` rule 16. Report it; the skill is meant to prevent this. |
| Claude asks for `BIOPORTAL_API_KEY` | Different thing: the local mapper at `:8000` is unreachable and the cascade fell through to BioPortal | Either start `search_hybrid` (§7) or get a free BioPortal key. This ask is legitimate. |
| `pipeline.py: error: argument --extractor is required` | `pipeline.py` was invoked for a stage the host model should have run | Don't drive the LLM stages through `pipeline.py` in a Claude Code session; use it only for the non-LLM helpers. |
| `/skills` doesn't list the skill | Wrong folder name or missing frontmatter | Check `name:` + `description:` in `SKILL.md`. |
| Claude ignores the skill in a conversation | Claude didn't see a trigger for it | Mention the task explicitly ("extract entities", "map to ontologies") or invoke by name. |
| Output still has `paper_title` on every entity | LLM ignored the prompt; or you're calling prompts directly without the pipeline | Run `python -m scripts.normalize_result <file>` — it's idempotent and lifts the legacy fields to the canonical shape. |
| Pipeline can't find the concept mapping service | Service not running at the expected port | Start `search_hybrid`, then re-run; or accept the interactive prompt and supply your URL. |
| HF NER models fail with "transformers not installed" | `transformers` package missing | `pip install transformers torch`, or omit `--ner-profile` to skip the ensemble. |
