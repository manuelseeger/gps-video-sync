from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
from itertools import repeat
from pathlib import Path
import struct
from typing import NamedTuple

MAX_METADATA = 64 << 20
MAX_BOXES = 100_000
MAX_TRACKS = 128
MAX_EDITS = 1024
MAX_SAMPLES = 10_000_000
MAX_OUTPUT = 100_000
MAX_PAYLOAD = 16 << 20
_SINGLETONS = {
    b"mdia", b"edts", b"mdhd", b"hdlr", b"minf", b"dinf", b"stbl", b"elst", b"dref",
    b"stsd", b"stts", b"ctts", b"stsz", b"stz2", b"stsc", b"stco", b"co64",
}


class Sample(NamedTuple):
    time: float
    duration: float
    offset: int
    size: int


class Timeline(NamedTuple):
    duration: float
    samples: tuple[Sample, ...]
    created_seconds: int | None = None


@dataclass(frozen=True)
class _Edit:
    delay: int = 0
    media_time: int = 0
    segment: int | None = None


@dataclass(frozen=True)
class _Track:
    scale: int
    duration: int
    edit: _Edit
    stts: tuple
    ctts: tuple
    sizes: tuple
    chunks: tuple
    stsc: tuple


def _unpack(fmt, data, offset=0):
    if offset + struct.calcsize(fmt) > len(data):
        raise ValueError("truncated metadata field")
    return struct.unpack_from(fmt, data, offset)


def _version(data, supported=(0,)):
    version, = _unpack(">B", data)
    if len(data) < 4:
        raise ValueError("truncated FullBox")
    if version not in supported:
        raise ValueError("unsupported box version")
    return version


class _Reader:
    def __init__(self):
        self.boxes = 0

    def header(self, raw, start, end, extended=b""):
        self.boxes += 1
        if self.boxes > MAX_BOXES:
            raise ValueError("box count limit exceeded")
        size, kind = _unpack(">I4s", raw)
        header = 8
        if size == 1:
            size, = _unpack(">Q", extended)
            header = 16
        if kind == b"uuid":
            header += 16
        if size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError("truncated or invalid box size")
        return kind, header, start + size

    def children(self, data):
        pos = 0
        while pos < len(data):
            kind, header, end = self.header(data[pos:pos + 8], pos, len(data), data[pos + 8:pos + 16])
            yield kind, data[pos + header:end]
            pos = end

    def mapping(self, data):
        result = {}
        for kind, body in self.children(data):
            if kind not in _SINGLETONS:
                continue
            if kind in result:
                raise ValueError(f"duplicate {kind.decode('ascii', 'replace')} box")
            result[kind] = body
        return result

    def file(self, handle):
        handle.seek(0, 2)
        size = handle.tell()
        pos, moov, media = 0, None, []
        while pos < size:
            handle.seek(pos)
            raw = handle.read(8)
            extended = handle.read(8) if len(raw) == 8 and raw[:4] == b"\0\0\0\1" else b""
            kind, header, end = self.header(raw, pos, size, extended)
            if kind == b"moof":
                raise ValueError("fragmented MP4 is not supported")
            if kind == b"mdat":
                media.append((pos + header, end))
            elif kind == b"moov":
                if moov is not None:
                    raise ValueError("duplicate moov box")
                if end - pos > MAX_METADATA:
                    raise ValueError("metadata limit exceeded")
                handle.seek(pos + header)
                moov = handle.read(end - pos - header)
                if len(moov) != end - pos - header:
                    raise ValueError("truncated moov")
            pos = end
        if moov is None:
            raise ValueError("no moov box")
        return memoryview(moov), media


def _required(boxes, kind):
    if kind not in boxes:
        raise ValueError(f"missing {kind.decode()} box")
    return boxes[kind]


def _clock(data):
    version = _version(data, (0, 1))
    scale, duration = _unpack(">IQ" if version else ">II", data, 20 if version else 12)
    if not scale:
        raise ValueError("invalid timescale")
    if duration == (1 << (64 if version else 32)) - 1:
        duration = 0
    return scale, duration


def _table(data, fmt, versions=(0,)):
    _version(data, versions)
    count, = _unpack(">I", data, 4)
    width = struct.calcsize(fmt)
    if len(data) != 8 + count * width:
        raise ValueError("truncated or inconsistent table length")
    if count > MAX_SAMPLES:
        raise ValueError("table entry limit exceeded")
    return struct.iter_unpack(fmt, data[8:])


def _edit(data):
    if data is None:
        return _Edit()
    version = _version(data, (0, 1))
    count, = _unpack(">I", data, 4)
    if count > MAX_EDITS:
        raise ValueError("edit count limit exceeded")
    entries = _table(data, ">Qqhh" if version else ">Iihh", (0, 1))
    delay, selected = 0, None
    for segment, media, rate, fraction in entries:
        if segment <= 0 or rate != 1 or fraction or media < -1 or selected is not None:
            raise ValueError("unsupported edit list")
        if media == -1:
            delay += segment
        else:
            selected = _Edit(delay, media, segment)
    if count and selected is None:
        raise ValueError("unsupported edit list without media edit")
    return selected or _Edit()


