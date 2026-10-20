#!/usr/bin/env python3
from dataclasses import asdict, replace
from datetime import datetime, timezone
from itertools import permutations
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import numpy as np

from gps_video_sync import RecordingClock, Telemetry, match_videos, read_track, read_video, synchronize
from gps_video_sync.verify import plot_verification, select_result

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def cli(command, paths, *options, expected=0):
    process = subprocess.run([sys.executable, "-m", "gps_video_sync", command, *map(str, paths),
                              *map(str, options)], capture_output=True, text=True, cwd=ROOT)
    assert process.returncode == expected, (process.stdout, process.stderr)
    assert "Traceback" not in process.stderr
    return json.loads(process.stdout) if process.stdout else None


def save(path, telemetry):
    metadata = dict(video=telemetry.video, duration=telemetry.duration,
                    media_offset_seconds=telemetry.media_offset_seconds)
    if telemetry.clock is not None:
        metadata["clock"] = asdict(telemetry.clock)
    np.savez_compressed(path, time=telemetry.time, **telemetry.channels,
                        gps_time=telemetry.gps_time, gps_utc=telemetry.gps_utc,
                        recording=json.dumps(metadata), schema_version="2.0.0")


def unchanged(actual, expected):
    a, b = actual.to_dict(), expected.to_dict()
    a.pop("shot")
    b.pop("shot")
    assert a == b


def adjacent(results):
    assert all(r.status == "ok" and r.shot["status"] == "joined" for r in results)
    rate = results[0].drift.get("clock_rate", 1.)
    for a, b in zip(results, results[1:]):
        assert b.drift.get("clock_rate", 1.) == rate
        assert b.offset_seconds == a.offset_seconds + rate * a.duration


def arrays_equal(a, b):
    assert (a.video, a.duration, a.media_offset_seconds, a.clock) == (b.video, b.duration, b.media_offset_seconds, b.clock)
    for name in ("time", "gps_time", "gps_utc"):
        np.testing.assert_array_equal(getattr(a, name), getattr(b, name))
    assert a.channels.keys() == b.channels.keys()
    for key in a.channels:
        np.testing.assert_array_equal(a.channels[key], b.channels[key])


