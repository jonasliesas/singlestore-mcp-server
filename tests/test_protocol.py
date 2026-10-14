"""MCP protocol surface: tools, ui:// resources and prompts (no cluster needed)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest
from support import APPS_DIR, McpSession
from test_static import tools_called

# Model-visible tools the regression run relies on (a rename or removal should be deliberate).
EXPECTED_TOOLS = {
    "list_databases", "list_tables", "describe_table", "run_sql", "list_pipelines", "pipeline_status", "get_pipeline_ddl",
    "create_pipeline", "alter_pipeline", "start_pipeline", "stop_pipeline", "test_pipeline", "drop_pipeline",
    "list_connections", "use_connection", "browser_link", "restart_server", "sql_editor_reply",
    "pipeline_monitor", "query_grid", "schema_explorer", "cluster_monitor", "sql_editor", "notebook", "connections_window",
    "query_history", "query_advisor_report", "alerts", "cleanup_work_tables",
    "workload", "workload_define", "workload_run", "workload_status", "workload_stop", "workload_compare", "workload_list",
}


@pytest.fixture(scope="module")
def tools(mcp: McpSession) -> dict[str, dict]:
    return {t.name: t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in mcp.list_tools()}


def _visibility(tool: dict) -> list[str]:
    return ((tool.get("_meta") or {}).get("ui") or {}).get("visibility") or ["model", "app"]


def test_server_starts_quickly(mcp: McpSession) -> None:
    assert mcp.startup_s < 30, f"server took {mcp.startup_s:.1f} s to initialize"


def test_expected_tools_present(tools: dict[str, dict]) -> None:
    missing = sorted(EXPECTED_TOOLS - set(tools))
    assert not missing, f"tools missing from tools/list: {missing}"


def test_every_tool_has_a_description_and_schema(tools: dict[str, dict]) -> None:
    bad = [n for n, t in tools.items() if not (t.get("description") or "").strip() or t.get("inputSchema", {}).get("type") != "object"]
    assert not bad, f"tools without description or object input schema: {bad}"


def test_app_tools_point_at_registered_resources(mcp: McpSession, tools: dict[str, dict]) -> None:
    uris = {str(r.uri) for r in mcp.list_resources()}
    bad = {n: t["_meta"]["ui"]["resourceUri"] for n, t in tools.items()
           if "resourceUri" in ((t.get("_meta") or {}).get("ui") or {}) and t["_meta"]["ui"]["resourceUri"] not in uris}
    assert not bad, f"app tools pointing at unknown ui:// resources: {bad}"


def test_every_tool_an_app_calls_exists_and_is_allowed_in_browser_view(tools: dict[str, dict]) -> None:
    """Same as the static allowlist check, against the live tools/list: a page's tool must exist, and the
    browser view only forwards tools with _meta.ui or listed in _EXTRA_TOOLS."""
    from test_static import extra_tools

    problems = {}
    for page in sorted(APPS_DIR.glob("*.html")) + [APPS_DIR / "shared.js"]:
        for name in sorted(tools_called(page.read_text(encoding="utf-8"))):
            if name not in tools:
                problems[f"{page.name}:{name}"] = "no such tool"
            elif "ui" not in (tools[name].get("_meta") or {}) and name not in extra_tools():
                problems[f"{page.name}:{name}"] = "blocked in the browser view"
    assert not problems, problems


def test_resources_list(mcp: McpSession) -> None:
    uris = [str(r.uri) for r in mcp.list_resources()]
    assert len(uris) >= 10, uris
    assert all(u.startswith("ui://singlestore/") for u in uris), uris


def test_every_resource_renders_html(mcp: McpSession) -> None:
    failures = []
    for r in mcp.list_resources():
        rr = mcp.read_resource(r.uri)
        html = rr.contents[0].text
        if "<html" not in html.lower() or len(html) <= 1000:
            failures.append(f"{r.uri}: {len(html)} chars")
        if "<!--S2:HEAD-->" in html or "<!--S2:VENDOR" in html:
            failures.append(f"{r.uri}: unexpanded template marker")
    assert not failures, failures


_PROMPT_ARGS = {"database": "SASDP", "table": "CARS", "sql": "SELECT 1"}


def test_every_prompt_renders(mcp: McpSession) -> None:
    prompts = mcp.list_prompts()
    assert {p.name for p in prompts} >= {"workspace", "explain_query", "create_pipeline", "pipeline_health", "table_report", "restart"}
    failures = []
    for p in prompts:
        args = {a.name: _PROMPT_ARGS.get(a.name, "s3://bucket/path") for a in (p.arguments or []) if a.required}
        try:
            g = mcp.get_prompt(p.name, args)
            text = " ".join(getattr(m.content, "text", "") for m in g.messages)
            if not g.messages or len(text) < 20:
                failures.append(f"{p.name}: empty")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{p.name}: {exc}")
    assert not failures, failures


# ----------------------------------------------------------------------------- browser view (served by this server)

def _post(url_base: str, name: str, arguments: dict, origin: str | None) -> tuple[int, dict | str]:
    req = urllib.request.Request(url_base + "call", data=json.dumps({"name": name, "arguments": arguments}).encode(),
                                 headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


@pytest.fixture(scope="module")
def view_base(mcp: McpSession) -> tuple[str, str]:
    url = mcp.ok("browser_link", {"tool": "alerts", "arguments": {}})["url"]
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", f"{parts.scheme}://{parts.netloc}"


def test_browser_link_rejects_non_app_tool(mcp: McpSession) -> None:
    mcp.err("browser_link", {"tool": "run_sql", "arguments": {"sql": "SELECT 1"}})


def test_browser_view_page_and_tool_metadata(view_base: tuple[str, str]) -> None:
    base, _ = view_base
    with urllib.request.urlopen(base, timeout=30) as r:
        assert r.status == 200 and b"iframe" in r.read()  # the host page (creates the app's iframe)
    with urllib.request.urlopen(base + "tool?name=alerts", timeout=30) as r:
        assert json.loads(r.read())["name"] == "alerts"


def test_browser_view_requires_origin(view_base: tuple[str, str]) -> None:
    base, _ = view_base
    status, _ = _post(base, "alerts_badge", {}, origin=None)
    assert status == 403
    status, _ = _post(base, "alerts_badge", {}, origin="http://evil.example")
    assert status == 403


def test_browser_view_refuses_tools_outside_allowlist(view_base: tuple[str, str]) -> None:
    base, origin = view_base
    status, body = _post(base, "run_sql", {"sql": "SELECT 1"}, origin)
    assert status == 200 and body.get("isError") and "not available in the browser view" in json.dumps(body)


@pytest.mark.parametrize("name,args", [
    ("alerts_badge", {}),
    ("workload_status", {"run_id": "zz-no-such-run"}),
    ("workload_stop", {"run_id": "zz-no-such-run"}),
], ids=["app tool", "workload_status", "workload_stop"])
def test_browser_view_forwards_allowed_tools(view_base: tuple[str, str], name: str, args: dict) -> None:
    """Regression: workload_status / workload_stop were refused in the browser view. With a bogus run id
    they fail, but with the tool's own error, not the allowlist's."""
    base, origin = view_base
    status, body = _post(base, name, args, origin)
    assert status == 200, body
    assert "not available in the browser view" not in json.dumps(body), body
