"""
Shared Hypothesis strategies for property-based testing.

This module contains reusable Hypothesis strategies used across multiple test files.
"""

from datetime import date, timedelta

from hypothesis import strategies as st

from cases.models import CaseType

# ============================================================================
# Entity ID Strategies
# ============================================================================


@st.composite
def valid_entity_id(draw):
    """Generate valid entity IDs matching NES format."""
    entity_types = ["person", "organization", "location"]
    entity_type = draw(st.sampled_from(entity_types))

    # Generate valid slug (ASCII lowercase letters, numbers, hyphens only)
    # NES validator expects: ^[a-z0-9]+(?:-[a-z0-9]+)*$
    slug = draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=3, max_size=50
        ).filter(
            lambda x: x
            and not x.startswith("-")
            and not x.endswith("-")
            and "--" not in x
        )
    )

    return f"entity:{entity_type}/{slug}"


@st.composite
def entity_id_list(draw, min_size=1, max_size=5):
    """Generate a list of valid entity IDs."""
    return draw(
        st.lists(valid_entity_id(), min_size=min_size, max_size=max_size, unique=True)
    )


@st.composite
def valid_entity_iri(draw):
    """Generate a valid canonical entity @id IRI (the ``nes_id`` bind form).

    Mirrors ``valid_entity_id`` but emits the schema.org @id IRI
    (``https://jawafdehi.org/entity/<prefix>/<slug>``) that
    ``CaseEntityRelationship.nes_id`` now requires.
    """
    return draw(valid_entity_id()).replace(
        "entity:", "https://jawafdehi.org/entity/", 1
    )


@st.composite
def entity_iri_list(draw, min_size=1, max_size=5):
    """Generate a list of valid entity @id IRIs (for ``nes_id`` binds)."""
    return draw(
        st.lists(valid_entity_iri(), min_size=min_size, max_size=max_size, unique=True)
    )


@st.composite
def invalid_entity_id(draw):
    """Generate invalid entity IDs for negative testing."""
    invalid_formats = [
        draw(st.text(min_size=1, max_size=20)),  # Random text
        f"invalid:{draw(st.text(min_size=1, max_size=20))}",  # Wrong prefix
        f"entity:{draw(st.text(min_size=1, max_size=20))}",  # Missing slash
        "",  # Empty string
    ]
    return draw(st.sampled_from(invalid_formats))


# ============================================================================
# Text and List Strategies
# ============================================================================


def filter_problematic_chars(text):
    """
    Filter out characters that cause issues in PostgreSQL JSON fields.

    Removes:
    - Null bytes (\u0000)
    - Other control characters that PostgreSQL can't handle in JSON
    - Unpaired surrogates
    """
    if not text:
        return text

    # Remove null bytes and other problematic control characters
    # Keep only printable characters and common whitespace
    filtered = "".join(char for char in text if char >= " " or char in "\t\n\r")

    # Remove unpaired surrogates (Unicode characters in the surrogate range)
    try:
        # Try to encode/decode to catch surrogate issues
        filtered.encode("utf-8")
        return filtered
    except UnicodeEncodeError:
        # If encoding fails, remove surrogates
        filtered = "".join(
            char for char in filtered if not (0xD800 <= ord(char) <= 0xDFFF)
        )
        return filtered


@st.composite
def text_list(draw, min_size=1, max_size=5):
    """Generate a list of text strings."""
    return draw(
        st.lists(
            st.text(min_size=1, max_size=200)
            .map(filter_problematic_chars)
            .filter(lambda x: x and x.strip()),
            min_size=min_size,
            max_size=max_size,
        )
    )


@st.composite
def tag_list(draw, min_size=0, max_size=5):
    """Generate a list of tags."""
    return draw(
        st.lists(
            st.text(
                alphabet=st.characters(
                    whitelist_categories=("Ll", "Nd"), whitelist_characters="-"
                ),
                min_size=3,
                max_size=30,
            ).filter(lambda x: x and not x.startswith("-") and not x.endswith("-")),
            min_size=min_size,
            max_size=max_size,
            unique=True,
        )
    )


# ============================================================================
# Timeline Strategies
# ============================================================================


@st.composite
def timeline_entry(draw):
    """Generate a valid timeline entry."""
    base_date = date(2020, 1, 1)
    days_offset = draw(st.integers(min_value=0, max_value=1825))  # 5 years

    title = draw(
        st.text(min_size=5, max_size=100)
        .map(filter_problematic_chars)
        .filter(lambda x: x and x.strip())
    )
    description = draw(
        st.text(min_size=10, max_size=500)
        .map(filter_problematic_chars)
        .filter(lambda x: x and x.strip())
    )

    return {
        "date": (base_date + timedelta(days=days_offset)).isoformat(),
        "title": title,
        "description": description,
    }


@st.composite
def timeline_list(draw, min_size=0, max_size=5):
    """Generate a list of timeline entries."""
    return draw(st.lists(timeline_entry(), min_size=min_size, max_size=max_size))


# ============================================================================
# Case Data Strategies
# ============================================================================