def _descriptions(reader, data):
    _version(data)
    count, = _unpack(">I", data, 4)
    entries = tuple(reader.children(data[8:]))
    if len(entries) != count:
        raise ValueError("inconsistent stsd count")
    return entries


def _sizes(boxes):
    if (b"stsz" in boxes) == (b"stz2" in boxes):
        raise ValueError("expected one sample size table")
    if b"stsz" in boxes:
        data = boxes[b"stsz"]
        _version(data)
        fixed, count = _unpack(">II", data, 4)
        width, body = 32, data[12:]
        expected = 0 if fixed else count * 4
    else:
        data = boxes[b"stz2"]
        _version(data)
        field, count = _unpack(">II", data, 4)
        if field not in (4, 8, 16):
            raise ValueError("invalid stz2 field size")
        fixed, width, body = 0, field, data[12:]
        expected = (count * width + 7) // 8
    if len(body) != expected:
        raise ValueError("truncated sample size table")
    if count > MAX_SAMPLES:
        raise ValueError("sample count limit exceeded")
    return count, fixed, width, body


def _size(sizes, index):
    _, fixed, width, body = sizes
    if fixed:
        return fixed
    if width == 4:
        return (body[index // 2] >> (4 if index % 2 == 0 else 0)) & 15
    return _unpack({8: ">B", 16: ">H", 32: ">I"}[width], body, index * (width // 8))[0]


def _track(reader, mdia, stbl, descriptions, edit, minf):
    scale, duration = _clock(_required(mdia, b"mdhd"))
    sizes = _sizes(stbl)
    count = sizes[0]
    stts = tuple(_table(_required(stbl, b"stts"), ">II"))
    if sum(n for n, _ in stts) != count or any(n == 0 for n, _ in stts):
        raise ValueError("inconsistent stts sample count")
    total = sum(n * delta for n, delta in stts)
    if stts and stts[-1][1] == 0 and stts[-1][0] == 1 and duration > total:
        stts = stts[:-1] + ((1, duration - total),)
    if any(delta <= 0 for _, delta in stts):
        raise ValueError("sample has no positive duration")
    ctts = ()
    if b"ctts" in stbl:
        data = stbl[b"ctts"]
        version = _version(data, (0, 1))
        ctts = tuple(_table(data, ">Ii" if version else ">II", (0, 1)))
        if sum(n for n, _ in ctts) != count or any(n == 0 for n, _ in ctts):
            raise ValueError("inconsistent ctts sample count")
    if (b"stco" in stbl) == (b"co64" in stbl):
        raise ValueError("expected one chunk offset table")
    chunks = tuple(v[0] for v in _table(stbl.get(b"stco", stbl.get(b"co64")),
                                       ">I" if b"stco" in stbl else ">Q"))
    stsc = tuple(_table(_required(stbl, b"stsc"), ">III"))
    if bool(stsc) != bool(chunks) or (stsc and stsc[0][0] != 1):
        raise ValueError("invalid stsc first chunk")
    capacity, used = 0, set()
    for i, (first, per_chunk, desc) in enumerate(stsc):
        end = stsc[i + 1][0] if i + 1 < len(stsc) else len(chunks) + 1
        if not 1 <= first < end <= len(chunks) + 1 or not per_chunk:
            raise ValueError("invalid stsc run")
        if not 1 <= desc <= len(descriptions):
            raise ValueError("invalid sample description index")
        used.add(desc)
        capacity += (end - first) * per_chunk
    if capacity != count:
        raise ValueError("inconsistent chunk sample count")
    if len({descriptions[i - 1][0] for i in used}) > 1:
        raise ValueError("unsupported mixed sample descriptions")
    if used:
        dinf = reader.mapping(_required(minf, b"dinf"))
        dref = _required(dinf, b"dref")
        _version(dref)
        refs = tuple(reader.children(dref[8:]))
        if len(refs) != _unpack(">I", dref, 4)[0]:
            raise ValueError("inconsistent dref count")
        for index in used:
            kind, body = descriptions[index - 1]
            reference, = _unpack(">H", body, 6)
            if kind in (b"encv", b"enca") or not 1 <= reference <= len(refs):
                raise ValueError("unsupported sample data reference")
            refkind, ref = refs[reference - 1]
            _version(ref)
            if refkind not in (b"url ", b"alis") or bytes(ref[:4]) != b"\0\0\0\1":
                raise ValueError("external sample data reference is not supported")
    return _Track(scale, duration, edit, stts, ctts, sizes, chunks, stsc)


def _timing(track):
    dts, earliest, i, j = 0, None, 0, 0
    remaining_time = track.stts[0][0] if track.stts else 0
    offsets = track.ctts or ((track.sizes[0], 0),)
    remaining_offset = offsets[0][0]
    while i < len(track.stts):
        n = min(remaining_time, remaining_offset)
        pts = dts + offsets[j][1]
        earliest = pts if earliest is None else min(earliest, pts)
        dts += n * track.stts[i][1]
        remaining_time -= n
        remaining_offset -= n
        if not remaining_time:
            i += 1
            if i < len(track.stts):
                remaining_time = track.stts[i][0]
        if not remaining_offset:
            j += 1
            if j < len(offsets):
                remaining_offset = offsets[j][0]
    return dts, earliest or 0


def _raw_samples(track, media):
    deltas = (v for n, v in track.stts for v in repeat(v, n))
    offsets = (v for n, v in (track.ctts or ((track.sizes[0], 0),)) for v in repeat(v, n))
    index, dts, run = 0, 0, 0
    for chunk_index, pos in enumerate(track.chunks, 1):
        if run + 1 < len(track.stsc) and chunk_index == track.stsc[run + 1][0]:
            run += 1
        for _ in range(track.stsc[run][1]):
            size = _size(track.sizes, index)
            region = bisect_right(media, pos, key=lambda span: span[0]) - 1
            if size <= 0 or region < 0 or pos + size > media[region][1]:
                raise ValueError("sample outside mdat payload")
            delta = next(deltas)
            yield dts + next(offsets), delta, pos, size
            dts += delta
            pos += size
            index += 1


def _read(handle, sample_format):
    reader = _Reader()
    moov, media = reader.file(handle)
    children = list(reader.children(moov))
    if any(kind == b"mvex" for kind, _ in children):
        raise ValueError("fragmented MP4 is not supported")
    movies = [body for kind, body in children if kind == b"mvhd"]
    if len(movies) != 1:
        raise ValueError("expected one mvhd box")
    movie_scale, movie_duration = _clock(movies[0])
    version = _version(movies[0], (0, 1))
    created, = _unpack(">Q" if version else ">I", movies[0], 4)
    tracks = [body for kind, body in children if kind == b"trak"]
    if len(tracks) > MAX_TRACKS:
        raise ValueError("track count limit exceeded")
    video, selected, sample_count = None, [], 0
    for body in tracks:
        boxes = reader.mapping(body)
        mdia = reader.mapping(_required(boxes, b"mdia"))
        handler = _required(mdia, b"hdlr")
        _version(handler)
        kind, = _unpack(">4s", handler, 8)
        minf = reader.mapping(_required(mdia, b"minf"))
        stbl = reader.mapping(_required(minf, b"stbl"))
        descriptions = _descriptions(reader, _required(stbl, b"stsd"))
        requested = any(fmt == sample_format for fmt, _ in descriptions)
        is_video = kind == b"vide" and video is None
        if not requested and not is_video:
            continue
        edits = reader.mapping(boxes[b"edts"]) if b"edts" in boxes else {}
        track = _track(reader, mdia, stbl, descriptions, _edit(edits.get(b"elst")), minf)
        sample_count += track.sizes[0]
        if sample_count > MAX_SAMPLES:
            raise ValueError("sample count limit exceeded")
        used = {descriptions[desc - 1][0] for _, _, desc in track.stsc}
        if requested and used and used != {sample_format}:
            raise ValueError("unsupported mixed sample descriptions")
        if is_video:
            video = track
        if requested:
            selected.append(track)
    if video is None:
        raise ValueError("no video track")
    total, first = _timing(video)
    edit = video.edit
    duration = (Fraction(edit.delay + edit.segment, movie_scale) if edit.segment is not None
                else Fraction(video.duration, video.scale) or Fraction(total, video.scale)
                or Fraction(movie_duration, movie_scale))
    if duration <= 0:
        raise ValueError("cannot determine video duration")
    origin = Fraction(0) if edit.segment is not None else Fraction(first, video.scale)
    if sum(t.sizes[0] for t in selected) > MAX_OUTPUT:
        raise ValueError("output sample limit exceeded")
    samples = []
    for track in selected:
        edit = track.edit
        delay = Fraction(edit.delay, movie_scale)
        if edit.segment is not None and delay + Fraction(edit.segment, movie_scale) < origin + duration:
            raise ValueError("unsupported differing edit visibility")
        for pts, delta, pos, size in _raw_samples(track, media):
            if size > MAX_PAYLOAD:
                raise ValueError("sample payload limit exceeded")
            time = delay + Fraction(pts - edit.media_time, track.scale)
            if edit.segment is not None:
                if time + Fraction(delta, track.scale) <= delay:
                    continue
                if time < delay and delay > origin:
                    raise ValueError("unsupported partial sample across empty edit")
            samples.append(Sample(float(time - origin), float(Fraction(delta, track.scale)), pos, size))
    return Timeline(float(duration), tuple(sorted(samples, key=lambda s: s.time)), created or None)


def read_timeline(path: str | Path, sample_format: str = "gpmd") -> Timeline:
    """Read sample times and byte spans relative to the first video timeline."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            return _read(handle, sample_format.encode("ascii"))
    except ValueError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc
