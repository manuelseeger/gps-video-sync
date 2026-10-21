from dataclasses import asdict
from datetime import date, datetime, timezone
from itertools import permutations
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import numpy as np

from gps_video_sync import Track, match_videos, read_track, read_video, synchronize
from gps_video_sync.verify import plot_verification

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def cli(command, paths, *options, expected=0):
    process = subprocess.run([sys.executable, "-m", "gps_video_sync", command, *map(str, paths),
                              *map(str, options)], capture_output=True, text=True, cwd=ROOT)
    assert process.returncode == expected, (process.returncode, process.stdout, process.stderr)
    assert "Traceback" not in process.stderr
    return json.loads(process.stdout) if process.stdout else None


def equal_arrays(actual, expected):
    assert actual.video == expected.video
    assert actual.duration == expected.duration
    assert actual.media_offset_seconds == expected.media_offset_seconds
    np.testing.assert_array_equal(actual.time, expected.time)
    np.testing.assert_array_equal(actual.gps_time, expected.gps_time)
    np.testing.assert_array_equal(actual.gps_utc, expected.gps_utc)
    assert actual.channels.keys() == expected.channels.keys()
    for key in expected.channels:
        np.testing.assert_array_equal(actual.channels[key], expected.channels[key])


def frame_equal(work, plot, video, seconds, seek, index):
    direct = work / "direct.png"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{seek:.6f}",
                    "-protocol_whitelist", "file", "-i", f"file:{video}",
                    "-frames:v", "1", str(direct)], check=True, capture_output=True)
    frame = plot.with_name(f"{plot.stem}-{video.stem}-frame-{index:03d}-{seconds:.6f}s.png")
    assert frame.read_bytes() == direct.read_bytes()


def check_synthetic(work):
    time = np.arange(120.)
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    track = Track(np.arange(1000.), np.zeros(1000), np.zeros(1000), np.zeros(1000), date(2026, 1, 1))
    igc = work / "synthetic.igc"
    igc.write_text("HFDTE010126\nB0000000000000N00000000EA0000000000\nB0016400000000N00000000EA0000000000\n")
    paths = []
    for name, offset in (("first", 100), ("gap", 600), ("overlap", 150)):
        path = work / f"{name}.npz"
        np.savez(path, time=time, gps_time=time, gps_utc=epoch + offset + time)
        paths.append(path)
    baseline = {r.video: r.to_dict() for r in match_videos(track, paths, source="npz")}
    assert [r["offset_seconds"] for r in baseline.values()] == [100, 600, 150]
    for order in (*permutations(paths), paths[:1], paths[1:]):
        expected = [baseline[str(p)] for p in order]
        assert [r.to_dict() for r in match_videos(track, order, source="npz")] == expected
        document = cli("sync", order, "--track", igc, "--source", "npz")
        assert document == dict(schema_version="3.0.0", videos=expected)
    weak, bad = work / "weak.npz", work / "bad.npz"
    np.savez(weak, time=time, GYRO=np.zeros((len(time), 3)))
    np.savez(bad, time=time, GYRO=np.zeros(len(time)))
    for extra, code, status in ((weak, 2, "ambiguous"), (bad, 1, "failed"),
                                (work / "missing.npz", 1, "failed")):
        for order in ([extra, *paths], [*paths, extra]):
            results = match_videos(track, order, source="npz")
            assert [r.to_dict() for r in results if r.video in baseline] == list(baseline.values())
            assert next(r for r in results if r.video == str(extra)).status == status
            document = cli("sync", order, "--track", igc, "--source", "npz", expected=code)
            assert document["videos"] == [r.to_dict() for r in results]
    print("Synthetic GPS gaps, overlaps, permutations, subsets, and isolated failures passed")


