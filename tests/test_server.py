"""Tests for the HTTP server: static files, API, limits and security headers."""
import http.client
import json
import os
import socket
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from unittest import mock
from urllib.parse import quote

from app import server
from test_xcs2svg import FIXTURES

SAMPLE = {
    "canvas": [{"id": "c1", "title": "{panel}1", "displays": [
        {"id": "r1", "type": "RECT", "x": 5, "y": 5, "width": 20, "height": 10, "angle": 0,
         "scale": {"x": 1, "y": 1}, "skew": {"x": 0, "y": 0}, "pivot": {"x": 0, "y": 0}},
    ]}],
}


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.make_server("127.0.0.1", 0, max_upload_mb=1)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response, response.read()
        finally:
            conn.close()

    def convert(self, data, filename="probe.xcs"):
        return self.request("POST", "/api/convert", data, {
            "Content-Type": "application/octet-stream", "X-Filename": quote(filename)})

    def assertSecurityHeaders(self, response):
        csp = response.getheader("Content-Security-Policy")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("img-src 'self' blob: data:", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")

    def test_healthz(self):
        response, body = self.request("GET", "/healthz")
        self.assertEqual((response.status, body), (200, b"ok"))
        self.assertSecurityHeaders(response)

    def test_index_and_assets(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/html"))
        self.assertIn(b'<html lang="de">', body)
        self.assertNotIn(b"<script>", body)
        self.assertNotIn(b"style=", body)
        self.assertSecurityHeaders(response)
        for path, mime in (("/app.js", "text/javascript"), ("/style.css", "text/css"), ("/favicon.svg", "image/svg+xml")):
            with self.subTest(path=path):
                response, _ = self.request("GET", path)
                self.assertEqual(response.status, 200)
                self.assertTrue(response.getheader("Content-Type").startswith(mime))

    def test_head_has_no_body(self):
        response, body = self.request("HEAD", "/")
        self.assertEqual((response.status, body), (200, b""))

    def test_path_traversal_and_unknown_files(self):
        for path in ("/../app/server.py", "/%2e%2e/server.py", "/static/../server.py", "/..%2fserver.py",
                     "/server.py", "/app/xcs2svg.py", "/static/app.js", "//etc/passwd", "/index.html/..",
                     "/C:/Windows/win.ini", "/app.js%00.png"):
            with self.subTest(path=path):
                response, _ = self.request("GET", path)
                self.assertEqual(response.status, 404)
                self.assertSecurityHeaders(response)

    def test_convert_synthetic(self):
        response, body = self.convert(json.dumps(SAMPLE).encode(), "Mein Projekt.xcs")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("application/json"))
        self.assertSecurityHeaders(response)
        payload = json.loads(body)
        self.assertEqual(payload["warnings"], [])
        self.assertEqual([f["name"] for f in payload["files"]], ["Mein Projekt.svg"])
        root = ET.fromstring(payload["files"][0]["svg"])
        self.assertEqual(root.get("width"), "25mm")

    def test_convert_real_samples(self):
        samples = sorted(FIXTURES.glob("*.xcs")) if FIXTURES.is_dir() else []
        if not samples:
            self.skipTest("XCS_FIXTURES not available")
        for source in samples:
            with self.subTest(sample=source.name):
                response, body = self.convert(source.read_bytes(), source.name)
                self.assertEqual(response.status, 200)
                for file in json.loads(body)["files"]:
                    ET.fromstring(file["svg"])

    def test_invalid_file_gives_german_error(self):
        response, body = self.convert(b"{kaputt")
        self.assertEqual(response.status, 400)
        self.assertSecurityHeaders(response)
        error = json.loads(body)["error"]
        self.assertIn("keine gültige XCS-Datei", error)
        self.assertNotIn("Traceback", error)

    def test_empty_body(self):
        response, _ = self.convert(b"")
        self.assertEqual(response.status, 400)

    def test_oversized_upload_is_rejected_before_reading(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/convert")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(2 * 1024 * 1024))
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            self.assertIn("zu groß", json.loads(response.read())["error"])
        finally:
            conn.close()

    def test_missing_or_bad_content_length(self):
        for headers in ({}, {"Content-Length": "abc"}, {"Content-Length": "-1"}, {"Transfer-Encoding": "chunked"},
                        {"Content-Length": "\u00b2"}, {"Content-Length": "9" * 13}):
            with self.subTest(headers=headers):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                try:
                    conn.putrequest("POST", "/api/convert", skip_accept_encoding=True)
                    conn.putheader("Content-Type", "application/octet-stream")
                    for name, value in headers.items():
                        conn.putheader(name, value)
                    conn.endheaders()
                    self.assertEqual(conn.getresponse().status, 411)
                finally:
                    conn.close()

    def test_wrong_content_type(self):
        response, _ = self.request("POST", "/api/convert", b"{}", {"Content-Type": "multipart/form-data"})
        self.assertEqual(response.status, 415)

    def test_error_reply_survives_an_unread_body(self):
        # The 415 goes out before the body is read. Without a lingering close the unread rest of the
        # body makes the kernel reset the connection, and the client loses the reply (WinError 10053).
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(b"POST /api/convert HTTP/1.1\r\nHost: x\r\nContent-Type: multipart/form-data\r\n"
                         b"Content-Length: 300000\r\n\r\n" + b"x" * 100_000)
            time.sleep(0.3)  # the server has answered and closed its side by now
            sock.sendall(b"x" * 200_000)
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        reply = b"".join(chunks)
        self.assertTrue(reply.startswith(b"HTTP/1.0 415"), reply[:60])
        self.assertIn("application/octet-stream", json.loads(reply.split(b"\r\n\r\n", 1)[1])["error"])

    def test_unknown_post_route(self):
        response, _ = self.request("POST", "/api/other", b"{}", {"Content-Type": "application/octet-stream"})
        self.assertEqual(response.status, 404)


class LimitTests(unittest.TestCase):
    """Slow or idle clients must not block conversions or exhaust threads."""

    def start(self, env=None, **handler_attrs):
        with mock.patch.dict(os.environ, env or {}):
            httpd = server.make_server("127.0.0.1", 0, max_upload_mb=1)
        for name, value in handler_attrs.items():
            setattr(httpd.RequestHandlerClass, name, value)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def stop():
            httpd.shutdown()
            httpd.server_close()
            thread.join(5)

        self.addCleanup(stop)
        return httpd.server_address[1]

    def open(self, port, data=b""):
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.addCleanup(sock.close)
        if data:
            sock.sendall(data)
        return sock

    def read_all(self, sock):
        chunks = []
        try:
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        except ConnectionError:
            pass
        return b"".join(chunks)

    def upload_head(self, length):
        return (f"POST /api/convert HTTP/1.1\r\nHost: x\r\nContent-Type: application/octet-stream\r\n"
                f"Content-Length: {length}\r\n\r\n").encode()

    def convert(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            conn.request("POST", "/api/convert", json.dumps(SAMPLE).encode(),
                         {"Content-Type": "application/octet-stream"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_slow_upload_does_not_hold_a_conversion_slot(self):
        port = self.start({"MAX_PARALLEL": "1"})
        slow = self.open(port, self.upload_head(100_000) + b"{")
        time.sleep(0.3)
        start = time.monotonic()
        status, _ = self.convert(port)
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - start, 5)
        slow.close()

    def test_upload_slower_than_the_minimum_rate_is_cut_off(self):
        port = self.start(body_grace_seconds=0.5)
        sock = self.open(port, self.upload_head(100_000) + b"{")
        start = time.monotonic()
        response = self.read_all(sock)
        self.assertTrue(response.startswith(b"HTTP/1.0 408"), response[:40])
        self.assertLess(time.monotonic() - start, 5)

    def test_headers_must_arrive_within_the_deadline(self):
        port = self.start(header_seconds=0.5)
        sock = self.open(port, b"GET /healthz HTTP/1.1\r\n")
        start = time.monotonic()
        for _ in range(10):  # keeps the per-recv idle timeout from ever firing
            try:
                sock.sendall(b"X-Slow: 1\r\n")
            except OSError:
                break
            time.sleep(0.2)
        self.assertEqual(self.read_all(sock), b"")
        self.assertLess(time.monotonic() - start, 5)

    def test_connections_are_capped(self):
        port = self.start({"MAX_CONNECTIONS": "2"})
        idle = [self.open(port, b"GET /healthz HTTP/1.1\r\n") for _ in range(2)]
        time.sleep(0.3)
        extra = self.open(port, b"GET /healthz HTTP/1.1\r\n\r\n")
        self.assertEqual(self.read_all(extra), b"")
        for sock in idle:
            sock.close()
        time.sleep(0.3)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/healthz")
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
