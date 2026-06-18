"""Render NGM judicial court-case records to markdown for the review judge.

The official court record (registration / verdict dates, parties, hearings) is the
authoritative source for verifying a case's claimed figures, dates and accused list.
We pull it once per case and hand it to EVERY provider (inline in the judge excerpts,
and as a staged file for the agentic provider) so figures stated in the case can be
checked against the record rather than taken on faith.
"""

from sourcing import ngm_client

# Known scalar fields rendered first, in a sensible reading order. Any other scalar
# fields on the record are appended after these (the NGM schema may grow).
_SCALAR_ORDER = (
    "court",
    "court_name",
    "case_number",
    "case_no",
    "case_type",
    "subject",
    "title",
    "status",
    "stage",
    "registration_date_bs",
    "registration_date_ad",
    "verdict_date_bs",
    "verdict_date_ad",
)


def _row(d):
    """Compact one record row (dict) to 'k: v, k: v' over its non-empty fields."""
    return ", ".join(f"{k}: {v}" for k, v in d.items() if v not in (None, "", [], {}))


def court_case_md(ref, record):
    """Render one NGM court-case record to a markdown block.

    `ref` is the "<court>:<case_number>" reference; `record` is the dict from
    `ngm_client.get_court_case` (or None when the ref was not found).
    """
    if not record:
        return (
            f"## NGM court record `{ref}`\n\n"
            "(no matching record found in the NGM judicial database)"
        )

    out = [
        f"## NGM court record `{ref}`",
        "Authoritative court-case data from the NGM judicial database "
        "(official record — use it to verify dates, parties and figures).",
        "",
    ]
    shown = set()
    for k in _SCALAR_ORDER:
        v = record.get(k)
        if v not in (None, "", [], {}):
            out.append(f"- **{k}**: {v}")
            shown.add(k)
    # Any remaining scalar fields the schema may carry.
    for k, v in record.items():
        if k in shown or k in ("entities", "parties", "hearings"):
            continue
        if isinstance(v, (dict, list)) or v in (None, ""):
            continue
        out.append(f"- **{k}**: {v}")

    parties = record.get("entities") or record.get("parties") or []
    if parties:
        out.append("\n### Parties")
        for p in parties:
            out.append(f"- {_row(p) if isinstance(p, dict) else p}")

    hearings = record.get("hearings") or []
    if hearings:
        out.append("\n### Hearings")
        for h in hearings[:60]:
            out.append(f"- {_row(h) if isinstance(h, dict) else h}")

    return "\n".join(out)


def case_records(case):
    """Pull every NGM court record referenced by a case.

    Returns a list of (ref, markdown) pairs (one per court ref); empty when the
    case has no court references. Per-ref failures degrade to an error note
    rather than aborting the review.
    """
    out = []
    for ref in ngm_client.court_refs_for_case(case):
        try:
            record = ngm_client.get_court_case(ref)
        except ngm_client.NgmNotFound:
            record = None
        except Exception as e:  # noqa: BLE001 - NGM is best-effort context
            out.append((ref, f"## NGM court record `{ref}`\n\n(lookup error: {e})"))
            continue
        out.append((ref, court_case_md(ref, record)))
    return out


def case_markdown(case):
    """Combined markdown for all of a case's NGM court records ('' if none)."""
    return "\n\n---\n\n".join(md for _, md in case_records(case))
