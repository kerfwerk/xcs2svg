"""Convert xTool Creative Space (.xcs) projects into SVG files for LightBurn.

XCS is JSON. Coordinates are millimetres on the machine bed, y pointing down.
Every element ("display") is placed like a PixiJS display object: ``x``/``y``
is where the element's local origin lands, ``angle`` (degrees) rotates around
that point, ``skew`` is in radians. For geometry stored as data (PATH, PEN,
POLYGON, glyphs) the local origin is the top-left corner of the geometry's
bounding box and ``scale`` applies; for parametric shapes (RECT, CIRCLE,
BITMAP) ``width``/``height`` already contain the scale.

All geometry is parsed into numbers, transformed and written back as absolute
path data, so no string from the input file reaches the SVG unparsed.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from . import xstudio

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_CONTAINERS = 1_000_000  # objects + arrays; counted before parsing to bound memory
# Keys + values of all JSON parsed for one upload (every bundle member and .xs chunk), counted before
# parsing: each costs 30-60 bytes as a Python object, bytes alone do not bound the memory.
MAX_JSON_VALUES = 4_000_000
MAX_CANVASES = 100
MAX_ELEMENTS = 20_000
MAX_SEGMENTS = 1_000_000  # whole file; each segment costs ~250 bytes while it is processed
MAX_TEXT_SEGMENTS = 200_000  # one text element (glyphs are reused per character)
MAX_PATH_CHARS = 5_000_000
MAX_TEXT_CHARS = 10_000
MAX_SECONDS = 60.0  # wall-clock budget of one conversion
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_COORD = 1e6
MAX_SCALE = 1e4
EPS = 1e-9

# LightBurn's default layer colours 00..29; LightBurn creates one layer per colour.
LIGHTBURN_PALETTE = (
    "#000000", "#0000FF", "#FF0000", "#00E000", "#D0D000", "#FF8000",
    "#00E0E0", "#FF00FF", "#B4B4B4", "#0000A0", "#A00000", "#00A000",
    "#A0A000", "#C08000", "#00A0FF", "#A000A0", "#808080", "#7D87B9",
    "#BB7784", "#4A6FE3", "#D33F6A", "#8CD78C", "#F0B98D", "#F6C4E1",
    "#FA9ED4", "#500A78", "#B45A00", "#004754", "#86FA88", "#FFDB66",
)

PROCESS_NAMES = {
    "VECTOR_CUTTING": "Schneiden",
    "VECTOR_ENGRAVING": "Linien gravieren",
    "FILL_VECTOR_ENGRAVING": "Flächengravur",
    "COLOR_FILL_ENGRAVE": "Flächengravur",
    "BITMAP_ENGRAVING": "Bildgravur",
    "KNIFE_CUTTING": "Messerschnitt",
}
FILL_PROCESSES = {"FILL_VECTOR_ENGRAVING", "COLOR_FILL_ENGRAVE"}

# Laser settings that separate layers and are shown in the layer name, in this order.
PARAMETER_LABELS = (
    ("power", "{} %"),
    ("speed", "{} mm/s"),
    ("repeat", "{}×"),
    ("density", "Dichte {}"),
    ("cutPressure", "Druck {}"),
)

TYPE_NAMES = {
    "RECT": "Rechteck",
    "CIRCLE": "Kreis/Ellipse",
    "LINE": "Linie",
    "PATH": "Pfad",
    "PEN": "Zeichenstift-Pfad",
    "POLYGON": "Polygon",
    "TEXT": "Text",
    "BITMAP": "Bild",
}

IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)

STROKE_WIDTH = "0.2"
KAPPA = 0.5522847498307936  # cubic Bezier approximation of a quarter circle

_TOKEN = re.compile(r"[A-Z0-9_]{1,40}")
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_PATH_CHARS = re.compile(r"[MmLlHhVvCcSsQqTtAaZz0-9eE.,+\-\s]*")
_PATH_SPACE = re.compile(r"[ \t\r\n,]*")
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_DATA_URI = re.compile(r"data:image/(png|jpe?g|gif|bmp);base64,([A-Za-z0-9+/=\s]+)", re.IGNORECASE)
_XML_INVALID = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_FILE_UNSAFE = re.compile(r"[^\w.\- ]+")
_FONT_UNSAFE = re.compile(r"[^\w \-]+")


class XcsError(ValueError):
    """The file cannot be converted. The message is German and meant for users."""


class _Skip(Exception):
    """A single element cannot be converted; the reason is German."""


@dataclass
class SvgFile:
    name: str
    title: str
    svg: str


@dataclass
class Result:
    files: list[SvgFile]
    warnings: list[str]


@dataclass
class _Drawable:
    tag: str
    attrs: dict[str, str]
    bbox: tuple[float, float, float, float]
    fillable: bool = True
    text: str | None = None


@dataclass
class _Layer:
    name: str
    processing: str | None
    items: list[tuple[_Drawable, bool, str | None]] = field(default_factory=list)  # drawable, filled, group


# --- small helpers ---------------------------------------------------------

def _xml(value: str) -> str:
    return escape(_XML_INVALID.sub("", value), {'"': "&quot;"})


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _finite(value: float, limit: float = MAX_COORD) -> float:
    if not math.isfinite(value) or abs(value) > limit:
        raise _Skip("ungültige Zahlenwerte")
    return value


def _num(obj: dict, key: str, default: float | None = None, limit: float = MAX_COORD) -> float:
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Skip(f"Feld „{key}“ fehlt oder ist keine Zahl")
    return _finite(float(value), limit)


def _point(value, default: tuple[float, float] | None = None, limit: float = MAX_COORD) -> tuple[float, float]:
    if value is None and default is not None:
        return default
    if not isinstance(value, dict):
        raise _Skip("ungültiger Punkt")
    return _num(value, "x", limit=limit), _num(value, "y", limit=limit)


def _snippet(text: str, length: int = 30) -> str:
    clean = " ".join(_XML_INVALID.sub("", text).split())
    return clean if len(clean) <= length else clean[: length - 1] + "…"


def _token(value) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def base_name(filename: str | None) -> str:
    stem = Path(filename or "").name
    for suffix in (".xcs", ".xs"):
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    stem = _FILE_UNSAFE.sub("_", stem).strip(" ._")[:80]
    return stem or "zeichnung"


# --- affine matrices (a, b, c, d, e, f): x' = a*x + c*y + e, y' = b*x + d*y + f

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _multiply(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D,
            a * E + c * F + e, b * E + d * F + f)


def _translate(tx: float, ty: float):
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


def _apply(m, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _placement(disp: dict, sx: float, sy: float):
    """Local-to-canvas matrix of an element, PixiJS style."""
    x, y = _num(disp, "x"), _num(disp, "y")
    angle = math.radians(_num(disp, "angle", 0, limit=1e5))
    kx, ky = _point(disp.get("skew"), (0.0, 0.0), limit=10)
    px, py = _point(disp.get("pivot"), (0.0, 0.0))
    a, b = math.cos(angle + ky) * sx, math.sin(angle + ky) * sx
    c, d = -math.sin(angle - kx) * sy, math.cos(angle - kx) * sy
    return (a, b, c, d, x - (px * a + py * c), y - (px * b + py * d))


def _scale(disp: dict) -> tuple[float, float]:
    return _point(disp.get("scale"), (1.0, 1.0), limit=MAX_SCALE)


def _sign(value: float) -> float:
    return -1.0 if value < 0 else 1.0


# --- path data: parsed into absolute M/L/Q/C/Z segments ----------------------

class _PathReader:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def _skip(self) -> None:
        self.pos = _PATH_SPACE.match(self.text, self.pos).end()

    def done(self) -> bool:
        self._skip()
        return self.pos >= len(self.text)

    def command(self) -> str | None:
        self._skip()
        char = self.text[self.pos]
        if char in "MmLlHhVvCcSsQqTtAaZz":
            self.pos += 1
            return char
        return None

    def number(self) -> float:
        self._skip()
        match = _NUMBER.match(self.text, self.pos)
        if not match:
            raise _Skip("ungültige Pfaddaten")
        self.pos = match.end()
        return _finite(float(match.group()))

    def flag(self) -> bool:
        self._skip()
        if self.pos < len(self.text) and self.text[self.pos] in "01":
            self.pos += 1
            return self.text[self.pos - 1] == "1"
        raise _Skip("ungültige Pfaddaten")


def parse_path(data: str, max_segments: int = MAX_SEGMENTS) -> list[tuple]:
    if not isinstance(data, str) or len(data) > MAX_PATH_CHARS or not _PATH_CHARS.fullmatch(data):
        raise _Skip("ungültige Pfaddaten")
    reader = _PathReader(data)
    segments: list[tuple] = []
    command = None
    cx = cy = sx = sy = 0.0
    last_cubic = last_quad = None
    while not reader.done():
        letter = reader.command()
        if letter is None:
            if command is None or command in "Zz":
                raise _Skip("ungültige Pfaddaten")
            letter = command
        if letter in "Zz":
            segments.append(("Z",))
            cx, cy = sx, sy
            command, last_cubic, last_quad = letter, None, None
            continue
        if not segments and letter not in "Mm":
            segments.append(("M", 0.0, 0.0))
        ox, oy = (cx, cy) if letter.islower() else (0.0, 0.0)
        kind = letter.upper()
        cubic = quad = None
        if kind == "M":
            cx, cy = reader.number() + ox, reader.number() + oy
            sx, sy = cx, cy
            segments.append(("M", cx, cy))
            letter = "l" if letter == "m" else "L"
        elif kind in "LHV":
            x = reader.number() + ox if kind != "V" else cx
            y = reader.number() + oy if kind != "H" else cy
            cx, cy = x, y
            segments.append(("L", cx, cy))
        elif kind in "CS":
            if kind == "C":
                x1, y1 = reader.number() + ox, reader.number() + oy
            else:
                x1, y1 = (2 * cx - last_cubic[0], 2 * cy - last_cubic[1]) if last_cubic else (cx, cy)
            x2, y2 = reader.number() + ox, reader.number() + oy
            cx, cy = reader.number() + ox, reader.number() + oy
            segments.append(("C", x1, y1, x2, y2, cx, cy))
            cubic = (x2, y2)
        elif kind in "QT":
            if kind == "Q":
                x1, y1 = reader.number() + ox, reader.number() + oy
            else:
                x1, y1 = (2 * cx - last_quad[0], 2 * cy - last_quad[1]) if last_quad else (cx, cy)
            cx, cy = reader.number() + ox, reader.number() + oy
            segments.append(("Q", x1, y1, cx, cy))
            quad = (x1, y1)
        else:  # arc
            rx, ry = reader.number(), reader.number()
            rotation = reader.number()
            large, sweep = reader.flag(), reader.flag()
            x, y = reader.number() + ox, reader.number() + oy
            segments.extend(_arc_to_cubics(cx, cy, rx, ry, rotation, large, sweep, x, y))
            cx, cy = x, y
        command, last_cubic, last_quad = letter, cubic, quad
        if len(segments) > max_segments:
            raise _Skip("zu viele Pfadsegmente")
    return segments


def _arc_to_cubics(x1, y1, rx, ry, rotation, large, sweep, x2, y2) -> list[tuple]:
    """SVG elliptical arc to cubic Beziers (SVG 1.1 implementation notes F.6.5)."""
    if (x1, y1) == (x2, y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if rx < EPS or ry < EPS:
        return [("L", x2, y2)]
    phi = math.radians(rotation % 360)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cos_p * dx + sin_p * dy, -sin_p * dx + cos_p * dy
    radii = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
    if radii > 1:
        rx, ry = rx * math.sqrt(radii), ry * math.sqrt(radii)
    numerator = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, numerator / denominator)) if denominator > 0 else 0.0
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    center_x = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2
    center_y = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    theta = angle(1, 0, ux, uy)
    delta = angle(ux, uy, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi

    def to_canvas(px, py):
        return (center_x + rx * px * cos_p - ry * py * sin_p,
                center_y + rx * px * sin_p + ry * py * cos_p)

    count = max(1, math.ceil(abs(delta) / (math.pi / 2) - 1e-9))
    step = delta / count
    k = 4 / 3 * math.tan(step / 4)
    segments = []
    for i in range(count):
        t1, t2 = theta + i * step, theta + (i + 1) * step
        c1 = to_canvas(math.cos(t1) - k * math.sin(t1), math.sin(t1) + k * math.cos(t1))
        c2 = to_canvas(math.cos(t2) + k * math.sin(t2), math.sin(t2) - k * math.cos(t2))
        end = (x2, y2) if i == count - 1 else to_canvas(math.cos(t2), math.sin(t2))
        segments.append(("C", *c1, *c2, *end))
    return segments


def _transform(segments: list[tuple], m) -> list[tuple]:
    """Transforms the list in place (one copy of the geometry in memory) and returns it."""
    for index, seg in enumerate(segments):
        coords = seg[1:]
        moved = []
        for i in range(0, len(coords), 2):
            moved.extend(_apply(m, coords[i], coords[i + 1]))
        segments[index] = (seg[0], *moved)
    return segments


def _bezier_roots(p0, p1, p2, p3=None) -> list[float]:
    """Parameters in (0, 1) where a quadratic/cubic Bezier has an extremum."""
    if p3 is None:
        denom = p0 - 2 * p1 + p2
        return [t for t in ([(p0 - p1) / denom] if abs(denom) > EPS else []) if 0 < t < 1]
    a, b, c = p1 - p0, p2 - p1, p3 - p2
    qa, qb, qc = a - 2 * b + c, 2 * (b - a), a
    if abs(qa) < EPS:
        roots = [-qc / qb] if abs(qb) > EPS else []
    else:
        disc = qb * qb - 4 * qa * qc
        if disc < 0:
            return []
        root = math.sqrt(disc)
        roots = [(-qb + root) / (2 * qa), (-qb - root) / (2 * qa)]
    return [t for t in roots if 0 < t < 1]


def _bezier_at(t, p0, p1, p2, p3=None) -> float:
    u = 1 - t
    if p3 is None:
        return u * u * p0 + 2 * u * t * p1 + t * t * p2
    return u ** 3 * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t ** 3 * p3


def path_bbox(segments: list[tuple]) -> tuple[float, float, float, float] | None:
    """Exact bounding box (min_x, min_y, max_x, max_y) of parsed segments."""
    xs: list[float] = []
    ys: list[float] = []
    cx = cy = sx = sy = 0.0
    for seg in segments:
        kind = seg[0]
        if kind == "Z":
            cx, cy = sx, sy
            continue
        if kind == "M":
            cx, cy = sx, sy = seg[1], seg[2]
        elif kind in ("C", "Q"):
            px = (cx, *seg[1::2])
            py = (cy, *seg[2::2])
            xs.extend(_bezier_at(t, *px) for t in _bezier_roots(*px))
            ys.extend(_bezier_at(t, *py) for t in _bezier_roots(*py))
            cx, cy = seg[-2], seg[-1]
        else:
            cx, cy = seg[1], seg[2]
        xs.append(cx)
        ys.append(cy)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def path_data(segments: list[tuple]) -> str:
    return " ".join(seg[0] + " ".join(_fmt(v) for v in seg[1:]) for seg in segments)


# --- local geometry of the element types ------------------------------------

def _rect(w: float, h: float, r: float) -> list[tuple]:
    r = max(0.0, min(r, w / 2, h / 2))
    if r < EPS:
        return [("M", 0.0, 0.0), ("L", w, 0.0), ("L", w, h), ("L", 0.0, h), ("Z",)]
    k = r * (1 - KAPPA)
    return [
        ("M", r, 0.0), ("L", w - r, 0.0), ("C", w - k, 0.0, w, k, w, r),
        ("L", w, h - r), ("C", w, h - k, w - k, h, w - r, h),
        ("L", r, h), ("C", k, h, 0.0, h - k, 0.0, h - r),
        ("L", 0.0, r), ("C", 0.0, k, k, 0.0, r, 0.0), ("Z",),
    ]


def _ellipse(w: float, h: float) -> list[tuple]:
    rx, ry = w / 2, h / 2
    ox, oy = rx * KAPPA, ry * KAPPA
    return [
        ("M", w, ry),
        ("C", w, ry + oy, rx + ox, h, rx, h),
        ("C", rx - ox, h, 0.0, ry + oy, 0.0, ry),
        ("C", 0.0, ry - oy, rx - ox, 0.0, rx, 0.0),
        ("C", rx + ox, 0.0, w, ry - oy, w, ry),
        ("Z",),
    ]


def _polygon(points, closed: bool) -> list[tuple]:
    if not isinstance(points, list) or len(points) > MAX_SEGMENTS:
        raise _Skip("ungültige Punktliste")
    if points and isinstance(points[0], dict):
        coords = [_point(p) for p in points]
    else:
        if len(points) % 2:
            raise _Skip("ungültige Punktliste")
        values = []
        for value in points:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _Skip("ungültige Punktliste")
            values.append(_finite(float(value)))
        coords = list(zip(values[::2], values[1::2]))
    if len(coords) < 2:
        raise _Skip("zu wenige Punkte")
    segments = [("M", *coords[0])] + [("L", *p) for p in coords[1:]]
    return segments + [("Z",)] if closed else segments


def _pen(disp: dict) -> list[tuple]:
    """Pen tool paths: absolute anchor points plus optional [in, out] handles per index."""
    points = disp.get("points")
    if not isinstance(points, list) or not 2 <= len(points) <= MAX_SEGMENTS:
        raise _Skip("ungültige Punktliste")
    anchors = [_point(p) for p in points]
    raw_handles = disp.get("controlPoints") or {}
    if not isinstance(raw_handles, dict):
        raise _Skip("ungültige Kontrollpunkte")
    handles: dict[int, tuple] = {}
    for key, pair in raw_handles.items():
        if not (isinstance(key, str) and key.isdigit() and isinstance(pair, list) and len(pair) == 2):
            raise _Skip("ungültige Kontrollpunkte")
        handles[int(key)] = (_point(pair[0]), _point(pair[1]))

    def segment(i: int, j: int) -> tuple:
        out_handle = handles[i][1] if i in handles else None
        in_handle = handles[j][0] if j in handles else None
        end = anchors[j]
        if out_handle and in_handle:
            return ("C", *out_handle, *in_handle, *end)
        if out_handle or in_handle:  # one handle: xTool draws a quadratic curve
            return ("Q", *(out_handle or in_handle), *end)
        return ("L", *end)

    segments = [("M", *anchors[0])]
    segments += [segment(i - 1, i) for i in range(1, len(anchors))]
    if disp.get("isClosePath"):
        segments += [segment(len(anchors) - 1, 0), ("Z",)]
    return segments


# --- the converter ---------------------------------------------------------

def _settings(entry: dict, processing: str) -> tuple[tuple[str, float], ...]:
    """The active laser parameters (power, speed, ...) of one element for its processing type."""
    data = entry.get("data")
    block = data.get(processing) if isinstance(data, dict) else None
    params = block.get("parameter") if isinstance(block, dict) else None
    if not isinstance(params, dict):
        return ()
    material = block.get("materialType")
    chosen = params.get(material) if isinstance(material, str) else None
    if not isinstance(chosen, dict):
        candidates = [value for value in params.values() if isinstance(value, dict)]
        chosen = candidates[0] if len(candidates) == 1 else {}
    settings = []
    for key, _ in PARAMETER_LABELS:
        value = chosen.get(key)
        if (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and abs(value) <= MAX_COORD):
            settings.append((key, float(value)))
    return tuple(settings)


def _processing_map(device) -> dict[str, dict[str, tuple[str, tuple]]]:
    """canvas id -> display id -> (processing type, laser settings), from the device settings."""
    result: dict[str, dict[str, tuple[str, tuple]]] = {}
    data = device.get("data") if isinstance(device, dict) else None
    entries = data.get("value") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return result
    for entry in entries[:MAX_CANVASES]:
        if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], dict)):
            continue
        displays = entry[1].get("displays")
        values = displays.get("value") if isinstance(displays, dict) else None
        mapping = {}
        for item in values[:MAX_ELEMENTS] if isinstance(values, list) else []:
            if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict):
                processing = _token(item[1].get("processingType"))
                if isinstance(item[0], str) and processing:
                    mapping[item[0]] = (processing, _settings(item[1], processing))
        if isinstance(entry[0], str):
            result[entry[0]] = mapping
    return result


def _children(value):
    return iter(value.values() if isinstance(value, dict) else value)


def _check_depth(doc) -> None:
    """Depth-first walk that keeps one iterator per level, so memory stays O(depth)."""
    stack = [_children(doc)]
    while stack:
        for value in stack[-1]:
            if isinstance(value, (dict, list)):
                if len(stack) >= MAX_JSON_DEPTH:
                    raise XcsError("Die Datei ist zu tief verschachtelt.")
                stack.append(_children(value))
                break
        else:
            stack.pop()


def _reject_constant(name: str):
    raise ValueError(name)


def new_budget() -> xstudio.Budget:
    """Unpacked bytes and JSON values allowed for one upload."""
    return xstudio.Budget(MAX_INPUT_BYTES, MAX_JSON_VALUES)


def load(data, budget: xstudio.Budget | None = None) -> dict:
    if isinstance(data, dict):
        doc = data
    else:
        if isinstance(data, str):
            data = data.encode("utf-8")
        if len(data) > MAX_INPUT_BYTES:
            raise XcsError("Die Datei ist zu groß.")
        budget = budget or new_budget()
        if data[:2] == b"PK":
            try:
                doc = xstudio.load(data, MAX_JSON_CONTAINERS, budget)
            except xstudio.XsError as exc:
                raise XcsError(str(exc)) from None
            _check_depth(doc)
            return doc
        # Upper bound of the JSON objects/arrays (brackets inside strings count too), checked
        # before json.loads materialises them: millions of tiny containers need gigabytes.
        if data.count(b"{") + data.count(b"[") > MAX_JSON_CONTAINERS:
            raise XcsError("Die Datei enthält zu viele verschachtelte Einträge.")
        try:
            budget.charge(data)
        except xstudio.XsError as exc:
            raise XcsError(str(exc)) from None
        try:
            doc = json.loads(data.decode("utf-8-sig"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise XcsError("Die Datei ist keine gültige XCS-Datei (kein lesbares JSON).") from None
    if not isinstance(doc, dict):
        raise XcsError("Die Datei ist keine gültige XCS-Datei.")
    _check_depth(doc)
    return doc


class _Converter:
    def __init__(self, doc: dict):
        self.doc = doc
        self.warnings: Counter[str] = Counter()
        self.elements_left = MAX_ELEMENTS
        self.segments_left = MAX_SEGMENTS
        self.deadline = time.monotonic() + MAX_SECONDS
        self.processing = _processing_map(doc.get("device"))
        self.used_ids: set[str] = set()

    def warn(self, message: str) -> None:
        self.warnings[message] += 1

    def check_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise XcsError("Die Umwandlung dauert zu lange – die Datei ist zu komplex.")

    def spend(self, segments: list[tuple]) -> list[tuple]:
        self.check_time()
        self.segments_left -= len(segments)
        if self.segments_left < 0:
            raise XcsError("Die Datei enthält zu viele Pfadsegmente.")
        return segments

    def parse(self, data, limit: int = MAX_SEGMENTS) -> list[tuple]:
        return self.spend(parse_path(data, max(min(self.segments_left, limit), 0)))

    # each builder returns a drawable in canvas coordinates
    def path_drawable(self, segments: list[tuple]) -> _Drawable:
        bbox = path_bbox(segments)
        if bbox is None:
            raise _Skip("keine Geometrie")
        return _Drawable("path", {"d": path_data(segments)}, bbox)

    def placed_geometry(self, disp: dict, segments: list[tuple], scale=None) -> list[tuple]:
        """Geometry whose bounding-box corner is the element's local origin."""
        bbox = path_bbox(segments)
        if bbox is None:
            raise _Skip("keine Geometrie")
        sx, sy = scale or _scale(disp)
        m = _multiply(_placement(disp, sx, sy), _translate(-bbox[0], -bbox[1]))
        return _transform(segments, m)

    def geometry_of(self, disp: dict, kind: str) -> list[tuple]:
        dpath = disp.get("dPath")
        if isinstance(dpath, str) and dpath.strip():
            return self.parse(dpath)
        if kind == "PEN":
            return self.spend(_pen(disp))
        if kind == "POLYGON":
            return self.spend(_polygon(disp.get("points"), disp.get("isClosePath") is not False))
        raise _Skip("keine Pfaddaten")

    def element(self, disp: dict, kind: str) -> _Drawable:
        if kind in ("RECT", "CIRCLE"):
            w, h = abs(_num(disp, "width")), abs(_num(disp, "height"))
            sx, sy = _scale(disp)
            m = _placement(disp, _sign(sx), _sign(sy))
            if kind == "RECT":
                radius = next((float(disp[k]) for k in ("radius", "cornerRadius", "borderRadius")
                               if isinstance(disp.get(k), (int, float)) and not isinstance(disp.get(k), bool)), 0.0)
                local = _rect(w, h, _finite(radius))
            else:
                local = _ellipse(w, h)
            return self.path_drawable(_transform(self.spend(local), m))
        if kind == "LINE":
            end = _point(disp.get("endPoint"))
            m = _placement(disp, *_scale(disp))
            line = self.spend([("M", 0.0, 0.0), ("L", *end)])
            drawable = self.path_drawable(_transform(line, m))
            drawable.fillable = False
            return drawable
        if kind in ("PATH", "PEN", "POLYGON"):
            return self.path_drawable(self.placed_geometry(disp, self.geometry_of(disp, kind)))
        if kind == "TEXT":
            return self.text(disp)
        if kind == "BITMAP":
            return self.bitmap(disp)
        raise LookupError(kind)

    def text(self, disp: dict) -> _Drawable:
        content = disp.get("text") if isinstance(disp.get("text"), str) else ""
        if len(content) > MAX_TEXT_CHARS:
            raise _Skip("Text ist zu lang")
        chars = disp.get("charJSONs")
        if isinstance(chars, list) and chars:
            segments: list[tuple] = []
            prerotated = False
            for char in chars:
                if not isinstance(char, dict):
                    continue
                self.take_element()
                glyph = char.get("dPath")
                if not isinstance(glyph, str) or not glyph.strip():
                    continue
                segments += self.placed_geometry(char, self.parse(glyph, MAX_TEXT_SEGMENTS - len(segments)))
                prerotated = prerotated or abs(_num(char, "angle", 0, limit=1e5)) > EPS
            if segments:
                return self.path_drawable(segments if prerotated else self.placed_text(disp, segments))
        font = disp.get("fontData")
        glyphs = font.get("glyphData") if isinstance(font, dict) else None
        if isinstance(glyphs, dict) and content.strip():
            segments = self.layout_glyphs(disp, content, font, glyphs)
            if segments:
                self.warn("Text wurde aus den eingebetteten Schriftdaten gesetzt – Zeichenabstände bitte prüfen.")
                return self.path_drawable(self.placed_text(disp, segments))
        return self.text_fallback(disp, content)

    def placed_text(self, disp: dict, segments: list[tuple]) -> list[tuple]:
        # Glyph geometry may or may not already contain the text's scale, so
        # derive it from the stored size instead of trusting "scale".
        bbox = path_bbox(segments)
        sx, sy = _scale(disp)
        width, height = abs(_num(disp, "width", 0)), abs(_num(disp, "height", 0))
        gw, gh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        fx = width / gw if gw > 1e-6 and width > 1e-6 else abs(sx)
        fy = height / gh if gh > 1e-6 and height > 1e-6 else abs(sy)
        return self.placed_geometry(disp, segments, (fx * _sign(sx), fy * _sign(sy)))

    def layout_glyphs(self, disp, content, font, glyphs) -> list[tuple]:
        style = disp.get("style") if isinstance(disp.get("style"), dict) else {}
        size = _num(style, "fontSize", 12, limit=1e4) * 25.4 / 72
        info = font.get("fontInfo") if isinstance(font.get("fontInfo"), dict) else {}
        line_height = _num(info, "lineHeight", size * 1.2)
        segments: list[tuple] = []
        outlines: dict[str, list[tuple]] = {}  # each glyph is parsed once, however often it is used
        missing = False
        for row, line in enumerate(content.split("\n")):
            pen_x, baseline = 0.0, row * line_height
            for char in line:
                glyph = glyphs.get(char)
                if not isinstance(glyph, dict):
                    missing = missing or not char.isspace()
                    pen_x += size * 0.25
                    continue
                self.take_element()
                outline = outlines.get(char)
                if outline is None:
                    raw = glyph.get("dPath")
                    valid = isinstance(raw, str) and raw.strip()
                    outline = outlines[char] = parse_path(raw, MAX_TEXT_SEGMENTS) if valid else []
                if outline:
                    if len(segments) + len(outline) > MAX_TEXT_SEGMENTS:
                        raise _Skip("Text ist zu komplex")
                    segments += _transform(list(self.spend(outline)), _translate(pen_x, baseline))
                pen_x += _num(glyph, "advanceWidth", size * 0.5)
        if missing:
            self.warn("Für einige Textzeichen fehlen Glyphen-Daten; sie wurden ausgelassen.")
        return segments

    def text_fallback(self, disp: dict, content: str) -> _Drawable:
        if not content.strip():
            raise _Skip("leerer Text")
        style = disp.get("style") if isinstance(disp.get("style"), dict) else {}
        size = _num(style, "fontSize", 12, limit=1e4) * 25.4 / 72
        family = style.get("fontFamily") if isinstance(style.get("fontFamily"), str) else ""
        family = _FONT_UNSAFE.sub("", family)[:64].strip() or "sans-serif"
        lines = content.split("\n")
        m = _placement(disp, *_scale(disp))
        width = abs(_num(disp, "width", size * 0.6 * max(len(line) for line in lines)))
        height = abs(_num(disp, "height", size * 1.2 * len(lines)))
        corners = [_apply(m, x, y) for x in (0, width) for y in (0, height)]
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        tspans = "".join(
            f'<tspan x="0" y="{_fmt(size * (0.8 + 1.2 * i))}">{_xml(line)}</tspan>'
            for i, line in enumerate(lines)
        )
        self.warn(f"Text „{_snippet(content)}“ enthält keine Pfade und wurde als SVG-Text ausgegeben – "
                  "in LightBurn Schriftart prüfen oder in xTool vorher in Pfade umwandeln.")
        attrs = {"transform": "matrix(" + " ".join(_fmt(v) for v in m) + ")",
                 "font-family": family, "font-size": _fmt(size)}
        return _Drawable("text", attrs, (min(xs), min(ys), max(xs), max(ys)), text=tspans)

    def bitmap(self, disp: dict) -> _Drawable:
        source = disp.get("base64")
        match = _DATA_URI.fullmatch(source) if isinstance(source, str) else None
        if not match:
            raise _Skip("Bilddaten fehlen oder Format wird nicht unterstützt (nur PNG, JPEG, GIF, BMP)")
        if len(source) > MAX_IMAGE_BYTES * 4 // 3 + 1024:
            raise _Skip("Bild ist zu groß")
        try:
            raw = base64.b64decode("".join(match.group(2).split()), validate=True)
        except (binascii.Error, ValueError):
            raise _Skip("Bilddaten sind beschädigt") from None
        mime = next((name for magic, name in IMAGE_MAGIC if raw.startswith(magic)), None)
        if mime is None:
            raise _Skip("Bilddaten sind kein PNG, JPEG, GIF oder BMP")
        w, h = abs(_num(disp, "width")), abs(_num(disp, "height"))
        sx, sy = _scale(disp)
        m = _placement(disp, _sign(sx), _sign(sy))
        corners = [_apply(m, x, y) for x in (0, w) for y in (0, h)]
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        attrs = {
            "x": "0", "y": "0", "width": _fmt(w), "height": _fmt(h),
            "preserveAspectRatio": "none",
            "transform": "matrix(" + " ".join(_fmt(v) for v in m) + ")",
            "xlink:href": f"data:image/{mime};base64,{base64.b64encode(raw).decode('ascii')}",
        }
        return _Drawable("image", attrs, (min(xs), min(ys), max(xs), max(ys)), fillable=False)

    def take_element(self) -> None:
        self.check_time()
        self.elements_left -= 1
        if self.elements_left < 0:
            raise XcsError(f"Die Datei enthält zu viele Elemente (maximal {MAX_ELEMENTS}).")

    def element_id(self, value) -> str | None:
        if isinstance(value, str) and _SAFE_ID.fullmatch(value):
            candidate = f"e-{value}"
            if candidate not in self.used_ids:
                self.used_ids.add(candidate)
                return candidate
        return None

    def canvas(self, canvas: dict, title: str) -> str | None:
        displays = canvas.get("displays")
        if not isinstance(displays, list):
            self.warn(f"„{_snippet(title)}“ enthält keine Elementliste und wurde übersprungen.")
            return None
        canvas_id = canvas.get("id") if isinstance(canvas.get("id"), str) else ""
        processing_of = self.processing.get(canvas_id, {})
        layer_data = canvas.get("layerData") if isinstance(canvas.get("layerData"), dict) else {}
        layers: dict[str, _Layer] = {}
        skewed = False
        for disp in displays:
            self.take_element()
            if not isinstance(disp, dict):
                self.warn("Ein ungültiger Eintrag in der Elementliste wurde übersprungen.")
                continue
            kind = disp.get("type")
            label = TYPE_NAMES.get(kind) if isinstance(kind, str) else None
            if label is None:
                shown = _token(kind) or "unbekannt"
                self.warn(f"Elementtyp „{shown}“ wird nicht unterstützt und wurde übersprungen.")
                continue
            try:
                drawable = self.element(disp, kind)
            except _Skip as reason:
                self.warn(f"{label} übersprungen: {reason}.")
                continue
            skew = disp.get("skew")
            if isinstance(skew, dict) and any(isinstance(skew.get(k), (int, float)) and abs(skew[k]) > EPS
                                              for k in ("x", "y")):
                skewed = True
            display_id = disp.get("id")
            known = processing_of.get(display_id) if isinstance(display_id, str) else None
            processing, settings = known or (None, ())
            filled = processing in FILL_PROCESSES if processing else disp.get("isFill") is True
            key, name = self.layer_for(disp, processing, settings, filled, layer_data)
            layer = layers.setdefault(key, _Layer(name, processing))
            element_id = self.element_id(display_id)
            if element_id:
                drawable.attrs = {"id": element_id, **drawable.attrs}
            # XCS 1.2 marks grouped elements with a shared groupTag instead of a GROUP element.
            group = disp.get("groupTag") if isinstance(disp.get("groupTag"), str) else None
            group = group if group and _SAFE_ID.fullmatch(group) else None
            layer.items.append((drawable, filled and drawable.fillable, group))
        if skewed:
            self.warn("Scherung (Skew) wurde wie in xTool übernommen – Form der betroffenen Elemente bitte prüfen.")
        if not layers:
            self.warn(f"„{_snippet(title)}“ enthält keine konvertierbaren Elemente.")
            return None
        return self.render(title, layers)

    @staticmethod
    def layer_for(disp: dict, processing, settings: tuple, filled: bool, layer_data: dict) -> tuple[str, str]:
        """Layer key and name. Elements with different laser settings never share a layer,
        because LightBurn gives every colour (= layer) one set of settings."""
        key_suffix = "".join(f"|{key}={_fmt(value)}" for key, value in settings)
        labels = dict(PARAMETER_LABELS)
        name_suffix = ", ".join(labels[key].format(_fmt(value)) for key, value in settings)
        name_suffix = f" ({name_suffix})" if name_suffix else ""
        color = disp.get("layerColor")
        if isinstance(color, str) and _HEX_COLOR.fullmatch(color):
            meta = layer_data.get(color) or layer_data.get(color.lower()) or layer_data.get(color.upper())
            name = meta.get("name") if isinstance(meta, dict) else None
            name = name if isinstance(name, str) and name.strip() else color.upper()
            return f"color:{color.lower()}{key_suffix}", name + name_suffix
        if processing:
            return f"process:{processing}{key_suffix}", PROCESS_NAMES.get(processing, processing) + name_suffix
        return ("plain:fill", "Gefüllte Flächen") if filled else ("plain:line", "Konturen")

    def render_items(self, items: list[tuple[_Drawable, bool, str | None]], color: str) -> list[str]:
        """Rendered drawables of one layer; members of an XCS group are wrapped in a nested <g>
        (inside the layer, whose colour LightBurn uses), so they import as one group."""
        sizes = Counter(group for _, _, group in items if group)
        entries: list[str | list[str]] = []
        members: dict[str, list[str]] = {}
        for drawable, filled, group in items:
            line = _render_drawable(drawable, color, filled)
            if group is None or sizes[group] < 2:
                entries.append(line)
            elif group in members:
                members[group].append(line)
            else:
                members[group] = [f"<g{_attributes({'id': self.group_id(group)})}>", line]
                entries.append(members[group])
        lines: list[str] = []
        for entry in entries:
            if isinstance(entry, list):
                lines.extend(entry)
                lines.append("</g>")
            else:
                lines.append(entry)
        return lines

    def group_id(self, tag: str) -> str:
        candidate, number = f"grp-{tag}", 1
        while candidate in self.used_ids:  # the same group can span several layers
            number += 1
            candidate = f"grp-{tag}-{number}"
        self.used_ids.add(candidate)
        return candidate

    def render(self, title: str, layers: dict[str, _Layer]) -> str:
        boxes = [item.bbox for layer in layers.values() for item, _, _ in layer.items]
        min_x = min(0.0, min(b[0] for b in boxes))
        min_y = min(0.0, min(b[1] for b in boxes))
        width = max(max(b[2] for b in boxes) - min_x, 1.0)
        height = max(max(b[3] for b in boxes) - min_y, 1.0)
        if len(layers) > len(LIGHTBURN_PALETTE):
            self.warn("Mehr Ebenen als LightBurn-Farben – einige Ebenen teilen sich eine Farbe.")
        lines = [
            '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1"'
            f' width="{_fmt(width)}mm" height="{_fmt(height)}mm"'
            f' viewBox="{_fmt(min_x)} {_fmt(min_y)} {_fmt(width)} {_fmt(height)}">',
            f"<title>{_xml(title)}</title>",
        ]
        for index, layer in enumerate(layers.values()):
            color = LIGHTBURN_PALETTE[index % len(LIGHTBURN_PALETTE)]
            group = {"id": f"ebene-{index:02d}", "data-name": layer.name[:100]}
            if layer.processing:
                group["data-processing"] = layer.processing
            lines.append(f"<g{_attributes(group)}>")
            lines.extend(self.render_items(layer.items, color))
            lines.append("</g>")
        lines.append("</svg>")
        return "\n".join(lines) + "\n"


