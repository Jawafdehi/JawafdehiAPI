"""Unit tests for the central job-queue engine (jobs.queue).

Cover the properties a single-purpose poller couldn't have: priority + FIFO
claim ordering, dedup, retry-with-backoff, terminal failure, and the lease
reaper (re-queue vs dead-letter). Registry hooks (build_payload / on_result /
on_failure) are tested with a throwaway kind so the tests don't depend on the
casework stack.
"""

import pytest
from django.utils import timezone

from jobs import queue, registry
from jobs.models import Job


@pytest.fixture
def _clean_registry():
    """Register throwaway kinds for tests; restore the registry afterwards."""
    saved = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


@pytest.mark.django_db
def test_enqueue_and_claim_roundtrip():
    job = queue.enqueue("kx", payload={"a": 1})
    assert job.status == Job.QUEUED
    assert job.attempts == 0

    claimed = queue.claim_next(["kx"])
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == Job.RUNNING
    assert claimed.attempts == 1
    assert claimed.lease_expires_at is not None

    # Nothing left to claim.
    assert queue.claim_next(["kx"]) is None


@pytest.mark.django_db
def test_claim_orders_by_priority_then_fifo():
    a = queue.enqueue("kx", priority=100)
    b = queue.enqueue("kx", priority=10)  # higher priority (lower number)
    c = queue.enqueue("kx", priority=100)

    # b (priority 10) first, then a and c FIFO by id.
    assert queue.claim_next(["kx"]).id == b.id
    assert queue.claim_next(["kx"]).id == a.id
    assert queue.claim_next(["kx"]).id == c.id


@pytest.mark.django_db
def test_claim_filters_by_kind():
    queue.enqueue("ka")
    kb = queue.enqueue("kb")
    got = queue.claim_next(["kb"])
    assert got.id == kb.id
    assert got.kind == "kb"


@pytest.mark.django_db
def test_claim_respects_available_at():
    future = timezone.now() + timezone.timedelta(hours=1)
    queue.enqueue("kx", available_at=future)
    assert queue.claim_next(["kx"]) is None  # not yet available


@pytest.mark.django_db
def test_dedup_key_blocks_double_enqueue_while_active():
    j1 = queue.enqueue("kx", dedup_key="only-one")
    j2 = queue.enqueue("kx", dedup_key="only-one")
    assert j1.id == j2.id  # second enqueue is a no-op
    assert Job.objects.filter(dedup_key="only-one").count() == 1


@pytest.mark.django_db
def test_dedup_key_frees_after_terminal():
    j1 = queue.enqueue("kx", dedup_key="k")
    claimed = queue.claim_next(["kx"])
    queue.finalize(claimed, status=Job.DONE, result={"ok": True})

    # Prior job is terminal -> the key frees and a fresh job can take it.
    j2 = queue.enqueue("kx", dedup_key="k")
    assert j2.id != j1.id
    assert j2.status == Job.QUEUED
    # The old job kept its identity but released the unique key.
    j1.refresh_from_db()
    assert j1.dedup_key is None


@pytest.mark.django_db
def test_finalize_rejects_non_running_job():
    """Stale-guard: finalizing a job that isn't RUNNING raises JobNotRunning.

    Simulates a zombie worker whose lease was reaped and the job re-queued: its
    late result submission must not clobber the newer state.
    """
    queue.enqueue("kx")
    job = queue.claim_next(["kx"])
    queue.finalize(job, status=Job.DONE, result={"first": True})  # now DONE

    # A second finalize (stale) on the same job is rejected.
    with pytest.raises(queue.JobNotRunning):
        queue.finalize(job, status=Job.DONE, result={"second": True})
    job.refresh_from_db()
    assert job.result == {"first": True}  # unchanged


@pytest.mark.django_db
def test_claim_without_reap_skips_sweep():
    """claim_next(reap=False) still claims but does not reclaim lapsed leases."""
    queue.enqueue("kx", max_attempts=3)
    running = queue.claim_next(["kx"])
    Job.objects.filter(pk=running.pk).update(
        lease_expires_at=timezone.now() - timezone.timedelta(minutes=1)
    )
    # A fresh queued job to claim, with reap disabled.
    queue.enqueue("kx")
    claimed = queue.claim_next(["kx"], reap=False)
    assert claimed is not None
    # The lapsed-lease job was NOT reaped (still RUNNING) because reap=False.
    running.refresh_from_db()
    assert running.status == Job.RUNNING


