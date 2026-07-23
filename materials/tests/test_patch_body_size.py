"""A PATCH body must be refused BEFORE it is parsed into memory.

CodeRabbit flagged that MAX_PATCH_OPS bounds the op *count* and
MAX_MATERIAL_DOC_BYTES bounds the *result*, so a single op carrying a huge
``value`` is fully parsed, patched and re-serialized before anything rejects it.

That gap is real, but it opens EARLIER than the suggested fix can reach: DRF's
``JSONParser`` has already read and parsed the whole stream by the time
``request.data`` returns a value to measure. Measuring ``raw_ops`` after the
fact cannot un-spend that memory — it allocates a second full copy to do it.

The guard therefore has to sit on Content-Length, before the body is touched.
"""

from __future__ import annotations

import json
from unittest.mock import patch as mock_patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.parsers import JSONParser
from rest_framework.test import APIClient

from materials.models import Material, Visibility
from materials.patch_validation import MAX_MATERIAL_DOC_BYTES

User = get_user_model()
pytestmark = pytest.mark.django_db(databases=["default", "ngm"])
IRI = "https://jawafdehi.org/material/ag/bodysize"


def _store():
    m = Material(
        iri=IRI, material_type="charge_sheet", source="ag", ident="bodysize",
        data={"@id": IRI, "@type": "DigitalDocument", "name": {"ne": "अभियोगपत्र"}},
        visibility=Visibility.LISTED,
    )
    m.save()
    return m


def _client():
    u = User.objects.create_user("cw-size", password="x")
    u.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
    c = APIClient()
    c.force_authenticate(u)
    return c


def _oversized_body():
    """One op, one value, comfortably past the ceiling."""
    filler = "क" * (MAX_MATERIAL_DOC_BYTES // 2)  # 3 bytes/char in UTF-8
    return {"patch_ops": [{"op": "add", "path": "/text", "value": filler}]}


def test_an_oversized_body_is_rejected_without_being_parsed():
    _store()
    body = json.dumps(_oversized_body())
    real_parse = JSONParser.parse
    parsed = []

    def spy(self, stream, media_type=None, parser_context=None):
        parsed.append(True)
        return real_parse(self, stream, media_type, parser_context)

    with mock_patch.object(JSONParser, "parse", spy):
        resp = _client().patch(
            f"/api/materials/?iri={IRI}",
            body,
            content_type="application/json",
        )

    assert resp.status_code == 413, resp.status_code
    assert not parsed, (
        "the body was parsed before it was rejected — the size guard is running "
        "downstream of the allocation it is supposed to prevent"
    )


@pytest.mark.parametrize("header", [None, "", "not-a-number"])
def test_an_absent_or_junk_content_length_is_not_itself_a_rejection(header):
    """A chunked request has no Content-Length; that must not be a rejection.

    Refusing it would break a valid client to defend against an attacker who
    can simply omit the header. Such a body still meets the op-count and
    post-apply document ceilings — later than we would like, but it is caught.

    Exercised against the guard directly: Django's test client synthesizes a
    Content-Length from the payload, so it cannot express this request.
    """
    from django.test import RequestFactory

    from materials.views import _reject_oversized_body

    request = RequestFactory().patch("/api/materials/", data=b"{}", content_type="application/json")
    if header is None:
        request.META.pop("CONTENT_LENGTH", None)
    else:
        request.META["CONTENT_LENGTH"] = header

    assert _reject_oversized_body(request) is None


def test_the_guard_rejects_on_a_declared_length_alone():
    """It must trust Content-Length, not wait to count bytes it has read."""
    from django.test import RequestFactory

    from materials.views import _reject_oversized_body
    from materials.patch_validation import MAX_PATCH_BODY_BYTES

    request = RequestFactory().patch("/api/materials/", data=b"{}", content_type="application/json")
    request.META["CONTENT_LENGTH"] = str(MAX_PATCH_BODY_BYTES + 1)
    resp = _reject_oversized_body(request)
    assert resp is not None and resp.status_code == 413

    request.META["CONTENT_LENGTH"] = str(MAX_PATCH_BODY_BYTES)
    assert _reject_oversized_body(request) is None, "the boundary itself must be allowed"


def test_a_normal_patch_still_goes_through():
    _store()
    resp = _client().patch(
        f"/api/materials/?iri={IRI}",
        {"patch_ops": [{"op": "add", "path": "/jawafdehi:caseNumber", "value": "081-CR-0094"}]},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    assert Material.objects.using("ngm").get(pk=IRI).data["jawafdehi:caseNumber"] == "081-CR-0094"
