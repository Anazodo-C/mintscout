"""Railway entrypoint: the live runner plus a tiny status endpoint.

Railway health-checks an HTTP port, and a worker with no listener gets marked
unhealthy and restarted. So the runner owns the main thread and a minimal HTTP
server runs alongside it on $PORT, exposing:

    GET /health   liveness -- 200 while the process is up
    GET /status   spend, counters and config (NEVER any key material)

If the runner exits, the process exits, and Railway restarts it per railway.json.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .live import Runner, log

_started = time.time()
_runner: Runner | None = None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", ""):
            return self._send(200, {"ok": True,
                                    "uptime_s": int(time.time() - _started)})
        if path == "/status":
            r = _runner
            if r is None:
                return self._send(503, {"ok": False, "detail": "runner starting"})
            # Deliberately excludes MINT_SEED, the derived private key and the
            # API key. The wallet ADDRESS is public information and is safe.
            return self._send(200, {
                "ok": True,
                "uptime_s": int(time.time() - _started),
                "mode": "live" if r.live else "dry_run",
                "chains": r.chains,
                "wallet": r.wallet,
                "llm_triage": r.use_llm,
                "prefilter_min_score": r.prefilter_min,
                "stats": r.stats,
                "budget": r.guard.summary,
                "limits": r.guard.limits.as_dict(),
            })
        return self._send(404, {"ok": False})

    def log_message(self, *_):
        return          # keep Railway logs free of HTTP access noise


def _serve_http() -> None:
    port = int(os.environ.get("PORT", "8080"))
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except Exception as e:
        log(f"[http] status server failed on :{port} ({type(e).__name__})")


def main() -> int:
    global _runner
    threading.Thread(target=_serve_http, daemon=True).start()
    log(f"[http] status endpoint on :{os.environ.get('PORT', '8080')} "
        f"(/health, /status)")
    _runner = Runner()
    return _runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
