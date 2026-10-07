<!-- To merge into README.md: a table row under "Schema Explorer, Pipeline Monitor, Cluster Monitor, Query Grid", plus the section below it. -->

| **Clean up** `cleanup_work_tables()` | Finds the work tables SAS jobs leave behind (`_dm…` Data Management, `_flw…` flow, `SASTMP…` / `_tmp…` temp tables) in every non-system database: rows, size, created, created by, age, last use and dependent views. Select tables, confirm, and they're dropped one at a time, with a dry-run mode and a log. The **🧹 Clean up** button in the Schema Explorer's header. |

**Clean up** lives in the Schema Explorer (the **🧹 Clean up** button in its
header, or ask Claude to "clean up the SAS work tables"). It scans all
databases except the system ones (`information_schema`, `memsql`, `cluster`,
`sys`, `mysql`, `performance_schema`) in two rounds of queries, whatever the
number of tables:

- **Name patterns**: one per line, a pattern and a label, e.g. `_dm*  SAS DM`.
  `*` and `?` are wildcards, case-insensitive. The defaults are `_dm*`,
  `_flw*`, `SASTMP*` and `_tmp*`. Optionally it also offers every table not
  created or altered for N days. The settings are kept in
  `~/.singlestore-mcp/cleanup.json`.
- **Last used** is the most recent query in the query history that names the
  table (see Query History above; only queries above its duration threshold
  are recorded, so "never seen" is no guarantee). Tables used or created in
  the last N days (default 7), and tables that a view depends on, are marked
  **risky**.
- **Dependent views** block a table's drop unless you tick them in the
  confirmation; they are then dropped first.
- **Dropping**: select tables (or *Select all* / *Select not risky*), then the
  confirmation lists the exact statements and the total size. Each object is
  dropped with its own `DROP TABLE IF EXISTS \`db\`.\`table\`` and gets its own
  result (dropped / error). **Dry run** is on until you turn it off: it only
  shows the statements. The server checks everything again before dropping.
- Every drop is logged (time, cluster user, Windows user, connection, table,
  rows, size, result) to `~/.singlestore-mcp/cleanup-log.jsonl`; the panel
  shows the recent entries.
