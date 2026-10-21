from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile
from typing import Literal

from .models import SyncResult
from .mp4 import (
    _Reader, _read, _clock, _version, _unpack, _SINGLETONS,
    MAX_TRACKS, PROVENANCE_UUID, PROVENANCE_VERSION,
)

EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class WriteReport:
    state: Literal["written", "unchanged", "planned", "skipped", "failed"]
    path: str | None
    utc: str | None = None
    message: str = ""


@dataclass(frozen=True)
class _Field:
    offset: int
    width: int
    value: int


def validate_input(path: str | Path):
    path = Path(path).expanduser()
    if path.suffix.lower() != ".mp4":
        raise ValueError("--in-place requires direct local MP4 inputs")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("--in-place requires regular files without symlinks or hard links")
    if not info.st_mode & 0o222 or not os.access(path, os.W_OK):
        raise ValueError("--in-place requires writable input files")
    return path, info


def _fields(reader, data):
    fields = []
    original = None

    def walk(start, end, parent):
        nonlocal original
        seen = {}
        pos = start
        while pos < end:
            kind, header, stop = reader.header(data[pos:pos + 8], pos, end, data[pos + 8:pos + 16])
            body = pos + header
            seen[kind] = seen.get(kind, 0) + 1
            if kind in _SINGLETONS and seen[kind] != 1:
                raise ValueError("duplicate MP4 metadata box")
            if kind in {b"mvex", b"moof"}:
                raise ValueError("fragmented MP4 is not supported")
            expected = {b"moov": b"mvhd", b"trak": b"tkhd", b"mdia": b"mdhd"}.get(parent)
            if kind in {b"mvhd", b"tkhd", b"mdhd"}:
                if kind != expected or seen[kind] != 1:
                    raise ValueError("unexpected or duplicate timestamp header")
                payload = data[body:stop]
                version = _version(payload, (0, 1))
                minimum = {b"mvhd": 100, b"tkhd": 84, b"mdhd": 24}[kind] + (12 if version else 0)
                if len(payload) < minimum:
                    raise ValueError("truncated timestamp header")
                if kind in {b"mvhd", b"mdhd"}:
                    _clock(payload)
                width = 8 if version else 4
                value, = _unpack(">Q" if version else ">I", payload, 4)
                fields.append(_Field(reader.moov_offset + body + 4, width, value))
                if kind == b"mvhd":
                    original = value
            containers = {b"moov": {b"trak"}, b"trak": {b"mdia", b"edts"},
                          b"mdia": {b"minf"}, b"minf": {b"stbl", b"dinf"}}
            if kind in containers.get(parent, set()):
                if kind != b"trak" and seen[kind] != 1:
                    raise ValueError("duplicate MP4 container")
                walk(body, stop, kind)
            pos = stop
        required = {b"moov": (b"mvhd", b"trak"), b"trak": (b"tkhd", b"mdia"),
                    b"mdia": (b"mdhd",)}.get(parent, ())
        if any(not seen.get(k) for k in required):
            raise ValueError("missing timestamp header or track container")
        if parent == b"moov" and seen.get(b"trak", 0) > MAX_TRACKS:
            raise ValueError("track count limit exceeded")

    walk(0, len(data), b"moov")
    return fields, original


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink)


def _write(path, seconds, dry_run):
    path, before = validate_input(path)
    staging = None
    try:
        with path.open("rb") as source:
            if _identity(os.fstat(source.fileno())) != _identity(before):
                raise ValueError("source changed before reading")
            reader = _Reader()
            moov, _ = reader.file(source)
            fields, original = _fields(reader, moov)
            timeline = _read(source, b"gpmd")
            if any(seconds < 0 or seconds >= 1 << (field.width * 8) for field in fields):
                raise ValueError("UTC date is outside the MP4 timestamp range")
            if all(field.value == seconds for field in fields):
                return "unchanged"
            append = reader.original_creation is None
            if append and reader.has_size_zero:
                raise ValueError("cannot append provenance after a top-level size-zero box")
            if dry_run:
                return "planned"
            with tempfile.NamedTemporaryFile(mode="w+b", prefix=f".{path.name}.",
                                             suffix=".tmp", dir=path.parent, delete=False) as target:
                staging = Path(target.name)
                source.seek(0)
                shutil.copyfileobj(source, target, length=1 << 20)
                for field in fields:
                    target.seek(field.offset)
                    target.write(seconds.to_bytes(field.width, "big"))
                if append:
                    target.seek(0, 2)
                    target.write(struct.pack(">I4s16sBQ", 33, b"uuid", PROVENANCE_UUID,
                                             PROVENANCE_VERSION, original))
                target.flush()
                staged_reader = _Reader()
                staged_moov, _ = staged_reader.file(target)
                staged_fields, _ = _fields(staged_reader, staged_moov)
                preserved = reader.original_creation if reader.original_creation is not None else original
                if (staged_reader.original_creation != preserved
                        or any(field.value != seconds for field in staged_fields)
                        or _read(target, b"gpmd") != timeline):
                    raise ValueError("staged MP4 verification failed")
                os.fchmod(target.fileno(), stat.S_IMODE(before.st_mode))
                os.fsync(target.fileno())
            if (_identity(path.lstat()) != _identity(before)
                    or _identity(os.fstat(source.fileno())) != _identity(before)):
                raise ValueError("source changed before replacement")
            os.replace(staging, path)
            staging = None
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                try:
                    os.fsync(directory)
                except OSError as exc:
                    raise OSError(f"file replaced but directory sync failed: {exc}") from exc
            finally:
                os.close(directory)
            return "written"
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def apply_alignment(result: SyncResult, *, dry_run=False) -> WriteReport:
    """Half seconds round toward the later second."""
    path = result.input_file
    if result.status != "ok":
        return WriteReport("skipped", path, message=f"alignment is {result.status}")
    utc = None
    try:
        if path is None or result.date is None or result.mapping is None:
            raise ValueError("alignment needs a direct input path and UTC origin date")
        if result.media_offset_seconds not in (None, 0):
            raise ValueError("cannot update a sliced recording")
        offset = result.mapping.offset_seconds
        if not math.isfinite(offset):
            raise ValueError("alignment offset must be finite")
        midnight = datetime.combine(result.date, datetime.min.time(), timezone.utc)
        seconds = (midnight - EPOCH).days * 86400 + math.floor(offset + 0.5)
        utc = (EPOCH + timedelta(seconds=seconds)).isoformat()
        state = _write(path, seconds, dry_run)
        return WriteReport(state, path, utc)
    except (OSError, ValueError, OverflowError) as exc:
        return WriteReport("failed", path, utc, str(exc))
