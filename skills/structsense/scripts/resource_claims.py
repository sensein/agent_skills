"""Validate source-stated identifiers and versions for Dataset/Tool resources.

The source text is the normalized text consumed by the extractor. A quote is
evidence of what the document says, not independent verification of a claim.
"""
from __future__ import annotations

import re


def _stated_exactly(value: str, quote: str) -> bool:
    """A version/ID must be a complete token, not a prefix of another value."""
    return bool(re.search(r"(?<![\w.])" + re.escape(value) + r"(?!\w|\.\w)", quote))


def checked_claims(resource: dict, source_text: str | None) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Return supported (field, value, quote) claims and explicit rejection reasons."""
    if resource.get("type") not in {"Dataset", "Tool"}:
        return [], []
    accepted: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for field in ("identifiers", "versions"):
        for index, claim in enumerate(resource.get(field) or []):
            context = f"{resource.get('name', '<unnamed>')}.{field}[{index}]"
            if not isinstance(claim, dict):
                errors.append(f"{context}: claim must be an object")
                continue
            value = claim.get("value")
            scheme = claim.get("scheme") if field == "identifiers" else None
            evidence = claim.get("evidence")
            quote = evidence.get("quote") if isinstance(evidence, dict) else None
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{context}: value must be nonempty text")
            elif field == "identifiers" and (not isinstance(scheme, str) or not scheme.strip()):
                errors.append(f"{context}: identifier scheme must be nonempty text")
            elif not isinstance(quote, str) or not quote.strip():
                errors.append(f"{context}: evidence.quote must be nonempty text")
            elif source_text is None or quote not in source_text:
                errors.append(f"{context}: evidence quote is not in the normalized source text")
            elif not _stated_exactly(value, quote) or (scheme is not None and not _stated_exactly(scheme, quote)):
                errors.append(f"{context}: value and scheme must occur verbatim in the quote")
            else:
                stated = f"{scheme}:{value}" if scheme is not None else value
                accepted.append((field, stated, quote))
    return accepted, errors


def require_supported_claims(result: dict, source_text: str) -> None:
    """Reject unsupported catalog claims before a pipeline result is published."""
    for container_name in ("judge_resource", "aligned_resources", "extracted_resources"):
        container = result.get(container_name)
        if not container:
            continue
        resources = (item for group in container.values() for item in group) if isinstance(container, dict) else iter(container)
        for resource in resources:
            _, errors = checked_claims(resource, source_text)
            if errors:
                raise ValueError("Unsupported resource metadata: " + "; ".join(errors))
