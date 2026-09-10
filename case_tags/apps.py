"""App config for the case tag vocabulary."""

from __future__ import annotations

from django.apps import AppConfig


class CaseTagsConfig(AppConfig):
    """The canonical tag vocabulary and its alias table.

    Deliberately separate from ``cases``: the vocabulary is a controlled list that
    happens to be applied to cases today, not a property of a case. Keeping it apart
    is what lets a second consumer (entity keywords, material keywords) reuse it
    later without importing the case model.

    Falls through ``config.db_router._db_for_label`` to the ``default`` database,
    alongside ``cases`` -- which it must, since resolving a case's tags joins the two.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "case_tags"
    verbose_name = "Case tags"
