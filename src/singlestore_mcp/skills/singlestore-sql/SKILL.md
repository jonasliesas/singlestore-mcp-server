---
name: singlestore-sql
description: Practical rules for writing SingleStore SQL (procedures, pipelines, DDL, large-table queries, information_schema) learned on this cluster. Use whenever writing, fixing or explaining SQL for SingleStore.
---

# SingleStore SQL: key learnings

SingleStore 9.0 is MySQL-wire-compatible, but not MySQL. Prefer these rules
over MySQL intuition. When unsure about a SingleStore-specific feature, say
so and check docs.singlestore.com rather than guessing.

## Names and data
- Database and table names are **case-sensitive**: `CARS` and `cars` can be
  two different tables. Check the exact name (`SHOW TABLES`,
  information_schema) before using it.
- String data may carry stray whitespace (e.g. `Model` values in
  `SASDP.cars_big_240` start with a space). `TRIM()` before grouping or joining.
- Big demo tables can be replicated copies: `cars_big_240` is ~240M rows of the
  428-row CARS data. A full aggregate scan takes several seconds.

## Statements that differ from MySQL
- `DROP TABLE a, b` is not supported: one table per `DROP TABLE` (error 1706).
- `GROUP_CONCAT(DISTINCT x ORDER BY x)` is not supported (1706). Use
  `GROUP_CONCAT(DISTINCT x SEPARATOR '/')` (unordered) or aggregate in a subquery.
- information_schema views (`MV_*`, `PIPELINES_*`, `TABLES` ...) can't be
  combined with user tables in one query (error 1749). Run them as separate
  queries.
- An explicit `LIMIT` overrides the session `sql_select_limit`.
- Columnstore `SORT KEY` and `SHARD KEY` can't be changed with `ALTER TABLE`.
  Create a new table with the keys you want and copy the data.
- For hundreds of millions of rows, avoid one huge `INSERT … SELECT` / CTAS.
  Use a pipeline, or copy in chunks.

## Procedures and functions
```sql
CREATE OR REPLACE PROCEDURE p(n INT) AS
DECLARE
  total INT = 0;
BEGIN
  WHILE total < n LOOP
    total = total + 1;
  END LOOP;
  ECHO SELECT total AS total;   -- returns a result set to the caller
END;
```
- Variables are assigned with `=` (no `SET`), and declared in the `DECLARE`
  section between `AS` and `BEGIN`.
- `ECHO SELECT …` returns rows from a procedure; `CALL p(5);` runs it.
- No `DELIMITER` is needed through client APIs. The whole `CREATE … END` is one
  statement. (The SQL Editor app also understands `DELIMITER //` scripts.)
- Table-valued function: `CREATE FUNCTION f() RETURNS TABLE AS RETURN SELECT …;`

## Pipelines
```sql
CREATE PIPELINE my_pipe AS
LOAD DATA S3 'bucket/path/*.csv'
CONFIG '{"region":"us-east-1"}'       -- public bucket: no CREDENTIALS needed
INTO TABLE t
FIELDS TERMINATED BY ','
(col1, col2, @raw3)
SET col3 = NULLIF(@raw3, ''), src_file = pipeline_source_file();
START PIPELINE my_pipe;
```
- `pipeline_source_file()` records which file each row came from.
- Stop a pipeline before `TEST PIPELINE`. A failed test is still recorded in
  the batch history and the error log.
- Progress: `information_schema.PIPELINES_FILES` (file states),
  `PIPELINES_BATCHES_SUMMARY`, `PIPELINES_ERRORS`; Kafka lag in `PIPELINES_CURSORS`.
- To rerun one pipeline's data when several pipelines share a table, `DELETE`
  that pipeline's rows (e.g. by date range) instead of `TRUNCATE`, then
  `ALTER PIPELINE … SET OFFSETS EARLIEST` (or re-create it) and start it.

## Exporting
- `SELECT … INTO KAFKA 'broker:9092/topic' FORMAT JSON`,
  `SELECT … INTO S3 'bucket/prefix' CONFIG … CREDENTIALS …` and
  `INTO OUTFILE` export natively. Each runs once, as a bulk export.

## Monitoring (information_schema)
- Per node: `MV_NODES`, `MV_SYSINFO_CPU`, `MV_SYSINFO_MEM`, `MV_SYSINFO_DISK`.
  CPU counters are cumulative, so take two samples and divide by elapsed time
  and the node's core limit.
- Running queries: `MV_PROCESSLIST`. Table sizes: `TABLE_STATISTICS`,
  `COLUMNAR_SEGMENTS`.

## Writing answers
- Test the SQL you suggest with a cheap read-only query when you can
  (`LIMIT`, aggregates, `EXPLAIN`). Avoid repeated full scans of very large tables.
- Give every statement ready to run, in uppercase keywords, one per ```sql block.
