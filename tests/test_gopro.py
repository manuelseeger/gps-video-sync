from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import subprocess

import numpy as np
import pytest

from mp4_builder import Track, box, build, table, video as video_track

from gps_video_sync import gopro, gpmf, mp4
from gps_video_sync.gopro import GoProSource
from gps_video_sync.gpmf import DTYPES, number, records, scaled, streams


def record(key, typ, size, count, data):
    return struct.pack(">4sBBH", key.encode(), ord(typ), size, count) + data + b"\0" * (-len(data) % 4)


def numeric(key, values, typ="l"):
    array = np.atleast_2d(values).astype(DTYPES[typ])
    return record(key, typ, array.shape[1] * array.dtype.itemsize, len(array), array.tobytes())


def stream(*fields):
    data = b"".join(fields)
    return record("STRM", "\0", 1, len(data), data)


def packet(*sensor_streams):
    data = b"".join(sensor_streams)
    return record("DEVC", "\0", 1, len(data), data)


def gps5(stamp="240101235959.500", fix=3, count=2):
    return stream(numeric("GPSF", [fix], "L"),
                  record("GPSU", "U", len(stamp), 1, stamp.encode()),
                  numeric("SCAL", [10**7, 10**7, 1000, 1000, 1000]),
                  numeric("GPS5", [[450000000, 70000000, 1200000, 2000, 3000]] * count))


def gps9(rows, *, type_text="lllllllSS", gpsf=None):
    data = b"".join(struct.pack(">lllllllHH", *row) for row in rows)
    fields = [record("TYPE", "c", 1, len(type_text), type_text.encode()),
              numeric("SCAL", [10**7, 10**7, 1000, 1000, 1000, 1, 1000, 100, 1]),
              record("GPS9", "?", 32, len(rows), data)]
    if gpsf is not None:
        fields.append(numeric("GPSF", [gpsf], "L"))
    return stream(*fields)


def video(tmp_path, name, payloads, *, duration=2.0, start=0.0, intervals=None, pts=None):
    path = tmp_path / name
    pts = np.arange(len(payloads)) + start if pts is None else np.asarray(pts)
    intervals = [1.0] * len(payloads) if intervals is None else intervals
    deltas = [round(i * 1000) for i in intervals]
    dts = np.r_[0, np.cumsum(deltas[:-1])] if deltas else []
    composition = [round(p * 1000) - int(d) for p, d in zip(pts, dts)]
    tracks = [video_track(deltas=[round(duration * 1000)], ctts=[round(start * 1000)])]
    if payloads:
        tracks.append(Track(deltas=deltas, payloads=payloads, ctts=composition,
                            ctts_version=int(any(c < 0 for c in composition))))
    path.write_bytes(build(tracks))
    return path


