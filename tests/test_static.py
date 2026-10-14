"""Offline checks: lint, JavaScript syntax of every app page, and the browser-view tool allowlist."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys

import pytest
from support import APPS_DIR, REPO

# ----------------------------------------------------------------------------- ruff

RUFF = REPO / ".venv" / "Scripts" / "ruff.exe"


@pytest.mark.parametrize("target", ["src", "tests", "perf"])
def test_ruff_clean(target: str) -> None:
    if not (REPO / target).exists():
        pytest.skip(f"no {target}/ folder")
    cmd = [str(RUFF)] if RUFF.exists() else [sys.executable, "-m", "ruff"]
    r = subprocess.run([*cmd, "check", "--no-cache", target], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, f"ruff check {target}:\n{r.stdout[-4000:]}{r.stderr[-1000:]}"


# ----------------------------------------------------------------------------- JavaScript syntax

_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", re.S | re.I)


def _js_blocks() -> list[tuple[str, str, str]]:
    """(id, extension, code) for every inline script of the app pages and every app .js file."""
    out = []
    files = sorted(APPS_DIR.glob("*.html")) + sorted(APPS_DIR.glob("*.js")) + sorted((APPS_DIR / "vendor").glob("*.js"))
    for f in files:
        text = f.read_text(encoding="utf-8")
        blocks = _SCRIPT.findall(text) if f.suffix == ".html" else [("", text)]
        for i, (attrs, code) in enumerate(blocks):
            if "json" in attrs.lower() or not code.strip():
                continue  # data blocks (type="application/json") and empty placeholders
            module = "module" in attrs.lower() or (f.suffix == ".js" and re.search(r"^\s*(export|import)\s", code, re.M))
            rel = f.relative_to(APPS_DIR).as_posix()
            out.append((f"{rel}#{i}" if f.suffix == ".html" else rel, ".mjs" if module else ".js", code))
    return out


_BLOCKS = _js_blocks()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("block_id,ext,code", _BLOCKS, ids=[b[0] for b in _BLOCKS])
def test_js_syntax(tmp_path, block_id: str, ext: str, code: str) -> None:
    p = tmp_path / f"script{ext}"
    p.write_text(code, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"{block_id}: {r.stderr.strip()[:1500]}"


# ----------------------------------------------------------------------------- browser-view allowlist

def app_tool_names() -> set[str]:
    """Tools declared with ``@apps.tool(resource_uri=...)`` in apps/*.py (these get ``_meta.ui``)."""
    names = set()
    for f in APPS_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "tool"
                        and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "apps"
                        and any(k.arg == "resource_uri" for k in dec.keywords)):
                    name = next((k.value.value for k in dec.keywords if k.arg == "name" and isinstance(k.value, ast.Constant)), node.name)
                    names.add(name)
    return names


def extra_tools() -> set[str]:
    tree = ast.parse((APPS_DIR / "browser_view.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_EXTRA_TOOLS" for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise AssertionError("_EXTRA_TOOLS not found in apps/browser_view.py")


_LITERAL_CALL = re.compile(r"""callTool\(\s*(["'`])(\w+)\1""")
_FUNC_DEF = re.compile(r"(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)\s*\{")
_ARROW_DEF = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>")


def _arg_at(js: str, start: int, index: int) -> str | None:
    """The ``index``-th top-level argument of a call whose '(' is just before ``start``."""
    depth, i, arg, quote = 0, start, 0, None
    begin = start
    while i < len(js):
        c = js[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return js[begin:i].strip() if arg == index else None
            depth -= 1
        elif c == "," and depth == 0:
            if arg == index:
                return js[begin:i].strip()
            arg += 1
            begin = i + 1
        i += 1
    return None


def tools_called(js: str) -> set[str]:
    """Tool names an app calls: ``callTool("x"`` and calls of local wrappers such as ``call("x", ...)``."""
    names = {m.group(2) for m in _LITERAL_CALL.finditer(js)}
    wrappers: dict[str, int] = {}
    for rx in (_FUNC_DEF, _ARROW_DEF):
        for m in rx.finditer(js):
            params = [p.strip().split("=")[0].strip() for p in m.group(2).split(",")]
            body = js[m.end(): m.end() + 800]
            for idx, p in enumerate(params):
                if p and re.search(rf"callTool\(\s*{re.escape(p)}\s*[,)]", body):
                    wrappers[m.group(1)] = idx
    for fname, idx in wrappers.items():
        for m in re.finditer(rf"(?<![\w.]){re.escape(fname)}\(", js):
            before = js[max(0, m.start() - 20): m.start()]
            if re.search(r"function\s*$", before):
                continue
            arg = _arg_at(js, m.end(), idx)
            lit = re.fullmatch(r"""(["'`])(\w+)\1""", arg or "")
            if lit:
                names.add(lit.group(2))
    return names


def _pages() -> list[str]:
    return sorted(p.name for p in APPS_DIR.glob("*.html")) + ["shared.js"]


def test_wrapper_detection_finds_known_calls() -> None:
    """Guards the parser above: it must see calls made through the apps' small wrappers."""
    pm =tools_called((APPS_DIR / "pipeline_monitor.html").read_text(encoding="utf-8"))
    assert {"start_pipeline", "stop_pipeline"} <= pm, pm
    assert "workload_status" in tools_called((APPS_DIR / "workload.html").read_text(encoding="utf-8"))


@pytest.mark.parametrize("page", _pages())
def test_browser_view_allows_every_tool_an_app_calls(page: str) -> None:
    """Every tool a page calls must be callable from the browser view ("Open in browser"): an app tool
    (``@apps.tool(resource_uri=...)``) or listed in ``_EXTRA_TOOLS`` in apps/browser_view.py.
    Regression: workload_status / workload_stop were blocked in the browser view."""
    allowed = app_tool_names() | extra_tools()
    called = tools_called((APPS_DIR / page).read_text(encoding="utf-8"))
    missing = sorted(called - allowed)
    assert not missing, (f"{page} calls tool(s) the browser view refuses ('not available in the browser view'): {missing}. "
                         "Make them app tools or add them to _EXTRA_TOOLS in apps/browser_view.py.")
