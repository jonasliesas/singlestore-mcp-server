"""Kernel bridge for the Notebook app. Runs in the notebook Python environment.

Started by ``notebook_kernel.py`` with the notebook environment's Python (which
has ipykernel, jupyter_client, pandas, matplotlib and singlestoredb), so the
MCP server itself needs none of those. It starts one IPython kernel and speaks
JSON lines over stdin/stdout:

  in:  {"id": "r1", "op": "execute", "code": "..."}
       {"op": "interrupt"} | {"op": "restart"} | {"op": "shutdown"}
  out: {"event": "ready"}
       {"id": "r1", "event": "output", "output": {...nbformat output...}}
       {"id": "r1", "event": "done", "status": "ok"|"error"|"aborted", "execution_count": 3}
       {"event": "restarted"} | {"event": "error", "message": "..."}

Kept free of singlestore_mcp imports: it runs from its file path in another
environment.
"""

from __future__ import annotations

import json
import queue
import sys
import threading

# Runs once in every new kernel: inline charts, pandas, a SingleStore
# connection `conn` (same SINGLESTORE_* settings as the MCP server), and the
# helpers the app uses to run %%sql cells and show DataFrames as grids.
STARTUP = r'''
%matplotlib inline
import warnings as _s2_warnings
# tqdm (used by transformers, sentence-transformers, …) asks for ipywidgets in
# Jupyter; this app shows its plain-text progress bars instead, so the hint is noise.
_s2_warnings.filterwarnings("ignore", message="IProgress not found")


# pandarallel doesn't work on native Windows (its docs: WSL only). Example
# notebooks call pandarallel.initialize() and .parallel_apply(); here those run
# as plain pandas .apply / .map so the notebooks work unchanged.
import os as _s2_os
if _s2_os.name == "nt":
    import importlib.abc as _s2_abc, importlib.machinery as _s2_machinery, sys as _s2_sys

    def _s2_patch_pandarallel(mod):
        def initialize(*args, **kwargs):
            import pandas as _pd
            for cls, pairs in ((_pd.Series, (("parallel_apply", "apply"), ("parallel_map", "map"))),
                               (_pd.DataFrame, (("parallel_apply", "apply"), ("parallel_applymap", "map")))):
                for par, plain in pairs:
                    setattr(cls, par, getattr(cls, plain))
            for grp in (_pd.core.groupby.DataFrameGroupBy, _pd.core.groupby.SeriesGroupBy):
                grp.parallel_apply = grp.apply
            print("Note: pandarallel doesn't run on native Windows; parallel_apply runs as plain pandas apply here.")
        mod.initialize = initialize

    class _S2PandarallelHook(_s2_abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name != "pandarallel":
                return None
            spec = _s2_machinery.PathFinder.find_spec(name, path)
            if spec and spec.loader:
                exec_module = spec.loader.exec_module
                def patched(module):
                    exec_module(module)
                    _s2_patch_pandarallel(module.pandarallel if hasattr(module, "pandarallel") else module)
                spec.loader.exec_module = patched
            return spec

    _s2_sys.meta_path.insert(0, _S2PandarallelHook())
import json as _s2_json, os as _s2_os
import pandas as pd
import singlestoredb as _s2

_S2_TABLE_MIME = "application/vnd.s2.table+json"
_S2_GRID_ROWS = 1000
_S2_FETCH_LIMIT = int(_s2_os.environ.get("SINGLESTORE_MCP_NOTEBOOK_FETCH_LIMIT", "100000"))


def _s2_connect():
    url = _s2_os.environ.get("SINGLESTORE_URL")
    if url:
        return _s2.connect(url, autocommit=True)
    kw = dict(
        host=_s2_os.environ["SINGLESTORE_HOST"],
        port=int(_s2_os.environ.get("SINGLESTORE_PORT", "3306")),
        user=_s2_os.environ.get("SINGLESTORE_USER", "root"),
        password=_s2_password(),
        autocommit=True,
        ssl_disabled=_s2_os.environ.get("SINGLESTORE_SSL_DISABLED", "").lower() in ("1", "true", "yes"),
    )
    if _s2_os.environ.get("SINGLESTORE_DATABASE"):
        kw["database"] = _s2_os.environ["SINGLESTORE_DATABASE"]
    kw.update(_s2_tls())
    return _s2.connect(**kw)


def _s2_token_left(token):
    """Seconds until a JWT expires (very large for passwords and tokens without exp)."""
    import base64, time
    try:
        part = token.split(".")[1]
        claims = _s2_json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return float(claims.get("exp", 1e18)) - time.time()
    except Exception:
        return 1e18


def _s2_password():
    """The password; for token logins (JWT / SSO / Entra ID) a fresh token, renewed through the MCP
    server's Python (SINGLESTORE_MCP_TOKEN_CMD) when the one this kernel got is about to expire."""
    pw = _s2_os.environ.get("SINGLESTORE_PASSWORD", "")
    cmd = _s2_os.environ.get("SINGLESTORE_MCP_TOKEN_CMD")
    if cmd and _s2_token_left(pw) < 300:
        import subprocess, sys
        try:
            out = subprocess.run(_s2_json.loads(cmd), capture_output=True, text=True, timeout=60,
                                 creationflags=0x08000000 if sys.platform == "win32" else 0)
            if out.returncode == 0 and out.stdout.strip():
                pw = _s2_os.environ["SINGLESTORE_PASSWORD"] = out.stdout.strip()
        except Exception:
            pass
    return pw


def _s2_tls():
    """TLS certificate settings of the active connection (SINGLESTORE_SSL_*)."""
    env, kw = _s2_os.environ, {}
    if env.get("SINGLESTORE_SSL_DISABLED", "").lower() in ("1", "true", "yes"):
        return kw
    if env.get("SINGLESTORE_SSL_CA"):
        kw["ssl_ca"] = env["SINGLESTORE_SSL_CA"]
        kw["ssl_verify_cert"] = env.get("SINGLESTORE_SSL_VERIFY", "1") not in ("0", "false", "no")
    if env.get("SINGLESTORE_SSL_CERT") and env.get("SINGLESTORE_SSL_KEY"):
        kw["ssl_cert"], kw["ssl_key"] = env["SINGLESTORE_SSL_CERT"], env["SINGLESTORE_SSL_KEY"]
    # SINGLESTORE_CREDENTIAL_TYPE=jwt needs nothing here: the token is the
    # password, sent with mysql_clear_password over TLS.
    return kw


class _S2LazyConnection:
    """`conn`: connects on first use, reconnects if the connection was dropped."""
    _c = None

    def _get(self):
        if self._c is None:
            self._c = _s2_connect()
        return self._c

    def __getattr__(self, name):
        return getattr(self._get(), name)

    def reset(self):
        try:
            self._c and self._c.close()
        except Exception:
            pass
        self._c = None


conn = _S2LazyConnection()


def _s2_cell(v):
    """Grid text for values JSON can't carry: binary (BLOB, packed vectors) and arrays."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "0x%s%s (%d bytes)" % (b[:16].hex().upper(), "…" if len(b) > 16 else "", len(b))
    if hasattr(v, "tolist") and not isinstance(v, (str, int, float)):
        items = v.tolist()
        if isinstance(items, list):
            head = ", ".join("%.4g" % x if isinstance(x, float) else str(x) for x in items[:6])
            return "[%s%s] (%d values)" % (head, ", …" if len(items) > 6 else "", len(items))
    return v


def _s2_table(df, max_rows=_S2_GRID_ROWS):
    shown = df.head(max_rows).copy()
    for col in shown.columns:
        if shown[col].dtype == object:
            shown[col] = shown[col].map(_s2_cell)
    data = _s2_json.loads(shown.to_json(orient="split", date_format="iso", default_handler=str))
    return {
        "columns": [str(c) for c in data["columns"]],
        "rows": data["data"],
        "row_count": int(len(df)),
        "truncated": len(df) > max_rows,
    }


def _s2_df_bundle(df, include=None, exclude=None):
    return {_S2_TABLE_MIME: _s2_table(df)}


get_ipython().display_formatter.mimebundle_formatter.for_type(pd.DataFrame, _s2_df_bundle)


_s2_database = None        # the notebook's database (set by the app before each cell)
connection_url = None      # as in SingleStore Notebooks: follows the selected database


def _s2_url(database=None):
    from urllib.parse import quote
    url = _s2_os.environ.get("SINGLESTORE_URL")
    if url:
        url = url if "://" in url else "singlestoredb://" + url
        return url if not database else url.rsplit("/", 1)[0] + "/" + database if url.count("/") > 2 else url + "/" + database
    host = _s2_os.environ.get("SINGLESTORE_HOST", "localhost")
    port = _s2_os.environ.get("SINGLESTORE_PORT", "3306")
    user = quote(_s2_os.environ.get("SINGLESTORE_USER", "root"), safe="")
    pw = quote(_s2_password(), safe="")
    from urllib.parse import urlencode
    tls = _s2_tls()
    query = "?" + urlencode({k: str(v) for k, v in tls.items()}) if tls else ""
    return "singlestoredb://%s:%s@%s:%s%s%s" % (user, pw, host, port, "/" + database if database else "", query)


def _s2_set_database(database):
    """Called by the app before each cell: the notebook's database for %sql,
    `connection_url`, and SINGLESTOREDB_URL (so s2.connect() / s2.create_engine() use it)."""
    global _s2_database, connection_url
    _s2_database = database or _s2_os.environ.get("SINGLESTORE_DATABASE") or None
    connection_url = _s2_url(_s2_database)
    _s2_os.environ["SINGLESTOREDB_URL"] = connection_url


_s2_set_database(None)
_s2_current_db = [None]    # what conn is USE-ing now


__S2_SPLIT__

def _s2_render(sql, ns):
    """{{ expr }} -> str(value of expr) from the notebook's variables (as jupysql/SingleStore Notebooks)."""
    import re as _re
    return _re.sub(r"\{\{\s*(.+?)\s*\}\}", lambda m: str(eval(m.group(1), ns)), sql)


class _S2ResultSet(list):
    """Rows of a %sql query (tuples): index it like rs[0][1], or rs.DataFrame()."""

    def __init__(self, rows, columns):
        # Rows are tuples that also carry the column names (`_fields`, like
        # SQLAlchemy / jupysql rows), so pd.DataFrame(result) keeps the names.
        row = type("Row", (tuple,), {"_fields": tuple(columns), "__slots__": ()})
        super().__init__(row(r) for r in rows)
        self.keys = self.columns = list(columns)

    def DataFrame(self):
        return pd.DataFrame(list(self), columns=self.columns)

    dataframe = DataFrame

    def _repr_mimebundle_(self, include=None, exclude=None):
        return {_S2_TABLE_MIME: _s2_table(self.DataFrame()), "text/plain": "%d rows" % len(self)}

    def __repr__(self):
        return "<%d rows: %s>" % (len(self), ", ".join(self.columns))


class _S2Affected:
    def __init__(self, n):
        self.rowcount = n

    def __bool__(self):
        return False

    def __repr__(self):
        return "%s row(s) affected" % self.rowcount


def _s2_execute(sql, database=None, ns=None):
    """Run one or more statements on `conn`; returns the last statement's result."""
    ns = ns if ns is not None else globals()
    sql = _s2_render(sql, ns)
    db = database or ns.get("_s2_database")
    result = None
    for attempt in (0, 1):
        try:
            cur = conn.cursor()
            break
        except Exception:
            conn.reset()
            _s2_current_db[0] = None
    try:
        if db and _s2_current_db[0] != db:
            cur.execute("USE `" + db.replace("`", "``") + "`")
            _s2_current_db[0] = db
        cur.execute("SET SESSION sql_select_limit = %d" % _S2_FETCH_LIMIT)
        for stmt in _s2_split(sql):
            cur.execute(stmt)
            if stmt.split(None, 1)[0].upper() == "USE":
                _s2_current_db[0] = stmt.split(None, 1)[1].strip().strip("`")
            if cur.description is None:
                result = _S2Affected(cur.rowcount)
            else:
                result = _S2ResultSet(cur.fetchall(), [d[0] for d in cur.description])
    finally:
        cur.close()
    return result


def _s2_sql(sql, database=None, name=None):
    """Run an SQL cell: the last result becomes `df` (and `name` if given) and shows as a grid."""
    from IPython.display import display
    result = _s2_execute(sql, database)
    if not isinstance(result, _S2ResultSet):
        display({"text/plain": repr(result) if result is not None else "Done."}, raw=True)
        return
    frame = result.DataFrame()
    globals()["df"] = frame
    if name:
        globals()[name] = frame
    table = _s2_table(frame)
    table["variable"] = name or "df"
    table["fetch_limited"] = len(frame) >= _S2_FETCH_LIMIT
    display({_S2_TABLE_MIME: table, "text/plain": "%d rows -> %s" % (len(frame), name or "df")}, raw=True)


def _s2_magic_target(text):
    """'name << SELECT …' -> ('name', 'SELECT …')."""
    import re as _re
    m = _re.match(r"\s*([A-Za-z_]\w*)\s*<<\s*(.*)\Z", text, _re.S)
    return (m.group(1), m.group(2)) if m else (None, text)


from IPython.core.magic import no_var_expand as _s2_no_var_expand
from IPython.core.magic import register_line_cell_magic as _s2_register


@_s2_register
@_s2_no_var_expand  # keep {{ var }} for our own substitution (as jupysql does)
def sql(line, cell=None):
    """%sql STATEMENT (returns rows) and %%sql [name <<] (cell), as in SingleStore Notebooks."""
    ns = get_ipython().user_ns
    if cell is None:
        name, text = _s2_magic_target(line)
        result = _s2_execute(text, ns=ns)
        if name:
            ns[name] = result
            return None
        return result
    name, _ = _s2_magic_target(line)
    result = _s2_execute(cell, ns=ns)
    if name:
        ns[name] = result
    if isinstance(result, _S2ResultSet):
        ns["df"] = result.DataFrame()
    return result
'''


