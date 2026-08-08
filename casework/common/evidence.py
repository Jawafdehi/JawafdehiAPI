"""Read and merge a case's `/evidence` list. Shared by every stage that writes it."""


def current_evidence(case):
    """The case's evidence in the `{material_iri, additional_details}` shape PATCH wants."""
    return [
        {"material_iri": e.get("material_iri"),
         "additional_details": e.get("additional_details") or ""}
        for e in (case.get("evidence") or [])
        if e.get("material_iri")
    ]


def merge_evidence(current, additions):
    """Append `(material_iri, note)` pairs not already present, preserving order.

    Never reorders, rewrites or drops an existing entry: `PATCH /evidence` replaces
    the whole list, so anything left out is deleted. An IRI already present is
    skipped rather than allowed to overwrite a note a human may have edited.
    """
    have = {e["material_iri"] for e in current}
    merged = list(current)
    for iri, note in additions:
        if iri in have:
            continue
        merged.append({"material_iri": iri, "additional_details": note})
        have.add(iri)
    return merged
