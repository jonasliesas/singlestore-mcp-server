# Roadmap

Agreed with the owner; confirm scope and order before starting a round. Details of what was built are in the
README and the git log; this file keeps what's next.
Last updated 2026-10-16 (latest release 0.10.1; one commit since).

## Released
- **0.9.0** (2026-10-14) – Workload, SQL Editor tabs/history/SELECT builder, Security and Config views (read-only),
  Cluster Monitor and Query History extensions, pytest + Playwright suite.
- **0.10.0** (2026-10-14) – SAS in the Workspace (Viya REST client, SAS/CAS sources, Run as PROC SQL / FedSQL /
  CAS, pushdown verdict), several Viya connections, Security changes from the GUI, Cluster Monitor Health /
  History / Backups, SAS EP tuning, table skew, streaming results, ~3× faster start-up, crash back-off, safe
  state files on Windows, password moved out of Claude's config, GitHub Actions.
- **0.10.1** (2026-10-14) – SAS autocomplete fetches columns itself (case-insensitive), SAS session no longer
  restarted on a missing table, Viya connection retries, SAS tests opt-in (`--sas`).

## Since 0.10.1 (committed, not released – collecting for 0.11.0)
- Security: **SQL script instead of Run** – Copy / Open in SQL Editor / Save as .sql, script basket, password
  placeholder, works without change privileges.
- SQL Editor: **open and save files on SAS Viya** – SAS Content and SAS Server (Compute file system), version
  check before overwriting, tabs remember their Viya file.
- Open / Save as as a **large dialog**; typing in its filter no longer jumps to the file name.

## Next round (proposed 2026-10-16 – waiting for Jonas's go)
1. **SS / S2 badges for CAS caslibs** – `SingleStore` type (SpeedyStore: live pushdown, no in-memory copy) →
   **SS**, purple; `singlestore_standard` (standard data connector, loads into CAS memory) → **S2**, grey-blue
   outline; tooltip with the SingleStore database. SAS/ACCESS libnames in Compute keep S2.
2. **Automatic S2 libname** from the active SingleStore connection in the Workspace's SAS session (as notebooks
   get): assigned at session start, reassigned when the active connection changes, startup code runs after it.
   Password from the credential store, sent with `nosource` (never in log/history); alternative `authdomain=`
   (needs a Viya admin). Entra/JWT: refresh before the token expires. Engine/TLS options as on Zeus.
3. **"Show code" for a library or caslib** – the `LIBNAME` / `caslib … datasource=(…)` statement (and
   `table.addCaslib`) rebuilt from the session's metadata, passwords/tokens as placeholders; Copy / Open in SQL
   Editor (plain SAS, not run). Marked "reconstructed from the current settings".

## Backlog
**Security**
- Password change (`SET PASSWORD` / `ALTER USER … IDENTIFIED BY`) – masked field, placeholder in scripts.
- Account lock / unlock, and unlock after a failed-login lockout – check which forms 9.0 supports.
- Column-level grants – check 9.0 support; else a view with the allowed columns + grant.
- `REQUIRE SSL` / `REQUIRE NONE` per user – check 9.0 syntax.
- Audit view – needs an admin: `auditlog_level` (e.g. ADMIN-ONLY) in the Operator's `spec.globalVariables`, the
  node log files shipped (Fluent Bit) into a table; then an Audit tab with filters. Today: guidance tab only.

**SAS in the Workspace**
- Stop button for long SAS runs (now cut off after 15 min); one SAS session serves all tabs, one run at a time.
- FedSQL (Compute) pushdown shows only the verdict – find a trace that gives the SQL text.
- "Find in Query History" for short pushed-down queries (the History list only has queries ≥ 1 s).
- SAS 9 libnames via SAS/CONNECT.
- Ideas: SQL Editor tree click switches the language; per-tab Viya connection; delete/rename in the Viya file
  browser; save from the Notebook / the Security basket straight to SAS Content.

**Workspace / performance**
- Notebook Open / Save as: same large dialog as the SQL Editor.
- CodeMirror (0.5 MB) is sent twice (SQL Editor and Notebook).
- Cluster view's first open waits 1 s for a CPU baseline.
- singlestoredb compiles its Fusion SQL grammars on every start (~0.9 s CPU) – raise upstream.
- Metrics history: trend alerts (e.g. "disk full in N days"), CSV export.
- Skew: "reshard simulation" for a candidate shard key (scans the table – explicit click only).

**SAS EP (with the SAS EP owners)**
- The fixed ~5 s wait before the EP's first SQL (no CPU or queueing in SingleStore; concurrent calls share one
  wait). Questions and hypotheses (DNS timeout / EP cold start / serialized step) are in the README.
- Re-check warm/cold split after lowering the trace threshold for a while; the daily `_hello_world` check fails
  because its output table exists (tell its owner).

## On hold
- **Multi-user Workspace** on a server with login – until Jonas decides the app will be used at that scale.
  Design proposal locally in `docs/MULTI_USER_DESIGN.md` (not committed).

## Known small issues
- Long single Python calls in a notebook can't be interrupted on Windows.
- SAS notebook tests passed without the SAS secrets opt-in (unexplained).
- `tests/ui/test_cluster_monitor_ui.py` failed once (no `.cm-node` within 10 s after going back to Overview).
- Mixing `tests/` and `tests/ui/` files in one pytest call fails with "fixture 'ui' not found" – run them separately.
- Writes (`sql_editor_execute`) and SAS runs aren't streamed (1,000 rows).