@st.composite
def minimal_case_data(draw):
    """
    Generate minimal valid case data for DRAFT state.

    According to Property 2, draft validation is lenient - only title and
    at least one alleged entity are required.
    """

    title = draw(
        st.text(min_size=1, max_size=200)
        .map(filter_problematic_chars)
        .filter(lambda x: x and x.strip())
    )

    return {
        "title": title,
        "alleged_entities": draw(entity_iri_list(min_size=1, max_size=3)),
        "case_type": draw(st.sampled_from([CaseType.CORRUPTION])),
    }


@st.composite
def complete_case_data(draw):
    """
    Generate complete valid case data for IN_REVIEW/PUBLISHED state.

    According to Property 2, IN_REVIEW validation is strict - all required
    fields must be present and valid.
    """

    title = draw(
        st.text(min_size=1, max_size=200)
        .map(filter_problematic_chars)
        .filter(lambda x: x and x.strip())
    )
    description = draw(
        st.text(min_size=10, max_size=1000)
        .map(filter_problematic_chars)
        .filter(lambda x: x and x.strip())
    )

    return {
        "title": title,
        "alleged_entities": draw(entity_iri_list(min_size=1, max_size=3)),
        "key_allegations": draw(text_list(min_size=1, max_size=5)),
        "case_type": draw(st.sampled_from([CaseType.CORRUPTION])),
        "description": description,
    }


@st.composite
def complete_case_data_with_timeline(draw):
    """
    Generate complete valid case data including timeline and tags.

    Suitable for PUBLISHED state with full data.
    """

    # Django's slugify() preserves underscores, but the case-slug grammar
    # (validate_slug / build_case_iri) rejects them, so a title containing "_"
    # would yield an invalid slug and 500 on serialization. Strip underscores
    # from the title only (it is the sole input to the auto-generated slug).
    title = draw(
        st.text(min_size=5, max_size=200)
        .map(filter_problematic_chars)
        .map(lambda x: x.replace("_", "") if x else x)
        .filter(lambda x: x and x.strip())
    )
    description = draw(
        st.text(min_size=20, max_size=1000)
        .map(filter_problematic_chars)
        .filter(lambda x: x and x.strip())
    )

    return {
        "title": title,
        "alleged_entities": draw(entity_iri_list(min_size=1, max_size=3)),
        "related_entities": draw(entity_iri_list(min_size=0, max_size=3)),
        "locations": draw(entity_iri_list(min_size=0, max_size=2)),
        "key_allegations": draw(text_list(min_size=1, max_size=5)),
        "case_type": draw(st.sampled_from([CaseType.CORRUPTION])),
        "description": description,
        "tags": draw(tag_list(min_size=0, max_size=5)),
        "timeline": draw(timeline_list(min_size=0, max_size=3)),
    }


# ============================================================================
# User Data Strategies
# ============================================================================


@st.composite
def user_with_role(draw, role):
    """
    Generate a User with the specified role.

    Roles: 'Admin', 'Moderator', 'Caseworker'
    """
    # Use Hypothesis-driven UUIDs instead of uuid4() so examples are fully
    # deterministic and reproducible during shrinking/replay.
    unique_id = draw(st.uuids()).hex[:8]
    base_username = draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789_",
            min_size=3,
            max_size=12,
        ).filter(lambda x: x and not x.startswith("_"))
    )

    username = f"{base_username}_{unique_id}"
    email = f"{username}@example.com"

    return {
        "username": username,
        "email": email,
        "role": role,
    }


# ============================================================================
# Simplified Strategies for Faster Tests
# ============================================================================


@st.composite
def simple_entity_id(draw):
    """Generate simple valid entity IDs for faster test execution."""
    entity_types = ["person", "organization", "location"]
    entity_type = draw(st.sampled_from(entity_types))

    # Use simpler alphabet for faster generation
    slug = draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=3, max_size=20
        )
    )

    return f"entity:{entity_type}/{slug}"


@st.composite
def simple_entity_id_list(draw, min_size=1, max_size=5):
    """Generate a list of simple entity IDs for faster tests."""
    return draw(
        st.lists(simple_entity_id(), min_size=min_size, max_size=max_size, unique=True)
    )


@st.composite
def simple_entity_iri(draw):
    """Generate a simple valid entity @id IRI (the ``nes_id`` bind form)."""
    return draw(simple_entity_id()).replace(
        "entity:", "https://jawafdehi.org/entity/", 1
    )


@st.composite
def simple_entity_iri_list(draw, min_size=1, max_size=5):
    """Generate a list of simple entity @id IRIs (for ``nes_id`` binds)."""
    return draw(
        st.lists(simple_entity_iri(), min_size=min_size, max_size=max_size, unique=True)
    )


@st.composite
def simple_text_list(draw, min_size=1, max_size=5):
    """Generate simple text lists for faster tests."""
    return draw(
        st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=5, max_size=50),
            min_size=min_size,
            max_size=max_size,
        )
    )


@st.composite
def simple_complete_case_data(draw):
    """Generate complete case data with simpler values for faster tests."""

    return {
        "title": draw(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=5, max_size=50)
        ),
        "alleged_entities": draw(simple_entity_iri_list(min_size=1, max_size=2)),
        "key_allegations": draw(simple_text_list(min_size=1, max_size=2)),
        "case_type": draw(st.sampled_from([CaseType.CORRUPTION])),
        "description": draw(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=20, max_size=100)
        ),
    }
