"""Tolerant JSON parsing for ill-behaved upstream APIs and LLM outputs.

Some external APIs (NCBI E-utilities, CORE, occasionally bioRxiv) and some
LLM completions return JSON whose string values contain raw control characters
(``\\x00-\\x1F``).  RFC 8259 forbids those inside strings, and stdlib
``json.loads`` correctly rejects them with::

    json.JSONDecodeError: Invalid control character at: line 1 column N (char N-1)

That error tends to abort otherwise-fine pipelines (long-running PRISMA reviews,
LLM tool-call parsing).  This module provides a layered repair strategy
chosen per call site:

HTTP responses (NCBI, CORE, bioRxiv, OpenAlex, …) — use stdlib only:

* :func:`loads`  — try strict parse first; on control-char failure, retry with
  ``strict=False``.  Other JSON errors propagate unchanged.  Upstream APIs
  emit *control-char* malformations, not truncated / unquoted-key JSON, so
  pulling in ``json_repair`` for the HTTP boundary would be over-broad.
* :func:`loads_response` — same, but for ``httpx.Response`` objects.

LLM-output validation — use the full repair stack (``json_repair``):

* :func:`model_validate_json_safe` — tries pydantic-core strict first, falls
  back to stdlib ``strict=False``, and finally to
  `json_repair <https://github.com/mangiucugna/json_repair>`_, which handles
  truncated JSON, unquoted keys, single quotes, trailing commas, smart
  quotes, embedded markdown fences, concatenated objects — everything LLMs
  commonly produce.  Only the original pydantic ``ValidationError`` is
  surfaced if all three layers fail.

Each repair layer logs a WARNING identifying which fallback was used so the
production telemetry shows how often each path fires.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Type, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Used only for telemetry (counting control chars repaired) — not for cleanup.
_CTRL_RE = re.compile(r"[\x00-\x1F]")


def _is_control_char_error(exc: json.JSONDecodeError) -> bool:
    """True if the decode failure was specifically about a forbidden control char."""
    return "control character" in (exc.msg or "").lower()


def loads(text: str | bytes, *, source: str | None = None) -> Any:
    """Parse JSON from *text*, tolerating raw control characters in strings.

    Strict parse runs first.  On a control-character ``JSONDecodeError`` the
    parse is retried with ``strict=False``.  All other errors (``Expecting
    value``, ``Unterminated string``, etc.) re-raise unchanged.

    ``source`` is used purely for logging so the warning identifies which
    upstream emitted the malformed payload (e.g. ``"ncbi:esearch"``).
    """
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if not _is_control_char_error(exc):
            raise
        # Stdlib's strict=False permits raw control chars (0x00-0x1F) inside
        # JSON strings — exactly the upstream-bug case we want to tolerate.
        try:
            value = json.loads(text, strict=False)
        except json.JSONDecodeError:
            raise exc from None  # repair didn't help; surface the original
        logger.warning(
            "[json_safe] repaired malformed JSON from %s "
            "(tolerated %d control char(s); first error at char %d)",
            source or "<unknown>",
            len(_CTRL_RE.findall(text)),
            exc.pos,
        )
        return value


def loads_response(resp: httpx.Response, *, source: str | None = None) -> Any:
    """Drop-in replacement for ``httpx.Response.json()`` that tolerates control chars."""
    if source is None and getattr(resp, "request", None) is not None:
        source = str(resp.request.url)
    return loads(resp.text, source=source or "<unknown>")


def model_validate_json_safe(
    target_type: Type[T],
    text: str,
    *,
    source: str | None = None,
) -> T:
    """``BaseModel.model_validate_json(text)`` with a layered repair fallback.

    Attempt order:

    1. **pydantic-core strict** — fast path; succeeds for well-formed JSON.
    2. **stdlib ``json.loads(strict=False)``** — handles raw control chars
       in strings (the common upstream-API and basic LLM bug).
    3. **`json_repair.loads`** — handles truncated JSON, unquoted keys,
       single quotes, trailing commas, smart quotes, embedded markdown
       fences, and other free-form LLM output disasters.

    The original pydantic ``ValidationError`` is re-raised if all three
    layers fail to produce a valid dict, so callers see the actionable
    schema error rather than an obscure JSON one.
    """
    # Tier 1: strict pydantic-core
    try:
        return target_type.model_validate_json(text)  # type: ignore[attr-defined]
    except Exception as pyd_exc:
        first_pyd_exc = pyd_exc

    # Tier 2: stdlib relaxed (handles control chars in strings)
    try:
        data = loads(text, source=source)
        result = target_type.model_validate(data)  # type: ignore[attr-defined]
        logger.warning(
            "[json_safe] LLM JSON repaired via stdlib strict=False (source=%s)",
            source or "<unknown>",
        )
        return result
    except json.JSONDecodeError:
        pass  # fall through to json_repair
    except Exception:
        # JSON parsed but validation failed — schema mismatch, not a JSON bug.
        # Re-raise the original pydantic error for the actionable message.
        raise first_pyd_exc

    # Tier 3: json_repair (handles truncated / quoted / fenced LLM output)
    try:
        import json_repair  # type: ignore[import-not-found]
    except ImportError:
        raise first_pyd_exc

    try:
        # json_repair.loads always returns *something* — empty dict on
        # complete garbage — so we need to validate the result against
        # the target schema rather than trust success.
        repaired = json_repair.loads(text)
        if repaired in ({}, [], "", None):
            # Likely json_repair couldn't extract anything useful.
            raise first_pyd_exc
        result = target_type.model_validate(repaired)  # type: ignore[attr-defined]
        logger.warning(
            "[json_safe] LLM JSON repaired via json_repair (source=%s, original error: %s)",
            source or "<unknown>",
            type(first_pyd_exc).__name__,
        )
        return result
    except Exception:
        raise first_pyd_exc from None
