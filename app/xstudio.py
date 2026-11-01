"""Read xTool Studio (.xs) projects into the document shape of an XCS file.

An .xs file is a ZIP archive of JSON chunks:

- ``project.json``: canvas order, active device
- ``canvases/<id>.json``: canvas meta (layerData, groupData, title)
- ``canvases/<id>/displays-<n>.json``: the elements, split into chunks
- ``vectors/<bucket>/data-<n>.json``: large path data, referenced from an element via
  ``vectorRef = {vectorHash, bucketType, originalField}``
- ``devices/*.json`` + ``profiles.json``: laser settings as profiles, material patches
  and bindings (profile + patches -> list of display ids)

Everything is read in memory with a hard budget on unpacked bytes, so a zip bomb
cannot exhaust the container. Nothing is extracted to disk and only the entry
names above are opened.
"""
from __future__ import annotations

import json
import re
import zipfile
import zlib
from io import BytesIO

MAX_ENTRIES = 2_000

_CANVAS = re.compile(r"canvases/([A-Za-z0-9_-]{1,64})\.json")
_CHUNK = re.compile(r"canvases/([A-Za-z0-9_-]{1,64})/displays-(\d{1,6})\.json")
_BUCKET = re.compile(r"vectors/([A-Za-z0-9_-]{1,32})/data-\d{1,6}\.json")
_DEVICE = re.compile(r"devices/[A-Za-z0-9_.-]{1,80}\.json")
_FIELD = re.compile(r"[A-Za-z]{1,32}")


class XsError(ValueError):
    """The archive is not a readable xTool Studio project."""


class Budget:
    """Unpacked bytes and JSON values left; shared by all archives and documents of one upload.

    Bytes alone do not bound memory: json.loads turns every short string or number into a Python
    object of 30-60 bytes, so 64 MB of ``"ab",`` would become 13 million objects (about 800 MB).
    """

    def __init__(self, max_bytes: int, max_values: int):
        self.left = max_bytes
        self.values_left = max_values

    def charge(self, raw: bytes) -> None:
        """Charge the values json.loads may create from raw, before it runs.

        Every value or key is followed by a comma or colon, or is the only item of a container,
        so this is an upper bound (separators inside strings count too).
        """
        self.values_left -= (raw.count(b",") + raw.count(b":") + raw.count(b"{") + raw.count(b"[")) + 1
        if self.values_left < 0:
            raise XsError("Die Datei enthält zu viele Einträge.")

    def read(self, archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        try:
            with archive.open(info) as stream:
                # Read at most the remaining budget + 1 byte; the size in the header is not trusted.
                raw = stream.read(max(self.left, 0) + 1)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError, zlib.error):
            raise XsError("Das Archiv ist beschädigt oder verschlüsselt.") from None
        self.left -= len(raw)
        if self.left < 0:
            raise XsError("Die Datei ist entpackt zu groß.")
        return raw


def _open_zip(data: bytes) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo]]:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
        infos = archive.infolist()
    except (zipfile.BadZipFile, ValueError, EOFError, OSError):
        raise XsError("Die Datei ist kein gültiges ZIP-Archiv.") from None
    if len(infos) > MAX_ENTRIES:
        raise XsError("Das Archiv enthält zu viele Dateien.")
    return archive, infos


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _reject_constant(name: str):
    raise ValueError(name)


