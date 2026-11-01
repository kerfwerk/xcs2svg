"""Tests for the XCS to SVG converter (stdlib unittest only)."""
import base64
import io
import json
import math
import os
import re
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from app import xcs2svg

SVG_NS = "{http://www.w3.org/2000/svg}"
# Real sample files are not part of the repository (third-party designs): point XCS_FIXTURES at a
# folder with your own .xcs/.xs/.zip samples, or put them into tests/fixtures/ (ignored by git).
FIXTURES = Path(os.environ.get("XCS_FIXTURES") or Path(__file__).resolve().parent / "fixtures")
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
EVIL = '</svg><script>alert("x")</script>&<!--'


def display(kind, **fields):
    base = {"id": f"id-{kind.lower()}", "type": kind, "x": 10, "y": 20, "angle": 0,
            "scale": {"x": 1, "y": 1}, "skew": {"x": 0, "y": 0}, "pivot": {"x": 0, "y": 0}}
    base.update(fields)
    return base


def device_entry(kind, parameters=None):
    """Device settings of one display as XCS 1.2 stores them."""
    entry = {"processingType": kind}
    if parameters is not None:
        entry["data"] = {kind: {"materialType": "customize", "processIgnore": False,
                                "parameter": {"customize": parameters}}}
    return entry


def document(*displays, processing=None, settings=None, canvas_id="c1", **canvas_fields):
    """A minimal XCS document; ``processing`` maps display id -> processingType and
    ``settings`` display id -> laser parameters (power, speed, ...)."""
    settings = settings or {}
    entries = [[did, device_entry(kind, settings.get(did))] for did, kind in (processing or {}).items()]
    return {
        "canvasId": canvas_id,
        "canvas": [{"id": canvas_id, "title": "{panel}1", "displays": list(displays), **canvas_fields}],
        "device": {"id": "TEST", "data": {"dataType": "Map", "value": [
            [canvas_id, {"mode": "LASER_PLANE", "displays": {"dataType": "Map", "value": entries}}],
        ]}},
        "version": "2.0.0",
    }


def parse_svg(svg):
    root = ET.fromstring(svg.encode("utf-8"))
    assert root.tag == SVG_NS + "svg", root.tag
    return root


