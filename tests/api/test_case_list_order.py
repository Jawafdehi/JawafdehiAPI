"""The case list's order is total, so paging through it visits every case once.

Bulk-created cases share a ``created_at``. Ordered by that alone, Postgres may
return tied rows in a different order on each page request, so a client walking
the pages sees some cases twice and others never (measured 2026-10-14: 69 of
3,070 cases repeated in one walk). sqlite happens to return ties in rowid order,
which is why this pins the ORDER BY rather than walking pages.
"""

import pytest
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from cases.api_views import CaseViewSet


def _ordering(params=""):
    view = CaseViewSet(action="list", kwargs={}, format_kwarg=None)
    view.request = Request(APIRequestFactory().get(f"/api/cases/{params}"))
    return list(view.get_queryset().query.order_by)


@pytest.mark.django_db
def test_the_list_is_newest_first_with_ties_broken_by_primary_key():
    assert _ordering() == ["-created_at", "-pk"]


@pytest.mark.django_db
def test_the_entity_filtered_list_breaks_ties_by_primary_key():
    order = _ordering("?entity=https://jawafdehi.org/entity/person/ram-bahadur-thapa")
    assert order == ["-_accused_first", "-created_at", "-pk"]
