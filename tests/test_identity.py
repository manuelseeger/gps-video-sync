from dataclasses import FrozenInstanceError
import errno
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

from gps_video_sync import (
    IDENTITY_CONTRACT, IdentityFailed, IdentityUnavailable, RecordingIdentity,
    compare_identity, identity_from_dict, read_recording_identity, read_video,
)
from gps_video_sync import gpmf, mp4
from mp4_builder import Track, box, build, table, video
from test_gopro import gps5, numeric, packet, record, stream


def motion(key, *, stamp=5_000_000, total=102, typ="s", rows=None, extra=b"",
           counters=True, counter_type="J"):
    rows = [[1, 2, 3], [4, 5, 6]] if rows is None else rows
    fields = [numeric(key, rows, typ), numeric("SCAL", [1000]), extra]
    if counters:
        fields += [numeric("STMP", [stamp], counter_type), numeric("TSMP", [total], counter_type)]
    return stream(*fields)


def payload(*, stamp=5_000_000, total=102, gps=False, serial=False, rows=None,
            extra=b"", typ="s", counters=True, counter_type="J"):
    sensors = [motion(key, stamp=stamp, total=total, rows=rows, typ=typ, extra=extra,
                      counters=counters, counter_type=counter_type) for key in ("ACCL", "GYRO")]
    if gps:
        sensors.append(gps5())
    if serial:
        sensors.append(record("CASN", "c", 1, 14, b"private-marker"))
    return packet(*sensors)


def media(tmp_path, name="source.mp4", *, data=None, created=3_800_000_000,
          ticks=1_801_002, encoding=b"avc1", gps=False, serial=False, **kwargs):
    data = [payload(gps=gps, serial=serial)] if data is None else data
    path = tmp_path / name
    tracks = [video(scale=60000, deltas=[ticks])]
    tracks[0].format = encoding
    if data:
        tracks.append(Track(payloads=data, deltas=[1000] * len(data)))
    path.write_bytes(build(tracks, created=created, **kwargs))
    return path


def test_golden_file_and_independent_canonical_bytes(tmp_path):
    path = media(tmp_path)
    result = read_recording_identity(path)
    expected = "6325f85402d4590a91d89ed1c177fcde8ff17fbf5d7fdd866d128e23bb4732b5"
    assert result.to_dict() == {"state": "available", "contract": IDENTITY_CONTRACT, "key": expected}
    canonical = b"gopro-recording-identity/1.0.0\0" + struct.pack(">Q", 3_800_000_000)
    for key in (b"ACCL", b"GYRO"):
        canonical += key + b"s\x06\x00\x02" + struct.pack(">hhhhhh", 1, 2, 3, 4, 5, 6)
        canonical += struct.pack(">QQ", 5_000_000, 102)
        canonical += b"\x01l\x04\x00\x01" + struct.pack(">i", 1000) + b"\0" * 5
    assert hashlib.sha256(canonical).hexdigest() == expected
    assert read_recording_identity(path) == result


@pytest.mark.parametrize("gps", [False, True])
def test_real_duration_regression_cache_and_hz_independence(tmp_path, monkeypatch, gps):
    monkeypatch.setenv("HOME", str(tmp_path))
    packets = [payload(stamp=5_000_000 + i * 1_000_000, total=102 + i * 2, gps=gps)
               for i in range(3)]
    a = media(tmp_path, "original.mp4", data=packets, ticks=1_801_002)
    b = media(tmp_path, "preview.lrv", data=packets, ticks=1_803_000, encoding=b"hvc1", moov_first=True)
    before = [read_recording_identity(p) for p in (a, b)]
    cache = tmp_path / "cache"
    cold = [read_video(p, hz=10, cache_dir=cache) for p in (a, b)]
    assert [r.duration for r in cold] == [30.0167, 30.05]
    assert cold[0].camera_start.bound_seconds != cold[1].camera_start.bound_seconds
    assert compare_identity(*before) == "same"
    for hz in (10, 25):
        for path, expected in zip((a, b), before):
            read_video(path, hz=hz, cache_dir=cache)
            assert read_recording_identity(path) == expected


