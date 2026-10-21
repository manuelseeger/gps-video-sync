#!/usr/bin/env python3
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

import numpy as np

from gps_video_sync import read_video
from gps_video_sync.mp4 import read_timeline

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


def inspect(path):
    headers, media = [], hashlib.sha256()
    with path.open("rb") as handle:
        def walk(start, end):
            pos = start
            while pos < end:
                handle.seek(pos)
                size, kind = struct.unpack(">I4s", handle.read(8))
                header = 8
                if size == 1:
                    size, = struct.unpack(">Q", handle.read(8))
                    header = 16
                if size == 0:
                    size = end - pos
                assert size >= header and pos + size <= end
                if kind in {b"mvhd", b"tkhd", b"mdhd"}:
                    version = handle.read(4)[0]
                    width = 8 if version else 4
                    created = int.from_bytes(handle.read(width), "big")
                    modified = int.from_bytes(handle.read(width), "big")
                    headers.append((kind, created, modified))
                elif kind == b"mdat":
                    left = size - header
                    while left:
                        chunk = handle.read(min(left, 1 << 20))
                        assert chunk
                        media.update(chunk)
                        left -= len(chunk)
                elif kind in {b"moov", b"trak", b"mdia"}:
                    walk(pos + header, pos + size)
                pos += size
        walk(0, path.stat().st_size)
    assert headers and any(kind == b"tkhd" for kind, _, _ in headers)
    return headers, media.hexdigest()


def digest(path):
    with path.open("rb") as handle:
        result = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            result.update(chunk)
        return result.hexdigest()


def identity(path):
    info = path.stat()
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode


def sync(paths, work, *options):
    process = subprocess.run([sys.executable, "-m", "gps_video_sync", "sync", *map(str, paths),
                              "--track", str(FIXTURES / "flight.igc"), "--cache-dir", str(work / "cache"),
                              *options], capture_output=True, text=True, cwd=ROOT)
    assert process.returncode == 0, process.stdout + process.stderr
    return json.loads(process.stdout)["videos"]


def main():
    originals = sorted(FIXTURES.glob("*.mp4"))
    assert originals, "bundled MP4 recordings are required"
    original_hashes = [digest(p) for p in originals]
    with TemporaryDirectory(prefix="check-in-place-") as directory:
        work = Path(directory).resolve()
        paths = [Path(shutil.copy2(p, work / p.name)) for p in originals]
        before = [inspect(p) for p in paths]
        timelines = [read_timeline(p) for p in paths]
        telemetry = [read_video(p, cache_dir=work / "before") for p in paths]
        baseline = sync(paths, work)
        preview = sync(paths, work, "--in-place", "--dry-run")
        assert all(r["in_place"]["state"] == "planned" for r in preview)
        assert [digest(p) for p in paths] == original_hashes
        written = sync(paths, work, "--in-place")
        assert all(r["in_place"]["state"] == "written" for r in written)
        for i, (path, report) in enumerate(zip(paths, written)):
            alignment = dict(report)
            alignment.pop("in_place")
            assert alignment == baseline[i]
            headers, media = inspect(path)
            utc = datetime.fromisoformat(report["video_start_utc"])
            seconds = math.floor((utc - EPOCH).total_seconds() + .5)
            assert all(created == seconds for _, created, _ in headers)
            assert [m for _, _, m in headers] == [m for _, _, m in before[i][0]]
            assert media == before[i][1]
            assert read_timeline(path) == timelines[i]
            after = read_video(path, cache_dir=work / "cold-after")
            old = telemetry[i]
            assert (after.camera_start, after.clock, after.duration) == (old.camera_start, old.clock, old.duration)
            for name in ("time", "gps_time", "gps_utc"):
                np.testing.assert_array_equal(getattr(after, name), getattr(old, name))
            assert after.channels.keys() == old.channels.keys()
            for channel in old.channels:
                np.testing.assert_array_equal(after.channels[channel], old.channels[channel])
            if shutil.which("ffprobe"):
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
                                        "-of", "json", str(path)], capture_output=True, text=True, check=True)
                tag = json.loads(probe.stdout)["format"]["tags"]["creation_time"]
                assert datetime.fromisoformat(tag.replace("Z", "+00:00")) == datetime.fromisoformat(report["in_place"]["utc"])
        hashes = [digest(p) for p in paths]
        identities = [identity(p) for p in paths]
        repeated = sync(paths, work, "--in-place")
        assert all(r["in_place"]["state"] == "unchanged" for r in repeated)
        assert [dict(r, in_place=None) for r in repeated] == [dict(r, in_place=None) for r in written]
        assert [digest(p) for p in paths] == hashes
        assert [identity(p) for p in paths] == identities
        cold = sync(paths, work / "cold-repeat", "--in-place")
        assert cold == repeated
    assert [digest(p) for p in originals] == original_hashes
    print("In-place UTC headers, unchanged media and telemetry, camera evidence, and cold/warm repeat checks passed")


if __name__ == "__main__":
    main()
