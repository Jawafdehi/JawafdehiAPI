"""Bulk-indexing behaviour, driven against the REAL opensearch-py helpers.

These tests deliberately do NOT patch ``streaming_bulk``. A fake that accepts
``**kwargs`` proves nothing: every keyword this module passes could be renamed
or dropped and the suite would stay green while the helper silently fell back to
its own defaults (100MB chunks, 500 docs, no retry). So the fake here is a
*client* — it implements only what opensearch-py actually touches
(``transport.serializer`` and ``bulk()``) and the real helper runs on top of it.

That is what lets these assert behaviour rather than configuration: real
chunking, real 429 retry, real error-item shapes.
"""

from __future__ import annotations

import json

import pytest
from opensearchpy.exceptions import TransportError
from opensearchpy.serializer import JSONSerializer

from jawafdehi_shared.search import indexing, opensearch, reindex

INDEX = "ngm-materials"

#: Distinctive content planted in every document body. If this ever shows up in
#: a raised error, a failed document was retained.
BODY_MARKER = "XX-WHOLE-DOCUMENT-BODY-XX"


# ── the fake cluster ─────────────────────────────────────────────────────────


class _Transport:
    def __init__(self):
        self.serializer = JSONSerializer()


class FakeBulkClient:
    """Implements exactly the surface opensearch-py's bulk helpers use."""

    def __init__(self, responder=None):
        self.transport = _Transport()
        self.requests: list[str] = []  # raw NDJSON bodies, in order
        self.responder = responder or (lambda ops, attempt: 200)

    def bulk(self, body, **kwargs):
        attempt = len(self.requests)
        self.requests.append(body)
        items = []
        for op_type, doc_id in _parse_ops(body):
            status = self.responder([(op_type, doc_id)], attempt)
            item = {"_index": INDEX, "_id": doc_id, "status": status}
            if not 200 <= status < 300:
                item["error"] = {
                    "type": "illegal_argument_exception",
                    "reason": "synthetic failure",
                }
            items.append({op_type: item})
        return {"items": items, "errors": any(i for i in items)}

    @property
    def bodies(self) -> list[int]:
        return [len(b.encode()) for b in self.requests]

    def ids_in(self, i: int) -> list[str]:
        return [doc_id for _op, doc_id in _parse_ops(self.requests[i])]


def _parse_ops(body: str) -> list[tuple[str, str]]:
    """Pull (op_type, _id) out of an NDJSON bulk body.

    Index ops carry a source line after the action line; deletes do not — which
    is exactly the asymmetry a hand-rolled parser gets wrong, so it is spelled
    out rather than assumed.
    """
    lines = [ln for ln in body.split("\n") if ln]
    ops: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        ((op_type, params),) = json.loads(lines[i]).items()
        i += 1
        if op_type != "delete":
            i += 1  # skip the source line
        ops.append((op_type, params["_id"]))
    return ops


def _docs(n: int, size: int = 0) -> list[dict]:
    return [
        {
            "iri": f"https://jawafdehi.org/material/m{i}",
            "raw": {"text": BODY_MARKER * max(1, size)},
        }
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Retry backoff is real ``time.sleep``; keep the suite fast but record it."""
    slept: list[float] = []
    monkeypatch.setattr(
        "opensearchpy.helpers.actions.time.sleep", lambda s: slept.append(s)
    )
    return slept


# ── the real helper actually accepts what we pass it ─────────────────────────


def test_real_streaming_bulk_accepts_our_configuration():
    """The test a mocked helper cannot give you.

    If a keyword is renamed or removed upstream this fails here, instead of the
    helper quietly reverting to 100MB/500/no-retry with every assertion green.
    """
    client = FakeBulkClient()
    assert indexing.stream_bulk(client, INDEX, _docs(3)) == 3
    assert len(client.requests) == 1


def test_chunks_are_bounded_by_bytes_not_just_count(monkeypatch):
    """The bound that matters, measured on real serialized request bodies."""
    monkeypatch.setenv("OPENSEARCH_BULK_MAX_CHUNK_BYTES", str(64 * 1024))
    client = FakeBulkClient()
    # 40 docs x ~25KB each: far under the 500-doc count bound, far over 64KiB.
    indexing.stream_bulk(client, INDEX, _docs(40, size=1000))
    assert len(client.requests) > 1, "byte bound did not split the request"
    # opensearch-py closes a chunk once adding the next doc would exceed the
    # bound, so a single oversized doc can overshoot; everything else must not.
    assert max(client.bodies[:-1]) < 64 * 1024 * 2


def test_chunk_count_matches_the_library_default():
    """500, not 200: the byte bound provides the memory guarantee, and a lower
    count only multiplies round-trips against an already-struggling cluster."""
    client = FakeBulkClient()
    indexing.stream_bulk(client, INDEX, _docs(1200))
    assert [len(client.ids_in(i)) for i in range(len(client.requests))] == [
        500,
        500,
        200,
    ]


# ── 429 backpressure is retried, on BOTH paths ───────────────────────────────


def test_429_is_retried_and_then_succeeds(_no_sleeping):
    client = FakeBulkClient(responder=lambda ops, attempt: 429 if attempt < 2 else 200)
    assert indexing.stream_bulk(client, INDEX, _docs(5)) == 5
    assert len(client.requests) == 3, "expected two retries then success"
    assert _no_sleeping == [2, 4], "expected exponential backoff"


def test_429_exhausted_raises_a_bounded_error():
    client = FakeBulkClient(responder=lambda ops, attempt: 429)
    with pytest.raises(RuntimeError) as excinfo:
        indexing.stream_bulk(client, INDEX, _docs(50))
    msg = str(excinfo.value)
    assert "50/50" in msg
    assert msg.count("_id=") == indexing._FAILURE_SAMPLE


def test_delete_retries_429_rather_than_aborting_after_the_swap(_no_sleeping):
    """The regression that matters most: reindex runs the tombstone pass AFTER
    the alias swap, so aborting it leaves a hidden row searchable."""
    client = FakeBulkClient(responder=lambda ops, attempt: 429 if attempt < 1 else 200)
    iris = [d["iri"] for d in _docs(4)]
    assert indexing.stream_bulk_delete(client, INDEX, iris) == 4
    assert len(client.requests) == 2, "a 429 tombstone must be retried, not fatal"


# ── failures never carry the document ────────────────────────────────────────


def test_index_error_never_contains_a_document_body():
    """Verified against the real error items, not a fabricated payload shape."""
    client = FakeBulkClient(responder=lambda ops, attempt: 400)
    with pytest.raises(RuntimeError) as excinfo:
        indexing.stream_bulk(client, INDEX, _docs(200, size=50))
    msg = str(excinfo.value)
    assert BODY_MARKER not in msg
    assert msg.count("_id=") == indexing._FAILURE_SAMPLE
    assert "200/200" in msg


def test_failure_reason_is_truncated():
    long_reason = "R" * 10_000

    def responder(ops, attempt):
        return 400

    client = FakeBulkClient(responder=responder)
    # Patch the reason in-place to something pathological.
    original = client.bulk

    def bulk(body, **kwargs):
        resp = original(body, **kwargs)
        for item in resp["items"]:
            for payload in item.values():
                if "error" in payload:
                    payload["error"]["reason"] = long_reason
        return resp

    client.bulk = bulk
    with pytest.raises(RuntimeError) as excinfo:
        indexing.stream_bulk(client, INDEX, _docs(1))
    assert len(str(excinfo.value)) < 1_000


def test_transport_error_propagates_rather_than_counting_as_failures():
    """A broken cluster is not N bad documents; it must fail loudly."""

    class Broken(FakeBulkClient):
        def bulk(self, body, **kwargs):
            raise TransportError(503, "connection refused")

    with pytest.raises(TransportError):
        indexing.stream_bulk(Broken(), INDEX, _docs(3))


# ── delete-path status policy ────────────────────────────────────────────────


def test_delete_treats_404_as_success():
    """Tombstoning a doc that was never indexed is the COMMON case."""
    client = FakeBulkClient(responder=lambda ops, attempt: 404)
    iris = [d["iri"] for d in _docs(10)]
    assert indexing.stream_bulk_delete(client, INDEX, iris) == 10


def test_delete_raises_on_a_real_failure():
    client = FakeBulkClient(responder=lambda ops, attempt: 403)
    iris = [d["iri"] for d in _docs(3)]
    with pytest.raises(RuntimeError) as excinfo:
        indexing.stream_bulk_delete(client, INDEX, iris)
    assert "3/3" in str(excinfo.value)


def test_delete_of_nothing_makes_no_request():
    client = FakeBulkClient()
    assert indexing.stream_bulk_delete(client, INDEX, []) == 0
    assert client.requests == []


# ── the caller streams instead of buffering ──────────────────────────────────


def test_stream_builds_docs_lazily_not_a_batch_up_front(monkeypatch):
    """``_stream`` must not materialise a batch before the helper runs.

    The old code accumulated batch_size fully-built docs — for ngm-materials, 500
    whole JSON-LD bodies — before a single byte was sent. Interleaving is the
    observable consequence of streaming, so assert on the ORDER of build_doc
    calls against bulk requests rather than on memory.
    """
    monkeypatch.setenv("OPENSEARCH_BULK_CHUNK_SIZE", "2")
    events: list[str] = []
    client = FakeBulkClient()
    original_bulk = client.bulk

    def tracking_bulk(body, **kwargs):
        events.append("REQUEST")
        return original_bulk(body, **kwargs)

    client.bulk = tracking_bulk

    def build_doc(record):
        events.append(f"build:{record}")
        return {"iri": f"https://jawafdehi.org/material/m{record}"}

    indexed, skipped = reindex._stream(client, INDEX, range(6), build_doc)
    assert (indexed, skipped) == (6, 0)

    # Builds interleave with requests instead of all preceding them. The exact
    # split is the library's business — _chunk_actions pulls ONE action past the
    # chunk boundary before closing a chunk, so a chunk_size of 2 builds 3 docs
    # before the first request — so assert the property, not that layout.
    assert events.count("REQUEST") == 3
    first_request = events.index("REQUEST")
    assert first_request <= 3, f"buffered too much before sending: {events}"
    assert "build:5" in events[first_request:], "all docs were built up front"


def test_stream_counts_skipped_docs():
    """Docs whose build_doc yields no iri are skipped, not indexed."""
    client = FakeBulkClient()

    def build_doc(record):
        return {} if record % 2 else {"iri": f"https://jawafdehi.org/material/m{record}"}

    indexed, skipped = reindex._stream(client, INDEX, range(10), build_doc)
    assert (indexed, skipped) == (5, 5)


# ── env plumbing ─────────────────────────────────────────────────────────────


def test_negative_retries_are_clamped_not_obeyed(monkeypatch):
    """range(max_retries + 1) becomes range(0) at -1: every chunk is skipped and
    the reindex reports success having sent nothing. Clamp instead."""
    monkeypatch.setenv("OPENSEARCH_BULK_MAX_RETRIES", "-1")
    assert opensearch.get_bulk_max_retries() == 0
    client = FakeBulkClient()
    assert indexing.stream_bulk(client, INDEX, _docs(3)) == 3
    assert len(client.requests) == 1, "chunks must still be sent"


def test_garbage_env_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_BULK_CHUNK_SIZE", "oops")
    assert opensearch.get_bulk_chunk_size() == 500


def test_zero_chunk_size_is_clamped(monkeypatch):
    """chunk_size=0 would make _chunk_actions emit empty chunks forever."""
    monkeypatch.setenv("OPENSEARCH_BULK_CHUNK_SIZE", "0")
    assert opensearch.get_bulk_chunk_size() == 1


def test_tunables_are_env_overridable(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_BULK_CHUNK_SIZE", "50")
    monkeypatch.setenv("OPENSEARCH_BULK_MAX_CHUNK_BYTES", "1048576")
    monkeypatch.setenv("OPENSEARCH_BULK_MAX_RETRIES", "7")
    monkeypatch.setenv("OPENSEARCH_BULK_INITIAL_BACKOFF", "1")
    monkeypatch.setenv("OPENSEARCH_BULK_MAX_BACKOFF", "30")
    assert opensearch.get_bulk_chunk_size() == 50
    assert opensearch.get_bulk_max_chunk_bytes() == 1024 * 1024
    assert opensearch.get_bulk_max_retries() == 7
    assert opensearch.get_bulk_initial_backoff() == 1
    assert opensearch.get_bulk_max_backoff() == 30
