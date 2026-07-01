"""NGM lakehouse service layer (DuckDB / Iceberg over Cloudflare R2).

A plain service module — deliberately NOT Django models. The court relational
tables (``courts.models``) are the Postgres projection; this package
is the medallion (bronze/silver/gold) substrate over object storage, queried
through DuckDB + an Iceberg REST catalog rather than the ORM.

Ported from the FastAPI ``ngm.lakehouse`` package. Everything imports cleanly
with no native/network dependency; the live-R2 / live-catalog boundary
(``engine.connect`` ATTACH, the ``medallion`` bronze/silver/gold bodies) is
stubbed with ``NotImplementedError`` + a precise TODO, exactly as upstream.
"""
