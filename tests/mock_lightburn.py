"""A faithful stand-in for the LightBurn 2.2 REST API (stdlib only), for tests and manual checks.

Behaviour follows the official docs (LightBurnSoftware/LightBurn-REST):

- ``GET /`` answers without authentication; ``POST /api/connect`` pairs (localhost only, 400 for an
  empty/unknown capability list, 403 when declined) and returns ``{status, secret}``.
- Every other endpoint needs ``Authorization: Bearer hex(HMAC-SHA256(secret, str(floor(t / 60))))``
  for the current or the previous minute, and the secret's capabilities must cover the endpoint
  (``state``: /api/status, /api/events; ``upload``: /api/file/*). Anything else is a 401 that does not
  say why: ``{"error": "Unauthorized"}``, with an empty body on /api/events.
- ``POST /api/file/upload`` and ``/api/file/open`` take the raw body with ``X-Filename`` and answer
  202; the result is broadcast later as a ``file_imported`` event ``{success, error?}`` (no file name)
  to every subscriber of ``GET /api/events`` (SSE, chunked, opening with a ``:connected`` comment,
  a ``:keepalive`` comment every 30 s). ``/api/file/open`` reports ``success: false`` for non-native
  formats such as SVG, even though the file opened.

Switches for tests: ``capabilities`` (e.g. {"upload"}: key without "state"), ``import_success``,
``import_delay`` (seconds between the 202 and file_imported), ``keepalive``, ``hang`` (accept the
connection, never answer), ``drip`` (answer one byte at a time, forever).

Run standalone for an end-to-end check, e.g. on the Windows host for the container:
    python tests/mock_lightburn.py --host 0.0.0.0 --port 19520 --secret <secret>
Each received upload is printed as one JSON line (without the token).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

CAPABILITIES = {"state", "project", "upload", "project_write"}
SCOPES = {"/api/status": "state", "/api/events": "state", "/api/file/upload": "upload", "/api/file/open": "upload"}
NATIVE = (".lbrn", ".lbrn2")
KEEPALIVE = 30.0  # documented interval of ":keepalive" on /api/events


def expected_token(secret: str, minute: int) -> str:
    return hmac.new(secret.encode("utf-8"), str(minute).encode("ascii"), hashlib.sha256).hexdigest()


class MockLightBurn:
    def __init__(self, secret: str = "aB3dE7fG9hJ1kL5m", host: str = "127.0.0.1", port: int = 0,
                 keepalive: float = KEEPALIVE, verbose: bool = False):
        self.secret = secret
        self.capabilities = set(CAPABILITIES)  # of the one secret this mock knows
        self.keepalive = keepalive
        self.verbose = verbose
        self.import_success = True
        self.failing: set[str] = set()  # file names whose import fails
        self.import_delay = 0.0
        self.hang = False
        self.drip = False
        self.pair = "approve"  # or "decline"
        self.uploads: list[dict] = []
        self.requests: list[tuple[str, str, str | None]] = []  # method, path, Authorization header
        self.events: list[tuple[str, str]] = []  # broadcast log: (event, data)
        self.cond = threading.Condition()
        self.stopping = threading.Event()
        self.httpd = ThreadingHTTPServer((host, port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # --- control -----------------------------------------------------------

    def start(self) -> MockLightBurn:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        with self.cond:
            self.cond.notify_all()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def __enter__(self) -> MockLightBurn:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def valid(self, authorization: str | None, now: float | None = None) -> bool:
        """Current or previous minute, exactly as documented; constant-time compare."""
        if not authorization or not authorization.startswith("Bearer "):
            return False
        presented = authorization[len("Bearer "):]
        minute = int((time.time() if now is None else now) // 60)
        return any(hmac.compare_digest(presented, expected_token(self.secret, m)) for m in (minute, minute - 1))

    def broadcast(self, event: str, payload: dict) -> None:
        with self.cond:
            self.events.append((event, json.dumps(payload)))
            self.cond.notify_all()

    def imported(self, payload: dict) -> None:
        """LightBurn imports asynchronously: file_imported follows the 202 after import_delay."""
        if self.import_delay <= 0:
            self.broadcast("file_imported", payload)
            return
        timer = threading.Timer(self.import_delay, self.broadcast, ("file_imported", payload))
        timer.daemon = True
        timer.start()

    # --- HTTP --------------------------------------------------------------

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "LightBurnMock"

            def log_message(self, format, *args):  # noqa: A002
                pass

            def reply(self, status: int, payload=None, mime="application/json") -> None:
                body = b"" if payload is None else (payload if isinstance(payload, bytes)
                                                     else json.dumps(payload).encode("utf-8"))
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def stall(self) -> bool:
                if mock.hang:
                    mock.stopping.wait(30)
                    self.close_connection = True
                    return True
                if mock.drip:
                    self.close_connection = True
                    try:
                        for byte in b"HTTP/1.1 202 Accepted\r\nContent-Length: 2\r\n\r\n{}" * 100:
                            if mock.stopping.wait(0.1):
                                break
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                    except OSError:
                        pass
                    return True
                return False

            def authorized(self) -> bool:
                path = urlsplit(self.path).path
                header = self.headers.get("Authorization")
                mock.requests.append((self.command, path, header))
                scope = SCOPES.get(path)
                if mock.valid(header) and (scope is None or scope in mock.capabilities):
                    return True
                self.reply(401, None if path == "/api/events" else {"error": "Unauthorized"})
                return False

            def do_GET(self):
                path = urlsplit(self.path).path
                if self.stall():
                    return
                if path == "/":
                    mock.requests.append(("GET", "/", self.headers.get("Authorization")))
                    self.reply(200, b"<html><body>LightBurn</body></html>", "text/html")
                elif path == "/api/status":
                    if self.authorized():
                        self.reply(200, {"connected": False, "device_name": "Mock Laser",
                                         "supports_z": False, "supports_overrides": False})
                elif path == "/api/events":
                    if self.authorized():
                        self.stream()
                else:
                    if self.authorized():
                        self.reply(404, {"error": "Not found"})

            def chunk(self, text: str) -> None:
                data = text.encode("utf-8")
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

            def stream(self) -> None:
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                with mock.cond:
                    seen = len(mock.events)
                try:
                    self.chunk(":connected\n\n")
                    # Multi-line data and a CRLF line ending, as any SSE peer may send them.
                    self.chunk('event: position\r\ndata: {"x": 1,\ndata: "y": 2}\n\n')
                    while not mock.stopping.is_set():
                        with mock.cond:
                            if len(mock.events) == seen:
                                mock.cond.wait(mock.keepalive)
                            fresh = mock.events[seen:]
                            seen = len(mock.events)
                        if not fresh:
                            self.chunk(":keepalive\n\n")
                        for event, data in fresh:
                            self.chunk(f"event: {event}\ndata: {data}\n\n")
                except OSError:
                    pass  # the client went away

            def do_POST(self):
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if path == "/api/connect":
                    self.connect(body)
                    return
                if self.stall():
                    return
                if path not in ("/api/file/upload", "/api/file/open"):
                    if self.authorized():
                        self.reply(404, {"error": "Not found"})
                    return
                if not self.authorized():
                    return
                filename = self.headers.get("X-Filename", "upload.lbrn2")
                record = {
                    "path": path, "filename": filename, "body": body,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "group_shapes": self.headers.get("X-Group-Shapes"),
                }
                mock.uploads.append(record)
                if mock.verbose:
                    print(json.dumps({"path": path, "filename": filename, "bytes": len(body),
                                      "svg": body.lstrip().startswith((b"<?xml", b"<svg")),
                                      "group_shapes": record["group_shapes"], "token_valid": True}), flush=True)
                self.reply(202, {"status": "accepted", "filename": filename})
                if path == "/api/file/open" and not filename.lower().endswith(NATIVE):
                    mock.imported({"success": False, "error": "Öffnen fehlgeschlagen"})
                elif mock.import_success and filename not in mock.failing:
                    mock.imported({"success": True})
                else:
                    mock.imported({"success": False, "error": "Unsupported file\nformat"})

            def connect(self, body: bytes) -> None:
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    self.reply(403, {"error": "Forbidden"})
                    return
                try:
                    request = json.loads(body)
                    caps = request["capabilities"]
                    ok = (isinstance(request.get("application_name"), str) and isinstance(caps, list)
                          and caps and all(cap in CAPABILITIES for cap in caps))
                except (ValueError, KeyError, TypeError):
                    ok = False
                if not ok:
                    self.reply(400, {"error": "Bad Request"})
                elif mock.pair != "approve":
                    self.reply(403, {"error": "Connection declined"})
                else:
                    mock.capabilities = set(caps)
                    self.reply(200, {"status": "ok", "secret": mock.secret})

        return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mock LightBurn REST API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19520)
    parser.add_argument("--secret", default="aB3dE7fG9hJ1kL5m")
    parser.add_argument("--seconds", type=float, default=600, help="stop after this many seconds")
    args = parser.parse_args(argv)
    mock = MockLightBurn(args.secret, args.host, args.port, verbose=True).start()
    print(json.dumps({"listening": f"{args.host}:{mock.port}"}), flush=True)
    try:
        mock.stopping.wait(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        mock.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
