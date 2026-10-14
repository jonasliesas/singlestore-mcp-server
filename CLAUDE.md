# SingleStore MCP server + Workspace – notes for Claude

MCP server for SingleStore (SAS SpeedyStore) with a browser "Workspace" of MCP Apps
(SQL Editor, Schema Explorer, Notebook, Cluster Monitor, Query History, Workload, Security, Config, …).
The owner (Jonas, SAS pre-sales) usually writes Norwegian – reply in the language he writes in.
Backlog and agreed designs: [ROADMAP.md](ROADMAP.md). User docs: [README.md](README.md).

## Layout
- `src/singlestore_mcp/server_impl.py` – the MCP tools and prompts; `server.py` / `supervisor.py` start it.
- `src/singlestore_mcp/apps/` – one `<name>.py` (tools + data) and `<name>.html` (view) per app.
  `workspace.html` is the shell (rail of views, tabs); `browser_host.html` + `browser_view.py` serve the
  apps in a normal browser – only app tools and `_EXTRA_TOOLS` in `browser_view.py` are callable there,
  so a new tool an app calls must be added to that allowlist.
- `apps/shared.js` / `shared.css` – common helpers: `S2.h` (DOM builder; sets `value` as a property),
  `S2.copy`, `S2.openView`, error banner. Charts in `apps/vendor/s2-charts.js`.
- State lives under `SINGLESTORE_MCP_HOME` (default `~/.singlestore-mcp`): SQLite history
  (`history_store.py`), SQL Editor drafts/tabs (`sql_editor_state.py`), workloads, connections.
- `sas_viya.py` / `sas_ep.py` – SAS Viya access and the SAS EP (embedded process) analysis.

## Running and testing
- Start the server with `uv run --no-sync` (plain `uv run` fails on the locked .exe). After code
  changes, restart the running server with the `restart_server` tool.
- Tests (pytest + Playwright, Edge channel by default; isolated `SINGLESTORE_MCP_HOME` in a temp folder):
  - `.venv\Scripts\python.exe -m pytest -m "not cluster and not ui"` – offline subset, no VPN
  - `.venv\Scripts\python.exe -m pytest` – everything except the assistants (~4–5 min, needs VPN)
  - `--assistants` also asks the in-app Claude assistants (uses Claude)
- Markers: `cluster`, `notebook`, `sas`, `assistants`, `ui` (`--strict-markers`). Lint: `ruff check`.
- Add a test for every bug fixed in a UI app (`tests/ui/test_apps.py`) and every new tool.
- `perf/perf.py` – timing report; the cluster is ~120 ms+ per round trip, so fewer, pooled and
  parallel queries matter more than anything else.

## Rules
- **Cluster is read-only** for Claude except actions the user confirms in the app (e.g. Analyze,
  workloads). Never change shared infrastructure (users, roles, cluster settings, caslibs) – give the SQL.
- **Never run PROFILE on SAS EP calls** (`sasepFunction`, `EP.h<32hex>_EP(...)`).
- **No secrets in the repo.** The SingleStore password comes from env (`SINGLESTORE_*`); never print or
  log it. Check the diff for passwords/tokens before every commit.
- SAS connection files in `/Users/norjni/My Folder/mcp` are read with `%include`; never show their contents.
- SingleStore syntax/capability questions: check docs.singlestore.com, don't guess. Table names in
  `SASDP` are case-sensitive.
- Match existing style: plain JS with `S2.h`, no frameworks; small focused changes.

## Releases
1. User tests → fix findings → full test suite green.
2. Bump `version` in `pyproject.toml`, write release notes since the previous tag, update README.
3. Commit on a branch (messages end with the Co-Authored-By line).
4. **The user pushes** – pushes from Claude's environment are blocked; never work around the proxy.
5. Create the GitHub release (github.com/jonasliesas/singlestore-mcp-server).
