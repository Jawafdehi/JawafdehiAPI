"""LLM-response parsing helpers ported from the deleted ``casework/common.py``
(recovered from git history at commit ``0321a85``, pre-collapse).

These are string-aware / brace-balanced parsers: a naive brace regex broke on
braces appearing inside quoted JSON string fields (e.g. an ``evidence_quote``
containing Devanagari text with literal braces) and on deeper nesting.
"""


def is_valid_iso_date(date_str) -> bool:
    """Strict YYYY-MM-DD validation."""
    from datetime import date

    if not isinstance(date_str, str):
        return False
    candidate = date_str.strip()
    if len(candidate) != 10 or candidate[4] != "-" or candidate[7] != "-":
        return False
    try:
        date.fromisoformat(candidate)
        return True
    except (ValueError, TypeError):
        return False


def balanced_object(text: str, start: int):
    """Return the balanced ``{...}`` substring starting at ``start``, or None if
    it never closes. JSON-string aware, so braces inside quoted values (e.g. an
    evidence_quote) don't throw off the depth count — unlike a brace regex."""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_extraction_response(response_text, wrapper_keys):
    """Extract a JSON object or array from an LLM response (handles ```fences```
    and {"<key>": [...]} / {"<key>": {...}} wrappers). Returns the unwrapped
    value, or None.

    NOTE: this differs from the donor (0321a85) by one condition. The donor
    only unwrapped list-typed wrapper values (`isinstance(obj.get(key), list)`);
    it never returned a dict-typed wrapper value, so it could not satisfy this
    port's required test (`parse_extraction_response(body, ("result",)) ==
    {"bigo": 500}`) or its declared `-> dict | None` interface. Broadened the
    check to `(list, dict)` — everything else (fence stripping, brace-depth
    scan, array fallback) is unchanged from the donor.
    """
    import json

    text = (response_text or "").strip()
    if "```" in text:
        start = text.find("```")
        nl = text.find("\n", start)
        if nl != -1:
            end = text.find("```", nl)
            if end != -1:
                text = text[nl + 1 : end].strip()

    obj_start = text.find("{")
    if obj_start != -1:
        depth, obj_end = 0, -1
        for i, ch in enumerate(text[obj_start:], obj_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    obj_end = i
                    break
        if obj_end != -1:
            try:
                obj = json.loads(text[obj_start : obj_end + 1])
                if isinstance(obj, dict):
                    for key in wrapper_keys:
                        value = obj.get(key)
                        if isinstance(value, (list, dict)) and value:
                            return value
            except json.JSONDecodeError:
                pass

    arr_start = text.find("[")
    if arr_start != -1:
        depth, arr_end = 0, -1
        for i, ch in enumerate(text[arr_start:], arr_start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    arr_end = i
                    break
        if arr_end != -1:
            try:
                entries = json.loads(text[arr_start : arr_end + 1])
                if isinstance(entries, list) and entries:
                    return entries
            except json.JSONDecodeError:
                pass
    return None
