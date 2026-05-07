from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.db.models import QuerySet

from .models import (
    AccessLevel,
    KnowledgeChunk,
    KnowledgeCollection,
    KnowledgeEmbedding,
    KnowledgeSource,
)

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"[\w\u0900-\u097F]+", flags=re.UNICODE)
RRF_K = 60


@dataclass(frozen=True)
class KnowledgeAccessContext:
    """Who is retrieving knowledge."""

    user: User | AnonymousUser | None = None
    public: bool = False

    @classmethod
    def public_context(cls) -> "KnowledgeAccessContext":
        return cls(user=None, public=True)


@dataclass(frozen=True)
class RetrievedKnowledgeChunk:
    chunk: KnowledgeChunk
    score: float
    retrieval_mode: str = "lexical"
    lexical_score: float = 0.0
    vector_score: float = 0.0

    def as_internal_evidence(self) -> dict:
        """Internal evidence shape. Do not use this for anonymous public chat."""
        source = self.chunk.source
        collection = source.collection
        return {
            "chunk_id": self.chunk.id,
            "document_id": source.id,
            "collection_id": collection.id,
            "collection_name": collection.name,
            "source_id": source.id,
            "source_title": source.title,
            "source_type": source.source_type,
            "source_url": source.source_url,
            "storage_path": source.storage_path,
            "page_start": self.chunk.page_start,
            "page_end": self.chunk.page_end,
            "section_title": self.chunk.section_title,
            "table_title": self.chunk.table_title,
            "text": self.chunk.text,
            "score": self.score,
            "retrieval_mode": self.retrieval_mode,
            "metadata": {
                "source": source.metadata,
                "chunk": self.chunk.metadata,
            },
        }

    def as_public_evidence(self) -> dict:
        """Strict public allowlist for anonymous chat prompts and citations."""
        source = self.chunk.source
        collection = source.collection
        citation = _public_citation(source.metadata)
        return {
            "chunk_id": str(self.chunk.id),
            "document_id": str(source.id),
            "collection_id": str(collection.id),
            "collection_name": collection.name,
            "source_id": str(source.id),
            "source_title": citation.get("title") or source.title,
            "source_type": source.source_type,
            "source_url": source.source_url or citation.get("url", ""),
            "public_citation": citation,
            "page_start": self.chunk.page_start,
            "page_end": self.chunk.page_end,
            "section_title": self.chunk.section_title,
            "table_title": self.chunk.table_title,
            "text": self.chunk.text,
            "score": self.score,
            "retrieval_mode": self.retrieval_mode,
        }


