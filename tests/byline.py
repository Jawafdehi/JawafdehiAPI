"""Shared helper for the publish-gate byline requirements.

``Case.validate()`` requires at least one credited author and a
``case_publish_date`` before a case may reach IN_REVIEW or PUBLISHED. Every test
that transitions a case out of DRAFT therefore needs a byline, and building one
by hand in a dozen helpers would drift. This is that one place.
"""

from datetime import date

from django.contrib.auth import get_user_model

from cases.models import AuthorProfile, Case, CaseAuthor

#: Arbitrary but fixed — tests assert on transitions, never on this value.
DEFAULT_PUBLISH_DATE = date(2026, 8, 1)


def credit_author(
    case,
    username="byline-author",
    publish_date=DEFAULT_PUBLISH_DATE,
    title=None,
):
    """Give ``case`` the author + publish date the publish gate requires.

    ``get_or_create`` on both rows so a test that credits several cases (or calls
    this twice on one) doesn't trip the username unique constraint or the
    ``unique_case_author`` constraint.

    The publish date is written with ``update()`` rather than ``save()`` to keep
    the helper inert: ``Case.save()`` carries slug generation, slug-immutability
    enforcement and slug-history recording, none of which a test asking for a
    byline is asking for.
    """
    user_model = get_user_model()
    user, _ = user_model.objects.get_or_create(
        username=username,
        defaults={"first_name": "Byline", "last_name": "Author"},
    )
    CaseAuthor.objects.get_or_create(case=case, user=user, defaults={"ordinal": 0})
    if title is not None:
        # Per-person, not per-case: it lives on the profile CaseAuthor.save()
        # just created.
        profile = AuthorProfile.objects.get(user=user)
        profile.title = title
        profile.save(update_fields=["title"])
    if publish_date is not None:
        Case.objects.filter(pk=case.pk).update(case_publish_date=publish_date)
        # Keep the caller's in-memory instance consistent: most callers transition
        # the object they were handed rather than re-reading it.
        case.case_publish_date = publish_date
    return case
