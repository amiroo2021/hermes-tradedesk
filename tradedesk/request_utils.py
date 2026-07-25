"""Deterministic accessor helpers for the normalize-layer canonical shape.

The TradeDesk normalize layer places user-supplied operation-specific fields
inside ``structured_request`` while keeping a fixed six-key top-level
schema::

    {version, exchange, account, operation, parent_operation,
     structured_request}

TP/SL agent code paths (which were authored before the normalize layer was
introduced) read operation-specific fields directly from the top level.
The accessor in this module mediates that contract gap: top-level values
win when present (preserving existing direct-caller compatibility);
``structured_request`` is the fallback when the top level omits the key.

Conflict behavior is deterministic: top-level wins; the strict variant
(``_request_field_strict``) raises when both layers carry the same key with
disagreeing non-None values. The strict variant is for tests only.

No mapping is mutated. No fields are copied onto the normalized top level.
"""
from collections.abc import Mapping


def _request_field(request, key, default=None):
    """Read ``key`` from a normalized TradeDesk request.

    Precedence (deterministic, no silent fallback that selects conflicting
    values):

      1. If ``request`` is not a ``Mapping``, return ``default``.
      2. If ``key in request``, return ``request[key]`` (top-level always
         wins; a deliberate ``None`` value counts as explicitly present).
      3. Otherwise, if ``request["structured_request"]`` is a ``Mapping``
         and ``key in structured_request``, return
         ``structured_request[key]``.
      4. Otherwise, return ``default``.

    No mutation is performed on either dict.
    """
    if not isinstance(request, Mapping):
        return default

    if key in request:
        return request[key]

    structured = request.get("structured_request")
    if isinstance(structured, Mapping) and key in structured:
        return structured[key]

    return default


def _request_field_strict(request, key, default=None):
    """Test-only strict accessor. Raises ``ValueError`` when both top-level
    and ``structured_request`` carry the same key with disagreeing non-None
    values. Use only in regression tests; production TP/SL code uses
    ``_request_field``.
    """
    if not isinstance(request, Mapping):
        return default

    has_tl = key in request and request[key] is not None
    structured = request.get("structured_request")
    has_sr = (
        isinstance(structured, Mapping)
        and key in structured
        and structured[key] is not None
    )
    if has_tl and has_sr and request[key] != structured[key]:
        raise ValueError(
            f"request_field conflict for {key!r}: "
            f"top-level={request[key]!r} vs "
            f"structured_request={structured[key]!r}"
        )
    if has_tl:
        return request[key]
    if has_sr:
        return structured[key]
    return default