def test_rename_move_duplicates_and_optional_private_metadata(tmp_path):
    a = media(tmp_path, "arbitrary.mp4")
    b = media(tmp_path, "unrelated.lrv", gps=True, serial=True,
              extra_moov=box(b"udta", box(b"MUID", b"private-marker")))
    original = read_recording_identity(a)
    assert compare_identity(original, read_recording_identity(b)) == "same"
    duplicate = tmp_path / "duplicate.mp4"
    shutil.copyfile(a, duplicate)
    directory = tmp_path / "other"
    directory.mkdir()
    moved = a.rename(directory / "renamed.lrv")
    assert compare_identity(original, read_recording_identity(moved)) == "same"
    assert compare_identity(original, read_recording_identity(duplicate)) == "same"
    assert "private-marker" not in json.dumps(read_recording_identity(b).to_dict())
    assert str(tmp_path) not in repr(original)


@pytest.mark.parametrize("created", [0, 2**32 - 1])
def test_no_context_is_not_a_matching_identity_even_itself(tmp_path, created):
    a = read_recording_identity(media(tmp_path, created=created))
    b = read_recording_identity(media(tmp_path, "other.mp4", created=created))
    assert a.to_dict()["reason"] == "no_recording_context"
    assert a != a
    assert a != b
    assert compare_identity(a, b) == "incomparable"


@pytest.mark.parametrize("change", ["creation", "stamp", "counter", "rows", "chapter"])
def test_distinct_captures_and_continuous_chapters(tmp_path, change):
    a = media(tmp_path)
    options = {"created": 3_800_000_001} if change == "creation" else {}
    changed = {"stamp": 6_000_000} if change in ("stamp", "chapter") else {}
    if change in ("counter", "chapter"):
        changed["total"] = 104
    if change in ("rows", "chapter"):
        changed["rows"] = [[2, 3, 4], [5, 6, 7]]
    b = media(tmp_path, "other.mp4", data=[payload(**changed)], **options)
    assert compare_identity(read_recording_identity(a), read_recording_identity(b)) == "different"


@pytest.mark.parametrize("data,reason", [
    ([], "no_telemetry"),
    ([packet(gps5())], "no_motion"),
    ([payload(rows=[[1, 2, 3]] * 2)], "insufficient_sensor_witness"),
    ([payload(counters=False)], "missing_native_counters"),
    ([payload(counter_type="d")], "missing_native_counters"),
    ([payload(total=1)], "missing_native_counters"),
    ([payload(typ="f")], "unsupported_layout"),
    ([payload(rows=[[1, 2], [3, 4]])], "unsupported_layout"),
    ([packet(motion("ACCL"))], "no_motion"),
    ([packet(motion("ACCL"), motion("ACCL"), motion("GYRO"))], "unsupported_layout"),
    ([payload(extra=numeric("TSMP", [102], "J"))], "unsupported_layout"),
    ([payload(extra=record("ORIN", "c", 1, 3, b"XXZ"))], "unsupported_layout"),
    ([payload(extra=numeric("MTRX", [1, 2]))], "unsupported_layout"),
])
def test_insufficient_supported_files(tmp_path, data, reason):
    result = read_recording_identity(media(tmp_path, data=data))
    assert result.to_dict() == {"state": "unavailable", "contract": IDENTITY_CONTRACT, "reason": reason}
    assert compare_identity(result, result) == "incomparable"


def test_multiple_and_empty_telemetry_tracks(tmp_path):
    path = tmp_path / "tracks.mp4"
    path.write_bytes(build([Track(payloads=[payload()]), Track(payloads=[payload()])], created=1))
    assert read_recording_identity(path).reason == "unsupported_layout"
    path.write_bytes(build([Track(payloads=[], deltas=[])], created=1))
    assert read_recording_identity(path).reason == "no_telemetry"


def test_first_decode_packet_ignores_video_tables_edits_and_later_damage(tmp_path):
    data = payload()
    baseline = read_recording_identity(media(tmp_path))
    path = tmp_path / "native.mp4"
    path.write_bytes(build([
        video(replacements={b"stts": None}),
        Track(payloads=[data, b"invalid later packet"], deltas=[1000, 1000],
              ctts=[2000, -1000], ctts_version=1, edits=[(1000, 1000)]),
    ], created=3_800_000_000))
    assert compare_identity(baseline, read_recording_identity(path)) == "same"
    path.write_bytes(build([Track(payloads=[data])], created=3_800_000_000))
    assert compare_identity(baseline, read_recording_identity(path)) == "same"


