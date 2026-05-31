import json
import subprocess
from pathlib import Path

import pytest
from django.db import transaction

from nes.core.identifiers import build_relationship_id

from nesdb.importer import entities_with_prefix, import_full, import_incremental
from nesdb.models import NesEntity, NesEntityName, NesRelationship, NesSyncState


pytestmark = pytest.mark.django_db


def entity_payload(slug="alice", prefix="person", full_name="Alice Person", sub_type=None):
    tp = prefix.split("/")[0]
    payload: dict = {
        "slug": slug,
        "entity_prefix": prefix,
        "type": tp,
        "names": [
            {
                "kind": "PRIMARY",
                "en": {"full": full_name, "given": full_name.split()[0]},
            }
        ],
        "version_summary": {
            "entity_or_relationship_id": f"entity:{prefix}/{slug}",
            "type": "ENTITY",
            "version_number": 1,
            "author": {"slug": "tester"},
            "change_description": "test",
            "created_at": "2026-01-01T00:00:00Z",
        },
        "created_at": "2026-01-01T00:00:00Z",
    }
    if sub_type is not None:
        payload["sub_type"] = sub_type
    elif "/" in prefix:
        payload["sub_type"] = prefix.split("/", 1)[1]
    return payload


def relationship_payload(source, target, relationship_type="EMPLOYED_BY"):
    return {
        "source_entity_id": source,
        "target_entity_id": target,
        "type": relationship_type,
        "created_at": "2026-01-01T00:00:00Z",
    }


def write_json(repo, relative_path, payload):
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit_all(repo, message):
    git(repo, "add", ".")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def nes_repo(tmp_path):
    repo = tmp_path / "nes-db" / "v2"
    repo.mkdir(parents=True)
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    return repo


def test_full_import_imports_entities_relationships_and_names(nes_repo):
    alice = entity_payload("alice")
    party = entity_payload("party", "organization/political_party", "Party")
    alice_id = "entity:person/alice"
    party_id = "entity:organization/political_party/party"
    relationship = relationship_payload(alice_id, party_id, "MEMBER_OF")
    rel_id = build_relationship_id(alice_id, party_id, "MEMBER_OF")

    write_json(nes_repo, "entity/person/alice.json", alice)
    write_json(nes_repo, "entity/organization/political_party/party.json", party)
    write_json(nes_repo, "relationship/person/alice/organization/political_party/party/MEMBER_OF.json", relationship)
    head = commit_all(nes_repo, "initial")

    result = import_full(nes_repo)

    assert result.commit_hash == head
    assert result.entities_upserted == 2
    assert result.relationships_upserted == 1
    assert NesEntity.objects.count() == 2
    assert NesEntityName.objects.filter(en_full="Alice Person").exists()
    assert NesRelationship.objects.get().relationship_id == rel_id
    assert NesSyncState.objects.get(pk=1).last_commit_hash == head


def test_incremental_sync_upserts_entity_from_git_diff(nes_repo):
    write_json(nes_repo, "entity/person/alice.json", entity_payload("alice", full_name="Alice Person"))
    commit_all(nes_repo, "initial")
    import_full(nes_repo)

    write_json(nes_repo, "entity/person/alice.json", entity_payload("alice", full_name="Alice Updated"))
    head = commit_all(nes_repo, "update")

    result = import_incremental(nes_repo)

    assert result.commit_hash == head
    assert result.entities_upserted == 1
    assert NesEntity.objects.count() == 1
    assert NesEntityName.objects.get().en_full == "Alice Updated"
    assert NesSyncState.objects.get(pk=1).last_commit_hash == head


def test_incremental_sync_deletes_removed_entity(nes_repo):
    write_json(nes_repo, "entity/person/alice.json", entity_payload("alice"))
    commit_all(nes_repo, "initial")
    import_full(nes_repo)

    (nes_repo / "entity/person/alice.json").unlink()
    head = commit_all(nes_repo, "delete")

    result = import_incremental(nes_repo)

    assert result.commit_hash == head
    assert result.entities_deleted == 1
    assert NesEntity.objects.count() == 0
    assert NesEntityName.objects.count() == 0


def test_watermark_update_rolls_back_with_import_transaction(nes_repo, monkeypatch):
    write_json(nes_repo, "entity/person/alice.json", entity_payload("alice"))
    commit_all(nes_repo, "initial")
    import_full(nes_repo)
    old_commit = NesSyncState.objects.get(pk=1).last_commit_hash

    write_json(nes_repo, "entity/person/alice.json", entity_payload("alice", full_name="Alice Updated"))
    commit_all(nes_repo, "update")

    def fail_update_watermark(result):
        raise RuntimeError("boom")

    monkeypatch.setattr("nesdb.importer.update_watermark", fail_update_watermark)

    with pytest.raises(RuntimeError):
        import_incremental(nes_repo)

    assert NesSyncState.objects.get(pk=1).last_commit_hash == old_commit
    assert NesEntityName.objects.get().en_full == "Alice Person"


def test_relationship_delete_uses_old_json_to_preserve_fk_integrity(nes_repo):
    alice_id = "entity:person/alice"
    party_id = "entity:organization/political_party/party"
    write_json(nes_repo, "entity/person/alice.json", entity_payload("alice"))
    write_json(nes_repo, "entity/organization/political_party/party.json", entity_payload("party", "organization/political_party", "Party"))
    write_json(nes_repo, "relationship/person/alice/organization/political_party/party/MEMBER_OF.json", relationship_payload(alice_id, party_id, "MEMBER_OF"))
    commit_all(nes_repo, "initial")
    import_full(nes_repo)

    (nes_repo / "relationship/person/alice/organization/political_party/party/MEMBER_OF.json").unlink()
    commit_all(nes_repo, "delete relationship")
    result = import_incremental(nes_repo)

    assert result.relationships_deleted == 1
    assert NesRelationship.objects.count() == 0
    assert NesEntity.objects.count() == 2


def test_entity_prefix_match_uses_segment_boundary_not_plain_startswith(nes_repo):
    write_json(nes_repo, "entity/organization/government/federal/ministry.json", entity_payload("ministry", "organization/government/federal", "Ministry", sub_type="government_body"))
    write_json(nes_repo, "entity/organization/government/legacy.json", entity_payload("legacy", "organization/government", "Legacy", sub_type="government_body"))
    write_json(nes_repo, "entity/organization/political_party/nepali-congress.json", entity_payload("nepali-congress", "organization/political_party", "NC"))
    commit_all(nes_repo, "initial")
    import_full(nes_repo)

    gov_ids = set(entities_with_prefix("organization/government").values_list("entity_id", flat=True))
    assert gov_ids == {"entity:organization/government/legacy", "entity:organization/government/federal/ministry"}

    party_ids = set(entities_with_prefix("organization/political_party").values_list("entity_id", flat=True))
    assert party_ids == {"entity:organization/political_party/nepali-congress"}
