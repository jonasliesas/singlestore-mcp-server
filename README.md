# singlestore-mcp-server

A local MCP server for SingleStore that runs in **stdio** mode from Claude
Code, Claude Desktop or VS Code, and works with **SingleStore Helios and
self-managed clusters** alike.

For how it's built and how it connects to SingleStore, Claude, SAS Viya and
identity providers, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What it does

- **SQL and schema tools**: run SQL, list databases and tables, describe
  tables, so Claude can query and explain your data.
- **Pipelines as first-class tools**: create, alter, start, stop, test, drop
  and inspect SingleStore Pipelines (S3, Kafka, Azure, GCS, filesystem).
- **Interactive apps** in Claude (MCP Apps), combined in one **SingleStore
  Workspace** with a left-hand rail:
  - **SQL Editor**: autocomplete for SingleStore SQL, functions and your
    schema; results grid; Open/Save `.sql` files; procedure-aware statements.
  - **Notebook**: SQL, Python and text cells on a Jupyter kernel; SQL results
    become pandas DataFrames; charts; `%sql` / `%%sql` magics; `.ipynb` files
    compatible with SingleStore Notebooks; several notebooks as tabs.
  - **Schema Explorer**, **Pipeline Monitor**, **Cluster Monitor** (CPU,
    memory, disk per node and running queries) and a **Query Grid**.
- **Claude inside the apps**: a chat panel in the SQL Editor and the Notebook
  that checks the database (read-only) and answers with SQL / Python you can
  insert with one click.
- **Slash commands**: `/singlestore-workspace`, `/singlestore-notebook`,
  `/singlestore-table-report`, `/singlestore-pipeline-health`, … and
  `/singlestore-restart` to reload the server without reconnecting.
- **Several SingleStore connections**: save connections (passwords in the OS
  credential store) and switch the active one from the workspace or by asking
  Claude.
