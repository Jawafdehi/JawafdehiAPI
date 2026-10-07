"""Both login paths must resolve one person to one Django user.

The platform had two OIDC entry points keyed on different claims — the DRF
bearer authenticator on ``sub``, the Django-admin session backend on email — so
a caseworker ended up with two accounts: a named one holding the byline and
``AuthorProfile`` slug, a numeric one holding every ``CaseStateChange``.

Moving staff to ``@jawafdehi.org`` addresses turned that latent split into an
active one. The email claim is mutable, so the first login after an address
change matched nothing and minted a third, empty account. On 2026-10-07 three
people picked up a fresh row that way within a single day, keeping their access
and silently losing their history.

These tests pin the contract that fixes it: ``sub`` decides who you are, an
address change does not create anybody, and the two paths land on the same row.
"""

import pytest
from django.contrib.auth import get_user_model

from cases.models import OIDCIdentity
from config.oidc_admin import AdminOIDCBackend
from jawafdehi_shared.auth import subject as subject_mod
from jawafdehi_shared.auth.subject import resolve_user

User = get_user_model()

SUBJECT = "377592055028777324"


def _claims(sub=SUBJECT, email="person@example.com", **extra):
    return {"sub": sub, "email": email, **extra}


@pytest.mark.django_db
def test_changed_email_resolves_to_the_same_user():
    """The regression: a new address must not mint a second account."""
    user, created = resolve_user(_claims(email="person@gmail.com"))
    assert created

    again, created_again = resolve_user(_claims(email="person@jawafdehi.org"))

    assert not created_again
    assert again.pk == user.pk
    assert User.objects.count() == 1


@pytest.mark.django_db
def test_existing_email_matched_user_is_adopted_and_bound():
    """A pre-existing row is adopted on first sight, then keyed on subject."""
    legacy = User.objects.create(username="person", email="person@gmail.com")
    assert not OIDCIdentity.objects.filter(user=legacy).exists()

    resolved, created = resolve_user(_claims(email="person@gmail.com"))

    assert not created
    assert resolved.pk == legacy.pk
    assert OIDCIdentity.objects.get(subject=SUBJECT).user_id == legacy.pk


@pytest.mark.django_db
def test_named_row_wins_over_numeric_row():
    """When both legacy rows exist, the person keeps their public identity.

    The numeric row carries the audit history but no name or slug, so adopting
    it would produce a numeric public URL for a byline. Email is matched first
    for exactly this reason.
    """
    numeric = User.objects.create(username=SUBJECT, email="")
    named = User.objects.create(username="person", email="person@gmail.com")

    resolved, _ = resolve_user(_claims(email="person@gmail.com"))

    assert resolved.pk == named.pk
    assert resolved.pk != numeric.pk


@pytest.mark.django_db
def test_legacy_numeric_row_adopted_when_no_email_match():
    """API-only users, who never had an email-keyed row, are still found."""
    numeric = User.objects.create(username=SUBJECT, email="")

    resolved, created = resolve_user(_claims(email="never-seen@example.com"))

    assert not created
    assert resolved.pk == numeric.pk


@pytest.mark.django_db
def test_both_login_paths_land_on_one_user():
    """The bearer path and the admin path must agree about who this is."""
    bearer_user, _ = resolve_user(_claims(), create_defaults={"username": SUBJECT})

    backend = AdminOIDCBackend()
    matched = backend.filter_users_by_claims(_claims())

    assert [u.pk for u in matched] == [bearer_user.pk]


@pytest.mark.django_db
def test_admin_create_survives_a_taken_username():
    """A shared address must not 500 the admin login on a username collision.

    The historical username is the address, so when the resolver correctly
    refuses to let a second subject adopt the first's row, creating the second
    account would otherwise violate the unique username.
    """
    backend = AdminOIDCBackend()
    first = backend.create_user(_claims(sub="aaa", email="shared@example.com"))

    second = backend.create_user(_claims(sub="bbb", email="shared@example.com"))

    assert second.pk != first.pk
    assert second.username == "bbb"
    assert second.email == "shared@example.com"


@pytest.mark.django_db
def test_different_subjects_stay_different_people():
    """Binding must not collapse two Zitadel accounts onto one row."""
    first, _ = resolve_user(_claims(sub="aaa", email="a@example.com"))
    second, created = resolve_user(_claims(sub="bbb", email="b@example.com"))

    assert created
    assert first.pk != second.pk
    assert OIDCIdentity.objects.count() == 2


@pytest.mark.django_db
def test_binding_is_idempotent_across_repeated_logins():
    resolve_user(_claims())
    resolve_user(_claims())
    resolve_user(_claims(email="moved@jawafdehi.org"))

    assert OIDCIdentity.objects.filter(subject=SUBJECT).count() == 1
    assert User.objects.count() == 1


@pytest.mark.django_db
def test_shared_email_does_not_hijack_an_already_bound_user():
    """A second subject sharing an address must not take over the first's row.

    Two Zitadel accounts on one address is a real possibility — the org carries
    a pile of duplicate self-registered users. Adopting an already-bound row
    would hand the second account the first one's bylines, and then let the role
    sync overwrite the owner's groups from a token carrying none. The claimant
    gets its own row instead.
    """
    owner, _ = resolve_user(_claims(sub="aaa", email="shared@example.com"))
    claimant, created = resolve_user(_claims(sub="bbb", email="shared@example.com"))

    assert created
    assert claimant.pk != owner.pk
    assert OIDCIdentity.objects.get(subject="aaa").user_id == owner.pk
    assert OIDCIdentity.objects.get(subject="bbb").user_id == claimant.pk


@pytest.mark.django_db(transaction=True)
def test_login_survives_the_window_before_the_table_is_migrated():
    """Deploys do not auto-migrate, so the table can be absent under live code.

    The new image rolls before `migrate` runs. If the resolver assumed its table
    existed, every OIDC login in that window would raise ProgrammingError — on
    the bearer API and the admin session path alike. It must fall back to the
    legacy email/username lookups instead, exactly as it behaved before this
    table was introduced.
    """
    from django.db import connection

    legacy = User.objects.create(username="person", email="person@gmail.com")

    with connection.schema_editor() as editor:
        editor.delete_model(OIDCIdentity)
    subject_mod.reset_table_cache()
    try:
        resolved, created = resolve_user(_claims(email="person@gmail.com"))
        assert not created
        assert resolved.pk == legacy.pk

        fresh, created = resolve_user(_claims(sub="zzz", email="new@example.com"))
        assert created
        assert fresh.email == "new@example.com"
    finally:
        with connection.schema_editor() as editor:
            editor.create_model(OIDCIdentity)
        subject_mod.reset_table_cache()


@pytest.mark.django_db
def test_binding_resumes_once_the_table_exists():
    """The absent-table result must not be cached, or it would need a restart."""
    subject_mod.reset_table_cache()

    user, _ = resolve_user(_claims())

    assert OIDCIdentity.objects.filter(subject=SUBJECT, user=user).exists()
