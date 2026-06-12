"""
Pytest configuration for test suite.

Ensures environment variables are set to their default values during testing.
"""

import os

import pytest

# Set DATABASE_URL before Django settings are loaded so tests run without a
# real PostgreSQL instance. This is intentionally only set here (test context).
os.environ.setdefault("DATABASE_URL", "sqlite:///db.sqlite3")

# Set a test SECRET_KEY before Django settings are loaded so the fail-fast
# check passes. This key is only used during testing.
os.environ.setdefault("SECRET_KEY", "test-key-for-tests-only")

# Enable DEBUG mode and set ALLOWED_HOSTS for the test suite so the
# production fail-fast guards do not fire.
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory
from hypothesis import settings as hypothesis_settings

from cases.models import (
    Case,
    CaseEntityRelationship,
    DocumentSource,
    JawafEntity,
    RelationshipType,
)

User = get_user_model()


def _is_ci() -> bool:
    """Return True when running in CI."""
    return os.environ.get("CI", "").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Hypothesis profiles
# ---------------------------------------------------------------------------
# "default"  — used locally; generous deadline, standard example count.
# "ci"       — activated when CI=true; keeps example count low to stay within
#              the PR build budget.  Full rigor is preserved in nightly/full
#              runs by explicitly loading the "default" profile.
# ---------------------------------------------------------------------------
hypothesis_settings.register_profile(
    "default",
    deadline=400,  # 400 ms instead of the library default 200 ms
)
hypothesis_settings.register_profile(
    "ci",
    deadline=800,  # slightly more forgiving on shared CI runners
    max_examples=10,  # reduce per-test cost; full nightly runs use "default"
)

# Auto-activate the ci profile when running inside GitHub Actions (or any
# environment that sets CI=true / CI=1 / CI=yes).
if _is_ci():
    hypothesis_settings.load_profile("ci")
else:
    hypothesis_settings.load_profile("default")


def pytest_collection_modifyitems(config, items):
    """
    Mark NESQ tests as integration.

    This preserves full-suite execution while still allowing marker-based
    selection when needed.
    """
    for item in items:
        normalized_path = str(item.fspath).replace("\\", "/")
        if "/tests/nesq/" in normalized_path:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True, scope="session")
def configure_test_settings():
    """
    Configure stable test settings once per session.

    Uses session scope to avoid repeating the same settings mutation for every
    test function — the storage backends we override here never change between
    tests so a single setup is safe and significantly faster.
    """
    from django.conf import settings as django_settings

    # Disable static file manifest checking for tests.
    # This prevents errors when static files haven't been collected.
    django_settings.STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }

    # Speed up auth-related tests dramatically and avoid timeout-induced
    # Hypothesis flakiness when creating many users.
    django_settings.PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.MD5PasswordHasher",
    ]


@pytest.fixture
def request_factory():
    """Create a Django RequestFactory for creating mock requests."""
    return RequestFactory()


def create_mock_request(user, method="get", path="/"):
    """
    Create a mock request object with the given user.

    Args:
        user: The user to attach to the request
        method: HTTP method ('get', 'post', etc.)
        path: Request path

    Returns:
        Mock request object with user attached
    """
    factory = RequestFactory()
    request_method = getattr(factory, method.lower())
    request = request_method(path)
    request.user = user
    return request


def create_entities_from_ids(entity_ids):
    """
    Helper function to create JawafEntity objects from entity ID strings.

    Uses bulk_create + a single filter query instead of N individual
    get_or_create calls, keeping the same semantics at a fraction of the
    database round-trips.

    Args:
        entity_ids: List of entity ID strings (e.g., ['entity:person/test'])

    Returns:
        List of JawafEntity objects in the same order as entity_ids
    """
    if not entity_ids:
        return []

    # Determine which ids already exist in one query to avoid re-saving them.
    existing_ids = set(
        JawafEntity.objects.filter(nes_id__in=entity_ids).values_list(
            "nes_id", flat=True
        )
    )
    # Save only new entities, going through full_clean() so that NES's
    # validate_entity_id() runs and malformed ids are caught in tests.
    for nid in entity_ids:
        if nid not in existing_ids:
            entity = JawafEntity(nes_id=nid)
            entity.full_clean()
            entity.save()
    # Fetch all requested entities in a single query and preserve order.
    by_id = {e.nes_id: e for e in JawafEntity.objects.filter(nes_id__in=entity_ids)}
    return [by_id[nid] for nid in entity_ids]