def _attributes(attrs: dict[str, str]) -> str:
    return "".join(f' {key}="{_xml(value)}"' for key, value in attrs.items())


def _render_drawable(drawable: _Drawable, color: str, filled: bool) -> str:
    attrs = dict(drawable.attrs)
    if drawable.tag == "path":
        attrs.update(fill=color if filled else "none", stroke=color, **{"stroke-width": STROKE_WIDTH})
        if filled:
            attrs["fill-rule"] = "evenodd"
        return f"<path{_attributes(attrs)}/>"
    if drawable.tag == "text":
        attrs.update(fill=color)
        return f"<text{_attributes(attrs)}>{drawable.text}</text>"
    return f"<image{_attributes(attrs)}/>"


@dataclass
class _Budget:
    """Limits shared by all projects of one ZIP bundle, so 50 members cannot cost 50 times as much."""
    elements: int = MAX_ELEMENTS
    segments: int = MAX_SEGMENTS
    deadline: float = 0.0


def _convert_doc(doc: dict, stem: str, names: set[str], budget: _Budget | None = None) -> Result:
    canvases = doc.get("canvas")
    if not isinstance(canvases, list) or not canvases:
        raise XcsError("Die Datei enthält keine Arbeitsfläche („canvas“).")
    if len(canvases) > MAX_CANVASES:
        raise XcsError(f"Die Datei enthält zu viele Arbeitsflächen (maximal {MAX_CANVASES}).")
    converter = _Converter(doc)
    if budget is not None:
        converter.elements_left, converter.segments_left = budget.elements, budget.segments
        converter.deadline = budget.deadline
    files: list[SvgFile] = []
    try:
        for index, canvas in enumerate(canvases, start=1):
            if not isinstance(canvas, dict):
                converter.warn("Eine ungültige Arbeitsfläche wurde übersprungen.")
                continue
            raw_title = canvas.get("title") if isinstance(canvas.get("title"), str) else ""
            title = _snippet(raw_title.replace("{panel}", "Arbeitsfläche "), 80) or f"Arbeitsfläche {index}"
            svg = converter.canvas(canvas, title)
            if svg is None:
                continue
            name = f"{stem}.svg" if len(canvases) == 1 else f"{stem}_{base_name(title)}.svg"
            while name in names:
                name = f"{name[:-4]}_{index}.svg"
            names.add(name)
            files.append(SvgFile(name, title, svg))
    finally:
        if budget is not None:
            budget.elements, budget.segments = converter.elements_left, converter.segments_left
    warnings = [message if count == 1 else f"{message} ({count}×)" for message, count in converter.warnings.items()]
    if not files:
        raise XcsError("Die Datei enthält keine konvertierbaren Elemente." + (" " + warnings[0] if warnings else ""))
    return Result(files, warnings)