def coords_bbox(d):
    """Bounding box of all coordinates in absolute M/L/C/Q/Z path data."""
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", d)]
    xs, ys = numbers[::2], numbers[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def exact_bbox(d):
    return xcs2svg.path_bbox(xcs2svg.parse_path(d))


class ConverterTestCase(unittest.TestCase):
    def convert(self, doc, name="test.xcs"):
        result = xcs2svg.convert(json.dumps(doc).encode("utf-8"), name)
        for file in result.files:
            parse_svg(file.svg)
        return result

    def only_svg(self, doc):
        result = self.convert(doc)
        self.assertEqual(len(result.files), 1)
        return parse_svg(result.files[0].svg), result

    def element(self, root, element_id):
        found = root.find(f".//*[@id='e-{element_id}']")
        self.assertIsNotNone(found, f"element {element_id} missing")
        return found

    def assertBox(self, actual, expected, places=3):
        for got, want in zip(actual, expected):
            self.assertAlmostEqual(got, want, places=places, msg=f"{actual} != {expected}")


class ShapeTests(ConverterTestCase):
    def test_rect_true_size_in_mm(self):
        root, result = self.only_svg(document(display("RECT", width=30, height=15)))
        self.assertEqual(result.warnings, [])
        path = self.element(root, "id-rect")
        self.assertBox(coords_bbox(path.get("d")), (10, 20, 40, 35))
        self.assertEqual(root.get("width"), "40mm")
        self.assertEqual(root.get("height"), "35mm")
        self.assertEqual(root.get("viewBox"), "0 0 40 35")

    def test_rect_corner_radius(self):
        root, _ = self.only_svg(document(display("RECT", width=30, height=15, radius=4)))
        d = self.element(root, "id-rect").get("d")
        self.assertEqual(d.count("C"), 4)
        self.assertBox(exact_bbox(d), (10, 20, 40, 35))

    def test_rect_radius_clamped_to_half_size(self):
        root, _ = self.only_svg(document(display("RECT", width=10, height=4, radius=50)))
        self.assertBox(exact_bbox(self.element(root, "id-rect").get("d")), (10, 20, 20, 24))

    def test_rotation_around_position(self):
        root, _ = self.only_svg(document(display("RECT", width=20, height=10, angle=90)))
        self.assertBox(coords_bbox(self.element(root, "id-rect").get("d")), (0, 20, 10, 40))

    def test_rotation_45_degrees_keeps_size(self):
        root, _ = self.only_svg(document(display("RECT", width=10, height=10, angle=45)))
        box = coords_bbox(self.element(root, "id-rect").get("d"))
        self.assertAlmostEqual(box[2] - box[0], 10 * math.sqrt(2), places=3)
        self.assertAlmostEqual(box[1], 20, places=3)

    def test_flip_mirrors_at_position(self):
        root, _ = self.only_svg(document(display("RECT", x=50, width=20, height=10, scale={"x": -1, "y": 1})))
        self.assertBox(coords_bbox(self.element(root, "id-rect").get("d")), (30, 20, 50, 30))

    def test_skew_is_applied_with_warning(self):
        root, result = self.only_svg(document(display("RECT", width=10, height=10, skew={"x": 0.5, "y": 0})))
        box = coords_bbox(self.element(root, "id-rect").get("d"))
        self.assertAlmostEqual(box[2] - box[0], 10 + 10 * math.sin(0.5), places=3)
        self.assertTrue(any("Scherung" in w for w in result.warnings))

    def test_circle_and_ellipse(self):
        doc = document(display("CIRCLE", id="c", width=20, height=20),
                       display("CIRCLE", id="e", x=50, width=30, height=10))
        root, _ = self.only_svg(doc)
        self.assertBox(exact_bbox(self.element(root, "c").get("d")), (10, 20, 30, 40))
        self.assertBox(exact_bbox(self.element(root, "e").get("d")), (50, 20, 80, 30))

    def test_line_is_never_filled(self):
        doc = document(display("LINE", endPoint={"x": 30, "y": -10}, isFill=True))
        root, _ = self.only_svg(doc)
        line = self.element(root, "id-line")
        self.assertEqual(line.get("fill"), "none")
        self.assertBox(coords_bbox(line.get("d")), (10, 10, 40, 20))

    def test_path_is_placed_by_its_bounding_box_and_scaled(self):
        doc = document(display("PATH", dPath="M100 100 L110 100 L110 105 Z", scale={"x": 2, "y": 2},
                               width=20, height=10))
        root, _ = self.only_svg(doc)
        self.assertBox(coords_bbox(self.element(root, "id-path").get("d")), (10, 20, 30, 30))

    def test_path_relative_commands_and_arcs(self):
        dpath = "m0 0 h10 v10 a5 5 0 0 1 -10 0 z"
        root, _ = self.only_svg(document(display("PATH", dPath=dpath)))
        d = self.element(root, "id-path").get("d")
        self.assertNotIn("A", d)
        self.assertBox(exact_bbox(d), (10, 20, 20, 35))

    def test_pen_with_handles(self):
        pen = display("PEN", x=0, y=0, points=[{"x": 0, "y": 0}, {"x": 10, "y": 0}],
                      controlPoints={"0": [{"x": 0, "y": 0}, {"x": 0, "y": 10}],
                                     "1": [{"x": 10, "y": 10}, {"x": 10, "y": 0}]},
                      isClosePath=False)
        root, _ = self.only_svg(document(pen))
        d = self.element(root, "id-pen").get("d")
        self.assertIn("C", d)
        self.assertBox(exact_bbox(d), (0, 0, 10, 7.5))

    def test_polygon_point_formats(self):
        dict_points = display("POLYGON", id="p1", points=[{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 5, "y": 8}])
        flat_points = display("POLYGON", id="p2", x=40, points=[0, 0, 10, 0, 5, 8])
        root, _ = self.only_svg(document(dict_points, flat_points))
        self.assertBox(coords_bbox(self.element(root, "p1").get("d")), (10, 20, 20, 28))
        self.assertBox(coords_bbox(self.element(root, "p2").get("d")), (40, 20, 50, 28))
        self.assertTrue(self.element(root, "p1").get("d").endswith("Z"))


class TextTests(ConverterTestCase):
    def test_text_with_glyph_paths(self):
        chars = [
            {"type": "PATH", "x": 10, "y": 20, "angle": 0, "scale": {"x": 1, "y": 1}, "dPath": "M1 -10 L5 -10 L5 0 L1 0 Z"},
            {"type": "PATH", "x": 17, "y": 20, "angle": 0, "scale": {"x": 1, "y": 1}, "dPath": "M0 -10 L3 -10 L3 0 L0 0 Z"},
        ]
        text = display("TEXT", text="Hi", width=10, height=10, charJSONs=chars, style={"fontSize": 40})
        root, result = self.only_svg(document(text, processing={"id-text": "FILL_VECTOR_ENGRAVING"}))
        self.assertEqual(result.warnings, [])
        self.assertEqual(root.findall(f".//{SVG_NS}text"), [])
        path = self.element(root, "id-text")
        self.assertBox(coords_bbox(path.get("d")), (10, 20, 20, 30))
        self.assertNotEqual(path.get("fill"), "none")

    def test_text_from_embedded_font_glyphs(self):
        font = {"fontInfo": {"lineHeight": 12}, "glyphData": {
            "A": {"dPath": "M0 -8 L6 -8 L6 0 L0 0 Z", "advanceWidth": 7},
            "B": {"dPath": "M0 -8 L5 -8 L5 0 L0 0 Z", "advanceWidth": 6},
        }}
        text = display("TEXT", text="AB", width=12, height=8, fontData=font, style={"fontSize": 30})
        root, result = self.only_svg(document(text))
        self.assertBox(coords_bbox(self.element(root, "id-text").get("d")), (10, 20, 22, 28))
        self.assertTrue(any("Schriftdaten" in w for w in result.warnings))

    def test_embedded_glyphs_are_parsed_once(self):
        glyphs = {"a": {"dPath": "M0 0 L1 1" + " " * 1_000_000 + "L2 0", "advanceWidth": 3},
                  "b": {"dPath": "M0 0 L0 2", "advanceWidth": 1}}
        doc = document(display("TEXT", id="t", text="ab" * 2000, width=0, height=0,
                               fontData={"glyphData": glyphs}))
        real = xcs2svg.parse_path
        with mock.patch.object(xcs2svg, "parse_path", side_effect=real) as parse:
            start = time.monotonic()
            root, _ = self.only_svg(doc)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(parse.call_count, 2)
        self.assertEqual(self.element(root, "t").get("d").count("M"), 4000)

    def test_glyph_reuse_is_capped_per_text(self):
        glyph = "M0 0" + " L1 1" * 199
        doc = document(display("TEXT", id="t", text="a" * 1001, fontData={"glyphData": {"a": {"dPath": glyph}}}),
                       display("RECT", id="r", width=5, height=5))
        root, result = self.only_svg(doc)
        self.assertIsNone(root.find(".//*[@id='e-t']"))
        self.assertIn("Text übersprungen: Text ist zu komplex.", result.warnings)

    def test_text_without_outlines_falls_back_to_svg_text(self):
        text = display("TEXT", text="Hallo\nWelt", width=30, height=12, style={"fontSize": 20, "fontFamily": "Lato"})
        root, result = self.only_svg(document(text))
        node = root.find(f".//{SVG_NS}text")
        self.assertIsNotNone(node)
        self.assertEqual([t.text for t in node.findall(f"{SVG_NS}tspan")], ["Hallo", "Welt"])
        self.assertEqual(node.get("font-family"), "Lato")
        self.assertTrue(any("SVG-Text" in w for w in result.warnings))


class BitmapTests(ConverterTestCase):
    def bitmap(self, uri, **fields):
        return document(display("BITMAP", width=40, height=30, base64=uri, **fields))

    def test_png_is_embedded(self):
        uri = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
        root, result = self.only_svg(self.bitmap(uri))
        image = root.find(f".//{SVG_NS}image")
        self.assertEqual(result.warnings, [])
        self.assertTrue(image.get("{http://www.w3.org/1999/xlink}href").startswith("data:image/png;base64,iVBOR"))
        self.assertEqual((image.get("width"), image.get("height")), ("40", "30"))

    def test_mime_is_taken_from_the_bytes(self):
        uri = "data:image/jpeg;base64," + base64.b64encode(PNG_1PX).decode()
        root, _ = self.only_svg(self.bitmap(uri))
        href = root.find(f".//{SVG_NS}image").get("{http://www.w3.org/1999/xlink}href")
        self.assertTrue(href.startswith("data:image/png;"))

    def test_svg_and_foreign_data_are_rejected(self):
        payloads = [
            "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload='alert(1)'/>").decode(),
            "data:image/png;base64," + base64.b64encode(b"<script>alert(1)</script>").decode(),
            "javascript:alert(1)",
            "data:image/png;base64,***",
        ]
        for uri in payloads:
            with self.subTest(uri=uri[:30]):
                doc = self.bitmap(uri)
                doc["canvas"][0]["displays"].append(display("RECT", id="r", width=1, height=1))
                result = self.convert(doc)
                self.assertNotIn("<image", result.files[0].svg)
                self.assertTrue(any(w.startswith("Bild übersprungen") for w in result.warnings))


