# Chunking strategy

When the input is larger than the model can swallow in one prompt (typically anything over ~25,000 characters for a 128k-token model once you account for the system prompt + JSON schema + output budget), you must chunk.

## Core principles

1. **Chunk on sentence boundaries**, never mid-sentence. NER spans that cross a chunk boundary are lost.
2. **Track each chunk's `start` offset** in the original text. When you merge results, add this offset back to every `start`/`end` to get global offsets.
3. **Parallelize the extractor**, not the alignment or judge (which depend on the merged result).
4. **Merge by stable identifier**, not by order. Don't assume chunk 1's output came back before chunk 2's.

## Sentence-aligned chunking

Use spaCy or NLTK to find sentence boundaries; greedily accumulate sentences until the next one would push you over `max_chars`. See `scripts/chunking.py` for a ready-to-use implementation.

Pseudocode:

```python
def chunk_by_sentences(text: str, max_chars: int = 2000) -> list[dict]:
    sents = sentence_split(text)         # list[(start, end, sentence_text)]
    chunks = []
    cur_start, cur_end = None, None
    for s_start, s_end, _ in sents:
        if cur_start is None:
            cur_start, cur_end = s_start, s_end
            continue
        if s_end - cur_start <= max_chars:
            cur_end = s_end
        else:
            chunks.append({"text": text[cur_start:cur_end], "start": cur_start})
            cur_start, cur_end = s_start, s_end
    if cur_start is not None:
        chunks.append({"text": text[cur_start:cur_end], "start": cur_start})
    return chunks
```

The result is a list of `{text, start}`. Every chunk overlaps no other chunk, and total reconstructs the original text minus inter-sentence whitespace.

## Picking `max_chars`

Rule of thumb: `max_chars ≈ model_context_chars × 0.20`.

Why 20%, not 90%?

- The system prompt + JSON schema + instructions eat ~5–15k tokens.
- The model needs room to **emit** the output JSON, which for dense text is roughly the size of the input again (more entities = more output).
- Internal model overhead (chain-of-thought tokens, retries) consume another chunk.

For a 128k-token model (≈500k chars after tokenization), aim for `max_chars = 25000`.
For a 32k model, aim for `max_chars = 6000`.
For an 8k model (rare now), aim for `max_chars = 1500`.

Smaller chunks = higher recall but slower and pricier. Common values in practice: **1500–3000 chars** for NER, **5000–8000** for resource extraction (which needs more context), **2000** for structured extraction.

## Parallel extraction

Run all chunks concurrently. The exact concurrency depends on the provider:

| Provider | Safe concurrent requests |
|---|---|
| OpenRouter | 8–16 (varies by underlying model and your account tier) |
| OpenAI direct | 16+ |
| Anthropic direct | 5–10 |
| Ollama local | 1–2 (single GPU) |

```python
import asyncio

async def extract_all(chunks):
    sem = asyncio.Semaphore(8)
    async def one(c):
        async with sem:
            return await extract_async(c["text"], chunk_start=c["start"])
    return await asyncio.gather(*[one(c) for c in chunks])
```

## Re-mapping offsets after extraction

Every chunk returns `start`/`end` **relative to the chunk text** (because that's all the model saw). Add `chunk.start` back to make them global:

```python
def reanchor(chunk_start: int, items: list[dict]) -> list[dict]:
    for it in items:
        it["start"] = it["start"] + chunk_start
        it["end"]   = it["end"]   + chunk_start
    return items
```

Validate after reanchoring: `assert original_text[item["start"]:item["end"]] == item["entity"]` for every item. Drop or repair items that fail.

## Merging chunk outputs

After reanchoring, concatenate the lists and deduplicate:

```python
def merge(chunk_results: list[dict]) -> dict:
    entities, key_terms = [], []
    for r in chunk_results:
        entities.extend(r.get("entities", []))
        key_terms.extend(r.get("key_terms", []))
    return {
        "entities": dedupe(entities, key=lambda e: (e["entity"], e["start"], e["end"])),
        "key_terms": dedupe(key_terms, key=lambda k: (k["term"], k["start"], k["end"])),
    }
```

## Downstream chunking (alignment / judge)

The merged extractor output may itself be too large for a single alignment or judge call. Chunk **by item count**:

```python
def chunk_items(items: list, chunk_size: int) -> list[list]:
    return [items[i:i+chunk_size] for i in range(0, len(items), chunk_size)]
```

Default `chunk_size`: `ceil(len(items) / max_workers)`. With 800 items and 8 workers → 100 items per chunk → 8 parallel jobs.

After each chunk runs through alignment/judge, **merge by stable key** (e.g. `(entity, start, end)`). Never assume positional alignment.

## Edge cases

- **Tables in PDFs** often render with `\n` between every cell. Sentence-splitters explode this into one-cell "sentences." Preprocess: collapse runs of single-line cells into a synthetic paragraph before chunking.
- **Code blocks and equations** can defeat sentence splitters. Treat any line starting with `$$` or `\(` as its own pseudo-sentence to prevent the splitter from misbehaving.
- **Multilingual text** may need a language-specific sentence splitter (e.g. spaCy's `xx_sent_ud_sm`).
- **Very short documents** (<2k chars): skip chunking entirely. Sentence-aware chunkers can output zero chunks on edge cases.

## Cost / latency dial

Roughly:

| `max_chars` | Effect |
|---|---|
| 1000 | Highest recall, ~2× cost, slowest |
| 2000 | Default; good balance |
| 3000 | Lower cost; slightly more missed entities at chunk boundaries |
| 6000+ | Risk: prompt + chunk + output exceeds context window on small-context models |

If a user says "slow" → raise `max_chars`. If they say "missing some entities at section breaks" → lower it.
