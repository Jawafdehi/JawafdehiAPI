"""The case DETAIL payload carries each party's bilingual name and picture.

Why this exists: a case page used to resolve every party with its own
``GET /api/entities/<iri>``. ``CONN_MAX_AGE = 0`` means one Postgres connection
per request, against a ``max_connections`` of 100 shared with the bus consumers,
the jobs processor and the reindex CronJobs. The widest published case binds 255
parties, so one page view could demand more connections than the database allows
— ``FATAL: sorry, too many clients already`` → ``OperationalError`` → 500. It ran
intermittently for nine days before anyone noticed, because the UI falls back to
``display_name`` and the page still *looks* right (COE 2026-09-29).

Embedding the two fields the SPA was fetching removes the fan-out. The tests here
pin the three things that make that safe:

* the DETAIL payload gains ``name``/``image``;
* the LIST payload and the SEARCH-INDEX card do NOT (they are built from the same
  ``build_entity_binds``, and the list page is already 782 KiB–2.9 MB);
* it stays ONE query no matter how many parties — the whole point is to remove
  connection pressure, not to move it server-side.
"""

import pytest
from django.db import connections
from django.test.utils import CaptureQueriesContext

from cases.models import Case, CaseEntityRelationship, CaseState, CaseType, RelationshipType
from cases.search_index import build_indexed_doc
from cases.serializers import CaseDetailSerializer, CaseSerializer
from cases.services.nes_resolver import resolve_entities
from entities.models import StoredEntity

PERSON = "https://jawafdehi.org/entity/person/ram-prasad-gautam"
NAMELESS = "https://jawafdehi.org/entity/person/no-such-entity-here"


def _entity(iri, *, name, image=None, is_deleted=False, entity_type="Person"):
    """A StoredEntity whose ``data`` is the schema.org JSON-LD actually stored."""
    prefix, slug = iri.split("/entity/", 1)[1].rsplit("/", 1)
    data = {"@id": iri, "@type": entity_type, "name": name}
    if image is not None:
        data["image"] = image
    return StoredEntity.objects.create(
        iri=iri,
        entity_type=entity_type,
        prefix=prefix,
        slug=slug,
        data=data,
        is_deleted=is_deleted,
    )


def _case(*nes_ids):
    case = Case.objects.create(
        title="Procurement fraud",
        offence_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        description="Detailed allegation description",
        short_description="Short",
        key_allegations=["Primary allegation"],
    )
    for nes_id in nes_ids:
        CaseEntityRelationship.objects.create(
            case=case, nes_id=nes_id, relationship_type=RelationshipType.ACCUSED
        )
    return case


def _bind(serializer_cls, case):
    return serializer_cls(case).data["entities"][0]


@pytest.mark.django_db
def test_detail_carries_both_scripts_and_the_picture():
    _entity(
        PERSON,
        name={"en": "Ram Prasad Gautam", "ne": "राम प्रसाद गौतम"},
        image="https://s3.jawafdehi.org/entity/ram.jpg",
    )
    bind = _bind(CaseDetailSerializer, _case(PERSON))

    assert bind["name"] == {"en": "Ram Prasad Gautam", "ne": "राम प्रसाद गौतम"}
    assert bind["image"] == "https://s3.jawafdehi.org/entity/ram.jpg"
    # The existing contract is untouched — the SPA still falls back to these.
    assert bind["display_name"] == "Ram Prasad Gautam"
    assert bind["entity_type"] == "Person"


@pytest.mark.django_db
def test_list_payload_does_not_grow():
    """The guard on ~3,100 binds across a payload that is already up to 2.9 MB.

    ``CaseDetailSerializer`` enables these through a class attribute, so a stray
    edit to the base class is exactly how this would regress unnoticed.
    """
    _entity(PERSON, name={"en": "Ram Prasad Gautam", "ne": "राम प्रसाद गौतम"})
    bind = _bind(CaseSerializer, _case(PERSON))

    assert "name" not in bind
    assert "image" not in bind
    assert bind["display_name"] == "Ram Prasad Gautam"


@pytest.mark.django_db
def test_search_document_is_unchanged():
    """The indexed card shares ``build_entity_binds``; changing it would need a reindex.

    ``build_indexed_doc`` is the resolving wrapper BOTH the live ``index()`` signal
    and the bulk ``reindex_cases`` driver go through, so it is the shape that
    actually lands in OpenSearch.
    """
    _entity(
        PERSON,
        name={"en": "Ram Prasad Gautam", "ne": "राम प्रसाद गौतम"},
        image="https://s3.jawafdehi.org/entity/ram.jpg",
    )
    entities = build_indexed_doc(_case(PERSON))["raw"]["card"]["entities"]

    assert entities, "fixture should bind one party"
    assert all("name" not in e and "image" not in e for e in entities)


