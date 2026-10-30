# singlestore-mcp-server

[![tests](https://github.com/jonasliesas/singlestore-mcp-server/actions/workflows/tests.yml/badge.svg)](https://github.com/jonasliesas/singlestore-mcp-server/actions/workflows/tests.yml)

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
    memory, disk per node, running queries, cluster health, 7-day metrics
    history and backups) and a **Query Grid**.
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
changes load in a few seconds without touching `/mcp`. Browser links and open
browser tabs keep working across a restart (see [Browser and desktop window](#browser-and-desktop-window)).
Set `SINGLESTORE_MCP_NO_SUPERVISOR=1` to run the server without the relay.

Restarts are fast because most of a fresh server's start is loading the MCP
SDK and the database driver, which don't change when this project's code does.
5 s after each start the relay prepares a *standby* process that has loaded only
those packages; `restart_server` hands over to it, and it then loads this
project's code fresh from disk while the old server stops. The standby has
also opened the server's database connections already (4, to the active saved
connection; password logins only), checks them with a ping every minute and
hands them over, so the first answer doesn't wait ~1–1.5 s for a connection:
`restart_server` → Workspace usable in ~0.85 s (was ~1.9 s; ~6 s without a
standby). It keeps them at most an hour (then the server connects at start, as
before); `SINGLESTORE_MCP_STANDBY_CONNECT=0` turns this off. The standby is an
idle Python process (~25–90 MB); if packages were installed or removed since it
loaded them, it steps aside and a fresh server starts.
`SINGLESTORE_MCP_NO_STANDBY=1` turns it off. Any server start also opens its
database connections while the code loads, so the first tool call doesn't wait
for them. On Windows the relay starts the servers with the base Python directly
(as `multiprocessing` does), not through the virtual environment's launcher, so
each Claude session runs four Python processes instead of six.

If the server exits on its own — typically a syntax or import error in code
you just changed — the relay starts it again after a back-off (1, 2, 5, 10,
20, then every 30 s; the count starts over once a server has run for a
minute) instead of in a tight loop. It logs the error (the end of the
traceback) to the MCP server log, and answers tool calls at once with that
error instead of letting them wait. Fix the code and call `restart_server`
(the relay answers it itself, so it works while the server is down): it starts
the server right away, or reports the new error.

## SingleStore Workspace

In hosts that support [MCP Apps](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp)
(e.g. Claude), the server's apps open as interactive windows in the chat. In
other hosts the same tools return a normal text result.

The apps are combined in the **SingleStore Workspace**, shown to customers as **SAS SpeedyStore Studio** (the name
and mark at the top of the rail, the browser tab's title). A slim rail on the left switches between its views:

| View | What it's for |
|---|---|
| **SQL** | [SQL Editor](#sql-editor): write and run SQL, with autocomplete and Claude |
| **Notebook** | [Notebook](#notebook): SQL and Python cells on a Jupyter kernel |
| **Schema** | [Schema Explorer](#schema-explorer-pipeline-monitor-cluster-monitor-query-grid): databases, tables, columns, keys |
| **Pipelines** | [Pipeline Monitor](#schema-explorer-pipeline-monitor-cluster-monitor-query-grid): state, progress and errors of pipelines |
| **Cluster** | [Cluster Monitor](#schema-explorer-pipeline-monitor-cluster-monitor-query-grid): CPU, memory, disk per node, running queries, health, metrics history, backups |
| **Connect** (bottom of the rail) | [Connections](#connections): saved SingleStore connections and the active one |

**Opening it**

- Ask Claude ("open the SingleStore workspace"), or use `/singlestore-workspace`
  (optionally with a database and view, e.g. `/singlestore-workspace SASDP notebook`).
- The tool behind it is `sql_editor(database?, sql?, view?, table?, new_tab?)`;
  `view` is `sql`, `notebook`, `schema`, `pipelines`, `cluster`, `history`, `security`, `config`, `alerts`,
  `workload` or `connections`. `sql` goes
  into the SQL Editor's active tab, or a new tab with `new_tab=true`.
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

**Where code runs.** Every place that runs SQL or code says where, in words and colour (a chip with an icon; on
narrow windows just "S2" / "Compute" / "CAS" with the full text as tooltip):

- *Before*: the Run button says the target, e.g. **▶ Run on SingleStore · prod**, **▶ Run in SAS Compute · Zeus**,
  **▶ Run in CAS · Zeus** (SingleStore: the active connection; SAS: the tab's SAS Viya connection). *Run as* has a
  visible label. The Workload's Run button says the same.
- *While it runs*: "Running on SingleStore…", "Running in SAS Compute…", "Running in CAS…".
- *After*: the result's status line, the SQL Editor's run history and the Notebook cell's status start with a chip
  **SingleStore · prod** / **SAS Compute · Zeus** / **CAS · Zeus**. PROC SQL / FedSQL / CAS on a SingleStore library
  add the pushdown outcome next to it: "→ pushed down to SingleStore: full / partial / none".
- *Notebook*: each cell's header shows its engine: SingleStore (SQL), SAS Compute (SAS), Python (local).
- *Query History*: every row ran on SingleStore; rows from SAS keep their Source (SAS EP / SAS CAS / SAS
  in-database) and say "ran on SingleStore, sent by CAS / SAS Compute / SAS (embedded process)".

Colours (shared.css, ≥ 4.5:1 in light and dark, checked by `tests/test_contrast.py`): SingleStore blue, SAS Compute
orange, CAS indigo, Python slate; the *Run as* languages use the same ones (PROC SQL / FedSQL orange, plain SAS teal).

**Presenter mode.** **Present** at the bottom of the rail (or `&present=1` / `&present=0` on a browser link) switches
it for every view; the browser remembers it. Text is ~20 % larger, and identifying metadata is replaced with neutral
labels, the same label for the same value in every view while the server runs: connection names → "Connection A",
hosts and IP addresses → "host-1", user names and e-mail addresses → "user1", SAS Viya addresses → "viya-1". The
labels come from the server (`presenter_labels`): saved SingleStore and SAS Viya connections, this computer's user and
host name, the cluster's user list, plus e-mails / IPs / host names a page finds. Only what's on screen changes (text,
tooltips, labels; form fields holding such a value are blurred until focused); what the views send stays real. Not
masked: the SQL / code editors and query results (a result cell is replaced only when it is exactly one of the known
values). Claude's context still has the real names (it's told presenter mode is on).

**The Studio's own queries.** The Workspace's housekeeping (the monitors, alert checks, schema and session reads) is
hidden from Cluster → Queries and Query History unless **Show Studio's own queries** is ticked (remembered per
browser). They are the statements marked `/* s2-… */`, and — because the plan cache drops comments — system-view reads
and session checks (`SELECT 1`, `NOW()`, `@@variables`, `SHOW`), which other tools send too.

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
- **▶▶ Run all** (Ctrl+Shift+Enter) runs every statement in the editor in
  order, split the same way as Ctrl+Enter. Statements that change data are
  listed and confirmed once for the whole run. Each statement gets a tab with
  its status, rows and time; the last result set is shown. Run all stops at
  the first error unless **Continue on error** is ticked. `USE db` switches the
  editor's database.
- **Big results stream.** A SingleStore read shows its first 1,000 rows as soon
  as they arrive (a `LIMIT 100000` no longer waits for all 100,000); the rest
  keeps loading on the server while the status line shows `1,000+ rows ·
  loading…`, and the total when it's done. Scrolling to the end of the grid
  loads the next 5,000 rows. **Stop** (while running or loading) cancels the
  query on the cluster (`KILL QUERY` on its own connection, after checking on
  `PROCESSLIST` that it still runs this statement); the rows so far stay.
  The editor keeps at most 100,000 rows (about 128 MB) of one result
  (`SINGLESTORE_MCP_EDITOR_MAX_ROWS`); a bigger one is marked *cut off at …*
  and the statement is ended. Confirmed writes stream the same way (a statement
  that returns rows pages like a SELECT; a DML shows its affected rows): they're
  sent once, never retried after a dropped connection, and run without the
  session row limit. SAS results page too (see *SAS* below).
- Results export to CSV: **Export CSV** first loads every row of the result
  (up to that limit) and writes all of them; sorting a column also loads the
  rest first, so it sorts the whole result. The chart uses the rows loaded.
- **Chart** (next to *Table* above the results) charts any result: bar
  (grouped or stacked), line, area, scatter or pie. It picks X and Y for you
  (a date or text column on X, the numeric columns on Y); you can change them,
  combine repeated X values (sum, average, count, min, max), split one measure
  into series by a column, sort, hide series in the legend and download the
  chart as SVG. **Style** picks the look: SingleStore, SAS, Classic,
  Colorblind-safe, Pastel, Ocean or Dark (remembered for all charts). Re-running the same query keeps the chart. Drawn as plain SVG,
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
- **Recent** (next to Open) and the top of the Open panel list the last 10
  `.sql` files you opened or saved; one click opens one. Files that no longer
  exist are left out.

**Files on SAS Viya**

The Open / Save as panel has three locations at the top: **This computer** (the
above), **SAS Content** and **SAS Server**. Both Viya locations use the active SAS
Viya connection (Connections → SAS Viya, or the Viya picker in a SAS tab).

- **SAS Content** – the Folders and Files services, as in SAS Studio's *Content*
  tree: starts in **My Folder**, shortcuts **Public** and **All folders** (the
  root folders you can see).
- **SAS Server** – the SAS Compute server's file system, as in SAS Studio's *SAS
  Server* tree, through the Workspace's own SAS session (the Compute API's file
  endpoints, so nothing runs as SAS code): starts in the compute context's
  lockdown path (e.g. a landing zone), shortcuts **Home** and **Root (/)**. The
  first use starts the SAS session (~30 s).
- Folders, `.sql` / `.sas` files with size and modified time; **All files** shows
  every file; **New folder** (Save as) creates one; **Connections…** goes to the
  sign-in.
- A file opened from Viya opens in a tab like a local one (`.sas` → PROC SQL,
  `.sql` → SingleStore SQL) and the tab remembers where it came from (connection,
  location and path), also after a restart; **Save** writes it back there and
  **Recent** lists it (*name (SAS Content)*).
- **Save** sends the version you opened (the file's ETag): if someone saved a
  newer version on Viya meanwhile (e.g. in SAS Studio), nothing is overwritten –
  the editor asks: **Overwrite** / **Save as…** / **Cancel**. Save as onto an
  existing file asks **Replace it**. A SAS Content file keeps its media type (a
  SAS Studio program stays a SAS program); a new `.sas` file is saved as one.
- The **Notebook**'s Open / Save as has the same three locations: a notebook on
  SAS Content or the SAS server is a `.ipynb` file (SAS Content stores it as
  `application/x-ipynb+json`, up to 20 MB with outputs), with the same version
  check on Save. The **Security** view's *Save as .sql…* opens the same file
  browser, so a DBA script can go straight to SAS Content.

**Rename and delete in the file browser**

Hover a file or folder in Open / Save as (This computer, SAS Content, SAS Server;
SQL Editor, Notebook, Security) for **Rename** and **Delete**:

- One item at a time, in the folder shown: a rename keeps it in its folder (a
  file without an extension in the new name keeps its own). **Delete** asks
  first; a folder is only deleted when it's empty and after you type its name.
- On this computer only the files the browser lists (`.sql` / `.sas`, or
  `.ipynb`) and folders are touched, and a delete is permanent (no Recycle Bin).
  The browser's roots and shortcut folders (user folder, SQL folder, Documents,
  Desktop, Downloads, `SINGLESTORE_MCP_FILE_ROOTS`), My Folder, top-level SAS
  Content folders, the SAS server's home and lockdown folders – and the folders
  above them – are never renamed or deleted.
- Version checks: the item's ETag is sent as `If-Match` (SAS Content and SAS
  Server), and a file whose modified time changed since the list was read is
  left alone (*changed since the list was read – Refresh*).
- SQL Editor tabs that have the file (or a file in the renamed folder) open
  follow it to the new path, also tabs not loaded right now and **Recent**; a
  renamed SAS Content file's new version is handed to the tab, so Save keeps
  working. A **deleted** file's tab keeps its text as unsaved (● untitled). The
  Notebook does the same for its file. Backend: `files_rename` / `files_delete`
  and `viya_files_rename` / `viya_files_delete` (app-only).
- If the active SAS Viya connection changes, a tab with a file from another
  connection shows *⚠ on Viya &lt;name&gt;*, and Save asks whether to save it to
  the same path on the active connection instead.
- Errors say what to do: not signed in → sign in under Connections; no
  permission; folder or file not found; changed on the server.
- Restored tabs don't re-read their Viya file (that could start a SAS session for
  every tab); Save's version check catches a newer version.

**Tabs**

- In the Workspace the editor has tabs, like the notebooks: **＋** (or
  **New**, or **Ctrl+Alt+N**) opens one; each tab has its own text, database,
  open file, cursor, results, *Continue on error* and Claude chat.
- A tab is labelled with its file name or *untitled N*, with ● while it has
  unsaved changes. Double-click a tab to give it your own title; drag it to
  move it. **×** closes it; a tab with unsaved changes asks first (click
  **Discard ×**). Closing the last tab leaves one empty tab.
- **Ctrl+Tab** / **Ctrl+Shift+Tab** switch to the next / previous tab where the
  browser lets the page have those keys; **Ctrl+Alt+PageDown** / **PageUp**
  do the same. (Ctrl+T and Ctrl+W belong to the browser.)
- **Open** and **Recent** open a file in a new tab when the current one has
  text, and switch to the tab that already has the file open. **Save** and
  **Save as** apply to the current tab.
- SQL from Claude or other views goes into the active tab.

**Autosave and history**

- Each tab saves its text, database, cursor and open file a second after you
  stop typing. The tabs come back in the same order, with the same active
  tab, when the SQL Editor opens again, also after a restart or in the
  browser window. Drafts are kept in `~/.singlestore-mcp/sql-editor/`
  (`tabs.json` plus `drafts/<tab>.json`), never in your `.sql` file: an open
  file shows **● modified** until you Save. If the file changed on disk
  meanwhile, the editor says so and keeps your changes. (The single
  `draft.json` from before tabs becomes the first tab once and is left in
  place as a backup.)
- SQL from Claude or other views (*Query in grid*, *Top 100*) is added below
  the active tab's text and selected, never replacing it.
- **History** lists the last 500 statements you ran in the editor (all tabs),
  with time, database, the tab or file it ran from, duration, rows and
  ok/error, also after a restart. Filter it, **Insert** a statement at the
  cursor or **Run** it again in its database; **Clear…** empties it.

**→ Workload**

- **→ Workload** in the toolbar sends the selection, else the statement at the
  cursor (or, by choice, every statement in the tab) to a test workload: pick
  an existing workload or **New workload…** (give it a name) and the runs per
  minute (default 60). The tab's database goes with it. A `-- name: …` or
  `-- per_minute: N` line right above a statement names it or sets its rate.
- In the Workspace the Workload view opens with the statements added but not
  saved, so you review them and **Save**; outside the Workspace the Workload app
  opens in your browser with them. Claude's answers in the chat panel have a
  **→ Workload** button on each SQL block too.
- A tab opened from the Workload view (**Open in SQL Editor**) has an **Update
  in workload** button; see [Workload](#schema-explorer-pipeline-monitor-cluster-monitor-query-grid).

**Building SELECTs**

- In the schema tree, **SELECT columns** on a table inserts a SELECT listing
  every column.
- Expand a table to tick columns (Shift+click for a range, **All** / **None**)
  and click **Insert SELECT** for just those, in table order.
- With the cursor on the `*` of `SELECT *` (or `t.*`), **Ctrl+Space → Expand \***
  replaces it with the column list, qualified by alias when the query joins
  several tables.
- Nothing runs automatically.

**Chat with Claude**

- The **Claude** button opens a chat panel on the right.
- Claude sees the editor's SQL and last result, checks the database
  (read-only), and answers with SQL that you **Replace**, **Insert** or
  **Copy** into the editor with one click. If the editor is empty, the
  answer's SQL goes straight in.
- **Answer here in the editor** (default) uses the
  [in-app assistant](#claude-in-the-apps); **in the Claude chat** sends the
  question to the chat box instead (you click Send there).

**SAS: Run as PROC SQL, FedSQL or FedSQL in CAS**

The editor also runs SQL in SAS on your SAS Viya (the active SAS Viya
connection in [Connections](#connections), or the one the tab is pinned to), so
the same SQL can be tried SingleStore-native and through SAS side by side.

- **Run as** (next to the database picker) picks what the tab's SQL runs as:
  **SingleStore SQL**, **PROC SQL** or **FedSQL** (SAS libraries, in the
  Workspace's SAS Compute session) or **FedSQL in CAS** (caslibs). The picker
  next to it then lists databases, SAS libraries or caslibs; **Ctrl+Shift+L**
  cycles the languages. Each tab keeps its language (with its library and
  options) in its draft; `.sas` files open as PROC SQL, `.sql` files as
  SingleStore SQL. The language is never guessed from the SQL.
- You write only the SQL: the editor wraps it (`proc sql; … quit;`,
  `proc fedsql; … quit;`, CAS `fedSql.execDirect`). A SELECT becomes
  `CREATE TABLE WORK._S2R_<tab> AS …` (each tab has its own result table;
  CASUSER for CAS), whose rows fill the result grid with their SAS formats
  applied (dates as dates). The first 1,000 rows come with the result; the rest
  load as you scroll (Compute data API paging / CAS `table.fetch` from–to),
  with the total in the status line; sorting and **Export CSV** load them all.
  PROC SQL keeps up to 100,000 rows in the result table (OUTOBS; a bigger
  result is marked *cut off*). A result can be paged until the tab runs
  something else. Statement splitting,
  **Ctrl+Enter** and **Run all** work as for SingleStore SQL (SAS rules: no
  `#` comments or backticks).
- Text that starts with `proc`, `data`, `%`, `libname`, `options` … runs as
  **plain SAS code**, unwrapped: the statement at the cursor takes the whole
  step through its `run;` / `quit;`. Its printed output shows in the result
  pane.
- Every Compute run starts with `options obs=max replace nosyntaxcheck;
  %let syscc=0;`: a failed step (a DATA step reading a table that doesn't
  exist, say) put the session in SAS's syntax check mode (OBS=0) with `&SYSCC`
  set, and every later run in it then ended *SAS reported an error* without
  output until the session was restarted.
- PROC SQL: the library you pick is used for one-level names
  (`options user=`); without one they're WORK tables, as in SAS. FedSQL
  one-level names are WORK tables; in CAS they're in the picked caslib
  (default CASUSER).
- **⚙** (SAS options, per tab): PROC SQL **OUTOBS** (default: 100,000 rows
  kept for paging), **NOEXEC** and **FEEDBACK**, **Show pushdown** (on by
  default), the tab's **SAS Viya connection** and **Restart SAS session**.
- **Long runs and Stop.** A SAS run goes to the background on the server; the
  status line shows *Running in SAS… 1 min 12 s* and **Stop**. Stop cancels the
  job (Compute: the documented `PUT …/jobs/{id}/state?value=canceled` with the
  job's ETag, or the session's running code if Viya refuses that; CAS: `DELETE
  …/cas/sessions/{id}/actions`, CAS's cancelAction): the run ends **stopped**
  with the SAS log up to there, and the SAS session keeps running (libraries,
  WORK and the startup code stay). If SAS doesn't stop the run (or it still
  runs 30 s after Stop), the status line offers **Restart SAS session**, which
  ends that connection's SAS sessions. A single job may run 4 hours
  (`SINGLESTORE_MCP_SAS_TIMEOUT_MIN`, minutes; `0` = no limit; it was 15
  minutes).
- **Several tabs.** Each SAS Viya connection has one Compute and one CAS
  session, shared by the tabs that use it, running one job at a time. A run
  that has to wait says so: *Waiting for the SAS run in tab “Report A”
  (running 2 min)…*; it can be stopped while it waits (nothing reaches SAS).
  Runs on another connection (or in CAS while Compute is busy) don't wait.
- **SAS session started in the background.** A cold SAS session (Compute session,
  automatic SingleStore library, startup code) takes ~15–50 s (on Zeus mostly
  Viya starting the Compute server: ~35–42 s for `POST …/sessions`, then ~6–8 s
  for the first job). So when the Workspace opens, it starts the active SAS Viya
  connection's sessions in the background (`sas_prewarm`, as soon as Claude or
  the browser link opens the Workspace – the `sql_editor` call itself – and
  again from the page ~1.5 s after its first paint; Compute first, then
  CAS), and again when another connection becomes the active one, when the
  active one is saved or signed in to. The header of the SQL Editor's SAS tabs
  and of the Schema Explorer's SAS sources shows **SAS session: starting… /
  ready / not signed in / SAS Viya unreachable**. A run or library list that
  comes while it starts waits for that same session (*Waiting for the SAS
  session to start…*) instead of starting a second one. Nothing is asked: a
  connection that isn't signed in, or a Viya that can't be reached (VPN off),
  is skipped and the reason shown. While the Workspace is open it asks every
  4 minutes; the server then reads the session's state (`GET
  /compute/sessions/{id}/state`, no SAS code; this resets Viya's idle timeout,
  `sessionInactiveTimeout`, 15 minutes on Zeus) and the CAS session, and starts
  a new one if Viya ended it anyway. Close the Workspace and the checks stop,
  so Viya ends the session after its idle timeout. Tabs pinned to another SAS
  Viya connection aren't warmed (they show its state); the SAS 9 sign-on still
  waits until the SAS 9 source is opened. Off per connection (Connections → SAS
  Viya → Edit → *Start the SAS session when the Workspace opens*) or for the
  server with `SINGLESTORE_MCP_SAS_PREWARM=0`.
- **Per-tab SAS Viya connection.** A tab can be pinned to a saved SAS Viya
  connection (⚙, or the *Viya:* picker in the header when there are several):
  its runs, tree and autocomplete then use that connection's own sessions;
  *Viya: active (…)* follows the active connection. The pin is saved with the
  tab; switching the active connection elsewhere doesn't touch a pinned tab.
- The result pane has **Result**, **Log** (the generated SAS code with
  **Copy code**, then the SAS log with errors, warnings and notes in colour)
  and **Pushdown** tabs. Colour badges show the language everywhere: S2 blue,
  PROC SQL / FedSQL orange, CAS indigo, SAS (plain code) teal, also in the run
  history; the Run button names the engine (*Run in SAS Compute · Zeus*, see
  [Where code runs](#singlestore-workspace)). The badge in the editor's corner is gone (it covered the first line).
- **Writes**: reads run straight away. Statements that write anywhere but
  WORK (Compute) or CASUSER (CAS) ask first, as do plain SAS steps that write
  outside WORK or run system commands, `proc datasets`, `caslib` statements
  and the like. Writes to a shared caslib get an extra warning.
- **Looks wrong?** The editor asks before running SQL that looks like another
  language, e.g. a SAS library (`s2.cars`) or a `proc` step while the tab
  runs SingleStore SQL, backticks in a SAS tab, or `LIMIT` in PROC SQL:
  **Run as …** instead, or **Run anyway**.
- **Autocomplete** in SAS tabs suggests libraries / caslibs, their tables
  (typing `lib.` loads its tables) and columns – also of tables you haven't
  expanded in the tree. When the SQL names a table the editor doesn't know
  yet, it fetches its columns in the background and shows them when they
  arrive (typing never waits; the first SAS use starts the session, ~35 s):
  - after `lib.table.`, after an alias (`select c. from s2.cars c` → the CARS
    columns), after a table named in the statement (`cars.`), and for
    unqualified names (the columns of every table in `FROM` / `JOIN`);
  - quoted names work: `"My caslib"."My table"`, `'a b'n`;
  - one-level names: PROC SQL uses the tab's library (else WORK), FedSQL in
    CAS the tab's caslib (else CASUSER); FedSQL one-level names are WORK
    tables.
  - Round trips stay few: lookups wait until you pause (~0.35 s), every
    unknown table of the statement goes in one call (up to 8), each table is
    looked up once and cached per SAS Viya connection + library + table, and
    a table that doesn't exist is remembered for a minute. The cache is
    dropped when the active Viya connection changes, when the SAS session
    restarts, with the tree's **↻** and with **Restart SAS session** (⚙).
  - Backend: `sas_lookup_columns(language, tables)` (app-only, read-only):
    Compute tables in parallel through the Compute data API (a miss is tried
    once more with the library's own spelling of the name), caslib tables
    through `table.columnInfo`.
- The history keeps the language with each statement; **Run** runs it again
  as that language.
- **The tree** lists SingleStore databases, SAS libraries or caslibs
  (**SingleStore / SAS / CAS** at its top; it follows the tab's language until
  you switch it). Using something from a source the tab doesn't run – choosing a
  library or database, inserting a name, **SELECT columns**, **Top 100** –
  switches the tab's language (a SAS library → PROC SQL, or FedSQL if the tab
  runs FedSQL; a caslib → FedSQL in CAS; a database → SingleStore SQL): at once
  while the tab is empty, otherwise only after **Switch to …** (**Keep …**
  leaves it), so a tab's SQL is never re-labelled silently.

**Show pushdown**

With **Show pushdown** on, a PROC SQL run adds
`options sastrace=',,,d' sastraceloc=saslog nostsuffix sql_ip_trace=(note, source);`
(and turns it off again after), FedSQL adds `_method`, and CAS FedSQL runs with
`method=true`. The **Pushdown** tab then shows the SQL SingleStore received
(metadata probes such as `WHERE 0=1` left out; parallel read-back streams
counted) and a verdict:

- **Pushed down fully**: SingleStore did the work, SAS received the result;
- **Partly pushed down**: e.g. the WHERE clause or column list went to
  SingleStore, SAS did the rest;
- **Pulled into SAS**: the rows were read into SAS / CAS and processed there;
- **No SingleStore**: nothing was sent to SingleStore.

**FedSQL** doesn't write the SQL it sends to the SAS log (`sastrace` doesn't
cover it). The SAS/ACCESS driver trace does: the automatic SingleStore library
(below) is assigned with `driver_trace=SQL driver_traceoptions=APPEND
driver_tracefile="<WORK>/_s2trace.log"` (LIBNAME options for PROC FEDSQL / DS2).
After a FedSQL run the editor reads that file into `WORK._S2TRC` (not through the
log: long lines would wrap), deletes it, and the Pushdown tab lists the
statements as SingleStore got them (the `DEFAULT:` lines, names in their own
case; `WHERE 1=0` probes left out); the verdict still comes from the FedSQL
plan (`_method`) – except when the trace shows SingleStore got only plain table
reads: the plan says *Push Down* then too (a GROUP BY with `ORDER BY` / `LIMIT`,
`LIMIT` alone), so the verdict is *Pulled into SAS*. Libraries assigned elsewhere (startup code) have no trace:
for them FedSQL shows the plan's verdict only. Tried and not working for FedSQL:
`options sastrace=',,,d'` / `',,,ds'` (no trace lines) and
`%log4sas_logger(App.TableServices, 'level=trace')` (nothing in the log).

**Find in Query History** checks whether a statement SingleStore received is in
the cluster's plan cache or the local query history (with its runs and average
time) and opens the History view on it: searching for it when the History list
has its runs, otherwise (every run under the 1 s trace threshold – typical for
a pushed-down count) on its **plan-cache entry**, listed at the top, selected
and marked *from the SQL Editor*, with its totals, parameterized text and tuning.

**Ask Claude in a SAS tab**

In a tab that runs PROC SQL, FedSQL or FedSQL in CAS, **Ask Claude** answers in
that dialect (never SingleStore SQL), as bare statements the editor wraps (no
`proc sql; … quit;` unless you ask for a SAS program, e.g. explicit
pass-through, a DATA step or a macro). The question carries:

- the tab's language, library / caslib (and the SingleStore database behind
  it, so Claude can check real table and column names with its read-only
  tools), the library's tables (caslibs: loaded or not), the columns the editor
  already knows (tables in the editor's SQL first) and the tab's options;
- once per Claude session and language, a compact **SAS guide** for that
  language only (`sas_guide.py`, ~1.1–1.4 K tokens): how the editor wraps and
  splits statements, what may be written without asking (WORK / CASUSER), the
  dialect's rules and pitfalls, checked in SAS (see below), and the function
  lists from the function matrix (pushed down / runs in SAS / not available).

Pitfalls the guides cover (each one tried in the Workspace's SAS session):
PROC SQL has no `LIMIT` (OUTOBS in ⚙, or a plain `proc sql outobs=10;` step;
`(obs=)` limits the rows read), `CALCULATED`, remerging, date literals
`'01JAN2024'd`, explicit pass-through only as one plain SAS step
(`connect using S2;` reuses the library's connection – the editor's write check
treats `connect` / `disconnect` as reads, `execute … by` still asks).
FedSQL: one-level names are WORK (always `library.table`), SASHELP can't be read
(a concatenated library: copy it to WORK first), unquoted names are upper case so
lower-case SingleStore tables need `S2."cars_big"`, no `CALCULATED` / data set
options / CTEs / window functions / `catx`, dates as `DATE '2024-01-01'`
(`'01JAN2024'd` is a syntax error), `LIMIT … OFFSET` – which FedSQL never sends
to SingleStore (the driver trace shows a plain table read; a GROUP BY with
`ORDER BY` / `LIMIT` isn't pushed either), so `select * from S2."cars_big" limit 5`
reads all 20 M rows into SAS and took the test's Compute session down; PROC SQL's
`(obs=5)` does go down as `LIMIT` – , `DROP TABLE … FORCE`
instead of `{options replace=true}` (CAS only), and on a SingleStore library no
subquery in `WHERE` (`x > (select avg(x) …)` / `in (select …)`: FedSQL pushes the
editor's whole `CREATE TABLE WORK… AS` to SingleStore → *Unknown database
'WORK'*; a join with a derived table works and is pushed down). FedSQL in CAS: only SELECT /
CREATE TABLE AS / DROP TABLE, no CTEs, `ORDER BY` only with `LIMIT` (the result
is a CAS table), only tables loaded in CAS (a SingleStore caslib's unloaded source
table "does not exist"; a session-scope load from plain SAS code lands in another
CAS session), `create table CASUSER.x {options replace=true} as …`.

Measured with 20 realistic questions per language (`top 10 makes by average
MSRP`, `join with a WORK table`, `count per month`, `create a table in WORK /
CASUSER`, `date filter`, `explicit pass-through` …), the generated SQL run in
SAS against SASHELP, the S2 library, PUBLIC / CFANS caslibs and WORK / CASUSER
(Sonnet, *Balanced*):

<!-- sas-assistant-results:begin -->
| Tab language | Before (0.10.1) | After |
|---|---|---|
| PROC SQL | 8 / 20 | 20 / 20 |
| FedSQL | 0 / 20 | 20 / 20 |
| FedSQL in CAS | 3 / 20 | 20 / 20 |
| Plain SAS (asked in a PROC SQL tab) | 4 / 20 | 20 / 20 |

"Pass" = every SQL block of the answer ran without a SAS error and without
asking for a write confirmation (each answer in a fresh SAS session; runs that
failed only because Viya couldn't start a session were run again). Typical
mistakes before: `LIMIT` in PROC SQL, one-level names in FedSQL (WORK, not the
tab's library), `FETCH FIRST` / CTEs / `ORDER BY` without `LIMIT` in CAS,
`catx` / window functions in FedSQL, tables created in the SingleStore database
`MyDB` instead of WORK, SingleStore SQL (`information_schema`) in a SAS tab.
<!-- sas-assistant-results:end -->

**SAS function matrix**

[`data/sas_functions.json`](src/singlestore_mcp/data/sas_functions.json) says,
per function, whether PROC SQL, FedSQL and FedSQL in CAS have it and whether
it is **pushed down** to SingleStore, its SingleStore equivalent and what to use
instead. Sources: SAS/ACCESS to SingleStore's own function map (the LIBNAME
options `SQL_FUNCTIONS_COPY=` – default list – and `SQL_FUNCTIONS=ALL`, dumped
from Zeus), the FedSQL / CAS FedSQL / PROC SQL references on
documentation.sas.com, and a run of every function in SAS
(`scripts/sas_functions.py --verify`: PROC SQL and FedSQL on `S2.CARS` /
`S2.PRDSAL`, FedSQL in CAS on the SpeedyStore table `CFANS.WORKFORCE_DATA`;
the Pushdown verdict decides "pushed down"). It's used in three places:

- the assistant's SAS guide (prefer functions that push down, say when one
  doesn't, suggest the alternative);
- the editor's pre-run check: before a SAS statement runs, **Check the
  functions** lists functions the tab's dialect hasn't got and – when the
  statement reads a SingleStore library / caslib – functions that stop pushdown
  (*Run anyway* / *Cancel*; `vendor/sas-funcs.js`, the same rules as
  `sas_functions.check`);
- this table (`scripts/sas_functions.py --table` rewrites it from the JSON).

✓ ↓ supported and pushed down · ✓ supported, runs in SAS / CAS · ✓ (↓) CAS:
the SAS Embedded Process does part of it (e.g. the grouping) · ✓ (ALL) PROC SQL
pushes it only with `SQL_FUNCTIONS=ALL` on the library · ✗ not available ·
\* from the documentation, not run.

<!-- sas-functions:begin -->
| Function | Category | PROC SQL | FedSQL | FedSQL in CAS | SingleStore | Instead |
|---|---|---|---|---|---|---|
| `COUNT` | aggregate | ✓ ↓ | ✓ ↓ | ✓ (↓) | COUNT |  |
| `COUNT(DISTINCT)` | aggregate | ✓ ↓ | ✓ ↓ | ✓ (↓) | COUNT(DISTINCT) |  |
| `SUM` | aggregate | ✓ ↓ | ✓ ↓ | ✓ (↓) | SUM |  |
| `AVG` | aggregate | ✓ ↓ | ✓ ↓ | ✓ (↓) | AVG |  |
| `MEAN` | aggregate | ✓ ↓ | ✓ ↓ | ✓ (↓) | AVG | AVG |
| `MIN` | aggregate | ✓ ↓ | ✓ ↓ | ✓ (↓) | MIN |  |
| `MAX` | aggregate | ✓ ↓ | ✓ ↓ | ✓ (↓) | MAX |  |
| `STD` | aggregate | ✓ | ✓ ↓ | ✓ (↓) | STDDEV_SAMP | PROC SQL: not pushed down - VAR (pushed down) and sqrt() it, or explicit pass-through with STDDEV_SAMP. FedSQL / CAS: STD or STDDEV |
| `STDDEV` | aggregate | ✗ | ✓ ↓ | ✓ (↓) | STDDEV_SAMP | PROC SQL: STD |
| `VAR` | aggregate | ✓ ↓ | ✗ | ✗ | VAR_SAMP | FedSQL / CAS: VARIANCE |
| `VARIANCE` | aggregate | ✗ | ✓ ↓ | ✓ (↓) | VAR_SAMP | PROC SQL: VAR |
| `N` | aggregate | ✓ ↓ | ✓ ↓ | ✓ (↓) | COUNT | COUNT(x) |
| `NMISS` | aggregate | ✓ ↓ | ✓ | ✓ (↓) | SUM(x IS NULL) |  |
| `MEDIAN` | aggregate | ✓ | ✗ | ✗ | MEDIAN | PROC SQL only (runs in SAS); in FedSQL / CAS MEDIAN is a row function, not an aggregate |
| `RANGE` | aggregate | ✓ ↓ | ✓ | ✓ (↓) | MAX(x) - MIN(x) |  |
| `ABS` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | ABS |  |
| `CEIL` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | CEILING |  |
| `FLOOR` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | FLOOR |  |
| `ROUND` | numeric | ✓ (ALL) | ✓ ↓ | ✓ ↓ | ROUND | PROC SQL on SingleStore: ROUND isn't pushed down (only with SQL_FUNCTIONS=ALL); FLOOR(x * 100 + 0.5) / 100 is |
| `INT` | numeric | ✓ | ✗ | ✗ | TRUNCATE(x, 0) | FLOOR (non-negative values) or CASE; FedSQL / CAS have no INT |
| `MOD` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | MOD |  |
| `SQRT` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | SQRT |  |
| `EXP` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | EXP |  |
| `LOG` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | LOG |  |
| `LOG10` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | LOG10 |  |
| `SIGN` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | SIGN |  |
| `POWER` | numeric | ✗ | ✓ ↓ | ✓ ↓ | POWER | PROC SQL: x ** 2 |
| `SIN` | numeric | ✓ ↓ | ✓ ↓ | ✓ ↓ | SIN |  |
| `UPCASE` | character | ✓ ↓ | ✓ ↓ | ✓ ↓ | UCASE | FedSQL / CAS also have UPPER |
| `UPPER` | character | ✓ ↓ | ✓ ↓ | ✓ ↓ | UPPER | PROC SQL: UPCASE |
| `LOWCASE` | character | ✓ ↓ | ✓ ↓ | ✓ ↓ | LCASE |  |
| `LOWER` | character | ✓ ↓ | ✓ ↓ | ✓ ↓ | LOWER |  |
| `SUBSTR` | character | ✓ (ALL) | ✓ ↓ | ✓ ↓ | SUBSTRING | PROC SQL on SingleStore: not pushed down (only with SQL_FUNCTIONS=ALL); filter with LIKE 'abc%' or use explicit pass-through |
| `SUBSTRING` | character | ✓ | ✓ ↓ | ✓ ↓ | SUBSTRING | PROC SQL: SUBSTR |
| `LENGTH` | character | ✓ (ALL) | ✓ ↓ | ✓ ↓ | LENGTH | PROC SQL on SingleStore: not pushed down (only with SQL_FUNCTIONS=ALL) |
| `TRIM` | character | ✓ | ✓ ↓ | ✓ ↓ | TRIM | PROC SQL on SingleStore: STRIP (pushed down as TRIM) |
| `STRIP` | character | ✓ ↓ | ✓ ↓ | ✓ ↓ | TRIM |  |
| `TRIMN` | character | ✓ ↓ | ✓ ↓ | ✓ ↓ | RTRIM |  |
| `LEFT` | character | ✓ | ✓ ↓ | ✓ ↓ | LTRIM | PROC SQL on SingleStore: STRIP (pushed down) |
| `COMPRESS` | character | ✓ (ALL) | ✓ ↓ | ✓ ↓ | REPLACE | PROC SQL on SingleStore: TRANWRD(x, ' ', '') (pushed down as REPLACE) |
| `TRANWRD` | character | ✓ ↓ | ✓ ↓ | ✓ ↓ | REPLACE |  |
| `INDEX` | character | ✓ ↓ | ✓ ↓ | ✓ ↓ | LOCATE |  |
| `FIND` | character | ✓ | ✓ ↓ | ✓ ↓ | LOCATE / INSTR | INDEX (PROC SQL: pushed down as LOCATE) |
| `SCAN` | character | ✓ | ✓ ↓ | ✓ ↓ | SUBSTRING_INDEX | explicit pass-through with SUBSTRING_INDEX |
| `CATX` | character | ✓ | ✗ | ✗ | CONCAT_WS | Not in FedSQL / CAS: trim(a) \|\| ' - ' \|\| trim(b). PROC SQL on SingleStore: a \|\| ' - ' \|\| b pushes down (catx() doesn't) |
| `CATS` | character | ✓ | ✗ | ✗ | CONCAT(TRIM(..)) | trim(a) \|\| trim(b); PROC SQL on SingleStore: a \|\| b pushes down |
| `\|\|` | character | ✓ ↓ | ✓ ↓ | ✓ ↓ | CONCAT | FedSQL / CAS keep CHAR padding: trim(a) \|\| trim(b) |
| `REPEAT` | character | ✓ (ALL) | ✓ ↓ | ✓ ↓ | REPEAT |  |
| `SOUNDEX` | character | ✓ (ALL) | ✗ | ✗ | SOUNDEX | PROC SQL only |
| `REVERSE` | character | ✓ | ✓ ↓ | ✓ ↓ | REVERSE |  |
| `PROPCASE` | character | ✓ | ✓ ↓ | ✓ ↓ | - |  |
| `TRANSLATE` | character | ✓ | ✗ | ✗ | - | PROC SQL only; FedSQL / CAS: nested REPLACE-style tranwrd() |
| `COALESCEC` | character | ✓ | ✓ ↓ | ✓ ↓ | COALESCE | COALESCE (PROC SQL: pushed down) |
| `TODAY` | date | ✓ ↓ | ✓ ↓ | ✓ | CURDATE() | CAS: compare with a DATE literal (TODAY() keeps the query in CAS) |
| `YEAR` | date | ✓ ↓ | ✓ ↓ | ✓ ↓ | YEAR |  |
| `MONTH` | date | ✓ ↓ | ✓ ↓ | ✓ ↓ | MONTH |  |
| `DAY` | date | ✓ ↓ | ✓ ↓ | ✓ ↓ | DAYOFMONTH |  |
| `QTR` | date | ✓ ↓ | ✓ ↓ | ✓ ↓ | QUARTER |  |
| `WEEKDAY` | date | ✓ ↓ | ✓ ↓ | ✓ ↓ | DAYOFWEEK |  |
| `INTNX` | date | ✓ | ✓ ↓ | ✓ ↓ | DATE_ADD | PROC SQL on SingleStore: filter with date literals, group with YEAR() / MONTH() (pushed down) |
| `INTCK` | date | ✓ | ✓ ↓ | ✓ ↓ | TIMESTAMPDIFF / PERIOD_DIFF | PROC SQL on SingleStore: YEAR() / MONTH() arithmetic, or explicit pass-through with TIMESTAMPDIFF |
| `MDY` | date | ✓ | ✓ ↓ | ✓ ↓ | MAKEDATE | a date literal ('01JAN2024'd / DATE '2024-01-01') |
| `DATEPART` | date | ✓ (ALL) | ✗ | ✗ | DATE | PROC SQL only (FedSQL / CAS: CAST(x AS DATE)) |
| `EXTRACT` | date | ✗ | ✗ | ✗ | EXTRACT | YEAR() / MONTH() / DAY() |
| `CURRENT_DATE` | date | ✗ | ✓ ↓ | ✓ | CURRENT_DATE | PROC SQL: TODAY() |
| `PUT (date format)` | conversion | ✓ | ✓ | ✓ ↓ | DATE_FORMAT | group by YEAR(d), MONTH(d) (pushed down) and format the result |
| `COALESCE` | null | ✓ ↓ | ✓ ↓ | ✓ ↓ | COALESCE |  |
| `IFN` | null | ✓ | ✗ | ✗ | IF() | CASE WHEN … THEN … ELSE … END (pushed down everywhere) |
| `IFC` | null | ✓ | ✗ | ✗ | IF() | CASE WHEN … THEN … ELSE … END (pushed down everywhere) |
| `NULLIF` | null | ✗ | ✓ ↓ | ✓ ↓ | NULLIF | PROC SQL: CASE WHEN x = v THEN . ELSE x END |
| `MISSING` | null | ✓ | ✓ ↓ | ✓ ↓ | x IS NULL | x IS NULL (pushed down) |
| `PUT (number)` | conversion | ✓ | ✓ | ✓ ↓ | FORMAT | aggregate first, format in the result (format= in PROC SQL) |
| `INPUT` | conversion | ✓ | ✗ | ✗ | - | PROC SQL only; FedSQL / CAS: CAST(x AS DOUBLE) |
| `CAST` | conversion | ✗ | ✓ ↓ | ✓ ↓ | CAST | PROC SQL has no CAST: PUT / INPUT |
<!-- sas-functions:end -->

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
- **Interrupt** stops Python code at once, also a long `time.sleep()` on
  Windows. A call waiting in C code can't be interrupted on Windows (a
  network read, a lock, a long numpy / C loop such as `sum(range(10**10))`):
  if the cell still runs 5 s after ■, the cell offers **Restart kernel**, which
  stops it (variables are cleared).

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
- **Open** and **Save as** are a large dialog over the notebook, as in the SQL
  Editor; Escape or Close / Cancel closes it.
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

- Set up once in **Connect → SAS Viya connections**: **+ New Viya connection**
  (a name, the Viya address and the compute context, e.g. *SAS Studio compute
  context*; **Test** lists them), then **Sign in** on its card. Sign-in uses
  your own Viya account (OAuth with Viya's built-in `vscode` client): SAS Logon
  opens in the browser and shows a code to paste once; after that the sign-in
  renews itself. If you already signed in with the SAS Viya MCP server or the
  SAS Viya CLI, **Test** reuses that sign-in for a connection to the same Viya.
- SAS cells use the **active** SAS Viya connection. A new kernel picks up the
  connection that is active when it starts; restart the kernel after **Use**
  on another one.
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

**SAS Viya connections** (bottom of the Connect view) work like the SingleStore
ones: several saved connections, **one active**. SAS cells in notebooks, the
Schema Explorer's SAS libraries / CAS sources and the SQL Editor's Run as
PROC SQL / FedSQL / CAS use the active one.

- **+ New Viya connection** / **Edit**: name, Viya address, compute context,
  client ID (default `vscode`), certificate check, SingleStore libref and, for
  SAS in the Workspace, CAS server and startup code (below). **Save and use**
  also makes it the active one.
- Each card shows whether it's signed in (as whom, how long the token is
  valid, whether it renews itself) with **Use** (make it active), **Sign in** /
  **Sign in again**, **Test**, **Sign out**, **Edit** and **Delete**.
- **Sign in** only signs in: it opens SAS Logon for that saved connection and
  asks for the code; it never saves the form. Each connection keeps its own
  sign-in (tokens in the OS credential store, never shown to the app or
  Claude). Renaming a connection keeps its sign-in, changing its address to
  another Viya drops it, **Delete** removes it.
- **Use** on another connection ends the Workspace's SAS Compute / CAS sessions
  (the next run starts them on the new Viya); new notebook kernels use it.
  The Schema Explorer's SAS sources and the SQL Editor's SAS options (⚙, and
  next to the library picker when there are several) show the active Viya
  connection and switch it.
- The list (no tokens) is in `~/.singlestore-mcp/sas.json`. An older
  single-settings `sas.json` becomes one connection, named after the Viya host,
  that keeps its sign-in; one without an address becomes an empty list.

Per connection, for SAS in the Workspace:

- **CAS server** (default `cas-shared-default`).
- **Startup code**: SAS code run once when the Workspace's SAS session starts,
  typically `%include` of the files that assign your libnames, e.g.

  ```sas
  filename inc filesrvc folderpath='/Users/me/My Folder/mcp' filename='s2_libname.sas';
  %include inc / nosource2;
  filename inc clear;
  ```

  It runs with `options nosource nosource2` and its log is never shown or kept
  (only the number of errors and the first error line, passwords masked), so
  the libname files' contents stay hidden. Don't put passwords in the startup
  code itself. Saving a connection with other session settings (address,
  compute context, CAS server, startup code, SAS 9 sign-on) restarts that
  connection's SAS sessions; **⚙ → Restart SAS session** in the SQL Editor
  does too (for the tab's connection). Switching the active connection
  restarts nothing: every connection has its own SAS sessions.
- **Automatic SingleStore library** (on by default; a checkbox in Edit): when
  the Workspace's SAS session starts, it gets the **SingleStore libref** (default
  `S2`) on the active SingleStore connection and its default database – the
  same `LIBNAME S2 SSTORE server= port= user= password= database=` the notebook
  kernels use. It's assigned *before* the startup code (which can override it),
  and again before the next SAS run when the active SingleStore connection, its
  settings or its password change. The password comes from the credential
  store and is submitted with `options nosource nonotes`, so it never appears
  in the SAS log, the Log tab, the history or tool results (error text is
  masked; the job is deleted from the session afterwards). Token connections
  (JWT / Entra ID) use the token as the password and are re-assigned when it's
  renewed (Entra renews a few minutes before expiry); connections with a URL
  get no library. It shows in the SAS libraries as an **S2** library marked
  *automatic*, with its database. On Zeus this replaces the compute context's
  own `S2`; turn it off to keep that one.
- **Start the SAS session when the Workspace opens** (on by default; a checkbox
  in Edit): see *SAS session started in the background* below.
- **SAS 9 sign-on code**, **SAS 9 server**, **SAS 9 libraries** (optional): SAS 9
  through SAS/CONNECT, see *Schema Explorer: SAS 9 libraries* below.
- The Workspace keeps one SAS Compute session and one CAS session and reuses
  them (the first use starts the session, which can take half a minute).

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
| **Schema Explorer** `schema_explorer(database?, table?)` | Databases (with a bottomless / local icon) → tables with row counts and sizes; per table the columns, DDL (shard / sort keys), a row preview and its **Distribution** (data skew over partitions and leaves); **Check skew** for a whole database. **Ask Claude**, **Query in grid** and **Analyze…** (refresh statistics, with confirmation). |
| **Pipeline Monitor** `pipeline_monitor(database?)` | Every pipeline's state, source → table, progress (files loaded / Kafka lag), latest batches and errors. Start / Stop (with confirmation), Test, error details, **Ask Claude**, auto-refresh. |
| **Cluster Monitor** `cluster_monitor()` | Tabs **Overview** (per node SingleStore CPU against its core limit, memory and disk, beside a 15-minute CPU chart), **Health** (node and partition state, cluster events, license, problems explained), **Queries** (running now, plus every statement that ran in the last 60 s), **Disk**, **Bottomless**, **IO**, **History** (CPU, memory, disk, queue and connections over 1 h / 24 h / 7 d) and **Backups**. Refreshes every 5 s. |
| **Security** `security(tab?)` | Users with risk flags, effective privileges with the reason for each, "who can access …", resource pools and the workload manager queue, audit log settings. Changes (users, grants, roles, groups, pools) only in the view, each previewed and confirmed, or written to a SQL script for a DBA. See [Security](#security). |
| **Config** `cluster_config(search?, changed_only?)` | Every engine variable per node, with the documented default; filter on changed from default / differs between nodes. See [Config](#config). |
| **Clean up** `cleanup_work_tables()` | Finds the work tables SAS jobs leave behind (`_dm…` Data Management, `_flw…` flow, `SASTMP…` / `_tmp…` temp tables) in every non-system database: rows, size, created, created by, age, last use and dependent views. Select tables, confirm, and they're dropped one at a time, with a dry-run mode and a log. The **🧹 Clean up** button in the Schema Explorer's header. |
| **Query History** `query_history(min_seconds=1, hours?, tab?)` | Every finished query the cluster traced: when, how long, user, database, rows, success / error, and a probable **Source** (SAS CAS, SAS in-database). Filter on runtime (1 s and up), period, user, database, status, type, source and text; sort by time, duration or rows. Repeated statements are grouped; statements only in the plan cache can be added. Select a query for its full SQL and **Get tuning recommendations** (plan tree, statistics, SAS EP card). Tabs **Queries**, **Trends**, **Usage** and **Advisor**. Its own **History** item in the workspace rail. |
| **Alerts** `alerts()` | Alerts the server raises while it watches the cluster in the background: queries running too long, slow or failed queries, failed pipeline batches and pipeline errors, node memory and disk over a threshold, nodes not online. Edit the rules, acknowledge, clear. Its own **Alerts** item in the workspace rail, with a red badge counting the unacknowledged alerts. |
| **Workload** `workload(name?)` | Saved sets of SQL statements, each with how often it runs, replayed against the cluster with N connections for a set time; live throughput and latency, then compare two runs statement by statement (e.g. before and after a new sort key or index). Its own **Workload** item in the workspace rail. |
| **Query Grid** `query_grid(sql, database?, max_rows=1000)` | Read-only results as a sortable, filterable grid with CSV export; the SQL can be edited and re-run. Writes are refused. Reading stops after `max_rows` (+1 to flag the cut), also when the SQL has a bigger `LIMIT`. |

`TEST PIPELINE` loads no data, but a failed test is still recorded in the
pipeline's batch history and error log.

**Schema Explorer: SAS libraries and caslibs**

- The switch above the tree picks the source: **SingleStore**, **SAS
  libraries** (the Workspace's SAS Compute session on Viya: what the compute
  context assigns plus the [startup code](#connections)) or **CAS** (caslibs on
  the CAS server).
- Libraries → tables → columns with type, length, format and label, and a
  **Preview** of the first 50 rows. For caslibs the tree shows tables loaded in
  memory and the source tables behind them (for a SingleStore caslib, the
  tables of its database).
- SingleStore libraries and caslibs are badged by kind (Schema Explorer tree and
  SQL Editor tree / library picker), with a tooltip naming the kind and the
  SingleStore database:
  - **SS** (SingleStore purple) – caslib type `SingleStore`: the **SAS
    SpeedyStore** integration, CAS pushes queries down to SingleStore live
    (no in-memory copy), e.g. `CSASDP` on Zeus;
  - **S2** (grey-blue outline) – caslib type `singlestore_standard`: the
    standard SingleStore data connector, CAS loads the tables into memory,
    e.g. `SASDP` / `SASDPSTD` on Zeus;
  - **S2** – SAS/ACCESS to SingleStore libnames in Compute (engine SSTORE),
    including the [automatic one](#connections);
  - **9** – SAS 9 libraries through SAS/CONNECT.

  They show their database with **Open … in SingleStore** to jump to it in the
  SingleStore source.
- **</> Show code** on a library or caslib: the statement that would assign it
  again, *reconstructed from its current settings* – `LIBNAME` from the
  session's `dictionary.libnames` (engine, path, server, user, database; SAS
  doesn't report the port or other engine options), and for a caslib the
  `CASLIB … datasource=(srctype=… …)` statement plus the `table.addCaslib`
  equivalent from `caslibInfo` (verbose). Passwords, tokens and authentication
  domains are placeholders (`password="<set password>"`), never real values.
  **Copy** or **Open in SQL Editor** (a new tab, runs as plain SAS – nothing is
  run).
- **Query in SQL Editor** opens the first 100 rows in the SQL Editor, running as
  PROC SQL (libraries) or FedSQL in CAS (caslibs) in that library.
- Backend (app-only tools): `sas_sources`, `sas_libraries`, `sas_tables`,
  `sas_columns`, `cas_caslibs`, `cas_tables`, `cas_columns`,
  `sas_library_code`, `cas_caslib_code`, `sas9_libraries` (each takes an
  optional `connection`: a saved SAS Viya connection, default the active one).
  The SQL Editor's runs: `sas_query` / `sas_execute` (background run with
  `run_id`, `wait_ms`, `connection`, `table_rows`), `sas_run_status`,
  `sas_stop`, `sas_rows` (paging), `sas_session_reset(connection)`.

**Schema Explorer: SAS 9 libraries (SAS/CONNECT)**

- Set up per SAS Viya connection (Connections → SAS Viya → Edit): **SAS 9
  sign-on code** (e.g. `%include` of the signon file you keep in SAS Content,
  such as `cauki_signon.sas`), **SAS 9 server** (the remote session name in its
  `SIGNON`, e.g. `CAUKI`) and optionally **SAS 9 libraries** (remote librefs,
  `local=REMOTE` to rename; empty = all but SASHELP, WORK, SASUSER, MAPS…).
- The **SAS 9** source (shown once the active Viya connection has a sign-on)
  signs on from the Workspace's SAS Compute session – once per session, with
  `NOSOURCE`/`NONOTES`, its log never shown – and assigns the remote libraries
  with remote library services (`libname STG server=CAUKI slibref=STG`). A
  libref already in use in the session is skipped. They're then ordinary SAS
  libraries: tables, columns, preview, PROC SQL in the SQL Editor, and they
  also appear under SAS libraries (badge **9**).
- CAUKI is shut down every evening at 22:00: the sign-on then fails with
  *Couldn't sign on to SAS 9*, which is expected. **↻ Reload libraries** signs
  on again.

**Schema Explorer: data skew (Distribution)**

- A table's **Distribution** tab, and **Check skew** on a database, show how
  evenly rows and data are spread over the partitions and leaves. Read only
  when you open the tab or press the button (two parallel queries on
  `information_schema.TABLE_STATISTICS`, master partitions, and
  `COLUMNAR_SEGMENTS` for disk size), never on a tree click.
- Per table: skew (even / some / high / too small to matter), *row variation*
  (relative standard deviation of rows per partition, the docs' skew score;
  under 10 % is fine), *largest vs even* ((max − avg) / avg), data variation,
  the largest partitions, rows per partition and per leaf with bars, and the
  shard key. Tables under 10,000 rows count as too small to matter.
- With skew it explains the likely cause in plain words: a shard key on a
  low-cardinality column or a few very common values (with buttons that put
  read-only checks of the key's values in the SQL Editor), a keyless table
  filled by `INSERT … SELECT` (fix: copy with
  `WITH(force_random_reshuffle=1)`), or partitions placed unevenly over the
  leaves. A shard key can't be changed with `ALTER TABLE`; the note says how
  to rebuild, with links to the SingleStore docs (*Detecting and resolving
  data skew*, *Shard keys*).
- **Check skew** on a database lists every distributed table, worst first,
  plus rows per leaf; click a table for its details. Backend:
  `schema_explorer_skew(database, table?)` (app-only, read-only).
- **Reshard simulation** (bottom of the Distribution tab): tick the columns of a
  candidate shard key and press **Simulate** to see how the rows would spread
  over the partitions – predicted rows per partition next to today's, the
  predicted row variation against the current one, distinct key values, and a
  note when the key has too few values. Nothing changes: it is the docs' query
  (*Detecting and resolving data skew* → reshard on another column), `SELECT
  WITH(leaf_pushdown=true) SUM(c), PARTITION_ID() FROM (SELECT COUNT(*) c FROM t
  GROUP BY <key>) GROUP BY PARTITION_ID()`, which hashes each key value to the
  partition it would land on. It reads the whole table, so it only runs on the
  click, with the row count shown first (a warning from 1 million rows);
  **Sample … rows** estimates from a random sample (`WHERE RAND() < f`, scaled
  up; every row is still read, but fewer are moved and grouped). **Query in SQL
  Editor** shows the query. Backend: `schema_explorer_reshard(database, table,
  columns, sample_rows?)` (app-only, read-only).

**Schema Explorer: bottomless and Analyze**

- Each database shows a cloud icon if it is bottomless (unlimited storage) or
  a disk icon if it is local-only. Hover for where its data lives: for
  bottomless databases the provider and remote bucket/directory
  (`MV_BOTTOMLESS_DATABASES`, `MV_BOTTOMLESS_STATUS`), otherwise the data
  directory on each node.
- **Analyze…** on a table and **Analyze all tables…** on a database refresh
  optimizer statistics with `ANALYZE TABLE`. First a read-only plan lists each
  table's statistics from `OPTIMIZER_STATISTICS`: when they were last updated,
  outdated or missing columns, histograms, and row-count drift. It marks stale
  tables and shows the exact statements. After you confirm (it changes the
  shared cluster), tables run one at a time with progress and Stop, and each
  gets its own result. ANALYZE reads the table and invalidates cached plans
  built on stale statistics. Backend: `schema_explorer_analyze_plan` and
  `schema_explorer_analyze(confirm=True)`, at most 10 tables per call.

**Cluster Monitor tabs**

- **Overview**: the CPU chart beside the node cards (CPU, memory, disk). The
  chart's height follows its width, so it stays readable in a wide window.
  CPU and disk rates are differences of counters between two reads. The metrics
  sampler takes a first read in the background about 8 s after the server
  starts (and keeps it fresh), so opening the Cluster view doesn't wait a
  second for one; only with the sampler off does the first open measure over
  a second.
- **Queries**: *Running now* from the process list, and *Ran in the last 60 s*
  from the aggregators' plan caches, which also shows the many short
  statements that finish between two refreshes (constants shown as `^` / `@`;
  run counts are since the plan was compiled). A **Queued** badge opens the
  resource pools in the [Security](#security) view.
- **Disk**: per mount total, used and free with a SingleStore / other / free
  split; nodes on one shared file system are grouped and their figures are
  for the whole mount. Per node, SingleStore's own files by type (columnstore
  data, transaction logs, rowstore snapshots, temporary blobs, plan cache,
  trace and audit logs, system files), the blob cache against its limit, query
  spilling, and the largest databases and columnstore tables. Directory sizes
  are measured at most once a minute (**Measure again** forces it).
- **Bottomless**: traffic to and from object storage per node and per
  database: upload, fetch and evict MB/s (from counter deltas), totals, GET/PUT
  rates, upload lag, pending and failed requests, rate limiting, the engine's
  own rates and recent blob cache events. **Measure remote storage** reads
  `MV_BOTTOMLESS_REMOTE_STATS` on demand, because it queries the object store.
- **IO**: session charts of disk read/write MB/s per node and blob cache
  fetched/evicted/uploaded MB/s, current throughput (IOPS where the device
  reports them), and the cached plans that waited most on the blob cache.
  SingleStore 9.0 has no blob cache hit counter, so no hit ratio is shown.
- **Health**: one banner (healthy / things to check / action needed) and the
  problems found, worst first, each with a plain explanation, a **Docs** link
  to docs.singlestore.com and **Ask Claude**. Below: nodes (state, role,
  availability group, leaf pair, ping latency, version, uptime), databases and
  their partitions (healthy / degraded / offline; masters, replicas, online,
  replicating, recovering, offline, unrecoverable, orphan partitions),
  replication to another cluster (lag, catch-up time) when there is any, the
  cluster's own events (`MV_EVENTS`: node failures, rebalances, memory and disk
  limits, stopped pipelines …) and license / version. Critical: a node or
  partitions offline, unrecoverable partitions, license capacity exceeded.
  Check: a degraded database, orphan partitions, failure events in the last
  24 h, replication lag, an expired or expiring license. Info: no high
  availability (`redundancy_level` 1), a rebalance running. Only documented
  9.0 views are read (`MV_NODES`, `LEAVES`, `AGGREGATORS`,
  `MV_DISTRIBUTED_DATABASES_STATUS`, `DISTRIBUTED_PARTITIONS`,
  `MV_REPLICATION_MANAGEMENT_ORPHANS_STATE`, `MV_AGGREGATED_REPLICATION_STATUS`,
  `MV_EVENTS`, license status variables on the master aggregator; never the
  license key), in one parallel round trip, every 30 s while the tab is open.
- **History**: per node SingleStore CPU, memory (% of max_memory), disk
  (% of the data mount), queued queries (peak), running queries and
  connections for the last **1 hour**, **24 hours** or **7 days** (one point per
  1, 5 or 30 minutes), with the same node colors and filters as the other
  charts. The server samples the cluster once a minute in the background (one
  round trip, starting about 20 s after the server so it never slows the
  start) and keeps the samples in `~/.singlestore-mcp/metrics.db`, per
  cluster. Raw samples are kept for 24 h, older ones as 10-minute averages
  (with the CPU and queue peaks), for 7 days. Gaps are times no server ran.
  The Claude server and the desktop Workspace share the file and take one
  sample a minute between them. **Sample now** adds one at once. Settings:
  `SINGLESTORE_MCP_METRICS=0` (off), `SINGLESTORE_MCP_METRICS_INTERVAL`
  (seconds, 15–300, default 60), `SINGLESTORE_MCP_METRICS_RETENTION_DAYS`
  (1–90, default 7). **Export CSV** saves the range shown (one row per point
  and node: time in UTC, node, host and the values the charts show). The
  Alerts view's trend rules (disk full / memory at max_memory within N days)
  read the same store.
- **Backups**: per database the last successful backup, its age, type, size
  and how long it took, the number of backups and failures and the last error;
  the backup history (newest first) and backups running now with their
  progress (`MV_BACKUP_HISTORY`, `MV_BACKUP_STATUS`). Flags databases whose
  last backup failed or whose last good backup is older than 1 / 7 / 30 days,
  and databases with no backup in the history. The history only holds backups
  made with `BACKUP DATABASE` on this cluster; volume snapshots and the like
  don't show. Read-only: the app never starts a backup.

The extra tabs only query the cluster while they are open (History reads only
the local store).

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

The header shows what the history records, e.g. **Tracing queries ≥ 1 s +
failures**. Click it for the `Query_completion` event trace settings per node
(threshold, query text, failed queries, enhanced stats, memory, events traced
and unconsumed), read from `MV_TRACE_EVENTS_STATUS`. It's read-only: changing
the trace needs `ALTER EVENT TRACE`.

**Group repeated statements** (on by default) merges runs of the same
statement (same text once literal values are replaced, same database) into
one row with count, average, p95, max, last run and users; open it to see and
select the individual runs.

**Plan cache.** The trace keeps only runs over its threshold; the plan cache
keeps every statement's totals since its plan was compiled. Each refresh
copies the aggregators' plans into `query_history.db` (so they survive
eviction and restarts). Traced statements show their plan-cache totals (all
executions, average time, memory, spilling, blob cache), and **Plan cache
statements** (a checkbox, off by default) adds the statements the trace never
kept, labelled as aggregates.

**Plan view.** Tuning shows the plan as a tree (from `EXPLAIN JSON`, which is
never cut short): one box per operator with estimated rows, filters and keys,
and the expensive parts marked: broadcasts, reshuffles, large hash builds,
nested-loop joins on large inputs, filtered full scans of tables without a
sort key, large gathers. **Profile (runs it)**, for read-only SELECT / WITH
only and after a confirmation, runs the query once more with `PROFILE` and
shows actual rows, time, memory, spills and segments skipped per operator;
it's cancelled after 5 minutes. Raw EXPLAIN / JSON / profile JSON are one
click away.

**Statistics.** In tuning, **Check statistics…** shows the statistics age and
staleness of the tables the query reads, with the exact `ANALYZE TABLE`
statements (via the Schema Explorer); **Analyze table(s)** runs them after a
confirmation.

**`SELECT *`** is flagged only when it costs something: not for SAS Embedded
Process calls, metadata probes (`WHERE 0=1`, `LIMIT 0`), SAS work tables,
`SELECT * FROM (SELECT cols…)`, or tables with one or two columns.

**SAS EP calls.** For SAS Embedded Process calls (`sasepFunction(…)`), tuning
shows a SAS EP card instead of generic advice. The call is an external
function, so `EXPLAIN` shows nothing inside it; the plan cache counts the
call's wall time but almost no CPU (SingleStore waits for the EP). The work happens in the statement the EP
generates (`CREATE TABLE … AS SELECT … FROM <input> JOIN <per-job EP
function>`), which runs on the leaves partition by partition. The card shows:

- the input table's **skew**: rows and size per partition, largest / average,
  and the time lost waiting for the largest;
- **partitions vs leaf cores**, using the container's CPU limit;
- **how much data goes into the EP**: rows, bytes and columns, whether a filter
  is applied before the EP, and whether the input's sort key lets that filter
  skip segments;
- **disk / blob-cache waits, memory, spilling and queueing** from the plan
  cache;
- the job's **runtime over time** as a sparkline.

Each point is judged Fine / Worth a look / Problem, with a next step such as
resharding the input, a filter before the EP call, or a sort key. EP calls are
never profiled, since that would run the SAS job again, and the password SAS
puts into the call is masked as `***` wherever the SQL is shown (Copy SQL
still copies the original).

**SAS VA / CAS EP queries.** SAS Visual Analytics and CAS queries that join
with per-job EP format functions (`… FROM (SELECT * FROM db.t) AS t JOIN
EP.h…_EP(cols) AS ep`) are labelled SAS EP and get the SAS EP card too, marked
*VA / CAS format function*. The function's name changes every run, so the
history matches runs by the statement with that name left out. The card shows:

- for CAS parallel loads (one statement per partition, `WHERE partition_id() =
  N`), each partition's runtime in that load, which shows skew directly;
- the input table's skew and partitions vs leaf cores;
- how much data reaches the EP: rows, bytes and the columns passed to the
  function (a `SELECT *` in the derived table costs little, since only those
  columns are read), and whether a filter applies before the EP and can skip
  segments through the sort key;
- plan-cache waits, memory, spilling and queueing when the plan is still known
  (each run is a new, short-lived plan, so it often isn't);
- the statement's runtime over time;
- how this run compares with every other EP call. On clusters where most EP
  calls take the same few seconds whatever they read, that's a fixed wait on
  the EP side, and the card says so.

Both kinds of EP card also show:

- **Where the time went**: a bar split into measured and estimated parts. For
  a sasepFunction job whose generated statement is in the trace: time before
  the EP sent its SQL, the generated statement itself, and the rest. Otherwise
  queueing (plan cache), the estimated fixed wait (this call minus the median
  of EP calls that ran on their own off the plateau) and the rest; plus the
  plan cache's CPU time, which shows whether SingleStore was working or waiting.
- **The fixed-wait plateau**: the runtime band most traced EP calls share
  (found from the data, ±4 %), a histogram of all EP runtimes, and a
  straight-line fit of time against rows returned (per call vs per row cost).
- **EP calls running at the same time**: overlapping calls, whether they
  finished together although they started seconds apart (one shared wait),
  and many small calls (≤ 10 rows each) that would be cheaper as fewer queries.
- **The EP's own statement failed**: the generated `CREATE TABLE … AS SELECT`
  failed (e.g. the output table already exists) although the sasepFunction
  call itself completed normally.
- `SELECT *` through the EP for jobs: every input column goes into the EP
  (KEEP= / DROP= on the input).
- **Tuning tips**, each marked *Measured* (seen in this cluster's data),
  *Documented* (with the source) or *Hypothesis* (to verify), with links.

**EP overview (Usage tab).** EP calls over the chosen period by source (SAS
job / SAS VA / CAS), database and user: calls, total and median time, how many
sat on the plateau and how much time that was, failures, overlapping groups
that shared one wait, and how many VA / CAS EP functions the plan cache saw
registered (`INSERT INTO EP.sasepv5`, every call however fast) against how
many were traced. Click a row for the EP calls. EP calls are kept in the local
history whatever their duration, so a lower trace threshold shows the fast ones.

**What explains the ~5.2 s, as far as the data shows (Oct 2026, read-only).**
Measured on this cluster (trace, plan cache, local history):

1. *It's a step, not a cost per row.* The daily `_hello_world` sasepFunction
   job took 0.24 s on 2026-10-13 (plan cache) and 5.24–5.30 s on the days it
   was traced: a +5.0 s step. Across all traced EP calls (1 to 7,599 rows
   returned) time doesn't follow rows (r² ≈ 0).
2. *It's before the EP touches the data.* In the slow job runs the EP's
   generated `CREATE TABLE … AS SELECT … sasepin …` started 5.20–5.23 s after
   the sasepFunction call started; it and everything after took < 50 ms.
3. *SingleStore isn't working or queueing.* The plan cache shows 5,255 ms
   execution time but 3 ms CPU and 0 ms queued for that call; all EP calls run
   in `default_pool` (no concurrency limit); workload management has queued 0
   queries since start-up on both aggregators.
4. *Concurrent calls share one wait.* VA calls that started 3.6–4.1 s after
   others finished within 0.2 s of them (2026-10-12 06:46, 2026-10-14 12:13).
5. *An EXPLAIN waited too.* A SAS-issued `EXPLAIN EXTENDED CREATE RESULT
   TABLE … JOIN EP.h…_EP(…)` (compiles, doesn't run) took 5.06 s in such a
   burst, so the wait can happen before execution.
6. *Most EP calls are fast and not traced.* The plan cache saw 68 VA / CAS EP
   functions registered against 26 traced; with a 1 s trace threshold, the
   "77 % at 5.2 s" is a share of the *slow* calls only. Sub-second warm calls
   (0.24 s job, ~1.1 s VA calls running on their own) are the normal case.
7. *Side findings:* the `_hello_world` job's generated statement fails every
   day with `ER_TABLE_EXISTS_ERROR` (`MyDB._hello_world_1` exists) while the
   call reports success; `EP.sasepv5` holds 147 orphan registrations from
   March–May 2026 and `EP` keeps a few old per-job functions.

Documented: SAS EP functions are SingleStore external functions `AS COLLOCATED
SERVICE '/opt/sas/sasep.sock'` (SHOW CREATE FUNCTION); SingleStore talks to a
collocated service over a Unix socket with shared-memory buffers and doesn't
start or manage the service ([CREATE EXTERNAL FUNCTION](https://docs.singlestore.com/db/v9.0/reference/sql-reference/procedural-sql-reference/create-or-replace-external-function/));
the EP is deployed on each leaf pod ([SAS SpeedyStore admin
guide](https://documentation.sas.com/api/collections/sasadmincdc/v_042/docsets/calsinglestore/content/calsinglestore.pdf?locale=en));
the trace keeps only queries over `Duration_threshold_ms` and traces DDL only
from 9.1 ([Query History](https://docs.singlestore.com/db/v9.1/query-data/query-tuning/query-history/)).
Nothing documented explains a fixed 5 s.

Hypotheses (to verify, not shown by the data): a constant +5.0 s matches the
Linux resolver's default timeout (`timeout:5` in
[resolv.conf](https://man7.org/linux/man-pages/man5/resolv.conf.5.html)); in
Kubernetes a lost A/AAAA lookup (the [conntrack race](https://cloud.tencent.com/developer/article/1583706))
costs exactly that. If the EP resolves a host name or opens a connection to
the aggregator when it's cold (the job definition is in `EP.sasepv5`, which the
EP may read back), every cold call pays 5 s and concurrent calls queue behind
the same step. Alternatively a 5 s poll / retry inside the EP, or the child
aggregator waiting for the new per-job function's DDL from the master
aggregator (not traced on 9.0).

**What to raise with the SAS EP owners:**
- What does the EP do in the first ~5 s of a cold call? Their EP log on the
  leaf pods at e.g. 2026-10-14 12:35:16–12:35:21 (cluster time) shows it.
- Does the EP connect back to SingleStore per call (host name, DNS, TLS,
  authentication), and is there a pool / keep-alive / idle timeout for its
  process or connections? Time `getent hosts <aggregator host>` in a leaf pod
  and check `/etc/resolv.conf` (`ndots`, `single-request-reopen`).
- Why do concurrent calls finish together: is a step in the EP serialized
  (e.g. reading or registering jobs in `EP.sasepv5`)?
- Cleanup of orphan rows in `EP.sasepv5` and old functions in `EP`; and the
  failing `_hello_world` health check (drop its output table or let it replace it).

To see the fast EP calls too, an admin can recreate the trace with a lower
threshold for a while (it's a cluster setting, not done by the Workspace):
`DROP EVENT TRACE Query_completion; CREATE EVENT TRACE Query_completion WITH
(Query_text = on, Duration_threshold_ms = 100, Trace_all_failed_executions =
on);` and back to 1000 afterwards (not 0: it costs performance).

SAS-side options worth knowing (they change the transfer, not the fixed
wait): `aggregatePushdown=true`, `epDatabase` / `udfDatabase` on the
SingleStore caslib ([SAS Communities](https://communities.sas.com/t5/SAS-Communities-Library/Using-SAS-with-SingleStore-Enhancing-Performance-with-Aggregate/ta-p/961443)),
`NUMREADNODES` for parallel CAS reads ([SAS blog](https://blogs.sas.com/content/sgf/2018/03/29/multi-node-data-transfer-to-cas/)),
and WHERE= / KEEP= on the input of in-database DATA steps. Fewer, larger EP
calls pay the wait fewer times: combine small DATA steps; in VA, fewer objects
or precomputed formatted / calculated columns (to verify with SAS which VA
items need EP).

**Usage tab.** Ranks tables and users over 24 h, 7, 30 or 90 days, or
everything stored.

- *Most used tables*: executions and time from the plan cache (every run of
  every statement naming the table, however short, copied locally on each
  refresh and sampled every 10 minutes), next to traced runs from the query
  history, with distinct statements, last use, users and source. Table names
  are read from the SQL (FROM / JOIN / INTO / UPDATE, `db.table` or resolved
  against the statement's database). Click a table to list its statements.
- *Most active users*: queries, total and average time, longest, failures,
  databases, distinct statements and last activity, from the traced runs (the
  plan cache records no user). Click a user to see their queries.

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
- **SAS EP**: calls the SAS Embedded Process (`sasepFunction(…)`, `EP.sasep…`);
- empty otherwise.

It's a heuristic, shown as "probably" in the detail panel, and a filter
(**Source**: All / SAS CAS / SAS in-database / SAS EP / Other).

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
| Disk projected full within **14 days** | the Cluster Monitor's History (`metrics.db`, local, no cluster query) | per node, active while the projection holds |
| Memory projected to reach `max_memory` within **3 days** | the same History | per node, active while the projection holds |

The two trend rules fit a straight line through the last 24 hours of History
samples (one a minute while a server runs) for each node's data mount and
memory. They only alert on a steady climb (at least 6 hours of samples, fit
R² ≥ 0.5), so a one-off spike or a load that goes up and down doesn't count;
the alert says how much it grew per day and when it is full at that rate
(critical when that is within 2 days, or a third of the rule's days). Without
History (the sampler off, or less than 6 hours stored) they find nothing.

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
checks off. The Cluster Monitor's metrics history has its own switch,
`SINGLESTORE_MCP_METRICS=0`.

The workspace rail shows the number of unacknowledged alerts as a red badge
(it reads the server's in-memory state every 30 seconds; no cluster queries).
With **Notify me** on, the workspace also shows a browser notification for new
alerts, after you allow notifications in the browser (it asks on your next
click in the workspace). That works in the browser and the desktop window; in
the Claude app the badge is the signal.

**Workload**

A *workload* is a name, a default database, settings and a list of SQL
statements, each with how often it runs a minute (or, with pacing "as fast as
possible", a relative weight). Define one by hand, take the heaviest read-only
queries of the last 24 hours with **From history**, or send a single query
over from the Query History panel's **Add to workload** button. Statements
may use placeholders filled in per execution: `{{int:1:1000}}`,
`{{float:0:1}}`, `{{pick:a|b|c}}`, `{{date:2024-01-01:2024-12-31}}` (quote
strings yourself, e.g. `'{{pick:US|EU}}'`).

Settings: **connections** (parallel workers, each its own connection, opened
once so connecting isn't measured), **duration**, a per-statement **timeout**
(the statement is cancelled with `KILL QUERY` after it), **arrivals** (evenly
spaced, or random/Poisson for bursts like real users at the same average
rate), **read all rows** (off measures just the execute time, not the cost of
reading the result back), **allow writes** (off by default — statements other
than `SELECT` / `WITH` run repeatedly, so point them at a test table) and
**trace every query during the run** (needs the `ALTER EVENT TRACE`
permission; temporarily sets the cluster's `Query_completion` trace to
`Duration_threshold_ms = 0` so every execution lands in Query History, then
restores the previous setting — tracing costs cluster performance, so compare
traced runs only with traced runs).

Only one workload runs at a time. While it runs, the view shows live KPIs
(executions, per second, p50 / p95 / p99, errors, skipped releases when the
cluster — or the connection count — can't keep up with the rate) and
per-second throughput and latency charts; **Stop** cancels the statements
running now. A run lasts its full duration (statements are released until
the end, and the per-second rates are over the whole duration, or the time
until Stop); statements still running at the end finish ("finishing…") and
count. Every statement is tagged `/* workload:<name> stmt:<id> */`, so
finished executions show up in Query History with the source "Workload:
\<name>", and the tag is stable across runs so query plans are reused.

**Saved runs** lists every past run of the selected workload; **Compare**
picks two finished runs (e.g. a baseline and one after a new sort key, shard
key or index) and shows, per statement, p50 / p95 before and after with the
percent change, throughput, errors, and whether the SQL or the row counts
changed between the runs — changes of 5% or more are coloured, smaller
differences are usually noise. **Tune** on a statement reuses the same
EXPLAIN and table-design recommendations as Query History's tuning panel.

*Claude in Workload.* The **Claude** button opens a chat panel on the right,
like the SQL Editor's: the [in-app assistant](#claude-in-the-apps) answers
(or, as there, the Claude chat). Each question carries the selected workload
(settings and every statement), the shown run's per-statement p50 / p95 /
errors / rows, and the definitions of the tables the statements read (keys,
row counts, distinct values per column). Ask for variants of a statement,
realistic parameter spread with the placeholders, heavier or lighter versions
or a realistic mix of rates; Claude may query the database read-only to find
real values, never runs writes, and puts each statement in its own SQL block,
optionally with `-- name:` and `-- per_minute:` lines. Every block has **Add to
workload** (a new statement, 60 a minute unless the block says otherwise),
**Replace statement N** (pick which; the last statement you clicked by
default), **Test** (runs it once, read-only, and shows rows and time) and
**Copy**. Changes are unsaved until **Save** / **Save and run**. Each workload
has its own conversation while the page is open.

*From and to the SQL Editor.* The SQL Editor's **→ Workload** adds statements
to a workload (see [SQL Editor](#sql-editor)). The other way, **Open in SQL
Editor** on a statement opens it in a new editor tab, in the statement's
database. That tab remembers the workload and statement (in its autosaved
draft, so also after a restart) and shows **Update in workload**, which writes
the tab's SQL (the selection, else the whole tab when it holds one statement,
else the statement at the cursor) back to the statement **and saves the
workload**; an open Workload view shows the new SQL right away and keeps its
own unsaved changes. If the workload or statement is gone (deleted, renamed,
or the statement was never saved), the tab says so and offers **→ Workload
instead**. Query History's **Add to workload** works as before.

Workloads and their runs are saved in `~/.singlestore-mcp/workloads/`
(`workloads.json`, and one JSON file per run under `runs/`). The model-visible
tools `workload_define`, `workload_run`, `workload_status`, `workload_stop`,
`workload_list` and `workload_compare` work the same way without opening the
app, so Claude can define, run, watch and compare a workload from the chat.

### Security

Who can do what, and (for users with the right privileges) a GUI to change it.
Claude's tools only read; every change is made in the view, shows the exact
SQL first and runs only after you confirm it.

- **Users**: every user with type, lock status (and the failed-login lockout
  setting), password age and expiry, default resource pool, groups,
  connections and flags (ALL / SUPER on `*.*`, grant option, host `'%'`,
  password never expires, locked, TLS required, unused, i.e. no traced query
  in the local Query History store for N days). Click a user for their
  effective privileges with the reason for each (direct, or via role ← group),
  `SHOW GRANTS` (password hash hidden) and an **Account** block: status,
  `REQUIRE SSL` / `NONE`, the failed-login lockout, and (for your own account)
  whether this app's connection uses TLS.
- **Roles & groups**: the user → group → role → privileges chain and **Who can
  access …** for a database or table.
- **Resource pools**: limits, live running and queued queries per pool, the
  workload manager queue with the limit each query waits for, and who uses
  which pool.
- **Audit log**: the audit settings (`SHOW VARIABLES LIKE 'audit%'`) and, when
  audit logging is off, the steps an admin takes to turn it on (see
  [Audit log](#audit-log)).

**Changes (v2).** Create / drop users (the password is typed in a masked field),
GRANT / REVOKE privileges on `*.*`, a database or a table (picker), revoke all,
create / drop roles and groups, grant a role to a group and a group to a user,
create / change / drop resource pools, set a user's default pool and grant pool
usage, and per user: **change password**, **lock / unlock**, the **failed-login
lockout**, **REQUIRE SSL / NONE** and **grant columns through a view** (see
[Passwords, locks, TLS and column grants](#passwords-locks-tls-and-column-grants)).
Each change:

1. **Preview SQL** shows the exact statements (a password always as
   `'********'`) with warnings and blockers.
2. You confirm with a tick, or by **typing the name** for a DROP, "revoke all",
   your own account, an admin account or anything that takes admin rights away.
3. The server builds the statements again from the cluster as it is now, runs
   them one by one (stopping at the first error), shows the result and
   refreshes the view.

Blocked outright: dropping the user you're connected as, and any change that
would leave no admin (a user with `CREATE USER` or `SUPER` and the grant option
on `*.*`). Warned: removing your own or another admin's rights, open
connections of a dropped user (`DROP USER` doesn't close them), privileges
that aren't granted directly, pools that are a user's default (SingleStore
refuses to drop those), CPU limits summing over 100 %. When you lack the
privilege for a kind of change (`CREATE USER`, the grant option, `ALTER USER`,
`CREATE POOL` / `DROP POOL`, per the SingleStore permissions matrix) the
buttons still open the preview, but **Run** is off with the reason: you can
make a SQL script of the change instead (below).

**SQL script instead of Run** – for customers where a DBA reviews and runs
security changes. Under every preview, next to Run:

- **Copy SQL**, **Open in SQL Editor** (a new editor tab with the script; it
  isn't run) and **Save as .sql…** (the file browser with This computer / SAS
  Content / SAS Server, suggested name `security-<date>-<time>-<what>.sql`; an
  existing file is only replaced after **Replace it**).
- **Add to script** collects the change in the **SQL script** basket under the
  panel: reorder (↑ ↓), remove (✕), **Show script**, then Copy SQL / Open in
  SQL Editor / Save as .sql… for all of them at once. The basket lasts for the
  browser session (also a page reload). A change can use what earlier changes
  in the basket create (e.g. grant a new role to a new group): the pickers list
  those objects "(in script)", and such a change can only go into the script.

The script has a header (connection name and host, the connected user, date and
time, one line per change), the statements in a safe order (creates – pools,
roles, groups, users – then grants and other changes, then revokes, then drops;
**Keep my order** turns that off) and each change's warnings as `-- WARNING:`
comments. The statements are the ones the preview shows and Run would execute,
built by the same plan builder from the cluster as it is now, with each change
checked as if the earlier ones had run (names, existing objects, lock-out / last
admin). Making a script runs nothing and needs none of the change privileges
(the header notes which privileges whoever runs it needs). **No password is
ever in a script**: `CREATE USER` and a password change get
`'<set password>'` and a `-- WARNING:` to replace it (the password field is never sent for a script).
SingleStore 9.0 also accepts `CREATE USER` without `IDENTIFIED BY`, but that
makes a user without a password, so it isn't used.

The password is sent once, as a bound parameter of `CREATE USER` (or the
password change), and is never
shown, logged, returned or stored (not in the plan, the model context or the
local Query History store, which skips the app's own `/* s2-… */` statements).
Found on 9.0.40 and handled: `REVOKE ALL PRIVILEGES, GRANT OPTION` needs every
privilege the revoker could revoke (an admin without `SUPER` gets "Access
denied"), so "revoke all" revokes what the grantee holds, one statement per
object; a pool limit can't be set back to "none" (except `MAX_CONCURRENCY = 0`);
a user's default pool can't be cleared, only set to another pool (e.g.
`default_pool`).

#### Passwords, locks, TLS and column grants

Buttons in a user's **Account** block (and options in **+ New user**). Checked in
the SingleStore 9.0 docs and tried on 9.0.40 with `zz_test_` users:

- **Change password** – two masked fields that must match; sent once as a bound
  parameter of `ALTER USER 'u'@'h' IDENTIFIED BY …`, `'********'` in the preview,
  `'<set password>'` in a script, never in results, logs or history. Your own
  password can always be changed: without `ALTER USER` it uses
  `SET PASSWORD FOR … = PASSWORD(…)` (9.0.40 refuses `ALTER USER` on yourself
  then). For your own account you type your name to confirm, and if the active
  saved connection signs in as you, a tick **saves the new password for that
  connection** (OS credential store) after the change succeeded. The
  environment-variables connection, a URL or a token login can't be updated
  that way (the panel says what to do). Open connections keep working; a lock
  stays.
- **Lock… / Unlock…** – `ALTER USER … ACCOUNT LOCK / UNLOCK`. Status from
  `information_schema.USERS.ACCOUNT_STATUS` (`LOCKED` / `OPEN`). Refused: locking
  yourself, or the last admin that isn't locked. Unlock also clears a lockout
  after too many failed logins. A locked login gets error 2449; open
  connections keep working; **a cluster restart clears every lock** (docs).
- **Failed-login lockout…** – `ALTER USER … SET FAILED_LOGIN_ATTEMPTS = n
  PASSWORD_LOCK_TIME = s` (seconds). Both above 0 turns it on, both 0 off;
  SingleStore refuses one without the other (error 2451). `+ New user` can set it
  too (`CREATE USER … WITH … FAILED_LOGIN_ATTEMPTS = n PASSWORD_LOCK_TIME = s`).
- **Require TLS (SSL)… / Allow non-TLS logins…** – `ALTER USER … REQUIRE SSL /
  NONE` (`+ New user`: optional `REQUIRE`). Read from `USER_PRIVILEGES`
  (`PRIVILEGE_TYPE = 'SSL'`) and `SHOW GRANTS`: `USERS` has no column for it. A
  non-TLS login then gets error 2455. Requiring SSL for **your own** account when
  this app's connection doesn't use TLS (`Ssl_cipher` of the session) shows a
  **lock-out risk** warning and needs the typed name. `X509` / `SUBJECT` aren't
  offered.
- **Grant columns (view)…** – 9.0 has **no column-level privileges**: the GRANT
  reference has only `*.*`, `db.*` and `db.table` ("Table privileges apply to all
  columns"), and on 9.0.40 `GRANT SELECT (c1, c2) ON db.t` is *accepted but
  grants SELECT on the whole table* (every column readable; `REVOKE SELECT (c)`
  likewise revokes the whole table). So the form picks a table and columns and
  previews two statements: `CREATE VIEW db.v AS SELECT c1, c2 FROM db.t` and
  `GRANT SELECT ON db.v TO …`. Views run as their creator (`DEFINER`), so the
  grantee needs nothing on the table; the plan warns when the grantee already
  has `SELECT` on the table, the database or `*.*` (then the restriction doesn't
  hold). Remove it later with Revoke on the view or `DROP VIEW`.

Docs: [ALTER USER](https://docs.singlestore.com/db/v9.0/reference/sql-reference/security-management-commands/alter-user/),
[CREATE USER](https://docs.singlestore.com/db/v9.0/reference/sql-reference/security-management-commands/create-user/),
[SET PASSWORD](https://docs.singlestore.com/db/v9.0/reference/sql-reference/security-management-commands/set-password/),
[GRANT](https://docs.singlestore.com/db/v9.0/reference/sql-reference/security-management-commands/grant/),
[CREATE VIEW](https://docs.singlestore.com/db/v9.0/reference/sql-reference/data-definition-language-ddl/create-view/).

#### Audit log

SingleStore's audit log (Enterprise edition) is **files on each node**, not a
table: there is no `information_schema` view of it. On this cluster it is off
(`auditlog_level = OFF`, files would go to `/var/lib/memsql/instance/auditlogs`).

- **Turn it on** (an admin, outside this app; the variables only take effect
  when a node starts): with SingleStore Tools
  `sdb-admin update-config --all --key "auditlog_level" --value "ADMIN-ONLY"`
  (plus `auditlog_retention_period`, days), then `sdb-admin restart-node --all`;
  with the Kubernetes Operator (this cluster's node names suggest it), set
  `auditlog_level: "ADMIN-ONLY"` under `spec.globalVariables` of the
  `MemsqlCluster` resource and `kubectl apply`. Check with
  `SHOW GLOBAL VARIABLES LIKE 'audit%';`.
- **Levels**: `LOGINS-ONLY`, `ADMIN-ONLY` (+ DDL, GRANT/REVOKE, CREATE USER),
  `WRITES-ONLY` (+ DML), `ALL-QUERIES` (+ SELECT), `*-PLAINTEXT` (literal
  query text), `ALL-RESULTS`, each with an `-INCLUDING-PARSE-FAILS` variant
  (which can log passwords typed in invalid statements). Passwords in valid
  statements are left out.
- **Files**: CSV, `auditlog_<host>-<port>_<date>_<time>.log` in `auditlogsdir`,
  rotated by `auditlog_rotation_size` (128 MB) and `auditlog_rotation_time`
  (1 h), removed by `auditlog_retention_period` (days) or
  `auditlog_retention_size` (MB); 0 = keep forever. Query lines: id, time,
  time zone, host:port, node type, aggregator id, thread id, user, database,
  correlation id, query hash, query; login lines carry user, remote host, auth
  type, status, TLS cipher and app name.
- **Reading it**: through SQL only by loading the files (a pipeline or
  `LOAD DATA`), which needs `FILE READ` and file-system access on the nodes;
  `FS` pipelines aren't supported with the Kubernetes Operator. The practical
  route is a log collector (Fluent Bit / Filebeat on each pod's `auditlogsdir`)
  into a SIEM or a SingleStore table, which the Security view could then show
  (filter by user, time, statement type).

Sources: [Configure Audit Logging](https://docs.singlestore.com/db/v9.0/security/audit-logging/configuring-audit-logging/),
[Audit Logging Levels](https://docs.singlestore.com/db/v9.0/security/audit-logging/audit-logging-levels/),
[Audit Log File Format](https://docs.singlestore.com/db/v9.0/security/audit-logging/audit-log-file-format/),
[Operator: Configure Audit Logging](https://docs.singlestore.com/db/v9.1/reference/singlestore-operator-reference/configure-audit-logging/).

Without admin rights it shows only your own groups, roles and grants. Ask
Claude "who can access database EP?", "what can user Hans do?" or "why are
queries queued?" (tools `security`, `who_can_access`, `user_privileges`,
`resource_pool_status`).

### Config

Every engine variable with its value on each node. Search, filter by "changed
from default", "differs between nodes", category, sync / non-sync and runtime
vs startup only, and export as CSV. Click a variable for its description, docs
link and the `SET GLOBAL` statement to copy (never run). Defaults and
descriptions come from the SingleStore docs for your version, downloaded once
and kept in `~/.singlestore-mcp` for 7 days; without internet access defaults
show as unknown. Ask Claude "which settings are changed from default?" (tool
`cluster_config`).

### Claude in the apps

The chat panels in the SQL Editor, the Notebook and Workload are answered by the
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
- **Knows SAS:** in a SQL Editor tab that runs PROC SQL, FedSQL or FedSQL in
  CAS, the question also carries the tab's library / caslib and known columns
  and, once per session and language, that language's SAS guide (see
  [Ask Claude in a SAS tab](#sql-editor)).
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
machine only (`127.0.0.1`, with a secret token in every link).

**Restarts don't break an open tab.** The link's port and token are kept in
`~/.singlestore-mcp/browser/` and reused by the next server, which listens on
them again right at start: after `restart_server`, a crash restart or a
restart of Claude, the tab's next call reaches the new server. Meanwhile the
page shows *Reconnecting to the SingleStore MCP server…*, holds the app's
calls and retries (also at once when the laptop wakes up or the network comes
back); Cluster Monitor, Query History and alerts then go on refreshing by
themselves. A call that changes something and was cut off mid-way isn't sent
twice: the app says it may or may not have run.

If the server doesn't come back within 60 s (`SINGLESTORE_MCP_BROWSER_RECONNECT`),
or the token is refused, the tab shows **Session ended** instead of the app
(all polling stops) — reopen the app from Claude or the desktop shortcut, or
press **Try again** once a server runs again. SQL Editor text that couldn't be
autosaved meanwhile is kept in that browser tab, can be copied from the
*Session ended* page, and is saved to its editor tab as soon as the page
reaches a server again. Database errors (VPN down, cluster unreachable) are
not this: see the next section.

### After a network break (VPN, sleep, Wi-Fi)

The server recovers by itself when the VPN drops or reconnects (also on another
gateway with a new address, e.g. 172.29.x → 172.16.x), the laptop wakes up or
Wi-Fi changes — **no restart needed**. While SingleStore or SAS Viya can't be
reached, the Workspace shows **Connection to SingleStore lost (since 14:02) –
retrying…** at the bottom (hover: the error; *Retry now* checks at once); a tool
call says so readably ("… reconnects by itself as soon as the cluster can be
reached again"). Once the network is back the line goes away within a few
seconds, a *Reconnected to SingleStore* message shows, the views reload what
failed meanwhile (Cluster Monitor, Query History and alerts were polling anyway;
the Schema Explorer reloads its failed lists) and you just carry on.

What happens underneath (`netguard.py`):

- **Dead connections are noticed in seconds, not hours.** Every connection to the
  cluster (pool, standby, notebook kernels, Compare / Workload runs) and to Viya
  has a short TCP keepalive (10 s idle, then 3 probes 5 s apart:
  `SINGLESTORE_MCP_TCP_KEEPALIVE`) instead of Windows' two hours, and connecting
  gives up after 10 s (a whole cluster login after 20 s).
- **A VPN that moved** to another local address: a watcher (every 2 s) sees that
  the route to the cluster / Viya now leaves from another address and closes the
  connections opened from the old one at once — a query waiting on one is read
  again on a fresh connection (a write that may have run is never sent twice).
- **After sleep** (the clock jumped) or any failure, idle pooled connections are
  checked with a ping (at most 3 s) before they're used again, and replaced if dead.
  Kept-alive Viya connections are replaced instead of reused.
- A call never waits on a hung connection for long: a full pool waits at most
  2 minutes (`SINGLESTORE_MCP_POOL_WAIT`) and says why.
- **SAS:** a Compute or CAS session Viya ended while the network was away is
  started again with the automatic S2 library and the startup code; the next run's
  log and the Workspace say once *The SAS session had ended … and was started again*
  (WORK tables from before are gone). The SAS pre-warm / keep-alive retries 10 s
  after a failed check (not after its 4 minutes), and at once when the page comes
  back online or Viya answers again; a token renewal that fails because the network
  is away says so and keeps the sign-in (not "sign in again"); proxy vs. direct is
  chosen again after a failure.
- While a service is down the server checks it again in the background (every few
  seconds for two minutes, then every 30 s), so it's found back even when nothing
  else asks. The MCP side never restarts the server over network errors.

**Check it yourself:** open the Workspace, run a query, disconnect the VPN for a
minute (or close the laptop lid), and reconnect. The bottom line appears while
it's away and goes by itself; run the query again — it works without
`restart_server`. A query running at the moment the VPN switches gateway is
retried on a fresh connection.

Who owns which address: every MCP client connection (Claude Desktop and
Claude Code each start one server) holds a *slot* (a lock file in that folder)
for as long as it runs, and its servers always use that slot's address; a
second client takes the next free slot, so two servers never share a port.
When Claude restarts, its new server takes the lowest free slot again. If
another program has taken the port, the server picks a new one (and keeps
that). The standalone workspace keeps its own address in `workspace.json`.
**Connections → Reset browser links** gives the server a new token: every open
tab and earlier link then ends (restarts never change it).

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
| `SINGLESTORE_MCP_POOL_WAIT` | `120` | Seconds a call waits for a free pooled connection before it gives up with a readable error |
| `SINGLESTORE_MCP_TCP_KEEPALIVE` | `10,5,3` | TCP keepalive of every cluster / Viya connection: idle seconds, seconds between probes, probes (`0` = the OS default) |
| `SINGLESTORE_MCP_CONNECT_TIMEOUT` | `10` | Seconds a TCP connect to the cluster or Viya may take |
| `SINGLESTORE_MCP_CONNECT_DEADLINE` | `20` | Seconds a whole cluster login (TCP, TLS, sign-in, session settings) may take |
| `SINGLESTORE_MCP_PING_TIMEOUT` | `3` | Seconds the ping that checks an idle pooled connection after sleep / a network change may take |
| `SINGLESTORE_MCP_IDLE_PING` | `300` | A pooled connection idle this long is pinged before use |
| `SINGLESTORE_MCP_NO_SUPERVISOR` | off | Run without the restart relay |
| `SINGLESTORE_MCP_NO_STANDBY` | off | Don't keep a pre-loaded standby process for fast restarts |
| `SINGLESTORE_MCP_STANDBY_DELAY` | `5` | Seconds after a start before the standby is prepared |
| `SINGLESTORE_MCP_STANDBY_CONNECT` | `4` | Database connections the standby opens ahead for the server after `restart_server` (0–4; `0` = off) |
| `SINGLESTORE_MCP_BROWSER_RECONNECT` | `60` | Seconds an open browser tab keeps trying to reach a restarting server before it shows *Session ended* |
| `SINGLESTORE_MCP_BROWSER_BIND_WAIT` | `15` | Seconds a new server waits for the previous one to free the browser port |
| `SINGLESTORE_MCP_METRICS` | on | `0` turns off the Cluster Monitor's background metrics sampler (History tab) |
| `SINGLESTORE_MCP_METRICS_INTERVAL` | `60` | Seconds between metrics samples (15–300) |
| `SINGLESTORE_MCP_METRICS_RETENTION_DAYS` | `7` | Days of metrics history kept (1–90) |
| `SINGLESTORE_MCP_METRICS_DELAY` | `20` | Seconds after a server start before the first metrics sample (the Cluster Monitor's CPU baseline is read after at most 8 s) |
| `SINGLESTORE_MCP_SAS_IMPORT` | on | `0`: never reuse the SAS Viya MCP server's / CLI's cached sign-in for a SAS Viya connection |
| `SINGLESTORE_MCP_SAS_PREWARM` | on | `0`: don't start the Workspace's SAS session in the background when it opens (the first SAS run starts it; the test suite runs with `0`) |

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
  messages and model context to the real host. Only the visible view loads
  on open; its page is fetched while the host still runs `sql_editor`, comes
  without the shared head (the ~0.45 MB ext-apps bundle, `shared.js`,
  `shared.css`; the workspace puts its own copy back), and a page the
  workspace already has (another SQL tab or notebook) is only revalidated.
  Vendor blocks (CodeMirror ~0.5 MB, the function list, the charts) are marked
  `data-s2-vendor` / `data-s2-hash`; the workspace keeps the ones it got and
  asks `app_page` for later pages without them (`vendors`), so opening the
  Notebook after the SQL Editor doesn't bring CodeMirror a second time.
  Standalone pages (`resources/read`, the browser view) are whole.
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
| SQL files on SAS Viya (SAS Content, SAS Server) | ✅ with a signed-in SAS Viya connection |
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
  `%sql` run as written, as in Jupyter. Changes to users, grants, roles,
  groups and resource pools are made only in the Security view, after a
  preview and your confirmation (see [Security](#security)).
- Notebook code runs with your Windows user rights, like any local Jupyter.

### Studio on a server (pilot)

SAS SpeedyStore Studio can also run on a server for several colleagues, without Claude: a small hub signs users in
with their SAS user on SAS Logon (Viya), starts one Studio per user with its own folder, and proxies it behind
`https://<studio host>/studio/`. SAS work runs as the signed-in user (their Viya token is pre-loaded); each user adds
their own SingleStore connection. Notebooks and Ask Claude are off in this first step.

```powershell
.\scripts\studio-hub-local.ps1          # try it on this PC: http://127.0.0.1:8800/
```

Container image, Kubernetes manifests and every setting: [deploy/README.md](deploy/README.md); what the Viya and
Kubernetes admins do (OAuth client in SAS Logon, ingress/TLS, storage, network, secrets):
[deploy/ADMIN-GUIDE.md](deploy/ADMIN-GUIDE.md).

## Setup

Uses [`uv`](https://docs.astral.sh/uv/) to manage the Python environment,
pinned to Python 3.12 via [`.python-version`](.python-version):

```bash
cd singlestore-mcp-server
uv sync
```

This creates `.venv/` and installs everything from `uv.lock`.

Then tell the server how to reach your cluster. **Recommended:** register the
server *without* credentials and add the connection in the **Connections**
window (`connections_window`, or Connections in the Workspace). The password
then goes to your OS credential store (Windows Credential Manager, macOS
Keychain, Secret Service on Linux), never into a config file. The installer
opens Connections by itself when there is no connection yet.

The older way, SINGLESTORE_* environment variables in the MCP config, still
works: they show up as the read-only connection "Environment variables". The
password then sits in the config file in plain text; see
[Moving the password out of the config](#moving-the-password-out-of-the-config).
Supported variables (see [`.env.example`](.env.example)):

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
is open — register it once at the user level instead, from a terminal, with
no credentials (add the connection in the Connections window afterwards):

```bash
claude mcp add singlestore -s user -- uv run --no-sync --directory "C:\path\to\singlestore-mcp-server" singlestore-mcp-server
```

`claude mcp add -e SINGLESTORE_PASSWORD=…` also works, but stores the password
in plain text in `~/.claude.json`.

#### Either way

Run `/mcp` inside a Claude Code session to check the `singlestore` server's
connection status and see the tools it exposes. If you just registered it
(either option) in a session that's already running, its tools won't appear
until you restart that session — MCP servers are only loaded at startup.

### Claude Desktop

Claude Desktop (the standalone app, not Claude Code) uses a different,
app-level config file — there's no per-project `.mcp.json` support and no
`${VAR}` expansion. Leave the credentials out of it and add the connection in
the Connections window.

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
           "run", "--no-sync",
           "--directory", "C:\\path\\to\\singlestore-mcp-server",
           "singlestore-mcp-server"
         ]
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

If you do put SINGLESTORE_* variables with a password in this file, it is
stored in plain text; treat it like any other credentials file. Logs for debugging a failed connection live in
`%APPDATA%\Claude\logs\mcp-server-singlestore.log` (Windows) or
`~/Library/Logs/Claude/mcp-server-singlestore.log` (macOS).

### Moving the password out of the config

If your `~/.claude.json` or `claude_desktop_config.json` still has
`SINGLESTORE_PASSWORD` in the `singlestore` entry, move it to the credential
store:

```powershell
uv run --no-sync python -m singlestore_mcp.move_password            # dry run: shows what it would do
uv run --no-sync python -m singlestore_mcp.move_password --apply    # do it (quit Claude Desktop and Claude Code first)
```

For every config entry with SingleStore credentials (Claude Code, Claude
Desktop including the Microsoft Store copy, and `claude_desktop_config*.json`
backups next to it) it uses the saved connection with the same host, port and
user, or saves a new one with the password in the credential store; makes it
the active connection if none is; and removes the SINGLESTORE_* connection
variables from the entry. It never prints the password, leaves `${…}`
placeholders alone, and stops at an entry whose password differs from the one
already saved (fix that in Connections and run it again).

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

## Testing

The test suite lives in `tests/` (pytest + Playwright); a performance report lives in `perf/`.

**Prerequisites**

- The dev dependencies in `.venv` (`uv sync --group dev` when no server is running from this checkout).
- **VPN / cluster access** for the `cluster` tests. The test server signs in with your active saved
  connection (set `S2_TEST_CONNECTION=<name>` to use another one); only without saved connections does it
  fall back to the `env` of the `singlestore` entry in `~/.claude.json`. If the cluster can't be reached,
  those tests are skipped and the offline tests still run.
- **Microsoft Edge** for the `ui` tests. Playwright drives the installed Edge (`channel="msedge"`), so no
  `playwright install` is needed. Set `S2_TEST_BROWSER_CHANNEL=chrome` (or `chromium` for Playwright's
  bundled browser) to use another browser.
- `node` on PATH for the JavaScript syntax checks (they are skipped without it).

**Running**

```powershell
.venv\Scripts\python.exe -m pytest -m "not cluster and not ui"   # offline subset (no VPN): lint, JS syntax, protocol, workload unit tests
.venv\Scripts\python.exe -m pytest                               # everything except the assistants and SAS (~8-12 min)
.venv\Scripts\python.exe -m pytest --assistants                  # also ask the in-app Claude assistants (uses Claude)
.venv\Scripts\python.exe -m pytest --sas                         # also the SAS Viya tests (slow: cold SAS sessions; before a release or after SAS changes)
.venv\Scripts\python.exe -m pytest tests\test_workload.py        # one file
.venv\Scripts\python.exe -m pytest -k "sql_editor or copy"       # by name
.venv\Scripts\python.exe -m pytest tests\ui --headed              # watch the browser tests
```

**On GitHub:** the offline subset and `ruff` run automatically on every push and pull request,
on Windows and Linux ([`.github/workflows/tests.yml`](.github/workflows/tests.yml), the
**Actions** tab). The cluster, browser, SAS and assistant tests only run locally.

Use the venv's Python directly, or `uv run --no-sync`. A plain `uv run` tries to sync, and that fails
while Claude has the server's .exe open.

**Markers**

| Marker | Meaning |
|---|---|
| `cluster` | Needs the SingleStore cluster. Skipped when `list_databases` fails. |
| `notebook` | Starts a notebook kernel (slow). |
| `sas` | Needs a signed-in SAS Viya. Skipped unless `--sas` is given (slow: cold SAS sessions), and when `sas_viya_test` fails (see below). |
| `assistants` | Asks the in-app Claude assistants. Runs only with `--assistants`. |
| `ui` | Drives the apps in a browser with Playwright. Most also carry `cluster`; `ui/test_connections_ui.py` needs neither the cluster nor Viya. |

**Files**

- `test_static.py`: ruff, `node --check` of every inline app script and the apps' JS, and the browser-view
  allowlist check. Every tool an app calls must be an app tool or listed in `_EXTRA_TOOLS`.
- `test_protocol.py`: tools, `ui://` resources and prompts, plus the browser view's origin check and allowlist.
- `test_tools.py`: model-visible tools, including Claude's 50,000-character result limit; app-only tools;
  error handling and safety guards; local writes.
- `test_workload.py`: placeholders, validation and run comparison (offline), and a real 20 s run (cluster).
- `test_network_recovery.py` (offline, ~35 s): network breaks without a restart. The pool against a tiny
  MySQL-protocol server (`fake_mysql.py`) and the SAS Viya client against a stand-in Viya (`fake_viya.py`), each
  behind a fault-injecting TCP proxy (`faultproxy.py`: drop, blackhole, refuse, slow, come back, move): dropped and
  silent connections replaced, a readable error during an outage and success after it, a hanging login / full pool
  bounded, a query on a connection whose route moved retried, SAS sessions restarted with a notice, the pre-warm
  retrying at once, proxy / direct re-chosen, and an MCP protocol test (outage during a tool call → readable error →
  the same server answers again). `ui/test_network_ui.py` (cluster): the Workspace through a fault proxy to the
  real cluster shows "Connection to SingleStore lost – retrying…", recovers by itself and runs queries again.
- `test_contrast.py` (offline): WCAG contrast of the engine-chip, primary-button, alert-badge, caslib-badge and
  "Ask Claude" colours in `shared.css`, light and dark. `test_presenter.py` (offline): presenter-mode labels and the
  "Studio's own queries" classifier.
- `ui/test_studio_ui.py`: where code runs (S2 chips, run history, Notebook cells, Workload; with `--sas` also SAS
  Compute, CAS and the pushdown chip), the Open / Save as dialogs' keyboard behaviour, presenter mode (no real
  connection name, host, user or e-mail in any view; results unmasked; off restores), the Studio's own queries hidden,
  and contrast measured on the rendered page in both themes.
- `test_sas_guide.py`: the assistant's SAS guide per language (offline: only the tab's language, once per
  session and language, the tab context in the question), the explicit pass-through write check, the SAS function
  matrix and its check in Python and in node (`vendor/sas-funcs.js`); with `--sas` a sample of the matrix's
  verified rows run again. `test_assistants.py` with `--assistants --sas`: Claude's PROC SQL / FedSQL / CAS
  answers run in SAS. `ui/test_sas_ui.py`: the pre-run *Check the functions* bar.
- `test_notebook.py`, `test_compare.py`, `test_assistants.py`, and `ui/test_apps.py` (Workspace views,
  SQL Editor, Query History, Workload, every standalone app). When a UI test fails, a screenshot is saved
  in `tests/ui/_artifacts/`. The UI fixtures (`ui`, the screenshot hook) are in `tests/conftest.py`, so one
  call can mix files from `tests/` and `tests/ui/` (`pytest tests\test_x.py tests\ui\test_y.py`).

**Isolated test home**

The suite starts one server (`python -m singlestore_mcp.server`, over stdio, through the restart
supervisor) with its own `SINGLESTORE_MCP_HOME` in pytest's temp folder. It does not use
`~/.singlestore-mcp`.

- These files are copied in from `~/.singlestore-mcp` when present: `connections.json`, `sas.json`,
  `query_history.db` and `singlestore_bundle.pem`. Passwords aren't in `connections.json`; the test server
  reads them from the OS credential store like the real one.
- `notebook-env` is linked in through a directory junction. At the end, only the junction is removed,
  never its target.
- SQL and notebook files go to a temporary `SINGLESTORE_MCP_SQL_DIR`.
- Background alerts and the metrics sampler are off (`SINGLESTORE_MCP_ALERTS=0`, `SINGLESTORE_MCP_METRICS=0`);
  the History tests take their samples with `sample_now`.

The tests are read-only on the cluster. Write statements are sent only to check that they are refused or
need confirmation, and are never confirmed. Scratch names start with `zz`.

**Which SAS Viya sign-in the `sas` tests use.** Your own sign-in lives in an encrypted secrets file that is
not copied by default. With no sign-in in the test home, the test server does what the real one does: it
reuses the SAS Viya MCP server's (`~/.sas-mcp-server/credentials.json`, from `sas-mcp-login`) or the SAS
Viya CLI's cached sign-in when that Viya accepts it. That is why the SAS (and SAS notebook) tests pass
without `S2_TEST_SAS_SECRETS` as long as the SAS Viya MCP server is signed in to Zeus. The `sas` fixture
prints the sign-in it got ("SAS tests sign in to SAS Viya with: …"). `S2_TEST_SAS_SIGNIN` makes the choice
explicit:

| `S2_TEST_SAS_SIGNIN` | Sign-in |
|---|---|
| `any` (default) | a copy of yours with `S2_TEST_SAS_SECRETS=1`, else another SAS tool's cached sign-in |
| `own` | only a copy of yours (secrets copied; the test server runs with `SINGLESTORE_MCP_SAS_IMPORT=0`) |
| `import` | only another SAS tool's cached sign-in (nothing copied) |

The test server may refresh the token in its copy only. `SINGLESTORE_MCP_SAS_IMPORT=0` also works for the
real server, if it should never reuse another SAS tool's sign-in.

The test home's `sas.json` is written in the current format (an old single-settings file is converted
there, so the test server never moves the real sign-in). To pick the Viya the `sas` tests use:
`S2_TEST_SAS_CONNECTION` (a saved SAS Viya connection's name), or `S2_TEST_SAS_URL` (adds a connection
with that address, named `S2_TEST_SAS_CONNECTION` or "Test Viya", optionally with `S2_TEST_SAS_CONTEXT`
as compute context). `S2_TEST_SAS_STARTUP` sets the Workspace startup code the SAS tests use.
`test_sas_viya.py` (offline) covers the connection list, migration and sign-in rules.

**Performance**

```powershell
.venv\Scripts\python.exe perf\perf.py            # ~6 min: cold start, latency p50/p95, concurrency, large results, 60 s soak, memory
.venv\Scripts\python.exe perf\perf.py --quick    # ~2 min
```

Results are written to `perf/results/<version>-<timestamp>.json` (gitignored). Each run is compared with
the previous results file, and p50/p95 changes over 25% are printed as warnings (never a failure).

```powershell
.venv\Scripts\python.exe perf\startup.py         # ~3 min: how fast the Workspace opens, cold and after restart_server
```

`perf/startup.py` measures the imports, server start → first tool answer → the calls the Workspace and
its SQL view make on open (time and size of each), `restart_server` → Workspace usable, the Workspace in
Edge (browser view: page → SQL Editor → schema tree, with every request), and the cluster round trips
(connects + statements) of the open sequence. Results go to `perf/results/startup-<timestamp>.json`
and are compared with the previous startup run. Run it in a terminal without `HTTPS_PROXY` (a proxy that can't
reach Viya makes the SAS pre-warm's first request wait for it).

Where the time goes (2026-10-26, medians of 6 runs, `startup-*-ab-base/new-*.json`; the machine varies ±30%):

| Scenario | Before | After | Mostly |
|---|---|---|---|
| Cold: spawn → handshake | 3.1 s | 3.0 s | importing the MCP SDK (~1.3–2.4 s, it loads client, HTTP and auth modules too), singlestoredb (~0.5 s), this package (~0.4 s) |
| Cold: spawn → Workspace usable (open sequence done) | 4.5 s | 3.9 s | the above + the open sequence |
| Cold: open sequence | 1.26 s | 1.01 s | `sql_editor` (1 round trip), schema (now read ahead) |
| `restart_server` → Workspace usable | 1.95 s | 0.84 s | was: waiting for a new connection (~1.1 s); now: loading this package (~0.4 s) + 1 round trip |
| Browser view: link → schema tree shown | 1.13 s | 0.64 s | the schema (3 queries, ~450 ms) now read while `sql_editor` runs |
| Browser view: link → editor ready | 0.59 s | 0.57 s | parsing the SQL Editor's page (1.2 MB: CodeMirror, the ext-apps bundle) |
| First SAS action after opening | ~45–60 s cold | the warm-up starts with `sql_editor` | Viya starting the Compute server (`POST …/sessions` 35–42 s) |

A connection costs ~8 round trips (~1.1–1.5 s): TCP, greeting, TLS (2), login, and the driver's
`SET NAMES` / `enable_extended_types_metadata` / `vector_type_project_format` (one each).
