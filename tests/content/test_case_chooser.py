"""Tests for the case chooser's search filter (`content.chooser`).

The chooser modal backs the article ``related_cases`` inline. ``cases.Case`` is
not registered with Wagtail search, so search is provided by a custom
``icontains`` filter form over ``title`` and ``slug`` — these guard that
behaviour (title-only, slug-only, empty input, and no-match).
"""

import pytest

from cases.models import Case, CaseState, CaseType
from content.chooser import CaseSearchFilterForm


@pytest.fixture
def cases(db):
    ncell = Case.objects.create(
        title="Ncell tax dispute",
        slug="ncell-tax-case",
        state=CaseState.PUBLISHED,
        case_type=CaseType.CORRUPTION,
    )
    omni = Case.objects.create(
        title="Omni scandal",
        slug="omni-procurement",
        state=CaseState.PUBLISHED,
        case_type=CaseType.CORRUPTION,
    )
    return ncell, omni


def _filter(query):
    form = CaseSearchFilterForm({"q": query} if query is not None else {})
    assert form.is_valid(), form.errors
    return form, form.filter(Case.objects.all())


@pytest.mark.django_db
def test_search_matches_on_title(cases):
    ncell, _ = cases
    form, results = _filter("scandal")
    assert list(results) == [cases[1]]
    assert form.is_searching is True
    assert form.search_query == "scandal"


@pytest.mark.django_db
def test_search_matches_on_slug(cases):
    ncell, _ = cases
    _, results = _filter("ncell-tax")
    assert list(results) == [ncell]


@pytest.mark.django_db
def test_empty_query_returns_all_unfiltered(cases):
    form, results = _filter("")
    assert set(results) == set(cases)
    assert form.is_searching is False
    assert form.search_query is None


@pytest.mark.django_db
def test_no_match_returns_empty(cases):
    _, results = _filter("nonexistent-xyz")
    assert list(results) == []
