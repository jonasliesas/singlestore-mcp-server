"""A tiny "browser" for the hub tests: its own cookie jar, no automatic redirects, Origin header on POSTs."""

from __future__ import annotations

import http.client
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any


@dataclass
class Response:
    status: int
    headers: http.client.HTTPMessage
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def location(self) -> str | None:
        return self.headers.get("Location")

    def set_cookies(self) -> list[str]:
        return self.headers.get_all("Set-Cookie") or []

    def hidden(self, name: str) -> str:
        m = re.search(rf'name="{name}" value="([^"]*)"', self.text)
        assert m, f"no hidden field {name!r} in the page"
        return m.group(1)


class Browser:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.cookies: dict[str, str] = {}
        self.origin = self.base

    def _store(self, res: Response) -> None:
        for line in res.set_cookies():
            pair = line.split(";", 1)[0]
            name, _, value = pair.partition("=")
            if "Max-Age=0" in line or not value:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value

    def request(self, method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None,
                origin: str | None | bool = True) -> Response:
        if url.startswith("http"):
            parts = urllib.parse.urlsplit(url)
            assert f"{parts.scheme}://{parts.netloc}" == self.base, url
            url = parts.path + (f"?{parts.query}" if parts.query else "")
        host = urllib.parse.urlsplit(self.base)
        conn = http.client.HTTPConnection(host.hostname, host.port, timeout=180)
        h = dict(headers or {})
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if method == "POST" and origin is True:
            h["Origin"] = self.origin
        elif isinstance(origin, str):
            h["Origin"] = origin
        try:
            conn.request(method, url, body=body, headers=h)
            r = conn.getresponse()
            res = Response(r.status, r.headers, r.read())
        finally:
            conn.close()
        self._store(res)
        return res

    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

    def post_form(self, url: str, data: dict[str, str], **kw: Any) -> Response:
        return self.request("POST", url, urllib.parse.urlencode(data).encode(),
                            {"Content-Type": "application/x-www-form-urlencoded"}, **kw)

    def call(self, name: str, arguments: dict[str, Any] | None = None, **kw: Any) -> Response:
        """A Studio tool call, as the page's fetch("call") sends it."""
        return self.request("POST", "/studio/call", json.dumps({"name": name, "arguments": arguments or {}}).encode(),
                            {"Content-Type": "application/json"}, **kw)

    def follow(self, res: Response) -> Response:
        while res.status in (302, 303) and res.location:
            loc = res.location
            if loc.startswith("http") and not loc.startswith(self.base):
                return res
            res = self.get(loc)
        return res
