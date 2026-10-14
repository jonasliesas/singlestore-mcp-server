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
- Move the SingleStore password out of `~/.claude.json` env into the app's connection store
  (keyring) or Entra ID login.

## SAS in the Workspace (next big feature – agreed design)
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
- **Audit log** – the cluster writes `AUDITLOGS` to node disks (files, not an information_schema view);
  research how to read it before designing a view.
- **Security view v2:** GUI for changes (GRANT/REVOKE, roles, groups, resource pools) with preview and
  explicit confirmation. v1 is read-only by agreement; many customers want the GUI.
- Cluster health panel (`MV_EVENTS`, partition/replication status).
- Persisted metrics history in SQLite (CPU/memory/disk/queue over 24 h–7 d).
- Per-table skew in Schema Explorer; backup overview.
- Multi-user Workspace on a server with login.
- Performance: `cluster_monitor_data` ~1.3 s, ~1.1 MB pages.
- Raise the fixed ~5.2 s wait seen in ~77 % of SAS EP calls with the SAS EP owners.

## Known small issues
- Workload runs end a little before the set duration (after the last scheduled statement).
- `LIMIT 100000` in the SQL Editor fetches all rows before showing any.
- Long single Python calls in a notebook can't be interrupted on Windows.
- SAS notebook tests passed without the SAS secrets opt-in (unexplained).
