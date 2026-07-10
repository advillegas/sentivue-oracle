---
name: data-science
description: Data-science workflow for the offline quant stack - DuckDB/Polars/Parquet pipelines, profiling discipline, tidy project layout, notebook-to-module promotion. Use when ingesting, cleaning, transforming, or exploring datasets.
---

# Data Science (offline stack)

Environment: `uv run --project env python`. Core: DuckDB, Polars, pandas, PyArrow.
Data lake: `data/lake.duckdb` + partitioned Parquet under `data/parquet/`.

## Golden path

1. **Land raw immutably** — `data/raw/<source>/<yyyy-mm-dd>/…`, never edited, checksummed.
2. **Transform with DuckDB SQL or Polars lazy** — deterministic scripts under `pipelines/`,
   one artifact per script, idempotent (safe to re-run).
3. **Publish to the lake** — Parquet (zstd), hive-partitioned by natural keys
   (`year=/month=` or `symbol=`), registered as DuckDB views.
4. **Explore in notebooks, promote to modules** — a notebook older than a week is either
   deleted or its logic moved into a tested module. Notebooks import project code; they
   do not define it.

## Profiling discipline (before ANY modeling)

```python
import duckdb
con = duckdb.connect("data/lake.duckdb")
con.sql("SUMMARIZE SELECT * FROM prices").show()          # min/max/nulls/approx_unique per column
con.sql("""
  SELECT count(*) rows, count(DISTINCT symbol) symbols,
         min(date) d0, max(date) d1,
         sum(CASE WHEN adj_close IS NULL THEN 1 ELSE 0 END) null_px
  FROM prices""").show()
```

Always check and record in the ledger: row counts, date coverage vs. expectation, null
rates, duplicate keys, weekend/holiday rows, zero/negative prices, split anomalies
(daily |return| > 50% flags), timezone of timestamps (store UTC, convert at the edge).

## DuckDB idioms

- `CREATE OR REPLACE TABLE t AS SELECT …` for derived tables; views over Parquet for
  cheap re-reads: `CREATE VIEW prices AS SELECT * FROM read_parquet('data/parquet/prices/**')`.
- `QUALIFY row_number() OVER (PARTITION BY symbol, date ORDER BY ingested_at DESC) = 1`
  to dedupe by recency.
- `ASOF JOIN` for point-in-time joins (fundamentals to prices) — never a plain join on date.
- Window frames must be explicit: `ROWS BETWEEN 251 PRECEDING AND CURRENT ROW`.

## Polars idioms

- Lazy by default: `pl.scan_parquet(...).filter(...).group_by(...).agg(...).collect()`.
- `group_by_dynamic("date", every="1mo")` for calendar resampling;
  `.over("symbol")` for grouped window ops without a groupby-apply.
- Zero-copy to pandas only at the matplotlib/statsmodels boundary.

## Pitfalls that corrupt research

- pandas silent alignment on indexes — prefer explicit `merge(..., validate="1:1")`.
- `float32` accumulation over long sums (use float64 for money).
- Local-time daylight-saving duplicates/gaps in intraday data.
- Forward-filling across delistings (creates ghost liquidity) — join the universe table.
- Re-computing a "raw" field downstream instead of landing it once.

## Definition of done

Pipeline script + pytest with a synthetic fixture + registered lake artifact +
ledger entry (source, rows, coverage, caveats).