def test_exact_unsigned_counters_and_creation_encoding(tmp_path):
    paths = [media(tmp_path, str(version), version=version,
                   data=[payload(stamp=2**53 + 1, total=2**53 + 3)]) for version in (0, 1)]
    assert compare_identity(*(read_recording_identity(p) for p in paths)) == "same"
    changed = media(tmp_path, "changed", data=[payload(stamp=2**53 + 2, total=2**53 + 3)])
    assert compare_identity(read_recording_identity(paths[0]), read_recording_identity(changed)) == "different"
    encoded = media(tmp_path, "encoded", data=[payload(counter_type="L")])
    assert compare_identity(read_recording_identity(media(tmp_path)), read_recording_identity(encoded)) == "same"


@pytest.mark.parametrize("extra", [numeric("SCAL", [2000]), record("ORIN", "c", 1, 3, b"XYZ"),
                                  record("ORIO", "c", 1, 3, b"ZXY"),
                                  numeric("MTRX", [1, 0, 0, 0, 1, 0, 0, 0, 1]),
                                  record("SIUN", "c", 1, 3, b"m/s"), record("UNIT", "c", 1, 1, b"g")])
def test_interpretation_metadata_changes_identity(tmp_path, extra):
    first = read_recording_identity(media(tmp_path))
    if extra[:4] == b"SCAL":
        data = packet(*(stream(numeric(k, [[1, 2, 3], [4, 5, 6]], "s"), extra,
                               numeric("STMP", [5_000_000], "J"), numeric("TSMP", [102], "J"))
                        for k in ("ACCL", "GYRO")))
    else:
        data = payload(extra=extra)
    changed = read_recording_identity(media(tmp_path, "changed", data=[data]))
    assert compare_identity(first, changed) == "different"


def test_native_gopro_single_byte_units_are_supported_and_exact(tmp_path):
    def meta(unit):
        return (record("ORIN", "c", 3, 1, b"YxZ")
                + record("SIUN", "c", len(unit), 1, unit))
    a = read_recording_identity(media(tmp_path, "a", data=[payload(extra=meta(b"m/s\xb2"))]))
    b = read_recording_identity(media(tmp_path, "b", data=[payload(extra=meta("m/s²".encode()))]))
    assert isinstance(a, RecordingIdentity) and isinstance(b, RecordingIdentity)
    assert compare_identity(a, b) == "different"
    assert compare_identity(a, read_recording_identity(media(tmp_path, "c", data=[payload(extra=meta(b"m/s\xb2"))]))) == "same"
    bad = read_recording_identity(media(tmp_path, "d", data=[payload(extra=meta(b"m/s\xff"))]))
    assert bad.reason == "unsupported_layout"


def test_raw_row_encoding_and_metadata_encoding_are_exact(tmp_path):
    a = read_recording_identity(media(tmp_path))
    b = read_recording_identity(media(tmp_path, "long", data=[payload(typ="l")]))
    assert compare_identity(a, b) == "different"
    extra_a = record("ORIN", "c", 1, 3, b"XYZ")
    extra_b = record("ORIN", "c", 3, 1, b"XYZ")
    c = read_recording_identity(media(tmp_path, "axis-a", data=[payload(extra=extra_a)]))
    d = read_recording_identity(media(tmp_path, "axis-b", data=[payload(extra=extra_b)]))
    assert compare_identity(c, d) == "different"


def test_io_failure_is_retryable_and_private(tmp_path, monkeypatch):
    missing = read_recording_identity(tmp_path / "private-marker")
    assert missing.to_dict() == {"state": "failed", "contract": IDENTITY_CONTRACT, "code": "read_error"}
    path = media(tmp_path)
    def fail(handle):
        raise OSError(errno.EIO, "private-marker")
    monkeypatch.setattr(mp4, "read_header", fail)
    result = read_recording_identity(path)
    assert result.to_dict() == missing.to_dict()
    assert result != result
    assert compare_identity(result, result) == "incomparable"


