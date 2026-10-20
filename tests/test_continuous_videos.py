from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from itertools import permutations
import json

import numpy as np
import pytest

from gps_video_sync import RecordingClock, Telemetry, Track, match_videos, read_video, synchronize
from gps_video_sync.cli import _export
from gps_video_sync.shots import find_shots
from gps_video_sync.verify import select_result

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def chapters(tmp_path):
    track = Track(np.arange(3000.), np.zeros(3000), np.zeros(3000), np.zeros(3000), date(2026, 1, 1))
    recordings, paths, start = [], [], 0.
    for i, duration in enumerate((600.125, 20.375, 30.2)):
        time = np.arange(0, duration, .5)
        clock = RecordingClock(1000. + start, .001, {"ACCL": (i * 1000, (i + 1) * 1000)})
        telemetry = Telemetry(time, {}, f"camera-{2-i}.mp4", duration, clock=clock)
        if i == 0:
            telemetry.gps_time = np.arange(600.)
            telemetry.gps_utc = EPOCH + 100. + 1.001 * telemetry.gps_time
        path = tmp_path / f"input-{i}.npz"
        _export(telemetry, path)
        paths.append(path)
        recordings.append(telemetry)
        start += duration
    return track, recordings, paths


def without_shot(result):
    value = result.to_dict()
    value.pop("shot")
    return value


def test_weak_rescue_exact_drift_boundaries_permutations_and_identity(chapters):
    track, recordings, paths = chapters
    assert [synchronize(track, t).status for t in recordings] == ["ok", "ambiguous", "ambiguous"]
    expected = match_videos(track, paths, source="npz")
    assert [r.status for r in expected] == ["ok", "ok", "ok"]
    assert expected[0].drift["clock_rate"] == pytest.approx(1.001, abs=1e-10)
    assert expected[0].drift["clock_rate"] != 1
    start = 0.
    for i, result in enumerate(expected):
        assert result.video == recordings[i].video
        assert result.input_file == str(paths[i])
        assert result.duration == recordings[i].duration
        assert result.media_offset_seconds == 0
        assert result.date == track.date
        assert result.shot == {"status": "joined", "members": list(map(str, paths)),
                               "index": i, "start_seconds": start, "reason": "",
                               "diagnostics_coordinates": "shot"}
        start += result.duration
        if i:
            previous = expected[i-1]
            assert result.offset_seconds == previous.offset_seconds + previous.drift["clock_rate"] * previous.duration
    for order in permutations(paths):
        actual = match_videos(track, order, source="npz")
        assert [r.to_dict() for r in actual] == [expected[paths.index(p)].to_dict() for p in order]
        document = dict(schema_version="2.0.0", videos=[r.to_dict() for r in actual])
        for p in paths:
            assert select_result(document, read_video(p, source="npz")) == expected[paths.index(p)].to_dict()


@pytest.mark.parametrize("change", ["gap", "overlap", "error_bound", "counter_reset", "counter_mismatch",
                                    "shared_mismatch", "no_shared_stream", "serial", "clock_missing"])
def test_dual_evidence_required_and_negatives_stay_independent(chapters, change):
    track, recordings, paths = chapters
    first, second = recordings[:2]
    clock = second.clock
    if change in ("gap", "overlap"):
        clock = replace(clock, origin_seconds=clock.origin_seconds + (.2 if change == "gap" else -.2))
    elif change == "error_bound":
        clock = replace(clock, reach_seconds=.05)
    elif change in ("counter_reset", "counter_mismatch"):
        clock = replace(clock, sample_counters={"ACCL": (0 if change == "counter_reset" else 1001, 2000)})
    elif change == "shared_mismatch":
        first.clock = replace(first.clock, sample_counters={"ACCL": (0, 1000), "GYRO": (0, 1000)})
        clock = replace(clock, sample_counters={"ACCL": (1000, 2000), "GYRO": (1001, 2000)})
    elif change == "no_shared_stream":
        clock = replace(clock, sample_counters={"GYRO": (1000, 2000)})
    elif change == "serial":
        first.clock = replace(first.clock, camera="one")
        clock = replace(clock, camera="two")
    elif change == "clock_missing":
        clock = None
    second.clock = clock
    for recording, path in zip(recordings, paths):
        _export(recording, path)
    expected = [match_videos(track, [p], source="npz")[0].to_dict() for p in paths[:2]]
    actual = match_videos(track, paths[:2], source="npz")
    assert [r.to_dict() for r in actual] == expected
    assert actual[0].status == "ok" and actual[0].offset_seconds == pytest.approx(100, abs=1e-6)
    assert actual[1].status == "ambiguous"


