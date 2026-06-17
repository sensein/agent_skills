# Connecting structsense-skills to Claude Code

Claude Code (the CLI tool) auto-discovers skills via the `name` + `description` in `SKILL.md`'s YAML frontmatter. There's nothing to configure — drop the folder in the right place and Claude Code will load it.

## 1. Install

Pick a scope:

### User-global (auto-loaded on every project)

```bash
mkdir -p ~/.claude/skills
cp -r /path/to/structsense-skills ~/.claude/skills/
```

After this, every Claude Code session on this machine has the skill available.

### Per-project (versioned with the repo)

```bash
cd /path/to/your/project
mkdir -p .claude/skills
cp -r /path/to/structsense-skills .claude/skills/
git add .claude/skills/structsense-skills && git commit -m "Add structsense-skills"
```

Per-project takes precedence over user-global with the same name.

## 2. Verify it loaded

```
/skills
```

You should see `structsense-skills` listed with its description. If you don't:

- The folder must be `structsense-skills/` containing `SKILL.md` at its root (not `SKILL.md` inside a subfolder).
- The frontmatter MUST have both `name:` and `description:` (and they MUST match the YAML format — three dashes, key/value lines, three dashes).
- Restart Claude Code (some installations cache the skill registry).

## 3. Invoke

Either implicitly (Claude picks it up from your message):

> "Extract all the gene mentions from this paper and map them to ontology IDs."

> "Pull the tools and datasets out of these three READMEs as a structured table."

Or explicitly:

> "Use the structsense-skills skill to extract entities from this PDF and write the result as paper_final.json."

## 4. What Claude will do

When invoked, Claude reads `SKILL.md`, then loads only the reference / prompt files relevant to the current task (progressive disclosure). For NER on a biomedical paper, that's typically:

- `references/ner-extraction.md`
- `prompts/extractor-ner-neuroscience.md` (or `extractor-ner-cns-cells.md`)
- `prompts/mask-recall-pass.md`
- `prompts/judge.md`
- `references/ontology-mapping.md`
- `references/ner-models.md`

For the Python pipeline driver, Claude will call `scripts/pipeline.py` via Bash. Make sure your project's working directory has Python + dependencies available (`pip install requests openai anthropic json-repair jsonschema` — install the providers you actually use).

## 5. API keys

Set keys in your shell environment **before** launching Claude Code, or in a `.env` file in your project root:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export BIOPORTAL_API_KEY=...
```

Claude Code inherits the env from the shell that launched it. **Do not paste API keys into chat** — Claude will warn you.

## 6. Picking models inside Claude Code

When Claude calls `scripts/pipeline.py`, it can pass model strings:

```bash
python -m scripts.pipeline \
    --task ner --input paper.txt \
    --extractor openrouter/anthropic/claude-sonnet-4-6 \
    --judge openrouter/openai/gpt-4o-mini \
    --mapper local --mapper-url http://localhost:8000 \
    --ner-profile biomedical_broad
```

You can tell Claude in plain English: *"use claude-sonnet-4-6 for extraction and gpt-4o-mini for the judge, with the cns_cells NER profile"*. Claude will translate that into the right CLI flags.

## 7. Working with the local concept-mapping service

If you run the [search_hybrid](https://github.com/sensein/search_hybrid) service locally for ontology mapping, **make sure it's reachable before invoking the skill**. Claude will tell you if the cascade falls through to BioPortal or asks you for an alternate URL.

```bash
# Verify the service is up before running the skill
curl -s http://localhost:8000/docs > /dev/null && echo OK
```

## 8. Updating the skill

When you pull a new version of the skill:

```bash
cd ~/.claude/skills/structsense-skills    # or .claude/skills/structsense-skills
git pull                                  # if it's a checkout
# OR
rm -rf structsense-skills/ && cp -r /path/to/new/structsense-skills .
```

Check the version with:

```bash
head -5 ~/.claude/skills/structsense-skills/SKILL.md
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
| `/skills` doesn't list the skill | Wrong folder name or missing frontmatter | Check `name:` + `description:` in `SKILL.md`. |
| Claude ignores the skill in a conversation | Claude didn't see a trigger for it | Mention the task explicitly ("extract entities", "map to ontologies") or invoke by name. |
| Output still has `paper_title` on every entity | LLM ignored the prompt; or you're calling prompts directly without the pipeline | Run `python -m scripts.normalize_result <file>` — it's idempotent and lifts the legacy fields to the canonical shape. |
| Pipeline can't find the concept mapping service | Service not running at the expected port | Start `search_hybrid`, then re-run; or accept the interactive prompt and supply your URL. |
| HF NER models fail with "transformers not installed" | `transformers` package missing | `pip install transformers torch`, or omit `--ner-profile` to skip the ensemble. |
