"""Config view: every engine variable with its value per node, read-only.

Sources (checked on SingleStore 9.0.40 and in the 9.0 docs):
- information_schema.MV_GLOBAL_VARIABLES: per node (NODE_ID, NODE_TYPE) the global value of every variable
  (names upper case), plus IS_SETTABLE_AT_STARTUP / IS_SETTABLE_AT_RUNTIME / IS_PER_SITE.
- SHOW GLOBAL VARIABLES EXTENDED (on the aggregator this server is connected to): Variable_type /
  Cluster_type (e.g. "Local", "Cluster-wide for aggregators") and In_Allow_List.
- The engine exposes no default value, description or category. Those come from the docs page "List of
  Engine Variables" for the cluster's version, which this server downloads on request and keeps in
  ~/.singlestore-mcp (it's not shipped with the code). Without it, defaults show as unknown.
  The docs say ON and 1 are synonymous, as are OFF and 0; sync variables are set on the master aggregator
  only; non-sync variables are set on each node and persisted in memsql.cnf.
Nothing here runs SET: the page shows the statement for the user to copy.
"""

from __future__ import annotations

import datetime
import html as html_lib
import json
import re
import threading
import time
import urllib.request
from html.parser import HTMLParser
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from ..db import db
from ..paths import data_dir
from ._core import APP_ONLY, apps, jsonable_rows, register_app, tool_result, with_browser_link

URI = "ui://singlestore/config.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
_MARKER = "/* s2-config */"
DOCS_URL = "https://docs.singlestore.com/db/v{version}/reference/configuration-reference/engine-variables/list-of-engine-variables/"
_DOCS_TTL_S = 7 * 86400
_CLUSTER_TTL_S = 30

register_app(
    URI,
    "config.html",
    name="Config",
    description="Every SingleStore engine variable with its value per node, its default and whether it was changed",
)


# ----------------------------------------------------------------------------- docs page (defaults, descriptions)

class _DocsParser(HTMLParser):
    """Reads the variable tables of "List of Engine Variables": section (h2), category (h3), Name / Description /
    Default (+ any extra column) per row, and every element id (for links to in-depth definitions)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.section = ""
        self.category = ""
        self._heading: str | None = None
        self._heading_text: list[str] = []
        self._in_table = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._header: list[str] | None = None
        self._cell_is_th = False
        self.rows: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for k, v in attrs:
            if k == "id" and v:
                self.ids.add(v)
        if tag in ("h2", "h3") and not self._in_table:
            self._heading, self._heading_text = tag, []
        elif tag == "table":
            self._in_table += 1
            self._header = None
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._cell_is_th = [], tag == "th"
        elif tag in ("p", "br", "li") and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == self._heading:
            text = " ".join("".join(self._heading_text).split())
            if tag == "h2":
                self.section, self.category = text, ""
            else:
                self.category = text
            self._heading = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._cell_is_th or self._header is None and [c.lower() for c in self._row[:1]] == ["name"]:
                self._header = [c.lower() for c in self._row]
            elif self._header:
                self.rows.append({"section": self.section, "category": self.category,
                                  "cells": dict(zip(self._header, self._row))})
            self._row = None
            self._cell_is_th = False
        elif tag == "table" and self._in_table:
            self._in_table -= 1

    def handle_data(self, data: str) -> None:
        if self._heading:
            self._heading_text.append(data)
        if self._cell is not None:
            self._cell.append(data)


_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def parse_docs_html(page: str, url: str = "") -> dict[str, Any]:
    p = _DocsParser()
    p.feed(page)
    variables: dict[str, dict[str, Any]] = {}
    for row in p.rows:
        cells = row["cells"]
        if "name" not in cells:
            continue
        names = [n for n in re.split(r"[\s,]+", cells["name"]) if _NAME.match(n)]
        section = row["section"].lower()
        sync = "non-sync" if "non-sync" in section else "sync" if "sync" in section else None
        extra = {k: v for k, v in cells.items() if k not in ("name", "description", "default") and v}
        for name in names:
            anchor = name.replace("_", "-")
            variables.setdefault(name, {
                "default": cells.get("default"),
                "description": cells.get("description"),
                "sync": sync,
                "section": row["section"],
                "category": row["category"] or ("General (sync)" if sync == "sync" else "General"),
                "extra": extra or None,
                "url": f"{url}#{anchor}" if url and anchor in p.ids else url or None,
                "in_depth": anchor in p.ids,
            })
    return {"variables": variables, "anchors": len(p.ids)}


_docs_lock = threading.Lock()


def _docs_file(version: str) -> Any:
    return data_dir() / f"engine-variables-v{version}.json"


def load_docs(version: str, fetch: bool = False, refresh: bool = False) -> dict[str, Any] | None:
    """The parsed docs for ``version`` ("9.0"): from the local copy, or downloaded when ``fetch`` and missing / old."""
    path = _docs_file(version)
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
    fresh = cached is not None and time.time() - cached.get("fetched_at", 0) < _DOCS_TTL_S
    if not fetch or (fresh and not refresh):
        return cached
    url = DOCS_URL.format(version=version)
    with _docs_lock:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "singlestore-mcp-server (engine variable reference)"})
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https docs URL
                page = resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - offline / proxy: keep the old copy, say why
            if cached is not None:
                return {**cached, "fetch_error": str(exc)[:200]}
            return {"error": f"Couldn't download the engine variable reference from {url}: {str(exc)[:200]}",
                    "url": url, "variables": {}}
        parsed = parse_docs_html(page, url)
        doc = {"version": version, "url": url, "fetched_at": time.time(), **parsed}
        if parsed["variables"]:
            path.write_text(json.dumps(doc), encoding="utf-8")
        else:
            doc["error"] = "The docs page was downloaded but no variable tables were found in it (its layout may have changed)."
        return doc


# ----------------------------------------------------------------------------- comparing values

def _canon(v: Any) -> str | None:
    """Comparable form of a value, or None when it can't be compared reliably."""
    if v is None:
        return None
    s = html_lib.unescape(str(v)).strip().strip("'\"`").strip()
    low = s.lower()
    if re.fullmatch(r"[a-z0-9_]+(\s*,\s*[a-z0-9_]+)+", low):  # a list such as sql_mode: order and spaces don't matter
        return ",".join(sorted(p.strip() for p in low.split(",")))
    if low in ("on", "true"):
        return "1"
    if low in ("off", "false"):
        return "0"
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else repr(f)
    except ValueError:
        return low


def _default_canon(default: Any) -> str | None:
    """The docs' default cell, when it is one plain value (optionally followed by a remark in parentheses)."""
    if default is None:
        return None
    s = html_lib.unescape(str(default)).strip()
    s = re.sub(r"\s*\(.*\)\s*$", "", s).strip()
    if not s or s.lower() in ("-", "n/a", "none", "empty", "varies", "see description"):
        return None
    # Only plain values are compared: a number, a word (ON, AUTO, default_pool), a list of words (sql_mode) or a
    # short quoted string. Expressions ("10*60", "2^64-1"), sizes with units ("16MB", "256K"), paths and prose are
    # left as "unknown" rather than guessed.
    plain = (re.fullmatch(r"-?\d+(\.\d+)?", s) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s)
             or re.fullmatch(r"\w+(\s*,\s*\w+)+", s) or re.fullmatch(r"(['\"])[\w-][\w.-]*\1", s))
    return _canon(s) if plain else None


def set_statement(name: str, value: Any) -> str:
    s = "" if value is None else str(value)
    if re.fullmatch(r"-?\d+(\.\d+)?|ON|OFF", s, re.I):
        literal = s
    else:
        literal = "'" + s.replace("\\", "\\\\").replace("'", "''") + "'"
    return f"SET GLOBAL {name} = {literal};"


# ----------------------------------------------------------------------------- cluster data

_cluster_cache: dict[str, Any] = {}
_cluster_lock = threading.Lock()


def _q(sql: str) -> list[dict[str, Any]]:
    return jsonable_rows(db.execute(f"{_MARKER} {sql}")[1])


