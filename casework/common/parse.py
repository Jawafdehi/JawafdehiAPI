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


def _balanced_span(text: str, start: int, opener: str, closer: str):
    """Return the balanced ``opener...closer`` substring starting at ``start``,
    or None if it never closes. JSON-string aware, so an ``opener``/``closer``
    inside a quoted value (e.g. a Nepali evidence_quote containing ``]``) does
    not throw off the depth count — unlike a bare bracket counter."""
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
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def balanced_object(text: str, start: int):
    """Balanced ``{...}`` substring from ``start``, string-aware. See _balanced_span."""
    return _balanced_span(text, start, "{", "}")


def balanced_array(text: str, start: int):
    """Balanced ``[...]`` substring from ``start``, string-aware. See _balanced_span."""
    return _balanced_span(text, start, "[", "]")


def _strip_fence(text: str) -> str:
    """Drop a leading ```-fenced block's fences, returning the body.

    Returns `text` unchanged when there is no complete fence pair.
    """
    if "```" not in text:
        return text
    start = text.find("```")
    nl = text.find("\n", start)
    if nl == -1:
        return text
    end = text.find("```", nl + 1)
    if end == -1:
        return text
    return text[nl + 1 : end].strip()


def parse_object_response(response_text, required_key):
    """First balanced JSON OBJECT in the response that carries `required_key`.

    ``parse_extraction_response`` only ever returns a LIST -- it is shaped for
    ``{"allegations": [...]}`` replies. A reply whose payload is a long STRING
    (``{"description": "### क) …"}``) needs its own parser.

    Every ``{`` position is tried, not just ``text.find("{")``: a model reply
    can open with an unrelated object (a preamble, an echoed tool argument)
    before the real payload, and stopping at the first brace returns None on
    exactly those. Ported from the donor's ``_parse_response``
    (``0321a85:casework/enrich_description.py``).

    Returns the parsed dict, or None.
    """
    import json

    text = _strip_fence((response_text or "").strip())
    for obj_start in range(len(text)):
        if text[obj_start] != "{":
            continue
        block = balanced_object(text, obj_start)
        if block is None:
            continue
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and required_key in obj:
            return obj
    return None


def parse_extraction_response(response_text, wrapper_keys):
    """Extract a JSON array from an LLM response (handles ```fences``` and
    {"<key>": [...]} wrappers). Returns the list, or None."""
    import json

    text = _strip_fence((response_text or "").strip())

    obj_start = text.find("{")
    if obj_start != -1:
        frag = balanced_object(text, obj_start)
        if frag is not None:
            try:
                obj = json.loads(frag)
                if isinstance(obj, dict):
                    for key in wrapper_keys:
                        if isinstance(obj.get(key), list) and obj[key]:
                            return obj[key]
            except json.JSONDecodeError:
                pass

    arr_start = text.find("[")
    if arr_start != -1:
        frag = balanced_array(text, arr_start)
        if frag is not None:
            try:
                entries = json.loads(frag)
                if isinstance(entries, list) and entries:
                    return entries
            except json.JSONDecodeError:
                pass
    return None
