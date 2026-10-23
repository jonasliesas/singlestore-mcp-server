"""SQL Editor files on SAS Viya (singlestore_mcp.viya_files) without SAS Viya: the location model (refs), path
handling, the write decision (exists / conflict) and the SQL Editor's open / save tools against a fake Viya that
answers the Folders, Files and Compute file APIs the way the real one does (ETag, If-Match → 412/428)."""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp import sas_viya
from singlestore_mcp import sql_editor_state as st
from singlestore_mcp import viya_files as vf
from singlestore_mcp.apps import sql_editor

# ------------------------------------------------------------------ refs and paths


def test_refs_round_trip() -> None:
    ref = vf.make_ref("content", "Zeus", "/Users/me/My Folder/a b.sql")
    assert ref == "sas-content://Zeus/Users/me/My Folder/a b.sql"
    assert vf.parse_ref(ref) == ("content", "Zeus", "/Users/me/My Folder/a b.sql")
    assert vf.parse_ref("sas-server://My Viya (test)/tmp/x.sas") == ("server", "My Viya (test)", "/tmp/x.sas")
    assert vf.is_ref(ref) and not vf.is_ref("C:\\Users\\me\\a.sql") and not vf.is_ref(None)
    assert vf.display(ref) == "SAS Content (Zeus): /Users/me/My Folder/a b.sql"
    for bad in ("sas-content://", "sas-content://Zeus", "file:///x.sql", "C:/x.sql"):
        with pytest.raises(ValueError):
            vf.parse_ref(bad)
    with pytest.raises(ValueError):
        vf.make_ref("local", "Zeus", "/x.sql")


def test_path_handling() -> None:
    assert vf.norm_path("/a//b/./c/../d.sql") == "/a/b/d.sql"
    assert vf.norm_path("/") == "/" and vf.norm_path("/a/b/") == "/a/b"
    assert vf.norm_path("\\srv\\x.sas") == "/srv/x.sas"
    with pytest.raises(ValueError):
        vf.norm_path("relative/x.sql")
    assert vf.split("/a/b.sql") == ("/a", "b.sql") and vf.split("/b.sql") == ("/", "b.sql")
    with pytest.raises(ValueError):
        vf.split("/")
    assert vf.with_suffix("/a/query") == "/a/query.sql"
    assert vf.with_suffix("/a/prog.sas") == "/a/prog.sas" and vf.with_suffix("/a/notes.txt") == "/a/notes.txt"
    for bad in ("", "..", "a/b", "a\\b", "x\ny"):
        with pytest.raises(ValueError):
            vf.check_name(bad)
    # The Compute file API: ~fs~ for each slash, every name percent-encoded, ~ too.
    assert vf.server_segment("/") == "~fs~"
    assert vf.server_segment("/tmp/a b/x~1#.sql") == "~fs~tmp~fs~a%20b~fs~x%7E1%23.sql"
    assert vf.content_disposition("łódź ø.sas") == "attachment; filename*=UTF-8''%C5%82%C3%B3d%C5%BA%20%C3%B8.sas"
    assert vf.media_type("p.SAS") == "application/vnd.sas.studio.programfile" and vf.media_type("q.sql") == "text/plain"
    assert vf.wanted("a.SQL", False) and vf.wanted("b.sas", False) and not vf.wanted("c.txt", False) and vf.wanted("c.txt", True)
    assert vf.epoch("2026-10-15T11:14:55.678Z") == 1792062896 and vf.epoch(None) is None and vf.epoch("nope") is None
    assert vf.decode("æ".encode()) == "æ" and vf.decode("\ufeffx".encode()) == "x" and vf.decode("æ".encode("cp1252")) == "æ"


@pytest.mark.parametrize(("exists", "current", "expected", "overwrite", "action"), [
    (False, None, None, False, "create"),
    (False, None, '"v1"', False, "create"),       # deleted on the server meanwhile: saved again
    (True, '"v1"', None, False, "exists"),        # Save as onto an existing file: ask
    (True, '"v1"', '"v1"', False, "write"),       # Save of the file the editor opened, unchanged on the server
    (True, '"v2"', '"v1"', False, "conflict"),    # changed on the server since: ask
    (True, None, '"v1"', False, "conflict"),
    (True, '"v2"', '"v1"', True, "write"),        # the user said overwrite
])
def test_write_decision(exists: bool, current: str | None, expected: str | None, overwrite: bool, action: str) -> None:
    assert vf.decide_write(exists, current, expected, overwrite) == action


# ------------------------------------------------------------------ a fake SAS Viya


