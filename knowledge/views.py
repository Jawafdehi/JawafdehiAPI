from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from caseworker.permissions import IsAdminOrReadOnly

from .importer import KnowledgeImportError, import_knowledge_manifest
from .models import (
    KnowledgeChunk,
    KnowledgeCollection,
    KnowledgeEmbedding,
    KnowledgeSource,
)
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
            },
            status=status.HTTP_201_CREATED,
        )
