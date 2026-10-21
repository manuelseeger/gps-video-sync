from datetime import date
import os
import struct

import numpy as np
import pytest

from gps_video_sync import read_video
from gps_video_sync.in_place import apply_alignment, EPOCH
from gps_video_sync.models import Accepted, Ambiguous, Failed, OwnGps, SyncResult, TimeMapping
from gps_video_sync.mp4 import PROVENANCE_UUID, read_timeline
from mp4_builder import Track, box, build, video
from test_gopro import clock_packets


def result(path, offset=1.5, day=date(2026, 1, 1)):
    return SyncResult(Accepted(TimeMapping(offset, .1, 1.0002, OwnGps())),
                      input_file=str(path), date=day, media_offset_seconds=0)


def headers(data):
    found = []
    for kind in (b"mvhd", b"tkhd", b"mdhd"):
        start = 0
        while (pos := data.find(kind, start)) >= 0:
            width = 8 if data[pos + 4] == 1 else 4
            found.append((kind, int.from_bytes(data[pos + 8:pos + 8 + width], "big"),
                          data[pos + 8 + width:pos + 8 + width * 2]))
            start = pos + 4
    return found


@pytest.mark.parametrize("version", [0, 1])
@pytest.mark.parametrize("layout", [{}, {"moov_first": True}, {"extended": True}])
@pytest.mark.parametrize("offset,expected", [(1.49, 1), (1.5, 2), (-.5, 0), (-1.5, -1), (86400.5, 86401)])
def test_all_creation_fields_and_only_creation_fields(tmp_path, version, layout, offset, expected):
    path = tmp_path / "clip.mp4"
    before = bytearray(build([video(version=version), Track(version=version), Track(version=version)],
                             version=version, created=123, **layout))
    for kind in (b"mvhd", b"tkhd", b"mdhd"):
        start = 0
        while (pos := before.find(kind, start)) >= 0:
            width = 8 if version else 4
            before[pos + 8 + width:pos + 8 + width * 2] = (987654).to_bytes(width, "big")
            start = pos + 4
    before = bytes(before)
    path.write_bytes(before)
    os.chmod(path, 0o640)
    timeline = read_timeline(path)
    report = apply_alignment(result(path, offset))
    assert report.state == "written"
    seconds = (date(2026, 1, 1) - EPOCH.date()).days * 86400 + expected
    after = path.read_bytes()
    assert len(headers(after)) == 7
    assert all(value == seconds for _, value, _ in headers(after))
    assert [modified for _, _, modified in headers(after)] == [modified for _, _, modified in headers(before)]
    restored = bytearray(after[:len(before)])
    for kind in (b"mvhd", b"tkhd", b"mdhd"):
        start = 0
        while (pos := before.find(kind, start)) >= 0:
            width = 8 if version else 4
            restored[pos + 8:pos + 8 + width] = before[pos + 8:pos + 8 + width]
            start = pos + 4
    assert bytes(restored) == before
    assert len(after) == len(before) + 33
    assert read_timeline(path) == timeline
    assert path.stat().st_mode & 0o777 == 0o640
    identity = path.stat()
    assert apply_alignment(result(path, offset)).state == "unchanged"
    assert path.read_bytes() == after
    after_stat = path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns, after_stat.st_ctime_ns) == (
        identity.st_ino, identity.st_mtime_ns, identity.st_ctime_ns)
    assert result(path).mapping.clock_rate == 1.0002


@pytest.mark.parametrize("created", [0, 123456])
def test_camera_evidence_survives_cold_extraction(tmp_path, created):
    path = tmp_path / "camera.mp4"
    path.write_bytes(build([video(deltas=[3000]), Track(deltas=[1000] * 3,
                          payloads=clock_packets())], created=created))
    before = read_video(path, cache_dir=tmp_path / "before")
    assert apply_alignment(result(path)).state == "written"
    after = read_video(path, cache_dir=tmp_path / "after")
    assert after.camera_start == before.camera_start
    assert after.clock == before.clock
    np.testing.assert_array_equal(after.time, before.time)
    for channel in before.channels:
        np.testing.assert_array_equal(after.channels[channel], before.channels[channel])
    assert apply_alignment(result(path, 42)).state == "written"
    assert path.read_bytes().count(PROVENANCE_UUID) == 1
    assert read_video(path, cache_dir=tmp_path / "again").camera_start == before.camera_start


def test_npz_never_follows_embedded_original(tmp_path):
    original = tmp_path / "original.mp4"
    before = build([video()])
    original.write_bytes(before)
    archive = tmp_path / "telemetry.npz"
    archive.write_bytes(b"export")
    alignment = result(archive)
    alignment.video = str(original)
    assert apply_alignment(alignment).state == "failed"
    assert original.read_bytes() == before
    assert archive.read_bytes() == b"export"


def test_already_correct_without_provenance(tmp_path):
    path = tmp_path / "correct.mp4"
    before = build([video()])
    path.write_bytes(before)
    assert apply_alignment(result(path, 0, date(1904, 1, 1))).state == "unchanged"
    assert path.read_bytes() == before
    assert PROVENANCE_UUID not in path.read_bytes()


