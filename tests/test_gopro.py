"""Synthetic GPMF/MP4 packet tests; no video fixtures are required."""
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from igc_video_sync import gopro
from igc_video_sync.gopro import GoProSource
from igc_video_sync.gpmf import DTYPES, number, records, scaled, streams


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
    data = bytearray(b"not a real mp4")
    packets = []
    if pts is None:
        pts = np.arange(len(payloads)) + start
    if intervals is None:
        intervals = [1.0] * len(payloads)
    for payload, time, interval in zip(payloads, pts, intervals):
        packets.append({"stream_index": 7, "pts_time": str(time),
                        "duration_time": str(interval), "pos": str(len(data)), "size": str(len(payload))})
        data.extend(payload)
    path.write_bytes(data)
    meta = {"streams": [{"index": 2, "codec_type": "video", "duration": str(duration), "start_time": str(start)},
                        {"index": 1, "codec_type": "data", "codec_tag_string": "other"},
                        {"index": 7, "codec_type": "data", "codec_tag_string": "gpmd"}],
            "packets": packets, "format": {"duration": str(duration + 5)}}
    return path, meta


def install_probe(monkeypatch, mapping):
    calls = []

    def probe(path):
        calls.append(path)
        return mapping[path]

    monkeypatch.setattr(gopro, "_probe", probe)
    return calls


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


def test_all_motion_channels_and_quaternion_signs(tmp_path, monkeypatch):
    payload = packet(*[stream(numeric("SCAL", [2]),
                              numeric(key, [[2, 4, 6], [4, 6, 8]])) for key in ("ACCL", "GYRO", "GRAV")],
                     *[stream(numeric("SCAL", [1000]),
                              numeric(key, [[1000, 0, 0, 0], [-1000, 0, 0, 0]])) for key in ("CORI", "IORI")])
    path, meta = video(tmp_path, "motion.mp4", [payload], duration=1.0)
    calls = install_probe(monkeypatch, {path: meta})
    result = GoProSource().read([path], hz=4, cache_dir=tmp_path / "cache")
    assert calls == [path]
    np.testing.assert_allclose(result.time, [0, .25, .5, .75])
    assert set(result.channels) == set(gopro.KEYS)
    for key in ("ACCL", "GYRO", "GRAV"):
        np.testing.assert_allclose(result.channels[key], [[1, 2, 3], [1.5, 2.5, 3.5], [2, 3, 4], [2, 3, 4]])
    for key in ("CORI", "IORI"):
        np.testing.assert_allclose(result.channels[key], [[1, 0, 0, 0]] * 4)
    assert result.gps_time.size == result.gps_utc.size == 0


def test_chapter_order_actual_duration_and_missing_channels(tmp_path, monkeypatch):
    first, m1 = video(tmp_path, "z.mp4", [packet(stream(numeric("ACCL", [[1, 2, 3]])))], duration=1.25)
    second, m2 = video(tmp_path, "a.mp4", [packet(stream(numeric("GYRO", [[4, 5, 6]])))], duration=.75)
    # duration_ts/time_base is a valid alternative to duration.
    del m2["streams"][0]["duration"]
    m2["streams"][0].update(duration_ts=750, time_base="1/1000")
    calls = install_probe(monkeypatch, {first: m1, second: m2})
    result = GoProSource().read([first, second], hz=4, cache_dir=tmp_path / "cache")
    assert calls == [first, second]
    assert [c["file"] for c in result.chapters] == [str(first), str(second)]
    assert [c["start"] for c in result.chapters] == [0, 1.25]
    assert [c["duration"] for c in result.chapters] == [1.25, .75]
    assert len(result.time) == 8
    assert np.isnan(result.channels["GYRO"][:5]).all()
    assert np.isnan(result.channels["ACCL"][5:]).all()
    assert set(result.channels) == {"ACCL", "GYRO"}


def test_gps5_utc_offsets_chapters_and_bad_fixes(tmp_path, monkeypatch):
    first, m1 = video(tmp_path, "first.mp4", [packet(gps5()), packet(gps5(fix=2))], duration=2.25,
                      start=5, pts=[5.25, 6.25])
    second, m2 = video(tmp_path, "second.mp4", [packet(gps5("240102000001.500"))], duration=1)
    install_probe(monkeypatch, {first: m1, second: m2})
    result = GoProSource().read([first, second], hz=2, cache_dir=tmp_path / "cache")
    epoch = datetime(2024, 1, 1, 23, 59, 59, 500000, tzinfo=timezone.utc).timestamp()
    np.testing.assert_allclose(result.gps_time, [.25, .75, 2.25, 2.75])
    np.testing.assert_allclose(result.gps_utc - epoch, [0, .5, 2, 2.5])
    np.testing.assert_allclose(result.gps_utc - result.gps_time, epoch - .25)
    assert result.channels == {}


