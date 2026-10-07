# README additions: monitoring (Alerts, Trends, before/after check, Source column)

Merge notes for the main README (`README.md` was not edited on this branch):

- Add a row for **Alerts** to the app table under
  "Schema Explorer, Pipeline Monitor, Cluster Monitor, Query Grid", and extend
  the **Query History** row (text below).
- Add the sections "Alerts", "Trends" and "Before/after check" after the
  Advisor paragraphs of the Query History text.
- Mention `alerts` among the workspace views / `--view` choices wherever the
  README lists them (`sql_editor(view=…)`, `workspace_app --view`,
  `make_shortcut.py --view`).

---

## Table rows

| App (tool) | What it shows and does |
|---|---|
| **Query History** `query_history(min_seconds=1, hours?, tab?)` | Every finished query the cluster traced: when, how long, user, database, rows, success / error, and a probable **Source** (SAS CAS, SAS in-database). Filter on runtime (1 s and up), period, user, database, status, type, source and text; sort by time, duration or rows. Select a query for its full SQL and **Get tuning recommendations**. Tabs **Queries**, **Trends** and **Advisor**. Its own **History** item in the workspace rail. |
| **Alerts** `alerts()` | Alerts the server raises while it watches the cluster in the background: queries running too long, slow or failed queries, failed pipeline batches and pipeline errors, node memory and disk over a threshold, nodes not online. Edit the rules, acknowledge, clear. Its own **Alerts** item in the workspace rail, with a red badge counting the unacknowledged alerts. |

## Source column

The Queries tab guesses where a statement came from, from its SQL:

- **SAS CAS**: contains `binary_serialization` or `PARALLELISM_LEVEL="SEGMENT"`
  (SAS SpeedyStore / CAS loading or pushing down through the cluster);
- **SAS in-database**: reads or writes tables named `_dm…`, `_flw…` or
  `SASTMP…` (SAS work tables created in the database);
- empty otherwise.

It's a heuristic, shown as "probably" in the detail panel, and a filter
(**Source**: All / SAS CAS / SAS in-database / Other).

## Trends

The cluster's query history is a ring buffer: older runs drop out. The server
keeps a summary of every traced run in a local SQLite file,
`~/.singlestore-mcp/query_history.db`: when it finished, how long it ran, user,
database, success / error code, type, rows, and its **query shape** (the SQL
with literals replaced, identified by its first 400 characters) with a short
sample. Nothing else is copied. It's updated whenever the history list loads
and on every Alerts check, and only reads events newer than the last copy, so
it's cheap. Each row records which cluster (host:port) it came from, and the
Trends tab only shows the active connection's cluster.

The **Trends** tab shows, per day (7 days, 30 days, 90 days, 1 year) or per
hour for the last 48 hours:

- the number of queries, the total runtime, failures, and the p95 duration
  (bars and a line, hover for the values);
- **Slower than before**: query shapes whose median duration in the last 7
  days is at least 1.5× and 1 s more than in the 7 days before, with the run
  counts of both weeks;
- **New heavy queries**: shapes first seen in the last 7 days with more than a
  minute of runtime in total.

Click a shape to see its runs in the Queries tab (as long as the cluster's
history still has them). Only queries the cluster traced are counted, i.e.
those over the event trace's `Duration_threshold_ms` (1 s in the setup above).

## Before/after check

When the Advisor suggests a new **SORT KEY**, **SHARD KEY** or a **REFERENCE**
table, it builds a copy of the table (e.g. `cars_big_240_sorted`). Once you've
built it, **Compare with new table** checks whether it actually helps:

1. Accept or change the new table's name and press **Find queries**. The app
   checks that the table exists (otherwise it says to build it first), then
   picks the heaviest read-only query shapes (`SELECT` / `WITH` only; never
   `INSERT`, `UPDATE`, `DELETE`, DDL or `SELECT … INTO`) from the history that
   read the table, and shows each one with the table name replaced (qualified
   and unqualified, with or without backticks; string literals, comments and
   other tables' columns are left alone). Nothing runs yet.
2. Pick the queries, the number of runs per table (default 3) and the timeout
   per query (default 120 s), and press **Run comparison**. A confirmation
   says exactly what will run and roughly how long it takes.
3. Each query runs as `SELECT COUNT(*) AS n FROM (<query>) AS _q`, so no rows
   are streamed to the client, alternating old / new table. The result per
   query: the old and new median (server time: wall time minus one network
   round trip), the speed-up, and whether both tables returned the same number
   of rows. A query over the timeout is cancelled with `KILL QUERY` (only after
   checking that its connection still runs this comparison's statement).
   **Stop** ends the comparison early.

Because of the `COUNT(*)` wrapper, the database may skip columns a query only
returns: the check times filtering, joining and grouping (what keys change),
not producing the output. Differences under about 10 % are noise. The
comparison's own statements are tagged and never appear in the history.

## Alerts

While the server runs (inside Claude, or the desktop workspace), a background
thread checks the cluster every 60 seconds (configurable, minimum 15). Each
check runs a few read-only `information_schema` queries in parallel, takes
about a second, and is tagged so it never shows up
in the history, the Cluster Monitor or the alerts themselves. A check that
fails never affects the server; if every check fails, a "can't reach the
cluster" alert says so.

| Rule (default) | Source | Alert |
|---|---|---|
| Query running longer than **60 s** | `MV_PROCESSLIST` (internal `distributed` connections excluded) | one alert per running query, active until it finishes |
| Finished query slower than **300 s** | new `MV_TRACE_EVENTS` since the last check | one alert per query shape, with a count |
| Failed queries | new `MV_TRACE_EVENTS` since the last check | one per error code and query shape, with a count |
| Pipelines | `PIPELINES_BATCHES_SUMMARY` (failed batches), `PIPELINES_ERRORS` (new errors), `PIPELINES` (state Error) | per pipeline (and error code) |
| Node memory above **85 %** of `max_memory` | `MV_SYSINFO_MEM`, `MV_NODES` | per node, active while above |
| Disk above **90 %** | `MV_SYSINFO_DISK` (SingleStore's mounts) | per node and mount, active while above |
| Node not online | `MV_NODES` | per node, active while not online |

Every rule can be switched off and its threshold changed in the view. An alert
is kept once, with first / last seen and a count, so a long-running query or a
full disk is one alert, not one a minute. Acknowledge one or all; an alert
that happens again after being acknowledged comes back unacknowledged.
**Clear acknowledged** / **Clear all** remove them; the list keeps the newest
300. On the first check against a cluster it starts from "now", so old
failures in the history don't flood the list. **Check now** runs the checks at
once.

Rules, settings and alerts are stored in `~/.singlestore-mcp/alerts.json`
(shared by the server Claude starts and the desktop workspace; a change in one
is picked up by the other). `SINGLESTORE_MCP_ALERTS=0` turns the background
checks off.

The workspace rail shows the number of unacknowledged alerts as a red badge
(it reads the server's in-memory state every 30 seconds; no cluster queries).
With **Notify me** on, the workspace also shows a browser notification for new
alerts, after you allow notifications in the browser (it asks on your next
click in the workspace). That works in the browser and the desktop window; in
the Claude app the badge is the signal.