class LayerTests(ConverterTestCase):
    def test_processing_types_become_coloured_layers(self):
        doc = document(display("RECT", id="cut", width=10, height=10),
                       display("RECT", id="fill", x=30, width=10, height=10),
                       display("CIRCLE", id="cut2", x=50, width=10, height=10),
                       processing={"cut": "VECTOR_CUTTING", "fill": "FILL_VECTOR_ENGRAVING", "cut2": "VECTOR_CUTTING"})
        root, _ = self.only_svg(doc)
        groups = root.findall(f"{SVG_NS}g")
        self.assertEqual([g.get("data-processing") for g in groups], ["VECTOR_CUTTING", "FILL_VECTOR_ENGRAVING"])
        cut, fill, cut2 = (self.element(root, i) for i in ("cut", "fill", "cut2"))
        self.assertEqual((cut.get("stroke"), cut.get("fill")), ("#000000", "none"))
        self.assertEqual((fill.get("stroke"), fill.get("fill")), ("#0000FF", "#0000FF"))
        self.assertEqual(cut2.get("stroke"), "#000000")

    def test_layer_colour_and_names(self):
        doc = document(display("RECT", id="a", width=5, height=5, layerColor="#FF0000"),
                       display("RECT", id="b", width=5, height=5, layerColor="#00ff00"),
                       display("RECT", id="c", width=5, height=5, layerColor="#ff0000"),
                       layerData={"#FF0000": {"name": EVIL}, "#00FF00": {"name": "Gravur"}})
        root, result = self.only_svg(doc)
        groups = root.findall(f"{SVG_NS}g")
        self.assertEqual([g.get("data-name") for g in groups], [EVIL, "Gravur"])
        self.assertEqual(len(groups[0]), 2)
        self.assertNotIn("<script", result.files[0].svg)

    def test_different_laser_settings_get_separate_layers(self):
        doc = document(display("RECT", id="strong", width=10, height=10),
                       display("RECT", id="weak", x=30, width=10, height=10),
                       display("RECT", id="strong2", x=50, width=10, height=10),
                       processing={"strong": "VECTOR_CUTTING", "weak": "VECTOR_CUTTING", "strong2": "VECTOR_CUTTING"},
                       settings={"strong": {"power": 100, "speed": 10, "repeat": 1},
                                 "weak": {"power": 30, "speed": 10, "repeat": 1},
                                 "strong2": {"power": 100, "speed": 10, "repeat": 1, "breakPointCut": False}})
        root, result = self.only_svg(doc)
        groups = root.findall(f"{SVG_NS}g")
        self.assertEqual([g.get("data-name") for g in groups],
                         ["Schneiden (100 %, 10 mm/s, 1×)", "Schneiden (30 %, 10 mm/s, 1×)"])
        self.assertEqual([g.get("data-processing") for g in groups], ["VECTOR_CUTTING"] * 2)
        strong, weak, strong2 = (self.element(root, i) for i in ("strong", "weak", "strong2"))
        self.assertEqual(strong.get("stroke"), strong2.get("stroke"))
        self.assertNotEqual(strong.get("stroke"), weak.get("stroke"))
        self.assertEqual(result.warnings, [])

    def test_hostile_laser_settings_are_ignored(self):
        doc = document(display("RECT", id="a", width=5, height=5), display("RECT", id="b", width=5, height=5),
                       processing={"a": "VECTOR_CUTTING", "b": "VECTOR_CUTTING"},
                       settings={"a": {"power": EVIL, "speed": True, "repeat": 1e300},
                                 "b": {"power": [1], EVIL: 5}})
        root, result = self.only_svg(doc)
        self.assertEqual([g.get("data-name") for g in root.findall(f"{SVG_NS}g")], ["Schneiden"])
        self.assertNotIn("<script", result.files[0].svg)

    def test_xcs_groups_are_nested_inside_their_layer(self):
        tag = "g-aa9fb573-51a9-4224-8b4d-0cdbbf00dae6"
        doc = document(display("RECT", id="a", width=5, height=5, groupTag=tag),
                       display("RECT", id="loose", width=5, height=5),
                       display("RECT", id="b", width=5, height=5, groupTag=tag),
                       display("RECT", id="fill", width=5, height=5, groupTag=tag),
                       display("RECT", id="fill2", width=5, height=5, groupTag=tag),
                       display("RECT", id="evil", width=5, height=5, groupTag=EVIL),
                       display("RECT", id="evil2", width=5, height=5, groupTag=EVIL),
                       processing={"a": "VECTOR_CUTTING", "loose": "VECTOR_CUTTING", "b": "VECTOR_CUTTING",
                                   "fill": "FILL_VECTOR_ENGRAVING", "fill2": "FILL_VECTOR_ENGRAVING"})
        root, _ = self.only_svg(doc)
        layers = root.findall(f"{SVG_NS}g")
        self.assertEqual(len(layers), 3)
        cut, fill, plain = layers
        nested = cut.find(f"{SVG_NS}g")
        self.assertEqual(nested.get("id"), f"grp-{tag}")
        self.assertEqual([e.get("id") for e in nested], ["e-a", "e-b"])
        self.assertEqual([e.get("id") for e in cut if e.tag == SVG_NS + "path"], ["e-loose"])
        self.assertEqual(fill.find(f"{SVG_NS}g").get("id"), f"grp-{tag}-2")
        self.assertIsNone(plain.find(f"{SVG_NS}g"))  # unsafe tags are not used as groups
        self.assertEqual(self.element(cut, "a").get("stroke"), cut.find(f"{SVG_NS}path").get("stroke"))

    def test_is_fill_used_without_device_settings(self):
        doc = document(display("RECT", id="f", width=5, height=5, isFill=True),
                       display("RECT", id="o", width=5, height=5, isFill=False))
        doc.pop("device")
        root, _ = self.only_svg(doc)
        self.assertNotEqual(self.element(root, "f").get("fill"), "none")
        self.assertEqual(self.element(root, "o").get("fill"), "none")


