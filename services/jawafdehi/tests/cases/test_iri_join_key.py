"""Join-key integrity: the canonical NES entity @id IRI host is enforced.

Jawafdehi stores only the entity @id IRI as a cross-service join key. The
scheme+host is part of that key, so the write boundaries (``validate_nes_id``,
``EntityListField``, the ``MultiEntityIDField`` widget) must STRICTLY reject a
valid-shaped IRI on a non-canonical host/scheme/port — otherwise a stored id
could never match the NES PK.
"""

import pytest
from django.core.exceptions import ValidationError

from cases.fields import EntityListField
from cases.models import CaseEntityRelationship, validate_nes_id
from cases.widgets import MultiEntityIDField

CANON = "https://jawafdehi.org/entity/person/ram"
NONCANONICAL = [
    "http://evil.com/entity/person/ram",
    "https://x:8443/entity/person/ram",
    "http://jawafdehi.org/entity/person/ram",  # wrong scheme
]


def test_validate_nes_id_accepts_canonical():
    validate_nes_id(CANON)  # no raise


@pytest.mark.parametrize("bad", NONCANONICAL)
def test_validate_nes_id_rejects_noncanonical_host(bad):
    with pytest.raises(ValidationError):
        validate_nes_id(bad)


def test_validate_nes_id_rejects_over_max_length():
    too_long = "https://jawafdehi.org/entity/person/" + ("a" * 300)
    with pytest.raises(ValidationError):
        validate_nes_id(too_long)


@pytest.mark.parametrize("bad", NONCANONICAL)
def test_entity_list_field_rejects_noncanonical_host(bad):
    field = EntityListField(blank=True)
    with pytest.raises(ValidationError):
        field.validate([bad], model_instance=None)  # type: ignore[arg-type]


def test_entity_list_field_accepts_canonical():
    field = EntityListField(blank=True)
    field.validate([CANON], model_instance=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", NONCANONICAL)
def test_widget_field_rejects_noncanonical_host(bad):
    with pytest.raises(ValidationError):
        MultiEntityIDField().validate([bad])


def test_relationship_clean_rejects_noncanonical_host(db):
    from cases.models import Case, CaseType, CaseState, RelationshipType

    case = Case.objects.create(
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        title="t",
    )
    rel = CaseEntityRelationship(
        case=case,
        nes_id="http://evil.com/entity/person/ram",
        relationship_type=RelationshipType.ALLEGED,
    )
    with pytest.raises(ValidationError):
        rel.full_clean()
    # ...and the canonical host validates fine.
    rel.nes_id = CANON
    rel.full_clean()
