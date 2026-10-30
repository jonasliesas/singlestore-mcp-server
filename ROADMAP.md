# Roadmap

Agreed with the owner; confirm scope and order before starting a round. Details of what was built are in the
README and the git log; this file keeps what's next.
Last updated 2026-10-30 (latest release 0.10.1; design round from `studio-design-review.md`; network-break recovery;
review polish round on branch `claude/polish`).

## Released
- **0.9.0** (2026-10-14) – Workload, SQL Editor tabs/history/SELECT builder, Security and Config views (read-only),
  Cluster Monitor and Query History extensions, pytest + Playwright suite.
- **0.10.0** (2026-10-14) – SAS in the Workspace (Viya REST client, SAS/CAS sources, Run as PROC SQL / FedSQL /
  CAS, pushdown verdict), several Viya connections, Security changes from the GUI, Cluster Monitor Health /
  History / Backups, SAS EP tuning, table skew, streaming results, ~3× faster start-up, crash back-off, safe
  state files on Windows, password moved out of Claude's config, GitHub Actions.
- **0.10.1** (2026-10-14) – SAS autocomplete fetches columns itself (case-insensitive), SAS session no longer
  restarted on a missing table, Viya connection retries, SAS tests opt-in (`--sas`).

## Since 0.10.1 (committed, not released – 0.11.0 planned the week of 2026-11-02: Jonas tests Monday, fixes, release before the Zeus deploy)
- **Load file into table** (2026-10-30, branch `claude/load-file`): Schema Explorer (database / table header) and the
  SQL Editor tree. Large Open dialog on This computer; detected encoding (UTF-8 / Windows-1252 / Latin-1), delimiter,
  quote, header, NULL markers (SAS `.`), decimal comma, date / time formats (SAS DATE9 / DATETIME too); 200-row preview;
  new table with an editable CREATE TABLE (conservative types, SORT KEY on a date, SHARD KEY on a unique id) or append
  with a by-name mapping; review = confirmation with the engine chip; above `SINGLESTORE_MCP_LOAD_LIMIT_MB` (100) a
  CREATE PIPELINE template (S3 / Azure, placeholders) and a second confirmation. Every row checked locally first (bad
  rows: line + reason, skip on request), then `LOAD DATA LOCAL INFILE ':stream:'` (UTF-8 TSV, all or nothing,
  `SKIP ALL ERRORS … ERRORS HANDLE` for the cluster's own refusals) or batched INSERTs in one transaction; Cancel
  aborts / rolls back and removes a new empty table; a network break is never re-sent ("may or may not"); row count
  checked; history entries. Off in server mode. Code: `apps/load_file.py`, `apps/vendor/s2-load-file.js`.
- **Reshard script** (2026-10-30, branch `claude/reshard-dropview`): Schema Explorer Distribution tab, next to a
  simulation result, "Copy to a new table with this shard key…" – a SQL script that never runs (Copy / Open in SQL
  Editor / Save as .sql, the Security basket's code now shared in `shared.js`): CREATE TABLE `<t>__reshard` from the
  real SHOW CREATE TABLE with only the SHARD KEY replaced, INSERT … SELECT (+ AGGREGATOR SYNC AUTO_INCREMENT), row-count
  and skew checks, the two-step rename swap, `-- DROP TABLE <t>__old`; warnings filled in (size ×2, pipelines with
  STOP / START, views and grants by name, existing work names, duration from the simulation, quiet window); unique
  keys without the new key refused (error 1744). Checked in the 9.0 docs and on 9.0.40 in `test`: no RENAME TABLE /
  atomic swap, renames keep dependent views working by name, AGGREGATOR SYNC refuses a backquoted table name (error
  1103 → database-wide sync). Code: `reshard.py`, `schema_explorer_reshard_script`.
- Security: **Drop view…** for column views (the "Grant columns (view)" form lists the views on the table; also next
  to views in a user's effective privileges and a role's privileges): preview lists the grants that go, which are
  revoked first (the docs don't say DROP VIEW removes them), then DROP VIEW; typed name to confirm, or into the
  script basket; creator / SUPER check (docs). New app-only `security_views`.
- Security: **SQL script instead of Run** – Copy / Open in SQL Editor / Save as .sql, script basket, password
  placeholder, works without change privileges.
- Security (not committed yet): **password change** (own password via SET PASSWORD without ALTER USER; optional
  update of the saved connection), **lock / unlock**, **failed-login lockout**, **REQUIRE SSL / NONE** (lock-out
  warning without TLS), **column grants through a view** (9.0.40 accepts `GRANT SELECT (cols)` but grants the
  whole table); REQUIRE SSL no longer shows as a privilege / in "revoke all"; user details reload after a change.
- SQL Editor: **open and save files on SAS Viya** – SAS Content and SAS Server (Compute file system), version
  check before overwriting, tabs remember their Viya file.
- Open / Save as as a **large dialog**; typing in its filter no longer jumps to the file name.
- Notebook: **Open / Save as as the same large dialog** (Escape closes; small prompts stay inline). **Interrupt**
  now stops a long `time.sleep()` on Windows (the kernel start-up code also sets CPython's SIGINT event); calls
  waiting in C (network read, lock, C loop) can't be interrupted on Windows, so after 5 s the cell offers
  **Restart kernel**, and a restart no longer waits behind such a cell.
- **CodeMirror sent once per Workspace**: vendor blocks are marked and `app_page(vendors=…)` leaves out the ones the
  Workspace already has – the Notebook's page after the SQL Editor 650 K → 72 K characters (perf/startup.py,
  `startup-20261016-*-before/after-vendor-cache.json`); standalone pages unchanged.
- Cluster view's **first open doesn't wait 1 s** for a CPU baseline: the metrics sampler reads one ~8 s after start
  and keeps it fresh (also in the process that skips the stored sample); no extra round trips on Workspace open (7).
- **Trend alerts** from the metrics History: disk projected full within 14 days, memory reaching max_memory within
  3 days (steady 24 h climb, days editable); History tab **Export CSV**.
- Tests: the flaky Cluster Monitor UI test was an app race (a re-render between mousedown and mouseup lost the click;
  renders now wait while a button is pressed) – 10/10 after, 2 of 8 failed before; UI fixtures moved to
  `tests/conftest.py`, so `tests/` and `tests/ui/` files mix in one call (pytest 9 built a second `tests/ui` node);
  SAS sign-in for the tests explicit (`S2_TEST_SAS_SIGNIN`, see README → Testing).

- SAS libraries (not committed yet, 2026-10-16): **SS / S2 badges** for SingleStore caslibs (SpeedyStore vs
  standard connector) and libnames; **automatic S2 libname** in the Workspace's SAS session from the active
  SingleStore connection (NOSOURCE, password never in log/history, re-assigned on connection/token change, can be
  turned off); **Show code** for a library / caslib (LIBNAME, CASLIB, table.addCaslib with placeholders);
  **SAS 9 libraries via SAS/CONNECT** (sign-on code per Viya connection, remote library services, SAS 9 source in
  the Schema Explorer; tested against CAUKI).
- SQL Editor SAS runs, round 2 (not committed yet, 2026-10-16): **Stop** for long SAS runs (Compute job cancel with
  the job's ETag / session cancel; CAS action cancel; partial log; "Restart SAS session" when the cancel doesn't
  work), time limit 4 h (`SINGLESTORE_MCP_SAS_TIMEOUT_MIN`, was 15 min); runs in the background with a **queue**
  ("waiting for the SAS run in tab X"); **per-tab SAS Viya connection** (one Compute/CAS session per connection;
  switching the active one restarts nothing); **paging** of SAS results (per-tab result tables, Compute data API
  start/limit, CAS `table.fetch` from/to, PROC SQL keeps 100,000 rows) and of confirmed SingleStore writes
  (streamed, sent once); **FedSQL pushdown SQL text** from DRIVER_TRACE=SQL on the automatic S2 library;
  **Find in Query History** opens the plan-cache entry for short queries; **tree source switch** (SingleStore /
  SAS / CAS) and language switch on tree use (asks when the tab has text).
- File browser and reshard (not committed yet, 2026-10-16): **rename / delete** in the file browser (This
  computer, SAS Content, SAS Server; one item in its folder, typed name for a folder, empty folders only, ETag /
  modified-time checks, protected roots and shortcut folders); SQL Editor tabs and Recent **follow a rename** (also
  tabs not loaded; a renamed Viya file's new version is handed over) and keep a **deleted file's text unsaved**;
  **Notebook Open / Save as on SAS Content / SAS Server** (`.ipynb`, `application/x-ipynb+json`, version check);
  **Security basket Save as .sql → choose the location**; Schema Explorer **reshard simulation** (Distribution
  tab: candidate shard key, optional sample, predicted vs current skew; the docs' PARTITION_ID() query, on a click).
- Browser tabs survive restarts (not committed yet, 2026-10-16): the browser view's **port and token are kept**
  per MCP client (`browser_address.py`: a slot lock held by the supervisor, `~/.singlestore-mcp/browser/slot-<n>.json`)
  and the next server listens on them at once (waits up to 15 s for its predecessor); the page **reconnects** (holds
  calls, "Reconnecting…", retries up to 60 s, `visibilitychange`/`online` retry at once; only read-only/idempotent
  calls are re-sent) and otherwise shows **Session ended** (app removed, polling stopped; unsent SQL Editor drafts kept
  in sessionStorage and saved after reconnecting); **Connections → Reset browser links** rotates the token; the
  standalone workspace reuses its address from `workspace.json`.
- SAS session pre-warm (not committed yet, 2026-10-23): when the Workspace opens (and when the active SAS Viya
  connection is switched, saved or signed in to) the **SAS session starts in the background** (`sas_prewarm.py`:
  Compute + automatic S2 libname + startup code, then CAS; never prompts; skips when not signed in / unreachable);
  a run or library list during the warm-up **waits for the same session** ("Waiting for the SAS session to
  start…"); **keep-alive** every 4 min while the Workspace is open (`GET …/sessions/{id}/state` resets Viya's
  `sessionInactiveTimeout`, 900 s on Zeus – checked: a 60 s session kept alive by state GETs for 200 s, its twin
  without them ended), an ended session is restarted in the background; **status badge** in the SQL Editor's SAS
  header and the Schema Explorer's SAS sources; per-connection setting (default on) and
  `SINGLESTORE_MCP_SAS_PREWARM=0` (the test suite's default). SAS 9 sign-on stays lazy.

- Faster Workspace open (not committed yet, 2026-10-26; README → Testing → Performance has the table):
  `sql_editor` **reads the editor's schema ahead** (the database given, else the active tab's draft database, else
  the first user database; the editor's first `sql_editor_schema` within 30 s gets it) – browser view link → schema
  tree 1.13 → 0.64 s; the Workspace asks for the **tab list on tool input** (with `sql_editor`, not after it); the
  browser host sends the app's **opening call while the page loads**; the **standby opens the server's database
  connections ahead** (`preconnect.py`, COM_PING every minute, at most an hour, password logins) – restart_server →
  usable 1.95 → 0.84 s; servers start with the **base Python directly** (no venv launcher: 4 Python processes per
  Claude session instead of 6); the **SAS pre-warm starts from `sql_editor`** (0.75 s after it answers) instead of
  only ~1.5 s after the page painted; a SAS **token renewal no longer blocks settings reads** (own lock: a slow SAS
  Logon held up every SAS call, e.g. 12 s behind a proxy).

- Design round "SAS SpeedyStore Studio" (not committed yet, 2026-10-26; review: `../studio-design-review.md`,
  screenshots before/after in `../studio-design-review/after/`):
  - **Where code runs**, everywhere SQL or code runs: Run buttons say the target in words ("Run on SingleStore ·
    prod", "Run in SAS Compute · Zeus", "Run in CAS · Zeus"; short "Run · S2" on narrow widths), *Run as* has a visible
    label, the status line says "Running on SingleStore… / in SAS Compute… / in CAS…", results, the run history and
    notebook cells carry an engine chip (`S2.engineChip`) with the connection, SAS runs on SingleStore libraries the
    pushdown outcome ("→ pushed down to SingleStore: full / partial / none"); the run history stores the connection and
    the pushdown verdict; Query History says "ran on SingleStore, sent by CAS / SAS Compute / SAS (embedded process)".
  - **Top 10 quick wins** done: product name + mark in the rail and the titles (browser tab "SAS SpeedyStore Studio",
    favicon: also the first-load 404); the editor's corner language chip removed (the Run button names the engine);
    CPU chart end labels fit the card; dark-mode contrast ≥ 4.5:1 for the primary button, count pills, alert badge,
    Run-as / SS / S2 colours (tokens in shared.css, `tests/test_contrast.py`); Open / Save as is a real modal
    (`S2.modal`: role=dialog, aria-modal, focus inside, Tab trapped, Escape anywhere, focus back) with a title and ✕;
    SQL Editor / Notebook as a flex column (a wrapping header no longer scrolls the page); labelled refreshes
    ("Refresh all / database / library", "Refresh usage / trends") and "Clean up work tables…"; Notebook "■ Interrupt"
    and "Restart kernel" with labels; one "Ask Claude" (`S2.askClaudeButton`, sparkle, secondary) in every view.
  - Small bugs: Config's last column (narrower category / value columns, labelled filters "Category / Sync /
    Settable"), "Click a point" under the p95 line chart, `--s2-accent`, badge case (Active, Open, Base table,
    Columnstore), focus ring on every button, toast live region created at start.
  - **Presenter mode** (rail "Present", `&present=1`; remembered per browser): ~20 % larger, connection names / hosts /
    IPs / users / e-mails / Viya addresses masked as "Connection A", "host-1", "user1", "viya-1" in every view
    (`S2.presenter`, labels from `presenter_labels`), query results and editors left as they are.
  - **Studio's own queries** hidden in Cluster → Queries and Query History unless "Show Studio's own queries" is
    ticked (`history_store.is_studio_query`: `/* s2-… */` marker, system-view reads, session checks).

- Network breaks without a restart (not committed yet, 2026-10-27; README → After a network break): `netguard.py` –
  short TCP keepalive on every cluster / Viya connection (10 s + 3×5 s instead of Windows' 2 h) and bounded connects
  (10 s TCP, 20 s cluster login); a watcher (2 s) that closes connections opened from a local address the route no
  longer uses (VPN on another gateway) and notices sleep / resume; idle pooled connections pinged (≤ 3 s) after such an
  event; a full pool waits ≤ 2 min; readable "… reconnects by itself" errors; a write cut off mid-way is never re-sent
  ("may or may not have run"); kept-alive Viya connections replaced after an event, a silent GET retried fresh; SAS
  sessions Viya ended meanwhile restarted with a one-time notice; SAS pre-warm retries after 10 s / at once on
  `online`; a token renewal over a dead network keeps the sign-in; notebook kernels reconnect at the next cell;
  health probes while down; Workspace line "Connection to SingleStore lost – retrying…" (`connection_health`) and
  `s2-reconnected` to the views. Tests: `test_network_recovery.py` (offline, fault proxy + fake MySQL / Viya, MCP
  protocol), `ui/test_network_ui.py` (cluster through a fault proxy).
- SQL Editor (not committed yet): **Ask Claude speaks the tab's SAS dialect** – the question carries the tab's
  language, library / caslib, tables and known columns, and once per session and language a SAS guide
  (`sas_guide.py`, rules checked in SAS); the **SAS function matrix** (`data/sas_functions.json`, SAS/ACCESS's own
  function map + every function run in PROC SQL / FedSQL / CAS with the pushdown verdict; `scripts/sas_functions.py`
  re-verifies and renders the README table) feeds the guide and a new pre-run **Check the functions** bar
  (unsupported functions, functions that stop pushdown on SingleStore libraries); explicit pass-through
  (`connect` / `disconnect`) no longer asks for confirmation; the FedSQL Pushdown verdict is *Pulled* when the
  driver trace shows only plain table reads (the plan claimed *Push Down* for GROUP BY + ORDER BY / LIMIT); every
  Compute run resets syntax check mode / `&SYSCC` (one failed DATA step made all later runs of the session fail).
  Measured: 15 / 80 → 80 / 80 of the assistant's answers run (README). Tests: `test_sas_guide.py`, `test_assistants.py`
  (`--assistants --sas`).

- Installer and desktop shortcut renamed **SAS SpeedyStore Studio** (agreed with Jonas 2026-10-30): the old
  "SingleStore Workspace" shortcut is replaced on the next install (pin it again if it was pinned); the shortcut's
  icon is the Studio mark (`scripts/studio.ico`, rendered by `scripts/make_studio_icon.py`). Program folder and MCP
  tool names unchanged.
- Design review polish (branch `claude/polish`, 2026-10-30; IDs from `../studio-design-review.md`):
  - **Empty states**: the SQL Editor's empty results pane offers example queries (B-2: "Top 10 cars by MSRP" when the
    database has CARS, "Rows per table in <db>" from TABLE_STATISTICS, the same cars query as PROC SQL with a SAS Viya
    connection; a click puts the SQL in and runs it; the keyboard hints folded into "Keyboard shortcuts"); Workload with
    no workload explains it in three steps with From history as the primary button and a sample workload on CARS (L-4).
  - **Result grids** (V-5): thousands separators in numeric columns (every digit kept; years / ids / codes ungrouped),
    a "#" column in the SQL Editor's grid; Export CSV and a copied selection keep the raw values (`S2.cellNumber`,
    `S2.rawCopy`); the Query Grid formats the same way.
  - **Toasts** (I-2): errors stay until dismissed (role=alert, "Error:"), info hides after 3.5 s but waits while
    hovered / focused, a ✕ on each, at most five, top centre while a dialog is open.
  - **Colour plus text** (A-5): Config's "changed" chip, Security flag severity as ▲ / ●, "▲ Errors" on a failing
    pipeline card.
  - **Keyboard**: arrow keys / Home / End in every tab bar (I-4; the focus stays on the tab through the redraws);
    the SQL Editor's tree has a roving tabindex, arrow keys, and shows a row's actions on focus (I-3).
  - **Buttons**: one danger style (`.s2-btn.danger`, `--s2-danger`, V-2); Security's account buttons one size with
    the rare ones under "More ▾", one "Change ▾" menu per table row (V-3, `S2.menuButton`).
  - Schema Explorer stat cards wrap instead of cutting off, Created shows the date (time in the tooltip) (L-2).
  - **One SAS session state** (C-3, bug 7): the badge says "starting… N s" while a view's library list starts the
    session (`sasSessionWatch.busy`, `started` from sas_prewarm); Connections re-reads the sign-in when shown again.
  - **Renders held while a button is pressed** in every view with timer re-renders (`S2.holdWhilePressed`: Cluster,
    Alerts, Pipelines, Query History, Security, Workload).
  - Tests: `tests/ui/test_polish_ui.py`, `test_contrast.py` (danger), `test_static.py` (render guard),
    `test_sas_prewarm.py` (`started`).
- Notebook **"Python in SAS" cells** (branch `claude/python-in-sas`, 2026-10-30; README → Notebook → Python in SAS
  cells): a new cell kind (`%%sas_python` + `metadata.singlestore.kind`) whose code runs as `PROC PYTHON` in the
  Workspace's SAS Compute session of the active SAS Viya connection (`sas_python.py`; `notebook_sas_python` /
  `notebook_sas_python_status`, the SQL Editor's `sas_runs` queue, pre-warmed session, `sas_stop`), so Python state
  persists between cells and WORK is shared with the SQL Editor's SAS runs; "Restart SAS session" in the header clears
  it. Output from the log: stdout without the `>>>` echo / NOTEs / page titles (`ls=max`, wrapped lines joined),
  tracebacks with `<cell>` line numbers, SAS ERROR / WARNING lines, ODS output via an ODS HTML5 file in WORK (inline
  SVG / images, read back through the Compute file API), the log collapsed; "Starting Python in SAS (first call ~8 s)…"
  until Python runs in the session; `endsubmit` refused, indentation removed. "SAS helpers" on the cell lists the
  `SAS` object's methods (checked with `dir(SAS)` on Zeus) and says to keep data on the server. Works without local
  kernels: in server mode the Notebook stays in the rail with Python in SAS and Text cells only. Ask Claude knows the
  kind (```sas_python blocks). Tests: `test_sas_python.py` (offline, logs recorded on Zeus in
  `tests/data/proc_python_logs.json`), `test_sas_python_viya.py` (`--sas`), `ui/test_pysas_ui.py`.

## Backlog
**Design review – what's left** (`../studio-design-review.md`; IDs from there)
- Bigger ideas: a Home / overview start view (1), responsive tables with column priorities (3: L-6 Query text and
  L-7 Security flags off-screen at 1024 px), one design-token layer for font sizes and the remaining hard-coded
  colours (4: engine / source / AI tokens done), `S2.pageHeader` (5).
- Presenter mode, what it doesn't do yet: hide demo-irrelevant chrome (keyboard-hint paragraphs, env-var notes, C-6);
  inside Claude Desktop it's remembered only where the app frame may use browser storage (the browser view always
  remembers it); masking is text-based, so a customer name used as a *database* or table name is only masked when it
  is also a connection or user name.
- Open: B-3 (grouped rail, README view table), B-4 (jargon tooltips), V-1 (one primary per region; Connections still
  has several), V-6/V-7, V-9/V-10, L-3, L-5 (pipeline cards), L-9 (Trends 2×2 grid, nice duration ticks), L-10, C-2
  (pushdown verdict is now in the status bar; still opt-in "Show pushdown"; no "SAS EP" quick filter), C-7, C-8 (column
  comments as chips), C-9, C-10, I-5 (skeletons), I-7, I-8, A-1 (`--s2-text-3` under 4.5:1; comes from the host in
  Claude), A-3 (input borders).
- Left from the polish round: I-4's `aria-controls` / tabpanel pairs (arrow keys done); I-3 only in the SQL Editor's
  tree (the Schema Explorer's tree rows are buttons already); the file browser's Rename / Delete row actions still
  show only on hover / focus; Workload's Run / Saved runs / Compare switch is a pressed-button group, not tabs.
- Tests: Playwright's hit testing is off inside CSS-zoomed frames, so presenter-mode UI tests click with JS.

**Security**
- Ideas: `REQUIRE X509 / SUBJECT` (mutual TLS); `PASSWORD EXPIRE` / `ACTIVATE`; re-apply locks after a cluster
  restart (restarts clear them). Drop view: check on 9.0.40 (with a zz_test_ role) whether DROP VIEW alone removes
  the grants on a view – if it does, the REVOKEs before it could go.
- Audit view – needs an admin: `auditlog_level` (e.g. ADMIN-ONLY) in the Operator's `spec.globalVariables`, the
  node log files shipped (Fluent Bit) into a table; then an Audit tab with filters. Today: guidance tab only.

**SAS in the Workspace**
- Python in SAS cells, what's left: they always use the *active* SAS Viya connection (no per-notebook pin like the
  SQL Editor's tabs); notebook **SAS cells** still run through saspy in the kernel with their own session, so their
  WORK isn't the Python in SAS cells' (`SAS.submit` is the way to run SAS code in that session) – moving SAS cells to
  the Workspace session too would also make them work in server mode; a run isn't stopped when its notebook tab is
  closed; ODS output is capped at 8 MB per cell.
- Ask Claude in SAS tabs (done, not committed – see "Since 0.10.1"): ideas left – a read-only SAS tool for the
  assistant (columns of any SAS table / caslib, `validate` a PROC SQL statement) so it doesn't depend on the columns
  the editor already knows; the "in the Claude chat" mode only gets the tab's language in the question, not the guide.
- CAS: no way to load a caslib's source table into the Workspace's CAS session from the editor (FedSQL in CAS only
  sees loaded tables; a PROC CASUTIL load in plain SAS code runs in Compute's own CAS session). A "Load into this
  session" tree action (`table.loadTable`, session scope) would let SpeedyStore caslib tables be queried live.
- FedSQL on a SingleStore library: a SELECT with a subquery in WHERE fails as the editor runs it (`CREATE TABLE
  WORK."_S2R…" AS …` is pushed down whole → SingleStore "Unknown database 'WORK'"; plain `proc fedsql; create table
  work.x as …` fails the same way, so it's FedSQL's). The assistant writes a derived-table join instead; the editor
  could warn before the run, or fetch such a SELECT without the CREATE TABLE (to check with SAS).
- Function matrix: re-run `scripts/sas_functions.py --verify` after a Viya / SAS/ACCESS update; rows for
  DATEPART / TIMEPART / HOUR … need a datetime column in the test tables; SQL_FUNCTIONS=ALL rows are from the engine's
  map, not run (the S2 library is assigned without it).
- FedSQL SQL text only for the automatic S2 library (DRIVER_TRACE is a LIBNAME option): libraries from the startup
  code could get `driver_trace=SQL …` in their own libname files if Jonas wants the text for them too.
- A second Compute session per connection (two tabs running at once) – today they queue; only if the queue hurts.
- SAS pre-warm: the keep-alive interval is fixed (4 min, the Workspace's poll); a compute context with an idle
  timeout under ~5 min would still lose its session between checks (read `sessionInactiveTimeout` from the session
  and poll faster if that ever matters). Optional: pre-warm a pinned tab's connection too, and the SAS 9 sign-on.
- SAS runs aren't stopped when their tab is closed (they finish in the background; Restart SAS session ends them).
- Automatic S2 libname: `authdomain=` instead of the password (needs a Viya admin to create the domain);
  TLS options for SAS/ACCESS to SingleStore if a cluster requires them (check the SAS/ACCESS docs – today the
  same options as the notebook kernels); a token connection (JWT / Entra) is untested against Zeus.
- SAS 9: the SQL Editor's "Run as" has no SAS 9 source of its own (the SAS 9 libraries are ordinary PROC SQL
  libraries once signed on from the Schema Explorer); "Show code" for SAS libraries the startup code assigned over
  SAS/CONNECT can't tell the remote libref (SAS reports the local one).
- Idea: show a tab's pinned SAS Viya connection in the Workspace tab bar (now in the editor's header and ⚙).
- File browser: rename / delete of several items at once, and moving between folders (today one item, in its own
  folder); a local delete goes straight away, not to the Recycle Bin.

**Load file into table – open items**
- Parquet and Excel (.xlsx): no reader among the dependencies (no pyarrow / openpyxl / pandas); .xlsx could be read with
  the stdlib (zip + XML, first sheet) if wanted; Parquet needs pyarrow (~100 MB) – decide before adding it.
- Server mode: upload from the browser (today the feature is shown as unavailable there).
- INSERT fallback: rows SingleStore refuses can't be skipped one by one (the transaction fails with the error and line
  range); LOAD DATA's `SKIP ALL ERRORS` covers it when local infile is allowed (it is on the test cluster).
- Gzip'd files (.csv.gz), several files at once, a fixed-width reader; type inference only sees the first 5,000 rows
  (a later value that doesn't fit is reported as a bad row – then widen the type in the CREATE TABLE).
- Presenter mode masks the path's user name and the connection like elsewhere; the CREATE TABLE text (a textarea) isn't
  masked (it only holds database / table / column names).

**Workspace / performance**
- Start-up, what's left (2026-10-26): a cold server is ~3 s of imports before the handshake, ~2 s of it the MCP SDK
  (its `__init__` loads the client, HTTP/auth and both protocol type sets) – an upstream issue, or a handshake the relay
  answers from a cache keyed by the code's file stamps (sends `list_changed` if the server's lists differ); each
  connection runs three `SET`s one by one (`SET NAMES`, extended types, vector format: ~0.4 s of its ~1.2 s) – a
  driver option or upstream change to send them as one; a sas_rest route learned "direct" (proxy skipped) is
  per process: a new server behind an `HTTPS_PROXY` that can't reach Viya waits for the proxy first again.
- singlestoredb compiles its Fusion SQL grammars on every start (~0.5 s of the connection import; no option skips
  it, also not on upstream `main`) – issue text ready for Jonas to file: `docs/upstream-singlestoredb-fusion.md`.

**SAS EP (with the SAS EP owners)**
- The fixed ~5 s wait before the EP's first SQL (no CPU or queueing in SingleStore; concurrent calls share one
  wait). Questions and hypotheses (DNS timeout / EP cold start / serialized step) are in the README.
- Re-check warm/cold split after lowering the trace threshold for a while; the daily `_hello_world` check fails
  because its output table exists (tell its owner).

## Server pilot (branch `claude/server-pilot`) – SAS SpeedyStore Studio for colleagues
Decided by Jonas 2026-10-30: colleagues (not customers), multi-user from the start, in SAS's network in the same
Kubernetes as the Zeus Viya, sign-in with SAS user + password via SAS Logon on Zeus (no Entra/SSO yet), notebooks off
in step 1, "Ask Claude" off by default (switchable later with an Anthropic / Foundry API key). Design background:
`docs/MULTI_USER_DESIGN.md` (local, not committed). How to run / deploy: [deploy/README.md](deploy/README.md),
[deploy/ADMIN-GUIDE.md](deploy/ADMIN-GUIDE.md).
- **Step 1 (built):** server mode for Studio (`studio_server.py`, `server_mode.py`: no stdio/Claude, encrypted file
  store with a hub-derived key per user, notebooks / Ask Claude / browser sign-ins off, "My files" = the user's
  folder); hub (`hub/`: SAS Logon Authorization Code + PKCE – own client with redirect or the `vscode` paste-the-code
  fallback –, one Studio process per user with the user's Viya token pre-loaded, proxy behind `/studio/`, cookies +
  CSRF, idle stop, max users, admin page, audit log, `/healthz`); `deploy/Dockerfile`; `deploy/k8s/` manifests;
  tests with a fake SAS Logon (`test_hub.py`, `test_hub_e2e.py`, `test_server_mode.py`).
- **Hardening (built 2026-10-30, branch `claude/hub-hardening`):** security review of hub + server mode, fixed with a
  test each: request smuggling (Transfer-Encoding / double Content-Length refused, GET bodies read), cross-site links
  can't pick the tool/args a Studio page runs on opening (Fetch Metadata), hub-only Studio routes (`hub/seed`) not
  proxied, sign-in rate limit per address (`STUDIO_LOGIN_RATE`), last `X-Forwarded-For` hop, `__Host-` sign-in
  cookie, 120 s client read timeout, TLS file paths of a connection only from the user's own folder. Robustness: a
  crashed or hung Studio (health ping every 30 s, call timeout `STUDIO_PROXY_TIMEOUT` + failed ping) is restarted at
  once and the page says "Your Studio is restarting" / reconnects; crash loop guard (3 in 5 min); idle stop waits for
  calls in flight; a crashed Studio doesn't hold a `max_users` place, admins may start one more; shutdown stops all
  Studios at once (≤10 s). Memory measured (hub ~36 MB flat, ~145 MB per Studio, ~210 MB per notebook kernel; K8s
  request 3Gi); admin page memory includes the process tree. `studio-hub-local.ps1` checks prerequisites (venv, also
  from a worktree; port free; Viya URL) and prints a checklist (URL, sign-in flow, logs, access, notebooks).
- **Access by SAS groups (built):** `STUDIO_USERS_GROUP=SpeedyStoreStudioUsers`, `STUDIO_NOTEBOOK_GROUP=
  SpeedyStoreStudioNotebooks` (Jonas creates them on Zeus on 2026-11-02). Checked at sign-in with the user's own token (`/identities/users/@currentUser/memberships`),
  re-checked by each Studio at every Viya token renewal (removal signs out within the token lifetime); identities
  down = new sign-ins refused, running sessions kept. Admin page shows access and which group gave it.
- **Notebooks `STUDIO_NOTEBOOKS=local` (built, test version):** kernels in the notebook user's own Studio process
  (shared read-only notebook environment, own folder, own tokens via the Studio's kernel token service), for the
  notebook group only. **No isolation between users' kernels: a few trusted users only** (Jonas + one colleague).
- **Next in step 1:** Jonas signs in against Zeus locally (`scripts/studio-hub-local.ps1 -Notebooks`, from 2026-11-02)
  and with a colleague; check on Zeus whether `/memberships` includes nested groups (until then members are added
  directly); first image build (no Docker here yet: check the notebook environment and `tini`); re-measure memory on
  Linux. **Deploy on Zeus the week of 2026-11-09** with Zeus's admin (away the week of
  11-02; he controls everything there: OAuth client, groups, Kubernetes, registry) – ADMIN-GUIDE as his checklist.
- **Step 2 – notebooks** (decided by Jonas 2026-10-30: show the full version from the start, harden later). No pod
  per user: Studio stays in the shared pod (it only runs our code); the **kernels** run behind a **Jupyter Enterprise
  Gateway**, and `notebook_kernel.py` gets a "remote" mode (HTTP/WebSocket, one access key per kernel, so a Studio only
  reaches its own kernels).
  - **2a – built 2026-10-31 (branch `claude/kernel-gateway`), `STUDIO_NOTEBOOKS=gateway`:** `notebook_gateway.py`
    (Studio side: `GatewayKernel` with the bridge's events over the Jupyter WebSocket protocol, stdlib WebSocket client,
    prelude with non-secret settings + relay token commands, installs / package list in a short-lived kernel),
    `hub/gateway.py` (kernel broker on 127.0.0.1 with a key per Studio and per kernel, WebSocket byte relay, limits,
    reaper, token relay on :8801), `kernel_gateway.py` (gateway pod entry: kernelspec + EG config, token out of the
    environment, non-dumpable PID 1), `kernel_launcher.py` (per kernel: clean env, `RLIMIT_AS`, nice, user folder,
    `pip --target`), Dockerfile `INSTALL_GATEWAY=1` (EG 3.3 in `/opt/gateway-env`, Python 3.11 - pyzmq < 25),
    `deploy/k8s/21`, `80-83` (Secret, PVC 50 Gi, Deployment, Services, NetworkPolicy per pip mode; hub policy scoped
    to the hub pod), admin page (gateway health, kernels, pip mode, warning), `studio-hub-local.ps1 -Gateway`,
    `tests/test_kernel_gateway.py` (local EG process, two users, isolation / relay / interrupt / restart / limits /
    culling / installs; skipped without `S2_TEST_EG_VENV`). ~130 MB per kernel after start (Windows). Open: first image
    build + Linux run (RLIMIT_AS value vs. real notebooks, interrupt via signals), Zeus admin's PyPI mirror answer,
    "My files" aren't visible to gateway kernels (separate volume), TLS CA files of a SingleStore connection (paths in
    the hub pod) aren't usable in gateway kernels, the quota isn't a filesystem quota.
  - **2a design (as decided):** the gateway starts kernels as processes in its own pod. Tokens come from a hub
    relay that knows each kernel's user (never in the kernel's environment); per-kernel memory cap (`RLIMIT_AS`), low
    CPU priority, max kernels per user and in total; one image environment + a per-user `pip install --target` folder.
    Package installs are a setting (`STUDIO_PIP_MODE`, decided 2026-10-30): `mirror` (default when
    `STUDIO_PIP_INDEX_URL` is set – an internal Artifactory / Nexus PyPI proxy; egress only to the mirror), `pypi`
    (alternative: straight to pypi.org + files.pythonhosted.org, needs that egress opening in the NetworkPolicy), `off`
    (only the image's packages). The deploy files ship a NetworkPolicy variant per mode; the admin page shows the mode.
    Egress otherwise only to SingleStore, Viya / SAS Logon and the relay. Notebooks only for a
    named group of colleagues. **No isolation between users' kernels** (same OS user and pod: one kernel can read
    another's files / key and act as that user; one runaway kernel can take the pod down) – say so in the docs and on
    the admin page.
  - **Test version (decided 2026-10-30):** next week's local hub and the first Zeus deploy keep notebooks on
    (`STUDIO_NOTEBOOKS=local`: kernels in the user's own Studio process, notebook group only) – used by Jonas and one
    colleague, no isolation between kernels; 2a replaces it before more users join. 2a is built in parallel.
  - **2b – one pod per kernel when needed** (customers / partners, audit that must prove who did what): switch the
    gateway to its Kubernetes process proxy (a deployment change, Studio unchanged) + NetworkPolicy, read-only root and
    limits on the kernel pods; the gateway (not the hub) then needs a ServiceAccount + Role to manage pods. Left to do
    (notes in deploy/README.md "Step 2b"): kernel image (notebook env + EG's `launch_ipykernel.py` + the launcher's env /
    pip / folder steps), kernelspec with `KubernetesProcessProxy` + pod template (resources instead of `RLIMIT_AS`,
    user folder as `subPath`), RBAC, kernel-pod NetworkPolicy (in: gateway on ZMQ ports + response port 8877), tests on
    a real cluster (kind / the Zeus namespace).
  - "Python in SAS" cells via `PROC PYTHON` in the user's own Compute session (isolated by Viya, nothing from the K8s
    admins) – **shipped 2026-10-30** ahead of 2a (see "Since 0.10.1"): the Notebook opens in server mode with Python in
    SAS and Text cells only. **Checked on Zeus 2026-10-30:** Python 3.12.12 (RHEL), pandas 2.3.3, numpy 2.5.4, swat 1.18.1,
    singlestoredb 1.18.1, pyarrow 26.0.0, scikit-learn 1.9.1; first call ~8 s (Python start), then ~0 s; state is kept
    between PROC PYTHON steps of the same SAS session ("Resuming Python state"). Can ship before 2a.
  - Who gets notebooks: the `STUDIO_NOTEBOOK_GROUP` SAS group (see the hub's group access). Groups decided
    2026-10-30: `SpeedyStoreStudioUsers` (app) and `SpeedyStoreStudioNotebooks` (notebooks); Jonas creates them on
    Zeus on 2026-11-02, so the local hub test can check real group membership.
  - Shared kernel volume: 50 GB in total, 5 GB quota per user (decided 2026-10-30; data belongs in SingleStore / Viya).
  - Question for Zeus's admin (deploy week): is there a PyPI mirror at SAS (Artifactory?) – until then `pypi`.
- **Step 2 – the rest:** Ask Claude stays **off** in the server version (decided 2026-10-30; later via an org key held by
  the hub – Foundry on SAS Azure or the Anthropic API – with a relay and per-user limits), shared metrics sampler
  instead of one per Studio, more than one hub replica (sessions out of memory), SingleStore per-user JWT logins, a separate origin for
  Studio pages (today they share the hub's origin: an XSS in a Studio app could act as that user on the hub), streaming
  large Studio responses instead of buffering them in the hub. 2a plugs in behind `notebook_kernel`'s server seam
  (`_shared_environment`, `_server_kernel_env`, `_kernel_cwd`, `kernel_tokens`) - done as `notebook_kernel._gateway()` /
  `_new_kernel` / `_gateway_prelude` and `kernel_tokens.RELAY_CLIENT`.

## Known small issues
- Network breaks: a query whose connection goes silent *without* a route change or sleep (e.g. a firewall that drops
  packets mid-query) still waits for the TCP keepalive (~25 s) before it's retried; a SAS job POST on a kept-alive
  connection that went silent the same way waits for its HTTP timeout (60 s) – it's not re-sent. Entra (token) logins
  to SingleStore: a silent token renewal while offline reports "sign in again" until the network is back.
  SAS cells in notebooks (saspy) have their own sessions and aren't covered.
- Notebook on Windows: calls waiting in C (socket read, lock, long C loop) still can't be interrupted – Restart
  kernel is offered after 5 s.
- A Compute session can get stuck after some SAS errors (seen once in a test after a failed CAS `promote=`
  DATA step: later PROC SQL steps "stopped because of errors" without an ERROR line); Restart SAS session fixes it.
