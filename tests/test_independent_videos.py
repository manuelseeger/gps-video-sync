from datetime import date, datetime, timezone
from itertools import permutations
import json
from pathlib import Path

import numpy as np
import pytest

from gps_video_sync import Track, Telemetry, match_videos, read_video, register_video
from gps_video_sync.cli import _export, main
from gps_video_sync.verify import _recording_frame, plot_verification, select_result


@pytest.fixture
def recordings(tmp_path):
    time = np.arange(120.)
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    track = Track(np.arange(1000.), np.zeros(1000), np.zeros(1000), np.zeros(1000), date(2026, 1, 1))
    paths = []
    for name, offset in (("first", 100), ("gap", 600), ("overlap", 150)):
        path = tmp_path / f"{name}.npz"
        np.savez(path, time=time, gps_time=time, gps_utc=epoch + offset + time)
        paths.append(path)
    return track, paths


def test_permutations_subsets_and_unrelated_additions(recordings, tmp_path):
    track, paths = recordings
    baseline = {r.input_file: r.to_dict() for r in match_videos(track, paths, source="npz")}
    assert [r["offset_seconds"] for r in baseline.values()] == [100, 600, 150]
    for order in (*permutations(paths), paths[:1], paths[1:], paths[::2]):
        assert [r.to_dict() for r in match_videos(track, order, source="npz")] == [baseline[str(p)] for p in order]
    unrelated = tmp_path / "unrelated.npz"
    np.savez(unrelated, time=np.arange(20.), GYRO=np.zeros((20, 3)))
    results = match_videos(track, [unrelated, *paths], source="npz")
    assert results[0].status == "ambiguous"
    assert "Insufficient" in results[0].message
    assert [r.to_dict() for r in results[1:]] == list(baseline.values())


@pytest.mark.parametrize("damage", ["missing", "broken", "shape", "metadata", "legacy", "time"])
def test_expected_file_failure_does_not_block_peer(recordings, tmp_path, damage):
    track, paths = recordings
    good = match_videos(track, paths[:1], source="npz")[0].to_dict()
    bad = tmp_path / "bad.npz"
    if damage == "broken":
        bad.write_bytes(b"not an archive")
    elif damage == "shape":
        np.savez(bad, time=np.arange(10.), GYRO=np.ones(10))
    elif damage == "metadata":
        np.savez(bad, time=np.arange(10.), recording=json.dumps(dict(video="bad", duration="long", media_offset_seconds=0)))
    elif damage == "legacy":
        np.savez(bad, time=np.arange(10.), chapters="[]")
    elif damage == "time":
        np.savez(bad, time=[0, 0])
    for order in ([bad, paths[0]], [paths[0], bad]):
        results = match_videos(track, order, source="npz")
        assert next(r for r in results if r.input_file == str(paths[0])).to_dict() == good
        failed = next(r for r in results if r.input_file == str(bad))
        assert failed.status == "failed" and failed.video == str(bad) and failed.message


def test_shared_validation_precedes_file_errors(recordings, monkeypatch):
    import gps_video_sync
    track, paths = recordings
    monkeypatch.setattr(gps_video_sync, "read_video", lambda *a, **kw: pytest.fail("must validate before extraction"))
    for options in ({"source": "missing"}, {"hz": 0}, {"hz": float("nan")}):
        with pytest.raises(ValueError):
            match_videos(track, paths, **options)
    for paths in ([], "file.mp4", [None]):
        with pytest.raises(ValueError):
            match_videos(track, paths)
    track.latitude = np.array([0.])
    with pytest.raises(ValueError, match="Track"):
        match_videos(track, ["missing.mp4"])


def test_programming_errors_are_not_failed_matches(recordings):
    class BrokenSource:
        def read(self, *args, **kwargs):
            raise RuntimeError("implementation defect")
    register_video("test-broken", BrokenSource())
    track, paths = recordings
    with pytest.raises(RuntimeError, match="implementation defect"):
        match_videos(track, paths, source="test-broken")


