"""Resolve the Django user an OIDC token means, keyed on the ``sub`` claim.

There are two login paths into this platform and they used to disagree about
what identifies a person:

* ``jawafdehi_shared.auth.oidc.OIDCAuthentication`` (DRF bearer) did
  ``get_or_create(username=sub)``, producing accounts whose username is a bare
  Zitadel user id. Those rows accumulated every ``CaseStateChange``.
* ``config.oidc_admin.AdminOIDCBackend`` (Django-admin / Wagtail session login)
  matched on the email claim. Those rows carry the human name, the byline and
  the ``AuthorProfile`` slug.

So one person had two accounts, and no code could tell they were the same
person. Changing staff over to ``@jawafdehi.org`` addresses then exposed the
sharper half of the problem: the email claim is *mutable*, so the moment it
changed the session path stopped matching the existing row and created a third,
empty one — the person kept their access and silently lost their history.

``sub`` is the only identifier Zitadel promises is stable across an email or
login-name change, so both paths now resolve through here.

**Resolution order**, first match wins:

1. An existing ``OIDCIdentity`` for this subject. Once a person is bound this is
   the only branch that runs, and it is immune to any later claim change.
2. The email claim, case-insensitively. This adopts the *named* legacy rows —
   deliberately ahead of (3), because that is the row holding the public
   identity (byline, profile slug), and the guidance has always been to credit
   the named account rather than the numeric one.
3. ``username == sub``. This adopts the legacy bearer rows for anyone who only
   ever used the API.
4. Create a new user.

Every branch but the first *binds* the subject on the way out, so a person is
resolved the expensive way at most once and by identity thereafter.

Adopting rather than merging is deliberate: this module makes no attempt to
reconcile rows that are *already* split, because that means re-pointing 23
relations and choosing a winner per person. It stops new splits; healing the
existing ones is a separate, deliberate operation.
"""

from __future__ import annotations

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import AppRegistryNotReady
from django.db import IntegrityError, transaction

User = get_user_model()


def _identity_model():
    """The OIDCIdentity model, or None when ``cases`` is not installed.

    This package is imported by services that do not all ship the ``cases`` app,
    so the binding is best-effort: without the model we fall back to the legacy
    lookups and behave exactly as before rather than failing a login.
    """
    try:
        return apps.get_model("cases", "OIDCIdentity")
    except (LookupError, AppRegistryNotReady):
        return None


def bind_subject(user, subject: str) -> None:
    """Record that ``subject`` means ``user``, if it is not recorded already.

    Tolerates the row already existing (a concurrent login racing us) and
    tolerates the user already being bound to a *different* subject, which would
    mean two Zitadel accounts pointing at one Django user — a real conflict, but
    not one a login should fail on. Leave the first binding in place and let the
    second account keep working unbound rather than hijacking the first's row.
    """
    model = _identity_model()
    if model is None or not subject:
        return
    try:
        with transaction.atomic():
            model.objects.get_or_create(subject=subject, defaults={"user": user})
    except IntegrityError:
        # OneToOne violated: this user is already bound to another subject.
        pass


def resolve_existing_user(claims: dict):
    """Return the user ``claims`` already belongs to, or None. Never creates.

    Binds the subject on an email or username match so the next login takes the
    identity branch and no longer depends on a mutable claim.
    """
    subject = claims.get("sub") or ""
    email = (claims.get("email") or "").strip().lower()
    model = _identity_model()

    if model is not None and subject:
        identity = model.objects.filter(subject=subject).select_related("user").first()
        if identity is not None:
            return identity.user

    # Email before username: the email-matched row is the one carrying the
    # public identity (byline, AuthorProfile slug), and that is the account the
    # person should keep being credited as.
    #
    # Rows already bound to a DIFFERENT subject are skipped. Two Zitadel
    # accounts can end up on one address — the org has a pile of duplicate
    # self-registered users — and adopting a bound row would hand the second
    # account the first one's bylines, then let the role sync overwrite the
    # owner's groups from a token that carries none. An unbound claimant gets
    # its own row instead.
    if email:
        candidates = User.objects.filter(email__iexact=email).order_by("pk")
        for user in candidates:
            if _bound_to_other_subject(user, subject):
                continue
            bind_subject(user, subject)
            return user

    if subject:
        user = User.objects.filter(username=subject).first()
        if user is not None and not _bound_to_other_subject(user, subject):
            bind_subject(user, subject)
            return user

    return None


def _bound_to_other_subject(user, subject: str) -> bool:
    """True when ``user`` already belongs to a different OIDC subject."""
    model = _identity_model()
    if model is None:
        return False
    existing = model.objects.filter(user=user).values_list("subject", flat=True).first()
    return existing is not None and existing != subject


def resolve_user(claims: dict, *, create_defaults: dict | None = None):
    """Return the Django user for ``claims``, creating one only as a last resort.

    ``create_defaults`` seeds a newly created user; the two call sites want
    different usernames. Returns ``(user, created)``.
    """
    user = resolve_existing_user(claims)
    if user is not None:
        return user, False

    subject = claims.get("sub") or ""
    email = (claims.get("email") or "").strip().lower()
    defaults = dict(create_defaults or {})
    username = defaults.pop("username", None) or subject or email
    user = User.objects.create(
        username=username,
        **{"email": email, "is_active": True, **defaults},
    )
    bind_subject(user, subject)
    return user, True