@pytest.mark.parametrize("damage", ["invalid", "truncated", "outside", "gpmf"])
def test_malformed_media_is_failed(tmp_path, damage):
    path = media(tmp_path)
    code = "malformed_container"
    if damage == "invalid":
        path.write_bytes(b"not an MP4")
    elif damage == "truncated":
        path.write_bytes(path.read_bytes()[:-1])
    elif damage == "outside":
        path.write_bytes(build([Track(payloads=[payload()], replacements={
            b"stco": table(b"stco", [(2**31,)], ">I")})], created=1))
    else:
        path = media(tmp_path, data=[b"bad gpmf"])
        code = "malformed_telemetry"
    assert read_recording_identity(path).to_dict() == {"state": "failed", "contract": IDENTITY_CONTRACT, "code": code}


@pytest.mark.parametrize("limit,value", [("MAX_METADATA", 64), ("MAX_BOXES", 2),
                                          ("MAX_TRACKS", 1), ("MAX_SAMPLES", 0), ("MAX_PAYLOAD", 4)])
def test_resource_bounds_are_typed_failures(tmp_path, monkeypatch, limit, value):
    path = media(tmp_path)
    monkeypatch.setattr(mp4, limit, value)
    assert read_recording_identity(path).code == "limit_exceeded"
    with path.open("rb") as handle, pytest.raises(mp4.LimitExceeded):
        mp4.read_header(handle)


def test_gpmf_depth_limit_is_failed(tmp_path, monkeypatch):
    path = media(tmp_path)
    monkeypatch.setattr(gpmf, "MAX_DEPTH", 0)
    assert read_recording_identity(path).code == "limit_exceeded"


def test_source_mutation_is_failed_for_available_and_unavailable(tmp_path, monkeypatch):
    original = mp4.read_header
    def change(handle):
        header = original(handle)
        with open(handle.name, "ab") as writer:
            writer.write(b"changed")
        return header
    monkeypatch.setattr(mp4, "read_header", change)
    for data in ([payload()], []):
        path = media(tmp_path, data=data)
        assert read_recording_identity(path).code == "source_changed"


def test_read_only_no_cache_no_retained_paths_or_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = media(tmp_path, "private-marker.mp4", gps=True, serial=True)
    data, stat = path.read_bytes(), path.stat()
    files = sorted(tmp_path.rglob("*"))
    result = read_recording_identity(path)
    assert result.to_dict()["state"] == "available"
    assert sorted(tmp_path.rglob("*")) == files
    assert path.read_bytes() == data
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    assert set(vars(result)) == {"key", "contract"}
    assert "private-marker" not in json.dumps(result.to_dict())
    with pytest.raises(FrozenInstanceError):
        result.key = "x"


@pytest.mark.parametrize("version", ["1.0.1", "1.1.0", "2.0.0"])
def test_exact_contract_compatibility_and_persistence(tmp_path, version):
    a = read_recording_identity(media(tmp_path))
    stored = a.to_dict()
    stored["contract"] = "gopro-recording-identity/" + version
    b = identity_from_dict(json.loads(json.dumps(stored)))
    assert b.to_dict() == stored
    assert compare_identity(a, b) == "incomparable"
    assert a != b
    assert b != b
    assert identity_from_dict(a.to_dict()) == a


@pytest.mark.parametrize("result", [IdentityUnavailable("no_telemetry"), IdentityFailed("read_error")])
def test_nonidentity_persistence(result):
    restored = identity_from_dict(json.loads(json.dumps(result.to_dict())))
    assert restored.to_dict() == result.to_dict()
    assert compare_identity(result, restored) == "incomparable"
    assert result != restored


@pytest.mark.parametrize("value", [None, {}, {"state": "available"},
    {"state": "available", "contract": IDENTITY_CONTRACT, "key": "A" * 64},
    {"state": "available", "contract": "1.0.0", "key": "a" * 64},
    {"state": "available", "contract": IDENTITY_CONTRACT, "key": "a" * 64, "path": "private-marker"},
    {"state": "failed", "contract": IDENTITY_CONTRACT, "code": "private-marker"},
    {"state": "unavailable", "contract": IDENTITY_CONTRACT, "reason": []},
])
def test_persistence_rejects_invalid_or_private_fields(value):
    with pytest.raises(ValueError):
        identity_from_dict(value)


