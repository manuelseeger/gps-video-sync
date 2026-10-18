from datetime import date, datetime, timezone
from itertools import permutations
import json
from pathlib import Path

import numpy as np
import pytest

from gps_video_sync import Track, Telemetry, match_videos, read_video, register_video, synchronize
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
        assert failed.to_dict()["media_offset_seconds"] is None
        assert failed.to_dict()["duration"] is None


def test_symlink_loop_isolated_from_valid_peer(recordings, tmp_path):
    track, paths = recordings
    loop = tmp_path / "loop.npz"
    loop.symlink_to(loop)
    expected = match_videos(track, paths[:1], source="npz")[0].to_dict()
    for order in ([loop, paths[0]], [paths[0], loop]):
        results = match_videos(track, order, source="npz")
        failed = next(r for r in results if r.input_file == str(loop))
        assert failed.status == "failed"
        assert failed.video == str(loop)
        assert failed.message
        assert next(r.to_dict() for r in results if r.input_file == str(paths[0])) == expected


def test_unknown_video_source_is_a_boundary_error():
    with pytest.raises(ValueError, match="^Unknown or unavailable video source: bogus$"):
        read_video("missing.mp4", source="bogus")


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


@pytest.fixture
def unidentified_source(local_timelines, monkeypatch):
    from gps_video_sync.plugins import VIDEO_SOURCES
    track, telemetry = local_timelines
    telemetry.video = None

    class GPSSource:
        def read(self, path, **kwargs):
            return telemetry

    monkeypatch.setitem(VIDEO_SOURCES, "test-gps-only", GPSSource())
    return track, telemetry


def test_source_without_identity_uses_canonical_input(unidentified_source, tmp_path, monkeypatch):
    track, original = unidentified_source
    monkeypatch.chdir(tmp_path)
    path = "recording.mp4"
    expected = str(tmp_path / path)
    loaded = read_video(path, source="test-gps-only")
    assert loaded.video == expected
    result = match_videos(track, [path], source="test-gps-only")[0]
    assert result.video == expected and result.input_file == expected
    assert result.status == "ok" and result.offset_seconds == 100
    assert original.video is None


def test_cli_verifies_source_without_identity(unidentified_source, tmp_path, monkeypatch):
    _, original = unidentified_source
    monkeypatch.chdir(tmp_path)
    track = tmp_path / "flight.igc"
    track.write_text("HFDTE010126\nB0000000000000N00000000EA0000000000\nB0016400000000N00000000EA0000000000\n")
    saved = tmp_path / "results.json"
    output = tmp_path / "plot.png"
    options = ["--source", "test-gps-only", "--track", str(track)]
    assert main(["sync", "recording.mp4", *options, "--json", str(saved)]) == 0
    assert json.loads(saved.read_text())["videos"][0]["video"] == str(tmp_path / "recording.mp4")
    assert main(["verify", "recording.mp4", *options, "--result", str(saved),
                 "--output", str(output)]) == 0
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert original.video is None


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


@pytest.fixture
def local_timelines(recordings, tmp_path):
    track, _ = recordings
    time = np.arange(120.)
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    telemetry = Telemetry(time, {}, str(tmp_path / "original.mp4"), 120,
                          time, epoch + 100 + time)
    return track, telemetry


@pytest.mark.parametrize("case,status,method", [
    ("gps", "ok", "gps"), ("motion", "ambiguous", "motion"),
    ("short", "ambiguous", "motion"), ("empty", "failed", "motion"),
])
def test_sync_result_retains_local_timeline(local_timelines, tmp_path, case, status, method):
    track, telemetry = local_timelines
    telemetry = telemetry.slice(20, 100)
    if case != "gps":
        telemetry.gps_time = telemetry.gps_utc = np.empty(0)
    if case == "short":
        telemetry.time = np.array([0., .1])
    elif case == "empty":
        telemetry.time = np.empty(0)
    result = synchronize(track, telemetry)
    assert (result.status, result.method) == (status, method)
    assert result.media_offset_seconds == 20
    assert result.duration == 80
    assert result.to_dict()["media_offset_seconds"] == 20
    assert result.to_dict()["duration"] == 80
    archive = tmp_path / "timeline.npz"
    _export(telemetry, archive)
    batch = match_videos(track, [archive], source="npz")[0]
    assert batch.to_dict() == dict(result.to_dict(), input_file=str(archive))


@pytest.mark.parametrize("bounds", [(0, 120), (40, 120), (20, 110)])
def test_plot_rejects_result_for_other_local_timeline(local_timelines, tmp_path, bounds):
    track, telemetry = local_timelines
    current = telemetry.slice(20, 100)
    correct = synchronize(track, current).to_dict()
    assert correct["offset_seconds"] == 120
    plot_verification(track, current, correct, tmp_path / "correct.png")
    assert (tmp_path / "correct.png").read_bytes().startswith(b"\x89PNG")
    other = synchronize(track, telemetry.slice(*bounds)).to_dict()
    with pytest.raises(ValueError, match="timeline"):
        plot_verification(track, current, other, tmp_path / "wrong.png")
    assert not (tmp_path / "wrong.png").exists()


