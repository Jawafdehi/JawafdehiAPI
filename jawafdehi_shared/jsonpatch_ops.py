"""RFC-6902 JSON Patch op-list validation, shared across the JSON-LD write planes.

Pure + dependency-free (no Django, no settings, not even ``jsonpatch``): this
validates the SHAPE of an op list and enforces a caller-supplied immutable-path
policy BEFORE the patch is handed to ``jsonpatch.apply_patch``. Applying first
and checking after would let a blocked op mutate the in-memory document.

Every JSON-LD write plane on the platform patches a stored document by ``@id``
and therefore needs the same two guarantees:

  * the op list is well-formed (a malformed op is a 422, not a 500 out of the
    patch library);
  * no op — including the ``from`` pointer of ``move``/``copy``, which is easy
    to forget — targets an immutable or server-owned path.

The immutable-path *policy* is per-plane (an entity's identity keys are not a
material's), so it is injected as ``is_blocked``; the op grammar is universal
and lives here so a plane added later cannot quietly ship a weaker check.

NOTE: ``entities.write_validation.normalize_patch_ops`` predates this module and
still carries its own copy. It is deliberately left alone — the NES patch path
has no test coverage today, so converging it belongs in a change that can prove
the behaviour is unchanged, not in a materials PR.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

#: The full RFC-6902 operation set. ``test`` is included: it mutates nothing and
#: lets a client assert a precondition inside the patch itself.
ALLOWED_OPS = frozenset({"add", "remove", "replace", "move", "copy", "test"})

#: Ops that write. ``test`` is excluded — asserting the current value of an
#: immutable path is harmless and is a legitimate way to guard a patch.
_MUTATING_OPS = frozenset({"add", "remove", "replace", "move", "copy"})


def normalize_patch_ops(
    raw_ops: Any,
    *,
    is_blocked: Optional[Callable[[str], bool]] = None,
) -> List[Dict[str, Any]]:
    """Validate + normalize an RFC-6902 patch list; return the cleaned ops.

    ``is_blocked`` is the per-plane immutable-path predicate; when supplied it is
    applied to every mutating op's ``path`` AND to the ``from`` pointer of
    ``move``/``copy``.

    Raises ``ValueError`` (the caller maps it to 422) on a malformed op or a
    blocked target. The whole list is rejected — a patch is atomic, so one bad op
    must not leave its well-formed siblings applied.
    """
    if not isinstance(raw_ops, list) or not raw_ops:
        raise ValueError("patch_ops must be a non-empty list.")

    normalized: List[Dict[str, Any]] = []
    for raw in raw_ops:
        if not isinstance(raw, dict):
            raise ValueError("Each patch op must be an object.")

        op = str(raw.get("op", "")).lower()
        if op not in ALLOWED_OPS:
            raise ValueError(
                f"Unsupported patch operation {raw.get('op')!r}. "
                f"Allowed ops: {sorted(ALLOWED_OPS)}"
            )

        path = raw.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path must be a valid JSON Pointer starting with '/'.")

        from_path = raw.get("from")
        if op in {"move", "copy"} and (
            not isinstance(from_path, str) or not from_path.startswith("/")
        ):
            raise ValueError(f"{op!r} operation requires a valid 'from' pointer.")
        if op in {"add", "replace", "test"} and "value" not in raw:
            raise ValueError(f"{op!r} operation requires 'value'.")

        if is_blocked is not None and op in _MUTATING_OPS:
            if is_blocked(path):
                raise ValueError(f"Patching path '{path}' is not allowed.")
            # `move` REMOVES from its source, so a move out of a blocked path
            # mutates that path just as surely as a `remove` on it would.
            if isinstance(from_path, str) and is_blocked(from_path):
                raise ValueError(f"Patching path '{from_path}' is not allowed.")

        clean: Dict[str, Any] = {"op": op, "path": path}
        if "value" in raw:
            clean["value"] = raw["value"]
        if from_path is not None:
            clean["from"] = from_path
        normalized.append(clean)

    return normalized


def blocked_path_predicate(prefixes) -> Callable[[str], bool]:
    """Build an ``is_blocked`` predicate from a set of JSON Pointer prefixes.

    A pointer is blocked when it equals a prefix or descends into one, so
    blocking ``/@id`` also blocks ``/@id/0``. RFC-6901 escapes ``~`` as ``~0``
    and ``/`` as ``~1``; a JSON-LD key like ``@id`` or ``jawafdehi:caseNumber``
    contains neither, so it appears literally in the pointer and a direct prefix
    match is correct.
    """
    frozen = frozenset(prefixes)

    def _is_blocked(path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in frozen)

    return _is_blocked
