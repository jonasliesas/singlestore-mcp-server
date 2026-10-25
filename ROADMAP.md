# Roadmap

Agreed with the owner; confirm scope and order before starting a round. Details of what was built are in the
README and the git log; this file keeps what's next.
Last updated 2026-10-26 (latest release 0.10.1).

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

## Backlog
**Security**
- Ideas: `REQUIRE X509 / SUBJECT` (mutual TLS); `PASSWORD EXPIRE` / `ACTIVATE`; "Drop view" for column views;
  re-apply locks after a cluster restart (restarts clear them).
- Audit view – needs an admin: `auditlog_level` (e.g. ADMIN-ONLY) in the Operator's `spec.globalVariables`, the
  node log files shipped (Fluent Bit) into a table; then an Audit tab with filters. Today: guidance tab only.

**SAS in the Workspace**
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

**Workspace / performance**
- Start-up, what's left (2026-10-26): a cold server is ~3 s of imports before the handshake, ~2 s of it the MCP SDK
  (its `__init__` loads the client, HTTP/auth and both protocol type sets) – an upstream issue, or a handshake the relay
  answers from a cache keyed by the code's file stamps (sends `list_changed` if the server's lists differ); each
  connection runs three `SET`s one by one (`SET NAMES`, extended types, vector format: ~0.4 s of its ~1.2 s) – a
  driver option or upstream change to send them as one; a sas_rest route learned "direct" (proxy skipped) is
  per process: a new server behind an `HTTPS_PROXY` that can't reach Viya waits for the proxy first again.
- singlestoredb compiles its Fusion SQL grammars on every start (~0.5 s of the connection import; no option skips
  it, also not on upstream `main`) – issue text ready for Jonas to file: `docs/upstream-singlestoredb-fusion.md`.
- Reshard simulation: a "Copy to a new table with this shard key" script (CREATE TABLE … SHARD KEY + INSERT …
  SELECT + swap) for a DBA, like the Security basket.

**SAS EP (with the SAS EP owners)**
- The fixed ~5 s wait before the EP's first SQL (no CPU or queueing in SingleStore; concurrent calls share one
  wait). Questions and hypotheses (DNS timeout / EP cold start / serialized step) are in the README.
- Re-check warm/cold split after lowering the trace threshold for a while; the daily `_hello_world` check fails
  because its output table exists (tell its owner).

## On hold
- **Multi-user Workspace** on a server with login – until Jonas decides the app will be used at that scale.
  Design proposal locally in `docs/MULTI_USER_DESIGN.md` (not committed).

## Known small issues
- Notebook on Windows: calls waiting in C (socket read, lock, long C loop) still can't be interrupted – Restart
  kernel is offered after 5 s.
- Other views rebuild their whole page on refresh like the Cluster Monitor did; if a click is lost there too, use
  the same "hold renders while a button is pressed" guard (`cluster_monitor.html`, `press`).
- A Compute session can get stuck after some SAS errors (seen once in a test after a failed CAS `promote=`
  DATA step: later PROC SQL steps "stopped because of errors" without an ERROR line); Restart SAS session fixes it.
