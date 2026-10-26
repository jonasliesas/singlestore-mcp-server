"""The SAS session status in the SQL Editor's SAS header, in a browser: "SAS session: starting…" while the session
starts in the background, then "ready", and a run that comes meanwhile says "Waiting for the SAS session to
start…". SAS Viya is stubbed (the sas_* calls are answered by the test), so it needs the cluster but not SAS."""

from __future__ import annotations

import json
from typing import Any

import pytest
from support import DB
from uihelpers import find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]


def _reply(route: Any, data: dict[str, Any], label: str = "ok") -> None:
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps({"content": [{"type": "text", "text": label}], "structuredContent": data}))


def test_sql_editor_shows_the_sas_session_state(ui, app_url) -> None:
    page, errors = ui
    warm = {"state": "starting"}
    calls: list[tuple[str, dict[str, Any]]] = []
    held: list[Any] = []

    def route(r: Any) -> None:
        try:
            body = json.loads(r.request.post_data or "{}")
        except ValueError:
            body = {}
        name, args = body.get("name") or "", body.get("arguments") or {}
        calls.append((name, args))
        if name == "sas_prewarm":
            _reply(r, {"connection": "Zeus", "enabled": True, "state": warm["state"], "compute_session": warm["state"] == "ready",
                       **({"ms": 21000, "compute_ms": 19000} if warm["state"] == "ready" else {})})
        elif name == "sas_libraries":
            _reply(r, {"libraries": [{"name": "WORK", "engine": "V9", "singlestore": False, "sas9": False}], "startup": None,
                       "compute_context": "ctx", "connection": "Zeus", "auto_libname": None, "ms": 1})
        elif name in ("sas_query", "sas_execute"):
            _reply(r, {"run_id": args.get("run_id"), "running": True, "state": "queued", "connection": "Zeus", "engine": "compute",
                       "elapsed_ms": 900, "waiting_for": {"run_id": None, "tab": None, "elapsed_ms": None, "warming": True}},
                   "Waiting for the SAS session to start")
        elif name == "sas_run_status":
            held.append(r)  # the run keeps waiting for the session
        else:
            r.continue_()

    page.route("**/call", route)
    page.goto(app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True, "sql": "-- SAS session status test"}),
              wait_until="domcontentloaded")
    find_frame(page, "SAS SpeedyStore Studio · Workspace", 30)
    sql = find_frame(page, "SQL Editor", 30)
    assert wait_js(sql, "document.querySelector('.cm-content') && document.body.innerText.includes('Ctrl')", 30)
    try:
        sql.get_by_label("Run as", exact=True).select_option("procsql")
        assert wait_js(sql, "document.querySelector('.sq-sas-session .s2-sas-session')?.innerText === 'SAS session: starting…'", 10), \
            "no 'starting' status: " + text(sql)[:400]
        assert any(n == "sas_prewarm" and not a.get("connection") and a.get("start") for n, a in calls)

        # A run while the session starts: the status line says what it waits for.
        sql.evaluate("(d) => window.dispatchEvent(new CustomEvent('s2:set-sql', {detail: d}))",
                     {"sql": "select name from work.s2_ui_class;", "run": True})
        assert wait_js(sql, "(document.querySelector('.sq-status')?.innerText ?? '').includes('Waiting for the SAS session to start')", 10), \
            "no waiting text: " + text(sql)[-400:]
        for _ in range(20):  # (route handlers run while Playwright is called)
            run = next((a for n, a in calls if n in ("sas_query", "sas_execute")), None)
            if run is not None:
                break
            page.wait_for_timeout(250)
        assert run is not None, [n for n, _ in calls]
        assert run["wait_ms"] <= 2000  # answered early while warming, so the status shows at once

        # The session is up: the badge polls and says "ready".
        warm["state"] = "ready"
        assert wait_js(sql, "document.querySelector('.sq-sas-session .s2-sas-session')?.dataset.state === 'ready'", 10), text(sql)[:400]
        badge = sql.locator(".sq-sas-session .s2-sas-session")
        assert badge.inner_text() == "SAS session: ready" and "21 s" in (badge.get_attribute("title") or "")
        workspace_calls = [a for n, a in calls if n == "sas_prewarm"]
        assert len(workspace_calls) >= 2
        # The Workspace shell asked too, once it had painted (no view has to be a SAS one for that).
        assert any(n == "sas_prewarm" and a == {} for n, a in calls), calls
    finally:
        for r in held:
            try:
                r.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"content": [{"type": "text", "text": "Stopped by the test"}], "isError": True}))
            except Exception:  # noqa: BLE001 - the page may be gone
                pass
        page.unroute("**/call")
        try:
            sql.get_by_label("Run as", exact=True).select_option("singlestore")
        except Exception:  # noqa: BLE001
            pass
