"""Small web UI and JSON API around the XCS converter (stdlib only).

Everything happens in memory, so the container can run with a read-only
root filesystem.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import lightburn, xcs2svg

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/einstellungen": ("settings.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/lightburn.js": ("lightburn.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self'; img-src 'self' blob: data:; script-src 'self'; "
        "style-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

_CONTENT_LENGTH = re.compile(r"[0-9]{1,12}")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

log = logging.getLogger("xcs2svg")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


def parse_public_hosts(raw: str) -> tuple[str, ...]:
    """PUBLIC_HOSTS: extra names the app is served under (e.g. a reverse-proxy domain or the LAN IP)."""
    items = [item.strip(" ") for item in raw.split(",") if item.strip(" ")]
    if len(items) > lightburn.MAX_TARGETS:
        raise lightburn.ConfigError(f"PUBLIC_HOSTS enthält zu viele Einträge (maximal {lightburn.MAX_TARGETS}).")
    return tuple(dict.fromkeys(lightburn.validate_host(item) for item in items))


def host_name(value: str) -> str | None:
    """The name part of a Host header ("[::1]:8080" -> "::1"), normalised; None if malformed."""
    if value.startswith("["):
        name, bracket, rest = value[1:].partition("]")
        if not bracket or (rest and not re.fullmatch(r":[0-9]{1,5}", rest)):
            return None
    else:
        name, colon, port = value.partition(":")
        if colon and not re.fullmatch(r"[0-9]{1,5}", port):
            return None
    try:
        return lightburn.validate_host(name)
    except lightburn.ConfigError:
        return None


def load_static() -> dict[str, tuple[bytes, str]]:
    return {route: ((STATIC_DIR / name).read_bytes(), mime)
            for route, (name, mime) in STATIC_FILES.items() if (STATIC_DIR / name).is_file()}


class _DeadlineReader(io.RawIOBase):
    """Socket reader with an overall deadline on top of the idle timeout of each recv.

    The plain socket timeout restarts with every byte, so a client sending one byte
    every few seconds could otherwise keep a connection (and its thread) forever.
    """

    def __init__(self, sock, idle: float):
        self.sock = sock
        self.idle = idle
        self.deadline: float | None = None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        timeout = self.idle
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("deadline exceeded")
        self.sock.settimeout(timeout)
        try:
            return self.sock.recv_into(buffer)
        finally:
            self.sock.settimeout(self.idle)  # writes keep the normal timeout


class Handler(BaseHTTPRequestHandler):
    server_version = "xcs2svg"
    sys_version = ""
    timeout = 30  # idle time per read/write
    header_seconds = 20  # request line + headers, including keep-alive idle time
    body_grace_seconds = 10  # an upload may lag this far behind min_upload_rate
    min_upload_rate = 64 * 1024  # bytes per second
    queue_seconds = 20  # wait for a free upload or conversion slot, then 503
    linger_seconds = 2.0  # after an error reply: keep reading what the client still sends, then close
    max_upload = 25 * 1024 * 1024
    static: dict[str, tuple[bytes, str]] = {}
    uploads = threading.BoundedSemaphore(4)  # request bodies held in memory
    slots = threading.BoundedSemaphore(2)  # conversions running at the same time
    sends = threading.BoundedSemaphore(2)  # LightBurn requests forwarded at the same time
    # One upload per LightBurn at a time: file_imported carries no file name or request id, so two
    # overlapping uploads to the same LightBurn could swap their results. Keys come from the allowlist.
    forwards: dict[tuple[str, int], threading.Lock] = {}
    forwards_guard = threading.Lock()
    public_hosts: tuple[str, ...] = ()  # PUBLIC_HOSTS; localhost, 127.0.0.1 and [::1] always work
    targets = lightburn.Targets.from_env({})  # the defaults; make_server reads LIGHTBURN_HOSTS/_PORTS
    lightburn_timeouts: dict[str, float] = {}  # tests shorten the LightBurn timeouts here

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self.reader = _DeadlineReader(self.connection, self.timeout)
        self.rfile = io.BufferedReader(self.reader)

    def handle_one_request(self) -> None:
        self.reader.deadline = time.monotonic() + self.header_seconds
        super().handle_one_request()

    def finish(self) -> None:
        super().finish()  # flushes the reply
        if self.__dict__.get("linger"):
            self.drain()

    def drain(self) -> None:
        """Lingering close after an error reply that may have left the request body unread.

        Closing a socket that still has unread data makes the kernel answer with a reset, and the
        client may then lose the reply we just sent (Windows: WinError 10053/10054). So we stop
        writing, read and drop what the client still sends for a short, bounded time, then close.
        """
        deadline = time.monotonic() + self.linger_seconds
        left = self.max_upload
        try:
            self.connection.shutdown(socket.SHUT_WR)
            while left > 0:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                self.connection.settimeout(timeout)
                chunk = self.connection.recv(min(left, 64 * 1024))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def log_request(self, code="-", size="-") -> None:
        status = code.value if isinstance(code, HTTPStatus) else code
        path = urlsplit(self.path).path[:200] if isinstance(getattr(self, "path", None), str) else "-"
        size = self.__dict__.pop("response_size", size)
        log.info("%s %s %s %s", getattr(self, "command", None) or "-", path, status, size)

    def log_error(self, format, *args) -> None:  # noqa: A002 - signature of the base class
        pass  # the base class would echo raw request data; log_request covers the status

    def send_body(self, status: int, body: bytes, mime: str, cache: str = "no-store") -> None:
        self.response_size = len(body)
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_body(status, body, "application/json; charset=utf-8")

    def send_error_json(self, status: int, message: str) -> None:
        self.close_connection = True
        self.linger = True  # the request body may be (partly) unread
        self.send_json(status, {"error": message})

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self.send_body(200, b"ok", "text/plain; charset=utf-8")
        elif path == "/api/limits":
            self.send_json(200, {"maxUploadMb": self.max_upload // (1024 * 1024)})
        elif path == "/api/lightburn/targets":
            if not self.foreign_host():
                self.send_json(200, self.targets.public())
        elif path in self.static:
            body, mime = self.static[path]
            self.send_body(200, body, mime, cache="no-cache")
        else:
            self.send_body(404, "Nicht gefunden.".encode("utf-8"), "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/convert":
            self.handle_convert()
        elif path in ("/api/lightburn/test", "/api/lightburn/send"):
            self.handle_lightburn(path.rsplit("/", 1)[1])
        else:
            self.send_error_json(404, "Nicht gefunden.")

    def body_length(self, content_types: tuple[str, ...]) -> int | None:
        """Validated Content-Length of a file upload, or None after an error response."""
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type not in content_types:
            self.send_error_json(415, "Bitte die Datei als application/octet-stream senden.")
            return None
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            self.send_error_json(411, "Content-Length fehlt.")
            return None
        length_header = (self.headers.get("Content-Length") or "").strip()
        if not _CONTENT_LENGTH.fullmatch(length_header):  # ASCII only: isdigit() also accepts "\u00b2"
            self.send_error_json(411, "Content-Length fehlt oder ist ungültig.")
            return None
        length = int(length_header)
        if length > self.max_upload:
            limit = self.max_upload // (1024 * 1024)
            self.send_error_json(413, f"Die Datei ist zu groß (maximal {limit} MB).")
            return None
        if length == 0:
            self.send_error_json(400, "Die Datei ist leer.")
            return None
        return length

    def handle_convert(self) -> None:
        length = self.body_length(("application/octet-stream",))
        if length is None:
            return
        if not self.uploads.acquire(timeout=self.queue_seconds):
            self.send_error_json(503, "Der Server ist ausgelastet. Bitte gleich noch einmal versuchen.")
            return
        try:
            self.convert_upload(length)
        finally:
            self.uploads.release()

    # --- LightBurn: stateless forwarding; the browser holds the key, we only ever see 1-minute tokens ---

    def foreign_host(self) -> bool:
        """DNS-rebinding guard: only answer under names the app is really served under (sends the 421).

        A page on a name the attacker rebinds to 127.0.0.1 is same-origin in the browser and would
        pass the Origin check below, so the Host itself must be one we know.
        """
        name = host_name(self.headers.get("Host") or "")
        if name is not None and (name in LOCAL_HOSTS or name in self.public_hosts):
            return False
        self.send_error_json(421, "Anfrage abgelehnt: unbekannter Host-Name. Die App über http://localhost "
                                  "öffnen oder den Namen in PUBLIC_HOSTS eintragen.")
        return True

    def cross_site(self) -> bool:
        """CSRF guard: a custom header (forces a CORS preflight we never approve) and a same-origin Origin."""
        if self.headers.get("X-Requested-With") != "xcs2svg":
            return True
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return True
        origin = self.headers.get("Origin")
        if origin is None:
            return False
        host = self.headers.get("Host") or ""
        # The server speaks plain HTTP; behind a TLS proxy the page origin is https:// with the same Host.
        return not host or origin not in (f"http://{host}", f"https://{host}")

    def forward_lock(self, host: str, port: int) -> threading.Lock:
        with self.forwards_guard:
            return self.forwards.setdefault((host, port), threading.Lock())

    def handle_lightburn(self, action: str) -> None:
        if self.foreign_host():
            return
        if self.cross_site():
            self.send_error_json(403, "Anfrage abgelehnt (nur von dieser Seite aus erlaubt).")
            return
        group = self.headers.get("X-LightBurn-Group", "false")
        try:
            host, port = self.targets.select(self.headers.get("X-LightBurn-Host"),
                                             self.headers.get("X-LightBurn-Port"))
            client = lightburn.Client(host, port, self.headers.get("X-LightBurn-Token"), **self.lightburn_timeouts)
            mode = lightburn.check_mode(self.headers.get("X-LightBurn-Mode", "upload")) if action == "send" else ""
            if group not in ("true", "false"):
                raise lightburn.RequestError("X-LightBurn-Group muss true oder false sein.")
        except lightburn.RequestError as exc:
            self.send_error_json(400, str(exc))
            return
        if action == "test":
            if (self.headers.get("Content-Length") or "0").strip() != "0" or self.headers.get("Transfer-Encoding"):
                self.send_error_json(400, "Für den Verbindungstest bitte keine Daten senden.")
                return
            # Never a conversion slot: a slow LightBurn must not block conversions (and vice versa).
            if not self.sends.acquire(timeout=self.queue_seconds):
                self.send_error_json(503, "Der Server ist ausgelastet. Bitte gleich noch einmal versuchen.")
                return
            try:
                ok, message = client.test()
            finally:
                self.sends.release()
            self.send_json(200, {"ok": ok, "message": message})
            return
        length = self.body_length(("application/octet-stream", "image/svg+xml"))
        if length is None:
            return
        # The body is read under an upload slot (as for conversions) before a send slot is taken,
        # so slow uploads cannot block sending.
        if not self.uploads.acquire(timeout=self.queue_seconds):
            self.send_error_json(503, "Der Server ist ausgelastet. Bitte gleich noch einmal versuchen.")
            return
        try:
            self.forward_upload(client, length, mode, group == "true")
        finally:
            self.uploads.release()

    def forward_upload(self, client: lightburn.Client, length: int, mode: str, group: bool) -> None:
        try:
            data = self.read_body(length)
        except TimeoutError:
            self.send_error_json(408, "Die Übertragung der Datei hat zu lange gedauert.")
            return
        except OSError:
            self.close_connection = True
            return
        if len(data) != length:
            self.send_error_json(400, "Die Datei wurde unvollständig übertragen.")
            return
        if b"<svg" not in bytes(data[:4096]):
            self.send_error_json(400, "Es können nur SVG-Dateien an LightBurn gesendet werden.")
            return
        filename = unquote(self.headers.get("X-Filename", ""))[:255]
        lock = self.forward_lock(client.host, client.port)
        if not lock.acquire(timeout=self.queue_seconds):
            self.send_error_json(503, "An diesen LightBurn wird gerade schon gesendet. Bitte gleich noch einmal versuchen.")
            return
        try:
            if not self.sends.acquire(timeout=self.queue_seconds):
                self.send_error_json(503, "Der Server ist ausgelastet. Bitte gleich noch einmal versuchen.")
                return
            try:
                result = client.send(data, filename, mode, group)
            finally:
                self.sends.release()
        finally:
            lock.release()
        del data
        self.send_json(200, result.public())

    def read_body(self, length: int) -> bytearray:
        """Reads the whole body; a sender slower than min_upload_rate runs into the deadline."""
        start = time.monotonic()
        data = bytearray()
        while len(data) < length:
            self.reader.deadline = start + self.body_grace_seconds + len(data) / self.min_upload_rate
            chunk = self.rfile.read1(min(length - len(data), 1024 * 1024))
            if not chunk:
                break
            data += chunk
        self.reader.deadline = None
        return data

    def convert_upload(self, length: int) -> None:
        # The body is read before a conversion slot is taken, so slow uploads cannot block conversions.
        try:
            data = self.read_body(length)
        except TimeoutError:
            self.send_error_json(408, "Die Übertragung der Datei hat zu lange gedauert.")
            return
        except OSError:
            self.close_connection = True
            return
        if len(data) != length:
            self.send_error_json(400, "Die Datei wurde unvollständig übertragen.")
            return
        if not self.slots.acquire(timeout=self.queue_seconds):
            self.send_error_json(503, "Der Server ist ausgelastet. Bitte gleich noch einmal versuchen.")
            return
        try:
            filename = unquote(self.headers.get("X-Filename", ""))[:255]
            result = xcs2svg.convert(data, filename)
        except xcs2svg.XcsError as exc:
            self.send_error_json(400, str(exc))
            return
        except Exception:
            log.exception("Konvertierung fehlgeschlagen")
            self.send_error_json(500, "Interner Fehler bei der Konvertierung.")
            return
        finally:
            self.slots.release()
        del data
        self.send_json(200, {
            "files": [{"name": f.name, "title": f.title, "svg": f.svg} for f in result.files],
            "warnings": result.warnings,
        })


class _Server(ThreadingHTTPServer):
    """One thread per connection, but never more than max_connections at once.

    Without a cap, idle connections exhaust the container's pid limit and no new
    thread (not even for the health check) can be started.
    """

    def __init__(self, address, handler, max_connections: int):
        self.connections = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address) -> None:
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)  # full: close at once instead of queueing
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()  # the thread did not start
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # the client went away; no traceback per dropped connection in the log
        log.exception("Fehler bei der Verarbeitung einer Anfrage")


def make_server(host: str = "0.0.0.0", port: int = 8080, max_upload_mb: int = 25) -> ThreadingHTTPServer:
    """Raises lightburn.ConfigError if LIGHTBURN_HOSTS / LIGHTBURN_PORTS / PUBLIC_HOSTS are invalid."""
    parallel = _env_int("MAX_PARALLEL", 2)
    handler = type("ConfiguredHandler", (Handler,), {
        "max_upload": max_upload_mb * 1024 * 1024,
        "static": load_static(),
        "uploads": threading.BoundedSemaphore(_env_int("MAX_UPLOADS", parallel + 2)),
        "slots": threading.BoundedSemaphore(parallel),
        "sends": threading.BoundedSemaphore(_env_int("MAX_SENDS", 2)),
        "forwards": {},
        "targets": lightburn.Targets.from_env(os.environ),
        "public_hosts": parse_public_hosts(os.environ.get("PUBLIC_HOSTS", "")),
    })
    server = _Server((host, port), handler, _env_int("MAX_CONNECTIONS", 32))
    server.daemon_threads = False  # let running conversions finish on shutdown
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    port = _env_int("PORT", 8080)
    try:
        server = make_server(os.environ.get("HOST", "0.0.0.0"), port, _env_int("MAX_UPLOAD_MB", 25))
    except lightburn.ConfigError as exc:
        log.error("Ungültige Einstellung: %s", exc)
        sys.exit(2)

    def stop(signum, _frame):
        log.info("Signal %s empfangen, fahre herunter", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("XCS-zu-SVG läuft auf Port %s", port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("Beendet")


if __name__ == "__main__":
    main()
