"""``seed_jawafdehi`` splats a dict into ``Case(**defaults)`` — every key has
to be a real model field.

The ``case_type`` → ``offence_type`` rename made this a live trap in both
directions: on create, an unknown key is a ``TypeError``; on update, the loop
``setattr``s it onto the instance, so the stray attribute is silently dropped
and the real field is never assigned. The second one is the dangerous half —
it saves a case with an empty offence type and reports success.

Written as a source read rather than a run, because the command's only entry
point pulls from a remote portal.
"""

import ast
import inspect
import textwrap

from django.apps import apps

from review.management.commands import seed_jawafdehi


def _defaults_keys() -> set[str]:
    """The literal keys of the ``defaults`` dict in ``_upsert_case``."""
    # ``textwrap.dedent`` because a method's source is indented and a
    # decorator line keeps ``ast.parse`` from accepting it otherwise.
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(seed_jawafdehi.Command._upsert_case))
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "defaults"
            and isinstance(node.value, ast.Dict)
        ):
            return {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError("no literal `defaults` dict found in _upsert_case")


#: ``evidence`` is a pre-existing dead key, not a new leak. ``Case.evidence``
#: was replaced by the CaseMaterialReference join (ADR: cases own no
#: documents), and this command's ``handle()`` already raises
#: NotImplementedError for exactly that reason -- it has to be rewired to
#: create Material rows before it can run at all. Left in the dict because the
#: rewire needs the payload; named here so the guard below still catches
#: anything NEW.
KNOWN_DEAD_KEYS = {"evidence"}


def test_every_seeded_key_is_a_real_case_field():
    Case = apps.get_model("cases", "Case")
    # ``court_cases`` is a settable property backed by a join, not a column.
    settable = {field.name for field in Case._meta.get_fields()} | {"court_cases"}

    unknown = _defaults_keys() - settable - KNOWN_DEAD_KEYS
    assert not unknown, f"seed_jawafdehi writes keys Case does not have: {unknown}"


def test_the_offence_type_is_seeded_under_its_real_name():
    """The specific instance of the bug above, pinned by name."""
    keys = _defaults_keys()
    assert "offence_type" in keys
    assert "case_type" not in keys, "the renamed field, still under its old name"