def main():
    track = read_track(FIXTURES / "flight.igc")
    paths = [FIXTURES / "chapter-01.mp4", FIXTURES / "chapter-02.mp4"]
    with TemporaryDirectory(prefix="independent-videos-") as directory:
        work = Path(directory).resolve()
        baseline = {r.video: r.to_dict() for r in match_videos(track, paths, cache_dir=work / "baseline")}
        for result, offset in zip(baseline.values(), [36160.112, 36460.112]):
            assert result["status"] == "ok" and result["method"] == "motion"
            assert abs(result["offset_seconds"] - offset) < 2
        print("Real recording offsets " + ", ".join(str(r["offset_seconds"]) for r in baseline.values()))
        independent = {str(p): match_videos(track, [p], cache_dir=work / "baseline")[0].to_dict()
                       for p in paths}
        orders = [paths, paths[::-1], paths[:1], paths[1:]]
        for mode in ("cold", "warm"):
            for index, order in enumerate(orders):
                cache = work / (f"cold-{index}" if mode == "cold" else "shared")
                expected = [(baseline if len(order) > 1 else independent)[str(p)] for p in order]
                assert [r.to_dict() for r in match_videos(track, order, cache_dir=cache)] == expected
                document = cli("sync", order, "--track", FIXTURES / "flight.igc", "--cache-dir", cache)
                assert document == dict(schema_version="3.0.0", videos=expected)
        assert len(list((work / "shared").glob("*.npz"))) == 2
        saved = work / "results.json"
        saved.write_text(json.dumps(dict(schema_version="3.0.0", videos=list(baseline.values()))))
        recordings = []
        for index, path in enumerate(paths):
            telemetry = read_video(path, cache_dir=work / "shared")
            recordings.append(telemetry)
            info = cli("info", [path], "--cache-dir", work / "shared")
            assert info["video"] == str(path) and info["duration"] == telemetry.duration
            archive, exported_json = work / f"export-{index}.npz", work / f"export-{index}.json"
            cli("telemetry", [path], "--cache-dir", work / "shared", "--out", archive)
            exported = read_video(archive, source="npz")
            equal_arrays(exported, telemetry)
            assert synchronize(track, exported).to_dict() == synchronize(track, telemetry).to_dict()
            batch = match_videos(track, [archive], source="npz")[0].to_dict()
            assert batch == dict(independent[str(path)], input_file=str(archive))
            cli("telemetry", [archive], "--source", "npz", "--out", exported_json)
            document = json.loads(exported_json.read_text())
            assert document["schema_version"] == "3.0.0"
            assert document["recording"] == dict(video=str(path), duration=telemetry.duration,
                                                  media_offset_seconds=0.,
                                                  clock=json.loads(json.dumps(asdict(telemetry.clock))),
                                                  **({"camera_start": asdict(telemetry.camera_start)}
                                                     if telemetry.camera_start else {}))
            assert document["time"] == telemetry.time.tolist()
            for key, values in telemetry.channels.items():
                assert document["channels"][key] == np.where(np.isfinite(values), values, None).tolist()
            plot = work / f"alignment-{index}.png"
            seconds = [1., .00001, telemetry.duration - 1]
            cli("verify", [archive], "--source", "npz", "--track", FIXTURES / "flight.igc",
                "--result", saved, "--output", plot, "--frames", *seconds)
            assert plot.read_bytes().startswith(b"\x89PNG")
            for frame_index, second in enumerate(seconds):
                frame_equal(work, plot, path, second, second, frame_index)
            cli("verify", [path], "--track", FIXTURES / "flight.igc", "--cache-dir", work / "shared",
                "--result", saved, "--output", plot, "--frames", telemetry.duration, expected=1)
        slice_telemetry = recordings[0].slice(45, 225)
        sliced_result = synchronize(track, slice_telemetry)
        assert sliced_result.status == "ok" and abs(sliced_result.offset_seconds - 36205.112) < 2
        plot = work / "slice.png"
        plot_verification(track, slice_telemetry, sliced_result.to_dict(), plot, frames=[2.])
        frame_equal(work, plot, paths[0], 2., 47., 0)
        cropped = []
        for index, (recording, start) in enumerate(((recordings[0], 0), (recordings[0], 90), (recordings[1], 90))):
            telemetry = recording.slice(start, start + 180)
            path = work / f"crop-{index}.npz"
            np.savez_compressed(path, time=telemetry.time, **telemetry.channels)
            cropped.append(path)
        expected_offsets = [36160.112, 36250.112, 36550.112]
        expected = {r.video: r.to_dict() for r in match_videos(track, cropped, source="npz")}
        for result, offset in zip(expected.values(), expected_offsets):
            assert result["status"] == "ok" and abs(result["offset_seconds"] - offset) < 2
        for order in (*permutations(cropped), cropped[:1], cropped[1:]):
            results = [expected[str(p)] for p in order]
            assert [r.to_dict() for r in match_videos(track, order, source="npz")] == results
            document = cli("sync", order, "--source", "npz", "--track", FIXTURES / "flight.igc")
            assert document["videos"] == results
        print("Real motion gaps and overlaps without camera clocks passed")
        check_synthetic(work)
        print("Independent videos verified with cold/warm caches, NPZ/JSON exports, plots, and 7 direct-seek frame comparisons")


if __name__ == "__main__":
    main()
