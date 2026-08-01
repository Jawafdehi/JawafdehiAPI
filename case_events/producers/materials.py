# SPDX-License-Identifier: Hippocratic-3.0
"""Signals for newly-archived documents: court orders and CIAA press releases.

One ``post_save`` receiver covers both, because both land the same way — a
Material written through the ingestion plane — and neither scraper should have
to know a bus exists. See :mod:`case_events.producers` for why the seam is here
rather than in ``scrape_court_orders`` / ``scrape_ciaa_press_releases``.

An earlier version of ``DESIGN.md`` §8 described the CIAA producer as blocked on
re-porting a suspended Scrapy spider. That is stale: the spider was replaced by
``manage.py scrape_ciaa_press_releases``, a REST client that has been minting
these Materials for some time. The producer was never the hard part.
"""

from __future__ import annotations

import structlog
from django.db import router, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from case_events import subjects
from case_events.producers import emit

logger = structlog.get_logger(__name__)

#: Material ``source`` -> the signal it raises. Sources not listed here are
#: archived silently, which is the intended default: a Material appearing is not
#: by itself news about a case. Only these two carry a fact worth proposing on.
SOURCE_SUBJECTS = {
    "court_order": subjects.SIGNAL_COURTORDER_PUBLISHED,
    "ciaa_press_release": subjects.SIGNAL_CIAA_PRESSRELEASE,
}


def _material_db(instance) -> str:
    """The alias the row was written on — the one whose commit we must wait for.

    Lifted from ``materials.signals`` because the trap is identical and worth
    repeating rather than importing: ``Material`` lives on ``ngm``, and an
    unqualified ``transaction.on_commit`` resolves against ``default``. Inside
    ``atomic(using="ngm")`` the ``default`` connection is still in autocommit, so
    Django would run the callback IMMEDIATELY — announcing a document before (or
    instead of) its row becoming durable.
    """
    from materials.models import Material

    return instance._state.db or router.db_for_write(Material, instance=instance)


def _part_of_iri(data) -> str:
    """The ``isPartOf`` IRI a court-order Material carries, or "".

    This is the load-bearing field. A court order's ``isPartOf`` is the canonical
    ``/courtcase/<court>/<number>`` IRI, which is exactly what the matcher joins
    on — without it the signal names a document and nothing else, and no case
    will ever be found for it.
    """
    if not isinstance(data, dict):
        return ""
    part_of = data.get("isPartOf")
    if isinstance(part_of, dict):
        return part_of.get("@id") or ""
    if isinstance(part_of, str):
        return part_of
    return ""


def signal_for(instance) -> tuple[str, dict, list[str], str] | None:
    """``(subject, payload, subject_refs, dedup_key)`` for ``instance``, or None.

    Split out from the receiver so the decision is testable without saving a row
    through two databases.
    """
    subject = SOURCE_SUBJECTS.get(instance.source)
    if subject is None:
        return None
    # A material created already soft-deleted is not an archival event. Rare, but
    # it happens on re-ingest of a withdrawn document.
    if getattr(instance, "is_deleted", False):
        return None

    data = instance.data if isinstance(instance.data, dict) else {}
    part_of = _part_of_iri(data)

    payload = {
        "material_iri": instance.iri,
        "material_type": instance.material_type,
        "source": instance.source,
        "ident": instance.ident,
        "title": data.get("name") or data.get("headline") or "",
        # The court case the document belongs to, when it names one. CIAA press
        # releases generally do not — they are matched on their own IRI once a
        # caseworker has linked one to a case, which is why this may be empty
        # without the signal being useless.
        "part_of": part_of,
    }
    # The material first: it is the reference most likely to be new. `part_of` is
    # what actually resolves a case today.
    refs = [ref for ref in (instance.iri, part_of) if ref]
    return subject, payload, list(dict.fromkeys(refs)), f"material:{instance.iri}"


@receiver(post_save, dispatch_uid="case_events_material_signal")
def emit_material_signal(sender, instance, created, **kwargs):
    """Announce a newly-archived court order or CIAA press release.

    Only on CREATE. A Material is re-saved for conversion results, visibility
    recomputation and index churn; announcing those would republish the same fact
    every time a background job touched the row.
    """
    from materials.models import Material

    # Connected without a `sender=` so it can be registered before the models are
    # importable at app-load; filter here instead.
    if sender is not Material or not created:
        return

    signal = signal_for(instance)
    if signal is None:
        return
    subject, payload, refs, dedup_key = signal

    def _run():
        emit(
            subject,
            producer="producer:materials",
            payload=payload,
            subject_refs=refs,
            dedup_key=dedup_key,
            source=instance.iri,
            raw_ref=instance.iri,
        )

    transaction.on_commit(_run, using=_material_db(instance))