class FakeViya:
    """The REST answers viya_files uses, from an in-memory SAS Content tree and file system."""

    def __init__(self) -> None:
        self.n = 0
        self.folders: dict[str, dict[str, Any]] = {}   # id -> {name, parent, type}
        self.files: dict[str, dict[str, Any]] = {}     # id -> {name, parent, data, ctype, etag, modified}
        self.fs: dict[str, dict[str, Any]] = {"/": {"dir": True}}  # server path -> {dir, data, etag}
        self.calls: list[tuple[str, str]] = []
        root = self._folder("Users", None)
        me = self._folder("me", root, "userFolder")
        self.my = self._folder("My Folder", me, "myFolder")
        self.public = self._folder("Public", None)
        self.secret = self._folder("locked", self.public)
        for d in ("/srv", "/srv/landing", "/tmp"):
            self.fs[d] = {"dir": True}

    def _id(self) -> str:
        self.n += 1
        return f"id{self.n:04d}"

    def _etag(self) -> str:
        self.n += 1
        return f'"e{self.n}"'

    def _folder(self, name: str, parent: str | None, kind: str = "folder") -> str:
        fid = self._id()
        self.folders[fid] = {"name": name, "parent": parent, "type": kind}
        return fid

    def folder_path(self, fid: str) -> str:
        names = []
        while fid:
            names.append(self.folders[fid]["name"])
            fid = self.folders[fid]["parent"]
        return "/" + "/".join(reversed(names))

    def add_file(self, folder: str, name: str, text: str, ctype: str = "text/plain") -> str:
        fid = self._id()
        self.files[fid] = {"name": name, "parent": folder, "data": text.encode(), "ctype": ctype, "etag": self._etag(),
                           "modified": "2026-10-01T10:00:00Z", "by": "me"}
        return fid

    def touch(self, fid: str, text: str, by: str = "someone") -> None:
        self.files[fid].update(data=text.encode(), etag=self._etag(), by=by)

    # -- dispatch

    def __call__(self, method: str, path: str, *, body: Any = None, headers: dict[str, str] | None = None,
                 content_type: str = "application/json", raw: bool = False, **_: Any) -> tuple[int, Any, dict[str, str]]:
        self.calls.append((method, path))
        url = urllib.parse.urlsplit(path)
        q = urllib.parse.parse_qs(url.query)
        p = url.path
        headers = headers or {}
        if p.startswith("/compute/sessions/S1/files/"):
            return self._compute(method, p[len("/compute/sessions/S1/files/"):], body, headers, raw)
        if p == "/compute/sessions/S1":
            return 200, {"id": "S1", "environment": {"autoExecLines": ["lockdown path='/srv/landing';"]}}, {}
        if p == "/folders/folders/@myFolder":
            return 200, {"id": self.my, "name": "My Folder", "parentFolderUri": f"/folders/folders/{self.folders[self.my]['parent']}"}, {}
        if p == "/folders/ancestors":
            fid = q["childUri"][0].rsplit("/", 1)[-1]
            out, cur = [], self.folders[fid]["parent"]
            while cur:
                par = self.folders[cur]["parent"]
                out.append({"id": cur, "name": self.folders[cur]["name"], "parentFolderUri": f"/folders/folders/{par}" if par else None})
                cur = par
            return 200, out, {}
        if p == "/folders/folders/@item":
            want = q["path"][0]
            hit = next((f for f in self.folders if self.folder_path(f) == want), None)
            return (200, {"id": hit, "name": self.folders[hit]["name"]}, {}) if hit else (404, {"message": "nope"}, {})
        if p == "/folders/rootFolders":
            return 200, {"items": [{"id": f, "name": v["name"]} for f, v in self.folders.items() if v["parent"] is None]}, {}
        m = re.fullmatch(r"/folders/folders/(\w+)/members", p)
        if m:
            fid = m.group(1)
            if fid == self.secret:
                return 403, {"message": "forbidden"}, {}
            items = [{"name": v["name"], "uri": f"/folders/folders/{k}", "contentType": "folder"}
                     for k, v in self.folders.items() if v["parent"] == fid]
            items += [{"name": v["name"], "uri": f"/files/files/{k}", "contentType": "file"} for k, v in self.files.items() if v["parent"] == fid]
            return 200, {"items": items}, {}
        if p == "/folders/folders" and method == "POST":
            parent = q["parentFolderUri"][0].rsplit("/", 1)[-1]
            if any(v["parent"] == parent and v["name"] == body["name"] for v in self.folders.values()):
                return 409, {"message": "exists"}, {}
            return 201, {"id": self._folder(body["name"], parent)}, {}
        if p == "/files/files" and method == "GET":
            ids = re.findall(r"'(\w+)'", q["filter"][0])
            return 200, {"items": [{"id": i, "size": len(self.files[i]["data"]), "contentType": self.files[i]["ctype"],
                                    "modifiedTimeStamp": self.files[i]["modified"]} for i in ids if i in self.files]}, {}
        if p == "/files/files" and method == "POST":
            parent = q["parentFolderUri"][0].rsplit("/", 1)[-1]
            name = urllib.parse.unquote(headers["Content-Disposition"].split("''", 1)[1])
            if any(v["parent"] == parent and v["name"] == name for v in self.files.values()):
                return 409, {"message": "exists"}, {}
            fid = self.add_file(parent, name, body.decode(), content_type)
            return 201, {"id": fid}, {"etag": self.files[fid]["etag"]}
        m = re.fullmatch(r"/files/files/(\w+)(/content)?", p)
        if m:
            f = self.files.get(m.group(1))
            if f is None:
                return 404, {"message": "gone"}, {}
            if method == "GET" and m.group(2):
                return 200, f["data"] if raw else f["data"].decode(), {"etag": f["etag"]}
            if method == "GET":
                return 200, {"id": m.group(1), "name": f["name"], "size": len(f["data"]), "contentType": f["ctype"], "links": [],
                             "modifiedTimeStamp": f["modified"], "modifiedBy": f["by"]}, {"etag": f["etag"]}
            if method == "PUT" and m.group(2):
                if "If-Match" not in headers:
                    return 428, {"message": "If-Match required"}, {}
                if headers["If-Match"] != f["etag"]:
                    return 412, {"message": "stale"}, {}
                f.update(data=body, ctype=content_type, etag=self._etag())
                return 200, {"name": f["name"]}, {"etag": f["etag"]}
            if method == "PUT":  # metadata (rename): the whole resource back, with If-Match
                if headers.get("If-Match") != f["etag"]:
                    return 412, {"message": "stale"}, {}
                assert body["id"] == m.group(1) and "links" not in body and content_type.startswith("application/vnd.sas.file+json")
                f.update(name=body["name"], etag=self._etag())
                return 200, {"name": f["name"]}, {"etag": f["etag"]}
            if method == "DELETE":
                if headers.get("If-Match") not in (None, f["etag"]):
                    return 412, {"message": "stale"}, {}
                del self.files[m.group(1)]
                return 204, None, {}
        m = re.fullmatch(r"/folders/folders/(\w+)", p)
        if m and m.group(1) in self.folders:
            fid, f = m.group(1), self.folders[m.group(1)]
            etag = f.setdefault("etag", self._etag())
            if method == "GET":
                return 200, {"id": fid, "name": f["name"], "type": f["type"], "links": []}, {"etag": etag}
            if headers.get("If-Match") != etag:
                return 412, {"message": "stale"}, {}
            if method == "PUT":
                f.update(name=body["name"], etag=self._etag())
                return 200, {"id": fid, "name": f["name"]}, {"etag": f["etag"]}
            if method == "DELETE":
                if any(v["parent"] == fid for v in self.folders.values()) or any(v["parent"] == fid for v in self.files.values()):
                    return 409, {"message": "not empty"}, {}
                del self.folders[fid]
                return 204, None, {}
        return 500, {"message": f"fake Viya doesn't know {method} {path}"}, {}

    def _compute(self, method: str, seg: str, body: Any, headers: dict[str, str], raw: bool) -> tuple[int, Any, dict[str, str]]:
        suffix = ""
        for s in ("/members", "/content"):
            if seg.endswith(s):
                seg, suffix = seg[: -len(s)], s
        if seg == "~~":
            return 200, {"isDirectory": True, "name": "", "path": "/"}, {"etag": '"home"'}
        assert seg.startswith("~fs~"), seg
        path = "/" + "/".join(urllib.parse.unquote(x) for x in seg[4:].split("~fs~") if x)
        node = self.fs.get(path)
        if suffix == "/members":
            if not node or not node["dir"]:
                return 404, {"message": "not a directory", "errorCode": 5436}, {}
            kids = [k for k in self.fs if k != path and k.rsplit("/", 1)[0] == ("" if path == "/" else path)]
            return 200, {"items": [{"name": k.rsplit("/", 1)[1], "isDirectory": self.fs[k]["dir"],
                                    "size": len(self.fs[k].get("data", b"")), "modifiedTimeStamp": "2026-10-01T10:00:00Z"}
                                   for k in kids]}, {}
        if suffix == "/content" and method == "GET":
            return (200, node["data"] if raw else node["data"].decode(), {"etag": node["etag"]}) if node else (404, {}, {})
        if suffix == "/content" and method == "PUT":
            if node is None:
                if path.rsplit("/", 1)[0] not in self.fs:
                    return 404, {"message": "no parent"}, {}
                self.fs[path] = {"dir": False, "data": body, "etag": self._etag()}
                return 201, {}, {"etag": self.fs[path]["etag"]}
            if "If-Match" not in headers:
                return 428, {}, {}
            if headers["If-Match"] != node["etag"]:
                return 412, {}, {}
            node.update(data=body, etag=self._etag())
            return 200, {}, {"etag": node["etag"]}
        if method == "PUT" and not suffix:  # rename: {name, path (the parent folder)}, If-Match required
            if node is None:
                return 404, {}, {}
            if headers.get("If-Match") != node.get("etag", '"d"'):
                return 412 if "If-Match" in headers else 428, {}, {}
            new = body["path"].rstrip("/") + "/" + body["name"]
            for k in [k for k in self.fs if k == path or k.startswith(path + "/")]:
                self.fs[new + k[len(path):]] = self.fs.pop(k)
            return 200, {"name": body["name"]}, {"etag": self._etag()}
        if method == "DELETE":
            if node is None:
                return 404, {}, {}
            if headers.get("If-Match") not in ("*", node.get("etag", '"d"')):
                return 412, {}, {}
            del self.fs[path]
            return 204, None, {}
        if method == "POST":
            new = path.rstrip("/") + "/" + body["name"]
            if new in self.fs:
                return 409, {}, {}
            self.fs[new] = {"dir": True}
            return 201, {"isDirectory": True}, {}
        if method == "GET":
            if node is None:
                return 404, {"message": "not available", "errorCode": 5437}, {}
            return 200, {"isDirectory": node["dir"], "size": len(node.get("data", b"")),
                         "modifiedTimeStamp": "2026-10-01T10:00:00Z"}, {"etag": node.get("etag", '"d"')}
        return 500, {}, {}