class KnowledgeQueryEmbedder:
    """Optional query embedder for production vector/hybrid retrieval."""

    def __init__(
        self,
        *,
        provider: str = "",
        model: str = "",
        api_key: str = "",
        base_url: str = "",
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    @classmethod
    def from_settings(cls) -> "KnowledgeQueryEmbedder":
        return cls(
            provider=getattr(settings, "KNOWLEDGE_RAG_EMBEDDING_PROVIDER", ""),
            model=getattr(settings, "KNOWLEDGE_RAG_EMBEDDING_MODEL", ""),
            api_key=getattr(settings, "KNOWLEDGE_RAG_EMBEDDING_API_KEY", ""),
            base_url=getattr(settings, "KNOWLEDGE_RAG_EMBEDDING_BASE_URL", ""),
        )

    def embed_query(self, query: str) -> list[float] | None:
        embeddings = self._openai_embeddings_or_none()
        if embeddings is None:
            return None
        return list(embeddings.embed_query(query))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        embeddings = self._openai_embeddings_or_none()
        if embeddings is None:
            return []
        return [list(row) for row in embeddings.embed_documents(texts)]

    def _openai_embeddings_or_none(self):
        if not self.provider:
            return None
        if self.provider != "openai":
            raise ValueError(
                f"Unsupported knowledge embedding provider: {self.provider}"
            )
        if not self.model:
            raise ValueError("KNOWLEDGE_RAG_EMBEDDING_MODEL is required.")

        from langchain_openai import OpenAIEmbeddings

        kwargs = {"model": self.model}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAIEmbeddings(**kwargs)


class KnowledgeRetriever:
    """Hybrid knowledge retriever with lexical fallback for dev/test."""

    def __init__(self, *, query_embedder=None) -> None:
        self.query_embedder = query_embedder or KnowledgeQueryEmbedder.from_settings()

    def retrieve(
        self,
        *,
        query: str,
        access_context: KnowledgeAccessContext,
        collections: Iterable[KnowledgeCollection] | QuerySet[KnowledgeCollection] = (),
        max_results: int = 5,
    ) -> list[RetrievedKnowledgeChunk]:
        max_results = max(1, max_results)
        query_tokens = _tokens(query)

        chunks = self._base_queryset(collections)
        if access_context.public:
            chunks = self._public_queryset(chunks)
        else:
            chunks = self._user_queryset(chunks, access_context.user)

        lexical_ranked = self._lexical_rank(query_tokens, chunks)
        vector_ranked = self._vector_rank(query, chunks)
        if vector_ranked:
            fused = self._fuse_rankings(lexical_ranked, vector_ranked)
            return self._select_diverse_chunks(fused, max_results)
        return self._select_diverse_chunks(lexical_ranked, max_results)

    def _lexical_rank(
        self, query_tokens: set[str], chunks: QuerySet[KnowledgeChunk]
    ) -> list[RetrievedKnowledgeChunk]:
        if not query_tokens:
            return []
        if connection.vendor == "postgresql":
            return self._postgres_lexical_rank(query_tokens, chunks)

        ranked: list[RetrievedKnowledgeChunk] = []
        candidate_limit = max(
            1, getattr(settings, "KNOWLEDGE_RAG_LEXICAL_CANDIDATES", 1000)
        )
        for chunk in chunks[:candidate_limit]:
            score = _score(query_tokens, chunk)
            if score >= self._min_lexical_score():
                ranked.append(
                    RetrievedKnowledgeChunk(
                        chunk=chunk,
                        score=score,
                        retrieval_mode="lexical",
                        lexical_score=score,
                    )
                )

        ranked.sort(
            key=lambda item: (
                item.score,
                item.chunk.source.collection.name,
                -item.chunk.chunk_index,
            ),
            reverse=True,
        )
        return ranked

    def _postgres_lexical_rank(
        self, query_tokens: set[str], chunks: QuerySet[KnowledgeChunk]
    ) -> list[RetrievedKnowledgeChunk]:
        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

        query_text = " ".join(sorted(query_tokens))
        search_query = SearchQuery(query_text, search_type="websearch")
        search_vector = (
            SearchVector("text", weight="A")
            + SearchVector("section_title", weight="B")
            + SearchVector("table_title", weight="B")
            + SearchVector("source__title", weight="C")
            + SearchVector("source__collection__display_name", weight="D")
        )
        ranked_qs = (
            chunks.annotate(search_rank=SearchRank(search_vector, search_query))
            .filter(search_rank__gte=self._min_lexical_score())
            .order_by("-search_rank", "source_id", "chunk_index")
        )
        results = []
        for chunk in ranked_qs[
            : getattr(settings, "KNOWLEDGE_RAG_LEXICAL_CANDIDATES", 1000)
        ]:
            score = float(getattr(chunk, "search_rank", 0.0) or 0.0)
            results.append(
                RetrievedKnowledgeChunk(
                    chunk=chunk,
                    score=score,
                    retrieval_mode="lexical",
                    lexical_score=score,
                )
            )
        return results

    def _vector_rank(
        self, query: str, chunks: QuerySet[KnowledgeChunk]
    ) -> list[RetrievedKnowledgeChunk]:
        try:
            query_embedding = self.query_embedder.embed_query(query)
        except Exception as exc:  # noqa: BLE001 - lexical fallback must stay available.
            logger.warning(
                "knowledge_vector_retrieval_unavailable error_type=%s",
                type(exc).__name__,
                extra={"error_type": type(exc).__name__},
            )
            return []

        if not query_embedding:
            return []

        if connection.vendor == "postgresql":
            return self._pgvector_rank(chunks, query_embedding)
        return self._json_vector_rank(chunks, query_embedding)

    def _pgvector_rank(
        self, chunks: QuerySet[KnowledgeChunk], query_embedding: list[float]
    ) -> list[RetrievedKnowledgeChunk]:
        from pgvector.django import CosineDistance

        embedding_qs = self._embedding_queryset(chunks).filter(vector__isnull=False)
        ranked = (
            embedding_qs.annotate(distance=CosineDistance("vector", query_embedding))
            .order_by("distance")
            .select_related("chunk", "chunk__source", "chunk__source__collection")
        )

        results: list[RetrievedKnowledgeChunk] = []
        for embedding in ranked[: self._vector_candidate_limit()]:
            distance = getattr(embedding, "distance", None)
            if distance is None:
                continue
            score = max(0.0, 1.0 - float(distance))
            if score < self._min_vector_score():
                continue
            results.append(
                RetrievedKnowledgeChunk(
                    chunk=embedding.chunk,
                    score=score,
                    retrieval_mode="vector",
                    vector_score=score,
                )
            )
        return results

    def _json_vector_rank(
        self, chunks: QuerySet[KnowledgeChunk], query_embedding: list[float]
    ) -> list[RetrievedKnowledgeChunk]:
        results: list[RetrievedKnowledgeChunk] = []
        for embedding in self._embedding_queryset(chunks).exclude(embedding=[]):
            score = _cosine_similarity(query_embedding, embedding.embedding)
            if score < self._min_vector_score():
                continue
            results.append(
                RetrievedKnowledgeChunk(
                    chunk=embedding.chunk,
                    score=score,
                    retrieval_mode="vector",
                    vector_score=score,
                )
            )

        results.sort(key=lambda item: item.score, reverse=True)
        return results[: self._vector_candidate_limit()]

    def _embedding_queryset(
        self, chunks: QuerySet[KnowledgeChunk]
    ) -> QuerySet[KnowledgeEmbedding]:
        embeddings = KnowledgeEmbedding.objects.filter(chunk__in=chunks).select_related(
            "chunk",
            "chunk__source",
            "chunk__source__collection",
        )
        embedding_model = getattr(settings, "KNOWLEDGE_RAG_EMBEDDING_MODEL", "")
        if embedding_model:
            embeddings = embeddings.filter(embedding_model=embedding_model)
        return embeddings

    @staticmethod
    def _vector_candidate_limit() -> int:
        return max(1, getattr(settings, "KNOWLEDGE_RAG_VECTOR_CANDIDATES", 50))

    @staticmethod
    def _min_lexical_score() -> float:
        return max(
            0.0, float(getattr(settings, "KNOWLEDGE_RAG_MIN_LEXICAL_SCORE", 0.1))
        )

    @staticmethod
    def _min_vector_score() -> float:
        return max(0.0, float(getattr(settings, "KNOWLEDGE_RAG_MIN_VECTOR_SCORE", 0.2)))

    @staticmethod
    def _select_diverse_chunks(
        ranked: list[RetrievedKnowledgeChunk], max_results: int
    ) -> list[RetrievedKnowledgeChunk]:
        if len(ranked) <= max_results:
            return ranked

        selected: list[RetrievedKnowledgeChunk] = []
        source_counts: dict[int, int] = {}
        lambda_value = min(
            1.0, max(0.0, float(getattr(settings, "KNOWLEDGE_RAG_MMR_LAMBDA", 0.75)))
        )
        for item in ranked:
            source_count = source_counts.get(item.chunk.source_id, 0)
            diversity_penalty = (1.0 - lambda_value) * source_count
            adjusted_score = lambda_value * item.score - diversity_penalty
            if source_count == 0 or adjusted_score > 0:
                selected.append(item)
                source_counts[item.chunk.source_id] = source_count + 1
            if len(selected) >= max_results:
                return selected

        for item in ranked:
            if item not in selected:
                selected.append(item)
            if len(selected) >= max_results:
                break
        return selected

    @staticmethod
    def _fuse_rankings(
        lexical_ranked: list[RetrievedKnowledgeChunk],
        vector_ranked: list[RetrievedKnowledgeChunk],
    ) -> list[RetrievedKnowledgeChunk]:
        combined: dict[int, dict] = {}

        def add_result(item: RetrievedKnowledgeChunk, rank: int, source: str) -> None:
            chunk_id = item.chunk.id
            entry = combined.setdefault(
                chunk_id,
                {
                    "chunk": item.chunk,
                    "score": 0.0,
                    "lexical_score": 0.0,
                    "vector_score": 0.0,
                },
            )
            entry["score"] += 1.0 / (RRF_K + rank)
            if source == "lexical":
                entry["lexical_score"] = max(entry["lexical_score"], item.score)
            else:
                entry["vector_score"] = max(entry["vector_score"], item.score)

        for rank, item in enumerate(lexical_ranked, start=1):
            add_result(item, rank, "lexical")
        for rank, item in enumerate(vector_ranked, start=1):
            add_result(item, rank, "vector")

        fused = [
            RetrievedKnowledgeChunk(
                chunk=entry["chunk"],
                score=entry["score"],
                retrieval_mode="hybrid",
                lexical_score=entry["lexical_score"],
                vector_score=entry["vector_score"],
            )
            for entry in combined.values()
        ]
        fused.sort(key=lambda item: item.score, reverse=True)
        return fused

    def _base_queryset(
        self,
        collections: Iterable[KnowledgeCollection] | QuerySet[KnowledgeCollection],
    ) -> QuerySet[KnowledgeChunk]:
        chunks = KnowledgeChunk.objects.select_related("source", "source__collection")
        collection_ids = _collection_ids(collections)
        if collection_ids is not None:
            if not collection_ids:
                return chunks.none()
            chunks = chunks.filter(source__collection_id__in=collection_ids)
        return chunks.filter(source__is_active=True, source__collection__is_active=True)

    def _public_queryset(
        self, chunks: QuerySet[KnowledgeChunk]
    ) -> QuerySet[KnowledgeChunk]:
        return chunks.filter(
            source__access_level=AccessLevel.PUBLIC,
            source__collection__access_level=AccessLevel.PUBLIC,
        )

    def _user_queryset(
        self, chunks: QuerySet[KnowledgeChunk], user: User | AnonymousUser | None
    ) -> QuerySet[KnowledgeChunk]:
        if user is None or not getattr(user, "is_authenticated", False):
            return chunks.none()

        if _is_admin_or_moderator(user):
            return chunks

        allowed_source_ids = [
            source.id
            for source in KnowledgeSource.objects.select_related(
                "case", "document_source"
            ).prefetch_related("allowed_users", "allowed_groups")
            if _can_user_access_source(user, source)
        ]
        return chunks.filter(source_id__in=allowed_source_ids)


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in TOKEN_RE.findall(text or "")
        if len(token) > 1 or token.isdigit()
    }


