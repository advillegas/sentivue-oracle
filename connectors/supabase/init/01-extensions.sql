-- Run once at first boot of the Postgres volume.
create extension if not exists vector;      -- pgvector: embeddings from qwen3-embedding-4b
create extension if not exists pg_stat_statements;

-- timescaledb is optional (not all supabase/postgres builds ship it) — don't abort init.
do $$ begin
  create extension if not exists timescaledb;
exception when others then
  raise notice 'timescaledb unavailable — skipping (partitioned tables work fine without it)';
end $$;

-- Convention: research artifacts live in their own schema, market data in another.
create schema if not exists research;
create schema if not exists market;