# Statement splitter shared by the kernel (inside STARTUP) and the MCP server
# (notebook.py checks each statement of a SQL cell for writes).
SPLIT_SOURCE = r'''
def _s2_split(sql):
    """Split on ; outside quotes and comments (a CREATE PROCEDURE … END is kept whole)."""
    out, start, i, quote, depth = [], 0, 0, None, 0
    import re as _re
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == "\\" and quote != "`":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif sql.startswith("--", i) or ch == "#":
            j = sql.find("\n", i)
            i = len(sql) if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = len(sql) if j < 0 else j + 1
        elif ch.isalpha() and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            m = _re.match(r"[A-Za-z_]\w*", sql[i:])
            w = m.group().upper()
            if w == "BEGIN" and (depth or _re.match(r"\s*CREATE\s+(OR\s+REPLACE\s+)?(DEFINER\s*=\s*\S+\s+)?(AGGREGATE\s+)?(PROCEDURE|FUNCTION|TRIGGER)\b", sql[start:], _re.I)):
                depth += 1
            elif w == "CASE" and depth:
                depth += 1
            elif w == "END" and depth and not _re.match(r"\s*(IF|LOOP|WHILE|REPEAT|FOR)\b", sql[i + 3:], _re.I):
                depth -= 1
            i += len(w)
            continue
        elif ch == ";" and not depth:
            out.append(sql[start:i])
            start = i + 1
        i += 1
    out.append(sql[start:])
    return [s.strip() for s in out if s.strip() and not all(l.strip().startswith(("--", "#")) for l in s.strip().splitlines())]
'''
STARTUP = STARTUP.replace("__S2_SPLIT__", SPLIT_SOURCE)


