"""Tests for reading xTool Studio (.xs) archives (stdlib unittest only)."""
import io
import json
import unittest
import zipfile
from unittest import mock

from app import xcs2svg
from test_xcs2svg import EVIL, FIXTURES, SVG_NS, display, parse_svg

CANVAS = "c1"


def archive(files: dict, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as zf:
        for name, content in files.items():
            zf.writestr(name, content if isinstance(content, (bytes, str)) else json.dumps(content))
    return buffer.getvalue()


def project(displays, chunks=None, extra=None, bindings=None) -> dict:
    """A minimal .xs project; ``chunks`` maps chunk index -> displays (default: one chunk)."""
    files = {
        ".format": "v2",
        "project.json": {"version": "2.0.0", "activeDeviceId": "dev", "modules": {"canvases": [CANVAS]}},
        f"canvases/{CANVAS}.json": {"id": CANVAS, "title": "{panel}1", "layerData": {}},
        "profiles.json": {"profiles": {
            "profile:cut": {"processingType": "VECTOR_CUTTING", "values": {"power": 1, "speed": 16, "repeat": 1}},
        }},
        "devices/device-dev.json": {"id": "dev", "processing": {CANVAS: {"activeMode": "LASER_PLANE", "modes": {
            "LASER_PLANE": {
                "patches": {"patch:a": {"profileId": "profile:cut", "overrides": {"power": 90, "speed": 10}}},
                "bindings": bindings if bindings is not None else [
                    {"baseProfileId": "profile:cut", "patchIds": ["patch:a"], "displayIds": ["cut"]}],
            }}}}},
    }
    for index, items in (chunks or {0: displays}).items():
        files[f"canvases/{CANVAS}/displays-{index}.json"] = {"canvasId": CANVAS, "chunkIndex": index,
                                                            "displays": items}
    files.update(extra or {})
    return files


class XsTests(unittest.TestCase):
    def convert(self, files, name="Box.xs"):
        result = xcs2svg.convert(archive(files), name)
        return result, [parse_svg(file.svg) for file in result.files]

    def test_settings_come_from_profile_and_material_patch(self):
        cut = display("PATH", id="cut", dPath="M0 0 H10 V5 H0 Z", width=10, height=5)
        result, (root,) = self.convert(project([cut]))
        self.assertEqual(result.files[0].name, "Box.svg")
        layer = root.find(f"{SVG_NS}g")
        self.assertEqual(layer.get("data-processing"), "VECTOR_CUTTING")
        self.assertIn("90 %, 10 mm/s", layer.get("data-name"))

    def test_vector_ref_is_resolved_from_bucket(self):
        ref = {"vectorHash": "h1", "bucketType": "svg", "originalField": "dPath"}
        cut = display("PATH", id="cut", vectorRef=ref, width=10, height=5)
        extra = {"vectors/svg/data-0.json": {"bucketType": "svg", "entries": {"h1": "M0 0 H10 V5 H0 Z"}}}
        _, (root,) = self.convert(project([cut], extra=extra))
        self.assertIsNotNone(root.find(".//*[@id='e-cut']"))

    def test_chunks_are_joined_in_index_order_and_unbound_types_kept(self):
        first = display("RECT", id="a", width=5, height=5)
        fill = display("PATH", id="b", dPath="M0 0 H4 V4 H0 Z", width=4, height=4,
                       customData={"processingType": "FILL_VECTOR_ENGRAVING"})
        _, (root,) = self.convert(project(None, chunks={1: [fill], 0: [first]}, bindings=[]))
        ids = [node.get("id") for node in root.iter() if (node.get("id") or "").startswith("e-")]
        self.assertEqual(ids, ["e-a", "e-b"])
        filled = root.find(".//*[@data-processing='FILL_VECTOR_ENGRAVING']")
        self.assertNotEqual(filled.get("fill"), "none")

    def test_canvas_order_follows_project(self):
        files = project([display("RECT", id="a", width=5, height=5)])
        files["project.json"]["modules"]["canvases"] = ["c2", CANVAS]
        files["canvases/c2.json"] = {"id": "c2", "title": "Zweite"}
        files["canvases/c2/displays-0.json"] = {"displays": [display("RECT", id="z", width=3, height=3)]}
        result, _ = self.convert(files)
        self.assertEqual([file.title for file in result.files], ["Zweite", "Arbeitsfläche 1"])

    def test_hostile_strings_never_reach_the_svg(self):
        ref = {"vectorHash": "h1", "bucketType": "svg", "originalField": "dPath"}
        evil = display("PATH", id="evil", name=EVIL, vectorRef=ref, width=1, height=1)
        bad_field = display("PATH", id="bad", vectorRef={**ref, "originalField": "on-load"}, width=1, height=1)
        good = display("RECT", id="ok", width=5, height=5)
        extra = {"vectors/svg/data-0.json": {"entries": {"h1": EVIL}}}
        result, (root,) = self.convert(project([evil, bad_field, good], extra=extra), EVIL + ".xs")
        svg = result.files[0].svg
        self.assertNotIn("<script", svg)
        self.assertNotIn("on-load", svg)
        self.assertIsNotNone(root.find(".//*[@id='e-ok']"))

    def test_unpacked_size_budget_stops_zip_bombs(self):
        files = project([display("RECT", id="a", width=5, height=5)])
        files["vectors/svg/data-0.json"] = b'{"entries": {"h": "' + b"0" * 2_000_000 + b'"}}'
        data = archive(files)
        self.assertLess(len(data), 50_000)
        with mock.patch.object(xcs2svg, "MAX_INPUT_BYTES", 100_000):
            with self.assertRaises(xcs2svg.XcsError) as ctx:
                xcs2svg.convert(data, "bomb.xs")
        self.assertIn("zu groß", str(ctx.exception))

    def test_container_budget_applies_across_entries(self):
        files = project([display("RECT", id="a", width=5, height=5)])
        files["vectors/svg/data-0.json"] = "[" + "{}," * 5_000 + "{}]"
        with mock.patch.object(xcs2svg, "MAX_JSON_CONTAINERS", 1_000):
            with self.assertRaises(xcs2svg.XcsError):
                xcs2svg.convert(archive(files), "x.xs")

    def test_value_budget_applies_across_chunks(self):
        files = project([display("RECT", id="a", width=5, height=5)])
        files["vectors/svg/data-0.json"] = b'{"entries": {' + b'"k": "ab",' * 600 + b'"h": "M0 0"}}'
        files["vectors/svg/data-1.json"] = b'{"entries": {' + b'"k": "ab",' * 600 + b'"h": "M0 0"}}'
        with mock.patch.object(xcs2svg, "MAX_JSON_VALUES", 2_000):
            with self.assertRaises(xcs2svg.XcsError) as ctx:
                xcs2svg.convert(archive(files), "x.xs")
        self.assertIn("zu viele Einträge", str(ctx.exception))
        xcs2svg.convert(archive(files), "x.xs")  # fine with the real limit

    def test_invalid_archives_give_german_errors(self):
        cases = {
            "kein ZIP": b"PK\x03\x04kaputt",
            "ohne project.json": archive({"canvases/c1.json": {}}),
            "kaputtes JSON": archive({"project.json": "{nope"}),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(xcs2svg.XcsError) as ctx:
                xcs2svg.convert(data, "x.xs")
            self.assertTrue(str(ctx.exception))

    def test_base_name_strips_xs(self):
        self.assertEqual(xcs2svg.base_name("Giftbox Birthday.xs"), "Giftbox Birthday")


@unittest.skipUnless(FIXTURES.is_dir() and any(FIXTURES.glob("*.xs")), "no .xs files in XCS_FIXTURES")
class RealXsSampleTests(unittest.TestCase):
    def test_samples_convert(self):
        for source in sorted(FIXTURES.glob("*.xs")):
            with self.subTest(sample=source.name):
                result = xcs2svg.convert(source.read_bytes(), source.name)
                self.assertTrue(result.files)
                for file in result.files:
                    root = parse_svg(file.svg)
                    self.assertTrue(root.get("width").endswith("mm"))
                    self.assertTrue(root.findall(f".//{SVG_NS}path"))


if __name__ == "__main__":
    unittest.main()