class DocumentTests(ConverterTestCase):
    def test_multiple_canvases(self):
        doc = document(display("RECT", width=5, height=5))
        doc["canvas"].append({"id": "c2", "title": "Deckel", "displays": [display("CIRCLE", width=5, height=5)]})
        doc["canvas"].append({"id": "c3", "title": "Deckel", "displays": [display("CIRCLE", width=5, height=5)]})
        result = self.convert(doc, "Box.xcs")
        names = [f.name for f in result.files]
        self.assertEqual(len(set(names)), 3)
        self.assertEqual(names, ["Box_Arbeitsfläche 1.svg", "Box_Deckel.svg", "Box_Deckel_3.svg"])

    def test_unknown_types_are_reported(self):
        doc = document(display("RECT", width=5, height=5), display("HOLOGRAM"), display("<b>", width=1))
        result = self.convert(doc)
        self.assertEqual(len(result.files), 1)
        self.assertIn("Elementtyp „HOLOGRAM“ wird nicht unterstützt und wurde übersprungen.", result.warnings)
        self.assertTrue(any("„unbekannt“" in w for w in result.warnings))

    def test_identical_warnings_are_counted(self):
        doc = document(display("RECT", width=5, height=5), *[display("GROUP") for _ in range(3)])
        self.assertIn("Elementtyp „GROUP“ wird nicht unterstützt und wurde übersprungen. (3×)",
                      self.convert(doc).warnings)

    def test_filename_is_sanitised(self):
        result = self.convert(document(display("RECT", width=5, height=5)), r"..\..\böse<>|?.xcs")
        self.assertEqual(result.files[0].name, "böse.svg")
        self.assertEqual(xcs2svg.base_name(""), "zeichnung")