def check_synthetic(work):
    igc = work / "synthetic.igc"
    igc.write_text("HFDTE010126\nB0000000000000N00000000EA0000000000\nB0050000000000N00000000EA0000000000\n")
    track = read_track(igc)
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    paths, recordings, start = [], [], 0.
    for i, duration in enumerate((600.125, 20.375, 30.2)):
        time = np.arange(0, duration, .5)
        clock = RecordingClock(1000 + start, .001, {"ACCL": (i * 1000, (i + 1) * 1000)})
        t = Telemetry(time, {}, f"synthetic-{2-i}.mp4", duration, clock=clock)
        if i == 0:
            t.gps_time = np.arange(600.)
            t.gps_utc = epoch + 100 + 1.001 * t.gps_time
        path = work / f"synthetic-{i}.npz"
        save(path, t)
        paths.append(path)
        recordings.append(t)
        start += duration
    expected = match_videos(track, paths, source="npz")
    adjacent(expected)
    assert abs(expected[0].drift["clock_rate"] - 1.001) < 1e-10
    assert [synchronize(track, t).status for t in recordings] == ["ok", "ambiguous", "ambiguous"]
    for order in permutations(paths):
        results = match_videos(track, order, source="npz")
        wanted = [expected[paths.index(p)].to_dict() for p in order]
        assert [r.to_dict() for r in results] == wanted
        assert cli("sync", order, "--source", "npz", "--track", igc)["videos"] == wanted
    unrelated = work / "unrelated.npz"
    time = np.arange(120.)
    np.savez(unrelated, time=time, gps_time=time, gps_utc=epoch + 2000 + time)
    solo = match_videos(track, [unrelated], source="npz")[0]
    actual = match_videos(track, [*paths, unrelated], source="npz")
    assert actual[-1].to_dict() == solo.to_dict() and solo.offset_seconds == 2000
    missing = work / "missing.npz"
    actual = match_videos(track, [paths[2], missing, paths[0]], source="npz")
    assert [r.status for r in actual] == ["ambiguous", "failed", "ok"]
    for p, r in zip((paths[2], missing, paths[0]), actual):
        assert r.to_dict() == match_videos(track, [p], source="npz")[0].to_dict()
    assert cli("sync", [paths[2], missing, paths[0]], "--source", "npz", "--track", igc,
               expected=1)["videos"] == [r.to_dict() for r in actual]
    original = recordings[1]
    changes = [None, replace(original.clock, origin_seconds=original.clock.origin_seconds + .2),
               replace(original.clock, origin_seconds=original.clock.origin_seconds - .2),
               replace(original.clock, reach_seconds=.05),
               replace(original.clock, sample_counters={"ACCL": (0, 1000)}),
               replace(original.clock, sample_counters={"ACCL": (1001, 2000)}),
               replace(original.clock, sample_counters={"GYRO": (1000, 2000)})]
    for clock in changes:
        save(paths[1], replace(original, clock=clock))
        actual = match_videos(track, paths[:2], source="npz")
        assert [r.shot["status"] for r in actual] == ["single", "single"]
        assert [r.status for r in actual] == ["ok", "ambiguous"]
        for p, r in zip(paths, actual):
            assert r.to_dict() == match_videos(track, [p], source="npz")[0].to_dict()
        assert cli("sync", paths[:2], "--source", "npz", "--track", igc, expected=2)["videos"] == [r.to_dict() for r in actual]
    save(paths[1], original)
    extra = work / "branch.npz"
    save(extra, replace(original, video="branch.mp4"))
    for order in ([paths[0], paths[1], extra], [extra, paths[1], paths[0]]):
        actual = match_videos(track, order, source="npz")
        assert all(r.shot["status"] == "ambiguous" for r in actual)
        for p, r in zip(order, actual):
            unchanged(r, match_videos(track, [p], source="npz")[0])
    save(paths[1], replace(original, time=np.empty(0)))
    actual = match_videos(track, paths, source="npz")
    assert [r.status for r in actual] == ["ok", "failed", "ambiguous"]
    assert all(r.shot["status"] == "ambiguous" for r in actual)
    save(paths[1], original)
    save(paths[0], replace(recordings[0], gps_time=np.empty(0), gps_utc=np.empty(0)))
    actual = match_videos(track, paths, source="npz")
    assert all(r.status == "ambiguous" and r.shot["status"] == "ambiguous" for r in actual)
    print("Synthetic non-unit drift, weak rescue, shuffling, missing middle, counter vetoes, branches, and failures passed")