def _collection_ids(
    collections: Iterable[KnowledgeCollection] | QuerySet[KnowledgeCollection],
) -> list[int] | None:
    if isinstance(collections, QuerySet):
        return list(collections.values_list("id", flat=True))
    if collections is None:
        return None
    return [collection.id for collection in collections]


def _public_citation(metadata: Any) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    citation = metadata.get("public_citation")
    if not isinstance(citation, dict):
        return {}
    allowed = {}
    for key in ["title", "url", "identifier", "publisher", "publication_date"]:
        value = citation.get(key)
        if isinstance(value, str) and value.strip():
            allowed[key] = value.strip()
    return allowed


def _score(query_tokens: set[str], chunk: KnowledgeChunk) -> float:
    chunk_tokens = _tokens(
        " ".join(
            [
                chunk.source.title,
                chunk.source.collection.display_name,
                chunk.section_title,
                chunk.table_title,
                chunk.text,
            ]
        )
    )
    overlap = query_tokens & chunk_tokens
    if not overlap:
        return 0.0

    exact_bonus = 0.0
    lowered_text = chunk.text.lower()
    for token in query_tokens:
        if token in lowered_text:
            exact_bonus += 0.1

    return float(len(overlap)) + exact_bonus


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _is_admin_or_moderator(user: User) -> bool:
    return (
        user.is_superuser
        or user.groups.filter(name__in=["Admin", "Moderator"]).exists()
    )


def _can_user_access_source(user: User, source: KnowledgeSource) -> bool:
    if (
        source.access_level == AccessLevel.PUBLIC
        and source.collection.access_level == AccessLevel.PUBLIC
    ):
        return True
    if source.owner_id == user.id:
        return True
    if source.allowed_users.filter(id=user.id).exists():
        return True
    if source.allowed_groups.filter(user=user).exists():
        return True
    if source.case_id and source.case.contributors.filter(id=user.id).exists():
        return True
    if (
        source.document_source_id
        and source.document_source.contributors.filter(id=user.id).exists()
    ):
        return True
    if source.document_source_id and _document_source_linked_to_user_case(
        user, source.document_source.source_id
    ):
        return True
    return False


def _document_source_linked_to_user_case(user: User, source_id: str) -> bool:
    from cases.models import Case

    for case in Case.objects.filter(contributors=user).only("evidence"):
        for evidence_item in case.evidence or []:
            if (
                isinstance(evidence_item, dict)
                and evidence_item.get("source_id") == source_id
            ):
                return True
    return False
