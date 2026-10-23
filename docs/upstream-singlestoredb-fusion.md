# Upstream issue draft: singlestoredb compiles every Fusion SQL grammar at import

For Jonas to file at https://github.com/singlestore-labs/singlestoredb-python/issues (not filed by Claude).
Checked 2026-10-16 in singlestoredb 1.17.1 (installed) and on the repo's `main` branch (same code).

---

**Title:** Importing the MySQL connection compiles all Fusion SQL grammars (~0.5 s CPU), even with Fusion disabled

**What happens**

`singlestoredb/mysql/connection.py` (and `http/connection.py`) do `from .. import fusion`.
`singlestoredb/fusion/__init__.py` imports every module in `fusion/handlers/`, and each module calls
`SomeHandler.register()` at import. `SQLHandler.register()` calls `cls.compile()`, which runs
`process_grammar()` and builds a parsimonious `Grammar` for the handler's docstring – 45 handlers in 1.17.1.
This happens on the first `singlestoredb.connect()` of every process, although `fusion.enabled`
(`SINGLESTOREDB_FUSION_ENABLED`) defaults to False and `get_handler()` returns None before any grammar is used.
There is no option that skips the compilation (`fusion.enabled` is only checked in `get_handler()`).

**Repro** (Windows 11, Python 3.12, singlestoredb 1.17.1)

```python
import time, singlestoredb
t = time.perf_counter()
import singlestoredb.mysql.connection   # imports singlestoredb.fusion
print(time.perf_counter() - t)
```

- As is: 0.45–0.99 s (median ~0.85 s on a busy laptop); under cProfile, `SQLHandler.compile` /
  `process_grammar` are ~72 % of this import.
- With `SQLHandler.compile` stubbed out (only `command_key` set): 0.18–0.61 s (median ~0.32 s).
- So about 0.5 s of wall time and CPU per process start goes to grammars that are never used when Fusion is off.

For a long-running app that's once, but for short-lived processes (CLI tools, MCP servers that restart,
notebook kernels, serverless functions) it is a fixed cost on every start.

**Suggested fix** (any of these)

1. In `register()`, compute only what the registry needs – `command_key` via `get_keywords()` on the
   stripped docstring, which is a regex – and leave `compile()` to `execute()` / `help`, which already call
   `type(self).compile()` (it is idempotent through `_is_compiled`).
2. Or skip importing `fusion.handlers` until `fusion.enabled` is true (e.g. in `get_handler()` on first use).
3. Or at least an option / environment variable to defer handler registration.

Option 1 keeps the behaviour (the same handlers, same regex in the registry) and needs no new option.