def test_gps9_primary_utc_and_fix_filter(tmp_path, monkeypatch):
    rows = [[450000000, 70000000, 1200000, 2000, 3000, 9000, second, 150, fix]
            for second, fix in [(43200123, 3), (43200623, 2), (43201123, 3)]]
    payload = packet(gps9(rows), gps5("240101120000.000", count=3))
    path, meta = video(tmp_path, "gps9.mp4", [payload], duration=1.5, intervals=[1.5])
    install_probe(monkeypatch, {path: meta})
    result = GoProSource().read([path], hz=10, cache_dir=tmp_path / "cache")
    np.testing.assert_allclose(result.gps_time, [0, 1])
    np.testing.assert_allclose(result.gps_utc - gopro.GPS_EPOCH - 9000 * 86400, [43200.123, 43201.123])
    # A poor GPSF must also reject GPS9, with no fallback to GPS5.
    path2, meta2 = video(tmp_path, "bad.mp4", [packet(gps9(rows, gpsf=0), gps5())])
    install_probe(monkeypatch, {path2: meta2})
    rejected = GoProSource().read([path2], cache_dir=tmp_path / "cache")
    assert len(rejected.gps_time) == len(rejected.gps_utc) == 0


@pytest.mark.parametrize("bad", ["garbage", "240132120000.000"])
def test_invalid_gpsu_ignored(tmp_path, monkeypatch, bad):
    path, meta = video(tmp_path, "bad-time.mp4", [packet(gps5(bad))])
    install_probe(monkeypatch, {path: meta})
    assert not len(GoProSource().read([path], cache_dir=tmp_path / "cache").gps_time)


def test_missing_gpmd_stream_and_sparse_probe_keys(tmp_path, monkeypatch):
    path, meta = video(tmp_path, "plain.mp4", [], duration=1)
    meta["streams"] = [{}, meta["streams"][0], {"codec_type": "data"}]
    del meta["packets"]
    install_probe(monkeypatch, {path: meta})
    result = GoProSource().read([path], hz=2, cache_dir=tmp_path / "cache")
    assert result.channels == {}
    assert result.chapters[0]["packets"] == 0
    np.testing.assert_array_equal(result.time, [0, .5])


def test_tag_and_inferred_packet_durations(tmp_path, monkeypatch):
    path, meta = video(tmp_path, "tag.mp4", [packet(gps5()), packet(gps5("240102000000.500"))])
    meta["streams"][2] = {"index": 7, "codec_tag": "0x646d7067"}
    for p in meta["packets"]:
        del p["duration_time"]
    install_probe(monkeypatch, {path: meta})
    result = GoProSource().read([path], cache_dir=tmp_path / "cache")
    np.testing.assert_array_equal(result.gps_time, [0, .5, 1, 1.5])


def test_video_duration_fallback_uses_video_packets():
    meta = {"streams": [{"index": 0, "codec_type": "video"}],
            "packets": [{"stream_index": 0, "pts_time": "2", "duration_time": ".25"},
                        {"stream_index": 0, "pts_time": "3", "duration_time": ".25"},
                        {"stream_index": 7, "pts_time": "6", "duration_time": "1"}],
            "format": {"duration": "7"}}
    assert gopro._video_timing(meta) == (2, 1.25)


@pytest.mark.parametrize("damage", ["size", "pos", "header", "missing-pos", "missing-pts"])
def test_truncated_and_malformed_video_packets(tmp_path, monkeypatch, damage):
    payload = packet(stream(numeric("ACCL", [[1, 2, 3]])))
    if damage == "header":
        payload = payload[:-1]
    path, meta = video(tmp_path, "truncated.mp4", [payload])
    if damage == "size":
        meta["packets"][0]["size"] = "999999"
    elif damage == "pos":
        meta["packets"][0]["pos"] = "-1"
    elif damage == "missing-pos":
        del meta["packets"][0]["pos"]
    elif damage == "missing-pts":
        del meta["packets"][0]["pts_time"]
    install_probe(monkeypatch, {path: meta})
    with pytest.raises(ValueError):
        GoProSource().read([path], cache_dir=tmp_path / "cache")


