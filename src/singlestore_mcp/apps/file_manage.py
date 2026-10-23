"""Rename and delete in the file browser (``S2.fileBrowser``) of the SQL Editor, the Notebook and the Security
script basket: on this computer (``files_rename`` / ``files_delete``) and on SAS Viya (``viya_files_rename`` /
``viya_files_delete``, see ``singlestore_mcp.viya_files``).

One item at a time, in the folder the browser shows: a rename keeps the item in its folder (the new name is a
plain name), only files of the kind the browser lists (.sql / .sas, or .ipynb) and folders are touched, a folder is
only deleted when it's empty, and the browser's roots and shortcut folders (user folder, SQL folder, Documents …;
My Folder; the SAS server's home and lockdown folders) and the folders above them are never renamed or deleted.
``expected_modified`` (the modified time the browser listed) refuses a file that changed since (``conflict``).

The SQL Editor tabs that have a renamed file open follow it to the new path, and a deleted file's tab keeps its
text as unsaved (``sql_editor_state.files_changed``, also for tabs not loaded right now); the browser tells the open
editors through the workspace (``s2-files-changed``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import sql_editor_state as editor_state
from .. import viya_files
from ._core import APP_ONLY, apps, tool_result
from .sql_editor import URI, _check_allowed, _hidden, _known_folders, _relative, file_roots, sql_dir

CHANGES = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
KIND_SUFFIXES = {"sql": (".sql", ".sas"), "notebook": (".ipynb",)}


def _protected_local() -> list[Path]:
    configured = [Path(r).expanduser() for r in os.environ.get("SINGLESTORE_MCP_FILE_ROOTS", "").split(os.pathsep) if r.strip()]
    # file_roots() drops a root inside another one; each configured root stays protected all the same.
    paths = [*file_roots(), *configured, sql_dir(), *(Path(f["path"]) for f in _known_folders())]
    out = []
    for p in paths:
        try:
            out.append(p.resolve())
        except OSError:
            continue
    return out


def _local_target(path: str) -> Path:
    if not path or not str(path).strip():
        raise ValueError("Give the path of the file or folder.")
    target = _check_allowed(Path(path).expanduser())
    for p in _protected_local():
        if target == p or p.is_relative_to(target):
            raise viya_files.Protected(f"{target} can't be renamed or deleted here (it is or holds {p}).")
    if not target.exists():
        raise LookupError(f"{target} doesn't exist (refresh the list).")
    if _hidden(target):
        raise viya_files.Protected(f"{target} is hidden: it can't be renamed or deleted here.")
    return target


def _check_kind(target: Path, kind: str) -> tuple[str, ...]:
    suffixes = KIND_SUFFIXES.get(kind)
    if suffixes is None:
        raise ValueError(f"kind must be one of {', '.join(KIND_SUFFIXES)}")
    if target.is_file() and target.suffix.lower() not in suffixes:
        raise viya_files.Protected(f"Only {' / '.join(suffixes)} files can be renamed or deleted here.")
    return suffixes


def _changed(target: Path, expected_modified: float | None) -> bool:
    return target.is_file() and viya_files.changed_since(expected_modified, round(target.stat().st_mtime))


def local_rename(path: str, new_name: str, kind: str = "sql", expected_modified: float | None = None) -> dict[str, Any]:
    target = _local_target(path)
    suffixes = _check_kind(target, kind)
    is_folder = target.is_dir()
    new_name = viya_files.check_name((new_name or "").strip())
    if new_name.endswith((".", " ")) or any(c in new_name for c in '<>:"|?*'):
        raise ValueError(f"Not a valid file or folder name: {new_name!r}")
    if not is_folder:
        if "." not in new_name.strip("."):
            new_name += target.suffix
        elif Path(new_name).suffix.lower() not in suffixes:
            raise ValueError(f"Keep a {' / '.join(suffixes)} extension (the browser only lists those files).")
    new = target.with_name(new_name)
    base = {"path": str(target), "new_path": str(new), "folder": is_folder, "from_ref": str(target), "to_ref": str(new)}
    if new == target:
        return {**base, "renamed": False, "unchanged": True}
    # A case-only rename on Windows is the same file: os.rename handles it.
    if new.exists() and os.path.normcase(str(new)) != os.path.normcase(str(target)):
        return {**base, "renamed": False, "exists": True}
    if _changed(target, expected_modified):
        return {**base, "renamed": False, "conflict": True, "modified": round(target.stat().st_mtime)}
    target.rename(new)
    return {**base, "renamed": True}


def local_delete(path: str, kind: str = "sql", expected_modified: float | None = None) -> dict[str, Any]:
    target = _local_target(path)
    _check_kind(target, kind)
    is_folder = target.is_dir()
    base = {"path": str(target), "folder": is_folder, "from_ref": str(target)}
    if is_folder:
        if any(target.iterdir()):
            return {**base, "deleted": False, "not_empty": True}
        target.rmdir()
        return {**base, "deleted": True}
    if _changed(target, expected_modified):
        return {**base, "deleted": False, "conflict": True, "modified": round(target.stat().st_mtime)}
    target.unlink()
    return {**base, "deleted": True}


def _follow(res: dict[str, Any], local: bool) -> dict[str, Any]:
    """Point the SQL Editor tabs (and Recent) that have the item open at its new path, or mark them unsaved."""
    done = res.get("renamed") or res.get("deleted")
    if done:
        to = res.get("to_ref") if res.get("renamed") else None
        name_of = (lambda p: _relative(Path(p))) if local else None
        versions = (res.get("old_version"), res.get("version")) if res.get("version") else None
        res["tabs"] = editor_state.files_changed(res["from_ref"], to, folder=bool(res.get("folder")), name_of=name_of,
                                                 versions=versions)
    return res


def _summary(res: dict[str, Any], verb: str) -> str:
    what = "folder" if res.get("folder") else "file"
    if res.get("renamed"):
        return f"Renamed {what} {res['path']} to {res['new_path']}"
    if res.get("deleted"):
        return f"Deleted {what} {res['path']}"
    if res.get("exists"):
        return f"{res.get('new_path')} already exists; nothing {verb}."
    if res.get("conflict"):
        return f"{res['path']} changed since the list was read; nothing {verb} (refresh and try again)."
    if res.get("not_empty"):
        return f"{res['path']} isn't empty; only empty folders are deleted."
    return f"Nothing {verb}."


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def files_rename(path: str, new_name: str, kind: str = "sql", expected_modified: float | None = None) -> CallToolResult:
    """Rename a file or folder on this computer in its folder (the file browser's Rename).

    ``path``: the item's full path (inside the folders the browser may use; never a root or shortcut folder);
    ``new_name``: a plain name (a file without an extension keeps its own); ``kind``: "sql" (.sql / .sas) or
    "notebook" (.ipynb), the only files touched; ``expected_modified``: the modified time the browser listed
    (``conflict`` if it changed). ``exists`` when the new name is taken. Open SQL Editor tabs follow the rename.
    """
    res = _follow(local_rename(path, new_name, kind, expected_modified), local=True)
    return tool_result(_summary(res, "renamed"), res)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def files_delete(path: str, kind: str = "sql", expected_modified: float | None = None) -> CallToolResult:
    """Delete one file, or one empty folder, on this computer (the file browser's Delete; the app confirms first).

    Same limits as files_rename; a folder that isn't empty answers ``not_empty`` and nothing is deleted. SQL Editor
    tabs that had the file open keep its text as unsaved.
    """
    res = _follow(local_delete(path, kind, expected_modified), local=True)
    return tool_result(_summary(res, "deleted"), res)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def viya_files_rename(location: str, path: str, new_name: str, expected_modified: float | None = None) -> CallToolResult:
    """Rename a file or folder on SAS Viya ("content" or "server", the active connection) in its folder.

    Sends the item's version (ETag) as If-Match: a change meanwhile answers ``conflict``, as does a file whose
    modified time differs from ``expected_modified``. Top-level folders, My Folder, the SAS server's home and
    lockdown folders (and the folders above them) are refused. Open SQL Editor tabs follow the rename.
    """
    res = _follow(viya_files.rename(location, path, new_name, expected_modified), local=False)
    return tool_result(_summary(res, "renamed"), res)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def viya_files_delete(location: str, path: str, expected_modified: float | None = None) -> CallToolResult:
    """Delete one file, or one empty folder, on SAS Viya ("content" or "server"); the app confirms first.

    Same checks as viya_files_rename (version / If-Match, protected folders); a folder that isn't empty answers
    ``not_empty``. SQL Editor tabs that had the file open keep its text as unsaved.
    """
    res = _follow(viya_files.remove(location, path, expected_modified), local=False)
    return tool_result(_summary(res, "deleted"), res)