class _Archive:
    def __init__(self, data: bytes, max_containers: int, budget: Budget):
        self.zip, infos = _open_zip(data)
        self.names = {info.filename: info for info in infos}
        self.budget = budget
        self.containers_left = max_containers

    def json(self, name: str):
        info = self.names.get(name)
        if info is None:
            return None
        raw = self.budget.read(self.zip, info)
        self.containers_left -= raw.count(b"{") + raw.count(b"[")
        if self.containers_left < 0:
            raise XsError("Die Datei enthält zu viele verschachtelte Einträge.")
        self.budget.charge(raw)
        try:
            text = raw.decode("utf-8-sig")
            del raw  # the bytes are not needed while json.loads builds the objects
            return json.loads(text, parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise XsError("Die Datei enthält unlesbare Projektdaten.") from None

    def matching(self, pattern: re.Pattern) -> list[tuple[re.Match, str]]:
        return [(match, name) for name in self.names if (match := pattern.fullmatch(name))]


def _vectors(archive: _Archive) -> dict[str, dict[str, str]]:
    """bucket type -> vector hash -> data (e.g. the dPath string)."""
    buckets: dict[str, dict[str, str]] = {}
    for match, name in archive.matching(_BUCKET):
        entries = _dict(_dict(archive.json(name)).get("entries"))
        bucket = buckets.setdefault(match.group(1), {})
        bucket.update((key, value) for key, value in entries.items() if isinstance(value, str))
    return buckets


def _resolve(disp: dict, vectors: dict[str, dict[str, str]]) -> None:
    ref = disp.get("vectorRef")
    if not isinstance(ref, dict):
        return
    field = ref.get("originalField", "dPath")
    key, bucket = ref.get("vectorHash"), ref.get("bucketType", "svg")
    if not (isinstance(field, str) and _FIELD.fullmatch(field) and isinstance(key, str) and isinstance(bucket, str)):
        return
    value = vectors.get(bucket, {}).get(key)
    if value is not None and disp.get(field) is None:
        disp[field] = value


def _resolve_all(root, vectors: dict[str, dict[str, str]]) -> None:
    """Resolve vectorRefs anywhere below root (elements, glyphs); one iterator per level."""
    stack = [iter((root,))]
    while stack:
        for value in stack[-1]:
            if isinstance(value, dict):
                _resolve(value, vectors)
                stack.append(iter(value.values()))
                break
            if isinstance(value, list):
                stack.append(iter(value))
                break
        else:
            stack.pop()


def _canvases(archive: _Archive, project: dict) -> list[dict]:
    listed = [item for item in _list(_dict(project.get("modules")).get("canvases")) if isinstance(item, str)]

    def rank(cid: str) -> int:
        return next((index for index, item in enumerate(listed) if cid in item), len(listed))

    chunks: dict[str, list[tuple[int, str]]] = {}
    for match, name in archive.matching(_CHUNK):
        chunks.setdefault(match.group(1), []).append((int(match.group(2)), name))
    ids = {match.group(1) for match, _ in archive.matching(_CANVAS)} | set(chunks)
    vectors = _vectors(archive)
    canvases = []
    for cid in sorted(ids, key=lambda cid: (rank(cid), cid)):
        canvas = dict(_dict(archive.json(f"canvases/{cid}.json")))
        displays: list = []
        for _, name in sorted(chunks.get(cid, [])):
            displays.extend(_list(_dict(archive.json(name)).get("displays")))
        _resolve_all(displays, vectors)
        canvas["id"] = canvas.get("id") if isinstance(canvas.get("id"), str) else cid
        canvas["displays"] = displays
        canvases.append(canvas)
    return canvases


def _device(archive: _Archive, project: dict) -> dict:
    devices = [_dict(archive.json(name)) for _, name in archive.matching(_DEVICE)]
    active = project.get("activeDeviceId")
    return next((device for device in devices if device.get("id") == active), devices[0] if devices else {})


def _processing(archive: _Archive, project: dict, canvases: list[dict]) -> list:
    """Laser settings in the XCS 1.2 device map shape: [[canvas id, {displays: Map}], ...]."""
    profiles = _dict(_dict(archive.json("profiles.json")).get("profiles"))
    processing = _dict(_device(archive, project).get("processing"))
    result = []
    for canvas in canvases:
        cid = canvas["id"]
        state = _dict(processing.get(cid))
        active = state.get("activeMode")
        mode = _dict(_dict(state.get("modes")).get(active)) if isinstance(active, str) else {}
        patches = _dict(mode.get("patches"))
        mapping: dict[str, dict] = {}
        for binding in _list(mode.get("bindings")):
            binding = _dict(binding)
            profile_id = binding.get("baseProfileId")
            profile = _dict(profiles.get(profile_id)) if isinstance(profile_id, str) else {}
            kind = profile.get("processingType")
            if not isinstance(kind, str):
                continue
            values = dict(_dict(profile.get("values")))
            for patch_id in _list(binding.get("patchIds")):
                if isinstance(patch_id, str):
                    values.update(_dict(_dict(patches.get(patch_id)).get("overrides")))
            entry = {"processingType": kind, "data": {kind: {"parameter": {"xs": values}}}}
            for display_id in _list(binding.get("displayIds")):
                if isinstance(display_id, str):
                    mapping[display_id] = entry
        # Elements without a binding still carry their processing type.
        for disp in canvas["displays"]:
            kind = _dict(_dict(disp).get("customData")).get("processingType")
            display_id = _dict(disp).get("id")
            if isinstance(kind, str) and isinstance(display_id, str):
                mapping.setdefault(display_id, {"processingType": kind})
        result.append([cid, {"displays": {"dataType": "Map", "value": [list(item) for item in mapping.items()]}}])
    return result


def load(data: bytes, max_containers: int, budget: Budget) -> dict:
    """Reassemble an .xs archive into ``{"canvas": [...], "device": {...}}`` like an XCS file."""
    archive = _Archive(data, max_containers, budget)
    project = archive.json("project.json")
    if not isinstance(project, dict):
        raise XsError("Das ZIP-Archiv ist kein xTool-Studio-Projekt (project.json fehlt).")
    canvases = _canvases(archive, project)
    device = {"data": {"dataType": "Map", "value": _processing(archive, project, canvases)}}
    return {"canvas": canvases, "device": device, "version": project.get("version")}


# --- bundles: a plain ZIP (e.g. an Atomm download) holding .xcs/.xs projects plus photos ---

PROJECT_SUFFIXES = (".xcs", ".xs")
MAX_BUNDLE_FILES = 50


def is_project(data: bytes) -> bool:
    """True if the ZIP is an xTool Studio project itself (project.json at the root)."""
    archive, infos = _open_zip(data)
    archive.close()
    return any(info.filename == "project.json" for info in infos)


def bundle(data: bytes, budget: Budget) -> tuple[list[tuple[str, bytes | XsError]], int]:
    """Project files of a bundle as (display name, bytes or the error reading it) + count of ignored files.

    Only the top level of the bundle is unpacked here: a ZIP inside is never opened as a bundle again
    (an inner .xs is read later as a project, which needs project.json). Directories, __MACOSX/ and
    dotfiles are skipped silently. All members share one unpacked-bytes budget.
    """
    archive, infos = _open_zip(data)
    members: list[tuple[str, zipfile.ZipInfo]] = []
    ignored = 0
    for info in infos:
        parts = [part for part in re.split(r"[/\\]+", info.filename) if part]
        if info.is_dir() or not parts or "__MACOSX" in parts:
            continue
        name = parts[-1]
        if any(part.startswith(".") and part not in (".", "..") for part in parts):
            continue  # dotfiles (.DS_Store) and hidden folders; ../ is only a (harmless) name part
        if name.lower().endswith(PROJECT_SUFFIXES):
            members.append((name, info))
        else:
            ignored += 1
    if len(members) > MAX_BUNDLE_FILES:
        raise XsError(f"Das ZIP-Archiv enthält zu viele Projektdateien (maximal {MAX_BUNDLE_FILES}).")
    result: list[tuple[str, bytes | XsError]] = []
    for name, info in members:
        if info.flag_bits & 0x1:
            result.append((name, XsError("Die Datei ist verschlüsselt.")))
            continue
        try:
            result.append((name, budget.read(archive, info)))
        except XsError as exc:
            if budget.left < 0:
                raise XsError("Das ZIP-Archiv ist entpackt zu groß.") from None
            result.append((name, exc))
    return result, ignored
