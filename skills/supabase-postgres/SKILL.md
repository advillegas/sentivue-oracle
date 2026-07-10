---
name: supabase-postgres
description: Self-hosted Supabase/Postgres on this machine - schema design for research and market data, pgvector embeddings with the local endpoint, migrations, performance. Use when designing schemas, writing SQL, or wiring storage.
---

# Supabase / Postgres (self-hosted, offline)

Local stack (`make supabase-up`): Postgres 15 + pgvector at `127.0.0.1:54322`
(user `postgres`, password in `connectors/supabase/.env`), PostgREST at `:54321`,
Studio UI at `:54323`. Everything binds loopback; there is no cloud project.

## Division of labor (important)

- **DuckDB/Parquet lake** — heavy columnar analytics, backtest inputs, bulk scans.
- **Postgres** — mutable state: experiment registry, run metadata, live positions,
  document embeddings, anything multiple processes read/write concurrently.
Don't put 10M-row price panels in Postgres; don't put concurrent mutable state in DuckDB.

## Schemas (convention from init/01-extensions.sql)

- `market` — reference/master data: instruments, calendars, corporate actions.
- `research` — experiments, runs, metrics, doc_chunks (embeddings).
- `public` — kept empty; PostgREST exposes only what you explicitly grant.

```sql
create table research.experiment (
  id          bigint generated always as identity primary key,
  name        text not null unique,
  git_sha     text not null,
  spec        jsonb not null,                  -- full config, queryable
  created_at  timestamptz not null default now()
);
create table research.run (
  id            bigint generated always as identity primary key,
  experiment_id bigint not null references research.experiment(id),
  seed          int not null,
  metrics       jsonb not null,
  artifact_path text,
  finished_at   timestamptz,
  unique (experiment_id, seed)                 -- idempotent re-runs
);
```

## pgvector with the local embedding endpoint

Embeddings come from `http://127.0.0.1:9099/v1/embeddings`, model `qwen3-embedding-4b`
(2560 dims). Loopback HTTP is allowed even under `make harden`.

```sql
create table if not exists research.doc_chunks (
  id        bigint generated always as identity primary key,
  doc_path  text not null,
  chunk_no  int  not null,
  content   text not null,
  embedding vector(2560) not null,
  unique (doc_path, chunk_no)                  -- idempotent indexer
);
create index on research.doc_chunks using hnsw (embedding vector_cosine_ops);
-- query: order by embedding <=> $1 limit 10;  (<=> is cosine distance with this opclass)
```

Python side: `psycopg` + `pgvector` adapter or pass `'[0.1,0.2,...]'::vector` literals;
normalize chunks to ~512 tokens with overlap; store the source path for citations.

## Time-series in Postgres (when it must live here)

Partition by range on a `timestamptz` column (monthly), BRIN index on time, composite
PK `(symbol_id, ts)`. `COPY` for bulk loads (psycopg `copy` API — 100x inserts).
For real panel analytics, export to Parquet and query with DuckDB instead.

## Migrations & access

- Plain numbered SQL files in `connectors/supabase/migrations/NNN_description.sql`,
  applied with psql in order; record applied versions in a `_migrations` table.
  No ORM migration magic — SQL is the artifact.
- The `postgres-mcp` connector runs in restricted mode (read-mostly). Write paths go
  through reviewed scripts, not ad-hoc agent SQL.
- `explain (analyze, buffers)` before adding any index; one HNSW index per embedding
  column is plenty at this scale.
