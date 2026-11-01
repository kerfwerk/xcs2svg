"""Tests for ZIP bundles (e.g. an Atomm download: project file plus product photos)."""
import json
import threading
import time
import tracemalloc
import unittest
from unittest import mock
from urllib.parse import quote

from app import server, xcs2svg, xstudio
from test_xcs2svg import FIXTURES, display, document, parse_svg
from test_xstudio import archive, project


def xcs(width=10, ident="r1") -> bytes:
    return json.dumps(document(display("RECT", id=ident, width=width, height=5))).encode("utf-8")


class BundleTests(unittest.TestCase):
    def convert(self, files, name="bundle.zip"):
        result = xcs2svg.convert(archive(files), name)
        for file in result.files:
            parse_svg(file.svg)
        return result

    def test_several_projects_are_converted_and_named_after_their_members(self):
        result = self.convert({
            "Box Deckel.xcs": xcs(10),
            "unter/Box Boden.XCS": xcs(20),
            "Anhänger.xs": archive(project([display("RECT", id="cut", width=5, height=5)])),
            "IMG_1350.jpeg": b"\xff\xd8\xff photo",
            "IMG_1351.jpeg": b"\xff\xd8\xff photo",
        })
        self.assertEqual([f.name for f in result.files], ["Box Deckel.svg", "Box Boden.svg", "Anhänger.svg"])
        self.assertIn("2 andere Dateien im ZIP ignoriert (Bilder o. Ä.).", result.warnings)

    def test_member_warnings_are_prefixed(self):
        doc = document(display("RECT", id="r1", width=10, height=5), display("UNKNOWN_THING", id="u1"))
        result = self.convert({"Teil A.xcs": json.dumps(doc), "liesmich.txt": "x"})
        self.assertTrue(any(w.startswith("Teil A.xcs: ") for w in result.warnings), result.warnings)
        self.assertIn("1 andere Datei im ZIP ignoriert (Bilder o. Ä.).", result.warnings)

    def test_failing_member_becomes_a_warning(self):
        result = self.convert({"gut.xcs": xcs(), "kaputt.xcs": b"{nope", "leer.xs": b"PK\x03\x04kaputt"})
        self.assertEqual([f.name for f in result.files], ["gut.svg"])
        self.assertTrue(any(w.startswith("kaputt.xcs: nicht umgewandelt – ") for w in result.warnings))
        self.assertTrue(any(w.startswith("leer.xs: nicht umgewandelt – ") for w in result.warnings))

    def test_duplicate_member_names_get_unique_svg_names(self):
        result = self.convert({"a/Teil.xcs": xcs(), "b/Teil.xcs": xcs()})
        self.assertEqual(len({f.name for f in result.files}), 2)

    def test_traversal_and_hostile_member_names_are_sanitised(self):
        result = self.convert({"../../x.xcs": xcs(), "..\\..\\evil<b>\"q.xcs": xcs(), "/abs/y.xcs": xcs()})
        names = [f.name for f in result.files]
        self.assertEqual(names[0], "x.svg")
        for name in names:
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertNotIn("<", name)
            self.assertNotIn('"', name)
            self.assertFalse(name.startswith("."))

    def test_nested_bundles_are_not_unpacked(self):
        inner = archive({"tief.xcs": xcs()})
        with mock.patch.object(xstudio, "bundle", wraps=xstudio.bundle) as spy:
            result = self.convert({"oben.xcs": xcs(), "inner.zip": inner, "inner.xs": inner})
        self.assertEqual(spy.call_count, 1)
        self.assertEqual([f.name for f in result.files], ["oben.svg"])
        self.assertTrue(any(w.startswith("inner.xs: nicht umgewandelt") and "project.json" in w
                            for w in result.warnings), result.warnings)
        self.assertIn("1 andere Datei im ZIP ignoriert (Bilder o. Ä.).", result.warnings)  # inner.zip

    def test_mac_metadata_and_dotfiles_are_skipped_silently(self):
        result = self.convert({"Teil.xcs": xcs(), "__MACOSX/._Teil.xcs": b"junk", ".DS_Store": b"junk",
                               "ordner/": b"", ".hidden/x.xcs": b"junk"})
        self.assertEqual([f.name for f in result.files], ["Teil.svg"])
        self.assertFalse(any("ignoriert" in w for w in result.warnings), result.warnings)

    def test_no_convertible_member(self):
        with self.assertRaises(xcs2svg.XcsError) as ctx:
            self.convert({"IMG_1350.jpeg": b"photo", "readme.txt": b"x"})
        self.assertEqual(str(ctx.exception), "Das ZIP-Archiv enthält keine .xcs- oder .xs-Datei.")
        with self.assertRaises(xcs2svg.XcsError) as ctx:
            self.convert({"a.xcs": b"{nope", "b.xcs": b"[]"})
        self.assertIn("Keine Datei im ZIP-Archiv ließ sich umwandeln.", str(ctx.exception))

    def test_shared_unpacked_budget(self):
        files = {f"teil{i}.xcs": xcs() + b" " * 40_000 for i in range(5)}
        data = archive(files)
        self.assertLess(len(data), 20_000)
        with mock.patch.object(xcs2svg, "MAX_INPUT_BYTES", 100_000):
            with self.assertRaises(xcs2svg.XcsError) as ctx:
                xcs2svg.convert(data, "bundle.zip")
        self.assertIn("entpackt zu groß", str(ctx.exception))

    def test_budget_does_not_trust_header_sizes(self):
        data = bytearray(archive({"bombe.xcs": b" " * 500_000 + xcs()}))
        # Lie about the uncompressed size in the central directory: claim 10 bytes.
        central = data.rfind(b"PK\x01\x02")
        data[central + 24:central + 28] = (10).to_bytes(4, "little")
        with mock.patch.object(xcs2svg, "MAX_INPUT_BYTES", 100_000):
            with self.assertRaises(xcs2svg.XcsError):
                xcs2svg.convert(bytes(data), "bundle.zip")

    def test_short_values_bomb_is_rejected_before_parsing(self):
        # 98 KB upload, 64 MB unpacked of '"ab",': json.loads would build 13 million str objects (~790 MB).
        bomb = b"[" + b'"ab",' * (64 * 1024 * 1024 // 5 - 10) + b"1]"
        uploads = {"bombe.zip": archive({"a.xcs": bomb}),
                   "bombe.xs": archive({"project.json": b"{}", "canvases/c/displays-0.json": b'{"displays":' + bomb + b"}"})}
        del bomb
        for name, data in uploads.items():
            with self.subTest(name):
                self.assertLess(len(data), 150_000)
                tracemalloc.start()
                start = time.monotonic()
                try:
                    with self.assertRaises(xcs2svg.XcsError) as ctx:
                        xcs2svg.convert(data, name)
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertIn("zu viele Einträge", str(ctx.exception))
                self.assertLess(peak, 200 * 1024 * 1024)
                self.assertLess(time.monotonic() - start, 5)

    def test_value_budget_is_shared_by_all_members(self):
        member = b"[" + b"1," * 400 + b"1]"  # about 400 values each
        files = {f"teil{i}.xcs": member for i in range(3)}
        with mock.patch.object(xcs2svg, "MAX_JSON_VALUES", 1_000):
            with self.assertRaises(xcs2svg.XcsError) as ctx:
                xcs2svg.convert(archive(files), "bundle.zip")
        self.assertIn("zu viele Einträge", str(ctx.exception))
        self.assertNotIn("nicht umgewandelt", str(ctx.exception))  # fatal, not a per-member warning

    def test_too_many_entries(self):
        files = {f"bild{i}.jpg": b"" for i in range(xstudio.MAX_ENTRIES + 1)}
        with self.assertRaises(xcs2svg.XcsError) as ctx:
            self.convert(files)
        self.assertIn("zu viele Dateien", str(ctx.exception))

    def test_too_many_project_files(self):
        files = {f"teil{i}.xcs": b"{}" for i in range(xstudio.MAX_BUNDLE_FILES + 1)}
        with self.assertRaises(xcs2svg.XcsError) as ctx:
            self.convert(files)
        self.assertIn("zu viele Projektdateien", str(ctx.exception))

    def test_xs_project_zip_still_takes_the_project_path(self):
        data = archive(project([display("RECT", id="cut", width=5, height=5)]))
        with mock.patch.object(xstudio, "bundle") as spy:
            result = xcs2svg.convert(data, "Projekt.xs")
        spy.assert_not_called()
        self.assertEqual([f.name for f in result.files], ["Projekt.svg"])


@unittest.skipUnless((FIXTURES / "Giftbox.zip").is_file(), "Giftbox.zip not in XCS_FIXTURES")
class GiftboxTests(unittest.TestCase):
    def test_atomm_download(self):
        result = xcs2svg.convert((FIXTURES / "Giftbox.zip").read_bytes(), "Giftbox.zip")
        self.assertEqual([f.name for f in result.files], ["Giftbox Birthday.svg"])
        self.assertIn("2 andere Dateien im ZIP ignoriert (Bilder o. Ä.).", result.warnings)
        root = parse_svg(result.files[0].svg)
        self.assertTrue(root.get("width").endswith("mm"))

    def test_atomm_download_over_http(self):
        import http.client

        httpd = server.make_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=30)
            conn.request("POST", "/api/convert", (FIXTURES / "Giftbox.zip").read_bytes(),
                         {"Content-Type": "application/octet-stream", "X-Filename": quote("Giftbox.zip")})
            response = conn.getresponse()
            payload = json.loads(response.read())
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(response.status, 200, payload)
        self.assertEqual([f["name"] for f in payload["files"]], ["Giftbox Birthday.svg"])


if __name__ == "__main__":
    unittest.main()
