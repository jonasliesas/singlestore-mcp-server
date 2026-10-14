# Roadmap

Agreed with the owner; confirm the order before starting a new release.
Last updated 2026-10-14 (0.9.0 released).

## 0.9.0 (released 2026-10-14)
SQL Editor tabs, autosave, history and SELECT builder; Schema Explorer bottomless info and Analyze;
Cluster Monitor Queries/Disk/Bottomless/IO tabs; Query History usage, grouping, plan tree, SAS EP card
(jobs and VA/CAS); Security and Config views (read-only); Workload Claude chat and SQL Editor ↔ Workload
hand-off; pytest + Playwright test suite.

## Next small items
- ~~Run the offline test subset in GitHub Actions on every push~~ – added after 0.9.0 (`.github/workflows/tests.yml`, Windows + Linux).
- ~~Move the SingleStore password out of `~/.claude.json` env into the OS credential store~~ – done after
  0.9.0: `python -m singlestore_mcp.move_password`; tests use the saved connection.
- ~~Workload runs end a little before the set duration~~ – fixed 2026-10-14: a run lasts start + duration
  (or until Stop), rates are over that window; `compare_runs` puts old saved runs (no `"timing": 2`) over
  their set duration too. Offline tests with a fake clock and connection (`TestRunTiming`).
- ~~Supervisor restarts in a tight loop when the server code has a syntax/import error~~ – fixed 2026-10-14:
  back-off 1/2/5/10/20/30 s (reset after 60 s of running), the error's traceback tail in the log and in an
  immediate tool error, `restart_server` retries at once and reports the new error (`tests/test_supervisor.py`).
- ~~SAS Viya sign-in "lost" after signing in in Connections~~ – fixed 2026-10-14 (uncommitted, for Jonas to
  test). Cause: the SAS Viya form rendered (empty, usable) before `sas_viya_state` answered – it can take
  seconds while it renews / imports a token over the network, and the app's tool calls queue behind it –
  and **Sign in saved the form first**, so a click in that window saved an empty address and startup code;
  saving another address deleted the token. Fix: no implicit save on Sign in, the list only renders once
  loaded (and its state reads no network), an empty address never blanks a saved one, a token is only
  dropped when the address changes to another non-empty one, `sas.json` is read/written with `fileio` (an
  unreadable file raises instead of reading as "no Viya"). Tests: `tests/test_sas_viya.py`,
  `tests/ui/test_connections_ui.py`.
- ~~Several SAS Viya connections, one active (Jonas: same setup as SingleStore)~~ – done 2026-10-14
  (uncommitted): `sas_viya.py` keeps a list in `sas.json` (old file migrated, its token kept; empty address →
  empty list), token per connection (`sas-viya:<name>#token`), Connections → SAS Viya connections with
  + New / Edit / Delete / Use / Sign in / Sign out / Test; Use resets the Workspace's SAS sessions; new
  kernels use the active one (token command names the connection); the Schema Explorer's SAS / CAS sources
  and the SQL Editor (⚙, and next to the library picker with ≥ 2 connections) show and switch it.
  Ideas: a Test for the form before saving; per-tab Viya connection in the SQL Editor.

