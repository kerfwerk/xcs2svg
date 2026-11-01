"""Stateless forwarder to the LightBurn 2.2 REST API (stdlib only).

LightBurn serves the API over plain HTTP on port 19520 and sends no CORS headers, so the browser
cannot call it directly. The browser keeps the shared secret as a non-extractable WebCrypto key and
only hands this server a token, ``hex(HMAC-SHA256(secret, floor(unix_time / 60)))``, which LightBurn
accepts for the current and the previous minute. This module never sees the secret, stores nothing
and never logs or echoes the token.

Where requests may go is fixed by the server's environment (``LIGHTBURN_HOSTS``, ``LIGHTBURN_PORTS``);
the client only picks one of those values. The URL is always built here as ``http://<host>:<port>``.

Uploads answer 202 at once; whether the import worked is only reported as a ``file_imported`` event
on the SSE stream ``GET /api/events``, so that stream is opened before the file is sent.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import re
import socket
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Mapping

DEFAULT_HOSTS = "host.containers.internal,host.docker.internal"
DEFAULT_PORTS = "19520"
MODES = ("upload", "open")  # import into the current project / replace the project

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 10.0  # idle time per read, and the overall limit of one small request
UPLOAD_SECONDS = 30.0  # overall limit for sending up to MAX_UPLOAD_MB and reading the answer
CONFIRM_SECONDS = 15.0  # wait for file_imported after the 202
EVENTS_IDLE = 40.0  # idle limit on GET /api/events: LightBurn sends ":keepalive" only every 30 s
MAX_RESPONSE = 64 * 1024
MAX_SSE_LINE = 64 * 1024
MAX_SSE_DATA = 256 * 1024
MAX_TARGETS = 16

_LABEL = re.compile(r"(?!-)[A-Za-z0-9_-]{1,63}(?<!-)")
_PORT = re.compile(r"[0-9]{1,5}")
TOKEN = re.compile(r"[0-9a-f]{64}")
_FILE_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]+")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


class ConfigError(ValueError):
    """LIGHTBURN_HOSTS / LIGHTBURN_PORTS are invalid; raised at startup."""


class RequestError(ValueError):
    """The client asked for something not allowed (unknown target, malformed token); German, safe to show."""


# --- allowlist from the environment ------------------------------------------

def validate_host(value: str) -> str:
    """A hostname or an IPv4/IPv6 literal; nothing that could change the URL we build."""
    host = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if not host or len(host) > 253 or not host.isascii() or not host.isprintable() or " " in host:
        raise ConfigError(f"Ungültiger Host: {value!r}")
    if any(char in host for char in "/?#@%\\[]"):
        raise ConfigError(f"Host ohne Schema, Pfad oder Benutzer angeben: {value!r}")
    if ":" in host:
        try:
            return str(ipaddress.IPv6Address(host))
        except ValueError:
            raise ConfigError(f"Host ohne Port angeben (Ports gehören in LIGHTBURN_PORTS): {value!r}") from None
    if re.fullmatch(r"[0-9.]+", host):
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError:
            raise ConfigError(f"Ungültige IP-Adresse: {value!r}") from None
    if not all(_LABEL.fullmatch(label) for label in host.split(".")):
        raise ConfigError(f"Ungültiger Hostname: {value!r}")
    return host.lower()


def _items(raw: str, name: str) -> list[str]:
    items = [item.strip(" ") for item in raw.split(",")]
    items = [item for item in items if item]
    if not items:
        raise ConfigError(f"{name} ist leer.")
    if len(items) > MAX_TARGETS:
        raise ConfigError(f"{name} enthält zu viele Einträge (maximal {MAX_TARGETS}).")
    return items


def parse_hosts(raw: str) -> tuple[str, ...]:
    hosts = [validate_host(item) for item in _items(raw, "LIGHTBURN_HOSTS")]
    return tuple(dict.fromkeys(hosts))


def parse_ports(raw: str) -> tuple[int, ...]:
    ports = []
    for item in _items(raw, "LIGHTBURN_PORTS"):
        if not _PORT.fullmatch(item) or not 1 <= int(item) <= 65535:
            raise ConfigError(f"Ungültiger Port in LIGHTBURN_PORTS: {item!r}")
        ports.append(int(item))
    return tuple(dict.fromkeys(ports))


@dataclass(frozen=True)
class Targets:
    hosts: tuple[str, ...]
    ports: tuple[int, ...]

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Targets:
        return cls(parse_hosts(environ.get("LIGHTBURN_HOSTS", DEFAULT_HOSTS)),
                   parse_ports(environ.get("LIGHTBURN_PORTS", DEFAULT_PORTS)))

    def public(self) -> dict:
        return {"hosts": list(self.hosts), "ports": list(self.ports)}

    def select(self, host: str | None, port: str | None) -> tuple[str, int]:
        """The allowlisted (host, port) the client named; anything else is a RequestError."""
        chosen = next((item for item in self.hosts if item == (host or "").lower()), None)
        if chosen is None:
            raise RequestError("Dieser LightBurn-Host ist auf dem Server nicht freigegeben "
                               "(LIGHTBURN_HOSTS). Bitte in den Einstellungen neu wählen.")
        if not (port and _PORT.fullmatch(port) and int(port) in self.ports):
            raise RequestError("Dieser LightBurn-Port ist auf dem Server nicht freigegeben "
                               "(LIGHTBURN_PORTS). Bitte in den Einstellungen neu wählen.")
        return chosen, int(port)


def check_token(value: str | None) -> str:
    if not value or not TOKEN.fullmatch(value):
        raise RequestError("Ungültiges LightBurn-Token (erwartet: 64 Hex-Zeichen).")
    return value


def check_mode(value: str | None) -> str:
    if value not in MODES:
        raise RequestError("Unbekannter Modus (erlaubt: upload oder open).")
    return value


def token(secret: str, now: float | None = None) -> str:
    """The token as in the LightBurn docs. The server never calls this; the browser does the same."""
    minute = int((time.time() if now is None else now) // 60)
    return hmac.new(secret.encode("utf-8"), str(minute).encode("ascii"), hashlib.sha256).hexdigest()


def svg_filename(name: str | None) -> str:
    """ASCII-only file name ending in .svg, safe as an HTTP header value (X-Filename)."""
    stem = re.split(r"[/\\]", name or "")[-1]
    if stem.lower().endswith(".svg"):
        stem = stem[:-4]
    stem = unicodedata.normalize("NFKD", stem.translate(_UMLAUTS)).encode("ascii", "ignore").decode("ascii")
    stem = _FILE_UNSAFE.sub("_", stem).strip(" ._")[:80]
    return f"{stem or 'zeichnung'}.svg"


# --- HTTP with a hard deadline -----------------------------------------------

class _DeadlineIO(io.RawIOBase):
    """Reads from the socket, but never past the connection's overall deadline.

    A plain socket timeout restarts with every received byte, so a peer dripping one byte at a
    time could otherwise hold the thread for as long as it likes.
    """

    def __init__(self, owner: _DeadlineSocket):
        self.owner = owner

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        self.owner.arm()
        return self.owner.sock.recv_into(buffer)


class _DeadlineSocket:
    def __init__(self, sock: socket.socket, idle: float, deadline: float):
        self.sock = sock
        self.idle = idle
        self.deadline = deadline

    def arm(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline exceeded")
        self.sock.settimeout(min(self.idle, left))

    def sendall(self, data) -> None:
        self.arm()  # since Python 3.5 the timeout bounds the whole sendall
        self.sock.sendall(data)

    def makefile(self, mode: str = "rb", *args, **kwargs):
        return io.BufferedReader(_DeadlineIO(self))

    def shutdown(self, how: int) -> None:
        self.sock.shutdown(how)

    def close(self) -> None:
        self.sock.close()


class _Connection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, connect_timeout: float, idle: float, seconds: float):
        super().__init__(host, port, timeout=connect_timeout)
        self.idle = idle
        self.seconds = seconds

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), self.timeout)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.sock = _DeadlineSocket(raw, self.idle, time.monotonic() + self.seconds)


# --- SSE -----------------------------------------------------------------------

class SseParser:
    """Line-based Server-Sent-Events parser (event:/data: fields, comments, multi-line data)."""

    def __init__(self):
        self.event = ""
        self.data: list[str] = []
        self.size = 0

    def feed(self, line: str) -> tuple[str, str] | None:
        """Feed one line without its line ending; returns (event, data) when an event is complete."""
        if line == "":
            if not self.data and not self.event:
                return None
            event = (self.event or "message", "\n".join(self.data))
            self.event, self.data, self.size = "", [], 0
            return event
        if line.startswith(":"):
            return None  # comment, e.g. ":connected" or ":keepalive"
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            self.event = value
        elif name == "data":
            self.size += len(value) + 1
            if self.size <= MAX_SSE_DATA:
                self.data.append(value)
        return None


class _EventWatcher(threading.Thread):
    """Holds GET /api/events open and remembers the first file_imported event after arm()."""

    def __init__(self, client: Client):
        super().__init__(daemon=True, name="lightburn-events")
        self.client = client
        self.ready = threading.Event()  # set once the stream is open or has failed
        self.done = threading.Event()  # set when file_imported arrived, the stream ended or close()
        self.available = False
        self.armed = False
        self.result: dict | None = None
        self.lock = threading.Lock()
        self.conn: _Connection | None = None

    def run(self) -> None:
        try:
            # A quiet stream (no laser attached) only carries a keepalive every 30 s, so it gets its own
            # idle limit; the overall deadline still ends it.
            conn = self.client.connection(self.client.connect_timeout + self.client.upload_seconds
                                          + self.client.confirm_seconds, idle=self.client.events_idle)
            with self.lock:
                if self.done.is_set():
                    conn.close()
                    return
                self.conn = conn
            conn.request("GET", "/api/events", headers={**self.client.auth(), "Accept": "text/event-stream"})
            response = conn.getresponse()
            if response.status != 200:
                return  # 401: the key lacks "state"; the upload goes ahead without confirmation
            self.available = True
            self.ready.set()
            parser = SseParser()
            while not self.done.is_set():
                raw = response.readline(MAX_SSE_LINE)
                if not raw:
                    return
                event = parser.feed(raw.decode("utf-8", "replace").rstrip("\r\n"))
                if event and event[0] == "file_imported" and self.armed:
                    try:
                        payload = json.loads(event[1])
                    except ValueError:
                        payload = None
                    self.result = payload if isinstance(payload, dict) else {"success": False}
                    return
        except (OSError, http.client.HTTPException, ValueError):
            return
        finally:
            self.ready.set()
            self.done.set()

    def close(self) -> None:
        with self.lock:
            self.done.set()
            conn = self.conn
        if conn is not None:
            if conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)  # wakes up a blocked readline
                except OSError:
                    pass
            conn.close()


# --- client --------------------------------------------------------------------

@dataclass
class SendResult:
    accepted: bool
    confirmed: bool | None  # None: LightBurn took the file but the result could not be confirmed
    message: str

    def public(self) -> dict:
        return {"accepted": self.accepted, "confirmed": self.confirmed, "message": self.message}


class LightBurnError(Exception):
    """A German message for the user (never contains a token)."""


UNAUTHORIZED = ("Schlüssel ungültig, widerrufen, Uhrzeit falsch oder ohne Berechtigung „{scope}“. "
                "Bitte die Uhrzeit prüfen oder neu koppeln.")


class Client:
    """One forwarded request to an allowlisted LightBurn; holds only a short-lived token."""

    def __init__(self, host: str, port: int, bearer: str, connect_timeout: float = CONNECT_TIMEOUT,
                 read_timeout: float = READ_TIMEOUT, confirm_seconds: float = CONFIRM_SECONDS,
                 upload_seconds: float = UPLOAD_SECONDS, events_idle: float = EVENTS_IDLE):
        self.host = host
        self.port = port
        self._bearer = check_token(bearer)
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.confirm_seconds = confirm_seconds
        self.upload_seconds = upload_seconds
        self.events_idle = events_idle

    def __repr__(self) -> str:  # keeps the token out of tracebacks and debug output
        return f"Client({self.where})"

    @property
    def where(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer}"}

    def connection(self, seconds: float, idle: float | None = None) -> _Connection:
        conn = _Connection(self.host, self.port, self.connect_timeout,
                           self.read_timeout if idle is None else idle, seconds)
        try:
            conn.connect()
        except BaseException:
            conn.close()
            raise
        return conn

    def unreachable(self, exc: BaseException) -> LightBurnError:
        hints = ("Läuft LightBurn (ab Version 2.2)? Ist in LightBurn unter Einstellungen > Erweiterungen "
                 "„Allow API Access From Network“ eingeschaltet? Lässt die Windows-Firewall Port "
                 f"{self.port} durch?")
        if isinstance(exc, socket.gaierror):
            return LightBurnError(f"Der Host „{self.host}“ ist im Container unbekannt (Name nicht auflösbar). "
                                  "Bitte extra_hosts in compose.yaml bzw. LIGHTBURN_HOSTS prüfen.")
        if isinstance(exc, ConnectionRefusedError):
            return LightBurnError(f"LightBurn ist unter {self.where} nicht erreichbar (Verbindung abgelehnt). {hints}")
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return LightBurnError(f"LightBurn unter {self.where} antwortet nicht (Zeitüberschreitung). {hints}")
        if isinstance(exc, http.client.HTTPException):
            return LightBurnError(f"Unter {self.where} antwortet kein LightBurn (ungültige HTTP-Antwort).")
        return LightBurnError(f"Verbindung zu LightBurn unter {self.where} fehlgeschlagen "
                              f"({type(exc).__name__}). {hints}")

    def call(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
             seconds: float | None = None) -> tuple[int, bytes]:
        try:
            conn = self.connection(self.read_timeout if seconds is None else seconds)
        except OSError as exc:
            raise self.unreachable(exc) from None
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read(MAX_RESPONSE)
        except (OSError, http.client.HTTPException) as exc:
            raise self.unreachable(exc) from None
        finally:
            conn.close()

    def test(self) -> tuple[bool, str]:
        try:
            self.call("GET", "/")
            status, body = self.call("GET", "/api/status", headers=self.auth())
        except LightBurnError as exc:
            return False, str(exc)
        if status == 401:
            return False, "LightBurn ist erreichbar, lehnt den Schlüssel aber ab: " + UNAUTHORIZED.format(scope="state")
        if status != 200:
            return False, f"LightBurn hat unerwartet mit HTTP {status} geantwortet."
        device = ""
        try:
            name = json.loads(body).get("device_name")
            if isinstance(name, str):
                device = _clean(name, 60)
        except (ValueError, AttributeError, UnicodeDecodeError):
            pass
        return True, f"Verbindung zu LightBurn unter {self.where} in Ordnung." + (
            f" Gerät: {device}." if device else "")

    def send(self, data: bytes, filename: str, mode: str = "upload", group_shapes: bool = False) -> SendResult:
        mode = check_mode(mode)
        path = "/api/file/upload" if mode == "upload" else "/api/file/open"
        headers = {**self.auth(), "Content-Type": "application/octet-stream", "X-Filename": svg_filename(filename)}
        if mode == "upload":
            headers["X-Group-Shapes"] = "true" if group_shapes else "false"

        watcher = _EventWatcher(self)
        watcher.start()  # open the stream first so the event cannot arrive before we listen
        watcher.ready.wait(self.connect_timeout + self.read_timeout)
        watcher.armed = True
        try:
            status, body = self.call("POST", path, body=bytes(data), headers=headers, seconds=self.upload_seconds)
            if status == 401:
                return SendResult(False, None, "LightBurn lehnt den Schlüssel ab: " + UNAUTHORIZED.format(scope="upload"))
            if status not in (200, 202):
                return SendResult(False, None, _http_message(status, body))
            if not watcher.available:
                return SendResult(True, None, "An LightBurn übergeben. Eine Bestätigung ist nicht möglich "
                                              "(Schlüssel ohne Berechtigung „state“).")
            start = time.monotonic()
            ended = watcher.done.wait(self.confirm_seconds)  # True: event, or the stream ended early
            waited = time.monotonic() - start
            event = watcher.result
        except LightBurnError as exc:
            return SendResult(False, None, str(exc))
        finally:
            watcher.close()
        if event is None and ended:
            return SendResult(True, None, "An LightBurn übergeben, aber LightBurn hat die Ereignis-Verbindung "
                                          f"nach {waited:.1f} s beendet, bevor der Import bestätigt war.")
        if event is None:
            return SendResult(True, None, "An LightBurn übergeben, aber LightBurn hat den Import nicht "
                                          f"innerhalb von {self.confirm_seconds:g} s bestätigt.")
        if mode == "open":
            # Documented: /api/file/open reports success=false for every non-native format (SVG) even
            # when the file opened fine, so the event says nothing either way.
            return SendResult(True, None, "An LightBurn übergeben. Beim Ersetzen des Projekts meldet LightBurn "
                                          "für SVG kein verlässliches Ergebnis – bitte in LightBurn nachsehen.")
        if event.get("success") is True:
            return SendResult(True, True, "In LightBurn importiert.")
        error = event.get("error")
        detail = f": {_clean(error, 200)}" if isinstance(error, str) and _clean(error, 200) else ""
        return SendResult(True, False, f"LightBurn konnte die Datei nicht importieren{detail}.")


def _clean(text: str, length: int) -> str:
    return " ".join("".join(char if char.isprintable() else " " for char in text).split())[:length]


def _http_message(status: int, body: bytes) -> str:
    detail = ""
    try:
        error = json.loads(body).get("error")
        if isinstance(error, str):
            detail = _clean(error, 200)
    except (ValueError, AttributeError, UnicodeDecodeError):
        pass
    suffix = f": {detail}" if detail else ""
    if status == 404:
        return "LightBurn kennt diesen Befehl nicht (HTTP 404) – ist LightBurn 2.2 oder neuer installiert?"
    if status == 403:
        return f"LightBurn verweigert den Zugriff (HTTP 403{suffix})."
    if status == 413:
        return "Die Datei ist für LightBurn zu groß (HTTP 413)."
    if status >= 500:
        return f"LightBurn hat einen internen Fehler gemeldet (HTTP {status}{suffix})."
    return f"LightBurn hat die Datei abgelehnt (HTTP {status}{suffix})."
