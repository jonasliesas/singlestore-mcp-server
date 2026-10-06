# singlestore-mcp-server

A local MCP server for SingleStore, meant to run in **stdio** mode from VS
Code. It's built on two official SDKs rather than reimplementing protocol or
driver code:

- [`mcp`](https://github.com/modelcontextprotocol/python-sdk) — the official
  Model Context Protocol Python SDK. It handles the stdio transport, JSON-RPC
  framing, tool-schema generation (`MCPServer`) and the MCP Apps extension.
- [`singlestoredb`](https://github.com/singlestore-labs/singlestoredb-python)
  — SingleStore's own official Python client. It handles the actual database
  connection.
- [`@modelcontextprotocol/ext-apps`](https://github.com/modelcontextprotocol/ext-apps)
  — the official MCP Apps browser client, vendored and inlined into the
  interactive UIs (see [MCP Apps](#mcp-apps)).

Everything in [`src/singlestore_mcp`](src/singlestore_mcp) is glue: a
connection wrapper ([`db.py`](src/singlestore_mcp/db.py)) and a set of MCP
tools ([`server.py`](src/singlestore_mcp/server.py)), including first-class
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

Interactive apps (see below): `pipeline_monitor`, `query_grid`, `schema_explorer`,
`cluster_monitor`, `sql_editor`.

## Standalone workspace (desktop shortcut)

The workspace also runs without Claude, in its own window:

```bash
uv run python scripts/make_shortcut.py --database SASDP
```

creates a **SingleStore Workspace** shortcut on the desktop. It runs
`pythonw -m singlestore_mcp.workspace_app` (no console window), which starts
the app server on `127.0.0.1:8790` and opens the workspace in an Edge app
window (`--browser` for the default browser). Clicking it again while it runs
just opens another window; the server stops 5 minutes after the last window
closes. It uses the `SINGLESTORE_*` environment variables (set them as user
environment variables), and the editor's chat uses the in-editor assistant.

## Prompts (slash commands)

In Claude Code the server's prompts show up as slash commands:

| Command | What it does |
|---|---|
| `/singlestore:workspace` | Open the SingleStore Workspace (optional database, view) |
| `/singlestore:explain_query` | Explain a query, run `EXPLAIN`, suggest a faster version |
| `/singlestore:create_pipeline` | Build a pipeline from an S3 path / Kafka topic (asks before creating) |
| `/singlestore:pipeline_health` | Check all pipelines for errors, stalls and lag |
| `/singlestore:table_report` | Size, storage, keys and data profile of a table |
| `/singlestore:restart` | Restart the server so code and app changes load |

### Restarting without reconnecting

The process Claude starts is a small relay ([`supervisor.py`](src/singlestore_mcp/supervisor.py))
that runs the real server as a child process. The `restart_server` tool (or
`/singlestore:restart`) replaces that child, replays the MCP handshake and
tells Claude that the tools, prompts and resources changed, so Python and app
changes load in a few seconds without touching `/mcp`. Browser links made
before a restart stop working. Set `SINGLESTORE_MCP_NO_SUPERVISOR=1` to run
the server without the relay.

## MCP Apps

In hosts that support [MCP Apps](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp)
(e.g. Claude), these tools render an interactive UI inline in the chat
instead of plain text. In other hosts they still return a normal text result.

| Tool | What it shows |
|---|---|
| `pipeline_monitor(database?)` | Every pipeline's state, source → target table, latest batch, batch history and recent errors. Start / Stop (with confirmation), Test, error drill-down, "Ask Claude" to diagnose an error, optional 10 s auto-refresh. |
| `query_grid(sql, database?, max_rows=1000)` | Read-only query results as a sortable, filterable, paginated grid with CSV export. The SQL can be edited and re-run from the grid. Only SELECT / WITH / SHOW / DESCRIBE / EXPLAIN are accepted; writes are rejected before reaching the database. |
| `schema_explorer(database?, table?)` | Databases → tables tree with row counts and sizes, and per-table columns, DDL (shard/sort keys) and a row preview. "Ask Claude" and "Query in grid" hand the table back to the chat. |
| `cluster_monitor()` | Every node (aggregators and leaves) with SingleStore CPU (against the node's core limit, with a short history), memory against `max_memory`, and disk space and read/write throughput; plus the queries running right now (expand a row for the full SQL and "Ask Claude"). Auto-refreshes every 5 s. |
| `sql_editor(database?, sql?, view?, table?)` | Opens the **SingleStore Workspace** (see below) on its SQL view: schema tree, SQL editor (CodeMirror) and results pane, split top/bottom with a draggable divider. Autocomplete for SQL and SingleStore keywords, SingleStore built-in functions (with signatures), your functions/procedures, databases, tables and columns (other databases load as you type `db.`). Ctrl+Enter runs the statement at the cursor or the selection. Reads run immediately; statements that change data or schema ask for confirmation first. A **Chat with Claude** tab answers questions about the editor's SQL; each SQL block in an answer gets Replace / Insert / Copy buttons. Answers come either **here in the editor** (default; see "In-app assistant" below) or **in the Claude chat** (the question goes to the chat box, you click Send, and Claude answers into the editor with `sql_editor_reply`). |

### SingleStore Workspace

`sql_editor` opens a workspace that combines the apps in one window: a slim
rail on the left switches between **SQL** (the editor), **Schema** (Schema
Explorer), **Pipelines** (Pipeline Monitor) and **Cluster** (Cluster
Monitor), with Open in browser / Full screen at the bottom. `view` picks the
view it opens on (`sql`, `schema`, `pipelines`, `cluster`) and `table`
pre-selects a table in the Schema view. Each view loads the first time you
open it and keeps its state when you switch away; hidden views pause their
auto-refresh. In the Schema view, **Query in grid** opens the table's query in
the SQL view and runs it. The standalone apps still work on their own.

The workspace acts as a small MCP Apps host for its views: each view runs in
its own sandboxed iframe, loaded with `resources/read`, and the workspace
relays its tool calls, chat messages and model-context updates (merged into
one context, so Claude knows which view you're on) to the real host.

### SQL files (SQL Editor)

**Open** / **Save** (Ctrl+S) / **Save as** in the editor's header work on
`.sql` files in a folder on the machine running the MCP server, by default
`Documents\SingleStore SQL` (set `SINGLESTORE_MCP_SQL_DIR` to use another
folder). Subfolders are allowed (`reports/sales.sql`); paths outside the
folder are refused, and replacing an existing file asks first. The header
shows the current file name and a ● for unsaved changes. This works inside
Claude and in the browser view.

**Browse computer…** (in the Open panel) opens any local file with the
browser's file picker; saving it stores a copy in the SQL folder. **Download**
(in Save as) saves the SQL through the browser instead, where the host allows
downloads (the browser view does; Claude may not).

### In-app assistant (SQL Editor chat)

With "Answer: here in the editor", the server answers editor questions itself
by running Claude Code headless (`claude -p`) with your own Claude login: no
API key, no Send click in the chat, and it works in the browser view too.
Progress ("Looking at the schema…", "Running query 2…") and a Stop button
show while it works; each editor keeps one Claude session, so follow-up
questions have context. Each editor keeps one Claude Code process running (streaming
input), started in the background when you open the chat tab, so questions don't
wait for Claude Code to start: they go straight to the model and the
read-only tools. Idle processes close after 10 minutes (at most 4 run
at once); changing Speed or pressing Stop restarts the process and resumes
the same conversation.

The headless session has no built-in tools (no shell or file access) and one
MCP server: this package started with `python -m singlestore_mcp.assistant
--serve-readonly`, which offers only `list_databases`, `list_tables`,
`describe_table` and `read_query` (the Query Grid's read-only guard, at most
200 rows). It can't change data; it gives you statements to review and run.

Requirements and settings:
- Claude Code installed and logged in (`claude`, then `/login`, once in a
  terminal). If it's missing, the editor offers only the Claude chat.
- It uses your Claude plan's usage like any other Claude Code session; its
  sessions are kept under `%LOCALAPPDATA%\singlestore-mcp\assistant`.
- **Speed** in the chat options picks the profile: *Fast* (Haiku),
  *Balanced* (Sonnet, medium effort; default) or *Thorough* (Opus, high effort).
- Every question also gets the SingleStore skill
  [`skills/singlestore-sql/SKILL.md`](src/singlestore_mcp/skills/singlestore-sql/SKILL.md):
  key SingleStore SQL learnings (case-sensitive names, one table per `DROP`,
  no `GROUP_CONCAT(DISTINCT … ORDER BY)`, procedure syntax, pipelines,
  monitoring views). Add to it as you find new pitfalls. It's a standard Claude
  skill, so you can also copy the folder to `~/.claude/skills/` for Claude Code itself.
- Optional environment variables: `SINGLESTORE_MCP_CLAUDE` (path to the
  `claude` executable), `SINGLESTORE_MCP_ASSISTANT_MODEL` /
  `SINGLESTORE_MCP_ASSISTANT_EFFORT` (the Balanced profile; default `sonnet` /
  `medium`), `SINGLESTORE_MCP_ASSISTANT_TIMEOUT` (seconds, default 300).

The model gets a compact text summary (e.g. the first 20 rows); the full data
goes to the UI only, via `structuredContent`, which the MCP Apps spec keeps
out of the model's context. Helper tools the UIs call for refreshes and
drill-downs are marked app-only, so they don't clutter the model's tool list.

Layout: [`src/singlestore_mcp/apps/`](src/singlestore_mcp/apps) — one
`<name>.py` (tools) + `<name>.html` (UI) per app, shared `shared.js` /
`shared.css` helpers, and the official ext-apps client in `vendor/`, inlined
at startup so the apps need no CDN or internet access. The SQL Editor also
inlines a CodeMirror bundle (`vendor/codemirror-bundle.js`, MIT) built by
`scripts/build_codemirror` (`npm install && npm run build`) and a list of
SingleStore built-in functions (`vendor/singlestore_functions.json`).

Each app has a **⤢ Full screen** button (Esc to exit) when the host offers
fullscreen display mode; the grid and explorer then use the full height.

**↗ Open in browser** reopens the current view (same database, query or
table) full-window in your normal browser, for when the chat column is too
narrow. Each app's result also carries this link (`browser_url`), which
Claude posts under the app, and the `browser_link` tool creates one on
request. If the host won't open links itself, the button shows the link with
Copy / "Put link in chat". The server starts a small web server on `127.0.0.1` on first use and
gives each run a secret link; it only accepts calls from its own page and
only runs the apps' tools plus start/stop/test pipeline. Links stop working
when the MCP server restarts. Actions taken there (e.g. Start/Stop) don't go
through Claude's approval prompt — the apps' own confirmations still apply —
and "Ask Claude" is only available inside Claude.

Notes:
- `TEST PIPELINE` loads no data, but a failed test is recorded in the
  pipeline's batch history and error log like a real batch.
- Each button that calls a tool goes through the host, which may ask you to
  approve app-initiated tool calls.

### Developing apps

`scripts/dev_host.py` is a local stand-in for Claude: it starts the real
server over stdio, renders an app in a sandboxed iframe and speaks the MCP
Apps protocol to it, against your real cluster.

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