class MalformedInputTests(ConverterTestCase):
    def assertRejected(self, data):
        with self.assertRaises(xcs2svg.XcsError) as ctx:
            xcs2svg.convert(data, "x.xcs")
        self.assertTrue(str(ctx.exception))
        return str(ctx.exception)

    def test_broken_documents(self):
        for data in (b"", b"{", b"\xff\xfe\x00", b"[]", b'"text"', b"null", b"PK\x03\x04rest",
                     b'{"canvas": 1}', b'{"canvas": []}', b'{"canvas": [{"displays": []}]}',
                     b'{"canvas": [{"displays": [{"type": "RECT"}]}]}', b'{"a": NaN}', b'{"a": Infinity}'):
            with self.subTest(data=data):
                self.assertRejected(data)

    def test_deep_nesting(self):
        self.assertRejected(b'{"canvas": ' + b"[" * 100_000 + b"]" * 100_000 + b"}")
        self.assertRejected(json.dumps({"canvas": [{"displays": []}], "x": json.loads("[" * 70 + "]" * 70)}))

    def test_too_many_elements(self):
        displays = [display("LINE", id=f"l{i}", endPoint={"x": 1, "y": 1}) for i in range(xcs2svg.MAX_ELEMENTS + 1)]
        self.assertIn("zu viele Elemente", self.assertRejected(json.dumps(document(*displays))))

    def test_container_bloat_is_rejected_before_parsing(self):
        count = xcs2svg.MAX_JSON_CONTAINERS
        data = b'{"canvas":[{"displays":[' + b"{}," * count + b"{}]}]}"
        with mock.patch.object(xcs2svg.json, "loads", side_effect=AssertionError("parsed")):
            self.assertIn("zu viele verschachtelte", self.assertRejected(data))

    def test_wide_document_is_checked_without_copying_every_child(self):
        doc = {"canvas": [{"displays": [display("RECT", width=5, height=5)]}], "pad": [0] * 1_000_000}
        self.assertEqual(len(xcs2svg.convert(doc).files), 1)
        deep = {"canvas": [{"displays": []}], "x": json.loads("[" * 62 + "]" * 62)}
        self.assertIs(xcs2svg.load(deep), deep)

    def test_conversion_has_a_time_limit(self):
        doc = json.dumps(document(display("RECT", width=5, height=5)))
        with mock.patch.object(xcs2svg, "MAX_SECONDS", -1):
            self.assertIn("dauert zu lange", self.assertRejected(doc))

    def test_too_many_canvases(self):
        doc = {"canvas": [{"displays": []}] * (xcs2svg.MAX_CANVASES + 1)}
        self.assertRejected(json.dumps(doc))

    def test_invalid_numbers_skip_the_element(self):
        bad = [
            display("RECT", id="huge", width=1e300, height=1),
            display("RECT", id="string", width="10", height=1),
            display("RECT", id="bool", width=True, height=1),
            display("RECT", id="angle", width=1, height=1, angle=1e30),
            display("RECT", id="scale", width=1, height=1, scale={"x": "1", "y": 1}),
            display("LINE", id="end", endPoint=[1, 2]),
            display("PATH", id="nan", dPath="M0 0 L1e999 0"),
            display("POLYGON", id="poly", points=[0, 0, "1", 1]),
            display("PEN", id="pen", points=[{"x": 0, "y": 0}, {"x": None, "y": 0}]),
        ]
        result = self.convert(document(display("RECT", id="ok", width=5, height=5), *bad))
        svg = result.files[0].svg
        self.assertIn('id="e-ok"', svg)
        for element in bad:
            self.assertNotIn(f'id="e-{element["id"]}"', svg)
        self.assertGreaterEqual(sum(1 for w in result.warnings if "übersprungen" in w), 5)

    def test_overflowing_literal_skips_the_element(self):
        raw = json.dumps(document(display("RECT", id="ok", width=5, height=5),
                                  display("RECT", id="inf", width="__INF__", height=1)))
        result = xcs2svg.convert(raw.replace('"__INF__"', "1e400"), "x.xcs")
        self.assertNotIn('id="e-inf"', result.files[0].svg)
        self.assertIn('id="e-ok"', result.files[0].svg)

    def test_only_broken_elements_is_an_error(self):
        message = self.assertRejected(json.dumps(document(display("RECT", width="x", height=1))))
        self.assertIn("keine konvertierbaren Elemente", message)


