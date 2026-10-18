from __future__ import annotations

import re
import struct
from typing import Iterator, NamedTuple

import numpy as np


class Record(NamedTuple):
    key: str
    typ: str
    size: int
    count: int
    data: bytes


DTYPES = {
    "b": ">i1", "B": ">u1", "s": ">i2", "S": ">u2",
    "l": ">i4", "L": ">u4", "j": ">i8", "J": ">u8",
    "f": ">f4", "d": ">f8", "q": ">i4", "Q": ">i8",
}


MAX_DEPTH = 8


def records(buf: bytes) -> Iterator[Record]:
    for record in _records(memoryview(buf)):
        yield record._replace(data=bytes(record.data))


def _records(buf: memoryview) -> Iterator[Record]:
    offset = 0
    while offset < len(buf):
        if len(buf) - offset < 8:
            raise ValueError("Truncated GPMF header")
        key, typ, size, count = struct.unpack_from(">4sBBH", buf, offset)
        length = size * count
        end = offset + 8 + length
        padded_end = offset + 8 + ((length + 3) // 4) * 4
        if padded_end > len(buf):
            raise ValueError("Truncated GPMF payload or padding")
        try:
            name = key.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid GPMF key") from exc
        yield Record(name, chr(typ), size, count, buf[offset + 8:end])
        offset = padded_end


def streams(buf: bytes, _depth: int = 0) -> Iterator[list[Record]]:
    """Yield STRM records. Nesting is limited to MAX_DEPTH containers, and
    payload bytes are not copied between levels, so work is linear in size."""
    if _depth > MAX_DEPTH:
        raise ValueError("GPMF container nesting is too deep")
    view = buf if isinstance(buf, memoryview) else memoryview(buf)
    for record in _records(view):
        if record.key == "STRM":
            yield [r._replace(data=bytes(r.data)) for r in _records(record.data)]
        elif record.typ == "\0":
            yield from streams(record.data, _depth + 1)


def _types(description: str, size: int) -> list[str]:
    types: list[str] = []
    offset = 0
    while offset < len(description):
        match = re.match(r"([bBsSlLjJfdqQ])(?:\[(\d+)\])?", description[offset:])
        if match is None:
            raise ValueError(f"Unsupported GPMF TYPE: {description!r}")
        count = int(match[2]) if match[2] else 1
        if count < 1 or count > size:
            raise ValueError("Invalid GPMF TYPE array size")
        types.extend([match[1]] * count)
        offset += len(match[0])
        if len(types) > size:
            raise ValueError("GPMF TYPE exceeds row size")
    if not types or sum(np.dtype(DTYPES[t]).itemsize for t in types) != size:
        raise ValueError("GPMF TYPE does not match row size")
    return types


def number(record: Record, type_hint: str | Record | None = None) -> np.ndarray:
    _, typ, size, count, data = record
    if len(data) != size * count or size == 0:
        raise ValueError("Invalid GPMF numeric payload size")
    if typ == "?":
        if isinstance(type_hint, Record):
            try:
                type_hint = type_hint.data.decode("ascii").rstrip("\0")
            except UnicodeDecodeError as exc:
                raise ValueError("Invalid GPMF TYPE text") from exc
        if not isinstance(type_hint, str):
            raise ValueError("Complex GPMF data requires TYPE")
        types = _types(type_hint, size)
        dtype = np.dtype([(f"f{i}", DTYPES[t]) for i, t in enumerate(types)])
        rows = np.frombuffer(data, dtype=dtype, count=count)
        values = np.column_stack([rows[f"f{i}"] for i in range(len(types))]).astype(float)
        for i, t in enumerate(types):
            if t in ("q", "Q"):
                values[:, i] /= 2 ** (16 if t == "q" else 32)
        return values
    if typ not in DTYPES:
        raise ValueError(f"Unsupported GPMF numeric type: {typ!r}")
    dtype = np.dtype(DTYPES[typ])
    if size % dtype.itemsize:
        raise ValueError("GPMF row size is not a multiple of its numeric type")
    values = np.frombuffer(data, dtype=dtype).astype(float).reshape(count, size // dtype.itemsize)
    if typ in ("q", "Q"):
        values /= 2 ** (16 if typ == "q" else 32)
    return values


def scaled(record: Record, fields: dict[str, Record]) -> np.ndarray:
    values = number(record, fields.get("TYPE"))
    if "SCAL" in fields:
        scale = number(fields["SCAL"]).ravel()
        if len(scale) not in (1, values.shape[1]):
            raise ValueError("GPMF SCAL does not match data columns")
        if not np.all(np.isfinite(scale)) or np.any(scale == 0):
            raise ValueError("GPMF SCAL must be finite and nonzero")
        values /= scale
    return values