@pytest.mark.django_db
def test_finalize_done_stores_result_and_clears_lease():
    queue.enqueue("kx")
    job = queue.claim_next(["kx"])
    out = queue.finalize(job, status=Job.DONE, result={"score": 42})
    assert out.status == Job.DONE
    assert out.result == {"score": 42}
    assert out.lease_expires_at is None
    assert out.completed_at is not None


@pytest.mark.django_db
def test_retryable_failure_requeues_with_backoff():
    queue.enqueue("kx", max_attempts=3)
    job = queue.claim_next(["kx"])
    assert job.attempts == 1
    out = queue.finalize(job, status=Job.FAILED, error="boom", retryable=True)
    # Re-queued, not terminal; available_at pushed into the future (backoff).
    assert out.status == Job.QUEUED
    assert out.available_at > timezone.now()
    assert out.can_retry


@pytest.mark.django_db
def test_retryable_failure_dead_letters_when_exhausted():
    queue.enqueue("kx", max_attempts=1)
    job = queue.claim_next(["kx"])  # attempts -> 1 == max
    out = queue.finalize(job, status=Job.FAILED, error="boom", retryable=True)
    assert out.status == Job.DEAD


@pytest.mark.django_db
def test_nonretryable_failure_is_terminal_failed():
    queue.enqueue("kx", max_attempts=5)
    job = queue.claim_next(["kx"])
    out = queue.finalize(job, status=Job.FAILED, error="nope", retryable=False)
    assert out.status == Job.FAILED  # not DEAD, not requeued


@pytest.mark.django_db
def test_reaper_requeues_expired_lease():
    queue.enqueue("kx", max_attempts=3)
    job = queue.claim_next(["kx"])
    # Force the lease into the past (simulate a crashed worker).
    Job.objects.filter(pk=job.pk).update(
        lease_expires_at=timezone.now() - timezone.timedelta(minutes=1)
    )
    n = queue.reap_expired()
    assert n == 1
    job.refresh_from_db()
    assert job.status == Job.QUEUED
    assert job.lease_expires_at is None


@pytest.mark.django_db
def test_reaper_dead_letters_when_exhausted():
    queue.enqueue("kx", max_attempts=1)
    job = queue.claim_next(["kx"])  # attempts == max_attempts
    Job.objects.filter(pk=job.pk).update(
        lease_expires_at=timezone.now() - timezone.timedelta(minutes=1)
    )
    queue.reap_expired()
    job.refresh_from_db()
    assert job.status == Job.DEAD


@pytest.mark.django_db
def test_touch_extends_lease_only_while_running():
    queue.enqueue("kx")
    job = queue.claim_next(["kx"])
    before = job.lease_expires_at
    Job.objects.filter(pk=job.pk).update(
        lease_expires_at=timezone.now() + timezone.timedelta(seconds=1)
    )
    job.refresh_from_db()
    queue.touch(job, stage="working")
    job.refresh_from_db()
    assert job.lease_expires_at > before
    assert job.stage == "working"


@pytest.mark.django_db
def test_build_payload_hook_enriches_on_claim(_clean_registry):
    registry.register(
        registry.KindSpec(
            kind="kbuild",
            build_payload=lambda job: {"resolved": job.payload["seed"] * 2},
        )
    )
    queue.enqueue("kbuild", payload={"seed": 21})
    job = queue.claim_next(["kbuild"])
    assert job.payload["resolved"] == 42


@pytest.mark.django_db
def test_build_payload_failure_fails_the_job(_clean_registry):
    def _boom(job):
        raise ValueError("cannot resolve")

    registry.register(registry.KindSpec(kind="kbad", build_payload=_boom))
    queue.enqueue("kbad", max_attempts=1)
    # claim_next returns None (job failed during payload build) ...
    assert queue.claim_next(["kbad"]) is None
    job = Job.objects.get(kind="kbad")
    assert job.status == Job.FAILED
    assert "cannot resolve" in job.error


@pytest.mark.django_db
def test_on_result_and_on_failure_hooks_fire(_clean_registry):
    calls = {}
    registry.register(
        registry.KindSpec(
            kind="khook",
            on_result=lambda job, result: calls.__setitem__("result", result),
            on_failure=lambda job: calls.__setitem__("failure", job.error),
        )
    )
    queue.enqueue("khook")
    job = queue.claim_next(["khook"])
    queue.finalize(job, status=Job.DONE, result={"v": 1})
    assert calls["result"] == {"v": 1}

    queue.enqueue("khook", max_attempts=1)
    job2 = queue.claim_next(["khook"])
    queue.finalize(job2, status=Job.FAILED, error="dead", retryable=False)
    assert calls["failure"] == "dead"