def observe_timeline(monkeypatch):
    calls = []
    original = mp4.read_timeline

    def read(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(mp4, "read_timeline", read)
    return calls


@pytest.mark.parametrize("duration,hz,count", [
    (16.92, 25, 423), (.29, 100, 29), (1.001, 10, 11),
    (2, 2.5, 5), (16.92, 10, 170),
])
def test_fractional_duration_grid_is_half_open(tmp_path, monkeypatch, duration, hz, count):
    from gps_video_sync import Track as SyncTrack, match_videos, read_video
    from gps_video_sync.sync import validate_telemetry

    path = video(tmp_path, "fractional.mp4",
                 [packet(stream(numeric("ACCL", [[0, 0, 10]] * 10)))], duration=duration)
    cache = tmp_path / "cache"
    track = SyncTrack(np.arange(100.), np.zeros(100), np.zeros(100), np.zeros(100))
    cold = read_video(path, hz=hz, cache_dir=cache)
    assert len(cold.time) == count
    assert cold.time[-1] < duration
    validate_telemetry(cold)

    monkeypatch.setattr(gopro, "_recording", lambda *a: pytest.fail("warm read must use cache"))
    warm = read_video(path, hz=hz, cache_dir=cache)
    assert len(warm.time) == count
    assert warm.time[-1] < duration
    np.testing.assert_array_equal(warm.time, cold.time)
    validate_telemetry(warm)
    result = match_videos(track, [path], hz=hz, cache_dir=cache)[0]
    assert result.status == "ambiguous"
    assert result.duration == duration


@pytest.mark.parametrize("duration,hz", [(4294967294, 10), (2, 500000.5), (2, 1e308)])
def test_oversized_grid_rejected_before_allocation(tmp_path, monkeypatch, duration, hz):
    payload = packet(stream(numeric("ACCL", [[0, 0, 10]])))
    path = tmp_path / "oversized.mp4"
    path.write_bytes(build([video_track(scale=1, deltas=[duration]),
                            Track(payloads=[payload])]))
    arange = np.arange
    def bounded_arange(stop, *args, **kwargs):
        if stop > 1_000_000:
            pytest.fail("oversized grid allocation attempted")
        return arange(stop, *args, **kwargs)
    monkeypatch.setattr(np, "arange", bounded_arange)
    with pytest.raises(ValueError, match="1,000,000"):
        GoProSource().read(path, hz=hz, cache_dir=tmp_path / "cache")


def test_oversized_recording_isolated_from_valid_peer(tmp_path, monkeypatch):
    from gps_video_sync import Track as SyncTrack, match_videos
    payload = packet(stream(numeric("ACCL", [[0, 0, 10]])))
    bad = tmp_path / "oversized.mp4"
    bad.write_bytes(build([video_track(scale=1, deltas=[4294967294]),
                           Track(payloads=[payload])]))
    good = video(tmp_path, "good.mp4", [payload], duration=1)
    track = SyncTrack(np.arange(100.), np.zeros(100), np.zeros(100), np.zeros(100))
    cache = tmp_path / "cache"
    baseline = match_videos(track, [good], cache_dir=cache)[0].to_dict()
    arange = np.arange
    def bounded_arange(stop, *args, **kwargs):
        if stop > 1_000_000:
            pytest.fail("oversized grid allocation attempted")
        return arange(stop, *args, **kwargs)
    monkeypatch.setattr(np, "arange", bounded_arange)
    for order in ([bad, good], [good, bad]):
        results = match_videos(track, order, cache_dir=cache)
        assert next(r.to_dict() for r in results if r.video == str(good)) == baseline
        failed = next(r for r in results if r.video == str(bad))
        assert failed.status == "failed" and "1,000,000" in failed.message
        assert failed.duration is None and failed.media_offset_seconds is None


@pytest.mark.parametrize("duration", [200000, float("inf"), 10**400])
def test_oversized_cache_metadata_rejected_before_sample_arrays(tmp_path, monkeypatch, duration):
    payload = packet(stream(numeric("ACCL", [[0, 0, 10]])))
    path = video(tmp_path, "cached.mp4", [payload], duration=2)
    cache = tmp_path / "cache"
    cache.mkdir()
    saved = gopro._cache_path(path, 10., cache)
    metadata = dict(version=gopro.CACHE_VERSION, channels=[], video=str(path), duration=duration)
    np.savez(saved, metadata=json.dumps(metadata), time=[0.], gps_time=[], gps_utc=[])
    getitem = np.lib.npyio.NpzFile.__getitem__
    def metadata_only(self, key):
        if key != "metadata":
            pytest.fail("oversized cache loaded sample arrays")
        return getitem(self, key)
    monkeypatch.setattr(np.lib.npyio.NpzFile, "__getitem__", metadata_only)
    result = GoProSource().read(path, hz=10, cache_dir=cache)
    assert result.duration == 2 and len(result.time) == 20
    assert list(result.channels) == ["ACCL"]


def test_records_endian_padding_and_scale():
    buf = numeric("TEST", [[-2, 256], [123, -32768]], "s")
    buf += record("NAME", "c", 1, 1, b"x") + numeric("LAST", [[0x01020304]], "L")
    parsed = list(records(buf))
    np.testing.assert_array_equal(number(parsed[0]), [[-2, 256], [123, -32768]])
    assert parsed[1].data == b"x"
    assert number(parsed[2])[0, 0] == 0x01020304
    fields = list(records(numeric("SCAL", [2, 4]) + numeric("ACCL", [[4, 12], [8, 16]])))
    np.testing.assert_array_equal(scaled(fields[1], {r.key: r for r in fields}), [[2, 3], [4, 4]])
    scalar = next(records(numeric("SCAL", [2])))
    np.testing.assert_array_equal(scaled(fields[1], {"SCAL": scalar}), [[2, 6], [4, 8]])


@pytest.mark.parametrize("buf", [b"X", b"1234567", b"123456789",
                                      struct.pack(">4sBBH", b"ACCL", ord("l"), 12, 1) + b"\0" * 8,
                                      record("NAME", "c", 1, 1, b"x")[:-1]])
def test_truncated_records(buf):
    with pytest.raises(ValueError, match="Truncated"):
        list(records(buf))


def test_nested_streams_and_complex_types():
    row = [450000000, 70000000, 1200000, 2000, 3000, 9000, 43200123, 65000, 3]
    for type_text in ("lllllllSS", "l[7]S[2]"):
        fields = {r.key: r for r in next(streams(packet(gps9([row], type_text=type_text))))}
        values = scaled(fields["GPS9"], fields)
        np.testing.assert_allclose(values, [[45, 7, 1200, 2, 3, 9000, 43200.123, 650, 3]])
        with pytest.raises(ValueError, match="requires TYPE"):
            number(fields["GPS9"])
        for bad_type in ("llllSS", "l[999999999]", "l[0]", "x"):
            with pytest.raises(ValueError):
                number(fields["GPS9"], bad_type)


@pytest.mark.parametrize("scale", [[0], [1, 2], [float("nan")]])
def test_invalid_scale(scale):
    rec = next(records(numeric("ACCL", [[1, 2, 3]])))
    scal = next(records(numeric("SCAL", scale, "d")))
    with pytest.raises(ValueError, match="SCAL"):
        scaled(rec, {"SCAL": scal})


def test_all_motion_channels_and_quaternion_signs(tmp_path):
    payload = packet(*[stream(numeric("SCAL", [2]),
                              numeric(key, [[2, 4, 6], [4, 6, 8]])) for key in ("ACCL", "GYRO", "GRAV")],
                     *[stream(numeric("SCAL", [1000]),
                              numeric(key, [[1000, 0, 0, 0], [-1000, 0, 0, 0]])) for key in ("CORI", "IORI")])
    path = video(tmp_path, "motion.mp4", [payload], duration=1.0)
    result = GoProSource().read(path, hz=4, cache_dir=tmp_path / "cache")
    np.testing.assert_allclose(result.time, [0, .25, .5, .75])
    assert set(result.channels) == set(gopro.KEYS)
    for key in ("ACCL", "GYRO", "GRAV"):
        np.testing.assert_allclose(result.channels[key], [[1, 2, 3], [1.5, 2.5, 3.5], [2, 3, 4], [2, 3, 4]])
    for key in ("CORI", "IORI"):
        np.testing.assert_allclose(result.channels[key], [[1, 0, 0, 0]] * 4)
    assert result.gps_time.size == result.gps_utc.size == 0




def test_gps9_primary_utc_and_fix_filter(tmp_path):
    rows = [[450000000, 70000000, 1200000, 2000, 3000, 9000, second, 150, fix]
            for second, fix in [(43200123, 3), (43200623, 2), (43201123, 3)]]
    payload = packet(gps9(rows), gps5("240101120000.000", count=3))
    path = video(tmp_path, "gps9.mp4", [payload], duration=1.5, intervals=[1.5])
    result = GoProSource().read(path, hz=10, cache_dir=tmp_path / "cache")
    np.testing.assert_allclose(result.gps_time, [0, 1])
    np.testing.assert_allclose(result.gps_utc - gopro.GPS_EPOCH - 9000 * 86400, [43200.123, 43201.123])
    path2 = video(tmp_path, "bad.mp4", [packet(gps9(rows, gpsf=0), gps5())])
    rejected = GoProSource().read(path2, cache_dir=tmp_path / "cache")
    assert len(rejected.gps_time) == len(rejected.gps_utc) == 0


@pytest.mark.parametrize("bad", ["garbage", "240132120000.000"])
def test_invalid_gpsu_ignored(tmp_path, bad):
    path = video(tmp_path, "bad-time.mp4", [packet(gps5(bad))])
    assert not len(GoProSource().read(path, cache_dir=tmp_path / "cache").gps_time)


def test_missing_gpmd_stream(tmp_path):
    path = video(tmp_path, "plain.mp4", [], duration=1)
    with pytest.raises(ValueError, match=r"No GoPro telemetry \(gpmd\) track found in: plain.mp4"):
        GoProSource().read(path, hz=2, cache_dir=tmp_path / "cache")



def test_deep_gpmf_nesting_is_rejected():
    payload = stream(numeric("ACCL", [[1, 2, 3]]))
    for _ in range(gpmf.MAX_DEPTH + 2):
        payload = record("DEVC", "\0", 1, len(payload), payload)
    with pytest.raises(ValueError, match="too deep"):
        list(streams(payload))


def test_variable_sample_durations(tmp_path):
    path = video(tmp_path, "variable.mp4",
                 [packet(gps5()), packet(gps5("240102000000.500"))],
                 intervals=[.5, 1.5], pts=[0, .5])
    result = GoProSource().read(path, cache_dir=tmp_path / "cache")
    np.testing.assert_array_equal(result.gps_time, [0, .25, .5, 1.25])


@pytest.mark.parametrize("damage", ["span", "header", "missing-timing"])
def test_truncated_and_malformed_video_packets(tmp_path, damage):
    payload = packet(stream(numeric("ACCL", [[1, 2, 3]])))
    if damage == "header":
        payload = payload[:-1]
    replacements = {}
    if damage == "span":
        replacements[b"stco"] = table(b"stco", [(2**32 - 1,)], ">I")
    elif damage == "missing-timing":
        replacements[b"stts"] = None
    path = tmp_path / "truncated.mp4"
    path.write_bytes(build([video_track(), Track(payloads=[payload], replacements=replacements)]))
    with pytest.raises(ValueError):
        GoProSource().read(path, cache_dir=tmp_path / "cache")


def test_cold_extraction_without_subprocess(tmp_path, monkeypatch):
    path = video(tmp_path, "native.mp4", [packet(gps5()), packet(gps5())])

    def forbidden(*args, **kwargs):
        pytest.fail("extraction started a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = GoProSource().read(path, cache_dir=tmp_path / "cache")
    np.testing.assert_array_equal(result.gps_time, [0, .5, 1, 1.5])
    epoch = datetime(2024, 1, 1, 23, 59, 59, 500000, tzinfo=timezone.utc).timestamp()
    np.testing.assert_array_equal(result.gps_utc - epoch, [0, .5, 0, .5])
    assert result.duration == 2


def test_partial_sample_sensor_positions(tmp_path):
    path = tmp_path / "partial.mp4"
    payload = packet(stream(numeric("ACCL", [[1, 2, 3]] * 4)))
    path.write_bytes(build([video_track(deltas=[1001]),
                           Track(deltas=[1001], payloads=[payload], edits=[(1001, 300)])]))
    chapter = gopro._recording(path)
    assert chapter.duration == 1.001
    np.testing.assert_allclose(chapter.motion["ACCL"][0][:, 0], [.2005, .45075], rtol=0, atol=1e-12)
    np.testing.assert_array_equal(chapter.motion["ACCL"][0][:, 1:], [[1, 2, 3], [1, 2, 3]])


def test_cache_roundtrip_all_fields_and_default_location(tmp_path, monkeypatch):
    path = video(tmp_path, "cache.mp4", [packet(gps5(), stream(numeric("CORI", [[1, 0, 0, 0]])))])
    calls = observe_timeline(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = GoProSource()
    result = source.read(path, hz=4)

    def forbidden(*args, **kwargs):
        pytest.fail("warm cache accessed the MP4 parser")

    with monkeypatch.context() as patch:
        patch.setattr(mp4, "read_timeline", forbidden)
        saved = source.read(path, hz=4)
    assert calls == [path]
    assert list((tmp_path / ".cache" / "gps-video-sync").glob("gopro-*.npz"))
    assert saved.video == result.video and saved.duration == result.duration
    np.testing.assert_array_equal(saved.time, result.time)
    np.testing.assert_array_equal(saved.gps_time, result.gps_time)
    np.testing.assert_array_equal(saved.gps_utc, result.gps_utc)
    np.testing.assert_array_equal(saved.channels["CORI"], result.channels["CORI"])
    source.read(path, hz=5)
    assert len(calls) == 2
    with path.open("ab") as handle:
        handle.write(box(b"free"))
    source.read(path, hz=4)
    assert len(calls) == 3
    stat = path.stat()
    import os
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    source.read(path, hz=4)
    assert len(calls) == 4
    monkeypatch.setattr(gopro, "CACHE_VERSION", "1.0.2")
    source.read(path, hz=4)
    assert len(calls) == 5


def test_old_cache_version_is_ignored(tmp_path, monkeypatch):
    path = video(tmp_path, "version.mp4", [packet(gps5())])
    cache = tmp_path / "cache"
    source = GoProSource()
    source.read(path, hz=2, cache_dir=cache)
    saved_path = next(cache.glob("*.npz"))
    with np.load(saved_path, allow_pickle=False) as saved:
        content = dict(saved)
    metadata = json.loads(str(content["metadata"].item()))
    metadata["version"] = "1.0.1"
    content["metadata"] = np.array(json.dumps(metadata))
    content["gps_utc"] = np.array([-1.0, -1.0])
    np.savez_compressed(saved_path, **content)
    calls = observe_timeline(monkeypatch)
    rebuilt = source.read(path, hz=2, cache_dir=cache)
    assert calls == [path]
    np.testing.assert_array_equal(rebuilt.gps_time, [0, .5])
    epoch = datetime(2024, 1, 1, 23, 59, 59, 500000, tzinfo=timezone.utc).timestamp()
    np.testing.assert_array_equal(rebuilt.gps_utc - epoch, [0, .5])




@pytest.mark.parametrize("hz", [0, -1, float("inf"), float("nan")])
def test_invalid_hz(hz):
    with pytest.raises(ValueError, match="hz"):
        GoProSource().read("unused.mp4", hz=hz)


def test_resample_sensor_gap_is_nan():
    blocks = [np.column_stack((np.arange(0, 1, .1), np.ones((10, 3)))),
              np.column_stack((np.arange(3, 4, .1), np.ones((10, 3))))]
    grid = np.arange(0, 4, .1)
    values = gopro._resample(blocks, grid, "GYRO", 10)
    assert np.isnan(values[11:30]).all()
    assert np.isfinite(values[:9]).all()



def test_gps5_local_utc_and_bad_fixes(tmp_path):
    path = video(tmp_path, "gps.mp4", [packet(gps5(count=4)), packet(gps5(fix=2))],
                 duration=2.25, start=5, pts=[5.25, 6.25])
    result = GoProSource().read(path, hz=2, cache_dir=tmp_path / "cache")
    epoch = datetime(2024, 1, 1, 23, 59, 59, 500000, tzinfo=timezone.utc).timestamp()
    np.testing.assert_allclose(result.gps_time, [.25, .5, .75, 1])
    np.testing.assert_allclose(result.gps_utc - epoch, [0, .25, .5, .75])
    assert result.video == str(path) and result.duration == 2.25


def test_independent_no_clock_cache_subset_and_corruption(tmp_path, monkeypatch):
    from gps_video_sync import Track, match_videos
    paths = [video(tmp_path, name, [packet(stream(numeric(key, [[1, 2, 3]])))], duration=1)
             for name, key in (("z.mp4", "ACCL"), ("a.mp4", "GYRO"))]
    calls = observe_timeline(monkeypatch)
    track = Track(np.arange(10.), np.zeros(10), np.zeros(10), np.zeros(10))
    cache = tmp_path / "cache"
    expected = match_videos(track, paths, cache_dir=cache)
    assert [r.status for r in expected] == ["ambiguous", "ambiguous"]
    assert calls == paths
    for order in (paths[::-1], paths[:1], paths[1:]):
        results = match_videos(track, order, cache_dir=cache)
        assert [r.to_dict() for r in results] == [next(r for r in expected if r.video == str(p)).to_dict() for p in order]
    assert calls == paths
    assert len(list(cache.glob("*.npz"))) == 2
    for item in cache.glob("*.npz"):
        item.write_bytes(b"broken cache")
    assert [r.to_dict() for r in match_videos(track, paths, cache_dir=cache)] == [r.to_dict() for r in expected]
    assert calls == paths + paths
    invalid = video(tmp_path, "no-gpmd.mp4", [], duration=1)
    results = match_videos(track, [paths[0], invalid, paths[1]], cache_dir=cache)
    assert [r.status for r in results] == ["ambiguous", "failed", "ambiguous"]
    assert results[1].video == str(invalid)


def test_single_path_boundary_and_missing_video(tmp_path):
    for paths in ([], ["video.mp4"]):
        with pytest.raises(ValueError, match="one video path"):
            GoProSource().read(paths)
    path = tmp_path / "no-video.mp4"
    path.write_bytes(build([Track()]))
    with pytest.raises(ValueError, match="no video"):
        GoProSource().read(path, cache_dir=tmp_path / "cache")
