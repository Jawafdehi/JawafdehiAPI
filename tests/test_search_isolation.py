"""The test suite must never be able to reach a real OpenSearch cluster.

REGRESSION GUARD for the 2026-08-26 incident. ``make_client()`` resolves its
endpoint from the RAW PROCESS ENV (``jawafdehi_shared/search/opensearch.py``),
not from Django settings, and defaults to ``http://localhost:9200``. The
write-time indexer runs from a ``post_save`` signal on
``transaction.on_commit`` (``cases/signals.py``). That callback never fires
under a plain ``@pytest.mark.django_db`` — the transaction is rolled back — but
it DOES fire for ``django_db(transaction=True)`` (every ``live_server`` test)
and for ``django_capture_on_commit_callbacks(execute=True)``. With a ``kubectl
port-forward -n platform svc/opensearch 9200:9200`` open, ``localhost:9200``
was production, and six test fixtures (``lc-a``, ``live-old``, ``live-q-old``,
``live-f-old``, ``live-h-old``, ``c-promo``) were written into the live
``jawafdehi-cases`` index, where they rendered as public case cards on
jawafdehi.org. Their rows died with the test database, so nothing could evict
them but a full rebuild.

The pin lives in the repo-root ``conftest.py`` (covers every pytest run, whatever
``DJANGO_SETTINGS_MODULE`` says) and in ``config/settings_test.py`` (covers
``manage.py test``). These tests fail if either is removed or softened back to a
``setdefault`` — the whole point is that an ``OPENSEARCH_URL`` exported in a
developer's shell must LOSE to it.
"""

from __future__ import annotations

import sys
import types
from urllib.parse import urlparse

import pytest

from cases.models import Case, CaseState, CaseType
from jawafdehi_shared.search.opensearch import get_opensearch_url, make_client

# Kept in sync with the conftest pin by assertion, not by import, so that
# deleting the conftest line fails this module rather than silently redefining
# what "isolated" means.
BLACKHOLE = "http://127.0.0.1:1"


def _fake_opensearchpy(monkeypatch) -> list[dict]:
    """Stub ``opensearchpy`` and return the list of kwargs clients are built with.

    ``make_client`` imports ``opensearchpy`` lazily inside the function body, so
    replacing the module in ``sys.modules`` is enough — no client ever reaches a
    socket, which is also what makes this test safe to run if the pin is broken.
    """
    captured: list[dict] = []

    class FakeOpenSearch:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    fake_mod = types.ModuleType("opensearchpy")
    fake_mod.OpenSearch = FakeOpenSearch
    monkeypatch.setitem(sys.modules, "opensearchpy", fake_mod)
    return captured


def test_opensearch_url_is_pinned_to_a_closed_port():
    """The resolved endpoint is loopback on a port nothing can bind unprivileged."""
    url = get_opensearch_url()
    assert url == BLACKHOLE, (
        f"OPENSEARCH_URL resolved to {url!r} during tests. The repo-root "
        "conftest.py must hard-assign it to the blackhole so a stray export or a "
        "kubectl port-forward on localhost:9200 cannot be reached from the suite."
    )
    parsed = urlparse(url)
    assert parsed.hostname in {"127.0.0.1", "localhost"}
    # Port 1 needs root to bind, so this cannot collide with a dev service the
    # way 9200 does — and it fails as an instant ECONNREFUSED rather than burning
    # the 30s OPENSEARCH_TIMEOUT and its retries on every accidental call.
    assert parsed.port == 1


def test_make_client_targets_the_blackhole(monkeypatch):
    """A no-argument client — the one the write-time signals build — is inert."""
    captured = _fake_opensearchpy(monkeypatch)

    make_client()

    assert captured, "make_client() built no client"
    assert captured[0]["hosts"] == [BLACKHOLE]


def test_an_exported_opensearch_url_does_not_win(monkeypatch):
    """The pin must be an assignment, not a ``setdefault``.

    Simulates the actual failure: an operator with a port-forward open exports
    the endpoint (or simply leaves the module default in play) and then runs the
    suite. Re-importing the conftest applies the pin over the top of it.
    """
    monkeypatch.setenv("OPENSEARCH_URL", "http://localhost:9200")

    import importlib

    import conftest

    importlib.reload(conftest)

    assert get_opensearch_url() == BLACKHOLE


@pytest.mark.django_db(transaction=True)
def test_committed_publish_cannot_index_to_a_real_cluster(monkeypatch):
    """The exact shape that leaked: a real COMMIT, so ``on_commit`` really runs.

    ``transaction=True`` means no enclosing atomic block, so the ``post_save``
    handler's ``transaction.on_commit(lambda: search_index.index(instance))``
    fires for real — as it did for the ``live_server`` slug-history tests. The
    case is PUBLISHED because the indexer's published-gate would otherwise turn
    the upsert into a delete and never build a client at all.

    Asserts the write is attempted (so this test still guards the path if the
    signal wiring changes) and that every endpoint it can reach is the blackhole.
    """
    captured = _fake_opensearchpy(monkeypatch)

    Case.objects.create(
        title="isolation probe",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        slug="search-isolation-probe",
        short_description="probe",
    )

    for kwargs in captured:
        assert kwargs["hosts"] == [BLACKHOLE], (
            f"the write-time indexer built a client for {kwargs['hosts']!r} — "
            "a committed test write can reach a real cluster again"
        )