def test_missing_middle_and_unrelated_inputs_do_not_change_peers(chapters, tmp_path):
    track, _, paths = chapters
    unrelated = tmp_path / "unrelated.npz"
    t = np.arange(120.)
    np.savez(unrelated, time=t, gps_time=t, gps_utc=EPOCH + 2000. + t)
    solo = match_videos(track, [unrelated], source="npz")[0]
    joined = match_videos(track, [paths[2], unrelated, paths[0], paths[1]], source="npz")
    assert joined[1].to_dict() == solo.to_dict()
    assert solo.offset_seconds == 2000
    for order in ([paths[0], paths[2]], [paths[2], tmp_path / "missing.npz", paths[0]]):
        actual = match_videos(track, order, source="npz")
        for path, result in zip(order, actual):
            assert result.to_dict() == match_videos(track, [path], source="npz")[0].to_dict()
    assert [r.status for r in actual] == ["ambiguous", "failed", "ok"]


def test_branches_fall_back_for_every_member(chapters, tmp_path):
    track, recordings, paths = chapters
    copy = replace(recordings[1], video="copy.mp4")
    extra = tmp_path / "copy.npz"
    _export(copy, extra)
    paths = [*paths[:2], extra]
    baseline = [match_videos(track, [p], source="npz")[0] for p in paths]
    for order in permutations(paths):
        actual = match_videos(track, order, source="npz")
        assert [r.status for r in actual] == [baseline[paths.index(p)].status for p in order]
        for path, result in zip(order, actual):
            assert without_shot(result) == without_shot(baseline[paths.index(path)])
            assert result.shot["status"] == "ambiguous"
            assert "branches" in result.shot["reason"]


def test_empty_member_never_promoted(chapters):
    track, recordings, paths = chapters
    recordings[1].time = np.empty(0)
    _export(recordings[1], paths[1])
    baseline = [match_videos(track, [p], source="npz")[0] for p in paths]
    actual = match_videos(track, paths, source="npz")
    assert [r.status for r in actual] == ["ok", "failed", "ambiguous"]
    assert [without_shot(r) for r in actual] == [without_shot(r) for r in baseline]
    assert all(r.shot["status"] == "ambiguous" for r in actual)


def test_unsolved_shot_keeps_independent_results(chapters):
    track, recordings, paths = chapters
    recordings[0].gps_time = recordings[0].gps_utc = np.empty(0)
    _export(recordings[0], paths[0])
    baseline = [match_videos(track, [p], source="npz")[0] for p in paths]
    actual = match_videos(track, paths, source="npz")
    assert [r.status for r in actual] == ["ambiguous"] * 3
    assert [without_shot(r) for r in actual] == [without_shot(r) for r in baseline]
    assert all(r.shot["status"] == "ambiguous" for r in actual)
    assert "not accepted" in actual[0].shot["reason"]


@pytest.mark.parametrize("error", [RuntimeError, ValueError])
def test_shot_programming_errors_propagate(chapters, monkeypatch, error):
    from gps_video_sync import sync
    track, _, paths = chapters
    original = sync._synchronize
    def broken(track, telemetry):
        if telemetry.video is None:
            raise error("shot defect")
        return original(track, telemetry)
    monkeypatch.setattr(sync, "_synchronize", broken)
    with pytest.raises(error, match="shot defect"):
        match_videos(track, paths, source="npz")


def test_projection_veto_checks_both_ends(chapters, monkeypatch):
    from gps_video_sync import sync
    track, _, paths = chapters
    baseline = match_videos(track, paths[:1], source="npz")[0]
    original = sync._synchronize
    def conflicting(track, telemetry):
        if telemetry.video is None:
            return replace(baseline, drift={"clock_rate": 1.004}, video=None,
                           duration=telemetry.duration)
        return original(track, telemetry)
    monkeypatch.setattr(sync, "_synchronize", conflicting)
    actual = match_videos(track, paths, source="npz")
    assert actual[0].offset_seconds == baseline.offset_seconds
    assert actual[0].status == "ok" and actual[1].status == "ambiguous"
    assert actual[0].shot["status"] == "ambiguous"
    assert "contradicts" in actual[0].shot["reason"]


