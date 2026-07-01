"""NGM lakehouse service layer (DuckDB / Iceberg over Cloudflare R2) — DORMANT.

**Status: dormant, Iceberg-ready seam — NOT the shipped storage layer.** The
platform data plane is **Postgres-SoR, lakehouse-lite**: Postgres (3 DBs) is the
system of record for served data, R2 is the immutable archive, OpenSearch serves
search. There is deliberately **no live Iceberg/DuckDB/Lakekeeper lake right now**
(see ``docs/data-plane-design.md`` §6 and ``docs/ARCHITECTURE.md`` §5).

This package is kept — fully importable, DDL/secret builders real and unit-tested —
so a lake can be stood up cheaply *if* a real recurring cross-domain analytical
query ever earns it. Until then it is not wired into any serving path.

**Superseded framing:** the medallion docstrings below describe silver as the
source with the Postgres tables *derived from* it. That direction is reversed:
Postgres + the R2 archive are the source; any future silver is derived *from*
them, never the other way. Read the medallion/schema modules as a **blueprint**
(e.g. the ``schema.py`` TableSpecs → future ``ngm``-DB Django models for
procurement/budget/audit/assets/gazette), not as the current storage contract.

Everything imports cleanly with no native/network dependency; the live-R2 /
live-catalog boundary (``engine.connect`` ATTACH, the ``medallion``
bronze/silver/gold bodies) is stubbed with ``NotImplementedError`` + a precise TODO.
"""
