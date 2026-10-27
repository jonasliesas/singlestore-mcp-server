"""SQL Editor files on SAS Viya: SAS Content (Folders / Files services) and the SAS Server file system (the
Compute server, as in SAS Studio's "SAS Server" tree).

A file on Viya is named by a *ref*, a string the editor keeps like a local path (tabs, drafts, Recent):

    sas-content://<connection>/Users/me/My Folder/query.sql   – SAS Content, by folder path
    sas-server://<connection>/srv/landingzone/query.sas        – the Compute server's file system

``<connection>`` is the saved SAS Viya connection (``sas_viya``) the file belongs to. Reading and writing always
go through the *active* connection: a ref of another connection raises ``ConnectionMismatch`` (Save then asks
whether to save it to the active connection instead).

SAS Content uses the documented REST APIs: ``GET /folders/folders/@item?path=`` (folder by path),
``/folders/folders/{id}/members``, ``/folders/rootFolders``, ``/folders/folders/@myFolder``; files are
``/files/files/{id}`` (``/content`` GET / PUT with ``If-Match``) and created with
``POST /files/files?parentFolderUri=``. The SAS Server file system uses the Compute API's file endpoints in the
Workspace's reused Compute session: ``/compute/sessions/{id}/files/{path}`` (properties, ``/members``,
``/content``), where the path's slashes are written ``~fs~`` (``~fs~tmp~fs~a.sql`` is ``/tmp/a.sql``) and
``~~`` is the session's home directory. Both answer an ``ETag``; a write sends the version the editor opened
as ``If-Match``, so a newer version on the server is never overwritten silently (HTTP 412 → ``conflict``).
"""

from __future__ import annotations

import posixpath
import re
import urllib.parse
from datetime import datetime
from typing import Any

from . import sas_rest, sas_viya
from .sas_sql import SasError

LOCATIONS = ("content", "server")
_SCHEMES = {"content": "sas-content://", "server": "sas-server://"}
LABELS = {"content": "SAS Content", "server": "SAS Server"}
MAX_FILE_BYTES = 2_000_000
MAX_NOTEBOOK_BYTES = 20_000_000  # a notebook keeps its outputs (tables, charts), as a local one does
MAX_LISTED = 1000
SQL_SUFFIXES = (".sql", ".sas")
NOTEBOOK_SUFFIXES = (".ipynb",)
KINDS = {"sql": SQL_SUFFIXES, "notebook": NOTEBOOK_SUFFIXES}
IPYNB_MEDIA_TYPE = "application/x-ipynb+json"
_COLLECTION = "application/vnd.sas.collection+json, application/json"
_FOLDER_JSON = "application/vnd.sas.content.folder+json"
_FILE_JSON = "application/vnd.sas.file+json"
_FILE_PROPS = "application/vnd.sas.compute.file.properties+json"
_LOCKDOWN = re.compile(r"""lockdown\s+path\s*=\s*(['"])(.+?)\1""", re.I)


class ConnectionMismatch(LookupError):
    """The file belongs to another SAS Viya connection than the active one."""

    def __init__(self, file_connection: str, active: str | None) -> None:
        self.file_connection, self.active = file_connection, active
        super().__init__(f"This file is on the SAS Viya connection {file_connection!r}, but the active one is "
                         f"{active!r}: switch to {file_connection!r} (Connections → SAS Viya, or the Viya picker "
                         "in a SAS tab) to use it.")


# ------------------------------------------------------------------ refs (the location model)


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(tuple(_SCHEMES.values()))


def make_ref(location: str, connection: str, path: str) -> str:
    if location not in _SCHEMES:
        raise ValueError(f"location must be one of {', '.join(LOCATIONS)}")
    if not connection or "/" in connection:
        raise ValueError("No SAS Viya connection for this file.")
    return f"{_SCHEMES[location]}{connection}{norm_path(path)}"


def parse_ref(ref: str) -> tuple[str, str, str]:
    """(location, connection, absolute path) of a ref."""
    for loc, scheme in _SCHEMES.items():
        if isinstance(ref, str) and ref.startswith(scheme):
            rest = ref[len(scheme):]
            conn, sep, path = rest.partition("/")
            if not conn or not sep:
                break
            return loc, conn, norm_path("/" + path)
    raise ValueError(f"Not a SAS Viya file: {ref!r}")


