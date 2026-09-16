"""A minimal ASGI caller.

The starlette test client needs httpx, which is not a dependency of this
project and is not worth adding to assert a status code. This exercises the
real app — real routing, real middleware, real status codes and headers — in
about thirty lines.
"""
from __future__ import annotations

import asyncio
import json as jsonlib

from issuergraph.api import app


class Response:
    def __init__(self, status: int, headers: list, body: bytes):
        self.status = status
        self.headers = {k.decode().lower(): v.decode() for k, v in headers}
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode()

    def json(self):
        return jsonlib.loads(self.body)


def request(method: str, path: str, json=None, headers=None) -> Response:
    query = b""
    if "?" in path:
        path, _, query_text = path.partition("?")
        query = query_text.encode()

    payload = jsonlib.dumps(json).encode() if json is not None else b""
    raw_headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method.upper(), "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query,
        "root_path": "", "headers": raw_headers,
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }

    state = {"sent": False, "status": None, "headers": [], "body": bytearray()}

    async def receive():
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            state["status"] = message["status"]
            state["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            state["body"].extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return Response(state["status"], state["headers"], bytes(state["body"]))


def get(path, **kwargs) -> Response:
    return request("GET", path, **kwargs)


def post(path, **kwargs) -> Response:
    return request("POST", path, **kwargs)
