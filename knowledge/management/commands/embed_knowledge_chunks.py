from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from knowledge.models import KnowledgeChunk, KnowledgeEmbedding
from knowledge.retrieval import KnowledgeQueryEmbedder


class Command(BaseCommand):
    help = "Generate embeddings for knowledge chunks missing the configured model."

    def add_arguments(self, parser):
        parser.add_argument(
            "--collection",
            action="append",
            dest="collections",
            help="Collection name to embed. Can be passed multiple times.",
        )
        parser.add_argument(
            "--source-id",
            type=int,
            help="Only embed chunks for this KnowledgeSource id.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=500,
            help="Maximum chunks to embed in this run.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=32,
            help="Embedding request batch size.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate embeddings even when the model already exists.",
        )

    def handle(self, *args, **options):
        embedding_model = getattr(settings, "KNOWLEDGE_RAG_EMBEDDING_MODEL", "")
        if not embedding_model:
            raise CommandError("KNOWLEDGE_RAG_EMBEDDING_MODEL is required.")

        limit = max(1, options["limit"])
        batch_size = max(1, options["batch_size"])
        chunks = self._queryset(options)
        if not options["force"]:
            chunks = chunks.exclude(embeddings__embedding_model=embedding_model)
        chunks = list(chunks[:limit])

        if not chunks:
            self.stdout.write("No knowledge chunks need embeddings.")
            return

        embedder = KnowledgeQueryEmbedder.from_settings()
        embedded_count = 0
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = embedder.embed_documents([chunk.text for chunk in batch])
            if len(vectors) != len(batch):
                raise CommandError(
                    "Embedding provider returned an unexpected batch size."
                )

            for chunk, vector in zip(batch, vectors, strict=True):
                if not vector:
                    raise CommandError(
                        f"Embedding provider returned an empty vector for chunk {chunk.id}."
                    )
                KnowledgeEmbedding.objects.update_or_create(
                    chunk=chunk,
                    embedding_model=embedding_model,
                    defaults={
                        "embedding": vector,
                        "vector": vector,
                        "dimensions": len(vector),
                    },
                )
                embedded_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Embedded {embedded_count} knowledge chunks with {embedding_model}."
            )
        )

    @staticmethod
    def _queryset(options):
        chunks = KnowledgeChunk.objects.select_related("source", "source__collection")
        if options.get("collections"):
            chunks = chunks.filter(source__collection__name__in=options["collections"])
        if options.get("source_id"):
            chunks = chunks.filter(source_id=options["source_id"])
        return chunks.order_by("source_id", "chunk_index")