def norm_path(path: str) -> str:
    """An absolute POSIX path without '.', '..', doubled or trailing slashes ("/" for the root)."""
    path = str(path or "").replace("\\", "/").strip()
    if not path.startswith("/"):
        raise ValueError(f"Give an absolute path (starting with /): {path!r}")
    out = posixpath.normpath(path)
    return "/" if out in ("/", "//") else out.rstrip("/")


def split(path: str) -> tuple[str, str]:
    """(parent folder, name) of a file path; the name must be a real name."""
    path = norm_path(path)
    parent, name = posixpath.split(path)
    check_name(name)
    return parent or "/", name


def check_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name or len(name) > 255 or any(ord(c) < 32 for c in name):
        raise ValueError(f"Not a valid file or folder name: {name!r}")
    return name


def with_suffix(path: str, suffix: str = ".sql") -> str:
    """A file name without any extension gets .sql (as a local save does; a notebook gets .ipynb)."""
    parent, name = split(path)
    if "." not in name.strip("."):
        name += suffix
    return posixpath.join(parent, name)


def display(ref: str) -> str:
    loc, conn, path = parse_ref(ref)
    return f"{LABELS[loc]} ({conn}): {path}"


def wanted(name: str, show_all: bool, kind: str = "sql") -> bool:
    return show_all or name.lower().endswith(KINDS.get(kind, SQL_SUFFIXES))