def test_saved_results_distinguish_crops_of_one_recording(local_timelines, tmp_path):
    track, telemetry = local_timelines
    paths = [tmp_path / f"crop-{i}.npz" for i in range(3)]
    for path, bounds in zip(paths, [(20, 100), (40, 120), (20, 110)]):
        _export(telemetry.slice(*bounds), path)
    results = match_videos(track, paths, source="npz")
    document = dict(schema_version="2.0.0", videos=[r.to_dict() for r in results])
    assert [r.offset_seconds for r in results] == [120, 140, 120]
    for path, offset, duration in zip(paths, [120, 140, 120], [80, 80, 90]):
        entry = select_result(document, read_video(path, source="npz"))
        assert entry["offset_seconds"] == offset
        assert entry["duration"] == duration
    with pytest.raises(ValueError, match="unique"):
        select_result(dict(schema_version="2.0.0", videos=[document["videos"][0]] * 2),
                      read_video(paths[0], source="npz"))


def test_cli_verify_saved_result_requires_current_timeline(local_timelines, tmp_path, capsys):
    _, telemetry = local_timelines
    track = tmp_path / "flight.igc"
    track.write_text("HFDTE010126\nB0000000000000N00000000EA0000000000\nB0016400000000N00000000EA0000000000\n")
    paths = [tmp_path / f"crop-{i}.npz" for i in range(4)]
    for path, bounds in zip(paths, [(0, 120), (20, 100), (40, 120), (20, 110)]):
        _export(telemetry.slice(*bounds), path)
    full = tmp_path / "full.json"
    batch = tmp_path / "batch.json"
    options = ["--source", "npz", "--track", str(track)]
    assert main(["sync", str(paths[0]), *options, "--json", str(full)]) == 0
    assert main(["sync", *map(str, paths), *options, "--json", str(batch)]) == 0
    output = tmp_path / "wrong.png"
    assert main(["verify", str(paths[1]), *options, "--result", str(full),
                 "--output", str(output)]) == 1
    assert not output.exists()
    assert "no unique" in capsys.readouterr().err
    for i, path in enumerate(paths):
        output = tmp_path / f"correct-{i}.png"
        assert main(["verify", str(path), *options, "--result", str(batch),
                     "--output", str(output)]) == 0
        assert output.read_bytes().startswith(b"\x89PNG")


def test_saved_result_selection_and_invalid_entries(recordings):
    track, paths = recordings
    entries = [r.to_dict() for r in match_videos(track, paths, source="npz")]
    document = dict(schema_version="2.0.0", videos=entries)
    assert select_result(document, read_video(paths[1], source="npz")) == entries[1]
    with pytest.raises(ValueError, match="no unique"):
        select_result(document, Telemetry(np.empty(0), {}, "other.mp4"))
    for videos in ([], [entries[0], entries[0]], [None], [{"video": "x"}],
                   [dict(entries[0], status="unknown")], [dict(entries[0], offset_seconds="bad")]):
        with pytest.raises(ValueError):
            select_result(dict(schema_version="2.0.0", videos=videos), read_video(paths[0], source="npz"))


def test_saved_result_ignores_unrelated_duplicate_timelines(recordings):
    track, paths = recordings
    entries = [r.to_dict() for r in match_videos(track, paths[:2], source="npz")]
    document = dict(schema_version="2.0.0", videos=[entries[0], entries[0], entries[1]])
    assert select_result(document, read_video(paths[1], source="npz")) == entries[1]
    with pytest.raises(ValueError, match="no unique"):
        select_result(document, read_video(paths[0], source="npz"))


@pytest.mark.parametrize("changes", [
    {"schema_version": "wrong"}, {"video": ""}, {"status": "unknown"},
    {"duration": -1}, {"confidence": []}, {"per_feature": []}, {"drift": []},
    {"offset_seconds": "bad"}, {"uncertainty_seconds": -1}, {"clockRate": 0},
])
def test_saved_result_validates_unrelated_entries(recordings, changes):
    track, paths = recordings
    entries = [r.to_dict() for r in match_videos(track, paths[:2], source="npz")]
    document = dict(schema_version="2.0.0", videos=[dict(entries[0], **changes), entries[1]])
    with pytest.raises(ValueError):
        select_result(document, read_video(paths[1], source="npz"))


@pytest.mark.parametrize("changes", [
    {"media_offset_seconds": None}, {"media_offset_seconds": -1},
    {"media_offset_seconds": True}, {"media_offset_seconds": float("nan")},
    {"media_offset_seconds": 10**400}, {"duration": 0}, {"duration": -1},
    {"duration": False}, {"duration": []}, {"duration": float("inf")},
])
def test_saved_result_rejects_invalid_timeline(local_timelines, changes):
    track, telemetry = local_timelines
    entry = dict(synchronize(track, telemetry).to_dict(), **changes)
    with pytest.raises(ValueError):
        select_result(dict(schema_version="2.0.0", videos=[entry]), telemetry)


@pytest.mark.parametrize("field", ["media_offset_seconds", "duration"])
def test_saved_result_requires_timeline_metadata(local_timelines, tmp_path, field):
    track, telemetry = local_timelines
    entry = synchronize(track, telemetry).to_dict()
    del entry[field]
    with pytest.raises(ValueError, match="timeline metadata"):
        select_result(dict(schema_version="2.0.0", videos=[entry]), telemetry)
    with pytest.raises(ValueError, match="timeline metadata"):
        plot_verification(track, telemetry, entry, tmp_path / "plot.png")


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
    result = SyncResult("ok", 0, video=str(video), input_file="untrusted.mp4",
                        media_offset_seconds=3, duration=5).to_dict()
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
        select_result(dict(schema_version="2.0.0", videos=[entry]), read_video(paths[0], source="npz"))


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
