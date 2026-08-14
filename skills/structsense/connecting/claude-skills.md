# Connecting structsense as a hosted Claude Skill

Anthropic Skills (in claude.ai and the Claude Agent SDK) accept the **same `SKILL.md` + supporting files** layout this repo uses. There's nothing to change in the skill itself — just upload or reference it.

## Option A: Upload to claude.ai as a personal skill

1. Go to claude.ai → Settings → Skills (or the analogous menu in your workspace).
2. Click "Create Skill" / "Upload Skill".
3. Upload the **entire `structsense/` folder** (or zip it first).
4. Claude reads `SKILL.md`'s `name:` + `description:` for discovery and `version:` for change-tracking. The frontmatter is already correct.
5. The skill becomes available in any conversation by mention ("use the structsense skill", or by invoking it implicitly with a triggering request).

Files Claude loads on demand from the skill:
- `references/*.md` — methodology docs (loaded only when relevant).
- `prompts/*.md` — system prompts (used directly as message content).
- `schemas/*.json` — JSON Schemas for validating outputs.
- `scripts/*.py` — Python helpers (executed via the code interpreter / sandbox if your workspace allows it).

## Option B: Use with the Claude Agent SDK

```python
from anthropic import Anthropic
from pathlib import Path

client = Anthropic()
SKILL = Path("structsense")

# Load SKILL.md + the prompts the task needs as the system message
system = "\n\n---\n\n".join([
    (SKILL / "SKILL.md").read_text(),
    (SKILL / "prompts" / "extractor-ner-neuroscience.md").read_text(),
])

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=8192,
    system=system,
    messages=[
        {"role": "user", "content": f"INPUT TEXT:\n<<<\n{paper_text}\n>>>"}
    ],
)
```

Or, if the Skills API in the SDK takes a folder path directly:

```python
response = client.messages.create(
    model="claude-sonnet-4-6",
    skills=[{"path": "structsense"}],   # SDK-specific
    messages=[...],
)
```

(Check the latest SDK docs — the exact parameter name evolves.)

## Option C: Share the skill via a shareable link

If you've made the skill installable, use the `ShareOnboardingGuide` tool or the platform's skill-sharing feature to generate a short link. Teammates open the link in Claude Code or claude.ai and the skill loads automatically.

## What Claude does with the skill

Once loaded, Claude:

1. Reads `SKILL.md` to understand when to trigger the skill.
2. When triggered (NER request, ontology mapping, schema-driven extraction, etc.), loads only the relevant `references/` and `prompts/` files (progressive disclosure — the rest is ignored).
3. Does the extraction and judging **itself** — Claude is the model, so those stages make no external call — and shells out to `scripts/` for the deterministic work (`mask_pass.py`, `group_by_entity.py`, `normalize_result.py`, `stats.py`, `iri_validation.py`). It drives `scripts/pipeline.py`'s LLM stages only when you ask for a headless run or a different model.
4. Always produces output in the canonical shape (top-level `source_metadata`, `entities[]` raw, `entities_grouped[]`, `stats`, per-entity `source_model`). Even if the LLM emits the legacy shape, the post-processor in `scripts/normalize_result.py` runs automatically before the file is saved.

## API keys — none for the default path

**No LLM API key is required.** Claude is the extractor and the judge, so
`OPENROUTER_API_KEY` and friends have nothing to authenticate. Claude asking you for
one is a defect — see `SKILL.md` rule 17.

Claude Skills inherit env vars from the sandbox / desktop session. The only key worth
setting is the concept-mapping **tool** credential, and only if the local hybrid mapper
isn't reachable (which, in a cloud sandbox, it never is):

```bash
export BIOPORTAL_API_KEY=...          # free; concept mapping fallback, not an LLM key
export OPENROUTER_API_KEY=sk-or-v1-...  # ONLY for a headless pipeline.py run
```

Never paste keys into chat — Claude will refuse and warn you.

## Updating the hosted skill

Re-upload after a version bump. Check `version:` in `SKILL.md`:

```bash
grep ^version structsense/SKILL.md
```

If the hosted version is behind, re-upload. Result files from earlier versions can be repaired in-place:

```bash
python -m scripts.normalize_result old.json --input paper.txt \
       --llm-model openrouter/anthropic/claude-sonnet-4-6
```

## What this skill does NOT do via Anthropic Skills

- It does not store API keys in the skill. Keys come from your env or `.env`.
- It does not auto-upgrade. You re-upload to pick up new versions.
- It does not bundle model weights. The HF NER ensemble downloads weights on first use.