- **Standalone workspace**: the same apps in their own window from a desktop
  shortcut, with no Claude subscription needed (only the Claude chat needs
  Claude); see [Using the workspace without Claude](#using-the-workspace-without-claude).

## How it's built

It's built on official SDKs rather than reimplementing protocol or driver code:

- [`mcp`](https://github.com/modelcontextprotocol/python-sdk) — the official
  Model Context Protocol Python SDK. It handles the stdio transport, JSON-RPC
  framing, tool-schema generation (`MCPServer`) and the MCP Apps extension.
- [`singlestoredb`](https://github.com/singlestore-labs/singlestoredb-python)
  — SingleStore's own official Python client. It handles the actual database
  connection.
- [`@modelcontextprotocol/ext-apps`](https://github.com/modelcontextprotocol/ext-apps)
  — the official MCP Apps browser client, vendored and inlined into the
  interactive UIs (see [SingleStore Workspace](#singlestore-workspace)).

Everything in [`src/singlestore_mcp`](src/singlestore_mcp) is glue: a
pooled connection wrapper ([`db.py`](src/singlestore_mcp/db.py)) and a set of MCP
tools ([`server_impl.py`](src/singlestore_mcp/server_impl.py)), including first-class
tools for **Pipelines** (SingleStore's mechanism for continuously loading
data from S3/Kafka/Azure/GCS/filesystem sources), which the official
`mcp-server-singlestore` package does not expose as dedicated tools.

Because it connects over the plain MySQL wire protocol via host/port/user/
password, it works identically against **SingleStore Helios (cloud) and
self-managed SingleStore clusters** — there's no dependency on the
Management API or browser OAuth.

## Tools

General:
- `run_sql` — run any SQL statement
- `list_databases`, `list_tables`, `describe_table`

Pipelines:
- `list_pipelines`, `pipeline_status`, `get_pipeline_ddl`
- `create_pipeline`, `alter_pipeline` (take a full statement — pipeline
  syntax varies too much by source/format to model as parameters)
- `start_pipeline` (background or `FOREGROUND`, with optional batch limit)
- `stop_pipeline`, `drop_pipeline`, `test_pipeline`

Interactive apps (see [SingleStore Workspace](#singlestore-workspace)): `sql_editor` (opens the
SingleStore Workspace), `notebook`, `pipeline_monitor`, `query_grid`,
`schema_explorer`, `cluster_monitor`.

Other:
- `browser_link`: a link that opens an app full-window in your browser
- `sql_editor_reply`: send SQL or an answer into an open SQL Editor's chat
- `restart_server`: reload the server's code and apps without reconnecting
- `list_connections`, `use_connection`: see the saved SingleStore connections
  and switch the active one

## Slash commands

| Command | What it does |
|---|---|
| `/singlestore-workspace [db] [view]` | Open the SingleStore Workspace, e.g. `/singlestore-workspace SASDP notebook` |
| `/singlestore-notebook [db] [file]` | Open a notebook |
| `/singlestore-explain-query <sql>` | Explain a query, run `EXPLAIN`, suggest a faster version |
| `/singlestore-create-pipeline <source>` | Build a pipeline from an S3 path / Kafka topic (asks before creating) |
| `/singlestore-pipeline-health [db]` | Check all pipelines for errors, stalls and lag |
| `/singlestore-table-report <table>` | Size, storage, keys and data profile of a table |
| `/singlestore-restart` | Restart the server so code and app changes load |

These are Claude Code **skills**, in [`claude-skills/`](claude-skills): copy
the folders to `~/.claude/skills/` to use them.

The server also offers the same commands as MCP prompts
(`/singlestore:workspace`, `/singlestore:restart`, …). The Claude Code
desktop app adds an "(MCP)" label to those that it then refuses to send, so
use the skills there; other clients can use the prompts.

### Restarting without reconnecting

The process Claude starts is a small relay ([`supervisor.py`](src/singlestore_mcp/supervisor.py))
that runs the real server as a child process. The `restart_server` tool (or
`/singlestore:restart`) replaces that child, replays the MCP handshake and
tells Claude that the tools, prompts and resources changed, so Python and app
changes load in a few seconds without touching `/mcp`. Browser links made
before a restart stop working. Set `SINGLESTORE_MCP_NO_SUPERVISOR=1` to run
the server without the relay.

## SingleStore Workspace

In hosts that support [MCP Apps](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp)
(e.g. Claude), the server's apps open as interactive windows in the chat. In
other hosts the same tools return a normal text result.

The apps are combined in the **SingleStore Workspace**. A slim rail on the
left switches between its views:

| View | What it's for |
|---|---|
| **SQL** | [SQL Editor](#sql-editor): write and run SQL, with autocomplete and Claude |
| **Notebook** | [Notebook](#notebook): SQL and Python cells on a Jupyter kernel |
| **Schema** | [Schema Explorer](#schema-explorer-pipeline-monitor-cluster-monitor-query-grid): databases, tables, columns, keys |
| **Pipelines** | [Pipeline Monitor](#schema-explorer-pipeline-monitor-cluster-monitor-query-grid): state, progress and errors of pipelines |
| **Cluster** | [Cluster Monitor](#schema-explorer-pipeline-monitor-cluster-monitor-query-grid): CPU, memory, disk per node and running queries |
| **Connect** (bottom of the rail) | [Connections](#connections): saved SingleStore connections and the active one |

**Opening it**

- Ask Claude ("open the SingleStore workspace"), or use `/singlestore-workspace`
  (optionally with a database and view, e.g. `/singlestore-workspace SASDP notebook`).
- The tool behind it is `sql_editor(database?, sql?, view?, table?)`; `view`
  is `sql`, `notebook`, `schema`, `pipelines` or `cluster`.
- Outside Claude: in your browser or as a desktop app, see
  [Browser and desktop window](#browser-and-desktop-window).

**Working with it**

- Each view loads the first time you open it and keeps its state when you
  switch; hidden views pause their auto-refresh.
- Views hand work to each other, e.g. **Query in grid** in the Schema view
  opens the table's query in the SQL view and runs it.
- The database dropdowns refresh themselves (when opened, after `CREATE` /
  `DROP DATABASE`, and with ↻).
- **⤢ Full screen** (where the host supports it) and **↗ Open in browser** sit
  at the bottom of the rail.
- Buttons that run tools go through Claude, which may ask you to approve
  them; statements that change data also ask in the app first.

### SQL Editor

**Writing and running SQL**

- Schema tree on the left; editor on top, results grid below (drag the
  divider to resize).
- Autocomplete for SQL and SingleStore keywords, built-in functions (with
  signatures), your functions and procedures, databases, tables and columns.
- **Ctrl+Enter** runs the statement at the cursor, or the selection. Reads run
  right away; statements that change data or schema ask first.
- Understands `CREATE PROCEDURE / FUNCTION … BEGIN … END` bodies, `DECLARE`
  sections and `DELIMITER //` scripts as one statement.
- History of recent statements; results export to CSV.
- **Chart** (next to *Table* above the results) charts any result: bar
  (grouped or stacked), line, area, scatter or pie. It picks X and Y for you
  (a date or text column on X, the numeric columns on Y); you can change them,
  combine repeated X values (sum, average, count, min, max), split one measure
  into series by a column, sort, hide series in the legend and download the
  chart as SVG. Re-running the same query keeps the chart. Drawn as plain SVG,
  so it works without internet access.

**Files**

- **Open**, **Save** (Ctrl+S) and **Save as** open a file browser: folders,
  plus shortcuts to the SQL folder, Documents, Desktop, Downloads and Home.
- Files can live anywhere under your user folder; the SQL folder
  (`Documents\SingleStore SQL` by default) is where the browser starts.
- The header shows the file name and ● for unsaved changes; replacing a file
  asks first.
- **Browse computer…** uses the browser's own file picker; **Download** saves
  through the browser (works in the browser window; Claude may block it).

**Chat with Claude**

- The **Claude** button opens a chat panel on the right.
- Claude sees the editor's SQL and last result, checks the database
  (read-only), and answers with SQL that you **Replace**, **Insert** or
  **Copy** into the editor with one click. If the editor is empty, the
  answer's SQL goes straight in.
- **Answer here in the editor** (default) uses the
  [in-app assistant](#claude-in-the-apps); **in the Claude chat** sends the
  question to the chat box instead (you click Send there).

### Notebook

**Cells**

- **SQL cells** run against SingleStore. The result shows as a grid and
  becomes the pandas DataFrame `df` (and a named variable if you fill in
  *result →*). Several statements per cell are fine; writes ask first.
- **Python cells** run on an IPython (Jupyter) kernel with pandas (`pd`),
  matplotlib (inline charts) and `conn` (a SingleStore connection).
- **SAS cells** run SAS code (DATA steps, PROCs, PROC SQL) on your SAS Viya,
  with the ODS output and the log (errors and warnings counted, the log opens
  when there are errors). See *SAS cells* below.
- **Text cells** are Markdown with embedded HTML, as in Jupyter (styled
  headers, images, alert boxes); scripts are stripped.
- **Shift+Enter** runs and moves on, **Ctrl+Enter** runs, **Alt+Enter** runs
  and adds a cell. The header has **Run all**, interrupt **■** and restart
  **↻**, plus the kernel's status.

**SQL from Python (SingleStore Notebooks compatible)**

- `rows = %sql SELECT …` returns rows (`rows[0][1]`, `pd.DataFrame(rows)`,
  `rows.DataFrame()`); `%sql name << SELECT …` stores the result in `name`.
- `%%sql [name <<]` cells, and `{{ variable }}` to insert Python values.
- `connection_url` (and `SINGLESTOREDB_URL`, used by `s2.connect()` /
  `s2.create_engine()`) follows the database selected in the notebook.
- Magics and Python writes run without the app's confirmation, as in Jupyter.

**Notebooks and files**

- Several notebooks open as **tabs**, each with its own kernel. **New** opens
  a new tab; **Open** loads into a new tab unless the current one is empty.
  Closing a tab stops its kernel (unsaved tabs need a second click).
- Saved as standard `.ipynb` files, with SQL cells as `%%sql` and SAS cells
  as `%%sas` cells, so they
  open in Jupyter / VS Code and SingleStore Notebooks, and SingleStore's
  example notebooks open here.

**Python environment and packages**

- Python runs in its own environment, `~/.singlestore-mcp/notebook-env`,
  installed with `uv` the first time (the notebook offers an **Install**
  button). It has what SingleStore's examples expect: pandas, matplotlib,
  singlestoredb, SQLAlchemy with the SingleStore dialect, ibis, scikit-learn,
  and SAS's Python packages: saspy, swat (CAS), DLPy (`sas-dlpy`), sasctl
  and sasoptpy.
- **Packages** lists what's installed (with filter) and installs more. `%pip
  install …` and `!pip install …` in a cell also install into this
  environment. Restart the kernel (↻) after installing.
- The environment is shared by every notebook, by Claude and by the desktop
  window, and survives restarts.
- Windows specifics handled for you: `pandarallel`'s `parallel_apply` runs as
  plain `apply`, and Hugging Face downloads use plain HTTPS (its newer
  download method stalls on some corporate networks).
- Code runs with your user rights on this machine, like any local Jupyter.
  Kernels stop after 30 idle minutes; SQL cells fetch at most 100,000 rows.

**SAS cells (SAS Viya)**

- Set up once in **Connect → SAS Viya**: the Viya address, the compute
  context (e.g. *SAS Studio compute context*; **Test** lists them) and
  **Sign in**. Sign-in uses your own Viya account (OAuth with Viya's built-in
  `vscode` client): SAS Logon opens in the browser and shows a code to paste
  once; after that the sign-in renews itself. If you already signed in with
  the SAS Viya MCP server or the SAS Viya CLI, that sign-in is reused.
- **+ SAS** adds a SAS cell. The code runs in a SAS Compute session on Viya
  (one per notebook, started on the first SAS cell, which takes about half a
  minute; restart the kernel for a fresh one).
- **SingleStore from SAS**: each session gets the library **S2** on the
  notebook's SingleStore database (SAS/ACCESS to SingleStore, engine
  `SSTORE`), so a DATA step can `set s2.mytable;` or write `data s2.newtable;`.
  The password isn't echoed to the SAS log. This needs a password connection
  (not JWT / SSO); the libref name can be changed in the settings.
- **Let Viya read SingleStore directly.** SAS reads and writes SingleStore
  itself through **S2** (and SAS/ACCESS pushes `WHERE` clauses and PROC SQL
  down to SingleStore); CAS reads it through a SingleStore-backed caslib (SAS
  Data Connector to SingleStore / SpeedyStore). Don't pull SingleStore data
  into the notebook's Python and upload it to Viya: that round trip through
  your machine is slow and unnecessary. Claude in the notebook follows the
  same rule.
- **From Python**: `sas_session()` is the saspy session, `cas_session()` a
  swat CAS connection (use it with DLPy) and `sasctl_session()` a sasctl
  session, all signed in with the same Viya sign-in. `sas_to_df` and
  `df_to_sas` exist for small, local results only.
- Claude in the notebook knows about SAS cells and suggests them in ```sas
  blocks with an **Insert SAS cell** button.

**Claude**

- The **Claude** button opens a chat panel that sees the notebook's cells and
  outputs, checks the database (read-only) and answers with SQL or Python
  you insert as a new cell, or use to replace the selected one.

### Connections

The **Connect** view (also the `connections_window` tool) manages saved
SingleStore connections. **One connection is active at a time**: every view,
Claude's tools and new notebook kernels use it. The rail's tooltip shows which
one.

- **+ New connection**: name, host, port, user, password, default database,
  TLS on/off, or a full connection URL.
- **TLS certificates**: a CA certificate (PEM) to verify the server, with
  **Browse…** and **Use SingleStore Helios CA** (downloads SingleStore's
  `singlestore_bundle.pem` to `~/.singlestore-mcp` once), *Verify the server's
  certificate*, and optional client certificate + key. Helios requires the CA
  (otherwise: "1251: No SSL detected"). Only the file paths are saved. For the
  Environment variables connection use `SINGLESTORE_SSL_CA`,
  `SINGLESTORE_SSL_CERT`, `SINGLESTORE_SSL_KEY` and `SINGLESTORE_SSL_VERIFY`. **Test** tries it before saving;
  **Save and connect** makes it active right away.
- **Authentication**: *Password*, *JWT token* (paste a token from your
  identity provider, for users created `IDENTIFIED WITH authentication_jwt`;
  stored like a password, and the card shows how long it stays valid) or
  *Browser SSO (SingleStore Helios)*: **Sign in** opens SingleStore's sign-in
  page in your browser, and the token is cached until it expires, or
  *Microsoft Entra ID (SSO)*: no app registration needed, just your UPN as
  the user (tenant, client ID and scope are optional). **Sign in** first uses the account you're
  signed in to Windows / macOS with (on an Entra-joined laptop usually without
  any prompt), otherwise Microsoft's sign-in page in the browser; after that,
  tokens are renewed silently, also inside running notebook kernels. One
  sign-in serves all views, notebook kernels and Claude. Token logins need
  TLS (for Helios: *Use SingleStore Helios CA*). When a token expires, new
  connections ask you to sign in again or paste a new token; for the
  Environment variables connection set `SINGLESTORE_CREDENTIAL_TYPE=jwt` and
  put the token in `SINGLESTORE_PASSWORD`.
- Each saved connection has **Connect**, **Test**, **Edit** and **Delete**.
- Your existing `SINGLESTORE_*` settings appear as the built-in, read-only
  connection **Environment variables**, so nothing changes until you add more.
- After a switch, the Schema, Pipelines and Cluster views reload and the SQL
  Editor refreshes its databases. Open notebooks keep their kernel (and
  variables) on the old connection until you restart it; they offer a
  **Restart kernel now** button.
- Claude can switch too: `list_connections` and `use_connection(name)`
  (e.g. "switch to the test cluster").

**Where things are stored:** the connection list (no passwords) in
`~/.singlestore-mcp/connections.json`. Passwords go to the operating system's
credential store via [`keyring`](https://pypi.org/project/keyring/): Windows
Credential Manager, macOS Keychain or the Secret Service on Linux. Where there
is none (e.g. a headless Linux server) they go to an encrypted file in
`~/.singlestore-mcp` whose key only your user account can read. Passwords are
never sent back to the app. Secrets too large for Windows Credential Manager
(Entra ID tokens) go to `~/.singlestore-mcp/secrets-large.enc`, encrypted with
a key kept in Credential Manager.

**Setting up Microsoft Entra ID logins** (self-managed cluster). No app
registration is needed: by default the sign-in uses Microsoft's Azure CLI
public client (available in every tenant) and requests the *Azure OSS
database* token (`https://ossrdbms-aad.database.windows.net`), the same token
Azure Database for MySQL / PostgreSQL accept for Entra logins. Only the
cluster needs setting up (its nodes must reach `login.microsoftonline.com`):

```sql
SET GLOBAL jwks_endpoint = 'https://login.microsoftonline.com/<tenant ID>/discovery/keys';
SET GLOBAL jwks_username_field = 'upn';
CREATE USER 'jonas@company.com'@'%' IDENTIFIED WITH authentication_jwt REQUIRE SSL;
GRANT SELECT ON mydb.* TO 'jonas@company.com'@'%';
```

Then in Connections: **Microsoft Entra ID (SSO)**, your UPN as the user, TLS
on → **Save** → **Sign in**. After a sign-in whose test fails, the card shows
the exact `jwks_endpoint`, `jwks_username_field` and user the cluster needs.

- Use the `/discovery/keys` (v1) key list: the database tokens are v1 tokens,
  and the keys in the `/discovery/v2.0/keys` list carry a v2 issuer that
  doesn't match them, so the cluster rejects the token.
- The database user must be spelled exactly as in the token's `upn`, upper
  and lower case included (e.g. `'Jane.Doe@company.com'@'%'`); after
  **Sign in**, the connection takes the token's spelling.
- `SET GLOBAL jwks_require_audience = 'https://ossrdbms-aad.database.windows.net';`
  makes the cluster accept only tokens issued for database logins.
- Entra's signing keys sign tokens for every app in the tenant, and SingleStore
  maps tokens to users by the user-name claim only, so create database users
  only for people who should have access. `upn` is only issued for verified
  domains, which is why it's the default rather than `preferred_username`.
- Some tenants block the Azure CLI app with conditional access. Then register
  an app of your own (public client, redirect URIs `http://localhost` and
  `ms-appx-web://Microsoft.AAD.BrokerPlugin/<client ID>`, an exposed API scope)
  and enter its client ID; tokens then come for `api://<client ID>/.default`.

### Schema Explorer, Pipeline Monitor, Cluster Monitor, Query Grid

| App (tool) | What it shows and does |
|---|---|
| **Schema Explorer** `schema_explorer(database?, table?)` | Databases → tables with row counts and sizes; per table the columns, DDL (shard / sort keys) and a row preview. **Ask Claude** and **Query in grid**. |
| **Pipeline Monitor** `pipeline_monitor(database?)` | Every pipeline's state, source → table, progress (files loaded / Kafka lag), latest batches and errors. Start / Stop (with confirmation), Test, error details, **Ask Claude**, auto-refresh. |
| **Cluster Monitor** `cluster_monitor()` | Per node SingleStore CPU (against its core limit), memory and disk with a 15-minute CPU chart (all / aggregators / leaves), plus the queries running now. Refreshes every 5 s. |
| **Clean up** `cleanup_work_tables()` | Finds the work tables SAS jobs leave behind (`_dm…` Data Management, `_flw…` flow, `SASTMP…` / `_tmp…` temp tables) in every non-system database: rows, size, created, created by, age, last use and dependent views. Select tables, confirm, and they're dropped one at a time, with a dry-run mode and a log. The **🧹 Clean up** button in the Schema Explorer's header. |
| **Query History** `query_history(min_seconds=1, hours?, tab?)` | Every finished query the cluster traced: when, how long, user, database, rows, success / error, and a probable **Source** (SAS CAS, SAS in-database). Filter on runtime (1 s and up), period, user, database, status, type, source and text; sort by time, duration or rows. Select a query for its full SQL and **Get tuning recommendations**. Tabs **Queries**, **Trends** and **Advisor**. Its own **History** item in the workspace rail. |
| **Alerts** `alerts()` | Alerts the server raises while it watches the cluster in the background: queries running too long, slow or failed queries, failed pipeline batches and pipeline errors, node memory and disk over a threshold, nodes not online. Edit the rules, acknowledge, clear. Its own **Alerts** item in the workspace rail, with a red badge counting the unacknowledged alerts. |
| **Query Grid** `query_grid(sql, database?, max_rows=1000)` | Read-only results as a sortable, filterable grid with CSV export; the SQL can be edited and re-run. Writes are refused. |

`TEST PIPELINE` loads no data, but a failed test is still recorded in the
pipeline's batch history and error log.

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

**Query History** needs SingleStore's query history (event tracing) turned on
once, by an admin:

```sql
CREATE EVENT TRACE Query_completion WITH (Query_text = on, Duration_threshold_ms = 1000);
```

Its **tuning recommendations** work without Claude. For the selected run they
combine:
- the run itself: rows returned or written, `SELECT *`, no filter,
  `CREATE TABLE … AS SELECT` without a shard key, errors such as unreachable
  Kafka brokers;
- the plan cache: disk spilling, time queued by workload management, memory,
  plan warnings, outdated statistics;
- `EXPLAIN` (compiles the statement without running it): missing column
  statistics (with the `ANALYZE` commands), broadcasts, reshuffles,
  nested-loop joins, filtered scans of tables without a sort key;
- the tables' shard and sort keys.

**Ask Claude** answers right in the panel: like the SQL Editor's assistant,
Claude Code runs in the background with read-only database access, gets the
query, the findings, the plan and the table definitions, and can check things
itself (row counts, cardinality). This also works in the desktop window and the
browser. Only without Claude Code installed does it fall back to the Claude
chat.

The **Advisor** tab (also the `query_advisor_report` tool) looks at the whole
history instead of one query. It `EXPLAIN`s every distinct query (about 5
seconds for a few hundred) to see which columns each one filters, joins and
groups on, weights them by how long those queries ran, and suggests per table:

- a **SORT KEY** on the columns most query time filters on, so whole segments
  are skipped;
- a **SHARD KEY** on the join / group-by columns when data is reshuffled or
  broadcast. Only columns with enough distinct values qualify (checked against
  the optimizer's statistics), so it never suggests a key that would skew the
  partitions;
- **REFERENCE tables** for small tables that get broadcast;
- **indexes** for filtered rowstore tables, and **ANALYZE** for missing statistics.

Keys can't be changed in place, so each suggestion comes with statements that
build a copy with the new keys from the table's own definition
(`CREATE TABLE … ; INSERT … SELECT`) and, commented out, swap the names. A
**Workload** card lists queries returning millions of rows, large `SELECT *`
queries and the most frequent errors.

**Source column**

The Queries tab guesses where a statement came from, from its SQL:

- **SAS CAS**: contains `binary_serialization` or `PARALLELISM_LEVEL="SEGMENT"`
  (SAS SpeedyStore / CAS loading or pushing down through the cluster);
- **SAS in-database**: reads or writes tables named `_dm…`, `_flw…` or
  `SASTMP…` (SAS work tables created in the database);
- empty otherwise.

It's a heuristic, shown as "probably" in the detail panel, and a filter
(**Source**: All / SAS CAS / SAS in-database / Other).

**Trends**

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
  (bars and a line, hover for the values). **Click a bar** to list the
  queries behind it, slowest first (on the Failures chart, only the failed
  ones). Click a query to open it in the Queries tab with its full SQL,
  tuning recommendations and Ask Claude, as long as the cluster's history
  still has it; older runs show the start of their SQL;
- **Slower than before**: query shapes whose median duration in the last 7
  days is at least 1.5× and 1 s more than in the 7 days before, with the run
  counts of both weeks;
- **New heavy queries**: shapes first seen in the last 7 days with more than a
  minute of runtime in total.

Click a shape to see its runs in the Queries tab (as long as the cluster's
history still has them). Only queries the cluster traced are counted, i.e.
those over the event trace's `Duration_threshold_ms` (1 s in the setup above).

**Before/after check**

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

**Alerts**

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

### Claude in the apps

The chat panels in the SQL Editor and the Notebook are answered by the
server itself, by running Claude Code headless with your own Claude login:
no API key, no Send click in the chat, and it also works in the browser and
desktop window.

- **What it can do:** read the schema and run read-only queries (through its
  own MCP server with only `list_databases`, `list_tables`, `describe_table`
  and `read_query`). It has no shell or file access and can't change data:
  it gives you statements to review and run.
- **Speed:** *Fast* (Haiku), *Balanced* (Sonnet, medium effort; default) or
  *Thorough* (Opus, high effort).
- **Knows SingleStore:** every question includes the SingleStore SQL skill
  ([`SKILL.md`](src/singlestore_mcp/skills/singlestore-sql/SKILL.md)), key
  learnings such as case-sensitive names and procedure syntax. Add to it as
  you find pitfalls.
- **Quick answers:** each editor / notebook keeps one Claude process running
  (started when you open the panel), so questions don't wait for Claude Code to
  start; follow-ups keep the conversation. Idle processes close after 10
  minutes. Progress and a **Stop** button show while it works.
- **Needs** Claude Code installed and logged in (`claude`, then `/login`,
  once). It uses your Claude plan like any other Claude Code session.

### Browser and desktop window

**↗ Open in browser** reopens the current view full-window in your browser,
for when the chat column is too narrow. Claude also posts this link under each
app, and the `browser_link` tool makes one on request. Links work on this
machine only and stop working when the server restarts.

The workspace also runs **without Claude**, in its own window:

```bash
uv run python scripts/make_shortcut.py --database SASDP
```

creates a **SingleStore Workspace** desktop shortcut. It starts the app server
on `127.0.0.1` and opens the workspace in an Edge app window (`--browser` for
your default browser). Clicking it again opens another window; after a code
update it replaces the running server; it stops 5 minutes after the last
window closes. It needs the `SINGLESTORE_*` settings as user environment
variables. Full guide, including installing on a new machine:
[Using the workspace without Claude](#using-the-workspace-without-claude).

In the browser and desktop window, actions such as Start / Stop pipeline don't
go through Claude's approval prompt (the apps' own confirmations still apply),
and chat answers come from the in-app assistant.

### Settings

All optional, as environment variables of the MCP server:

| Variable | Default | What it does |
|---|---|---|
| `SINGLESTORE_MCP_SQL_DIR` | `Documents\SingleStore SQL` | Folder the file browser starts in |
| `SINGLESTORE_MCP_FILE_ROOTS` | your user folder | Extra folders Open / Save may use (separated by `;`) |
| `SINGLESTORE_MCP_HOME` | `~/.singlestore-mcp` | Where the notebook environment and app state live |
| `SINGLESTORE_MCP_NOTEBOOK_PYTHON` | (own environment) | Use an existing Python for notebooks instead |
| `SINGLESTORE_MCP_NOTEBOOK_FETCH_LIMIT` | `100000` | Max rows a notebook SQL cell fetches |
| `SINGLESTORE_MCP_IMAGE_DOMAINS` | GitHub raw hosts | Image hosts allowed in notebook text cells inside Claude |
| `SINGLESTORE_MCP_CLAUDE` | `claude` on PATH | Claude Code executable for the in-app assistant |
| `SINGLESTORE_MCP_ASSISTANT_MODEL` / `_EFFORT` | `sonnet` / `medium` | The *Balanced* assistant profile |
| `SINGLESTORE_MCP_ASSISTANT_TIMEOUT` | `300` | Seconds before an answer is abandoned |
| `SINGLESTORE_MCP_POOL_SIZE` | `8` | Database connections in the server's pool |
| `SINGLESTORE_MCP_NO_SUPERVISOR` | off | Run without the restart relay |

`~/.singlestore-mcp` is used rather than `%LOCALAPPDATA%` because Windows gives
the Claude desktop app a private copy of that folder, so Claude and the desktop
window would otherwise use different notebook environments.

### How the apps work (for developers)

- The model gets a compact text summary of each app's result (e.g. the
  first 20 rows); the full data goes to the app only. Helper tools the apps
  call for refreshes and drill-downs are app-only, so they don't clutter the
  model's tool list.
- Layout: [`src/singlestore_mcp/apps/`](src/singlestore_mcp/apps) has one
  `<name>.py` (tools) + `<name>.html` (UI) per app, shared `shared.js` /
  `shared.css`, and vendored libraries in `vendor/` (the official ext-apps
  client and a CodeMirror bundle built by `scripts/build_codemirror`), inlined
  so the apps need no internet access.
- The workspace is a small MCP Apps host for its views: each view runs in its
  own sandboxed iframe (loaded through a tool call, so hosts can't serve a
  stale page after a restart), and the workspace relays its tool calls,
  messages and model context to the real host.
- Notebook kernels run in the notebook environment through
  [`notebook_bridge.py`](src/singlestore_mcp/notebook_bridge.py), driven by
  [`notebook_kernel.py`](src/singlestore_mcp/notebook_kernel.py).
- Every page carries a build stamp (hover the Notebook title) to spot an
  outdated page.

**Dev host:** `scripts/dev_host.py` is a local stand-in for Claude. It starts
the real server over stdio, renders an app and speaks the MCP Apps protocol
to it, against your real cluster.

```bash
uv run python scripts/dev_host.py --port 8765
```

Open http://127.0.0.1:8765, pick a tool, give JSON arguments, Run. Each run
starts a fresh server, so Python and HTML edits show up on reload. The right
panel shows the protocol log and anything the app sends to the chat. Add
`&debug=1` to the URL to give the iframe same-origin access (`appDoc()` in
the console returns the app's document) for scripted testing.

## Using the workspace without Claude

The SingleStore Workspace also runs as a **standalone desktop app**: a small
local web server plus a browser window, talking directly to SingleStore. No
Claude subscription, Claude app or MCP client is needed for it. Only the
Claude chat panels need Claude.

### Quick install (Windows, one command)

Open PowerShell (or press Win+R) and run:

```bash
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/jonasliesas/singlestore-mcp-server/master/install.ps1 | iex"
```

No administrator rights needed. [`install.ps1`](install.ps1):
1. installs [uv](https://docs.astral.sh/uv/), which brings Python 3.12;
2. downloads the program to `%USERPROFILE%\singlestore-workspace` (with git if
   available, otherwise as a ZIP);
3. installs the dependencies;
4. creates a **SingleStore Workspace** shortcut on the desktop and in the Start
   menu;
5. opens the workspace. On the first start it opens on **Connections**, where
   you add your cluster (password, JWT, Helios SSO or Microsoft Entra ID).

Run the same command again to **update**. Options (pass them with
`& ([scriptblock]::Create((irm <url>))) -Notebook -Database SASDP`, or run a
downloaded `install.ps1` with them):

| Option | What it does |
|---|---|
| `-Database SASDP` | database the shortcut opens in |
| `-Notebook` | also install the notebook's Python environment now (about 150 MB) |
| `-InstallDir <folder>` | install somewhere else |
| `-Source <folder>` | install from a local copy of the project (share, USB stick), not from GitHub |
| `-WithClaude` | also register the MCP server and skills with Claude Code, if installed |
| `-NoLaunch` | don't open the workspace at the end |
| `-Uninstall` | remove the program and shortcuts (keeps connections, files and notebooks) |

The manual steps below do the same by hand.

### What works and what doesn't

| Feature | Without Claude |
|---|---|
| SQL Editor: autocomplete, run SQL, results, history, CSV export | ✅ |
| SQL files: Open / Save / Save as, file browser | ✅ |
| Notebook: SQL, Python and text cells, charts, `%sql` / `%%sql`, `.ipynb` files, tabs | ✅ |
| Notebook Python environment and the **Packages** panel | ✅ (installed with `uv`) |
| Schema Explorer, Pipeline Monitor (incl. Start / Stop / Test), Cluster Monitor, Query Grid | ✅ |
| Database list refresh, full-window layout | ✅ |
| **Claude** chat panels (SQL Editor, Notebook) | ❌ need Claude Code logged in to a Claude account; without it the panel shows a message and nothing else is affected |
| **Ask Claude** buttons (Schema Explorer, Pipeline Monitor) | ❌ hand the question to the Claude chat, which the standalone window doesn't have; the button says so |
| Slash commands, asking Claude in a chat | ❌ need a Claude client |

Other MCP clients (e.g. VS Code with GitHub Copilot) can still use the
server's tools, but show plain text results instead of the apps.

### What you need

- **Windows** with **Microsoft Edge** (for the app window). On macOS / Linux the
  standalone app also works with `--browser`, but the desktop shortcut
  script is Windows-only.
- **[uv](https://docs.astral.sh/uv/)**, which installs Python 3.12 and all
  dependencies for you. No separate Python installation is needed.
- **Git**, or download the repository as a ZIP from GitHub.
- Network access to your SingleStore cluster (default port 3306) and, for the
  first notebook setup, to the Python package index (PyPI).

### 1. Install

```bash
git clone https://github.com/jonasliesas/singlestore-mcp-server.git
```

```bash
cd singlestore-mcp-server
```

```bash
uv sync
```

`uv sync` creates `.venv` in the project folder with Python 3.12 and the
server's dependencies.

### 2. Connection settings

The app reads the same settings as the MCP server, from **user environment
variables** (Windows: *Settings → System → About → Advanced system settings →
Environment Variables → User variables*):

| Variable | Example | Required |
|---|---|---|
| `SINGLESTORE_HOST` | `svc-xxxx.svc.singlestore.com` or your cluster's host | yes (or `SINGLESTORE_URL`) |
| `SINGLESTORE_USER` | `admin` | yes |
| `SINGLESTORE_PASSWORD` | your password | yes |
| `SINGLESTORE_PORT` | `3306` | no (default 3306) |
| `SINGLESTORE_DATABASE` | `SASDP` | no: default database |
| `SINGLESTORE_SSL_DISABLED` | `1` | no: only if your cluster has no TLS |
| `SINGLESTORE_URL` | `user:password@host:3306/db` | no: instead of the separate variables |

Enter the password in the Environment Variables dialog rather than with
`setx` in a terminal, so it doesn't end up in your shell history. Windows
picks up new variables in newly started programs, so set them before creating
the shortcut, or sign out and in again afterwards.

### 3. Create the desktop shortcut

```bash
uv run python scripts/make_shortcut.py --database SASDP
```

This creates a **SingleStore Workspace** shortcut with a database icon on your
desktop. To find it from the Start menu too, copy it to
`%APPDATA%\Microsoft\Windows\Start Menu\Programs`. Options:

- `--database SASDP`: database to start in
- `--view notebook`: open on another view (`sql`, `notebook`, `schema`,
  `pipelines`, `cluster`)
- `--name "SingleStore Notebook"`: shortcut name (make several shortcuts for
  different databases or views)

Without a shortcut, start it from the project folder with
`.venv\Scripts\pythonw.exe -m singlestore_mcp.workspace_app --database SASDP`
(add `--browser` to use your default browser instead of an Edge window).

### 4. First start

1. Double-click the shortcut. A window opens with the workspace, usually on
   the SQL view. The first start takes a few seconds; the server runs in the
   background without a console window.
2. Open the **Notebook** view. The first time, it offers to **Install** the
   notebook's Python environment (about 150 MB, a minute or two). Later
   starts skip this.
3. Pick your database in the dropdowns and start working.

Everything you create is stored on your machine: SQL files and notebooks in
`Documents\SingleStore SQL` (or wherever you save them), the notebook
environment in `~/.singlestore-mcp`.

### Day to day

- **Several windows:** clicking the shortcut again while it runs opens
  another window on the same server (fast; notebooks and kernels are shared).
- **Stopping:** close the windows. The server stops by itself 5 minutes after
  the last window closes, along with any notebook kernels.
- **Updating:** run `git pull` and `uv sync` in the project folder. The next
  click on the shortcut notices the new code and restarts the server (this
  also restarts notebook kernels).
- **Packages for notebooks:** use the notebook's **Packages** panel or
  `%pip install …` in a cell; restart the kernel (↻) afterwards.

### Troubleshooting

| Symptom | What to do |
|---|---|
| Nothing happens on double-click | Run the command from step 3 with `python.exe` instead of `pythonw.exe` in a terminal to see the error (often missing `SINGLESTORE_*` variables). |
| "Couldn't connect" / errors in every view | Check the `SINGLESTORE_*` variables and that the cluster is reachable from this machine. |
| Window shows "Not found" | An old server is still running from an earlier version; close all windows, wait a few seconds and click the shortcut again. |
| Notebook says the environment is missing | Click **Install** in the notebook; it needs `uv` on PATH (or set `SINGLESTORE_MCP_NOTEBOOK_PYTHON` to a Python that has ipykernel). |
| A new package isn't found in a notebook | Restart the kernel (↻); check `import sys; print(sys.executable)` shows `…\.singlestore-mcp\notebook-env\…`. |
| Model downloads (Hugging Face) hang | Already handled: downloads use plain HTTPS. If you're behind a proxy, set `HTTPS_PROXY` as a user environment variable. |

### Security notes

- The server listens on `127.0.0.1` only, and every window uses a secret
  link; other machines and other web sites can't use it.
- It uses your SingleStore user's permissions. Statements that change data
  ask for confirmation in the SQL Editor and in SQL cells; Python cells and
  `%sql` run as written, as in Jupyter.
- Notebook code runs with your Windows user rights, like any local Jupyter.

## Setup

Uses [`uv`](https://docs.astral.sh/uv/) to manage the Python environment,
pinned to Python 3.12 via [`.python-version`](.python-version):

```bash
cd singlestore-mcp-server
uv sync
```

This creates `.venv/` and installs everything from `uv.lock`.

Then give the server your SingleStore credentials. Supported variables (see
[`.env.example`](.env.example)):

| Variable | Required | Notes |
|---|---|---|
| `SINGLESTORE_HOST` | yes* | |
| `SINGLESTORE_PORT` | no | default `3306` |
| `SINGLESTORE_USER` | no | default `root` |
| `SINGLESTORE_PASSWORD` | no | |
| `SINGLESTORE_DATABASE` | no | default database for queries |
| `SINGLESTORE_SSL_DISABLED` | no | set `true` for a self-managed cluster without TLS configured |
| `SINGLESTORE_URL` | yes* | alternative to the above: `user:password@host:port/database` |

\* set either `SINGLESTORE_HOST` or `SINGLESTORE_URL`.

How you wire these in depends on which MCP client you're using — the two
below are unrelated mechanisms, use whichever matches your setup.

### Claude Code (in VS Code, or the terminal)

There are two ways to register the server with Claude Code — pick one.
They're independent; you don't need both.

#### Option A: project-scoped, via `.mcp.json` (already done)

[`.mcp.json`](.mcp.json) at the project root already registers the server,
scoped to this project only:

```json
{
  "mcpServers": {
    "singlestore": {
      "command": "uv",
      "args": ["run", "--directory", ".", "singlestore-mcp-server"],
      "env": {
        "SINGLESTORE_HOST": "${SINGLESTORE_HOST}",
        "SINGLESTORE_USER": "${SINGLESTORE_USER}",
        "SINGLESTORE_PASSWORD": "${SINGLESTORE_PASSWORD}",
        "SINGLESTORE_DATABASE": "${SINGLESTORE_DATABASE:-}",
        "SINGLESTORE_SSL_DISABLED": "${SINGLESTORE_SSL_DISABLED:-false}"
      }
    }
  }
}
```

Claude Code doesn't have an interactive "prompt me for the secret" flow the
way VS Code Copilot Chat does. Instead, the `${VAR}` syntax above is expanded
from **your actual shell/OS environment** when Claude Code starts, so the
value never has to live in this tracked file — set the real variables
(Windows: System Properties → Environment Variables, or
`setx SINGLESTORE_PASSWORD hunter2`, then restart the terminal/VS Code so it
inherits the change) and `.mcp.json` stays safe to commit as-is.

Once the env vars are set, open **this folder** in VS Code with the Claude
Code extension (or run `claude` from a terminal cd'd into this folder) and
the `singlestore` server connects automatically — there's no separate mode
toggle to flip. Because the registration lives in this folder's `.mcp.json`,
it's only picked up when Claude Code's working directory is this project;
opening a different folder won't see it.

#### Option B: user-scoped, via `claude mcp add` (available everywhere)

To make the server available from *any* project — not just when this folder
is open — register it once at the user level instead, from a terminal (the
CLI reads your already-exported `SINGLESTORE_*` variables and bakes their
current values into the stored config, since `claude mcp add` doesn't do the
`${VAR}` expansion `.mcp.json` does):

```bash
claude mcp add singlestore -s user \
  -e SINGLESTORE_HOST="$SINGLESTORE_HOST" \
  -e SINGLESTORE_USER="$SINGLESTORE_USER" \
  -e SINGLESTORE_PASSWORD="$SINGLESTORE_PASSWORD" \
  -- uv run --directory "C:\path\to\singlestore-mcp-server" singlestore-mcp-server
```

(Swap `$SINGLESTORE_HOST` etc. for literal values on Windows PowerShell,
where `$VAR` bash-expansion inside a `bash`-tool call won't apply — or just
type the real host/user/password in place of those placeholders.) If you
later rotate the password, re-run the same command — `claude mcp add`
overwrites an existing entry with the same name.

#### Either way

Run `/mcp` inside a Claude Code session to check the `singlestore` server's
connection status and see the tools it exposes. If you just registered it
(either option) in a session that's already running, its tools won't appear
until you restart that session — MCP servers are only loaded at startup.

### Claude Desktop

Claude Desktop (the standalone app, not Claude Code) uses a different,
app-level config file — there's no per-project `.mcp.json` support and no
`${VAR}` expansion, so credentials have to be written into the file as
literal values.

1. Open the config file for your OS (create it if it doesn't exist yet —
   or in the Claude Desktop app, go to **Settings → Developer → Edit
   Config**, which creates and opens it for you):
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
2. Add a `singlestore` entry to `mcpServers`, merging with whatever's
   already there rather than replacing the whole file:

   ```json
   {
     "mcpServers": {
       "singlestore": {
         "command": "uv",
         "args": [
           "run",
           "--directory", "C:\\Users\\norjni\\claude code\\singlestore-mcp-server",
           "singlestore-mcp-server"
         ],
         "env": {
           "SINGLESTORE_HOST": "10.104.80.126",
           "SINGLESTORE_USER": "admin",
           "SINGLESTORE_PASSWORD": "your-actual-password-here",
           "SINGLESTORE_SSL_DISABLED": "false"
         }
       }
     }
   }
   ```

   Use an absolute path for `--directory` (Desktop doesn't run this from the
   project folder the way VS Code does), and escape backslashes as `\\` on
   Windows. On macOS the path would look like
   `/Users/you/singlestore-mcp-server`.
3. Quit Claude Desktop completely and reopen it — config changes only take
   effect on a full restart, not just closing the window.
4. Click the "Add files, connectors, and more" (**+**) control in the
   message box, open **Connectors → Manage connectors**, and confirm
   `singlestore` is listed and connected.

Since this file stores the password in plain text, treat it like any other
credentials file — don't commit it or share it, and rotate the password if
it ever leaks. Logs for debugging a failed connection live in
`%APPDATA%\Claude\logs\mcp-server-singlestore.log` (Windows) or
`~/Library/Logs/Claude/mcp-server-singlestore.log` (macOS).

### VS Code + GitHub Copilot Chat

If you're using Copilot Chat's own MCP support instead, [`.vscode/mcp.json`](.vscode/mcp.json)
registers the server for that. Copilot Chat *does* support an interactive
prompt via an `inputs` block + `${input:<id>}` placeholders (VS Code pops a
masked input box on first start and caches the value), which is the closest
equivalent to Claude Code's env-var approach above — see the comments in
that file. Switch Copilot Chat's mode dropdown to **Agent** for the
server's tools to show up; they're invisible in Ask/Edit mode.

## Manual smoke test

```bash
SINGLESTORE_HOST=127.0.0.1 SINGLESTORE_USER=root SINGLESTORE_PASSWORD=pw \
  uv run singlestore-mcp-server
```

This starts the stdio server and blocks waiting for JSON-RPC on stdin — that
hang is expected; it means it's up. Use the MCP Inspector
(`npx @modelcontextprotocol/inspector uv run singlestore-mcp-server`)
for interactive testing instead of talking to stdin by hand.

## Example: an S3 pipeline

```
create_pipeline(create_pipeline_sql="""
  CREATE PIPELINE orders_pipeline AS
  LOAD DATA S3 's3://my-bucket/orders/'
  CONFIG '{"region": "us-east-1"}'
  CREDENTIALS '{"aws_access_key_id": "...", "aws_secret_access_key": "..."}'
  INTO TABLE orders
  FIELDS TERMINATED BY ','
""")

start_pipeline(pipeline_name="orders_pipeline")
pipeline_status(pipeline_name="orders_pipeline")
```
