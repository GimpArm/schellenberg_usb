"""Stable per-blind identifier helpers."""

from __future__ import annotations

from collections.abc import MutableSet
from uuid import UUID, uuid4


def normalize_blind_id(value: object) -> str | None:
    """Return one canonical UUID string or None for legacy/invalid values."""
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return None


def generate_blind_id() -> str:
    """Generate a new random per-blind UUID."""
    return str(uuid4())


def claim_blind_id(value: object, used_ids: MutableSet[str]) -> tuple[str, bool]:
    """Claim a valid unique ID or generate a collision-free replacement.

    Return the claimed ID and whether persisted data must be updated.
    """
    candidate = normalize_blind_id(value)
    if candidate is not None and candidate not in used_ids:
        used_ids.add(candidate)
        return candidate, candidate != value

    while True:
        candidate = generate_blind_id()
        if candidate not in used_ids:
            used_ids.add(candidate)
            return candidate, True
