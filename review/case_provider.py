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

from sourcing import jds_client


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


def _case_by_court_case_number(court_case_number):
    """Find a local Case carrying the given court case ref ("court:number").

    The ref is the exact form stored in ``Case.court_cases`` (e.g.
    ``"special:081-CR-0079"``). Raises CaseNotFound if it is malformed or no
    case references it.
    """
    from django.db import connection

    from cases.models import Case
    from sourcing.ngm_client import parse_court_ref

    parsed = parse_court_ref(court_case_number)
    if not parsed:
        raise CaseNotFound(
            f"Invalid court case number '{court_case_number}'; "
            f"expected '<court>:<case_number>' (e.g. 'special:081-CR-0079')."
        )
    ref = "{}:{}".format(*parsed)

    # JSONField __contains needs PostgreSQL (it raises NotSupportedError on
    # SQLite, used by the test suite), so non-PG backends fall back to an
    # in-memory scan over the (small) set of cases with court refs. Collect up
    # to two matches so an ambiguous ref can be rejected rather than silently
    # resolving to an arbitrary case.
    if connection.vendor == "postgresql":
        matches = list(Case.objects.filter(court_cases__contains=[ref])[:2])
    else:
        matches = []
        for c in Case.objects.exclude(court_cases__isnull=True):
            if isinstance(c.court_cases, list) and ref in c.court_cases:
                matches.append(c)
                if len(matches) == 2:
                    break
    if not matches:
        raise CaseNotFound(f"No case references court case number '{ref}'.")
    if len(matches) > 1:
        raise CaseNotFound(
            f"Multiple cases reference court case number '{ref}'; "
            f"resolution is ambiguous."
        )
    return matches[0]


def resolve_target(slug=None, court_case_number=None):
    """Verify a review target exists and return its ``(slug, title)``.

    Accepts exactly one of a Jawafdehi case ``slug`` or a ``court_case_number``
    (a "court:number" ref as stored in ``Case.court_cases``). Raises
    CaseNotFound when the case cannot be located so the caller can surface a
    clear error instead of queuing a doomed job.
    """
    if court_case_number:
        case = _case_by_court_case_number(court_case_number)
        return case.slug, case.title
    if slug:
        data = get_case(slug)
        return data.get("slug") or slug, data.get("title", "")
    raise CaseNotFound("Provide either a case slug or a court case number.")