@pytest.fixture
def viya(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeViya:
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    fake = FakeViya()
    active = {"name": "Zeus"}
    monkeypatch.setattr(vf, "_http", fake)
    monkeypatch.setattr(vf, "_sid", lambda: "S1")
    monkeypatch.setattr(vf, "_session_info", {})
    monkeypatch.setattr(vf, "_my_folders", {})
    monkeypatch.setattr(sas_viya, "active_name", lambda: active["name"])
    monkeypatch.setattr(sas_viya, "settings", lambda name=None: {"name": active["name"], "url": "https://viya.example",
                                                                  "compute_context": "ctx"})
    fake.active = active  # type: ignore[attr-defined]
    return fake


def _data(result: Any) -> dict[str, Any]:
    return result.structured_content


# ------------------------------------------------------------------ SAS Content


def test_content_browse_lists_folders_and_sql_files(viya: FakeViya) -> None:
    viya._folder("sub", viya.my)
    viya.add_file(viya.my, "a.sql", "select 1;")
    viya.add_file(viya.my, "b.SAS", "proc print; run;", "application/vnd.sas.studio.programfile")
    viya.add_file(viya.my, "notes.txt", "x")
    d = vf.browse("content")
    assert d["path"] == "/Users/me/My Folder" and d["parent"] == "/Users/me" and d["connection"] == "Zeus"
    assert [s["label"] for s in d["shortcuts"]] == ["My Folder", "Public", "All folders"]
    assert [f["name"] for f in d["folders"]] == ["sub"]
    assert sorted(f["name"] for f in d["files"]) == ["a.sql", "b.SAS"]
    a = next(f for f in d["files"] if f["name"] == "a.sql")
    assert a["size"] == 9 and a["modified"] and a["ref"] == "sas-content://Zeus/Users/me/My Folder/a.sql"
    assert d["ref_prefix"] + a["path"] == a["ref"]
    assert {f["name"] for f in vf.browse("content", "/Users/me/My Folder", show_all=True)["files"]} == {"a.sql", "b.SAS", "notes.txt"}
    root = vf.browse("content", "/")
    assert {f["name"] for f in root["folders"]} == {"Users", "Public"} and root["parent"] is None
    with pytest.raises(LookupError, match="No SAS Content folder"):
        vf.browse("content", "/Nope")
    with pytest.raises(PermissionError, match="No permission"):
        vf.browse("content", "/Public/locked")
    # My Folder's path is looked up once per connection.
    n = sum(1 for c in viya.calls if c[1].startswith("/folders/folders/@myFolder"))
    vf.browse("content")
    assert sum(1 for c in viya.calls if c[1].startswith("/folders/folders/@myFolder")) == n


def test_content_open_save_conflict_and_media_type(viya: FakeViya) -> None:
    fid = viya.add_file(viya.my, "prog.sas", "proc sql; select 1; quit;", "application/vnd.sas.studio.programfile; charset=UTF-8")
    ref = "sas-content://Zeus/Users/me/My Folder/prog.sas"
    opened = _data(sql_editor.sql_editor_open_file(ref))
    assert opened["sql"] == "proc sql; select 1; quit;" and opened["name"] == "prog.sas" and opened["path"] == ref
    assert opened["version"] == viya.files[fid]["etag"] and opened["location"] == "content"
    assert st.recent_files()[0] == {"path": ref, "name": "prog.sas", "t": st.recent_files()[0]["t"], "location": "content"}

    saved = _data(sql_editor.sql_editor_save_file(ref, "proc sql; select 2; quit;", expected_version=opened["version"]))
    assert saved["saved"] and saved["version"] == viya.files[fid]["etag"] != opened["version"]
    assert viya.files[fid]["data"] == b"proc sql; select 2; quit;"
    assert viya.files[fid]["ctype"] == "application/vnd.sas.studio.programfile; charset=UTF-8"  # media type kept

    viya.touch(fid, "changed elsewhere")  # someone saves a newer version in SAS Studio
    stale = _data(sql_editor.sql_editor_save_file(ref, "mine", expected_version=saved["version"]))
    assert stale["conflict"] and not stale["saved"] and stale["modified_by"] == "someone"
    assert viya.files[fid]["data"] == b"changed elsewhere"  # nothing written
    forced = _data(sql_editor.sql_editor_save_file(ref, "mine", overwrite=True))
    assert forced["saved"] and viya.files[fid]["data"] == b"mine"


def test_content_save_as_new_and_existing(viya: FakeViya) -> None:
    ref = "sas-content://Zeus/Users/me/My Folder/new æ ł"
    d = _data(sql_editor.sql_editor_save_file(ref, "select 'æ';"))
    assert d["saved"] and d["created"] and d["name"] == "new æ ł.sql" and d["path"].endswith("/new æ ł.sql")
    fid = next(k for k, v in viya.files.items() if v["name"] == "new æ ł.sql")
    assert viya.files[fid]["ctype"] == "text/plain" and viya.files[fid]["data"] == "select 'æ';".encode()
    again = _data(sql_editor.sql_editor_save_file(d["path"], "select 2;"))
    assert again["exists"] and not again["saved"]
    assert _data(sql_editor.sql_editor_save_file(d["path"], "select 2;", overwrite=True))["saved"]
    sas = _data(sql_editor.sql_editor_save_file("sas-content://Zeus/Users/me/My Folder/p.sas", "data _null_; run;"))
    assert viya.files[next(k for k, v in viya.files.items() if v["name"] == "p.sas")]["ctype"] == "application/vnd.sas.studio.programfile"
    assert sas["saved"]
    with pytest.raises(LookupError, match="No SAS Content folder"):
        sql_editor.sql_editor_save_file("sas-content://Zeus/Nope/x.sql", "x")
    viya._folder("sub.d", viya.my)
    with pytest.raises(ValueError, match="folder"):
        sql_editor.sql_editor_save_file("sas-content://Zeus/Users/me/My Folder/sub.d", "x", overwrite=True)


def test_content_mkdir(viya: FakeViya) -> None:
    d = vf.mkdir("content", "/Users/me/My Folder", " zz_test ")
    assert d["path"] == "/Users/me/My Folder/zz_test"
    assert [f["name"] for f in vf.browse("content")["folders"]] == ["zz_test"]
    with pytest.raises(ValueError, match="already exists"):
        vf.mkdir("content", "/Users/me/My Folder", "zz_test")
    with pytest.raises(ValueError):
        vf.mkdir("content", "/Users/me/My Folder", "a/b")


# ------------------------------------------------------------------ SAS Server


def test_server_browse_open_save_conflict(viya: FakeViya) -> None:
    d = vf.browse("server")
    assert d["path"] == "/srv/landing"  # the first lockdown path
    assert [s["label"] for s in d["shortcuts"]] == ["Home", "landing"]  # home is / here, so no separate Root
    viya.fs["/srv/landing/q.sql"] = {"dir": False, "data": b"select 1;", "etag": '"s1"'}
    viya.fs["/srv/landing/.hidden.sql"] = {"dir": False, "data": b"x", "etag": '"s0"'}
    viya.fs["/srv/landing/sub"] = {"dir": True}
    d = vf.browse("server", "/srv/landing")
    assert [f["name"] for f in d["files"]] == ["q.sql"] and [f["name"] for f in d["folders"]] == ["sub"]
    assert d["files"][0]["ref"] == "sas-server://Zeus/srv/landing/q.sql" and d["parent"] == "/srv"
    with pytest.raises(LookupError, match="isn't a folder"):
        vf.browse("server", "/nope")

    ref = "sas-server://Zeus/srv/landing/q.sql"
    opened = _data(sql_editor.sql_editor_open_file(ref))
    assert opened["sql"] == "select 1;" and opened["version"] == '"s1"' and opened["location"] == "server"
    saved = _data(sql_editor.sql_editor_save_file(ref, "select 2;", expected_version='"s1"'))
    assert saved["saved"] and viya.fs["/srv/landing/q.sql"]["data"] == b"select 2;"
    viya.fs["/srv/landing/q.sql"].update(data=b"other", etag='"zz"')
    assert _data(sql_editor.sql_editor_save_file(ref, "mine", expected_version=saved["version"]))["conflict"]
    assert viya.fs["/srv/landing/q.sql"]["data"] == b"other"
    assert _data(sql_editor.sql_editor_save_file(ref, "mine", overwrite=True))["saved"]

    new = _data(sql_editor.sql_editor_save_file("sas-server://Zeus/srv/landing/sub/new.sas", "proc print; run;"))
    assert new["saved"] and new["created"] and viya.fs["/srv/landing/sub/new.sas"]["data"] == b"proc print; run;"
    assert _data(sql_editor.sql_editor_save_file("sas-server://Zeus/srv/landing/sub/new.sas", "x"))["exists"]
    with pytest.raises(LookupError, match="doesn't exist"):
        sql_editor.sql_editor_save_file("sas-server://Zeus/nope/x.sql", "x")
    with pytest.raises(LookupError, match="doesn't exist"):
        sql_editor.sql_editor_open_file("sas-server://Zeus/srv/landing/missing.sql")
    assert vf.mkdir("server", "/srv/landing", "zz")["path"] == "/srv/landing/zz" and viya.fs["/srv/landing/zz"]["dir"]


# ------------------------------------------------------------------ connections and the editor's state


def test_other_connection_is_reported_and_save_can_use_the_active_one(viya: FakeViya) -> None:
    viya.add_file(viya.my, "a.sql", "select 1;")
    ref = "sas-content://Zeus/Users/me/My Folder/a.sql"
    opened = _data(sql_editor.sql_editor_open_file(ref))
    viya.active["name"] = "Other"  # the user switched the active SAS Viya connection
    with pytest.raises(LookupError, match="'Zeus'.*active one is 'Other'"):
        sql_editor.sql_editor_open_file(ref)
    d = _data(sql_editor.sql_editor_save_file(ref, "select 2;", expected_version=opened["version"]))
    assert d["connection_mismatch"] and d["file_connection"] == "Zeus" and d["active"] == "Other" and not d["saved"]
    # Save to the same path on the active connection: another file there, so the version opened doesn't apply.
    d = _data(sql_editor.sql_editor_save_file(ref, "select 2;", expected_version=opened["version"], use_active=True))
    assert d["exists"]
    d = _data(sql_editor.sql_editor_save_file(ref, "select 2;", overwrite=True, use_active=True))
    assert d["saved"] and d["path"] == "sas-content://Other/Users/me/My Folder/a.sql" and d["connection"] == "Other"


def test_not_signed_in_points_to_connections(viya: FakeViya, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise sas_viya.NeedsSignIn("Not signed in to SAS Viya (Zeus): use Sign in under SAS Viya in the Connections window.")

    monkeypatch.setattr(vf, "_http", refuse)
    with pytest.raises(sas_viya.NeedsSignIn, match="Connections window"):
        vf.browse("content")
    viya.active["name"] = None
    with pytest.raises(sas_viya.NeedsSignIn, match="Connections window"):
        vf.browse("server")


def test_draft_keeps_the_viya_version_and_recent_keeps_viya_files(viya: FakeViya, tmp_path: Path) -> None:
    ref = "sas-server://Zeus/srv/landing/q.sql"
    tab = st.resolve_tab(None)
    sql_editor.sql_editor_save_draft("select 1;", file_name="q.sql", file_path=ref, saved_hash="h", file_version='"s1"', tab_id=tab)
    assert st.load_draft(tab)["file"] == {"name": "q.sql", "path": ref, "external": False, "saved_hash": "h", "version": '"s1"'}
    # A local file never gets a version.
    sql_editor.sql_editor_save_draft("select 1;", file_name="x.sql", file_path=str(tmp_path / "x.sql"), file_version="v", tab_id=tab)
    assert "version" not in st.load_draft(tab)["file"]
    st.remember_file(ref, "q.sql")
    st.remember_file(str(tmp_path / "gone.sql"), "gone.sql")  # a local file that doesn't exist is dropped
    assert [r["path"] for r in st.recent_files()] == [ref]
    assert json.loads((tmp_path / "sql-editor" / "recent.json").read_text(encoding="utf-8"))[1]["path"] == ref


# ------------------------------------------------------------------ rename and delete (the file browser)


def test_moved_path() -> None:
    old = "sas-content://Zeus/Users/me/My Folder/a"
    assert st.moved_path(old + "/x.sql", old, old + "2", True) == old + "2/x.sql"
    assert st.moved_path(old + "/x.sql", old, None, True) == ""
    assert st.moved_path(old + "b/x.sql", old, old + "2", True) is None  # a sibling with a longer name
    assert st.moved_path(old + "/x.sql", old, old + "2", False) is None  # a file's change doesn't touch others
    assert st.moved_path(old, old, "sas-content://Zeus/n", False) == "sas-content://Zeus/n"
    assert st.moved_path(old.upper(), old, "x", False) is None  # refs compare exactly
    assert st.moved_path("", old, "x", False) is None


def test_content_rename_and_delete(viya: FakeViya) -> None:
    my = "/Users/me/My Folder"
    sub = viya._folder("sub", viya.my)
    fid = viya.add_file(sub, "a.sql", "select 1;")
    viya.add_file(sub, "b.sql", "select 2;")
    ref = f"sas-content://Zeus{my}/sub/a.sql"
    opened = _data(sql_editor.sql_editor_open_file(ref))
    st.resolve_tab(None)
    tab = st.create_tab()["tab"]["id"]  # a second tab, with the file open
    sql_editor.sql_editor_save_draft("select 1;", file_name="a.sql", file_path=ref, saved_hash="h", file_version=opened["version"], tab_id=tab)

    from singlestore_mcp.apps import file_manage as fm
    listed = vf.browse("content", f"{my}/sub")
    a = next(f for f in listed["files"] if f["name"] == "a.sql")
    assert listed["manage"] and listed["folders"] == [] and a["ref"] == ref

    # A name that's taken, then a file changed since it was listed: nothing renamed.
    assert _data(fm.viya_files_rename("content", a["path"], "b.sql"))["exists"]
    assert _data(fm.viya_files_rename("content", a["path"], "c", expected_modified=a["modified"] + 60))["conflict"]
    res = _data(fm.viya_files_rename("content", a["path"], "c", expected_modified=a["modified"]))
    assert res["renamed"] and res["new_path"] == f"{my}/sub/c.sql"  # keeps its extension
    assert res["from_ref"] == ref and res["to_ref"] == f"sas-content://Zeus{my}/sub/c.sql" and res["tabs"] == [tab]
    assert viya.files[fid]["name"] == "c.sql"
    assert st.load_draft(tab)["file"]["path"] == res["to_ref"] and st.load_draft(tab)["file"]["name"] == "c.sql"
    # The rename changed the file's version: the tab, which had the old one, gets the new one (Save keeps working).
    assert res["old_version"] == opened["version"] and st.load_draft(tab)["file"]["version"] == viya.files[fid]["etag"] == res["version"]
    assert st.recent_files()[0]["path"] == res["to_ref"]

    # The folder: renamed (the tab follows), never into a path, and only deleted when it's empty.
    with pytest.raises(ValueError):
        fm.viya_files_rename("content", f"{my}/sub", "x/y")
    res = _data(fm.viya_files_rename("content", f"{my}/sub", "sub2"))
    assert res["renamed"] and res["folder"] and viya.folders[sub]["name"] == "sub2"
    assert st.load_draft(tab)["file"]["path"] == f"sas-content://Zeus{my}/sub2/c.sql"
    assert _data(fm.viya_files_delete("content", f"{my}/sub2"))["not_empty"]
    assert sub in viya.folders

    # Delete the file: the tab keeps its text as unsaved; then the empty folder.
    stale = _data(fm.viya_files_delete("content", f"{my}/sub2/c.sql", expected_modified=1))
    assert stale["conflict"] and fid in viya.files
    res = _data(fm.viya_files_delete("content", f"{my}/sub2/c.sql"))
    assert res["deleted"] and fid not in viya.files and res["tabs"] == [tab]
    draft = st.load_draft(tab)
    assert draft["file"] is None and draft["dirty"] and draft["sql"] == "select 1;"
    assert all("c.sql" not in r["path"] for r in st.recent_files())
    _data(fm.viya_files_delete("content", f"{my}/sub2/b.sql"))
    assert _data(fm.viya_files_delete("content", f"{my}/sub2"))["deleted"] and sub not in viya.folders


def test_content_protected_items(viya: FakeViya) -> None:
    from singlestore_mcp.apps import file_manage as fm
    for path in ("/Users/me/My Folder", "/Users/me", "/Users", "/Public", "/"):
        with pytest.raises(ValueError):
            fm.viya_files_delete("content", path)
        with pytest.raises(ValueError):
            fm.viya_files_rename("content", path, "x")
    with pytest.raises(LookupError, match="doesn't exist"):
        fm.viya_files_delete("content", "/Users/me/My Folder/nope.sql")
    with pytest.raises(ValueError):
        fm.viya_files_rename("content", "/Users/me/My Folder/a.sql", "..")


def test_server_rename_and_delete(viya: FakeViya) -> None:
    from singlestore_mcp.apps import file_manage as fm
    viya.fs["/srv/landing/sub"] = {"dir": True, "etag": '"d1"'}
    viya.fs["/srv/landing/sub/q.sas"] = {"dir": False, "data": b"proc print; run;", "etag": '"s1"'}
    ref = "sas-server://Zeus/srv/landing/sub/q.sas"
    tab = st.resolve_tab(None)
    sql_editor.sql_editor_save_draft("x", file_name="q.sas", file_path=ref, saved_hash="h", tab_id=tab)
    for path in ("/srv/landing", "/srv", "/tmp", "/"):  # the lockdown path, a folder above it, top level
        with pytest.raises(ValueError):
            fm.viya_files_rename("server", path, "x")
    res = _data(fm.viya_files_rename("server", "/srv/landing/sub", "sub2"))
    assert res["renamed"] and "/srv/landing/sub2/q.sas" in viya.fs and "/srv/landing/sub" not in viya.fs
    assert st.load_draft(tab)["file"]["path"] == "sas-server://Zeus/srv/landing/sub2/q.sas"
    assert _data(fm.viya_files_rename("server", "/srv/landing/sub2/q.sas", "q.sas"))["unchanged"]
    assert _data(fm.viya_files_delete("server", "/srv/landing/sub2"))["not_empty"]
    res = _data(fm.viya_files_delete("server", "/srv/landing/sub2/q.sas"))
    assert res["deleted"] and "/srv/landing/sub2/q.sas" not in viya.fs and st.load_draft(tab)["file"] is None
    assert _data(fm.viya_files_delete("server", "/srv/landing/sub2"))["deleted"]


def test_notebook_on_viya(viya: FakeViya) -> None:
    from singlestore_mcp.apps import notebook
    nb = {"cells": [{"cell_type": "code", "source": "1", "metadata": {}, "outputs": [], "execution_count": None}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    ref = "sas-content://Zeus/Users/me/My Folder/analysis"
    saved = _data(notebook.notebook_save(ref, nb))
    assert saved["saved"] and saved["path"] == ref + ".ipynb" and saved["name"] == "analysis.ipynb"
    fid = next(k for k, v in viya.files.items() if v["name"] == "analysis.ipynb")
    assert viya.files[fid]["ctype"] == vf.IPYNB_MEDIA_TYPE
    assert _data(notebook.notebook_save(saved["path"], nb))["exists"]
    listing = vf.browse("content", "/Users/me/My Folder", kind="notebook")
    assert [f["name"] for f in listing["files"]] == ["analysis.ipynb"]
    assert vf.browse("content", "/Users/me/My Folder")["files"] == []  # the SQL Editor lists .sql / .sas only
    opened = _data(notebook.notebook_open(saved["path"]))
    assert opened["notebook"]["cells"][0]["source"] == "1" and opened["version"] == viya.files[fid]["etag"]
    viya.touch(fid, json.dumps(nb))
    assert _data(notebook.notebook_save(saved["path"], nb, expected_version=opened["version"]))["conflict"]
    viya.touch(fid, "not json")
    with pytest.raises(ValueError, match="isn't a valid notebook"):
        notebook.notebook_open(saved["path"])