def test_single_ffprobe_invocation_and_packet_reads(tmp_path, monkeypatch):
    payload = packet(gps5())
    path, meta = video(tmp_path, "one-probe.mp4", [payload, payload])
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["check"]
        return SimpleNamespace(stdout=json.dumps(meta))

    monkeypatch.setattr(gopro.subprocess, "run", run)
    GoProSource().read([path], cache_dir=tmp_path / "cache")
    assert len(calls) == 1
    assert calls[0][0] == "ffprobe"
    assert "-show_streams" in calls[0] and "-show_packets" in calls[0]
    assert calls[0][-1] == str(path)


def test_cache_roundtrip_all_fields_and_default_location(tmp_path, monkeypatch):
    path, meta = video(tmp_path, "cache.mp4", [packet(gps5(), stream(numeric("CORI", [[1, 0, 0, 0]])))])
    calls = install_probe(monkeypatch, {path: meta})
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = GoProSource()
    result = source.read([path], hz=4)
    saved = source.read([path], hz=4)
    assert calls == [path]
    assert list((tmp_path / ".cache" / "igc-video-sync").glob("gopro-*.npz"))
    assert saved.chapters == result.chapters
    np.testing.assert_array_equal(saved.time, result.time)
    np.testing.assert_array_equal(saved.gps_time, result.gps_time)
    np.testing.assert_array_equal(saved.gps_utc, result.gps_utc)
    np.testing.assert_array_equal(saved.channels["CORI"], result.channels["CORI"])
    source.read([path], hz=5)
    assert len(calls) == 2
    # Size and nanosecond mtime are independent cache inputs.
    with path.open("ab") as handle:
        handle.write(b"x")
    source.read([path], hz=4)
    assert len(calls) == 3
    stat = path.stat()
    import os
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    source.read([path], hz=4)
    assert len(calls) == 4
    monkeypatch.setattr(gopro, "CACHE_VERSION", "1.0.2")
    source.read([path], hz=4)
    assert len(calls) == 5


def test_cache_chapter_order_and_corruption(tmp_path, monkeypatch):
    first, m1 = video(tmp_path, "first.mp4", [packet(gps5())])
    second, m2 = video(tmp_path, "second.mp4", [packet(gps5())])
    calls = install_probe(monkeypatch, {first: m1, second: m2})
    cache = tmp_path / "cache"
    source = GoProSource()
    source.read([first, second], cache_dir=cache)
    result = source.read([second, first], cache_dir=cache)
    assert calls == [first, second, second, first]
    assert result.chapters[0]["file"] == str(second)
    for item in cache.glob("*.npz"):
        item.write_bytes(b"broken cache")
    source.read([first, second], cache_dir=cache)
    assert len(calls) == 6


def test_quaternion_continuity_across_chapters_and_low_hz(tmp_path, monkeypatch):
    first, m1 = video(tmp_path, "a.mp4", [packet(stream(numeric("CORI", [[1, 0, 0, 0]])))], duration=.25)
    second, m2 = video(tmp_path, "b.mp4", [packet(stream(numeric("CORI", [[-1, 0, 0, 0]])))], duration=.25)
    install_probe(monkeypatch, {first: m1, second: m2})
    result = GoProSource().read([first, second], hz=4, cache_dir=tmp_path / "cache")
    np.testing.assert_array_equal(result.channels["CORI"], [[1, 0, 0, 0], [1, 0, 0, 0]])
    result = GoProSource().read([first, second], hz=1, cache_dir=tmp_path / "cache")
    assert result.channels["CORI"].shape == (1, 4)


@pytest.mark.parametrize("hz", [0, -1, float("inf"), float("nan")])
def test_invalid_hz(hz):
    with pytest.raises(ValueError, match="hz"):
        GoProSource().read(["unused.mp4"], hz=hz)


def test_resample_sensor_gap_is_nan():
    blocks = [np.column_stack((np.arange(0, 1, .1), np.ones((10, 3)))),
              np.column_stack((np.arange(3, 4, .1), np.ones((10, 3))))]
    grid = np.arange(0, 4, .1)
    values = gopro._resample(blocks, grid, "GYRO", 10)
    assert np.isnan(values[11:30]).all()
    assert np.isfinite(values[:9]).all()


def test_invalid_paths_and_missing_video(tmp_path, monkeypatch):
    for paths in ([], "video.mp4", Path("video.mp4")):
        with pytest.raises(ValueError, match="sequence"):
            GoProSource().read(paths)
    path, meta = video(tmp_path, "no-video.mp4", [])
    meta["streams"] = [{}]
    install_probe(monkeypatch, {path: meta})
    with pytest.raises(ValueError, match="no video"):
        GoProSource().read([path], cache_dir=tmp_path / "cache")