class InjectionTests(ConverterTestCase):
    def test_hostile_strings_are_escaped_or_dropped(self):
        displays = [
            display("RECT", id=EVIL, width=5, height=5, name=EVIL),
            display("TEXT", id="t", text=EVIL + "\x00\x1b", width=20, height=5,
                    style={"fontSize": 12, "fontFamily": 'Arial"; onload="alert(1)'}),
            display("RECT", id="r", width=5, height=5, layerColor='#000000" onload="x'),
        ]
        doc = document(*displays)
        doc["canvas"][0]["title"] = EVIL
        result = self.convert(doc, EVIL + ".xcs")
        svg = result.files[0].svg
        root = parse_svg(svg)
        self.assertNotIn("<script", svg)
        self.assertNotIn("\x00", svg)
        self.assertEqual(root.find(f"{SVG_NS}title").text, xcs2svg._snippet(EVIL, 80))
        self.assertIn(EVIL, root.find(f".//{SVG_NS}tspan").text)
        self.assertNotIn("<", result.files[0].name)
        for el in root.iter():
            self.assertIn(el.tag.split("}")[1], {"svg", "title", "g", "path", "text", "tspan"})
            self.assertFalse(any(name.lower().startswith("on") for name in el.attrib))
        self.assertRegex(root.find(f".//{SVG_NS}text").get("font-family"), r"^[\w \-]+$")

    def test_path_data_outside_the_grammar_is_rejected(self):
        for dpath in ('M0 0 L10 10"/><script>alert(1)</script>', "M0 0 L10 10 url(#x)", "M0 0 X5", "M 0 0 1"):
            with self.subTest(dpath=dpath):
                with self.assertRaises(Exception):
                    xcs2svg.parse_path(dpath)
        doc = document(display("PATH", id="p", dPath='M0 0 L1 1"/><script>'), display("RECT", width=1, height=1))
        result = self.convert(doc)
        self.assertNotIn("script", result.files[0].svg)
        self.assertTrue(any("ungültige Pfaddaten" in w for w in result.warnings))

    def test_output_path_data_is_only_numbers_and_commands(self):
        doc = document(display("PATH", dPath="M0,0 C 1e1 .5 -3-4 5 6 s1 2 3 4 Q1 1 2 2 T3 3 z"))
        root, _ = self.only_svg(doc)
        self.assertRegex(self.element(root, "id-path").get("d"), r"^[MLCQZ0-9 .\-]+$")


