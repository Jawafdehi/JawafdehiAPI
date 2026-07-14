"""Case provider: get a normalized case dict by slug.

The rule-centered scorer/casetype/converter all consume a single "case dict"
shape, namely the one produced by ``cases.serializers.CaseDetailSerializer``:

    {title, state, slug, description, key_allegations, timeline, entities,
     court_cases, missing_details,
     evidence: [{material_iri, additional_details,
                 material: {display_name, material_type, urls: [{link, role}]}}]}

This module produces that shape from one of two backends, selected by
``settings.REVIEW_CASE_SOURCE``:

  - "local"  (default): serialize a ``cases.models.Case`` row from THIS database.
               After ``seed_jawafdehi`` has imported cases/sources/entities,
               everything runs fully offline (no network, no JDS token).
  - "remote" : fall back to the live JDS public API via ``jds_client``.

Using the project's own serializer for the local path guarantees the dict is
byte-for-byte the same shape the scorer already understands, so no engine code
had to change when porting the casework system into jawafdehi-api.
"""

from django.conf import settings

from . import jds_client


class CaseNotFound(Exception):
    pass


def _serialize_local_case(slug):
    """Serialize a local cases.Case to the CaseDetailSerializer dict shape."""
    from cases.models import Case
    from cases.serializers import CaseDetailSerializer

    try:
        case = Case.objects.get(slug=slug)
    except Case.DoesNotExist:
        # Submissions are resolved to a canonical slug at submit time
        # (review.serializers.SubmitSerializer), so reaching here means the case
        # was removed/renamed between submit and grade — not a "needs seeding"
        # condition (the review DB IS the live case DB in the monolith).
        raise CaseNotFound(f"No Jawafdehi case found with slug '{slug}'.")
    # context=None is fine: get_url builds relative URLs when there is no request.
    data = CaseDetailSerializer(case, context={}).data
    # Normalize to plain dict and ensure slug present.
    data = dict(data)
    data.setdefault("slug", slug)
    return data


def _serialize_local_case_by_id(case_id):
    """Serialize a local cases.Case (looked up by pk) to the case dict shape.

    Reviews key on the stable case PK, so the payload build resolves the case by
    id — a re-slug between submit and grade can never orphan the lookup. The
    serialized dict already carries the case's CURRENT slug, so no fix-up here.
    """
    from cases.models import Case
    from cases.serializers import CaseDetailSerializer

    try:
        case = Case.objects.get(pk=case_id)
    except Case.DoesNotExist:
        raise CaseNotFound(f"No Jawafdehi case found with id {case_id}.")
    return dict(CaseDetailSerializer(case, context={}).data)


def get_case(slug):
    """Return the normalized case dict for a slug, honoring REVIEW_CASE_SOURCE.

    Legacy/slug-addressed path — kept for the remote (JDS) source. The case
    review pipeline now resolves cases by PK via :func:`get_case_by_id`.
    """
    source = getattr(settings, "REVIEW_CASE_SOURCE", "local")
    if source == "remote":
        case = jds_client.get_case(slug)
        case.setdefault("slug", slug)
        return case
    return _serialize_local_case(slug)


def get_case_by_id(case_id):
    """Return the normalized case dict for a case PK (the review payload key).

    Only the local source is PK-addressable; the remote/JDS API (:func:`get_case`)
    is slug-addressed and legacy, so id lookups always serialize the local row.
    """
    return _serialize_local_case_by_id(case_id)
