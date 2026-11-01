"""Tests for the stateless LightBurn forwarder and its endpoints, against a mock LightBurn."""
import hashlib
import hmac
import http.client
import json
import logging
import os
import socket
import threading
import time
import unittest
from unittest import mock

from app import lightburn, server
from mock_lightburn import MockLightBurn

SECRET = "aB3dE7fG9hJ1kL5m"  # the example secret from the LightBurn docs
# hex(HMAC-SHA256("aB3dE7fG9hJ1kL5m", "28333333")) for unix time 1_700_000_000 (minute 28333333);
# the browser test (lightburn.js) must produce exactly this value as well.
KNOWN_TOKEN = "61b028425829f4009a6f97766b38e1fabb5a3cb255b5e428807c2b0fe41b4771"
SVG = (b'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="10mm" '
       b'height="10mm" viewBox="0 0 10 10"><rect width="5" height="5"/></svg>')
FAST = {"connect_timeout": 1.0, "read_timeout": 1.0, "confirm_seconds": 2.0, "upload_seconds": 1.5}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TokenTests(unittest.TestCase):
    def test_known_vector(self):
        minute = 1_700_000_000 // 60
        independent = hmac.new(SECRET.encode(), str(minute).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(minute, 28333333)
        self.assertEqual(independent, KNOWN_TOKEN)
        self.assertEqual(lightburn.token(SECRET, 1_700_000_000), KNOWN_TOKEN)
        self.assertEqual(lightburn.token(SECRET, 1_700_000_000 + 39.9), KNOWN_TOKEN)  # same minute

    def test_mock_accepts_current_and_previous_minute_only(self):
        lb = MockLightBurn(SECRET)
        try:
            now = 1_700_000_000
            self.assertTrue(lb.valid(f"Bearer {KNOWN_TOKEN}", now))
            self.assertTrue(lb.valid(f"Bearer {KNOWN_TOKEN}", now + 60))  # previous minute
            self.assertFalse(lb.valid(f"Bearer {KNOWN_TOKEN}", now + 120))
            self.assertFalse(lb.valid(f"Bearer {KNOWN_TOKEN}", now - 60))  # future token
            self.assertFalse(lb.valid(KNOWN_TOKEN, now))  # no "Bearer "
            self.assertFalse(lb.valid(f"Bearer {lightburn.token('other-secret', now)}", now))
            self.assertFalse(lb.valid(None, now))
        finally:
            lb.httpd.server_close()


class SseParserTests(unittest.TestCase):
    def feed(self, lines):
        parser = lightburn.SseParser()
        return [event for line in lines if (event := parser.feed(line))]

    def test_fields_comments_and_multiline_data(self):
        events = self.feed([":connected", "", "event: position", 'data: {"x": 1,', 'data: "y": 2}', "",
                            ":keepalive", "event:file_imported", 'data:{"success": true}', "", "data: plain", ""])
        self.assertEqual(events, [("position", '{"x": 1,\n"y": 2}'), ("file_imported", '{"success": true}'),
                                  ("message", "plain")])

    def test_data_is_bounded(self):
        parser = lightburn.SseParser()
        for _ in range(100):
            parser.feed("data: " + "x" * 10_000)
        event = parser.feed("")
        self.assertLessEqual(len(event[1]), lightburn.MAX_SSE_DATA)


class AllowlistTests(unittest.TestCase):
    def test_valid_hosts_and_ports(self):
        self.assertEqual(lightburn.parse_hosts(" host.containers.internal, HOST.docker.internal,192.168.1.5,[::1]"),
                         ("host.containers.internal", "host.docker.internal", "192.168.1.5", "::1"))
        self.assertEqual(lightburn.parse_ports("19520, 19521"), (19520, 19521))
        targets = lightburn.Targets.from_env({})
        self.assertEqual(targets.public(), {"hosts": ["host.containers.internal", "host.docker.internal"],
                                            "ports": [19520]})

    def test_invalid_hosts_are_rejected_at_startup(self):
        for raw in ("http://evil", "evil/path", "user@evil", "evil:19520", "evil\r\nX: 1", "evil\tx", "a b",
                    "", ",", "999.1.1.1", "-bad.example", "bad..example", "evil?x", "evil#x", "ev%41l", "exämple"):
            with self.subTest(raw=raw), self.assertRaises(lightburn.ConfigError):
                lightburn.parse_hosts(raw)

    def test_invalid_ports_are_rejected_at_startup(self):
        for raw in ("", "0", "65536", "19520x", "-1", "1e3", "19 520", "²"):
            with self.subTest(raw=raw), self.assertRaises(lightburn.ConfigError):
                lightburn.parse_ports(raw)

    def test_make_server_validates_the_environment(self):
        with mock.patch.dict(os.environ, {"LIGHTBURN_HOSTS": "http://169.254.169.254/latest"}):
            with self.assertRaises(lightburn.ConfigError):
                server.make_server("127.0.0.1", 0)
        with mock.patch.dict(os.environ, {"PUBLIC_HOSTS": "laser.example,evil/path"}):
            with self.assertRaises(lightburn.ConfigError):
                server.make_server("127.0.0.1", 0)
        with mock.patch.dict(os.environ, {"LIGHTBURN_PORTS": "80,abc"}):
            with self.assertRaises(lightburn.ConfigError):
                server.make_server("127.0.0.1", 0)

    def test_select_only_allows_listed_values(self):
        targets = lightburn.Targets(("host.containers.internal",), (19520,))
        self.assertEqual(targets.select("HOST.containers.internal", "19520"), ("host.containers.internal", 19520))
        for host, port in (("169.254.169.254", "19520"), ("host.containers.internal", "80"), (None, "19520"),
                           ("host.containers.internal", None), ("host.containers.internal", "019520x"),
                           ("host.containers.internal\r\nX: y", "19520"), ("host.containers.internal ", "19520")):
            with self.subTest(host=host, port=port), self.assertRaises(lightburn.RequestError):
                targets.select(host, port)

    def test_token_format(self):
        self.assertEqual(lightburn.check_token(KNOWN_TOKEN), KNOWN_TOKEN)
        for bad in (None, "", KNOWN_TOKEN.upper(), KNOWN_TOKEN[:-1], KNOWN_TOKEN + "0", KNOWN_TOKEN + "\n",
                    "g" * 64, SECRET):
            with self.subTest(bad=bad), self.assertRaises(lightburn.RequestError):
                lightburn.check_token(bad)

    def test_svg_filename(self):
        self.assertEqual(lightburn.svg_filename("Geschenkbox Größe.svg"), "Geschenkbox Groesse.svg")
        self.assertEqual(lightburn.svg_filename("../../etc/passwd"), "passwd.svg")
        self.assertEqual(lightburn.svg_filename("a\r\nX-Evil: 1"), "a_X-Evil_ 1.svg")
        self.assertEqual(lightburn.svg_filename(""), "zeichnung.svg")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.lb = MockLightBurn(SECRET).start()
        self.addCleanup(self.lb.stop)

    def client(self, secret=SECRET, port=None, **overrides):
        return lightburn.Client("127.0.0.1", port or self.lb.port, lightburn.token(secret), **{**FAST, **overrides})

    def test_connection_test_ok(self):
        ok, message = self.client().test()
        self.assertTrue(ok, message)
        self.assertIn("Mock Laser", message)
        self.assertEqual([path for _, path, _ in self.lb.requests], ["/", "/api/status"])
        self.assertIsNone(self.lb.requests[0][2])  # GET / goes out without the token

    def test_connection_test_401(self):
        ok, message = self.client(secret="wrong-secret").test()
        self.assertFalse(ok)
        self.assertIn("Schlüssel ungültig, widerrufen, Uhrzeit falsch", message)
        self.assertIn("„state“", message)

    def test_connection_refused(self):
        # Windows retries a refused connect for about 2 s; Linux (the container) refuses at once.
        ok, message = self.client(port=free_port(), connect_timeout=5.0).test()
        self.assertFalse(ok)
        self.assertIn("nicht erreichbar", message)
        self.assertIn("Allow API Access From Network", message)
        self.assertIn("Firewall", message)

    def test_upload_is_forwarded_exactly_and_confirmed(self):
        client = self.client()
        result = client.send(SVG, "Giftbox Birthday.svg", "upload", group_shapes=True)
        self.assertEqual(result.public(), {"accepted": True, "confirmed": True, "message": "In LightBurn importiert."})
        [upload] = self.lb.uploads
        self.assertEqual(upload["path"], "/api/file/upload")
        self.assertEqual(upload["body"], SVG)
        self.assertEqual(upload["filename"], "Giftbox Birthday.svg")
        self.assertEqual(upload["group_shapes"], "true")
        self.assertEqual(upload["content_type"], "application/octet-stream")
        self.assertEqual(upload["authorization"], f"Bearer {client._bearer}")
        # the event stream was opened (with the same token) before the upload
        paths = [path for _, path, _ in self.lb.requests]
        self.assertLess(paths.index("/api/events"), paths.index("/api/file/upload"))

    def test_group_shapes_false_and_name_forced_to_svg(self):
        self.client().send(SVG, "Zeichnung.xcs", "upload", group_shapes=False)
        [upload] = self.lb.uploads
        self.assertEqual(upload["group_shapes"], "false")
        self.assertEqual(upload["filename"], "Zeichnung.xcs.svg")

    def test_open_mode_reports_unknown_not_an_error(self):
        result = self.client().send(SVG, "a.svg", "open")
        [upload] = self.lb.uploads
        self.assertEqual(upload["path"], "/api/file/open")
        self.assertIsNone(upload["group_shapes"])  # no grouping header for open
        self.assertTrue(result.accepted)
        self.assertIsNone(result.confirmed)  # LightBurn says success=false for SVG even on success
        self.assertNotIn("Fehler", result.message)

    def test_failed_import_is_reported(self):
        self.lb.import_success = False
        result = self.client().send(SVG, "a.svg")
        self.assertTrue(result.accepted)
        self.assertIs(result.confirmed, False)
        self.assertIn("Unsupported file format", result.message)  # cleaned of control characters

    def test_key_without_state_can_send_but_not_test(self):
        self.lb.capabilities = {"upload"}  # e.g. an entry picked under "Manage API Security"
        result = self.client().send(SVG, "a.svg")
        self.assertTrue(result.accepted)
        self.assertIsNone(result.confirmed)
        self.assertIn("Bestätigung ist nicht möglich", result.message)
        self.assertEqual(len(self.lb.uploads), 1)
        ok, message = self.client().test()  # GET /api/status needs "state" as well
        self.assertFalse(ok)
        self.assertIn("„state“", message)

    def test_key_without_upload_is_rejected(self):
        self.lb.capabilities = {"state"}
        self.assertTrue(self.client().test()[0])
        result = self.client().send(SVG, "a.svg")
        self.assertFalse(result.accepted)
        self.assertIn("„upload“", result.message)
        self.assertEqual(self.lb.uploads, [])

    def test_quiet_stream_and_slow_import_are_still_confirmed(self):
        # Real LightBurn: ":keepalive" only every 30 s and the import finishes asynchronously, here
        # 1.6 s after the 202 - longer than the 1 s idle timeout of ordinary requests.
        self.assertEqual(self.lb.keepalive, 30.0)
        self.lb.import_delay = 1.6
        start = time.monotonic()
        result = self.client(confirm_seconds=4.0).send(SVG, "a.svg")
        self.assertEqual(result.public(), {"accepted": True, "confirmed": True, "message": "In LightBurn importiert."})
        self.assertLess(time.monotonic() - start, 4)

    def test_stream_ending_early_is_reported_as_such(self):
        real = lightburn._EventWatcher.run

        def run_and_end(watcher):  # LightBurn closes the stream right after ":connected"
            watcher.client.events_idle = 0.3
            real(watcher)

        self.lb.import_delay = 3.0
        with mock.patch.object(lightburn._EventWatcher, "run", run_and_end):
            result = self.client(confirm_seconds=2.0).send(SVG, "a.svg")
        self.assertTrue(result.accepted)
        self.assertIsNone(result.confirmed)
        self.assertIn("Ereignis-Verbindung", result.message)
        self.assertNotIn("innerhalb von", result.message)

    def test_upload_401(self):
        result = self.client(secret="wrong-secret").send(SVG, "a.svg")
        self.assertFalse(result.accepted)
        self.assertIn("„upload“", result.message)
        self.assertEqual(self.lb.uploads, [])

    def test_send_connection_refused(self):
        result = self.client(port=free_port(), connect_timeout=5.0).send(SVG, "a.svg")
        self.assertFalse(result.accepted)
        self.assertIn("Verbindung abgelehnt", result.message)

    def test_hanging_peer_times_out(self):
        self.lb.hang = True
        start = time.monotonic()
        result = self.client().send(SVG, "a.svg")
        self.assertFalse(result.accepted)
        self.assertIn("Zeitüberschreitung", result.message)
        self.assertLess(time.monotonic() - start, 6)
        ok, message = self.client().test()
        self.assertFalse(ok)
        self.assertIn("Zeitüberschreitung", message)

    def test_drip_feeding_peer_hits_the_hard_deadline(self):
        self.lb.drip = True  # one byte every 0.1 s: the idle timeout alone would never fire
        start = time.monotonic()
        result = self.client(read_timeout=0.5, upload_seconds=1.0).send(SVG, "a.svg")
        elapsed = time.monotonic() - start
        self.assertFalse(result.accepted)
        self.assertIn("Zeitüberschreitung", result.message)
        self.assertLess(elapsed, 4)
        # the event watcher thread is gone as well
        deadline = time.monotonic() + 3
        while any(t.name == "lightburn-events" for t in threading.enumerate()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(any(t.name == "lightburn-events" for t in threading.enumerate()))

    def test_no_confirmation_within_time(self):
        with mock.patch.object(self.lb, "broadcast"):  # LightBurn never sends file_imported
            result = self.client(confirm_seconds=0.5).send(SVG, "a.svg")
        self.assertTrue(result.accepted)
        self.assertIsNone(result.confirmed)
        self.assertIn("nicht innerhalb von 0.5 s bestätigt", result.message)

    def test_repr_hides_token(self):
        client = self.client()
        self.assertNotIn(client._bearer, repr(client))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lb = MockLightBurn(SECRET).start()
        env = {"LIGHTBURN_HOSTS": "127.0.0.1,host.containers.internal", "LIGHTBURN_PORTS": f"{cls.lb.port},19520"}
        with mock.patch.dict(os.environ, env):
            cls.httpd = server.make_server("127.0.0.1", 0, max_upload_mb=1)
        cls.httpd.RequestHandlerClass.lightburn_timeouts = FAST
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(5)
        cls.lb.stop()

    def setUp(self):
        self.lb.uploads.clear()
        self.lb.requests.clear()
        self.capture = _Capture()
        logging.getLogger().addHandler(self.capture)
        self.addCleanup(logging.getLogger().removeHandler, self.capture)
        logging.getLogger("xcs2svg").setLevel(logging.DEBUG)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response, response.read()
        finally:
            conn.close()

    def lb_headers(self, token=None, **extra):
        headers = {"X-Requested-With": "xcs2svg", "X-LightBurn-Host": "127.0.0.1",
                   "X-LightBurn-Port": str(self.lb.port), "X-LightBurn-Token": token or lightburn.token(SECRET)}
        headers.update(extra)
        return headers

    def send(self, body=SVG, **headers):
        merged = {"Content-Type": "image/svg+xml", "X-Filename": "Giftbox%20Birthday.svg", **self.lb_headers()}
        merged.update(headers)
        return self.request("POST", "/api/lightburn/send", body, merged)

    def test_targets(self):
        response, body = self.request("GET", "/api/lightburn/targets")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body), {"hosts": ["127.0.0.1", "host.containers.internal"],
                                            "ports": [self.lb.port, 19520]})
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_settings_page_and_scripts_are_served(self):
        for path, needle in (("/einstellungen", b"Schl\xc3\xbcssel"), ("/settings.js", b"importSecret"),
                             ("/lightburn.js", b"importKey")):
            with self.subTest(path=path):
                response, body = self.request("GET", path)
                self.assertEqual(response.status, 200)
                self.assertIn(needle, body)
                self.assertIn("connect-src 'self'", response.getheader("Content-Security-Policy"))
        _, script = self.request("GET", "/lightburn.js")
        self.assertIn(b"false, // non-extractable", script)
        for storage in (b"localStorage", b"sessionStorage", b"document.cookie"):
            self.assertNotIn(storage, script)

    def test_connection_test_endpoint(self):
        response, body = self.request("POST", "/api/lightburn/test", headers=self.lb_headers())
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(body)["ok"])
        response, body = self.request("POST", "/api/lightburn/test",
                                      headers=self.lb_headers(token=lightburn.token("wrong-secret")))
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("Schlüssel ungültig", payload["message"])

    def test_send_endpoint_forwards_and_confirms(self):
        token = lightburn.token(SECRET)
        response, body = self.send(**{"X-LightBurn-Token": token, "X-LightBurn-Group": "true"})
        self.assertEqual(response.status, 200, body)
        self.assertEqual(json.loads(body), {"accepted": True, "confirmed": True,
                                            "message": "In LightBurn importiert."})
        [upload] = self.lb.uploads
        self.assertEqual(upload["authorization"], f"Bearer {token}")
        self.assertEqual(upload["filename"], "Giftbox Birthday.svg")
        self.assertEqual(upload["group_shapes"], "true")
        self.assertEqual(upload["body"], SVG)
        self.assertNotIn(token.encode(), body)
        self.assertFalse(any(token in line for line in self.capture.lines), self.capture.lines)
        self.assertTrue(any("/api/lightburn/send 200" in line for line in self.capture.lines))

    def test_send_open_mode(self):
        response, body = self.send(**{"X-LightBurn-Mode": "open"})
        payload = json.loads(body)
        self.assertTrue(payload["accepted"])
        self.assertIsNone(payload["confirmed"])
        self.assertEqual(self.lb.uploads[0]["path"], "/api/file/open")

    def test_token_never_echoed_on_errors(self):
        token = lightburn.token("wrong-secret")
        for path in ("/api/lightburn/test", "/api/lightburn/send"):
            response, body = self.request("POST", path, SVG if path.endswith("send") else None,
                                          {**self.lb_headers(token=token), "Content-Type": "image/svg+xml"})
            self.assertNotIn(token.encode(), body)
        bad = token[:-1] + "Z"
        response, body = self.request("POST", "/api/lightburn/test", headers=self.lb_headers(token=bad))
        self.assertEqual(response.status, 400)
        self.assertNotIn(bad.encode(), body)
        self.assertFalse(any(token in line or bad in line for line in self.capture.lines))

    def test_send_does_not_need_a_conversion_slot(self):
        slots = self.httpd.RequestHandlerClass.slots
        taken = 0
        while slots.acquire(blocking=False):
            taken += 1
        try:
            response, body = self.send()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(body)["accepted"])
        finally:
            for _ in range(taken):
                slots.release()

    def test_rejects_targets_outside_the_allowlist(self):
        cases = {
            "host": {"X-LightBurn-Host": "169.254.169.254"},
            "host with port": {"X-LightBurn-Host": f"127.0.0.1:{self.lb.port}"},
            "port": {"X-LightBurn-Port": "22"},
            "token upper": {"X-LightBurn-Token": lightburn.token(SECRET).upper()},
            "token short": {"X-LightBurn-Token": "abc"},
            "secret instead of token": {"X-LightBurn-Token": SECRET},
            "mode": {"X-LightBurn-Mode": "delete"},
            "group": {"X-LightBurn-Group": "yes"},
        }
        for label, headers in cases.items():
            with self.subTest(label):
                response, body = self.send(**headers)
                self.assertEqual(response.status, 400, body)
                self.assertIn("error", json.loads(body))
        self.assertEqual(self.lb.uploads, [])
        self.assertEqual(self.lb.requests, [])

    def test_slow_uploads_do_not_block_sending(self):
        # Two senders that never finish their body (any 64-hex token passes the format check) used to
        # hold both send slots for minutes. The body is now read before a send slot is taken.
        head = (f"POST /api/lightburn/send HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                "X-Requested-With: xcs2svg\r\nContent-Type: image/svg+xml\r\n"
                f"X-LightBurn-Host: 127.0.0.1\r\nX-LightBurn-Port: {self.lb.port}\r\n"
                f"X-LightBurn-Token: {'0' * 64}\r\nContent-Length: 1000000\r\n\r\n<svg").encode()
        slow = []
        for _ in range(2):  # MAX_SENDS
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            self.addCleanup(sock.close)
            sock.sendall(head)
            slow.append(sock)
        time.sleep(0.3)
        start = time.monotonic()
        response, body = self.send()
        self.assertEqual(response.status, 200, body)
        self.assertTrue(json.loads(body)["confirmed"])
        self.assertLess(time.monotonic() - start, 5)

    def test_overlapping_sends_to_one_lightburn_get_their_own_result(self):
        # file_imported names no file, so two overlapping uploads could swap results; they are serialised.
        self.lb.import_delay, self.lb.failing = 0.6, {"bad.svg"}
        self.addCleanup(setattr, self.lb, "import_delay", 0.0)
        self.addCleanup(setattr, self.lb, "failing", set())
        results = {}

        def send(name):
            response, body = self.send(**{"X-Filename": name})
            results[name] = json.loads(body)

        first = threading.Thread(target=send, args=("good.svg",))
        first.start()
        time.sleep(0.2)
        second = threading.Thread(target=send, args=("bad.svg",))
        second.start()
        first.join(10)
        second.join(10)
        self.assertIs(results["good.svg"]["confirmed"], True, results)
        self.assertIs(results["bad.svg"]["confirmed"], False, results)
        self.assertIn("nicht importieren", results["bad.svg"]["message"])

    def test_unknown_host_names_are_rejected(self):
        # DNS rebinding: the attacker's name resolves to 127.0.0.1, so Origin and Host match each other.
        for host in ("rebind.attacker.example", "localhost.attacker.example", "127.0.0.1.nip.io", "", "local host"):
            with self.subTest(host=host):
                own = f"rebind.attacker.example:{self.port}" if host == "rebind.attacker.example" else host
                headers = self.lb_headers(Host=own, Origin=f"http://{own}", **{"Sec-Fetch-Site": "same-origin"})
                for method, path in (("POST", "/api/lightburn/test"), ("POST", "/api/lightburn/send"),
                                     ("GET", "/api/lightburn/targets")):
                    body = SVG if path.endswith("send") else None
                    response, payload = self.request(method, path, body,
                                                     {**headers, "Content-Type": "image/svg+xml"} if body else headers)
                    self.assertEqual(response.status, 421, (path, payload))
        self.assertEqual(self.lb.requests, [])
        for host in (f"localhost:{self.port}", f"LOCALHOST:{self.port}", f"[::1]:{self.port}", "127.0.0.1"):
            with self.subTest(host=host):
                response, payload = self.request("POST", "/api/lightburn/test",
                                                 headers=self.lb_headers(Host=host, Origin=f"http://{host}"))
                self.assertEqual(response.status, 200, payload)

    def test_public_hosts_allow_a_proxy_name(self):
        env = {"LIGHTBURN_HOSTS": "127.0.0.1", "LIGHTBURN_PORTS": str(self.lb.port), "PUBLIC_HOSTS": "Laser.Example"}
        with mock.patch.dict(os.environ, env):
            httpd = server.make_server("127.0.0.1", 0, max_upload_mb=1)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=15)
        try:
            conn.request("POST", "/api/lightburn/test", headers=self.lb_headers(
                Host="laser.example", Origin="https://laser.example"))
            response = conn.getresponse()
            self.assertEqual(response.status, 200, response.read())
        finally:
            conn.close()

    def raw(self, request: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(request)
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        return b"".join(chunks)

    def test_crlf_and_folded_headers_are_rejected(self):
        token = lightburn.token(SECRET).encode()
        # obs-fold: the parser joins the continuation line into the value
        reply = self.raw(b"POST /api/lightburn/test HTTP/1.1\r\nHost: localhost\r\nX-Requested-With: xcs2svg\r\n"
                         b"X-LightBurn-Host: 127.0.0.1\r\n evil.example\r\nX-LightBurn-Port: " +
                         str(self.lb.port).encode() + b"\r\nX-LightBurn-Token: " + token +
                         b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        self.assertIn(b" 400 ", reply.split(b"\r\n", 1)[0])
        reply = self.raw(b"POST /api/lightburn/test HTTP/1.1\r\nHost: localhost\r\nX-Requested-With: xcs2svg\r\n"
                         b"X-LightBurn-Host: 127.0.0.1\tevil.example\r\nX-LightBurn-Port: " +
                         str(self.lb.port).encode() + b"\r\nX-LightBurn-Token: " + token +
                         b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        self.assertIn(b" 400 ", reply.split(b"\r\n", 1)[0])
        self.assertEqual(self.lb.requests, [])

    def test_csrf_protection(self):
        own = f"http://127.0.0.1:{self.port}"
        cases = [
            ("missing X-Requested-With", {"X-Requested-With": None}, 403),
            ("wrong X-Requested-With", {"X-Requested-With": "XMLHttpRequest"}, 403),
            ("foreign Origin", {"Origin": "http://evil.example"}, 403),
            ("foreign port", {"Origin": "http://127.0.0.1:1"}, 403),
            ("null Origin", {"Origin": "null"}, 403),
            ("cross-site fetch", {"Sec-Fetch-Site": "cross-site", "Origin": own}, 403),
            ("same Origin", {"Origin": own, "Sec-Fetch-Site": "same-origin"}, 200),
            ("no Origin (curl)", {}, 200),
        ]
        for label, changes, status in cases:
            with self.subTest(label):
                headers = self.lb_headers()
                for name, value in changes.items():
                    if value is None:
                        headers.pop(name, None)
                    else:
                        headers[name] = value
                for path in ("/api/lightburn/test", "/api/lightburn/send"):
                    body = SVG if path.endswith("send") else None
                    if body:
                        headers["Content-Type"] = "image/svg+xml"
                    response, payload = self.request("POST", path, body, headers)
                    self.assertEqual(response.status, status, (path, payload))
                    self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                    self.assertIsNone(response.getheader("Access-Control-Allow-Headers"))

    def test_preflight_is_never_approved(self):
        response, _ = self.request("OPTIONS", "/api/lightburn/send", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with,x-lightburn-token"})
        self.assertNotEqual(response.status, 200)
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_send_body_rules(self):
        response, body = self.send(body=b"\x89PNG not an svg")
        self.assertEqual(response.status, 400)
        self.assertIn("SVG", json.loads(body)["error"])
        response, _ = self.send(body=b"")
        self.assertEqual(response.status, 400)
        response, _ = self.send(**{"Content-Type": "text/plain"})
        self.assertEqual(response.status, 415)
        response, body = self.send(body=b"<svg" + b" " * (1024 * 1024 + 10) + b"/>")
        self.assertEqual(response.status, 413)
        response, _ = self.request("POST", "/api/lightburn/test", b"x", self.lb_headers())
        self.assertEqual(response.status, 400)
        self.assertEqual(self.lb.uploads, [])

    def test_no_settings_storage_endpoints(self):
        for method, path in (("GET", "/api/settings"), ("POST", "/api/settings"), ("GET", "/api/lightburn")):
            response, _ = self.request(method, path, b"{}" if method == "POST" else None,
                                       {"Content-Type": "application/json"})
            self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
