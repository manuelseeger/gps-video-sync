"""Versioned, read-only GoPro recording evidence, independent of synchronization."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
import struct
from typing import Literal

import numpy as np

from . import gpmf, mp4

IDENTITY_CONTRACT = "gopro-recording-identity/1.0.0"
_UNAVAILABLE = frozenset({"no_telemetry", "no_recording_context", "no_motion",
                          "unsupported_layout", "missing_native_counters",
                          "insufficient_sensor_witness"})
_FAILURES = frozenset({"read_error", "malformed_container", "malformed_telemetry",
                      "limit_exceeded", "source_changed"})
_METADATA = ("SCAL", "ORIN", "ORIO", "MTRX", "SIUN", "UNIT")


def _contract(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"gopro-recording-identity/(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError("Invalid identity contract")


@dataclass(frozen=True, eq=False)
class RecordingIdentity:
    """Matching evidence, not proof of provenance or anonymization."""
    key: str
    contract: str = IDENTITY_CONTRACT

    def __post_init__(self):
        _contract(self.contract)
        if not isinstance(self.key, str) or not re.fullmatch(r"[0-9a-f]{64}", self.key):
            raise ValueError("Invalid identity key")

    def __eq__(self, other):
        return compare_identity(self, other) == "same"

    __hash__ = None

    def to_dict(self) -> dict:
        return {"state": "available", "contract": self.contract, "key": self.key}


@dataclass(frozen=True, eq=False)
class IdentityUnavailable:
    """Readable media without sufficient supported recording evidence."""
    reason: str
    contract: str = IDENTITY_CONTRACT

    def __post_init__(self):
        _contract(self.contract)
        if not isinstance(self.reason, str) or self.reason not in _UNAVAILABLE:
            raise ValueError("Invalid unavailable reason")

    def __eq__(self, other):
        return False

    __hash__ = None

    def to_dict(self) -> dict:
        return {"state": "unavailable", "contract": self.contract, "reason": self.reason}


@dataclass(frozen=True, eq=False)
class IdentityFailed:
    """Extraction could not determine whether sufficient evidence exists."""
    code: str
    contract: str = IDENTITY_CONTRACT

    def __post_init__(self):
        _contract(self.contract)
        if not isinstance(self.code, str) or self.code not in _FAILURES:
            raise ValueError("Invalid identity failure code")

    def __eq__(self, other):
        return False

    __hash__ = None

    def to_dict(self) -> dict:
        return {"state": "failed", "contract": self.contract, "code": self.code}


IdentityResult = RecordingIdentity | IdentityUnavailable | IdentityFailed


def identity_from_dict(value: dict) -> IdentityResult:
    """Validate a persisted result, retaining unsupported contract versions."""
    if not isinstance(value, dict):
        raise ValueError("Invalid identity result")
    state = value.get("state")
    if state == "available" and set(value) == {"state", "contract", "key"}:
        return RecordingIdentity(value["key"], value["contract"])
    if state == "unavailable" and set(value) == {"state", "contract", "reason"}:
        return IdentityUnavailable(value["reason"], value["contract"])
    if state == "failed" and set(value) == {"state", "contract", "code"}:
        return IdentityFailed(value["code"], value["contract"])
    raise ValueError("Invalid identity result")


def compare_identity(a: IdentityResult, b: IdentityResult) -> Literal["same", "different", "incomparable"]:
    """Compare available evidence only under the exact supported contract."""
    if (not isinstance(a, RecordingIdentity) or not isinstance(b, RecordingIdentity)
            or a.contract != IDENTITY_CONTRACT or b.contract != IDENTITY_CONTRACT):
        return "incomparable"
    return "same" if a.key == b.key else "different"


_NATIVE_UNIT_BYTES = frozenset(b"\xb0\xb2\xb3\xb5")


def _text(data):
    data = data.rstrip(b"\0")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if any(not (0x20 <= b < 0x7F or b in _NATIVE_UNIT_BYTES) for b in data):
        return None
    return data.decode("latin-1")


def _interpretation(fields):
    for key in ("SCAL", "MTRX"):
        if key in fields:
            record = fields[key]
            if record.typ not in gpmf.DTYPES:
                return False
            values = gpmf.number(record).ravel()
            if (len(values) not in ((1, 3) if key == "SCAL" else (9,))
                    or not np.all(np.isfinite(values))
                    or (key == "SCAL" and np.any(values == 0))):
                return False
    for key in ("ORIN", "ORIO", "SIUN", "UNIT"):
        if key not in fields:
            continue
        record = fields[key]
        if record.typ != "c" or not record.data:
            return False
        text = _text(record.data)
        if text is None:
            return False
        if not text or not text.isprintable():
            return False
        if key in ("ORIN", "ORIO") and sorted(text.upper()) != ["X", "Y", "Z"]:
            return False
    return True


def _record_bytes(record):
    return struct.pack(">BBH", ord(record.typ), record.size, record.count) + record.data


def _evidence(header, payload):
    streams = list(gpmf.streams(payload))
    selected = []
    for key in ("ACCL", "GYRO"):
        candidates = [s for s in streams if any(r.key == key for r in s)]
        if not candidates:
            return IdentityUnavailable("no_motion")
        if len(candidates) != 1:
            return IdentityUnavailable("unsupported_layout")
        stream = candidates[0]
        relevant = (key, "STMP", "TSMP", *_METADATA)
        fields = {r.key: r for r in stream if r.key in relevant}
        if sum(r.key in relevant for r in stream) != len(fields):
            return IdentityUnavailable("unsupported_layout")
        motion = fields[key]
        if (motion.typ not in ("s", "l") or motion.size != (6 if motion.typ == "s" else 12)
                or not motion.count):
            return IdentityUnavailable("unsupported_layout")
        try:
            stamp, total = (gpmf.integer(fields[k]) for k in ("STMP", "TSMP"))
        except (KeyError, ValueError):
            return IdentityUnavailable("missing_native_counters")
        if total < motion.count:
            return IdentityUnavailable("missing_native_counters")
        if not _interpretation(fields):
            return IdentityUnavailable("unsupported_layout")
        first = motion.data[:motion.size]
        if all(motion.data[i:i + motion.size] == first
               for i in range(0, len(motion.data), motion.size)):
            return IdentityUnavailable("insufficient_sensor_witness")
        selected.append((key, motion, stamp, total, fields))
    if header.created_seconds is None:
        return IdentityUnavailable("no_recording_context")
    material = bytearray(IDENTITY_CONTRACT.encode("ascii") + b"\0")
    material.extend(struct.pack(">Q", header.created_seconds))
    for key, motion, stamp, total, fields in selected:
        material.extend(key.encode("ascii") + _record_bytes(motion))
        material.extend(struct.pack(">QQ", stamp, total))
        for name in _METADATA:
            record = fields.get(name)
            material.extend(b"\0" if record is None else b"\1" + _record_bytes(record))
    return RecordingIdentity(hashlib.sha256(material).hexdigest())


def _extract(handle):
    try:
        header = mp4.read_header(handle)
        if len(header.telemetry) > 1:
            return IdentityUnavailable("unsupported_layout")
        if not header.telemetry or header.telemetry[0] is None:
            return IdentityUnavailable("no_telemetry")
        span = header.telemetry[0]
        handle.seek(span.offset)
        payload = handle.read(span.size)
        if len(payload) != span.size:
            return IdentityFailed("malformed_container")
    except mp4.LimitExceeded:
        return IdentityFailed("limit_exceeded")
    except ValueError:
        return IdentityFailed("malformed_container")
    try:
        return _evidence(header, payload)
    except gpmf.LimitExceeded:
        return IdentityFailed("limit_exceeded")
    except ValueError:
        return IdentityFailed("malformed_telemetry")


def _fingerprint(handle):
    stat = os.fstat(handle.fileno())
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def read_recording_identity(path: str | os.PathLike[str]) -> IdentityResult:
    """Read one file without cache access, writes, or retained telemetry or paths.

    Media and I/O errors return IdentityFailed. Invalid path types raise TypeError.
    An embedded null character in the path raises ValueError.
    """
    path = os.fspath(path)
    if not isinstance(path, str):
        raise TypeError("Supply one text video path")
    try:
        with open(path, "rb") as handle:
            before = _fingerprint(handle)
            result = _extract(handle)
            if before != _fingerprint(handle):
                return IdentityFailed("source_changed")
            return result
    except OSError:
        return IdentityFailed("read_error")