def _cluster(refresh: bool) -> dict[str, Any]:
    with _cluster_lock:
        hit = _cluster_cache.get("v")
        if hit and not refresh and time.time() - hit["at"] < _CLUSTER_TTL_S:
            return hit
    per_node, extended, nodes, version = db.parallel(
        lambda: _q("SELECT NODE_ID, NODE_TYPE, VARIABLE_NAME, VARIABLE_VALUE, IS_SETTABLE_AT_STARTUP, IS_SETTABLE_AT_RUNTIME,"
                   " IS_PER_SITE FROM information_schema.MV_GLOBAL_VARIABLES"),
        lambda: jsonable_rows(db.execute(f"{_MARKER} SHOW GLOBAL VARIABLES EXTENDED")[1]),
        lambda: _q("SELECT ID, IP_ADDR, PORT, TYPE FROM information_schema.MV_NODES ORDER BY ID"),
        lambda: _q("SELECT @@memsql_version AS v"),
    )
    value = {"at": time.time(), "per_node": per_node, "extended": extended, "nodes": nodes,
             "version": str(version[0]["v"]) if version else ""}
    with _cluster_lock:
        _cluster_cache["v"] = value
    return value


def _short_host(host: str) -> str:
    return host.split(".svc-")[0] if ".svc-" in host else host


def _flag(v: Any) -> bool | None:
    if v is None:
        return None
    return str(v).strip().lower() in ("1", "yes", "true", "on")


def collect(fetch_docs: bool = False, refresh: bool = False, refresh_docs: bool = False) -> dict[str, Any]:
    c = _cluster(refresh)
    m = re.match(r"(\d+)\.(\d+)", c["version"])
    doc_version = f"{m.group(1)}.{m.group(2)}" if m else None
    docs = load_docs(doc_version, fetch=fetch_docs, refresh=refresh_docs) if doc_version else None
    dvars = (docs or {}).get("variables", {})
    node_ids = [n["ID"] for n in c["nodes"]]
    by_name: dict[str, dict[str, Any]] = {}
    for r in c["per_node"]:
        name = str(r["VARIABLE_NAME"]).lower()
        v = by_name.setdefault(name, {"name": name, "values": {}, "settable_startup": _flag(r["IS_SETTABLE_AT_STARTUP"]),
                                      "settable_runtime": _flag(r["IS_SETTABLE_AT_RUNTIME"]), "per_site": _flag(r["IS_PER_SITE"])})
        v["values"][str(r["NODE_ID"])] = r["VARIABLE_VALUE"]
    for r in c["extended"]:
        name = str(r.get("Variable_name", "")).lower()
        if not name:
            continue
        v = by_name.setdefault(name, {"name": name, "values": {}, "settable_startup": _flag(r.get("Is_Settable_At_Startup")),
                                      "settable_runtime": _flag(r.get("Is_Settable_At_Runtime")), "per_site": _flag(r.get("Is_Per_Site"))})
        v["value_here"] = r.get("Value")
        v["variable_type"] = r.get("Variable_type")
        v["cluster_type"] = r.get("Cluster_type")
        v["in_allow_list"] = _flag(r.get("In_Allow_List"))
    out = []
    for name in sorted(by_name):
        v = by_name[name]
        values = v["values"]
        distinct = sorted({str(x) for x in values.values()}) if values else ([str(v["value_here"])] if "value_here" in v else [])
        d = dvars.get(name)
        dc = _default_canon(d["default"]) if d else None
        if dc is None:
            status = "unknown"
        else:
            current = list(values.values()) or [v.get("value_here")]
            status = "changed" if any(_canon(x) != dc for x in current) else "default"
        value = v.get("value_here", distinct[0] if len(distinct) == 1 else None)
        out.append({
            **v, "value": value if len(distinct) <= 1 else None, "distinct": distinct,
            "differs": len({_canon(x) for x in values.values()}) > 1,
            "default": d["default"] if d else None, "status": status,
            "sync": d["sync"] if d else None, "category": d["category"] if d else None,
            "description": d["description"] if d else None, "doc_url": d["url"] if d else None,
            "doc_extra": d["extra"] if d else None,
            "set_sql": set_statement(name, value if value is not None else (distinct[0] if distinct else "")),
        })
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "version": c["version"],
        "nodes": [{"id": n["ID"], "host": _short_host(n["IP_ADDR"]), "port": n["PORT"], "type": n["TYPE"]} for n in c["nodes"]],
        "node_ids": node_ids,
        "variables": out,
        "docs": None if docs is None else {
            "version": docs.get("version", doc_version), "url": docs.get("url") or (DOCS_URL.format(version=doc_version) if doc_version else None),
            "fetched_at": docs.get("fetched_at"), "count": len(dvars), "error": docs.get("error"),
            "fetch_error": docs.get("fetch_error")},
        "docs_url": DOCS_URL.format(version=doc_version) if doc_version else None,
    }