class CliTests(unittest.TestCase):
    def test_cli_writes_svg_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "probe.xcs")
            source.write_text(json.dumps(document(display("RECT", width=5, height=5))), encoding="utf-8")
            broken = Path(tmp, "kaputt.xcs")
            broken.write_bytes(b"{")
            out = Path(tmp, "out")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                status = xcs2svg.main([str(source), str(broken), "-o", str(out)])
            self.assertEqual(status, 1)
            self.assertIn("kaputt.xcs", err.getvalue())
            parse_svg((out / "probe.svg").read_text(encoding="utf-8"))


@unittest.skipUnless(FIXTURES.is_dir(), "XCS_FIXTURES not available")
class RealSampleTests(ConverterTestCase):
    def samples(self):
        files = sorted(FIXTURES.glob("*.xcs"))
        if not files:
            self.skipTest("no .xcs files in XCS_FIXTURES")
        return files

    def test_samples_convert_and_match_their_boxes(self):
        for source in self.samples():
            with self.subTest(sample=source.name):
                doc = json.loads(source.read_bytes())
                result = xcs2svg.convert(source.read_bytes(), source.name)
                self.assertEqual(len(result.files), len(doc["canvas"]))
                for canvas, file in zip(doc["canvas"], result.files):
                    root = parse_svg(file.svg)
                    self.assertTrue(root.get("width").endswith("mm"))
                    self.check_canvas(canvas, root)

    def check_canvas(self, canvas, root):
        for disp in canvas["displays"]:
            node = root.find(f".//*[@id='e-{disp['id']}']")
            self.assertIsNotNone(node, f"{disp['type']} {disp['id']} missing")
            if disp.get("angle") or any(disp["skew"].values()) or node.tag != SVG_NS + "path":
                continue
            box = exact_bbox(node.get("d"))
            expected = (disp["x"], disp["y"], disp["x"] + disp["width"], disp["y"] + disp["height"])
            for got, want in zip(box, expected):
                self.assertAlmostEqual(got, want, delta=0.05, msg=f"{disp['type']} {box} != {expected}")

    def test_grouped_sample_elements_stay_grouped(self):
        source = FIXTURES / "convert_to_svg.xcs"
        if not source.is_file():
            self.skipTest("convert_to_svg.xcs not available")
        doc = json.loads(source.read_bytes())
        result = xcs2svg.convert(source.read_bytes(), source.name)
        checked = 0
        for canvas, file in zip(doc["canvas"], result.files):
            root = parse_svg(file.svg)
            grouped = {}
            for disp in canvas["displays"]:
                if disp.get("groupTag"):
                    grouped.setdefault(disp["groupTag"], []).append(f"e-{disp['id']}")
            for tag, ids in grouped.items():
                with self.subTest(group=tag):
                    group = root.find(f".//*[@id='grp-{tag}']")
                    self.assertIsNotNone(group)
                    self.assertEqual(sorted(e.get("id") for e in group), sorted(ids))
                    checked += 1
        self.assertGreater(checked, 0)

    def test_rotated_copies_keep_their_centre_line(self):
        source = FIXTURES / "rect.xcs"
        if not source.is_file():
            self.skipTest("rect.xcs not available")
        root = parse_svg(xcs2svg.convert(source.read_bytes(), "rect.xcs").files[0].svg)
        centres = [(b[1] + b[3]) / 2 for b in (exact_bbox(p.get("d")) for p in root.iter(SVG_NS + "path"))]
        self.assertLess(max(centres) - min(centres), 1.0)


if __name__ == "__main__":
    unittest.main()