## SAS in the Workspace (next big feature – agreed design)
**Status (2026-10-14, uncommitted, for Jonas to test):** built per the design below – `sas_rest.py` (Viya REST
client: one kept-alive Compute session with startup code, Compute data API, one CAS session), `sas_sql.py`
(wrapping, write checks, sastrace / FedSQL plan parsing, verdicts), `apps/sas_workspace.py` (app tools),
Schema Explorer sources SingleStore / SAS libraries / CAS, SQL Editor "Run as" with Result / Log / Pushdown
tabs, ⚙ PROC SQL options, Ctrl+Shift+L, "looks wrong" check, badges in editor / results / history, .sas
files, Connections → SAS Viya: CAS server + startup code. Tests: `tests/test_sas_sql.py` (offline),
`tests/test_sas_workspace.py` (`sas`), `tests/ui/test_sas_ui.py` (`ui` + `sas`).
What's left / ideas:
- The SQL Editor's own tree follows the tab's language (it doesn't switch the language); the Schema
  Explorer's **Query in SQL Editor** does set it. Revisit if a click in the tree should switch it too.
- FedSQL (Compute) pushdown: the verdict comes from the `_method` plan; the SQL text FedSQL sends isn't in
  the log, so no "Find in Query History" there. A trace option for that is still to find.
- "Find in Query History": the History view's list only has queries ≥ 1 s, so short pushed-down queries
  are found in the plan cache (shown in a toast) but may not be listed in the view.
- No Stop for a long SAS run yet (jobs time out after 15 min); one SAS session serves all tabs, one run at
  a time.
- Autocomplete has columns only for tables expanded in the tree (one call per table, kept cheap on purpose).
- An "S2" libname from the active SingleStore connection (as notebooks get) isn't assigned in the
  Workspace session; use the startup code.
- Agreed design (kept for reference):
- **Schema Explorer** gets three sources: SingleStore, SAS libnames (Viya Compute), SAS caslibs (CAS).
  Libnames = what the compute context assigns + optional startup code run when the session starts
  (e.g. `%include` of the libname files), configured in Connections. SAS/ACCESS libnames
  (engine SSTORE) show their SingleStore database and link to it.
- **SQL Editor language choice:** SingleStore SQL / PROC SQL (libnames) / FedSQL (libnames) /
  FedSQL in CAS (caslibs), with result grid, SAS log tab and autocomplete from SAS metadata.
  - The user writes only the SQL; the editor wraps it (`proc sql; … quit;`, `proc fedsql; … quit;`,
    CAS `fedSql.execDirect`). Same statement splitting / Ctrl+Enter / Run all as SingleStore SQL.
    The generated SAS code is shown in the log tab with Copy.
  - Text starting with `proc` / `data` / `%` runs as plain SAS code, unwrapped.
  - PROC SQL options (outobs, noexec, feedback) via a small settings menu.
  - "Run as" selector next to the database picker (which then lists databases / librefs / caslibs);
    colour badges (S2 blue, PROC SQL / FedSQL orange, CAS purple, SAS for unwrapped code) in editor,
    log and history. Set automatically from the schema tree and by file type (.sas → PROC SQL,
    .sql → SingleStore), saved with draft/file and history; never guessed from the SQL text, but warn
    when it looks wrong (e.g. `s2.cars` while SingleStore is selected). Ctrl+Shift+L cycles.
- **Writes** allowed with confirmation, except no confirmation for the SAS WORK library; shared caslibs
  get an extra warning.
- **Show pushdown:** sastrace / sql_ip_trace; show the SQL sent to SingleStore and mark clearly whether
  it was pushed down fully, partly, or pulled into SAS.
- Reuse `sas_viya.py` (OAuth token, sas.json, compute context); the server needs its own Viya REST
  client (Compute + CAS) – today SAS code only runs via saspy in the notebook kernel.
- SAS 9 libnames via SAS/CONNECT: a later round.

## Later (0.10.0 candidates)
- **Audit log** – researched 2026-10-14 (README, Security → Audit log): the log is CSV files per node in
  `auditlogsdir`, no SQL view; this cluster has `auditlog_level = OFF` and runs on the Kubernetes Operator
  (FS pipelines unsupported, the app user has no FILE READ), so it can't be read from the server without
  infrastructure changes. Done: a read-only **Audit log** tab with the settings and the admin steps.
  Design for a real Audit view, once an admin decides: (1) set `auditlog_level = ADMIN-ONLY` (or
  `LOGINS-ONLY`) and `auditlog_retention_period` in the MemsqlCluster `spec.globalVariables` (rolling
  restart); (2) ship each pod's `auditlogsdir` with a log collector (Fluent Bit) into a table, e.g.
  `audit.auditlog(entry_id, ts, node, node_type, thread_id, user, db, correlation_id, query_hash, query,
  status, kind)`; (3) the Audit tab reads that table: filter by user, time, statement type (LOGIN / DDL /
  GRANT / DML), with the completion records (`C`) joined for success/failure. Shared infrastructure:
  give the admin the settings, don't change them.
- ~~**Security view v2:** GUI for changes (GRANT/REVOKE, roles, groups, resource pools) with preview and
  explicit confirmation~~ – built 2026-10-14 (`security_admin.py`, tools `security_admin_capabilities`,
  `security_change_plan`, `security_change_apply`; tests `tests/test_security_admin.py`,
  `tests/ui/test_security_ui.py`), to be tested by the owner. Not yet: SET PASSWORD / ALTER USER password,
  account lock/unlock and lockout settings, column-level grants, `REQUIRE SSL`, model-visible change tools
  (deliberately app-only so passwords never go through the chat).
- ~~Cluster health panel (`MV_EVENTS`, partition/replication status)~~ – done 2026-10-14: Cluster Monitor
  **Health** tab (`cluster_monitor_health`, `apps/cluster_ops.py`).
- ~~Persisted metrics history in SQLite (CPU/memory/disk/queue over 24 h–7 d)~~ – done 2026-10-14:
  `metrics_store.py` (sampler, `metrics.db`, 24 h raw + 10-min rows, 7 days) and the **History** tab.
  Later: memory/disk alerts on trends (e.g. "disk full in N days"), CSV export of the history.
- ~~Per-table skew in Schema Explorer~~ – built 2026-10-14 (uncommitted, for Jonas to test): a table's
  **Distribution** tab and **Check skew** on a database (`schema_explorer_skew`; TABLE_STATISTICS master
  partitions + COLUMNAR_SEGMENTS, on demand only): skew %, largest partitions, per leaf, shard key and advice
  with docs links. Tests: `tests/test_skew.py`, `tests/test_streaming_skew.py`, UI `test_schema_explorer_distribution`.
  Idea: a "reshard simulation" for a candidate key (the docs' `PARTITION_ID()` query) – it scans the table,
  so only on an explicit click.
  ~~Backup overview~~ – done 2026-10-14: **Backups** tab
  (`MV_BACKUP_HISTORY` / `MV_BACKUP_STATUS`, read-only). The demo cluster has no backups in its history
  (and its license expired 2024-11-14; the Health tab shows it).
- Multi-user Workspace on a server with login – **on hold** until Jonas has a decision that the app will be used
  at that scale. A design proposal exists locally (`docs/MULTI_USER_DESIGN.md`, not committed).
- ~~Performance: `cluster_monitor_data` ~1.3 s, ~1.1 MB pages~~ – done 2026-10-14 (`perf/startup.py`):
  `cluster_monitor_data` reuses an earlier counter sample (~0.16 s; only the first call in 5 min still
  waits 1 s for a CPU baseline); view pages come without the shared head (13–120 KB instead of ~0.45 MB;
  SQL Editor / Notebook ~0.65 MB, the CodeMirror bundle); DB connections open while the server loads;
  `restart_server` hands over to a pre-loaded standby (restart → Workspace usable ~2.3 s, was ~7 s).
- Startup, still open: the first `s2.connect` in a process compiles singlestoredb's Fusion SQL grammars
  (~0.9 s CPU, even with Fusion off – upstream issue to raise); CodeMirror (0.5 MB) is still sent once for the
  SQL Editor and once for the Notebook; the Cluster view's first open waits 1 s for its CPU baseline.
- Raise the fixed ~5.2 s wait with the SAS EP owners – analysed after 0.9.0 (README, SAS EP): a +5.0 s step
  before the EP sends its first SQL, no CPU or queueing in SingleStore, concurrent calls share one wait, and most
  EP calls are fast and untraced (the "77 %" is of traced calls only). Questions for them are in the README;
  hypotheses (DNS timeout / EP cold start / serialized step) still to verify with their EP logs.
- ~~SAS EP card: time breakdown, plateau / shared-wait / small-calls flags, failed generated statement, sourced
  tips; EP overview in the Usage tab~~ – done after 0.9.0 (`sas_ep.py`, `tests/test_sas_ep.py`).
- SAS EP follow-ups: once the trace threshold is lowered for a while, re-check the warm/cold split with the fast
  calls; the `_hello_world` health check fails daily because its output table exists (tell its owner).

## Known small issues
- ~~`LIMIT 100000` in the SQL Editor fetches all rows before showing any~~ – fixed 2026-10-14 (uncommitted):
  reads stream (`result_stream.py`, `db.stream`): first 1,000 rows at once, the rest loads on the server (cap
  100,000 rows / ~128 MB per result, 384 MB in all), pages on scroll, total when done, Stop = KILL QUERY;
  Export CSV / sort load all rows first. `query_grid` stops reading at max_rows (it read every row before).
  Not streamed yet: writes (`sql_editor_execute`) and SAS runs (1,000 rows as before).
- Long single Python calls in a notebook can't be interrupted on Windows.
- SAS notebook tests passed without the SAS secrets opt-in (unexplained).
- ~~Two UI tests are flaky (about 1 run in 3): `test_sql_editor_tabs` and `test_sql_editor_and_workload_round_trip`~~
  – cause found 2026-10-14: on Windows `os.replace` fails (PermissionError) while the target or the new temp
  file is open – a concurrent read of the same JSON (the tests poll it while the app saves), the other server
  process, or Defender scanning (measured: ~1 in 20 replaces in %TEMP% with nobody else, almost all of them
  with a reader polling) – and a read during a replace could fail and be taken for "file missing": for the SQL
  Editor's tabs.json that started a fresh tab list (all tabs lost). Fixed for the SQL Editor state
  (`fileio.py`: retried replace and read, unreadable ≠ missing, in-process reads under the writer's lock);
  the workload test now waits for the app's Save to finish before it reads the store.
  The same fix now covers workloads, saved connections, alerts and notebook saves (an unreadable
  workloads.json / connections.json used to read as empty, so the next save could drop every workload or
  connection).
- Under the load of the full suite, `tests/ui/test_sas_ep_ui.py` and `tests/ui/test_sas_ui.py::test_sql_editor_run_as_proc_sql`
  failed once (2026-10-14); both pass when run on their own – probably waits too short for a cold SAS session.
