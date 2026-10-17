from dataclasses import dataclass, field
from itertools import groupby
import struct


def box(kind, body=b"", *, extended=False, to_end=False):
    if to_end:
        return struct.pack(">I4s", 0, kind) + body
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(body) + 16) + body
    return struct.pack(">I4s", len(body) + 8, kind) + body


def full(kind, body=b"", version=0):
    return box(kind, bytes([version, 0, 0, 0]) + body)


def table(kind, rows, fmt, version=0):
    return full(kind, struct.pack(">I", len(rows)) + b"".join(struct.pack(fmt, *r) for r in rows), version)


def runs(values):
    return [(len(list(group)), value) for value, group in groupby(values)]


@dataclass
class Track:
    handler: bytes = b"meta"
    format: bytes = b"gpmd"
    scale: int = 1000
    deltas: list = field(default_factory=lambda: [1000])
    payloads: list | None = None
    ctts: list | None = None
    ctts_version: int = 0
    edits: list | None = None
    version: int = 0
    duration: int | None = None
    chunks: list | None = None
    sizes: str = "stsz"
    offsets: str = "stco"
    replacements: dict = field(default_factory=dict)
    descriptions: list | None = None
    description_indices: list | None = None
    reference: bytes = b"\0\0\0\1"
    reference_kind: bytes = b"url "

    def data(self):
        return self.payloads if self.payloads is not None else [b"x"] * len(self.deltas)


def video(**kwargs):
    return Track(handler=b"vide", format=b"avc1", **kwargs)


def clock(kind, scale, duration, version=0):
    body = (struct.pack(">QQIQ", 0, 0, scale, duration) if version
            else struct.pack(">IIII", 0, 0, scale, duration))
    return full(kind, body + b"\0" * 80, version)


def track_box(t, offsets):
    payloads = t.data()
    sizes = [len(p) for p in payloads]
    chunk_sizes = t.chunks if t.chunks is not None else [1] * len(payloads)
    descriptions = t.descriptions or [t.format]
    entries = [box(fmt, b"\0" * 6 + struct.pack(">H", 1)) for fmt in descriptions]
    stsd = full(b"stsd", struct.pack(">I", len(entries)) + b"".join(entries))
    tables = {b"stsd": stsd, b"stts": table(b"stts", runs(t.deltas), ">II")}
    if t.ctts is not None:
        tables[b"ctts"] = table(b"ctts", runs(t.ctts), ">Ii" if t.ctts_version else ">II", t.ctts_version)
    if t.sizes.startswith("stz2"):
        width = int(t.sizes.split("-")[1])
        if width == 4:
            padded = sizes + [0]
            packed = bytes((padded[i] << 4) | padded[i + 1] for i in range(0, len(sizes), 2))
        else:
            packed = b"".join(struct.pack(">B" if width == 8 else ">H", n) for n in sizes)
        tables[b"stz2"] = full(b"stz2", struct.pack(">II", width, len(sizes)) + packed)
    else:
        fixed = sizes[0] if sizes and t.sizes == "stsz-fixed" else 0
        tables[b"stsz"] = full(b"stsz", struct.pack(">II", fixed, len(sizes)) +
                               (b"" if fixed else b"".join(struct.pack(">I", n) for n in sizes)))
    rows = []
    descs = t.description_indices or [1] * len(chunk_sizes)
    for i, (count, desc) in enumerate(zip(chunk_sizes, descs), 1):
        if not rows or rows[-1][1:] != (count, desc):
            rows.append((i, count, desc))
    tables[b"stsc"] = table(b"stsc", rows, ">III")
    tables[t.offsets] = table(t.offsets, [(o,) for o in offsets], ">Q" if t.offsets == b"co64" else ">I")
    for kind, replacement in t.replacements.items():
        if replacement is None:
            tables.pop(kind, None)
        else:
            tables[kind] = replacement
    duration = sum(t.deltas) if t.duration is None else t.duration
    dref = full(b"dref", struct.pack(">I", 1) + box(t.reference_kind, t.reference))
    minf = box(b"minf", box(b"dinf", dref) + box(b"stbl", b"".join(tables.values())))
    mdia = box(b"mdia", clock(b"mdhd", t.scale, duration, t.version) +
               full(b"hdlr", b"\0" * 4 + t.handler + b"\0" * 12) + minf)
    edts = b""
    if t.edits is not None:
        rows = [tuple(e) if len(e) == 4 else (*e, 1, 0) for e in t.edits]
        edts = box(b"edts", table(b"elst", rows, ">Qqhh" if t.version else ">Iihh", t.version))
    return box(b"trak", edts + mdia)


def build(tracks, *, movie_scale=1000, movie_duration=0, version=0,
          moov_first=False, extended=False, to_end=None, extra_top=b"", extra_moov=b"",
          payload_start=None):
    for t in tracks:
        if isinstance(t.offsets, str):
            t.offsets = t.offsets.encode()
    header = 16 if extended else 8
    payload = b"".join(p for t in tracks for p in t.data())

    def moov(start):
        cursor, bodies = start, []
        for t in tracks:
            offsets, index = [], 0
            data = t.data()
            for n in (t.chunks if t.chunks is not None else [1] * len(data)):
                offsets.append(cursor)
                cursor += sum(len(p) for p in data[index:index + n])
                index += n
            bodies.append(track_box(t, offsets))
        return box(b"moov", clock(b"mvhd", movie_scale, movie_duration, version) +
                   b"".join(bodies) + extra_moov, extended=extended, to_end=to_end == "moov")

    start = payload_start if payload_start is not None else header
    metadata = moov(start)
    if moov_first and payload_start is None:
        metadata = moov(len(metadata) + header)
    mdat = box(b"mdat", payload, extended=extended, to_end=to_end == "mdat")
    return (metadata + mdat if moov_first else mdat + metadata) + extra_top
