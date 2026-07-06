"""Model context-window registry + token-aware chunk-size math.

Two things this module gives you:

1. ``get_model_context_window(model_str)`` — best-known context size (tokens)
   for a model string. Patterns are matched longest-first.
2. ``compute_downstream_chunk_size(...)`` — how many items per chunk to feed
   into alignment / judge / humanfeedback so the *prompt + payload + output*
   stays under the model's context window.

Adapted from structsense `model_context.py`. The OpenRouter live-probe cache
is omitted (it adds complexity and a network round-trip; the static patterns
are accurate enough for chunk-sizing decisions).
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Tuning constants. Override per-call via the function arguments; these are
# safe defaults that work across the major frontier models.
# ---------------------------------------------------------------------------

# Fraction of (context - prompt_overhead) usable for the payload. The rest
# is reserved for the model's output generation.
_CONTEXT_SAFETY_FACTOR: float = 0.70

# Conservative overhead for ReAct/tool-loop boilerplate, JSON schema reminders,
# format instructions. Set to ~3k for agent-loop frameworks like CrewAI; set
# to ~500 for direct API calls with a focused system prompt.
_DEFAULT_FRAMEWORK_OVERHEAD_TOKENS: int = 3_000


# ---------------------------------------------------------------------------
# Model family → context window (tokens). Ordered MOST SPECIFIC first so
# the first substring match wins.
# ---------------------------------------------------------------------------
_MODEL_CONTEXT_PATTERNS: list[tuple[str, int]] = [
    # Meta Llama
    ("llama-4-scout",           10_000_000),
    ("llama-4-maverick",         1_000_000),
    ("llama-4",                  1_000_000),
    ("llama-3.3",                  128_000),
    ("llama-3.2",                  128_000),
    ("llama-3.1",                  128_000),
    ("llama-3",                      8_000),
    ("llama",                      128_000),

    # OpenAI
    ("gpt-4.1",                  1_000_000),
    ("gpt-5-mini",                 400_000),
    ("gpt-5-nano",                 400_000),
    ("gpt-5",                      400_000),
    ("gpt-4o-mini",                128_000),
    ("gpt-4o",                     128_000),
    ("gpt-4-turbo",                128_000),
    ("gpt-4",                      128_000),
    ("gpt-3.5",                      4_000),
    ("o3",                         200_000),
    ("o1",                         128_000),

    # Anthropic Claude
    ("claude-opus-4-7",            1_000_000),  # opus-4-7 with extended ctx
    ("claude-sonnet-4-6",          200_000),
    ("claude-sonnet-4",            200_000),
    ("claude-haiku-4-5",           200_000),
    ("claude-haiku-4",             200_000),
    ("claude-opus-4",              200_000),
    ("claude-3-5-sonnet",          200_000),
    ("claude-3-5-haiku",           200_000),
    ("claude-3",                   200_000),
    ("claude",                     200_000),

    # Google Gemini
    ("gemini-2.5",               1_000_000),
    ("gemini-2.0",               1_000_000),
    ("gemini-1.5",               1_000_000),
    ("gemini",                   1_000_000),

    # DeepSeek
    ("deepseek-r1",                128_000),
    ("deepseek-v3",                128_000),
    ("deepseek",                   128_000),

    # Mistral
    ("mistral-large",              128_000),
    ("mistral-medium",             128_000),
    ("mistral-small",              128_000),
    ("mixtral",                     32_000),
    ("mistral",                    128_000),

    # Qwen / Alibaba
    ("qwen3-coder",                256_000),
    ("qwen3",                      256_000),
    ("qwen2.5",                    128_000),
    ("qwen",                       128_000),

    # xAI Grok
    ("grok-3",                     131_000),
    ("grok",                       131_000),

    # Cohere
    ("command-r-plus",             128_000),
    ("command-r",                  128_000),
    ("command",                      4_000),

    # Amazon
    ("nova-pro",                   300_000),
    ("nova",                       300_000),

    # Microsoft Phi
    ("phi-3.5",                    128_000),
    ("phi-3",                      128_000),
    ("phi",                        128_000),

    # Perplexity Sonar
    ("sonar",                      127_000),
]

_DEFAULT_CONTEXT_WINDOW: int = 128_000


def _normalize_model_id(model_str: str) -> str:
    """Strip provider prefixes ('openrouter/', 'openai/', 'anthropic/', etc.)
    and any ':variant' suffix so substring matching is robust.
    """
    m = (model_str or "").strip().lower()
    for prefix in ("openrouter/", "openai/", "anthropic/", "google/",
                   "ollama/", "gemini/", "mistralai/"):
        if m.startswith(prefix):
            m = m[len(prefix):]
            break
    if ":" in m:
        m = m.split(":")[0]
    return m


def get_model_context_window(model_str: str) -> int:
    """Return the context-window size in tokens for ``model_str``.

    Falls back to ``_DEFAULT_CONTEXT_WINDOW`` (128k) when no pattern matches.
    """
    if not model_str:
        return _DEFAULT_CONTEXT_WINDOW
    m = _normalize_model_id(model_str)
    for pattern, ctx in _MODEL_CONTEXT_PATTERNS:
        if pattern in m:
            return ctx
    return _DEFAULT_CONTEXT_WINDOW


# ---------------------------------------------------------------------------
# Token estimation for structured payloads
# ---------------------------------------------------------------------------

def estimate_payload_tokens(payload: Any) -> int:
    """Estimate token count for a JSON-serializable payload.

    Uses the character count of the JSON form as a 1:1 token estimate.
    For structured data (entity arrays, ontology IDs, offsets), this is
    empirically accurate — every delimiter character (`{}`, `[]`, `"`, `:`, `,`)
    is its own token.

    Deliberately conservative: may overestimate for text-heavy content (harmless,
    just triggers extra chunking), but never underestimates (which would cause
    context-overflow errors).
    """
    try:
        return max(1, len(json.dumps(payload, default=str)))
    except Exception:
        return 1


def estimate_prompt_tokens(system: str = "", user_template: str = "",
                           framework_overhead: int = _DEFAULT_FRAMEWORK_OVERHEAD_TOKENS,
                           ) -> int:
    """Estimate the prompt overhead in tokens.

    Strips {placeholder} markers from `user_template` since the payload that
    gets injected at runtime is tracked separately via `estimate_payload_tokens`.
    """
    text_parts: list[str] = []
    if system:
        text_parts.append(system)
    if user_template:
        text_parts.append(re.sub(r"\{[^}]+\}", "", user_template))
    combined = " ".join(text_parts)
    return max(1, len(combined)) + framework_overhead


# ---------------------------------------------------------------------------
# Downstream chunk-size math
# ---------------------------------------------------------------------------

def compute_downstream_chunk_size(
    items: list[Any],
    model_str: str,
    *,
    max_workers: int = 8,
    prompt_overhead_tokens: Optional[int] = None,
    context_window_override: Optional[int] = None,
    safety_factor: float = _CONTEXT_SAFETY_FACTOR,
    min_chunk: int = 1,
    max_chunk: int = 500,
) -> int:
    """How many items per chunk to feed to a downstream agent?

    The goal: `prompt_overhead + payload(chunk) + expected_output ≤
    context_window * safety_factor`. We solve for `len(chunk)` and clamp.

    A pragmatic split also caps at `ceil(len(items) / max_workers)` so we
    actually use parallelism — no point in one big chunk when 8 workers are
    available.

    Returns an integer in [min_chunk, max_chunk]. Use it as
    ``chunks = [items[i:i+size] for i in range(0, len(items), size)]``.
    """
    if not items:
        return min_chunk

    ctx = context_window_override or get_model_context_window(model_str)
    overhead = prompt_overhead_tokens or _DEFAULT_FRAMEWORK_OVERHEAD_TOKENS
    budget = int((ctx - overhead) * safety_factor)
    if budget <= 0:
        # Pathological case (overhead exceeds window) — fall back to 1.
        return max(min_chunk, 1)

    # average tokens per item
    sample = items[: min(50, len(items))]
    avg_per_item = max(1, estimate_payload_tokens(sample) // max(1, len(sample)))

    by_budget = max(1, budget // avg_per_item)
    by_workers = math.ceil(len(items) / max(1, max_workers))

    return max(min_chunk, min(max_chunk, by_budget, by_workers))


# ---------------------------------------------------------------------------
# CLI for ad-hoc inspection
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Inspect a model's known context window and recommended "
                    "downstream chunk size for an items-list of a given size."
    )
    ap.add_argument("model", help="model string, e.g. openrouter/anthropic/claude-sonnet-4-6")
    ap.add_argument("--items", type=int, default=1000,
                    help="number of items to chunk for the downstream agent")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--avg-item-tokens", type=int, default=250,
                    help="rough average tokens per item; used to synthesize a sample")
    args = ap.parse_args()

    ctx = get_model_context_window(args.model)
    sample_items = [{"i": i, "pad": "x" * args.avg_item_tokens} for i in range(args.items)]
    size = compute_downstream_chunk_size(sample_items, args.model,
                                         max_workers=args.workers)
    print(f"model={args.model}")
    print(f"context_window: {ctx:,} tokens")
    print(f"recommended chunk size: {size} items per call")
    print(f"  → {math.ceil(args.items / size)} chunks, "
          f"≈{math.ceil((args.items / size) / args.workers)} round(s) "
          f"over {args.workers} workers")
