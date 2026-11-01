"""Loopback-only HTTP server over Arena's public read model."""

from __future__ import annotations

import json
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlparse

from rsi_arena.projection import ArenaProjectionError, ArenaRunProjection

_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


class _ArenaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    projections: ClassVar[dict[str, ArenaRunProjection]] = {}
    server_version = "rsi-arena-inspector"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in {"", "/"}:
            self._send_static("index.html")
            return
        if path == "/api/runs":
            try:
                runs = [
                    projection.catalogue_entry()
                    for _run_id, projection in sorted(self.projections.items())
                ]
            except ArenaProjectionError as exc:
                self._send_json({"error": str(exc)}, status=409)
                return
            self._send_json({"runs": runs})
            return
        if path.startswith("/api/runs/"):
            run_id = path.removeprefix("/api/runs/")
            projection = self.projections.get(run_id)
            if not run_id:
                self._send_json({"error": "run_id is required"}, status=400)
            elif projection is None:
                self._send_json({"error": "unknown Arena run"}, status=404)
            else:
                try:
                    self._send_json(projection.detail())
                except ArenaProjectionError as exc:
                    self._send_json({"error": str(exc)}, status=409)
            return
        if path == "/api/diff":
            query = parse_qs(parsed.query)
            run_id = query.get("run_id", [""])[0]
            candidate_id = query.get("candidate_id", [""])[0]
            base = query.get("base", [""])[0]
            if not run_id or not candidate_id or not base:
                self._send_json(
                    {"error": "run_id, candidate_id, and base are required"},
                    status=400,
                )
                return
            projection = self.projections.get(run_id)
            if projection is None:
                self._send_json({"error": "unknown Arena run"}, status=404)
                return
            try:
                self._send_json(projection.diff(candidate_id, base))
            except ArenaProjectionError as exc:
                self._send_json({"error": str(exc)}, status=409)
            return
        self._send_static(path.lstrip("/"))

    def _send_static(self, relative: str) -> None:
        try:
            target = (_STATIC_ROOT / relative).resolve(strict=True)
            target.relative_to(_STATIC_ROOT)
        except (OSError, ValueError):
            self.send_error(404)
            return
        if not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            _CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy", "default-src 'self'; object-src 'none'"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(
    run_roots: Sequence[str | Path],
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Create a read-only inspector for one or more canonical Arena runs."""

    if host not in _LOOPBACK_HOSTS:
        raise ValueError("Arena inspector must bind to a loopback host")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Arena inspector port must be between 0 and 65535")
    if isinstance(run_roots, (str, Path)):
        raise TypeError("Arena inspector run_roots must be a sequence")
    roots = tuple(run_roots)
    if not roots:
        raise ValueError("at least one Arena run root is required")
    projections: dict[str, ArenaRunProjection] = {}
    for root in roots:
        projection = ArenaRunProjection(root)
        if projection.run_id in projections:
            raise ValueError("duplicate Arena run id")
        projections[projection.run_id] = projection
    handler = type(
        "_BoundArenaHandler",
        (_Handler,),
        {"projections": projections},
    )
    return _ArenaHTTPServer((host, port), handler)


__all__ = ["serve"]