def split_statements(sql: str) -> list[str]:
    ns: dict = {}
    exec(SPLIT_SOURCE, ns)  # noqa: S102 - our own constant source
    return ns["_s2_split"](sql)


def _emit(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def _to_output(msg: dict) -> dict | None:
    """IOPub message -> nbformat output, or None for messages that aren't output."""
    kind, content = msg["header"]["msg_type"], msg["content"]
    if kind == "stream":
        return {"output_type": "stream", "name": content["name"], "text": content["text"]}
    if kind in ("display_data", "execute_result"):
        out = {"output_type": kind, "data": content.get("data", {}), "metadata": content.get("metadata", {})}
        if kind == "execute_result":
            out["execution_count"] = content.get("execution_count")
        return out
    if kind == "error":
        return {"output_type": "error", "ename": content["ename"], "evalue": content["evalue"],
                "traceback": content.get("traceback", [])}
    return None


class Bridge:
    def __init__(self) -> None:
        from jupyter_client.manager import start_new_kernel

        self.km, self.kc = start_new_kernel(kernel_name="python3")
        self.jobs: queue.Queue[dict] = queue.Queue()
        self._startup()

    def _startup(self) -> None:
        self._run(None, STARTUP, silent=True)

    def _run(self, run_id: str | None, code: str, silent: bool = False) -> None:
        msg_id = self.kc.execute(code, silent=silent, store_history=not silent)
        status, count = "ok", None
        while True:
            try:
                msg = self.kc.get_iopub_msg(timeout=3600)
            except queue.Empty:
                status = "aborted"
                break
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            kind = msg["header"]["msg_type"]
            if kind == "status" and msg["content"].get("execution_state") == "idle":
                break
            if kind == "execute_input":
                count = msg["content"].get("execution_count")
            if kind == "error":
                status = "error"
            if kind == "clear_output" and run_id:
                _emit({"id": run_id, "event": "clear"})
                continue
            out = _to_output(msg)
            if out and run_id and not silent:
                _emit({"id": run_id, "event": "output", "output": out})
            elif out and silent and out["output_type"] == "error":
                _emit({"event": "error", "message": f"Kernel startup failed: {out['ename']}: {out['evalue']}"})
        if run_id:
            _emit({"id": run_id, "event": "done", "status": status, "execution_count": count})

    def worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            if job.get("restart"):
                try:
                    self.restart()
                except Exception as exc:  # noqa: BLE001
                    _emit({"event": "error", "message": f"Kernel restart failed: {exc}"})
                continue
            if job.get("silent"):
                try:
                    self._run(None, job["code"], silent=True)
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                self._run(job["id"], job["code"])
            except Exception as exc:  # noqa: BLE001 - report and keep serving
                _emit({"id": job["id"], "event": "output",
                       "output": {"output_type": "error", "ename": type(exc).__name__, "evalue": str(exc), "traceback": []}})
                _emit({"id": job["id"], "event": "done", "status": "error", "execution_count": None})

    def restart(self) -> None:
        self.km.restart_kernel(now=True)
        self.kc.stop_channels()
        self.kc = self.km.client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=60)
        self._startup()
        _emit({"event": "restarted"})

    def shutdown(self) -> None:
        try:
            self.kc.stop_channels()
            self.km.shutdown_kernel(now=True)
        except Exception:  # noqa: BLE001 - exiting anyway
            pass


def main() -> None:
    try:
        bridge = Bridge()
    except Exception as exc:  # noqa: BLE001
        _emit({"event": "error", "message": f"Couldn't start the Python kernel: {exc}"})
        return
    threading.Thread(target=bridge.worker, daemon=True).start()
    _emit({"event": "ready"})
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        op = msg.get("op")
        if op == "execute":
            bridge.jobs.put(msg)
        elif op == "interrupt":
            bridge.km.interrupt_kernel()
        elif op == "restart":
            # Drop queued cells; the running one ends with the restart.
            while not bridge.jobs.empty():
                job = bridge.jobs.get_nowait()
                if job:
                    _emit({"id": job["id"], "event": "done", "status": "aborted", "execution_count": None})
            bridge.km.interrupt_kernel()
            bridge.jobs.put({"restart": True})  # after the running cell has stopped
        elif op == "shutdown":
            break
    # stdin closed (server gone) or shutdown requested.
    bridge.jobs.put(None)
    bridge.shutdown()


if __name__ == "__main__":
    main()
