"""Helpers for persisted Schellenberg status identities."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

type StatusIdentity = tuple[str, str]

_STATUS_IDENTITY_PATTERN = re.compile(
    r"^([0-9A-Fa-f]{6})\s*[/:-]\s*([0-9A-Fa-f]{1,2})$"
)


def normalize_status_identity(
    device_id: object, device_enum: object
) -> StatusIdentity | None:
    """Normalize one status identity, returning None when it is malformed."""
    normalized_id = str(device_id).strip().upper()
    normalized_enum = str(device_enum).strip().upper()
    if len(normalized_enum) <= 2:
        normalized_enum = normalized_enum.zfill(2)
    if (
        len(normalized_id) != 6
        or len(normalized_enum) != 2
        or any(character not in "0123456789ABCDEF" for character in normalized_id)
        or any(character not in "0123456789ABCDEF" for character in normalized_enum)
    ):
        return None
    return normalized_id, normalized_enum


def normalize_status_identities(value: object) -> tuple[StatusIdentity, ...]:
    """Normalize persisted list or text representations, ignoring invalid items."""
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        try:
            return parse_status_identities_text(value)
        except ValueError:
            return ()
    if not isinstance(value, Iterable) or isinstance(value, (bytes, Mapping)):
        return ()

    identities: list[StatusIdentity] = []
    for item in value:
        identity: StatusIdentity | None = None
        if isinstance(item, Mapping):
            identity = normalize_status_identity(
                item.get("device_id", ""),
                item.get("enum", item.get("device_enum", "")),
            )
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            identity = normalize_status_identity(item[0], item[1])
        if identity is not None and identity not in identities:
            identities.append(identity)
    return tuple(identities)


def parse_status_identities_text(value: object) -> tuple[StatusIdentity, ...]:
    """Parse comma/newline-separated DEVICE_ID/ENUM identities strictly."""
    text = str(value or "").strip()
    if not text:
        return ()
    identities: list[StatusIdentity] = []
    for token in re.split(r"[,;\n]+", text):
        token = token.strip()
        if not token:
            continue
        match = _STATUS_IDENTITY_PATTERN.fullmatch(token)
        if match is None:
            raise ValueError(f"invalid status identity: {token}")
        identity = normalize_status_identity(match.group(1), match.group(2))
        if identity is None:
            raise ValueError(f"invalid status identity: {token}")
        if identity not in identities:
            identities.append(identity)
    return tuple(identities)


def serialize_status_identities(
    identities: Iterable[StatusIdentity],
) -> list[dict[str, str]]:
    """Return stable JSON-compatible status identity dictionaries."""
    return [
        {"device_id": device_id, "enum": device_enum}
        for device_id, device_enum in identities
    ]


def format_status_identities(value: object) -> str:
    """Format persisted identities for a multiline config-flow text field."""
    return "\n".join(
        f"{device_id}/{device_enum}"
        for device_id, device_enum in normalize_status_identities(value)
    )