@pytest.mark.parametrize("bad_clock", [None, {}, {"origin_seconds": "bad"},
    {"origin_seconds": 1, "reach_seconds": 0, "sample_counters": {}},
    {"origin_seconds": float("inf"), "reach_seconds": 0, "sample_counters": {"ACCL": [0, 10]}},
    {"origin_seconds": 1, "reach_seconds": -1, "sample_counters": {"ACCL": [0, 10]}},
    {"origin_seconds": 1, "reach_seconds": 0, "sample_counters": {"ACCL": [0., 10.]}},
    {"origin_seconds": 1, "reach_seconds": 0, "sample_counters": {"ACCL": [10, 0]}}])
def test_optional_malformed_npz_clock_keeps_matching_usable(chapters, bad_clock):
    track, recordings, paths = chapters
    recording = recordings[0]
    metadata = dict(video=recording.video, duration=recording.duration, media_offset_seconds=0, clock=bad_clock)
    np.savez(paths[0], time=recording.time, gps_time=recording.gps_time, gps_utc=recording.gps_utc,
             recording=json.dumps(metadata))
    loaded = read_video(paths[0], source="npz")
    assert loaded.clock is None
    result = match_videos(track, paths[:2], source="npz")
    assert [r.status for r in result] == ["ok", "ambiguous"]
    assert result[0].offset_seconds == pytest.approx(100, abs=1e-6)


def test_exports_and_crops_do_not_reuse_counter_endpoints(chapters, tmp_path):
    track, recordings, paths = chapters
    for telemetry, path in zip(recordings, paths):
        assert read_video(path, source="npz").clock == telemetry.clock
    cropped = recordings[0].slice(0, recordings[0].duration)
    assert cropped.clock is None
    nested = cropped.slice(2, 600)
    assert nested.clock is None
    path = tmp_path / "crop.npz"
    _export(cropped, path)
    assert read_video(path, source="npz").clock is None
    actual = match_videos(track, [path, paths[1]], source="npz")
    assert [r.shot["status"] for r in actual] == ["single", "single"]
    assert actual[0].status == "ok"
    forged = asdict(recordings[1].clock)
    metadata = dict(video="crop.mp4", duration=20., media_offset_seconds=2., clock=forged)
    np.savez(path, time=np.arange(20.), recording=json.dumps(metadata))
    assert read_video(path, source="npz").clock is None
    document = tmp_path / "telemetry.json"
    _export(recordings[0], document)
    assert json.loads(document.read_text())["recording"]["clock"] == json.loads(json.dumps(asdict(recordings[0].clock)))


def test_shot_can_solve_when_every_member_is_weak_alone(chapters, tmp_path):
    track, _, _ = chapters
    paths = [tmp_path / f"short-{i}.npz" for i in (0, 1)]
    for i, path in enumerate(paths):
        time = np.arange(8.)
        telemetry = Telemetry(time, {}, f"short-{i}.mp4", 8., time,
                              EPOCH + 100 + i * 8 + time,
                              clock=RecordingClock(1000 + i * 8, .001,
                                                   {"ACCL": (i * 100, (i + 1) * 100)}))
        _export(telemetry, path)
        assert synchronize(track, telemetry).status == "ambiguous"
    actual = match_videos(track, paths, source="npz")
    assert [r.status for r in actual] == ["ok", "ok"]
    assert [r.offset_seconds for r in actual] == [100., 108.]
    assert [r.shot["status"] for r in actual] == ["joined", "joined"]


def test_chain_serial_conflict_cannot_hide_behind_missing_serial(chapters):
    _, recordings, _ = chapters
    recordings[0].clock = replace(recordings[0].clock, camera="one")
    recordings[2].clock = replace(recordings[2].clock, camera="two")
    shots = find_shots(recordings)
    assert [s.status for s in shots] == ["ambiguous"] * 3
    assert [s.members for s in shots] == [(0,), (1,), (2,)]
