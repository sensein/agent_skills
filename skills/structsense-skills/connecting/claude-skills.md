# Connecting structsense-skills as a hosted Claude Skill

Anthropic Skills (in claude.ai and the Claude Agent SDK) accept the **same `SKILL.md` + supporting files** layout this repo uses. There's nothing to change in the skill itself — just upload or reference it.

## Option A: Upload to claude.ai as a personal skill

1. Go to claude.ai → Settings → Skills (or the analogous menu in your workspace).
2. Click "Create Skill" / "Upload Skill".
3. Upload the **entire `structsense-skills/` folder** (or zip it first).
4. Claude reads `SKILL.md`'s `name:` + `description:` for discovery and `version:` for change-tracking. The frontmatter is already correct.
5. The skill becomes available in any conversation by mention ("use the structsense-skills skill", or by invoking it implicitly with a triggering request).

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
SKILL = Path("structsense-skills")

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
    skills=[{"path": "structsense-skills"}],   # SDK-specific
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
3. For runnable work, calls `scripts/pipeline.py` via Bash or the sandbox.
4. Always produces output in the canonical shape (top-level `source_metadata`, `entities[]` raw, `entities_grouped[]`, `stats`, per-entity `source_model`). Even if the LLM emits the legacy shape, the post-processor in `scripts/normalize_result.py` runs automatically before the file is saved.

## API keys

Claude Skills typically inherit env vars from the sandbox / desktop session. Set:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
export BIOPORTAL_API_KEY=...
```

Never paste keys into chat — Claude will refuse and warn you.

## Updating the hosted skill

Re-upload after a version bump. Check `version:` in `SKILL.md`:

```bash
grep ^version structsense-skills/SKILL.md
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