def test_dry_run(tmp_path):
    path = tmp_path / "clip.mp4"
    before = build([video()])
    path.write_bytes(before)
    info = path.stat()
    assert apply_alignment(result(path), dry_run=True).state == "planned"
    assert path.read_bytes() == before
    after_stat = path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns, after_stat.st_ctime_ns) == (
        info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("outcome", [Ambiguous("not unique"), Failed("bad clip")])
def test_skip_unaccepted(tmp_path, outcome):
    path = tmp_path / "missing.mp4"
    assert apply_alignment(SyncResult(outcome, input_file=str(path))).state == "skipped"
    assert not path.exists()


@pytest.mark.parametrize("defect", ["truncated", "fragmented", "mvex", "duplicate", "bad_version",
                                    "bad_provenance", "duplicate_provenance", "zero_size",
                                    "missing_tkhd", "missing_mdhd", "duplicate_table", "zero_scale"])
def test_invalid_files_fail_without_change(tmp_path, defect):
    provenance = box(b"uuid", PROVENANCE_UUID + struct.pack(">BQ", 1, 0))
    data = build([video()])
    if defect == "truncated":
        data = data[:-1]
    elif defect == "fragmented":
        data += box(b"moof")
    elif defect == "mvex":
        data = build([video()], extra_moov=box(b"mvex"))
    elif defect == "duplicate":
        data = build([video()], extra_moov=box(b"mvhd", b"\0" * 100))
    elif defect in {"missing_tkhd", "missing_mdhd"}:
        kind = b"tkhd" if defect == "missing_tkhd" else b"mdhd"
        data = data.replace(kind, b"free", 1)
    elif defect == "duplicate_table":
        from mp4_builder import table
        stts = table(b"stts", [(1, 1000)], ">II")
        data = build([video(replacements={b"stts": stts * 2})])
    elif defect == "zero_scale":
        pos = data.index(b"mdhd") + 16
        data = data[:pos] + b"\0" * 4 + data[pos + 4:]
    elif defect == "bad_version":
        pos = data.index(b"mvhd") + 4
        data = data[:pos] + b"\2" + data[pos + 1:]
    elif defect == "bad_provenance":
        data += box(b"uuid", PROVENANCE_UUID + b"\2" + b"\0" * 8)
    elif defect == "duplicate_provenance":
        data += provenance * 2
    else:
        data = build([video()], to_end="moov")
    path = tmp_path / "bad.mp4"
    path.write_bytes(data)
    assert apply_alignment(result(path)).state == "failed"
    assert path.read_bytes() == data
    assert list(tmp_path.iterdir()) == [path]
    if "provenance" in defect:
        with pytest.raises(ValueError, match="provenance"):
            read_timeline(path)


@pytest.mark.parametrize("version,day,expected", [(0, date(2041, 1, 1), "failed"),
                                                 (1, date(2041, 1, 1), "written"),
                                                 (1, date(1903, 1, 1), "failed")])
def test_timestamp_range(tmp_path, version, day, expected):
    path = tmp_path / "range.mp4"
    before = build([video(version=version)], version=version)
    path.write_bytes(before)
    assert apply_alignment(result(path, day=day)).state == expected
    if expected == "failed":
        assert path.read_bytes() == before


@pytest.mark.parametrize("defect", ["replace", "copy", "fsync", "source_changed", "hardlink", "symlink", "read_only"])
def test_safe_replacement(tmp_path, monkeypatch, defect):
    import gps_video_sync.in_place as writer
    path = tmp_path / "clip.mp4"
    before = build([video()])
    path.write_bytes(before)
    if defect == "replace":
        monkeypatch.setattr(writer.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("replace failed")))
    elif defect == "copy":
        monkeypatch.setattr(writer.shutil, "copyfileobj", lambda *a, **kw: (_ for _ in ()).throw(OSError("copy failed")))
    elif defect == "fsync":
        monkeypatch.setattr(writer.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("fsync failed")))
    elif defect == "source_changed":
        fsync = writer.os.fsync
        def change(fd):
            fsync(fd)
            os.utime(path, ns=(1, 1))
        monkeypatch.setattr(writer.os, "fsync", change)
    elif defect == "hardlink":
        os.link(path, tmp_path / "other.mp4")
    elif defect == "read_only":
        path.chmod(0o444)
    else:
        link = tmp_path / "link.mp4"
        link.symlink_to(path)
        path = link
    assert apply_alignment(result(path)).state == "failed"
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_staged_timeline_must_match_before_replacement(tmp_path, monkeypatch):
    import gps_video_sync.in_place as writer
    path = tmp_path / "clip.mp4"
    before = build([video()])
    path.write_bytes(before)
    copy = writer.shutil.copyfileobj

    def corrupt(source, target, **kwargs):
        copy(source, target, **kwargs)
        position = before.index(b"mdhd") + 20
        target.seek(position)
        target.write(struct.pack(">I", 2000))

    monkeypatch.setattr(writer.shutil, "copyfileobj", corrupt)
    report = apply_alignment(result(path))
    assert report.state == "failed"
    assert "staged MP4 verification failed" in report.message
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_directory_sync_failure_reports_completed_replacement(tmp_path, monkeypatch):
    import gps_video_sync.in_place as writer
    path = tmp_path / "clip.mp4"
    before = build([video()])
    path.write_bytes(before)
    sync = writer.os.fsync
    calls = 0

    def fail_directory(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory unavailable")
        sync(fd)

    monkeypatch.setattr(writer.os, "fsync", fail_directory)
    report = apply_alignment(result(path))
    assert report.state == "failed"
    assert "file replaced but directory sync failed" in report.message
    assert path.read_bytes() != before
    assert read_timeline(path).created_seconds is None
    assert list(tmp_path.iterdir()) == [path]
    assert apply_alignment(result(path)).state == "unchanged"
