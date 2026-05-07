import json

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from caseworker.models import Prompt, PublicChatConfig, RAGSkillProfile, Skill
from knowledge.models import (
    AccessLevel,
    KnowledgeChunk,
    KnowledgeCollection,
    KnowledgeEmbedding,
    KnowledgeSource,
)
from knowledge.retrieval import KnowledgeAccessContext, KnowledgeRetriever

User = get_user_model()


def create_user_with_role(username, email, role):
    user = User.objects.create_user(username=username, email=email, password="testpass")
    group, _ = Group.objects.get_or_create(name=role)
    user.groups.add(group)
    if role in {"Admin", "Moderator", "Contributor"}:
        user.is_staff = True
    if role == "Admin":
        user.is_superuser = True
    user.save()
    return user


@pytest.fixture
def staff_client():
    user = User.objects.create_user(
        username="knowledge-admin",
        email="knowledge-admin@example.com",
        password="testpass",
        is_staff=True,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_collection(**overrides):
    data = {
        "name": "annual_reports",
        "display_name": "Annual Reports",
        "description": "CIAA annual reports",
    }
    data.update(overrides)
    return KnowledgeCollection.objects.create(**data)


def make_source(collection, **overrides):
    data = {
        "collection": collection,
        "title": "Annual Report 2079",
        "source_type": "annual_report",
    }
    data.update(overrides)
    return KnowledgeSource.objects.create(**data)


def make_chunk(source, **overrides):
    data = {
        "source": source,
        "chunk_index": 0,
        "text": "In fiscal year 2079, 120 corruption cases were registered.",
        "content_hash": "hash-2079-0",
    }
    data.update(overrides)
    return KnowledgeChunk.objects.create(**data)


class FakeQueryEmbedder:
    def __init__(self, vector):
        self.vector = vector
        self.documents = []

    def embed_query(self, query):
        return self.vector

    def embed_documents(self, texts):
        self.documents.extend(texts)
        return [self.vector for _ in texts]


@pytest.mark.django_db
def test_knowledge_defaults_private():
    collection = make_collection()
    source = make_source(collection)

    assert collection.access_level == AccessLevel.PRIVATE
    assert source.access_level == AccessLevel.PRIVATE


@pytest.mark.django_db
def test_public_retrieval_only_uses_public_active_configured_collections():
    configured = make_collection(access_level=AccessLevel.PUBLIC)
    configured_source = make_source(
        configured,
        access_level=AccessLevel.PUBLIC,
        source_url="https://jawafdehi.org/reports/2079.pdf",
    )
    make_chunk(configured_source, text="2079 annual report registered 120 cases.")

    unconfigured = make_collection(
        name="faq", display_name="FAQ", access_level=AccessLevel.PUBLIC
    )
    unconfigured_source = make_source(
        unconfigured,
        title="FAQ",
        access_level=AccessLevel.PUBLIC,
        source_url="https://jawafdehi.org/faq",
    )
    make_chunk(
        unconfigured_source,
        text="2079 annual report registered 999 cases.",
        content_hash="hash-faq",
    )

    private_collection = make_collection(
        name="internal", display_name="Internal", access_level=AccessLevel.PRIVATE
    )
    private_source = make_source(private_collection, title="Private report")
    make_chunk(
        private_source,
        text="2079 annual report registered 888 private cases.",
        content_hash="hash-private",
    )

    results = KnowledgeRetriever().retrieve(
        query="2079 annual report registered cases",
        access_context=KnowledgeAccessContext.public_context(),
        collections=KnowledgeCollection.objects.filter(id=configured.id),
        max_results=5,
    )

    assert [result.chunk.source_id for result in results] == [configured_source.id]


@pytest.mark.django_db
def test_staff_can_import_public_knowledge_manifest_from_api(staff_client):
    response = staff_client.post(
        "/api/knowledge/import/",
        data={
            "collection": {
                "name": "public_docs",
                "display_name": "Public Docs",
                "access_level": AccessLevel.PUBLIC,
            },
            "source": {
                "title": "Public FAQ",
                "source_type": "faq",
                "source_url": "https://jawafdehi.org/faq",
                "access_level": AccessLevel.PUBLIC,
            },
            "chunks": [
                {
                    "text": "Public chat answers document questions from public chunks.",
                    "chunk_index": 0,
                    "section_title": "Public chat",
                }
            ],
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["collection"]["name"] == "public_docs"
    assert response.data["source"]["title"] == "Public FAQ"
    assert response.data["chunks_imported"] == 1
    assert KnowledgeCollection.objects.filter(name="public_docs").exists()
    assert KnowledgeChunk.objects.filter(
        text__icontains="document questions from public chunks"
    ).exists()


@pytest.mark.django_db
def test_public_knowledge_import_requires_public_citation(staff_client):
    response = staff_client.post(
        "/api/knowledge/import/",
        data={
            "collection": {
                "name": "public_docs",
                "display_name": "Public Docs",
                "access_level": AccessLevel.PUBLIC,
            },
            "source": {
                "title": "Uncited public doc",
                "access_level": AccessLevel.PUBLIC,
            },
            "chunks": [{"text": "Cannot be public without citation."}],
        },
        format="json",
    )

    assert response.status_code == 400
    assert "source_url" in response.data["detail"]


@pytest.mark.django_db
def test_public_retrieval_with_empty_collection_scope_returns_no_chunks():
    collection = make_collection(access_level=AccessLevel.PUBLIC)
    source = make_source(
        collection,
        access_level=AccessLevel.PUBLIC,
        source_url="https://jawafdehi.org/reports/2079.pdf",
    )
    make_chunk(source, text="2079 annual report registered 120 cases.")

    results = KnowledgeRetriever().retrieve(
        query="2079 annual report registered cases",
        access_context=KnowledgeAccessContext.public_context(),
        collections=KnowledgeCollection.objects.none(),
        max_results=5,
    )

    assert results == []


@pytest.mark.django_db
def test_public_evidence_uses_strict_public_allowlist():
    collection = make_collection(access_level=AccessLevel.PUBLIC)
    source = make_source(
        collection,
        access_level=AccessLevel.PUBLIC,
        storage_path="gs://private-bucket/annual-report-2079.pdf",
        metadata={"secret": "do-not-expose"},
    )
    chunk = make_chunk(
        source,
        metadata={"internal_page_key": "private"},
    )

    result = KnowledgeRetriever().retrieve(
        query="2079 corruption cases",
        access_context=KnowledgeAccessContext.public_context(),
        collections=KnowledgeCollection.objects.filter(id=collection.id),
        max_results=1,
    )[0]
    public_evidence = result.as_public_evidence()

    assert public_evidence["chunk_id"] == str(chunk.id)
    assert public_evidence["source_url"] == ""
    assert "storage_path" not in public_evidence
    assert "metadata" not in public_evidence


@pytest.mark.django_db
def test_retriever_uses_hybrid_mode_when_embeddings_are_available(settings):
    settings.KNOWLEDGE_RAG_EMBEDDING_MODEL = "test-embedding"
    collection = make_collection(access_level=AccessLevel.PUBLIC)
    source = make_source(
        collection,
        access_level=AccessLevel.PUBLIC,
        source_url="https://jawafdehi.org/reports/2079.pdf",
    )
    matching_chunk = make_chunk(
        source,
        text="This chunk has unrelated words.",
        content_hash="semantic-match",
    )
    other_chunk = make_chunk(
        source,
        chunk_index=1,
        text="This chunk is unrelated too.",
        content_hash="semantic-other",
    )
    KnowledgeEmbedding.objects.create(
        chunk=matching_chunk,
        embedding_model="test-embedding",
        embedding=[1.0, 0.0, 0.0],
    )
    KnowledgeEmbedding.objects.create(
        chunk=other_chunk,
        embedding_model="test-embedding",
        embedding=[0.0, 1.0, 0.0],
    )

    results = KnowledgeRetriever(
        query_embedder=FakeQueryEmbedder([1.0, 0.0, 0.0])
    ).retrieve(
        query="semantic annual report",
        access_context=KnowledgeAccessContext.public_context(),
        collections=KnowledgeCollection.objects.filter(id=collection.id),
        max_results=2,
    )

    assert results[0].chunk.id == matching_chunk.id
    assert results[0].retrieval_mode == "hybrid"
    assert results[0].vector_score > 0


@pytest.mark.django_db
def test_anonymous_cannot_retrieve_private_chunks():
    collection = make_collection()
    source = make_source(collection)
    make_chunk(source)

    results = KnowledgeRetriever().retrieve(
        query="2079 corruption cases",
        access_context=KnowledgeAccessContext.public_context(),
        collections=KnowledgeCollection.objects.filter(id=collection.id),
        max_results=5,
    )

    assert results == []


@pytest.mark.django_db
def test_admin_and_contributor_access_private_knowledge_by_role_or_share():
    collection = make_collection()
    source = make_source(collection)
    make_chunk(source)
    admin = create_user_with_role("admin", "admin@example.com", "Admin")
    contributor = create_user_with_role(
        "contributor", "contributor@example.com", "Contributor"
    )
    outsider = create_user_with_role("outsider", "outsider@example.com", "Contributor")
    source.allowed_users.add(contributor)

    admin_results = KnowledgeRetriever().retrieve(
        query="2079 corruption cases",
        access_context=KnowledgeAccessContext(user=admin),
        collections=KnowledgeCollection.objects.filter(id=collection.id),
        max_results=5,
    )
    contributor_results = KnowledgeRetriever().retrieve(
        query="2079 corruption cases",
        access_context=KnowledgeAccessContext(user=contributor),
        collections=KnowledgeCollection.objects.filter(id=collection.id),
        max_results=5,
    )
    outsider_results = KnowledgeRetriever().retrieve(
        query="2079 corruption cases",
        access_context=KnowledgeAccessContext(user=outsider),
        collections=KnowledgeCollection.objects.filter(id=collection.id),
        max_results=5,
    )

    assert [result.chunk.source_id for result in admin_results] == [source.id]
    assert [result.chunk.source_id for result in contributor_results] == [source.id]
    assert outsider_results == []


@pytest.mark.django_db
def test_import_knowledge_artifacts_is_idempotent(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    chunks_path = tmp_path / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            [
                {
                    "text": "Annual report 2079 registered 120 cases.",
                    "chunk_index": 0,
                    "page_start": 12,
                    "page_end": 12,
                    "section_title": "Registered cases",
                    "embedding_model": "test-embedding",
                    "embedding": [0.1, 0.2, 0.3],
                }
            ]
        ),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(
            {
                "collection": {
                    "name": "annual_reports",
                    "display_name": "Annual Reports",
                    "access_level": "public",
                },
                "source": {
                    "title": "Annual Report 2079",
                    "source_type": "annual_report",
                    "access_level": "public",
                    "source_url": "https://jawafdehi.org/reports/2079.pdf",
                    "checksum": "checksum-2079",
                },
                "chunks_file": "chunks.json",
            }
        ),
        encoding="utf-8",
    )

    call_command("import_knowledge_artifacts", str(manifest_path))
    call_command("import_knowledge_artifacts", str(manifest_path))

    assert KnowledgeCollection.objects.count() == 1
    assert KnowledgeSource.objects.count() == 1
    assert KnowledgeChunk.objects.count() == 1
    chunk = KnowledgeChunk.objects.get()
    assert chunk.page_start == 12
    assert chunk.embeddings.get().dimensions == 3


@pytest.mark.django_db
def test_sync_rag_skill_imports_manifest_and_attaches_active_public_chat(tmp_path):
    PublicChatConfig.objects.all().delete()
    prompt = Prompt.objects.create(
        name="public-chat-test",
        display_name="Public Chat Test",
        prompt="Use public evidence only.",
    )
    config = PublicChatConfig.objects.create(
        name="default-test",
        is_active=True,
        enabled=True,
        prompt=prompt,
    )
    skill_dir = tmp_path / "ciaa-annual-reports"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "# CIAA Annual Reports\n\nAnswer only from annual report chunks.",
        encoding="utf-8",
    )
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "name": "ciaa_annual_reports",
                "display_name": "CIAA Annual Reports",
                "description": "CIAA annual report RAG skill",
                "trigger_keywords": ["annual report", "2081/82"],
                "attach_active_public_chat": True,
                "max_results": 3,
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "collection": {
                    "name": "ciaa_annual_reports",
                    "display_name": "CIAA Annual Reports",
                    "access_level": "public",
                },
                "source": {
                    "title": "CIAA Annual Report 2081/82",
                    "source_type": "annual_report",
                    "access_level": "public",
                    "source_url": "https://jawafdehi.org/reports/2081-82.pdf",
                },
                "chunks": [
                    {
                        "text": "In 2081/82, the annual report registered 135 cases.",
                        "chunk_index": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    call_command("sync_rag_skill", str(skill_dir))

    skill = Skill.objects.get(name="ciaa_annual_reports")
    profile = RAGSkillProfile.objects.get(name="ciaa_annual_reports")
    collection = KnowledgeCollection.objects.get(name="ciaa_annual_reports")
    config.refresh_from_db()

    assert "Answer only from annual report chunks" in skill.content
    assert profile.max_results == 3
    assert list(profile.collections.values_list("name", flat=True)) == [
        "ciaa_annual_reports"
    ]
    assert config.knowledge_rag_enabled is True
    assert config.rag_skill_profiles.get() == profile
    assert config.knowledge_collections.get() == collection
    assert list(config.prompt.skills.all()) == []


@pytest.mark.django_db
def test_import_rejects_public_sources_without_citation_target(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "collection": {"name": "annual_reports", "access_level": "public"},
                "source": {
                    "title": "Annual Report 2079",
                    "access_level": "public",
                },
                "chunks": [{"text": "Report text"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CommandError, match="require source_url"):
        call_command("import_knowledge_artifacts", str(manifest_path))


@pytest.mark.django_db
def test_import_rejects_storage_path_only_public_sources(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "collection": {"name": "annual_reports", "access_level": "public"},
                "source": {
                    "title": "Annual Report 2079",
                    "access_level": "public",
                    "storage_path": "gs://private-bucket/annual-report-2079.pdf",
                },
                "chunks": [{"text": "Report text"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CommandError, match="metadata.public_citation"):
        call_command("import_knowledge_artifacts", str(manifest_path))


@pytest.mark.django_db
def test_import_accepts_explicit_public_citation_metadata(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "collection": {"name": "annual_reports", "access_level": "public"},
                "source": {
                    "title": "Annual Report 2079",
                    "access_level": "public",
                    "storage_path": "gs://private-bucket/annual-report-2079.pdf",
                    "metadata": {
                        "public_citation": {
                            "title": "Annual Report 2079",
                            "identifier": "annual-report-2079",
                        }
                    },
                },
                "chunks": [{"text": "Report text"}],
            }
        ),
        encoding="utf-8",
    )

    call_command("import_knowledge_artifacts", str(manifest_path))

    assert KnowledgeSource.objects.get().metadata["public_citation"]["identifier"] == (
        "annual-report-2079"
    )


@pytest.mark.django_db
def test_embed_knowledge_chunks_generates_missing_embeddings(settings, monkeypatch):
    from knowledge.management.commands import embed_knowledge_chunks

    settings.KNOWLEDGE_RAG_EMBEDDING_MODEL = "test-embedding"
    collection = make_collection(access_level=AccessLevel.PUBLIC)
    source = make_source(
        collection,
        access_level=AccessLevel.PUBLIC,
        source_url="https://jawafdehi.org/reports/2079.pdf",
    )
    chunk = make_chunk(source, text="Annual report 2079 registered 120 cases.")
    embedder = FakeQueryEmbedder([0.1, 0.2, 0.3])
    monkeypatch.setattr(
        embed_knowledge_chunks.KnowledgeQueryEmbedder,
        "from_settings",
        staticmethod(lambda: embedder),
    )

    call_command("embed_knowledge_chunks", "--collection", collection.name)

    embedding = KnowledgeEmbedding.objects.get(chunk=chunk)
    assert embedder.documents == ["Annual report 2079 registered 120 cases."]
    assert embedding.embedding_model == "test-embedding"
    assert embedding.embedding == [0.1, 0.2, 0.3]
    assert embedding.dimensions == 3


@pytest.mark.django_db
def test_embed_knowledge_chunks_skips_existing_embeddings_without_force(
    settings, monkeypatch
):
    from knowledge.management.commands import embed_knowledge_chunks

    settings.KNOWLEDGE_RAG_EMBEDDING_MODEL = "test-embedding"
    collection = make_collection(access_level=AccessLevel.PUBLIC)
    source = make_source(
        collection,
        access_level=AccessLevel.PUBLIC,
        source_url="https://jawafdehi.org/reports/2079.pdf",
    )
    chunk = make_chunk(source, text="Annual report 2079 registered 120 cases.")
    KnowledgeEmbedding.objects.create(
        chunk=chunk,
        embedding_model="test-embedding",
        embedding=[1.0, 0.0],
        dimensions=2,
    )
    embedder = FakeQueryEmbedder([0.1, 0.2, 0.3])
    monkeypatch.setattr(
        embed_knowledge_chunks.KnowledgeQueryEmbedder,
        "from_settings",
        staticmethod(lambda: embedder),
    )

    call_command("embed_knowledge_chunks", "--collection", collection.name)

    embedding = KnowledgeEmbedding.objects.get(chunk=chunk)
    assert embedder.documents == []
    assert embedding.embedding == [1.0, 0.0]

    call_command("embed_knowledge_chunks", "--collection", collection.name, "--force")

    embedding.refresh_from_db()
    assert embedder.documents == ["Annual report 2079 registered 120 cases."]
    assert embedding.embedding == [0.1, 0.2, 0.3]
    assert embedding.dimensions == 3