def _convert_bundle(data: bytes, filename: str | None) -> Result:
    """A ZIP that is not an .xs project: convert every .xcs/.xs inside (one level, shared limits)."""
    unpacked = new_budget()
    try:
        members, ignored = xstudio.bundle(data, unpacked)
    except xstudio.XsError as exc:
        raise XcsError(str(exc)) from None
    del data
    if not members:
        if (filename or "").lower().endswith(".xs"):
            raise XcsError("Das ZIP-Archiv ist kein xTool-Studio-Projekt (project.json fehlt).")
        raise XcsError("Das ZIP-Archiv enthält keine .xcs- oder .xs-Datei.")
    budget = _Budget(deadline=time.monotonic() + MAX_SECONDS)
    files: list[SvgFile] = []
    warnings: list[str] = []
    names: set[str] = set()
    members.reverse()
    while members:
        doc = None  # the previous document is not kept alive while the next one is parsed
        member, content = members.pop()  # nor are the bytes of members already parsed
        label = _snippet(member, 80)
        try:
            if isinstance(content, xstudio.XsError):
                raise XcsError(str(content))
            doc = load(content, unpacked)
            content = None
            result = _convert_doc(doc, base_name(member), names, budget)
        except XcsError as exc:
            if unpacked.left < 0:
                raise XcsError("Das ZIP-Archiv ist entpackt zu groß.") from None
            if unpacked.values_left < 0:
                raise  # shared as well
            if budget.segments < 0 or budget.elements < 0 or time.monotonic() > budget.deadline:
                raise  # a shared limit is used up: the remaining members would fail the same way
            warnings.append(f"{label}: nicht umgewandelt – {exc}")
            continue
        files.extend(result.files)
        warnings.extend(f"{label}: {warning}" for warning in result.warnings)
    if ignored:
        warnings.append(f"{ignored} andere Datei im ZIP ignoriert (Bilder o. Ä.)." if ignored == 1
                        else f"{ignored} andere Dateien im ZIP ignoriert (Bilder o. Ä.).")
    if not files:
        reason = next((w for w in warnings if ": nicht umgewandelt – " in w), "")
        raise XcsError("Keine Datei im ZIP-Archiv ließ sich umwandeln." + (" " + reason if reason else ""))
    return Result(files, warnings)