def max_bytes(path: str) -> int:
    return MAX_NOTEBOOK_BYTES if str(path).lower().endswith(NOTEBOOK_SUFFIXES) else MAX_FILE_BYTES


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def epoch(ts: Any) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return round(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def media_type(name: str) -> str:
    """Media type of a new SAS Content file: SAS programs as SAS Studio saves them, notebooks as Jupyter's
    ``application/x-ipynb+json``, anything else as text."""
    low = name.lower()
    if low.endswith(".sas"):
        return "application/vnd.sas.studio.programfile"
    return IPYNB_MEDIA_TYPE if low.endswith(".ipynb") else "text/plain"


def content_disposition(name: str) -> str:
    """``filename*`` (RFC 5987) keeps any character of the name; HTTP headers themselves are Latin-1."""
    return f"attachment; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def server_segment(path: str) -> str:
    """A Compute file path as the API wants it: ``~fs~`` for each slash, each name percent-encoded (``~`` too)."""
    parts = [p for p in norm_path(path).split("/") if p]
    return "~fs~" + "~fs~".join(urllib.parse.quote(p, safe="").replace("~", "%7E") for p in parts)


def decide_write(exists: bool, current: str | None, expected: str | None, overwrite: bool) -> str:
    """What a save does with what's on the server: "create", "write", "exists" (ask: replace it?) or "conflict"
    (the file changed since the editor opened it: ask before overwriting)."""
    if not exists:
        return "create"
    if overwrite:
        return "write"
    if expected is None:
        return "exists"
    return "write" if current is not None and current == expected else "conflict"


# ------------------------------------------------------------------ HTTP


def _base() -> str:
    url = sas_viya.settings()["url"]
    if not url:
        raise sas_viya.NeedsSignIn("SAS Viya isn't set up: add a SAS Viya connection in the Connections window.")
    return url


def _http(method: str, path: str, **kw: Any) -> tuple[int, Any, dict[str, str]]:
    """One request to the active Viya (a test replaces this with a fake Viya)."""
    return sas_rest.request(method, _base() + path, **kw)


def active_connection() -> str:
    name = sas_viya.active_name()
    if not name:
        raise sas_viya.NeedsSignIn("SAS Viya isn't set up: add a SAS Viya connection in the Connections window.")
    return name


def check_connection(connection: str, use_active: bool = False) -> str:
    """The active connection; ConnectionMismatch when ``connection`` is another one (unless ``use_active``)."""
    active = active_connection()
    if connection != active and not use_active:
        raise ConnectionMismatch(connection, active)
    return active


def _fail(status: int, body: Any, what: str) -> Exception:
    if status == 403:
        return PermissionError(f"No permission to {what} on SAS Viya.")
    if status == 404:
        return LookupError(f"Couldn't {what}: not found on SAS Viya.")
    return sas_rest._viya_error(status, body, f"Couldn't {what}")


def _items(body: Any) -> list[dict[str, Any]]:
    return [i for i in (body.get("items") or [])] if isinstance(body, dict) else []


# ------------------------------------------------------------------ SAS Content


def _folder(path: str) -> dict[str, Any]:
    path = norm_path(path)
    status, body, _ = _http("GET", f"/folders/folders/@item?path={urllib.parse.quote(path, safe='')}",
                            accept=f"{_FOLDER_JSON}, application/json")
    if status == 404:
        raise LookupError(f"No SAS Content folder {path}.")
    if status != 200 or not isinstance(body, dict) or not body.get("id"):
        raise _fail(status, body, f"open the folder {path}")
    return body


def _members(folder_id: str) -> list[dict[str, Any]]:
    status, body, _ = _http("GET", f"/folders/folders/{folder_id}/members?limit={MAX_LISTED * 5}", accept=_COLLECTION)
    if status != 200:
        raise _fail(status, body, "list the folder")
    return _items(body)


def _is_folder(m: dict[str, Any]) -> bool:
    return str(m.get("uri") or "").startswith("/folders/folders/")


def _is_file(m: dict[str, Any]) -> bool:
    return str(m.get("uri") or "").startswith("/files/files/")


_my_folders: dict[str, str] = {}  # Viya address + user -> My Folder path


def _my_folder() -> str | None:
    """The path of the user's My Folder (``/Users/<user>/My Folder``), remembered per Viya and connection."""
    key = f"{sas_viya.settings().get('url')}|{sas_viya.active_name()}"
    if key not in _my_folders:
        path = _find_my_folder()
        if path:
            _my_folders[key] = path
        return path
    return _my_folders[key]


def _find_my_folder() -> str | None:
    status, body, _ = _http("GET", "/folders/folders/@myFolder")
    if status != 200 or not isinstance(body, dict) or not body.get("id"):
        return None
    status, anc, _ = _http("GET", f"/folders/ancestors?childUri=/folders/folders/{body['id']}", accept=_COLLECTION)
    by_uri = {f"/folders/folders/{a.get('id')}": a for a in (anc if isinstance(anc, list) else _items(anc))}
    names, parent = [body.get("name") or "My Folder"], body.get("parentFolderUri")
    while parent in by_uri and len(names) < 50:
        names.append(by_uri[parent].get("name") or "")
        parent = by_uri[parent].get("parentFolderUri")
    return "/" + "/".join(reversed(names))


def _file_details(ids: list[str]) -> dict[str, dict[str, Any]]:
    """Size and modified time of several files in one request per 100 (the folder members don't say)."""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        filt = urllib.parse.quote("in(id," + ",".join(f"'{x}'" for x in chunk) + ")", safe="")
        status, body, _ = _http("GET", f"/files/files?limit={len(chunk)}&filter={filt}", accept=_COLLECTION)
        if status == 200:
            out.update({str(f.get("id")): f for f in _items(body)})
    return out


def content_browse(path: str | None, show_all: bool = False, kind: str = "sql") -> dict[str, Any]:
    my = _my_folder()
    shortcuts = [s for s in ({"label": "My Folder", "path": my} if my else None,
                             {"label": "Public", "path": "/Public"}, {"label": "All folders", "path": "/"}) if s]
    path = norm_path(path) if path else (my or "/")
    folders, files = [], []
    if path == "/":
        status, body, _ = _http("GET", f"/folders/rootFolders?limit={MAX_LISTED}", accept=_COLLECTION)
        if status != 200:
            raise _fail(status, body, "list the SAS Content folders")
        folders = [{"name": f["name"], "path": "/" + f["name"]} for f in _items(body) if f.get("name")]
    else:
        members = _members(_folder(path)["id"])
        for m in members:
            name = m.get("name") or ""
            if _is_folder(m) and name:
                folders.append({"name": name, "path": posixpath.join(path, name)})
        listed = [m for m in members if _is_file(m) and wanted(m.get("name") or "", show_all, kind)][:MAX_LISTED]
        details = _file_details([m["uri"].rsplit("/", 1)[-1] for m in listed])
        for m in listed:
            d = details.get(m["uri"].rsplit("/", 1)[-1], {})
            files.append({"name": m["name"], "path": posixpath.join(path, m["name"]), "size": d.get("size"),
                          "modified": epoch(d.get("modifiedTimeStamp") or m.get("modifiedTimeStamp")),
                          "media_type": d.get("contentType")})
    folders.sort(key=lambda f: f["name"].lower())
    files.sort(key=lambda f: f["modified"] or 0, reverse=True)
    return {"path": path, "parent": None if path == "/" else posixpath.dirname(path) or "/",
            "folders": folders[:MAX_LISTED], "files": files, "shortcuts": shortcuts}


def _content_member(path: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(parent folder, the member named like the file or None)."""
    parent, name = split(path)
    folder = _folder(parent)
    member = next((m for m in _members(folder["id"]) if m.get("name") == name), None)
    return folder, member


def content_read(path: str) -> dict[str, Any]:
    path = norm_path(path)
    _, member = _content_member(path)
    if member is None:
        raise LookupError(f"{path} doesn't exist in SAS Content.")
    if not _is_file(member):
        raise ValueError(f"{path} isn't a file the editor can open.")
    status, meta, _ = _http("GET", member["uri"])
    if status != 200 or not isinstance(meta, dict):
        raise _fail(status, meta, f"read {path}")
    if (meta.get("size") or 0) > max_bytes(path):
        raise ValueError(f"{path} is larger than {max_bytes(path) // 1_000_000} MB.")
    status, raw, headers = _http("GET", member["uri"] + "/content", accept="*/*", raw=True)
    if status != 200:
        raise _fail(status, raw, f"read {path}")
    return {"text": decode(raw), "version": headers.get("etag"), "path": path, "media_type": meta.get("contentType")}


def content_write(path: str, text: str, expected: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    path = norm_path(path)
    data = text.encode("utf-8")
    if len(data) > max_bytes(path):
        raise ValueError(f"The text is larger than {max_bytes(path) // 1_000_000} MB.")
    folder, member = _content_member(path)
    name = posixpath.basename(path)
    if member is not None and not _is_file(member):
        raise ValueError(f"{path} is a folder or another kind of SAS Content item, not a file.")
    meta, current = {}, None
    if member is not None:
        status, meta, headers = _http("GET", member["uri"])
        if status != 200 or not isinstance(meta, dict):
            raise _fail(status, meta, f"read {path}")
        current = headers.get("etag")
    action = decide_write(member is not None, current, expected, overwrite)
    if action == "exists":
        return {"exists": True, "saved": False, "path": path}
    if action == "conflict":
        return {"conflict": True, "saved": False, "path": path, "modified": epoch(meta.get("modifiedTimeStamp")),
                "modified_by": meta.get("modifiedBy")}
    if action == "create":
        status, body, headers = _http("POST", f"/files/files?parentFolderUri=/folders/folders/{folder['id']}", body=data,
                                      content_type=media_type(name), headers={"Content-Disposition": content_disposition(name)})
        if status == 409:
            return {"exists": True, "saved": False, "path": path}
        if status not in (200, 201):
            raise _fail(status, body, f"save {path}")
        return {"saved": True, "created": True, "version": headers.get("etag"), "path": path}
    status, body, headers = _http("PUT", member["uri"] + "/content", body=data,  # type: ignore[index]
                                  content_type=meta.get("contentType") or media_type(name),  # keeps its media type
                                  headers={"If-Match": current if overwrite else expected})
    if status == 412:
        return {"conflict": True, "saved": False, "path": path}
    if status not in (200, 201, 204):
        raise _fail(status, body, f"save {path}")
    return {"saved": True, "created": False, "version": headers.get("etag"), "path": path}


def content_mkdir(parent: str, name: str) -> dict[str, Any]:
    folder = _folder(parent)
    check_name(name)
    status, body, _ = _http("POST", f"/folders/folders?parentFolderUri=/folders/folders/{folder['id']}",
                            body={"name": name, "type": "folder"}, content_type=_FOLDER_JSON,
                            accept=f"{_FOLDER_JSON}, application/json")
    if status == 409:
        raise ValueError(f"{name} already exists in {norm_path(parent)}.")
    if status not in (200, 201):
        raise _fail(status, body, f"create the folder {name}")
    return {"path": posixpath.join(norm_path(parent), name)}


# ------------------------------------------------------------------ SAS Server (Compute file system)


_session_info: dict[str, dict[str, Any]] = {}  # session id -> {"home", "lockdown"}


def _sid() -> str:
    """The Workspace's Compute session (reused; started when there's none). Doesn't wait for a running job."""
    c = sas_rest.compute
    s = sas_viya.settings()
    sid, key = c.sid, c.key
    if sid and key == (s["url"], s["compute_context"]):
        return sid
    return c.session()


def _srv(method: str, path: str, suffix: str = "", **kw: Any) -> tuple[int, Any, dict[str, str]]:
    """A Compute file request for ``path``; an ended session is started again once."""
    for attempt in range(2):
        sid = _sid()
        status, body, headers = _http(method, f"/compute/sessions/{sid}/files/{server_segment(path) if path != '~~' else '~~'}{suffix}", **kw)
        if sas_rest.session_gone(status, body) and not attempt:
            with sas_rest.compute.lock:
                if sas_rest.compute.sid == sid:
                    sas_rest.compute.gone()
            continue
        return status, body, headers
    raise SasError("The SAS session ended and couldn't be restarted.")


def _server_info() -> dict[str, Any]:
    """Home directory and lockdown paths (the folders SAS code may use) of the Compute session."""
    sid = _sid()
    if sid in _session_info:
        return _session_info[sid]
    info: dict[str, Any] = {"home": None, "lockdown": []}
    status, body, _ = _srv("GET", "~~", accept=f"{_FILE_PROPS}, application/json")
    if status == 200 and isinstance(body, dict):
        info["home"] = norm_path(posixpath.join(body.get("path") or "/", body.get("name") or ""))
    status, body, _ = _http("GET", f"/compute/sessions/{sid}")
    if status == 200 and isinstance(body, dict):
        lines = (body.get("environment") or {}).get("autoExecLines") or []
        info["lockdown"] = [m.group(2) for line in lines for m in _LOCKDOWN.finditer(str(line))]
    _session_info.clear()
    _session_info[sid] = info
    return info


def server_browse(path: str | None, show_all: bool = False, kind: str = "sql") -> dict[str, Any]:
    info = _server_info()
    shortcuts: list[dict[str, str]] = []
    for label, p in [("Home", info["home"]), *[(posixpath.basename(x.rstrip("/")) or x, x) for x in info["lockdown"]],
                     ("Root (/)", "/")]:
        if p and norm_path(p) not in {s["path"] for s in shortcuts}:
            shortcuts.append({"label": label, "path": norm_path(p)})
    start = norm_path(info["lockdown"][0]) if info["lockdown"] else (info["home"] or "/")
    path = norm_path(path) if path else start
    status, body, _ = _srv("GET", path, "/members?limit=10000", accept=_COLLECTION)
    if status == 404:
        raise LookupError(f"{path} isn't a folder the SAS session can read.")
    if status != 200:
        raise _fail(status, body, f"list {path}")
    folders, files = [], []
    for i in _items(body):
        name = str(i.get("name") or "")
        if not name or name.startswith("."):
            continue
        full = posixpath.join(path, name)
        if i.get("isDirectory"):
            folders.append({"name": name, "path": full, "read_only": bool(i.get("readOnly"))})
        elif wanted(name, show_all, kind):
            files.append({"name": name, "path": full, "size": i.get("size"), "modified": epoch(i.get("modifiedTimeStamp")),
                          "read_only": bool(i.get("readOnly"))})
    folders.sort(key=lambda f: f["name"].lower())
    files.sort(key=lambda f: f["modified"] or 0, reverse=True)
    return {"path": path, "parent": None if path == "/" else posixpath.dirname(path) or "/",
            "folders": folders[:MAX_LISTED], "files": files[:MAX_LISTED], "shortcuts": shortcuts}


def _server_props(path: str) -> tuple[dict[str, Any] | None, str | None]:
    status, body, headers = _srv("GET", path, accept=f"{_FILE_PROPS}, application/json")
    if status == 404:
        return None, None
    if status != 200 or not isinstance(body, dict):
        raise _fail(status, body, f"read {path}")
    return body, headers.get("etag")


def server_read(path: str) -> dict[str, Any]:
    path = norm_path(path)
    props, _ = _server_props(path)
    if props is None:
        raise LookupError(f"{path} doesn't exist on the SAS server (or the SAS session can't read it).")
    if props.get("isDirectory"):
        raise ValueError(f"{path} is a folder.")
    if (props.get("size") or 0) > max_bytes(path):
        raise ValueError(f"{path} is larger than {max_bytes(path) // 1_000_000} MB.")
    status, raw, headers = _srv("GET", path, "/content", accept="*/*", raw=True)
    if status != 200:
        raise _fail(status, raw, f"read {path}")
    return {"text": decode(raw), "version": headers.get("etag"), "path": path}


def server_write(path: str, text: str, expected: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    path = norm_path(path)
    data = text.encode("utf-8")
    if len(data) > max_bytes(path):
        raise ValueError(f"The text is larger than {max_bytes(path) // 1_000_000} MB.")
    split(path)
    props, current = _server_props(path)
    if props is not None and props.get("isDirectory"):
        raise ValueError(f"{path} is a folder.")
    action = decide_write(props is not None, current, expected, overwrite)
    if action == "exists":
        return {"exists": True, "saved": False, "path": path}
    if action == "conflict":
        return {"conflict": True, "saved": False, "path": path, "modified": epoch((props or {}).get("modifiedTimeStamp"))}
    headers = {} if action == "create" else {"If-Match": current if overwrite else expected}
    status, body, rh = _srv("PUT", path, "/content", body=data, content_type="application/octet-stream",
                            accept=f"{_FILE_PROPS}, application/json", headers=headers)
    if status == 412 or (status == 428 and action == "create"):  # 428: created by someone else meanwhile
        return {"conflict": True, "saved": False, "path": path}
    if status == 404:
        raise LookupError(f"The folder {posixpath.dirname(path)} doesn't exist on the SAS server.")
    if status not in (200, 201, 204):
        raise _fail(status, body, f"save {path}")
    return {"saved": True, "created": action == "create", "version": rh.get("etag"), "path": path}


def server_mkdir(parent: str, name: str) -> dict[str, Any]:
    check_name(name)
    status, body, _ = _srv("POST", parent, body={"name": name, "isDirectory": True}, content_type=_FILE_PROPS,
                           accept=f"{_FILE_PROPS}, application/json")
    if status == 409:
        raise ValueError(f"{name} already exists in {norm_path(parent)}.")
    if status not in (200, 201):
        raise _fail(status, body, f"create the folder {name}")
    return {"path": posixpath.join(norm_path(parent), name)}


def server_delete(path: str) -> bool:
    """Delete one file or empty folder without the file browser's checks (the tests' cleanup)."""
    props, etag = _server_props(path)
    if props is None:
        return False
    status, body, _ = _srv("DELETE", path, headers={"If-Match": etag or "*"})
    if status not in (200, 204):
        raise _fail(status, body, f"delete {path}")
    return True


# ------------------------------------------------------------------ rename and delete (the file browser)
# Only one item at a time, by its path in the folder the browser shows: a rename keeps the item in its folder (the
# new name is a plain name), a folder is only deleted when it's empty, and top-level folders, My Folder, the SAS
# server's home and lockdown folders (and the folders above them) are never renamed or deleted. Each change reads
# the item's version first and sends it as If-Match, so an item changed meanwhile isn't touched (412 ->
# ``conflict``); ``expected_modified`` (the modified time the browser listed) also catches a file changed between
# listing and clicking.


class Protected(ValueError):
    """An item the file browser never renames or deletes."""


def changed_since(listed: float | None, current: float | None) -> bool:
    """The item's modified time differs from the one the browser listed (both epoch seconds)."""
    return listed is not None and current is not None and abs(float(listed) - float(current)) >= 1


def check_target(path: str, protected: list[str]) -> str:
    """The path to rename / delete: not the root, not a top-level item, not a protected folder or one above it."""
    path = norm_path(path)
    parent, _ = split(path)
    if parent == "/":
        raise Protected(f"{path} is a top-level item: it can't be renamed or deleted here.")
    for p in protected:
        p = norm_path(p)
        if path == p or p.startswith(path + "/"):
            raise Protected(f"{path} can't be renamed or deleted here (it is or holds {p}).")
    return path


def new_sibling(path: str, new_name: str) -> str:
    """The renamed path: same folder, a plain new name (a file keeps its extension when the new name has none)."""
    parent, name = split(path)
    new_name = check_name((new_name or "").strip())
    if not name.startswith(".") and "." in name.strip(".") and "." not in new_name.strip("."):
        new_name += posixpath.splitext(name)[1]
    return posixpath.join(parent, new_name)


def _content_protected() -> list[str]:
    my = _my_folder()
    return [my] if my else []


def _content_item(path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    folder, member = _content_member(path)
    if member is None:
        raise LookupError(f"{path} doesn't exist in SAS Content (refresh the list).")
    if not (_is_file(member) or _is_folder(member)):
        raise ValueError(f"{path} is a {member.get('contentType') or 'item'} (a report or another kind of SAS Content "
                         "item): only files and folders can be renamed or deleted here.")
    return folder, member


def _content_meta(member: dict[str, Any], path: str) -> tuple[dict[str, Any], str | None]:
    accept = f"{_FOLDER_JSON}, application/json" if _is_folder(member) else f"{_FILE_JSON}, application/json"
    status, meta, headers = _http("GET", member["uri"], accept=accept)
    if status != 200 or not isinstance(meta, dict):
        raise _fail(status, meta, f"read {path}")
    return meta, headers.get("etag")


def content_rename(path: str, new_name: str, expected_modified: float | None = None) -> dict[str, Any]:
    path = check_target(path, _content_protected())
    new = new_sibling(path, new_name)
    folder, member = _content_item(path)
    is_folder = _is_folder(member)
    if new == path:
        return {"renamed": False, "unchanged": True, "path": path, "new_path": new, "folder": is_folder}
    if any(m.get("name") == posixpath.basename(new) for m in _members(folder["id"])):
        return {"renamed": False, "exists": True, "path": path, "new_path": new, "folder": is_folder}
    meta, etag = _content_meta(member, path)
    if not is_folder and changed_since(expected_modified, epoch(meta.get("modifiedTimeStamp"))):
        return {"renamed": False, "conflict": True, "path": path, "folder": False,
                "modified": epoch(meta.get("modifiedTimeStamp")), "modified_by": meta.get("modifiedBy")}
    body = {k: v for k, v in meta.items() if k != "links"}
    body["name"] = posixpath.basename(new)
    ctype = _FOLDER_JSON if is_folder else _FILE_JSON
    status, out, rh = _http("PUT", member["uri"], body=body, content_type=ctype, accept=f"{ctype}, application/json",
                            headers={"If-Match": etag or "*"})
    if status == 412:
        return {"renamed": False, "conflict": True, "path": path, "folder": is_folder}
    if status == 409:
        return {"renamed": False, "exists": True, "path": path, "new_path": new, "folder": is_folder}
    if status not in (200, 204):
        raise _fail(status, out, f"rename {path}")
    # A file's version (ETag) changes with the rename: an editor that had the old one gets the new one.
    versions = {} if is_folder else {"old_version": etag, "version": rh.get("etag")}
    return {"renamed": True, "path": path, "new_path": new, "folder": is_folder, **versions}


def content_delete(path: str, expected_modified: float | None = None) -> dict[str, Any]:
    path = check_target(path, _content_protected())
    _, member = _content_item(path)
    is_folder = _is_folder(member)
    meta, etag = _content_meta(member, path)
    if is_folder:
        inside = _members(member["uri"].rsplit("/", 1)[-1])
        if inside:
            return {"deleted": False, "not_empty": True, "path": path, "folder": True, "items": len(inside)}
    elif changed_since(expected_modified, epoch(meta.get("modifiedTimeStamp"))):
        return {"deleted": False, "conflict": True, "path": path, "folder": False,
                "modified": epoch(meta.get("modifiedTimeStamp")), "modified_by": meta.get("modifiedBy")}
    status, body, _ = _http("DELETE", member["uri"], headers={"If-Match": etag} if etag else None)
    if status == 412:
        return {"deleted": False, "conflict": True, "path": path, "folder": is_folder}
    if status == 409 and is_folder:
        return {"deleted": False, "not_empty": True, "path": path, "folder": True}
    if status not in (200, 204):
        raise _fail(status, body, f"delete {path}")
    return {"deleted": True, "path": path, "folder": is_folder}


def _server_protected() -> list[str]:
    info = _server_info()
    return [p for p in [info.get("home"), *info.get("lockdown", [])] if p]


def server_rename(path: str, new_name: str, expected_modified: float | None = None) -> dict[str, Any]:
    path = check_target(path, _server_protected())
    new = new_sibling(path, new_name)
    props, etag = _server_props(path)
    if props is None:
        raise LookupError(f"{path} doesn't exist on the SAS server (refresh the list).")
    is_folder = bool(props.get("isDirectory"))
    if new == path:
        return {"renamed": False, "unchanged": True, "path": path, "new_path": new, "folder": is_folder}
    if _server_props(new)[0] is not None:
        return {"renamed": False, "exists": True, "path": path, "new_path": new, "folder": is_folder}
    if not is_folder and changed_since(expected_modified, epoch(props.get("modifiedTimeStamp"))):
        return {"renamed": False, "conflict": True, "path": path, "folder": False, "modified": epoch(props.get("modifiedTimeStamp"))}
    body = {"name": posixpath.basename(new), "path": posixpath.dirname(path)}
    status, out, rh = _srv("PUT", path, body=body, content_type=_FILE_PROPS, accept=f"{_FILE_PROPS}, application/json",
                           headers={"If-Match": etag or "*"})
    if status == 412:
        return {"renamed": False, "conflict": True, "path": path, "folder": is_folder}
    if status not in (200, 201, 204):
        raise _fail(status, out, f"rename {path}")
    versions = {} if is_folder else {"old_version": etag, "version": rh.get("etag")}
    return {"renamed": True, "path": path, "new_path": new, "folder": is_folder, **versions}


def server_remove(path: str, expected_modified: float | None = None) -> dict[str, Any]:
    path = check_target(path, _server_protected())
    props, etag = _server_props(path)
    if props is None:
        raise LookupError(f"{path} doesn't exist on the SAS server (refresh the list).")
    is_folder = bool(props.get("isDirectory"))
    if is_folder:
        status, body, _ = _srv("GET", path, "/members?limit=1", accept=_COLLECTION)
        if status != 200:
            raise _fail(status, body, f"list {path}")
        if _items(body):  # hidden (dot) files count too
            return {"deleted": False, "not_empty": True, "path": path, "folder": True}
    elif changed_since(expected_modified, epoch(props.get("modifiedTimeStamp"))):
        return {"deleted": False, "conflict": True, "path": path, "folder": False, "modified": epoch(props.get("modifiedTimeStamp"))}
    status, body, _ = _srv("DELETE", path, headers={"If-Match": etag or "*"})
    if status == 412:
        return {"deleted": False, "conflict": True, "path": path, "folder": is_folder}
    if status not in (200, 204):
        raise _fail(status, body, f"delete {path}")
    return {"deleted": True, "path": path, "folder": is_folder}


# ------------------------------------------------------------------ the editor's entry points


def browse(location: str, path: str | None = None, show_all: bool = False, kind: str = "sql") -> dict[str, Any]:
    if location not in LOCATIONS:
        raise ValueError(f"location must be one of {', '.join(LOCATIONS)}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    conn = active_connection()
    data = (content_browse if location == "content" else server_browse)(path, show_all, kind)
    prefix = _SCHEMES[location] + conn
    for f in data["files"]:
        f["ref"] = prefix + f["path"]
    for f in data["folders"]:
        f["ref"] = prefix + f["path"]
    return {**data, "location": location, "label": LABELS[location], "connection": conn, "ref_prefix": prefix,
            "show_all": show_all, "kind": kind, "manage": True}


def mkdir(location: str, parent: str, name: str) -> dict[str, Any]:
    active_connection()
    name = check_name((name or "").strip())
    out = (content_mkdir if location == "content" else server_mkdir)(parent, name)
    return {**out, "location": location}


def read(ref: str) -> dict[str, Any]:
    loc, conn, path = parse_ref(ref)
    check_connection(conn)
    out = (content_read if loc == "content" else server_read)(path)
    return {**out, "ref": ref, "name": posixpath.basename(path), "location": loc, "connection": conn,
            "display": display(ref)}


def write(ref: str, text: str, *, expected: str | None = None, overwrite: bool = False,
          use_active: bool = False, suffix: str = ".sql") -> dict[str, Any]:
    """Save to a ref. ``use_active``: save a file of another connection to the same path on the active one (it's
    a different file there, so the version opened elsewhere doesn't apply)."""
    loc, conn, path = parse_ref(ref)
    try:
        active = check_connection(conn, use_active)
    except ConnectionMismatch as exc:
        return {"connection_mismatch": True, "saved": False, "file_connection": exc.file_connection,
                "active": exc.active, "message": str(exc)}
    if active != conn:
        expected = None
    path = with_suffix(path, suffix)
    ref = make_ref(loc, active, path)
    out = (content_write if loc == "content" else server_write)(path, text, expected, overwrite)
    return {**out, "ref": make_ref(loc, active, out.get("path", path)), "name": posixpath.basename(path),
            "location": loc, "connection": active, "display": display(ref)}


def rename(location: str, path: str, new_name: str, expected_modified: float | None = None) -> dict[str, Any]:
    """Rename a file or folder in its folder on the active connection (``check_target`` says what's refused).
    ``from_ref`` / ``to_ref``: the refs before and after, for the editors that have the file (or a file in the
    folder) open."""
    if location not in LOCATIONS:
        raise ValueError(f"location must be one of {', '.join(LOCATIONS)}")
    conn = active_connection()
    out = (content_rename if location == "content" else server_rename)(path, new_name, expected_modified)
    prefix = _SCHEMES[location] + conn
    return {**out, "location": location, "connection": conn, "from_ref": prefix + out["path"],
            "to_ref": prefix + out["new_path"] if out.get("new_path") else None}


def remove(location: str, path: str, expected_modified: float | None = None) -> dict[str, Any]:
    """Delete one file, or one empty folder, on the active connection."""
    if location not in LOCATIONS:
        raise ValueError(f"location must be one of {', '.join(LOCATIONS)}")
    conn = active_connection()
    out = (content_delete if location == "content" else server_remove)(path, expected_modified)
    return {**out, "location": location, "connection": conn, "from_ref": _SCHEMES[location] + conn + out["path"]}