@pytest.mark.django_db
def test_soft_deleted_entity_yields_no_identity():
    """A retired entity renders as it does today: fallback name, generic glyph.

    ``resolve_entities`` does not filter ``is_deleted`` (narrowing it would change
    ``display_name`` on the list payload and in the index), but
    ``GET /api/entities/<iri>`` does — so the page has never shown this entity's
    bilingual name or photo. Keep it that way; promoting a merged-away party onto
    the page would be a product change smuggled in as a performance fix.
    """
    _entity(
        PERSON,
        name={"en": "Ram Prasad Gautam", "ne": "राम प्रसाद गौतम"},
        image="https://s3.jawafdehi.org/entity/ram.jpg",
        is_deleted=True,
    )
    bind = _bind(CaseDetailSerializer, _case(PERSON))

    assert bind["name"] == {"en": None, "ne": None}
    assert bind["image"] is None
    # display_name is deliberately NOT gated — that is the pre-existing contract.
    assert bind["display_name"] == "Ram Prasad Gautam"


@pytest.mark.django_db
def test_unresolvable_id_yields_nulls_rather_than_raising():
    """Indexing and the API both run this on best-effort data; it must not throw."""
    bind = _bind(CaseDetailSerializer, _case(NAMELESS))

    assert bind["name"] == {"en": None, "ne": None}
    assert bind["image"] is None
    assert bind["display_name"] is None


@pytest.mark.django_db
def test_bare_string_name_counts_as_english():
    """Mirrors the SPA's ``jsonLdToEntity``: a string ``name`` is en, never ne.

    If the two sides disagree the card prints the same string twice — once as the
    name and once as its "other script" spelling.
    """
    _entity(PERSON, name="Ram Prasad Gautam")
    bind = _bind(CaseDetailSerializer, _case(PERSON))

    assert bind["name"] == {"en": "Ram Prasad Gautam", "ne": None}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "image,expected",
    [
        ("https://s3.jawafdehi.org/a.jpg", "https://s3.jawafdehi.org/a.jpg"),
        ({"url": "https://s3.jawafdehi.org/b.jpg"}, "https://s3.jawafdehi.org/b.jpg"),
        ({"contentUrl": "https://s3.jawafdehi.org/c.jpg"}, "https://s3.jawafdehi.org/c.jpg"),
        ([{"url": "https://s3.jawafdehi.org/d.jpg"}], "https://s3.jawafdehi.org/d.jpg"),
        ([{}, "https://s3.jawafdehi.org/e.jpg"], "https://s3.jawafdehi.org/e.jpg"),
        ({"url": "   "}, None),
        ({}, None),
        ([], None),
    ],
)
def test_image_accepts_every_schema_org_spelling(image, expected):
    """schema.org ``image`` is a URL, an ImageObject, or an array of either."""
    _entity(PERSON, name={"en": "Ram"}, image=image)
    assert _bind(CaseDetailSerializer, _case(PERSON))["image"] == expected


@pytest.mark.django_db
def test_two_hundred_fifty_five_parties_cost_one_query():
    """The reason this fix is safe at all.

    The widest published case binds 255 parties. Resolving them per-party — which
    is what ``/api/entities?ids=`` still does internally — was measured at 17.6s
    against production. ``resolve_entities`` is a single ``iri__in``, and it has to
    stay that way: this fix removes connection pressure, it does not relocate it.
    """
    iris = [f"https://jawafdehi.org/entity/person/party-{i:03d}" for i in range(255)]
    for i, iri in enumerate(iris):
        _entity(iri, name={"en": f"Party {i}", "ne": f"पक्ष {i}"})

    with CaptureQueriesContext(connections["nes"]) as captured:
        resolved = resolve_entities(iris)

    assert len(resolved) == 255
    assert len(captured) == 1, f"expected one query, got {len(captured)}"


@pytest.mark.django_db
def test_detail_endpoint_serves_the_embedded_identity(client):
    """End-to-end through the public read plane, not just the serializer."""
    _entity(
        PERSON,
        name={"en": "Ram Prasad Gautam", "ne": "राम प्रसाद गौतम"},
        image="https://s3.jawafdehi.org/entity/ram.jpg",
    )
    case = _case(PERSON)

    payload = client.get(f"/api/cases/{case.slug}/").json()

    bind = payload["entities"][0]
    assert bind["name"]["ne"] == "राम प्रसाद गौतम"
    assert bind["image"] == "https://s3.jawafdehi.org/entity/ram.jpg"