def convert(data, filename: str | None = None) -> Result:
    """Convert XCS content (bytes, str or parsed dict) into one SVG per canvas.

    A ZIP is either an xTool Studio project (.xs) or a bundle of such files, e.g. an Atomm download
    with the project and some product photos.
    """
    if isinstance(data, (bytes, bytearray)) and data[:2] == b"PK":
        if len(data) > MAX_INPUT_BYTES:
            raise XcsError("Die Datei ist zu groß.")
        try:
            project = xstudio.is_project(data)
        except xstudio.XsError as exc:
            raise XcsError(str(exc)) from None
        if not project:
            return _convert_bundle(bytes(data), filename)
    return _convert_doc(load(data), base_name(filename), set())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.xcs2svg",
                                     description="Konvertiert xTool-.xcs-Dateien in SVG für LightBurn.")
    parser.add_argument("inputs", nargs="+", type=Path, help=".xcs-Datei(en)")
    parser.add_argument("-o", "--outdir", type=Path, help="Zielordner (Standard: neben der Eingabedatei)")
    args = parser.parse_args(argv)
    status = 0
    for source in args.inputs:
        try:
            if source.stat().st_size > MAX_INPUT_BYTES:
                raise XcsError("Die Datei ist zu groß.")
            result = convert(source.read_bytes(), source.name)
        except (OSError, XcsError) as exc:
            print(f"{source}: {exc}", file=sys.stderr)
            status = 1
            continue
        target = args.outdir or source.parent
        target.mkdir(parents=True, exist_ok=True)
        for svg in result.files:
            (target / svg.name).write_text(svg.svg, encoding="utf-8")
            print(target / svg.name)
        for warning in result.warnings:
            print(f"{source.name}: Hinweis: {warning}", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
