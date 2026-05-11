from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from caseworker.permissions import IsAdminOrReadOnly

from .importer import KnowledgeImportError, import_knowledge_manifest
from .models import (
    AccessLevel,
    KnowledgeChunk,
    KnowledgeCollection,
    KnowledgeEmbedding,
    KnowledgeSource,
)
from .retrieval import KnowledgeAccessContext, KnowledgeRetriever
from .serializers import (
    KnowledgeChunkSerializer,
    KnowledgeCollectionSerializer,
    KnowledgeEmbeddingSerializer,
    KnowledgeSourceSerializer,
)


class KnowledgeCollectionViewSet(viewsets.ModelViewSet):
    queryset = KnowledgeCollection.objects.all()
    serializer_class = KnowledgeCollectionSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]


class KnowledgeSourceViewSet(viewsets.ModelViewSet):
    queryset = KnowledgeSource.objects.select_related(
        "collection", "owner", "case", "document_source"
    ).prefetch_related("allowed_users", "allowed_groups")
    serializer_class = KnowledgeSourceSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]


class KnowledgeChunkViewSet(viewsets.ModelViewSet):
    queryset = KnowledgeChunk.objects.select_related("source", "source__collection")
    serializer_class = KnowledgeChunkSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]


class KnowledgeEmbeddingViewSet(viewsets.ModelViewSet):
    queryset = KnowledgeEmbedding.objects.select_related(
        "chunk", "chunk__source", "chunk__source__collection"
    )
    serializer_class = KnowledgeEmbeddingSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]


class KnowledgeImportView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def post(self, request):
        manifest = (
            request.data.get("manifest", request.data)
            if isinstance(request.data, dict)
            else request.data
        )
        try:
            result = import_knowledge_manifest(manifest)
        except KnowledgeImportError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "collection": KnowledgeCollectionSerializer(result.collection).data,
                "source": KnowledgeSourceSerializer(result.source).data,
                "chunks_imported": result.chunks_imported,
                "embeddings_imported": result.embeddings_imported,
            },
            status=status.HTTP_201_CREATED,
        )


class PublicKnowledgeSearchView(APIView):
    """Anonymous public-only knowledge retrieval for agentic public chat."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        query = str(request.query_params.get("query") or "").strip()
        if not query:
            return Response(
                {"detail": "query is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        collection_name = str(request.query_params.get("collection") or "").strip()
        source_type = str(request.query_params.get("source_type") or "").strip()
        year = str(request.query_params.get("year") or "").strip()
        try:
            max_results = int(request.query_params.get("max_results") or 5)
        except (TypeError, ValueError):
            max_results = 5
        max_results = min(max(max_results, 1), 10)

        collections = KnowledgeCollection.objects.filter(
            access_level=AccessLevel.PUBLIC,
            is_active=True,
        )
        if collection_name:
            collections = collections.filter(name=collection_name)

        retrieval_query = " ".join(part for part in [query, year] if part).strip()
        results = KnowledgeRetriever().retrieve(
            query=retrieval_query,
            access_context=KnowledgeAccessContext.public_context(),
            collections=collections,
            max_results=max_results * 2 if source_type else max_results,
        )

        chunks = []
        for result in results:
            if source_type and result.chunk.source.source_type != source_type:
                continue
            evidence = result.as_public_evidence()
            evidence["metadata"] = _public_source_metadata(result.chunk.source)
            chunks.append(evidence)
            if len(chunks) >= max_results:
                break

        return Response(
            {
                "query": query,
                "year": year,
                "collection": collection_name,
                "source_type": source_type,
                "results": chunks,
            }
        )


class PublicKnowledgeSourceView(APIView):
    """Anonymous public-only metadata lookup for one knowledge source."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request, source_id):
        source = (
            KnowledgeSource.objects.select_related(
                "collection", "case", "document_source"
            )
            .filter(
                id=source_id,
                access_level=AccessLevel.PUBLIC,
                is_active=True,
                collection__access_level=AccessLevel.PUBLIC,
                collection__is_active=True,
            )
            .first()
        )
        if source is None:
            return Response(
                {"detail": "Public knowledge source not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(_public_source_payload(source))


def _public_source_payload(source: KnowledgeSource) -> dict:
    citation = source.metadata.get("public_citation", {})
    if not isinstance(citation, dict):
        citation = {}
    return {
        "source_id": str(source.id),
        "collection_id": str(source.collection_id),
        "collection_name": source.collection.name,
        "title": citation.get("title") or source.title,
        "source_type": source.source_type,
        "source_url": source.source_url or citation.get("url", ""),
        "public_citation": citation,
        "metadata": _public_source_metadata(source),
        "case_id": source.case_id,
        "document_source_id": source.document_source_id,
    }


def _public_source_metadata(source: KnowledgeSource) -> dict:
    return {
        key: value
        for key, value in source.metadata.items()
        if key
        in {
            "public_citation",
            "toc_pages",
            "toc_page_range",
            "toc_page_start",
            "toc_page_end",
            "toc_section_title",
            "toc_section_titles",
            "toc_pdf_page_offset",
            "answer_page_window",
            "pages",
            "page_range",
            "page_start",
            "page_end",
            "source_page_ranges",
        }
    }
