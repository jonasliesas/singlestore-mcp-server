# singlestore-mcp-server

A local MCP server for SingleStore that runs in **stdio** mode from Claude
Code, Claude Desktop or VS Code, and works with **SingleStore Helios and
self-managed clusters** alike.

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
- **Standalone workspace**: the same apps in their own window from a desktop
  shortcut, without Claude.

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
- Saved as standard `.ipynb` files, with SQL cells as `%%sql` cells, so they
  open in Jupyter / VS Code and SingleStore Notebooks, and SingleStore's
  example notebooks open here.

**Python environment and packages**

- Python runs in its own environment, `~/.singlestore-mcp/notebook-env`,
  installed with `uv` the first time (the notebook offers an **Install**
  button). It has what SingleStore's examples expect: pandas, matplotlib,
  singlestoredb, SQLAlchemy with the SingleStore dialect, ibis, scikit-learn.
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

**Claude**

- The **Claude** button opens a chat panel that sees the notebook's cells and
  outputs, checks the database (read-only) and answers with SQL or Python
  you insert as a new cell, or use to replace the selected one.

### Schema Explorer, Pipeline Monitor, Cluster Monitor, Query Grid

| App (tool) | What it shows and does |
|---|---|
| **Schema Explorer** `schema_explorer(database?, table?)` | Databases → tables with row counts and sizes; per table the columns, DDL (shard / sort keys) and a row preview. **Ask Claude** and **Query in grid**. |
| **Pipeline Monitor** `pipeline_monitor(database?)` | Every pipeline's state, source → table, progress (files loaded / Kafka lag), latest batches and errors. Start / Stop (with confirmation), Test, error details, **Ask Claude**, auto-refresh. |
| **Cluster Monitor** `cluster_monitor()` | Per node SingleStore CPU (against its core limit), memory and disk with a 15-minute CPU chart (all / aggregators / leaves), plus the queries running now. Refreshes every 5 s. |
| **Query Grid** `query_grid(sql, database?, max_rows=1000)` | Read-only results as a sortable, filterable grid with CSV export; the SQL can be edited and re-run. Writes are refused. |

`TEST PIPELINE` loads no data, but a failed test is still recorded in the
pipeline's batch history and error log.

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
variables.

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