def test_npz_identity_export_and_slice_roundtrip(tmp_path):
    video = tmp_path / "original.mp4"
    video.touch()
    telemetry = Telemetry(np.arange(20.), {"GYRO": np.zeros((20, 3))}, str(video), 20,
                          np.array([0., 10.]), np.array([1700000000., 1700000010.]))
    sliced = telemetry.slice(5, 15).slice(2, 8)
    assert sliced.time.tolist() == [0., 1., 2., 3., 4., 5.]
    assert sliced.duration == 6 and sliced.media_offset_seconds == 7
    assert _recording_frame(sliced, 2) == (video, 9)
    for seconds in (-1, 6, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            _recording_frame(sliced, seconds)
    archive = tmp_path / "export.npz"
    _export(sliced, archive)
    restored = read_video(archive, source="npz")
    assert restored.video == str(video) and restored.video != str(archive)
    assert restored.duration == 6 and restored.media_offset_seconds == 7
    np.testing.assert_array_equal(restored.time, sliced.time)
    np.testing.assert_array_equal(restored.channels["GYRO"], sliced.channels["GYRO"])
    np.testing.assert_array_equal(restored.gps_time, sliced.gps_time)
    np.testing.assert_array_equal(restored.gps_utc, sliced.gps_utc)
    assert _recording_frame(restored, 1) == (video, 8)
    _export(restored, tmp_path / "export.json")
    document = json.loads((tmp_path / "export.json").read_text())
    assert document["recording"] == {"video": str(video), "duration": 6, "media_offset_seconds": 7}
    assert document["time"] == [0., 1., 2., 3., 4., 5.]


def test_saved_result_selection_and_invalid_entries(recordings):
    track, paths = recordings
    entries = [r.to_dict() for r in match_videos(track, paths, source="npz")]
    document = dict(schema_version="2.0.0", videos=entries)
    assert select_result(document, str(paths[1])) == entries[1]
    with pytest.raises(ValueError, match="no unique"):
        select_result(document, "other.mp4")
    for videos in ([], [entries[0], entries[0]], [None], [{"video": "x"}],
                   [dict(entries[0], status="unknown")], [dict(entries[0], offset_seconds="bad")]):
        with pytest.raises(ValueError):
            select_result(dict(schema_version="2.0.0", videos=videos), str(paths[0]))


def test_cli_writes_all_results_and_failure_precedence(recordings, tmp_path, capsys):
    _, paths = recordings
    track = tmp_path / "flight.igc"
    track.write_text("HFDTE010126\nB0000000000000N00000000EA0000000000\nB0016400000000N00000000EA0000000000\n")
    weak = tmp_path / "weak.npz"
    np.savez(weak, time=np.arange(20.), GYRO=np.zeros((20, 3)))
    output = tmp_path / "results.json"
    assert main(["sync", str(paths[0]), str(weak), str(tmp_path / "missing.npz"),
                 "--source", "npz", "--track", str(track), "--json", str(output)]) == 1
    results = json.loads(output.read_text())["videos"]
    assert [r["status"] for r in results] == ["ok", "ambiguous", "failed"]
    assert results[0]["offset_seconds"] == 100
    errors = capsys.readouterr().err
    assert str(weak) in errors and "missing.npz" in errors


def test_frame_seeks_use_current_recording_not_saved_input(tmp_path, monkeypatch):
    video = tmp_path / "current.mp4"
    video.touch()
    telemetry = Telemetry(np.arange(10.), {}, str(video), 10).slice(3, 8)
    track = Track(np.arange(10.), np.zeros(10), np.zeros(10), np.zeros(10))
    from gps_video_sync import SyncResult
    result = SyncResult("ok", 0, video=str(video), input_file="untrusted.mp4").to_dict()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"frame")
    monkeypatch.setattr("gps_video_sync.verify.subprocess.run", run)
    plot_verification(track, telemetry, result, tmp_path / "plot.png", [2, .00001])
    assert calls[0][calls[0].index("-i") + 1] == f"file:{video}"
    assert calls[0][calls[0].index("-ss") + 1] == "5.000000"
    assert calls[1][calls[1].index("-ss") + 1] == "3.000010"
    assert calls[0][calls[0].index("-protocol_whitelist") + 1] == "file"
    assert "current-frame-000" in calls[0][-1]


def test_track_validated_once_before_batch(recordings, monkeypatch):
    from gps_video_sync import sync
    track, paths = recordings
    calls = []
    original = sync.validate_track
    def validate(value):
        calls.append(value)
        original(value)
    monkeypatch.setattr(sync, "validate_track", validate)
    assert [r.status for r in match_videos(track, paths, source="npz")] == ["ok", "ok", "ok"]
    assert len(calls) == 1 and calls[0] is track


@pytest.mark.parametrize("command,options", [
    ("info", []), ("telemetry", ["--out", "out.npz"]),
    ("verify", ["--track", "unused.igc", "--output", "plot.png"]),
])
def test_single_recording_cli_commands_reject_multiple(command, options):
    import subprocess
    import sys
    process = subprocess.run([sys.executable, "-m", "gps_video_sync", command,
                              "one.mp4", "two.mp4", *options], capture_output=True, text=True)
    assert process.returncode == 1
    assert "unrecognized arguments: two.mp4" in process.stderr
    assert "Traceback" not in process.stderr


@pytest.mark.parametrize("status", [[], {}, "unknown", None])
def test_saved_result_rejects_malformed_status(recordings, status):
    track, paths = recordings
    entry = match_videos(track, paths[:1], source="npz")[0].to_dict()
    entry["status"] = status
    with pytest.raises(ValueError, match="status"):
        select_result(dict(schema_version="2.0.0", videos=[entry]), str(paths[0]))


def test_invalid_path_isolated_from_valid_peer(recordings):
    track, paths = recordings
    expected = match_videos(track, paths[:1], source="npz")[0].to_dict()
    results = match_videos(track, ["invalid" + chr(0) + ".npz", paths[0]], source="npz")
    assert results[0].status == "failed"
    assert "null" in results[0].message
    assert results[1].to_dict() == expected


@pytest.mark.parametrize("field", ["duration", "media_offset_seconds"])
def test_oversized_metadata_is_isolated(recordings, tmp_path, field):
    track, paths = recordings
    archive = tmp_path / "oversized.npz"
    metadata = dict(video="original.mp4", duration=20, media_offset_seconds=0)
    metadata[field] = 10**400
    np.savez(archive, time=np.arange(20.), recording=json.dumps(metadata))
    results = match_videos(track, [archive, paths[0]], source="npz")
    assert [r.status for r in results] == ["failed", "ok"]
    assert "finite" in results[0].message