def check_real(work):
    track = read_track(FIXTURES / "flight.igc")
    paths = [FIXTURES / "chapter-01.mp4", FIXTURES / "chapter-02.mp4"]
    cache = work / "cache"
    recordings = [read_video(p, cache_dir=cache) for p in paths]
    for t, p in zip(recordings, paths):
        assert t.clock is not None
        arrays_equal(read_video(p, cache_dir=cache), t)
    a, b = recordings
    assert abs(b.clock.origin_seconds - a.clock.origin_seconds - a.duration) < .001
    assert a.clock.sample_counters["ACCL"][1] == b.clock.sample_counters["ACCL"][0] == 427870
    expected = match_videos(track, paths, cache_dir=cache)
    assert [r.offset_seconds for r in expected] == [36160.0, 36459.9997]
    assert abs(expected[1].offset_seconds - synchronize(track, recordings[1]).offset_seconds) < .001
    adjacent(expected)
    assert abs(expected[0].offset_seconds - 36160.112) < 2
    for mode in ("cold", "warm"):
        for i, order in enumerate((paths, paths[::-1], paths[:1], paths[1:])):
            directory = work / f"cold-{i}" if mode == "cold" else cache
            results = match_videos(track, order, cache_dir=directory)
            wanted = ([expected[paths.index(p)].to_dict() for p in order] if len(order) == 2 else
                      [dict(synchronize(track, recordings[paths.index(p)]).to_dict(), input_file=str(p)) for p in order])
            assert [r.to_dict() for r in results] == wanted
            assert cli("sync", order, "--track", FIXTURES / "flight.igc", "--cache-dir", directory)["videos"] == wanted
    for saved_path in cache.glob("*.npz"):
        with np.load(saved_path, allow_pickle=False) as saved:
            content = dict(saved)
        metadata = json.loads(str(content["metadata"].item()))
        metadata["version"] = "2.0.0"
        metadata.pop("clock")
        content["metadata"] = np.array(json.dumps(metadata))
        np.savez_compressed(saved_path, **content)
    for p, t in zip(paths, recordings):
        arrays_equal(read_video(p, cache_dir=cache), t)
    document = dict(schema_version="2.0.0", videos=[r.to_dict() for r in expected])
    saved_result = work / "result.json"
    saved_result.write_text(json.dumps(document))
    exports = []
    for i, (p, t) in enumerate(zip(paths, recordings)):
        archive, exported_json = work / f"export-{i}.npz", work / f"export-{i}.json"
        cli("telemetry", [p], "--cache-dir", cache, "--out", archive)
        exported = read_video(archive, source="npz")
        arrays_equal(exported, t)
        exports.append(archive)
        cli("telemetry", [archive], "--source", "npz", "--out", exported_json)
        assert json.loads(exported_json.read_text())["recording"]["clock"] == json.loads(json.dumps(asdict(t.clock)))
        assert select_result(document, exported) == expected[i].to_dict()
        plot = work / f"chapter-{i}.png"
        cli("verify", [archive], "--source", "npz", "--track", FIXTURES / "flight.igc",
            "--result", saved_result, "--output", plot, "--frames", 1)
        assert plot.read_bytes().startswith(b"\x89PNG")
        direct = work / "direct.png"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "1.000000",
                        "-protocol_whitelist", "file", "-i", f"file:{p}", "-frames:v", "1", str(direct)],
                       check=True, capture_output=True)
        frame = plot.with_name(f"{plot.stem}-{p.stem}-frame-000-1.000000s.png")
        assert frame.read_bytes() == direct.read_bytes()
    exported_results = match_videos(track, exports, source="npz")
    adjacent(exported_results)
    assert [r.offset_seconds for r in exported_results] == [r.offset_seconds for r in expected]
    assert [r.video for r in exported_results] == [str(p) for p in paths]
    assert [r.input_file for r in exported_results] == [str(p) for p in exports]
    cropped = a.slice(45, 225)
    assert cropped.clock is None and cropped.slice(0, 100).clock is None
    crop_path = work / "crop.npz"
    save(crop_path, cropped)
    assert read_video(crop_path, source="npz").clock is None
    result = match_videos(track, [crop_path, exports[1]], source="npz")
    assert all(r.shot["status"] == "single" for r in result)
    plot_verification(track, cropped, result[0].to_dict(), work / "crop.png")
    weak_path = work / "weak.npz"
    save(weak_path, replace(b, channels={}))
    rescued = match_videos(track, [exports[0], weak_path], source="npz")
    assert synchronize(track, read_video(weak_path, source="npz")).status == "ambiguous"
    adjacent(rescued)
    epoch = datetime.combine(track.date, datetime.min.time(), timezone.utc).timestamp()
    for step in (False, True):
        first = replace(a, gps_time=a.time, gps_utc=epoch + 36160 + a.time) if step else a
        second = replace(b, gps_time=b.time, gps_utc=epoch + 36160 + a.duration + b.time + (1 if step else 20))
        conflicting = [work / "conflict-a.npz", work / "conflict-b.npz"]
        for p, t in zip(conflicting, (first, second)):
            save(p, t)
        actual = match_videos(track, conflicting, source="npz")
        assert all(r.status == "ok" and r.shot["status"] == "ambiguous" for r in actual)
        assert ("GPS UTC step" if step else "contradicts") in actual[0].shot["reason"]
        for p, r in zip(conflicting, actual):
            unchanged(r, match_videos(track, [p], source="npz")[0])
        assert cli("sync", conflicting, "--source", "npz", "--track", FIXTURES / "flight.igc")["videos"] == [r.to_dict() for r in actual]
    print("Real media cold/warm extraction, exact boundaries, subset semantics, cache rebuild, exports, crops, local verification, and contradiction vetoes passed")


def main():
    with TemporaryDirectory(prefix="continuous-videos-") as directory:
        work = Path(directory).resolve()
        check_real(work)
        check_synthetic(work)
    print("Continuous videos verified")


if __name__ == "__main__":
    main()
