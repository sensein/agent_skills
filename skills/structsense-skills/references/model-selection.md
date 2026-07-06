# Model selection per stage

You don't need to use the same model for every stage. Pick the cheapest model that's *good enough* for each role.

## Per-stage requirements

| Stage | Most important capability | Acceptable second-tier |
|---|---|---|
| **Extractor** | Strong instruction-following + span fidelity (offsets must match) | Long context, tool use unhelpful |
| **Alignment (LLM-driven)** | Tool use, JSON-typed tool inputs, world knowledge of ontologies | Long context |
| **Alignment (direct tool)** | (no LLM) | — |
| **Judge** | Factual consistency reasoning, numeric calibration | Fast/cheap |
| **Human feedback** | Following imperative edit instructions | Fast/cheap |

## Default picks (June 2026)

| Stage | Default | Why |
|---|---|---|
| Extractor | `claude-sonnet-4-6` or `gpt-4.1` or `gpt-4o` | Strong span fidelity, good JSON, fast enough |
| Alignment | Direct tool call (no LLM) when possible | Cheapest & most accurate. See `ontology-mapping.md`. |
| Alignment (LLM fallback) | `claude-haiku-4-5` or `gpt-4o-mini` | Cheap and good at calling tools |
| Judge | `gpt-4o-mini` or `claude-haiku-4-5` | Per-item batched, cost-sensitive |
| Human feedback | Same as judge | Same constraints |

Local-only: `ollama/qwen2.5:14b`, `ollama/deepseek-r1:14b`, `ollama/llama3.1:70b`. Use Q4 quantization for memory-constrained machines.

## Configuration (provider prefixes)

Most agent frameworks use these prefixes:

| Provider | Model string | Base URL |
|---|---|---|
| OpenRouter | `openrouter/<provider>/<model>` e.g. `openrouter/anthropic/claude-sonnet-4-6` | `https://openrouter.ai/api/v1` |
| OpenAI direct | `openai/<model>` e.g. `openai/gpt-4o-mini` | (default OpenAI base) |
| Anthropic direct | `anthropic/<model>` | (default Anthropic base) |
| Ollama (local) | `ollama/<model>` e.g. `ollama/qwen2.5:14b` | `http://localhost:11434` |
| vLLM (self-hosted) | `openai/<model>` (vLLM speaks OpenAI API) | your vLLM server URL |
| Gemini | `gemini/<model>` | per provider docs |

For OpenRouter, the same key works for any underlying model — handy for trying providers without re-keying.

## Cost levers

| Lever | Effect |
|---|---|
| Smaller extractor model | 5–10× cheaper but more missed entities and bad spans |
| Direct tool call for alignment | Removes ~50–80% of total cost on entity-heavy runs |
| `skip_judge_llm=true` (auto-approve) | Removes judge cost entirely |
| `direct_judge_api=true` (parallel API calls, no agent loop) | 3–5× cheaper than CrewAI agent loop |
| Larger `chunk_size` | Fewer extractor calls, but risk missed entities at chunk boundaries |
| Tighter `max_iter` (e.g. 3 instead of 20) | Stops runaway tool loops; harmless for well-prompted tasks |

## Quality levers

| Lever | Effect |
|---|---|
| Larger extractor model | Better recall + better spans |
| Smaller `chunk_size` (e.g. 1000) | Better recall at chunk boundaries; more cost |
| Multi-result ontology lookup + LLM re-rank | Better ontology IRIs when first hit is wrong |
| Two-pass extraction (rough → refine) | Better recall on long documents; ~2× cost |
| Human feedback stage | Catches systematic errors |

## What to use when

| Situation | Setup |
|---|---|
| Production NER on biomedical papers, budget OK | Extractor: `claude-sonnet-4-6`. Alignment: direct local-hybrid tool call. Judge: `gpt-4o-mini`. |
| Prototyping cheaply | Extractor + judge: `gpt-4o-mini`. Alignment: skip. |
| Air-gapped / on-prem | Everything in `ollama/<model>` (or vLLM). Use `qwen2.5:14b` or `llama3.1:70b`. |
| Highest accuracy, cost no object | Extractor: `claude-sonnet-4-6` with multi-result alignment + LLM re-rank with `gpt-4.1`. Judge: `claude-sonnet-4-6`. Human feedback enabled. |
| Reproducible / deterministic | Pin model versions explicitly. `temperature=0`. Use `seed=` when supported. |

## Mixing models across stages

A common cost-effective mix:

```yaml
# Extractor — strong span fidelity
extractor:
  model: openrouter/anthropic/claude-sonnet-4-6
  temperature: 0

# Alignment — tool-using small model (or skip)
alignment:
  model: openrouter/anthropic/claude-haiku-4-5
  temperature: 0

# Judge — cheap factuality checker
judge:
  model: openrouter/openai/gpt-4o-mini
  temperature: 0
```

Roughly 50–70% cheaper than running every stage on the strong model, with negligible quality loss on most tasks.

## Picking ollama models for offline use

| Model | RAM (Q4) | Good for |
|---|---|---|
| `qwen2.5:7b` | 6 GB | Quick prototyping; weaker JSON discipline |
| `qwen2.5:14b` | 10 GB | Solid extractor for most tasks |
| `qwen2.5:32b` | 20 GB | Production-quality extractor on a workstation |
| `llama3.1:70b` (Q4) | 40 GB | High quality; needs serious hardware |
| `deepseek-r1:14b` | 10 GB | Strong reasoning; useful for judge |
| `nomic-embed-text` | 0.5 GB | Embeddings only (for retrieval/memory) |

Be aware: smaller local models often produce malformed JSON. Pair them with constrained decoding (Outlines, Guidance, llguidance) or aggressive Tier-3 schema re-prompting from `json-output-discipline.md`.

## Token budgets

For each call, budget:

- ~5–15k tokens for system prompt + schema + instructions
- input chars / 4 for the user content
- 2× input tokens for output (NER on dense text produces a lot)
- 1–2k tokens for tool-use traces if alignment uses an agent loop

If `system + input + expected_output ≥ context_window`, drop `chunk_size`.