def _value_text(v: dict[str, Any]) -> str:
    if v["differs"]:
        return "differs by node: " + "; ".join(f"node {k}={str(x)[:40]}" for k, x in v["values"].items())
    return str(v["value"] if v["value"] is not None else (v["distinct"] or [""])[0])[:80]


def summary(d: dict[str, Any], search: str | None = None, changed_only: bool = False, limit: int = 400) -> str:
    vs = d["variables"]
    if search:
        s = search.lower()
        vs = [v for v in vs if s in v["name"] or s in (v["description"] or "").lower() or s in (v["category"] or "").lower()]
    if changed_only:
        vs = [v for v in vs if v["status"] == "changed" or v["differs"]]
    docs = d["docs"]
    head = [f"SingleStore {d['version']}, {len(d['nodes'])} nodes, {len(d['variables'])} engine variables"
            + (f"; {len(vs)} match" if search or changed_only else "") + "."]
    if not docs or not docs.get("count"):
        head.append("Default values are unknown: the engine doesn't report them and the docs reference hasn't been "
                    f"downloaded{' (' + docs['error'] + ')' if docs and docs.get('error') else ''}. Open the Config view to load it.")
    else:
        changed = sum(1 for v in d["variables"] if v["status"] == "changed")
        unknown = sum(1 for v in d["variables"] if v["status"] == "unknown")
        head.append(f"{changed} changed from the documented default, {unknown} with no comparable default (docs v{docs['version']}).")
    head.append(f"{sum(1 for v in d['variables'] if v['differs'])} differ between nodes.")
    lines = []
    for v in vs[:limit]:
        tag = {"changed": " [CHANGED]", "default": "", "unknown": ""}[v["status"]] + (" [DIFFERS BY NODE]" if v["differs"] else "")
        dflt = f" (documented default {str(v['default'])[:40]})" if v["default"] and v["status"] != "default" else ""
        lines.append(f"- {v['name']} = {_value_text(v)}{dflt}{tag}")
    if len(vs) > limit:
        lines.append(f"… {len(vs) - limit} more (narrow with search=)")
    return "\n".join(head + lines)


@apps.tool(resource_uri=URI, title="Config", annotations=READ_ONLY)
def cluster_config(search: str | None = None, changed_only: bool = False) -> CallToolResult:
    """List SingleStore engine variables (configuration parameters) with their values per node, read-only.

    Marks variables whose value differs from the documented default (defaults come from the docs page for the
    cluster's version, downloaded once and kept locally) and variables whose value differs between nodes.
    Opens the Config view; nothing is changed.

    Args:
        search: Only variables whose name, description or category contains this text.
        changed_only: Only variables changed from the default or differing between nodes.

    The result includes ``browser_url``, which opens this view full-window in the user's browser: post it
    as a clickable link right under the app.
    """
    d = collect(fetch_docs=True)
    text = summary(d, search, changed_only)
    while len(text) > 45000:
        text = summary(d, search, changed_only, limit=max(20, text.count("\n- ") // 2))
    data: dict[str, Any] = {"kind": "summary", "search": search, "changed_only": changed_only}
    text = with_browser_link(text, data, "cluster_config", {"search": search, "changed_only": changed_only or None})
    return tool_result(text, data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def config_data(fetch_docs: bool = False, refresh: bool = False, refresh_docs: bool = False) -> CallToolResult:
    """All engine variables for the Config view. ``fetch_docs`` downloads the docs reference when it isn't kept locally yet."""
    d = collect(fetch_docs=fetch_docs or refresh_docs, refresh=refresh, refresh_docs=refresh_docs)
    return tool_result(f"{len(d['variables'])} variables", d)