def test_optional_absence_and_unrelated_stream_fields(tmp_path):
    baseline = read_recording_identity(media(tmp_path))
    unrelated = read_recording_identity(media(tmp_path, "unrelated", data=[payload(extra=numeric("TMPC", [20]))]))
    assert compare_identity(baseline, unrelated) == "same"
    no_scale = packet(*(stream(numeric(k, [[1, 2, 3], [4, 5, 6]], "s"),
                               numeric("STMP", [5_000_000], "J"), numeric("TSMP", [102], "J"))
                        for k in ("ACCL", "GYRO")))
    assert compare_identity(baseline, read_recording_identity(media(tmp_path, "no-scale", data=[no_scale]))) == "different"


def test_truncated_creation_and_gpmf_padding_are_failed(tmp_path):
    path = tmp_path / "invalid.mp4"
    path.write_bytes(box(b"moov", box(b"mvhd", b"\0" * 8)))
    assert read_recording_identity(path).code == "malformed_container"
    broken = packet(motion("ACCL"), motion("GYRO")) + record("TEST", "c", 1, 1, b"x")[:-1]
    path = media(tmp_path, data=[broken])
    assert read_recording_identity(path).code == "malformed_telemetry"


def run_check(*args):
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_recording_identity.py"
    process = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True)
    return process, json.loads(process.stdout)


def test_private_check_explicit_arbitrary_names_and_negative_chapter(tmp_path):
    a = media(tmp_path, "private-marker-original")
    b = media(tmp_path, "private-marker-preview", gps=True, serial=True)
    c = media(tmp_path, "private-marker-chapter", data=[payload(stamp=6_000_000, total=104)])
    files = sorted(tmp_path.iterdir())
    process, summary = run_check("--pair", a, b, "--different", a, c)
    assert process.returncode == 0
    assert summary["positive_passed"] == 1
    assert summary["negative_passed"] == 1
    assert summary["alias_same"] == 3
    assert summary["repeat_and_persistence_passed"] == 3
    assert summary["checks_failed"] == 0
    assert summary["unavailable"] == summary["failed"] == {}
    assert process.stderr == ""
    assert "private-marker" not in process.stdout
    assert str(tmp_path) not in process.stdout
    assert read_recording_identity(a).key not in process.stdout
    assert sorted(tmp_path.iterdir()) == files


@pytest.mark.parametrize("defect", ["unavailable", "mismatch", "negative_same", "invalid", "missing", "arguments"])
def test_private_check_fails_without_private_output(tmp_path, defect):
    a = media(tmp_path, "private-marker")
    b = media(tmp_path, "private-marker-other", created=0 if defect == "unavailable" else 3_800_000_001)
    options = ["--pair", a, b]
    if defect == "negative_same":
        options = ["--pair", a, a, "--different", a, a]
    elif defect == "invalid":
        b.write_bytes(b"invalid media")
    elif defect == "missing":
        b.unlink()
    elif defect == "arguments":
        options = ["--private-marker"]
    process, summary = run_check(*options)
    assert process.returncode == (2 if defect in ("missing", "arguments") else 1)
    if process.returncode == 1:
        assert summary["checks_failed"] > 0
    else:
        assert summary["error"] in ("invalid_arguments", "input_or_check_error")
    assert process.stderr == ""
    assert "private-marker" not in process.stdout
    assert str(tmp_path) not in process.stdout


def test_private_check_optional_filename_survey(tmp_path):
    media(tmp_path, "GX010001 extra.MP4")
    media(tmp_path, "GL010001.LRV")
    media(tmp_path, "GL020001.LRV", data=[payload(stamp=6_000_000, total=104)])
    media(tmp_path, "GX010002.MP4", created=3_800_000_001)
    media(tmp_path, "GL010002.LRV", created=3_800_000_001)
    process, summary = run_check("--gopro-folder", tmp_path)
    assert process.returncode == 0
    assert summary["survey_pairs"] == summary["survey_passed"] == 2
    assert summary["chapter_pairs"] == summary["chapter_passed"] == 2
    assert summary["cross_recording_pairs"] == summary["cross_recording_passed"] == 6
    assert summary["checks_failed"] == 0
    assert process.stderr == ""
    assert str(tmp_path) not in process.stdout


def test_embedded_null_path_is_invalid_argument():
    with pytest.raises(ValueError, match="embedded null"):
        read_recording_identity("invalid\0path")


@pytest.mark.parametrize("path", [None, 1, ["file"], b"file"])
def test_invalid_path_type_is_programmer_error(path):
    with pytest.raises(TypeError):
        read_recording_identity(path)
