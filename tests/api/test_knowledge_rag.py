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
from knowledge.importer import import_knowledge_manifest
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
def test_import_chunks_raw_document_text_with_recursive_overlap(staff_client):
    document_text = "\n\n".join(
        [
            "## Case statistics",
            "In 2079, annual report case statistics were published. " * 12,
            "## Investigation outcomes",
            "The report grouped outcomes by case type and institution. " * 12,
        ]
    )

    response = staff_client.post(
        "/api/knowledge/import/",
        data={
            "collection": {
                "name": "auto_chunked_reports",
                "display_name": "Auto Chunked Reports",
                "access_level": AccessLevel.PUBLIC,
            },
            "source": {
                "title": "Annual Report 2079",
                "source_type": "annual_report",
                "source_url": "https://jawafdehi.org/reports/2079.pdf",
                "access_level": AccessLevel.PUBLIC,
            },
            "document": {"markdown": document_text},
            "chunking": {
                "strategy": "recursive",
                "chunk_size": 360,
                "chunk_overlap": 80,
                "min_chunk_chars": 60,
            },
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["chunks_imported"] > 1
    chunks = list(KnowledgeChunk.objects.order_by("chunk_index"))
    assert chunks[0].section_title == "Case statistics"
    assert chunks[0].metadata["chunking_strategy"] == "recursive"
    assert all(len(chunk.text) <= 460 for chunk in chunks)


@pytest.mark.django_db
def test_import_chunks_page_aware_document_and_preserves_page_metadata():
    result = import_knowledge_manifest(
        {
            "collection": {
                "name": "page_reports",
                "display_name": "Page Reports",
                "access_level": AccessLevel.PUBLIC,
            },
            "source": {
                "title": "Annual Report 2080",
                "source_type": "annual_report",
                "source_url": "https://jawafdehi.org/reports/2080.pdf",
                "access_level": AccessLevel.PUBLIC,
            },
            "document": {
                "pages": [
                    {
                        "page": 10,
                        "section_title": "Registered cases",
                        "text": "Registered cases by type. " * 20,
                    },
                    {
                        "page": 11,
                        "section_title": "Court outcomes",
                        "text": "Court outcomes by institution. " * 20,
                    },
                ]
            },
            "chunking": {"chunk_size": 240, "chunk_overlap": 40},
        }
    )

    assert result.chunks_imported >= 2
    pages = list(
        KnowledgeChunk.objects.order_by("chunk_index").values_list(
            "page_start", "page_end", "section_title"
        )
    )
    assert (10, 10, "Registered cases") in pages
    assert (11, 11, "Court outcomes") in pages


@pytest.mark.django_db
def test_import_can_generate_embeddings_into_vector_store(settings):
    settings.KNOWLEDGE_RAG_EMBEDDING_MODEL = "test-embedding"
    embedder = FakeQueryEmbedder([0.1, 0.2, 0.3])

    result = import_knowledge_manifest(
        {
            "collection": {
                "name": "embedded_reports",
                "display_name": "Embedded Reports",
                "access_level": AccessLevel.PUBLIC,
            },
            "source": {
                "title": "Embedded Annual Report",
                "source_type": "annual_report",
                "source_url": "https://jawafdehi.org/reports/embedded.pdf",
                "access_level": AccessLevel.PUBLIC,
            },
            "document": {"text": "Annual report 2079 registered 120 cases. " * 12},
            "chunking": {"chunk_size": 240, "chunk_overlap": 40},
            "embedding": {"auto": True, "model": "test-embedding", "batch_size": 2},
        },
        query_embedder=embedder,
    )

    assert result.chunks_imported > 0
    assert result.embeddings_imported == result.chunks_imported
    assert KnowledgeEmbedding.objects.count() == result.chunks_imported
    embedding = KnowledgeEmbedding.objects.first()
    assert embedding.embedding_model == "test-embedding"
    assert embedding.embedding == [0.1, 0.2, 0.3]
    assert embedding.vector is not None


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
def test_import_source_pasted_markdown_chunks_and_indexes(staff_client):
    response = staff_client.post(
        "/api/knowledge/import-source/",
        data={
            "collection_name": "public_docs",
            "collection_display_name": "Public Docs",
            "source_title": "Jawafdehi platform FAQ",
            "source_type": "faq",
            "access_level": "public",
            "markdown": "## Case types\n\nJawafdehi tracks corruption accountability records.",
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["sources_imported"] == 1
    assert response.data["chunks_imported"] == 1
    chunk = KnowledgeChunk.objects.get()
    assert "corruption accountability" in chunk.text
    assert chunk.source.metadata["public_citation"]["title"] == "Jawafdehi platform FAQ"


@pytest.mark.django_db
def test_import_source_catalog_expands_manuscripts_and_is_idempotent(
    staff_client, monkeypatch
):
    class FakeResponse:
        content = json.dumps(
            {
                "name": "ciaa-annual-reports",
                "path": "/ciaa-annual-reports",
                "manuscripts": [
                    {
                        "url": "https://ngm-store.jawafdehi.org/uploads/2081.pdf",
                        "file_name": "annual-report-2081-82.pdf",
                        "metadata": {
                            "title": "पैँतिसौँ वार्षिक प्रतिवेदन आर्थिक वर्ष २०८१/८२",
                            "serial_number": "1",
                        },
                    }
                ],
            }
        ).encode()
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        "knowledge.source_importer.httpx.get",
        lambda *args, **kwargs: FakeResponse(),
    )

    payload = {
        "collection_name": "ciaa_annual_reports",
        "collection_display_name": "CIAA Annual Reports",
        "source_type": "annual_report",
        "access_level": "public",
        "source_url": "https://ngm-store.jawafdehi.org/indices/index.ciaa-annual-reports.json",
        "expand_catalog": True,
    }
    first = staff_client.post(
        "/api/knowledge/import-source/", data=payload, format="json"
    )
    second = staff_client.post(
        "/api/knowledge/import-source/", data=payload, format="json"
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert KnowledgeSource.objects.count() == 1
    assert KnowledgeChunk.objects.count() == 1
    source = KnowledgeSource.objects.get()
    assert source.source_url == "https://ngm-store.jawafdehi.org/uploads/2081.pdf"
    assert "2081" in source.metadata["year_tokens"]
    assert "२०८१" in source.metadata["year_tokens"]
    assert source.metadata["catalog_url"] == payload["source_url"]
    assert "2081/82" in KnowledgeChunk.objects.get().text

    search_response = staff_client.get(
        "/api/knowledge/public-search/",
        data={
            "query": "registered cases",
            "collection": "ciaa_annual_reports",
            "source_type": "annual_report",
            "year": "2081",
        },
    )
    assert search_response.status_code == 200
    assert search_response.data["results"][0]["source_url"] == source.source_url
    assert "2081" in search_response.data["results"][0]["metadata"]["year_tokens"]


@pytest.mark.django_db
def test_import_source_catalog_skips_embeddings_when_model_unconfigured(
    staff_client, monkeypatch, settings
):
    settings.KNOWLEDGE_RAG_EMBEDDING_MODEL = ""

    class FakeResponse:
        content = json.dumps(
            {
                "manuscripts": [
                    {
                        "url": "https://ngm-store.jawafdehi.org/uploads/2081.pdf",
                        "metadata": {"title": "CIAA Annual Report 2081/82"},
                    }
                ]
            }
        ).encode()
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        "knowledge.source_importer.httpx.get",
        lambda *args, **kwargs: FakeResponse(),
    )

    response = staff_client.post(
        "/api/knowledge/import-source/",
        data={
            "collection_name": "ciaa_annual_reports",
            "source_type": "annual_report",
            "access_level": "public",
            "source_url": "https://ngm-store.jawafdehi.org/indices/index.ciaa-annual-reports.json",
            "expand_catalog": True,
            "embed": True,
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["sources_imported"] == 1
    assert response.data["chunks_imported"] == 1
    assert response.data["embeddings_imported"] == 0
    assert KnowledgeEmbedding.objects.count() == 0


@pytest.mark.django_db
def test_import_source_pdf_url_passes_page_range_to_converter(
    staff_client, monkeypatch
):
    class FakeResponse:
        content = b"%PDF fake"
        headers = {"content-type": "application/pdf"}

        def raise_for_status(self):
            return None

    captured = {}

    def fake_convert(content, *, source_url, content_type, original_file_name, pages):
        captured["pages"] = pages
        captured["source_url"] = source_url
        return "Registered cases table from PDF page range."

    monkeypatch.setattr(
        "knowledge.source_importer.httpx.get",
        lambda *args, **kwargs: FakeResponse(),
    )
    monkeypatch.setattr(
        "knowledge.source_importer._content_to_markdown",
        fake_convert,
    )

    response = staff_client.post(
        "/api/knowledge/import-source/",
        data={
            "collection_name": "public_docs",
            "source_title": "CIAA Annual Report 2081",
            "source_type": "annual_report",
            "source_url": "https://jawafdehi.org/reports/2081.pdf",
            "pages": "12-15",
            "access_level": "public",
        },
        format="json",
    )

    assert response.status_code == 201
    assert captured == {
        "pages": "12-15",
        "source_url": "https://jawafdehi.org/reports/2081.pdf",
    }
    assert KnowledgeChunk.objects.filter(text__icontains="Registered cases").exists()


@pytest.mark.django_db
def test_knowledge_sources_api_filters_by_collection_and_returns_chunk_count(
    staff_client,
):
    annual_collection = make_collection(
        name="ciaa_annual_reports",
        display_name="CIAA Annual Reports",
        access_level=AccessLevel.PUBLIC,
    )
    other_collection = make_collection(
        name="other_docs",
        display_name="Other Docs",
        access_level=AccessLevel.PUBLIC,
    )
    annual_source = make_source(
        annual_collection,
        title="CIAA Annual Report 2081",
        access_level=AccessLevel.PUBLIC,
        source_url="https://jawafdehi.org/reports/2081.pdf",
    )
    make_source(
        other_collection,
        title="Other Public Document",
        access_level=AccessLevel.PUBLIC,
        source_url="https://jawafdehi.org/docs/other.pdf",
    )
    make_chunk(annual_source, text="Annual report locator chunk.")

    response = staff_client.get(
        "/api/knowledge/sources/",
        data={"collection": annual_collection.id, "search": "2081"},
    )

    assert response.status_code == 200
    rows = (
        response.data["results"] if isinstance(response.data, dict) else response.data
    )
    assert len(rows) == 1
    assert rows[0]["title"] == "CIAA Annual Report 2081"
    assert rows[0]["chunk_count"] == 1


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