def create_case_with_entities(**kwargs):
    """
    Helper function to create a Case with entity relationships.

    Handles conversion of entity ID lists to JawafEntity objects.

    Args:
        **kwargs: Case fields, including:
            - alleged_entities: List of entity ID strings
            - related_entities: List of entity ID strings
            - locations: List of entity ID strings (stored as related relationships)

    Returns:
        Case object
    """
    # Extract entity fields
    alleged_entity_ids = kwargs.pop("alleged_entities", [])
    related_entity_ids = kwargs.pop("related_entities", [])
    location_ids = kwargs.pop("locations", [])

    # Create the case without entities
    case = Case.objects.create(**kwargs)

    # Add entity relationships using CaseEntityRelationship
    for nes_id in alleged_entity_ids:
        entity, _ = JawafEntity.objects.get_or_create(nes_id=nes_id)
        CaseEntityRelationship.objects.get_or_create(
            case=case, entity=entity, relationship_type=RelationshipType.ACCUSED
        )
    for nes_id in related_entity_ids:
        entity, _ = JawafEntity.objects.get_or_create(nes_id=nes_id)
        CaseEntityRelationship.objects.get_or_create(
            case=case, entity=entity, relationship_type=RelationshipType.RELATED
        )
    for nes_id in location_ids:
        entity, _ = JawafEntity.objects.get_or_create(nes_id=nes_id)
        CaseEntityRelationship.objects.get_or_create(
            case=case, entity=entity, relationship_type=RelationshipType.RELATED
        )

    return case


def create_document_source_with_entities(**kwargs):
    """
    Helper function to create a DocumentSource with entity relationships.

    Handles conversion of entity ID lists to JawafEntity objects.

    Args:
        **kwargs: DocumentSource fields, including:
            - related_entity_ids: List of entity ID strings (legacy name)
            - related_entities: List of entity ID strings

    Returns:
        DocumentSource object
    """
    # Extract entity fields (support both old and new names)
    related_entity_ids = kwargs.pop(
        "related_entity_ids", kwargs.pop("related_entities", [])
    )

    # Create the source without entities
    source = DocumentSource.objects.create(**kwargs)

    # Add entities using set()
    if related_entity_ids:
        source.related_entities.set(create_entities_from_ids(related_entity_ids))

    return source


def create_user_with_role(username, email, role, password="testpass123"):
    """
    Create a user with the specified role.

    Creates the role group if it doesn't exist and assigns the user to it.
    Also sets up necessary Django permissions.

    Args:
        username: Username for the user
        email: Email for the user
        role: Role name ('Admin', 'Moderator', 'Contributor', 'ReadOnly')
        password: Password for the user (default: 'testpass123')

    Returns:
        User object with role assigned
    """
    # Hypothesis examples can occasionally reuse generated usernames within the
    # same test transaction. Ensure uniqueness here to avoid IntegrityError
    # flakiness from auth_user.username constraints.
    normalize_username = User.objects.model.normalize_username
    base_username = normalize_username(username)
    username = base_username
    if "@" in email:
        email_local, email_domain = email.split("@", 1)
    else:
        email_local, email_domain = email, "example.com"

    suffix = 0
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = normalize_username(f"{base_username}_{suffix}")
        email = f"{email_local}_{suffix}@{email_domain}"

    user = User.objects.create_user(username=username, email=email, password=password)

    # Create or get the role group
    group, _ = Group.objects.get_or_create(name=role)
    user.groups.add(group)

    # Set staff status for Admin, Moderator, and Contributor
    if role in ["Admin", "Moderator", "Contributor"]:
        user.is_staff = True
        user.save()

    # Set superuser status for Admin
    if role == "Admin":
        user.is_superuser = True
        user.save()

    # Fetch all required permissions in two batched queries instead of 8+
    # individual get_or_create calls.  Django creates standard model permissions
    # during migrations so filter() is sufficient here.
    case_ct = ContentType.objects.get_for_model(Case)
    user_ct = ContentType.objects.get_for_model(User)

    case_perms = Permission.objects.filter(
        codename__in=["view_case", "change_case", "add_case", "delete_case"],
        content_type=case_ct,
    )
    user_perms = Permission.objects.filter(
        codename__in=["view_user", "change_user", "add_user", "delete_user"],
        content_type=user_ct,
    )

    if role in ["Admin", "Moderator", "Contributor"]:
        user.user_permissions.add(*case_perms)

    # Moderators and Admins can manage users
    if role in ["Admin", "Moderator"]:
        user.user_permissions.add(*user_perms)

    # ReadOnly: org-wide read role. No is_staff/is_superuser and no write perms.
    # Mirror the view_* set granted to the ReadOnly group in create_groups.py so
    # model-perm-gated checks (e.g. DjangoModelPermissions) behave faithfully:
    # GET on every content model is allowed, but no add/change/delete.
    if role == "ReadOnly":
        readonly_view_perms = Permission.objects.filter(
            codename__in=[
                "view_case",
                "view_documentsource",
                "view_documentsourceupload",
                "view_jawafentity",
                "view_caseentityrelationship",
            ],
            content_type__in=[
                ContentType.objects.get_for_model(Case),
                ContentType.objects.get_for_model(DocumentSource),
                ContentType.objects.get_for_model(JawafEntity),
                ContentType.objects.get_for_model(CaseEntityRelationship),
            ],
        )
        user.user_permissions.add(*readonly_view_perms)

    return user
