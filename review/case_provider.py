"""Case provider: get a normalized case dict by slug.

The rule-centered scorer/casetype/converter all consume a single "case dict"
shape, namely the one produced by ``cases.serializers.CaseDetailSerializer``:

    {title, state, slug, description, key_allegations, timeline, entities,
     court_cases, missing_details,
     evidence: [{source_id, description, source: {title, source_type, url[]}}]}

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
        raise CaseNotFound(
            f"Case '{slug}' is not in the local database. "
            f"Seed it first with: python manage.py seed_jawafdehi --slug {slug}"
        )
    # context=None is fine: get_url builds relative URLs when there is no request.
    data = CaseDetailSerializer(case, context={}).data
    # Normalize to plain dict and ensure slug present.
    data = dict(data)
    data.setdefault("slug", slug)
    return data


def get_case(slug):
    """Return the normalized case dict for a slug, honoring REVIEW_CASE_SOURCE."""
    source = getattr(settings, "REVIEW_CASE_SOURCE", "local")
    if source == "remote":
        case = jds_client.get_case(slug)
        case.setdefault("slug", slug)
        return case
    return _serialize_local_case(slug)
