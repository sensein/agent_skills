"""Minimal multi-provider LLM client.

Wraps OpenAI / OpenRouter / Anthropic / Ollama behind one call() interface so
the rest of the pipeline doesn't care which provider is in use.

Usage:
    from llm_client import call

    raw = call(
        model="openrouter/anthropic/claude-sonnet-4-6",
        system=PROMPT_EXTRACTOR,
        user=USER_TEXT,
        json_mode=True,
        temperature=0,
    )

Model string conventions (same as CrewAI / structsense):
    openai/<model>                  -> OpenAI direct
    openrouter/<provider>/<model>   -> OpenRouter
    anthropic/<model>               -> Anthropic direct
    ollama/<model>                  -> local Ollama
    gemini/<model>                  -> Google Gemini
"""
from __future__ import annotations

import os
import json
import logging
from typing import Optional

logger = logging.getLogger("llm_client")


def _provider_and_model(model: str) -> tuple[str, str]:
    """Split 'openrouter/anthropic/claude-...' into ('openrouter', 'anthropic/claude-...')."""
    if "/" not in model:
        raise ValueError(f"model must include provider prefix, got {model!r}")
    provider, _, rest = model.partition("/")
    return provider.lower(), rest


def call(
    model: str,
    system: str,
    user: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    json_mode: bool = False,
    response_schema: Optional[dict] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """Single, synchronous LLM call. Returns the raw text reply."""
    provider, real_model = _provider_and_model(model)
    extra = extra or {}

    if provider in {"openai", "openrouter"}:
        return _openai_compatible(
            provider, real_model, system, user, temperature, max_tokens,
            json_mode, response_schema, api_key, base_url, extra,
        )
    if provider == "anthropic":
        return _anthropic(real_model, system, user, temperature, max_tokens,
                          api_key, extra)
    if provider == "ollama":
        return _ollama(real_model, system, user, temperature, max_tokens,
                       json_mode, base_url, extra)
    if provider == "gemini":
        return _gemini(real_model, system, user, temperature, max_tokens,
                       json_mode, api_key, extra)
    raise ValueError(f"unsupported provider: {provider!r}")


# ---------------------------------------------------------------------------
# OpenAI-compatible (OpenAI direct, OpenRouter, vLLM, …)
# ---------------------------------------------------------------------------
def _openai_compatible(provider, real_model, system, user, temperature,
                       max_tokens, json_mode, response_schema, api_key,
                       base_url, extra):
    from openai import OpenAI  # pip install openai
    if provider == "openrouter":
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        base_url = base_url or "https://openrouter.ai/api/v1"
    else:
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
    client = OpenAI(api_key=api_key, base_url=base_url)

    kwargs = dict(
        model=real_model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if response_schema is not None:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "out", "schema": response_schema, "strict": True},
        }
    elif json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    kwargs.update(extra)

    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
def _anthropic(real_model, system, user, temperature, max_tokens, api_key, extra):
    from anthropic import Anthropic  # pip install anthropic
    client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
    kwargs = dict(
        model=real_model,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[{"role": "user", "content": user}],
    )
    kwargs.update(extra)
    resp = client.messages.create(**kwargs)
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
def _ollama(real_model, system, user, temperature, max_tokens, json_mode,
            base_url, extra):
    import requests
    base = base_url or "http://localhost:11434"
    payload = {
        "model": real_model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "stream": False,
    }
    if json_mode:
        payload["format"] = "json"
    payload.update(extra)
    r = requests.post(f"{base}/api/chat", json=payload, timeout=600)
    r.raise_for_status()
    return ((r.json() or {}).get("message") or {}).get("content") or ""


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def _gemini(real_model, system, user, temperature, max_tokens, json_mode,
            api_key, extra):
    import google.generativeai as genai  # pip install google-generativeai
    genai.configure(api_key=api_key or os.environ.get("GEMINI_API_KEY"))
    config = {
        "temperature": temperature,
        "max_output_tokens": max_tokens,
    }
    if json_mode:
        config["response_mime_type"] = "application/json"
    config.update(extra)
    model = genai.GenerativeModel(real_model, system_instruction=system,
                                  generation_config=config)
    resp = model.generate_content(user)
    return resp.text or ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = call(
        model=os.environ.get("DEMO_MODEL", "ollama/qwen2.5:7b"),
        system="You output strict JSON only. No prose, no fences.",
        user='Say hello in JSON: {"greeting": "..."}',
        json_mode=True,
    )
    print(out)
