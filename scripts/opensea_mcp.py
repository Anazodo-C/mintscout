"""Minimal OpenSea MCP client for a SCOPED TOKEN.

A scoped token is not an API key. It authenticates `Authorization: Bearer`
against the MCP endpoint, whereas the REST v2 collection endpoints want
`X-API-KEY` and reject it with "Invalid API key". Two credential types, two
surfaces -- so floor/offers are read through MCP here rather than REST.

Two practical gotchas, both discovered the hard way:
  * Cloudflare blocks python-urllib's default User-Agent with error 1010
    ("browser signature banned"). A browser UA is required.
  * `initialize` returns an `Mcp-Session-Id` response header that every
    subsequent call must echo back, or the server answers
    "Bad Request: Mcp-Session-Id header is required".

Responses come back as SSE (`event: message\\ndata: {...}`), so the JSON has to
be pulled out of the data lines.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from mintscout.cli import _load_dotenv  # noqa: E402

_load_dotenv()

ENDPOINT = "https://mcp.opensea.io/mcp"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _parse_sse(body: str) -> dict:
    """Pull the JSON payload out of an SSE response."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                continue
    try:
        return json.loads(body)
    except ValueError:
        return {}


class OpenSeaMCP:
    def __init__(self, token: str | None = None):
        self.token = (token or os.environ.get("OPENSEA_API_KEY") or "").strip()
        if not self.token:
            raise RuntimeError("no scoped token: set OPENSEA_API_KEY")
        self.session: str | None = None
        self._id = 0

    def _call(self, method: str, params: dict | None = None,
              timeout: int = 45) -> dict:
        self._id += 1
        headers = {
            "Authorization": f"Bearer {self.token}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "user-agent": UA,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(
            ENDPOINT,
            data=json.dumps({"jsonrpc": "2.0", "id": self._id,
                             "method": method, "params": params or {}}).encode(),
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session = sid
                return _parse_sse(r.read(400_000).decode("utf8", "replace"))
        except Exception as e:
            body = ""
            if hasattr(e, "read"):
                try:
                    body = e.read(600).decode("utf8", "replace")
                except Exception:
                    pass
            return {"error": {"message": f"{type(e).__name__}: {body or e}"}}

    def connect(self) -> dict:
        r = self._call("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "mintscout", "version": "0.1"}})
        # the notification is required before the server will serve tools
        self._call("notifications/initialized")
        return r

    def tools(self) -> list[dict]:
        r = self._call("tools/list")
        return ((r.get("result") or {}).get("tools")) or []

    def call_tool(self, name: str, args: dict) -> dict:
        return self._call("tools/call", {"name": name, "arguments": args})


if __name__ == "__main__":
    m = OpenSeaMCP()
    info = m.connect()
    print("server:", ((info.get("result") or {}).get("serverInfo") or {}))
    print("session:", (m.session or "")[:12], "…")
    ts = m.tools()
    print(f"\n{len(ts)} tools:")
    for t in ts:
        print(f"  {t.get('name'):34} {(t.get('description') or '')[:78]}")
