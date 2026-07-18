import pytest
from casework.ab.seed import (
    material_iris_from_case,
    seed_from_snapshot,
    snapshot_is_usable,
)


def _case_with_material(*, iri, material_type, urls, slug="x", display_name="Some Doc"):
    return {
        "slug": slug,
        "title": slug,
        "evidence": [
            {
                "material_iri": iri,
                "additional_details": "",
                "material": {
                    "display_name": display_name,
                    "material_type": material_type,
                    "urls": urls,
                },
            }
        ],
    }


def test_material_iris_from_case_reads_evidence():
    case = {"slug": "x", "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa_press_release/123"},
        {"material_iri": "https://jawafdehi.org/material/court_order/special.081-cr-0098"},
    ]}
    assert material_iris_from_case(case) == [
        "https://jawafdehi.org/material/ciaa_press_release/123",
        "https://jawafdehi.org/material/court_order/special.081-cr-0098",
    ]


def test_material_iris_from_case_handles_no_evidence():
    assert material_iris_from_case({"slug": "x"}) == []


def test_snapshot_is_usable_rejects_empty():
    with pytest.raises(ValueError, match="no cases"):
        snapshot_is_usable([])


@pytest.mark.django_db
def test_seed_creates_material_with_markdown_role(tmp_path):
    from materials.models import Material

    iri = "https://jawafdehi.org/material/press_release/20260409.98ebd075"
    urls = [
        {
            "link": "https://s3.jawafdehi.org/case_uploads/raw.pdf",
            "role": "RAW",
        },
        {
            "link": "https://s3.jawafdehi.org/case_uploads/markdown.md",
            "role": "MARKDOWN",
        },
    ]
    case = _case_with_material(iri=iri, material_type="press_release", urls=urls)
    snapshot_dir = tmp_path
    (snapshot_dir / "cases").mkdir()
    (snapshot_dir / "cases" / "x.json").write_text(
        __import__("json").dumps(case), encoding="utf-8"
    )

    result = seed_from_snapshot(str(snapshot_dir))

    assert result["materials_seeded"] == 1
    material = Material.objects.get(iri=iri)
    assert material.material_type == "press_release"
    roles = {
        mo.get("jawafdehi:linkRole"): mo.get("contentUrl")
        for mo in material.data.get("associatedMedia", [])
    }
    assert roles.get("MARKDOWN") == "https://s3.jawafdehi.org/case_uploads/markdown.md"
    assert roles.get("RAW") == "https://s3.jawafdehi.org/case_uploads/raw.pdf"


@pytest.mark.django_db
def test_seed_does_not_invent_markdown_role_when_absent(tmp_path):
    from materials.models import Material

    iri = "https://jawafdehi.org/material/news/20260606.fd1976ba"
    urls = [
        {
            "link": "https://example.com/story/1",
            "role": "RAW",
        },
    ]
    case = _case_with_material(iri=iri, material_type="news", urls=urls)
    snapshot_dir = tmp_path
    (snapshot_dir / "cases").mkdir()
    (snapshot_dir / "cases" / "x.json").write_text(
        __import__("json").dumps(case), encoding="utf-8"
    )

    seed_from_snapshot(str(snapshot_dir))

    material = Material.objects.get(iri=iri)
    roles = {mo.get("jawafdehi:linkRole") for mo in material.data.get("associatedMedia", [])}
    assert "MARKDOWN" not in roles
    assert roles == {"RAW"}


@pytest.mark.django_db
def test_seed_is_idempotent_on_rerun(tmp_path):
    from materials.models import Material

    iri = "https://jawafdehi.org/material/press_release/20260409.98ebd075"
    urls = [
        {"link": "https://s3.jawafdehi.org/case_uploads/raw.pdf", "role": "RAW"},
        {"link": "https://s3.jawafdehi.org/case_uploads/markdown.md", "role": "MARKDOWN"},
    ]
    case = _case_with_material(iri=iri, material_type="press_release", urls=urls)
    snapshot_dir = tmp_path
    (snapshot_dir / "cases").mkdir()
    (snapshot_dir / "cases" / "x.json").write_text(
        __import__("json").dumps(case), encoding="utf-8"
    )

    seed_from_snapshot(str(snapshot_dir))
    seed_from_snapshot(str(snapshot_dir))

    assert Material.objects.filter(iri=iri).count() == 1
    material = Material.objects.get(iri=iri)
    roles = [mo.get("jawafdehi:linkRole") for mo in material.data.get("associatedMedia", [])]
    assert roles.count("MARKDOWN") == 1
    assert roles.count("RAW") == 1
