"""Tests for the two guards that keep one bad row, or one fat batch, from
taking down a whole reindex.

Both regressions are real and dated:

* 2026-08-15 — material ``news_shilaptra/2082`` carried the Bikram Sambat date
  ``2081-02-29`` in ``datePublished``. ``helpers.bulk`` defaults to
  ``raise_on_error=True``, so that single ``mapper_parsing_exception`` aborted a
  345,000-document run.
* 2026-08-22 onward — ``ngm-materials`` averages ~70 characters of ``body`` but
  contains a block of OCR'd Special Court judgments averaging ~56,000. A
  500-document batch there serialises to ~149 MiB, which at opensearch-py's
  100 MiB default goes out as two ~100 MiB requests — far past the read timeout.

No live OpenSearch: the client is mocked and the bulk helper is patched.
"""

from unittest.mock import MagicMock, patch

import pytest

from jawafdehi_shared.search import indexing


@pytest.mark.parametrize(
    "value",
    [
        "2024-06-11",
        "2024-06-11T10:00:00Z",
        "2024-06-11T10:00:00.123+05:45",
        "2024-06-11 10:00:00",
        "1718000000000",  # epoch_millis
    ],
)
def test_gregorian_date_accepts_what_the_cluster_accepts(value):
    assert indexing.gregorian_date_or_none(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "2081-02-29",  # the 2026-08-15 outage: BS date, no such Gregorian day
        "2082-13-45",
        "२०८१-०२-२९",  # Devanagari digits
        "not a date",
        "",
        "   ",
        None,
    ],
)
def test_gregorian_date_rejects_what_would_abort_the_reindex(value):
    assert indexing.gregorian_date_or_none(value) is None


def test_gregorian_date_is_not_looser_than_the_mapping():
    """A prefix that merely *starts* with a date must not slip through.

    Being looser than ``strict_date_optional_time`` would reintroduce the exact
    failure this function exists to prevent.
    """
    assert indexing.gregorian_date_or_none("2024-06-11-GARBAGE") is None


def test_max_chunk_bytes_defaults_to_10_mib(monkeypatch):
    monkeypatch.delenv("OPENSEARCH_MAX_CHUNK_BYTES", raising=False)
    assert indexing.get_max_chunk_bytes() == 10 * 1024 * 1024


def test_max_chunk_bytes_is_tunable(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_MAX_CHUNK_BYTES", "2097152")
    assert indexing.get_max_chunk_bytes() == 2 * 1024 * 1024


def test_stream_bulk_bounds_the_request_by_bytes(monkeypatch):
    """The point of the fix: batch size is in DOCUMENTS, the request cap is in
    BYTES. A document count is not a bound on request size when document sizes
    span three orders of magnitude."""
    monkeypatch.delenv("OPENSEARCH_MAX_CHUNK_BYTES", raising=False)
    docs = [{"iri": "iri-%d" % i} for i in range(3)]
    # The real helper drains the action generator; the count depends on that.
    with patch(
        "opensearchpy.helpers.bulk",
        side_effect=lambda _client, actions, **kw: list(actions),
    ) as fake_bulk:
        submitted = indexing.stream_bulk(MagicMock(), "ngm-materials", docs)
    assert submitted == 3
    _, kwargs = fake_bulk.call_args
    assert kwargs["max_chunk_bytes"] == 10 * 1024 * 1024


def test_stream_bulk_still_keys_every_doc_by_its_iri():
    """Idempotency of a re-sent bulk depends on the deterministic ``_id``;
    the byte cap must not disturb it."""
    docs = [{"iri": "iri-a"}, {"iri": "iri-b"}]
    with patch("opensearchpy.helpers.bulk") as fake_bulk:
        indexing.stream_bulk(MagicMock(), "ngm-materials", docs)
    actions = list(fake_bulk.call_args[0][1])
    assert [a["_id"] for a in actions] == ["iri-a", "iri-b"]
    assert {a["_index"] for a in actions} == {"ngm-materials"}
