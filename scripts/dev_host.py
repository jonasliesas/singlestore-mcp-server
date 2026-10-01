"""Local dev host for this server's MCP Apps.

Plays the part of Claude: launches the real MCP server over stdio, renders an
app's ``ui://`` resource in a sandboxed iframe and speaks the MCP Apps
postMessage protocol to it, forwarding the app's tool calls to the server.
Lets you iterate on an app in a browser against a real cluster without
reinstalling it in Claude.

    uv run python scripts/dev_host.py [--port 8765]

Then open http://127.0.0.1:8765. Every app page load starts a fresh server
process, so Python and HTML edits show up on browser reload. Uses the same
SINGLESTORE_* environment variables as the server.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

PROJECT_DIR = Path(__file__).resolve().parent.parent
HOST_PAGE = Path(__file__).with_name("dev_host.html")


def _dump(model: Any) -> Any:
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


class ServerSession:
    """Owns the stdio MCP session in one long-lived task.

    anyio context managers must be exited by the task that entered them, so
    requests hand work to this task through a queue instead of touching the
    session directly.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, Any, asyncio.Future[Any]]] = asyncio.Queue()

    async def run(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "singlestore_mcp.server"],
            env={**os.environ, "SINGLESTORE_MCP_DEV_SKIP_BROKEN_APPS": "1"},
            cwd=str(PROJECT_DIR),
        )
        while True:
            try:
                async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=60)
                    while True:
                        op, payload, fut = await self._queue.get()
                        if op == "restart":
                            fut.set_result(None)
                            break
                        try:
                            fut.set_result(await self._handle(session, op, payload))
                        except Exception as exc:  # noqa: BLE001 - surfaced to the browser
                            fut.set_exception(exc)
            except Exception as exc:  # noqa: BLE001
                # Server failed to start or died: report it to the next request
                # instead of hanging, then try a fresh process after that.
                print(f"MCP server failed: {exc!r} (its traceback is above)", file=sys.stderr, flush=True)
                op, _, fut = await self._queue.get()
                if op == "restart":
                    fut.set_result(None)
                else:
                    fut.set_exception(RuntimeError(f"MCP server failed to start: {exc!r}. See the dev host console."))

    async def _handle(self, session: ClientSession, op: str, payload: Any) -> Any:
        if op == "tools":
            return [_dump(t) for t in (await session.list_tools()).tools]
        if op == "call":
            return _dump(await session.call_tool(payload["name"], payload.get("arguments") or {}))
        if op == "resource":
            return _dump(await session.read_resource(payload["uri"]))
        raise ValueError(f"unknown op {op}")

    async def submit(self, op: str, payload: Any = None) -> Any:
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put((op, payload, fut))
        return await fut


session = ServerSession()


async def index(_: Request) -> HTMLResponse:
    return HTMLResponse(HOST_PAGE.read_text(encoding="utf-8"))


async def api(request: Request) -> JSONResponse:
    op = request.path_params["op"]
    payload = await request.json() if request.method == "POST" else None
    try:
        return JSONResponse(await session.submit(op, payload))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


@asynccontextmanager
async def lifespan(_: Starlette):
    task = asyncio.create_task(session.run())
    yield
    task.cancel()


app = Starlette(
    routes=[Route("/", index), Route("/api/{op}", api, methods=["GET", "POST"])],
    lifespan=lifespan,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(f"Dev host: http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
