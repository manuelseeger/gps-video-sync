from datetime import date, datetime, timezone
from itertools import permutations
import json

import numpy as np
import pytest

from gps_video_sync import RecordingClock, Telemetry, Track, match_videos
from gps_video_sync.cli import _export, main
from gps_video_sync.models import CameraStart, CameraSession, JoinedShot
from gps_video_sync.session import Anchor, SessionMember, place_session
from gps_video_sync.verify import select_result
from gps_video_sync import read_video

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def track():
    return Track(np.arange(1001.), np.zeros(1001), np.zeros(1001), np.zeros(1001), date(2026, 1, 1))


def recording(tmp_path, name, camera_time, *, offset=None, duration=30., camera=None,
              origin=0., counter=0, rate=1.):
    time = np.arange(duration)
    clock = RecordingClock(origin, 0., {"ACCL": (counter, counter + 100)}, camera)
    telemetry = Telemetry(time, {}, name + ".mp4", duration, clock=clock,
                          camera_start=CameraStart(1_000_000 + camera_time, 1., camera))
    if offset is not None:
        telemetry.gps_time = time
        telemetry.gps_utc = EPOCH + offset + rate * time
    path = tmp_path / (name + ".npz")
    _export(telemetry, path)
    return path, telemetry


def anchors(tmp_path, *, second_offset=500.):
    return [recording(tmp_path, "first", 0, offset=100)[0],
            recording(tmp_path, "second", 400, offset=second_offset)[0]]


def test_agreeing_anchors_place_short_partial_and_zero_coverage_clips(track, tmp_path):
    paths = anchors(tmp_path)
    for name, start, duration in [("short", 200, 20), ("partial", 850, 200), ("outside", 1200, 40)]:
        paths.append(recording(tmp_path, name, start, duration=duration)[0])
    results = match_videos(track, paths, source="npz")
    assert [r.status for r in results] == ["ok"] * 5
    assert [r.offset_seconds for r in results] == [100., 500., 300., 950., 1300.]
    for result, uncertainty, overlap in zip(results[2:], [2.58, 2.99, 2.74], [20., 50., 0.]):
        assert result.mapping.clock_rate == 1.
        assert result.uncertainty_seconds == pytest.approx(uncertainty)
        assert result.method == "session"
        assert isinstance(result.mapping.source, CameraSession)
        assert result.mapping.source.supporting == (str(paths[0]), str(paths[1]))
        source = result.to_dict()["source"]
        assert source["kind"] == "session"
        assert source["track_overlap_seconds"] == overlap
        assert source["independent_rejection"] == "Insufficient varying, overlapping motion (minimum 90 s)"
        assert source["camera_start"]["camera"] is None
        document = dict(schema_version="3.0.0", videos=[r.to_dict() for r in results])
        assert select_result(document, read_video(result.input_file, source="npz"))["offset_seconds"] == result.offset_seconds
    for order in (paths[::-1], paths[2:] + paths[:2], paths[1::2] + paths[::2]):
        actual = match_videos(track, order, source="npz")
        expected = {r.input_file: r.to_dict() for r in results}
        assert {r.input_file: r.to_dict() for r in actual} == expected


def test_contradiction_is_distinct_from_one_anchor_insufficiency(track, tmp_path):
    paths = anchors(tmp_path, second_offset=510)
    weak, _ = recording(tmp_path, "weak", 200, duration=20)
    one = match_videos(track, [paths[0], weak], source="npz")[-1]
    both = match_videos(track, [*paths, weak], source="npz")[-1]
    assert (one.status, one.offset_seconds, one.to_dict()["session"]["verdict"]) == ("ambiguous", None, "insufficient")
    assert one.message == "Insufficient varying, overlapping motion (minimum 90 s). Fewer than two accepted recordings from this camera"
    assert (both.status, both.offset_seconds, both.to_dict()["session"]["verdict"]) == ("ambiguous", None, "contradictory")
    assert both.message == "Insufficient varying, overlapping motion (minimum 90 s). Accepted recordings disagree on the camera clock offset"
    assert both.to_dict()["session"]["recordings"] == list(map(str, paths))


def test_pairwise_agreement_does_not_widen_for_a_distant_target():
    values = [SessionMember("a.mp4", CameraStart(0, 1), 10, Anchor(0, .5, ("a.mp4",))),
              SessionMember("b.mp4", CameraStart(100, 1), 10, Anchor(110, .5, ("b.mp4",))),
              SessionMember("far.mp4", CameraStart(50000, 1), 10)]
    for order in permutations(values):
        verdict = place_session(order)["far.mp4"]
        assert verdict.message == "Accepted recordings disagree on the camera clock offset"
        assert verdict.recordings == ("a.mp4", "b.mp4")


def test_camera_timeline_overlap_is_contradictory(track, tmp_path):
    paths = anchors(tmp_path)
    weak, _ = recording(tmp_path, "overlap", 10, duration=10)
    result = match_videos(track, [*paths, weak], source="npz")[-1]
    assert result.status == "ambiguous"
    assert result.offset_seconds is None
    assert result.message == "Insufficient varying, overlapping motion (minimum 90 s). Recordings overlap on the camera clock"
    assert result.to_dict()["session"]["recordings"] == sorted([str(paths[0]), str(weak)])


@pytest.mark.parametrize("offset", [301., 320.])
def test_accepted_independent_mapping_stays_unchanged(track, tmp_path, offset):
    paths = anchors(tmp_path)
    accepted, _ = recording(tmp_path, "accepted", 200, offset=offset)
    weak, _ = recording(tmp_path, "weak", 300, duration=20)
    baseline = match_videos(track, [accepted], source="npz")[0].to_dict()
    results = match_videos(track, [*paths, accepted, weak], source="npz")
    assert results[2].to_dict() == baseline
    assert results[2].offset_seconds == offset
    if offset == 301:
        assert results[3].status == "ok"
        assert results[3].offset_seconds == pytest.approx(400.5)
    else:
        assert results[3].status == "ambiguous"
        assert results[3].offset_seconds is None
        assert "Accepted recordings disagree on the camera clock offset" in results[3].message


def test_gps_step_and_failed_recording_are_not_promoted(track, tmp_path):
    paths = anchors(tmp_path)
    stepped, telemetry = recording(tmp_path, "stepped", 200, offset=300, duration=20)
    telemetry.gps_utc[10:] += .5
    _export(telemetry, stepped)
    empty, telemetry = recording(tmp_path, "empty", 300)
    telemetry.time = np.empty(0)
    _export(telemetry, empty)
    results = match_videos(track, [*paths, stepped, empty], source="npz")
    assert [r.status for r in results] == ["ok", "ok", "ambiguous", "failed"]
    assert results[2].offset_seconds is None
    assert results[2].to_dict()["session"]["verdict"] == "contradictory"
    assert results[2].to_dict()["session"]["recordings"] == [str(stepped)]
    assert results[2].message == "GPS UTC has a step of more than 0.1 s; motion Insufficient varying, overlapping motion (minimum 90 s). A GPS UTC step blocks session placement"
    assert results[3].offset_seconds is None
    assert results[3].message == "No telemetry samples"


def test_joined_chapter_keeps_nonunit_rate_and_is_one_anchor(track, tmp_path):
    first, _ = recording(tmp_path, "chapter-1", 5, offset=100, duration=600,
                         origin=5, counter=0, rate=1.001)
    second, _ = recording(tmp_path, "chapter-2", 605, duration=20, origin=605, counter=100)
    weak, _ = recording(tmp_path, "weak", 1005, duration=20)
    without_other = match_videos(track, [first, second, weak], source="npz")
    assert without_other[2].status == "ambiguous"
    assert "Fewer than two accepted recordings from this camera" in without_other[2].message
    other, _ = recording(tmp_path, "other", 805, offset=900)
    results = match_videos(track, [first, second, weak, other], source="npz")
    assert [r.status for r in results] == ["ok"] * 4
    assert results[1].offset_seconds == pytest.approx(700.6, abs=1e-6)
    assert results[1].mapping.clock_rate == pytest.approx(1.001, abs=1e-10)
    assert results[1].mapping == without_other[1].mapping
    assert isinstance(results[1].mapping.source, JoinedShot)
    assert results[1].method == "gps"
    assert results[2].offset_seconds == pytest.approx(1100., abs=1e-6)
    assert results[2].mapping.clock_rate == 1
    assert results[2].mapping.source.supporting == tuple(sorted(map(str, [first, second, other])))


def test_unapproved_continuity_mapping_blocks_session_rescue(track, tmp_path):
    paths = []
    for i, offset in enumerate([100, 130.3, None]):
        path, _ = recording(tmp_path, f"chapter-{i}", 5 + i * 30, offset=offset,
                            origin=5 + i * 30, counter=i * 100)
        paths.append(path)
    results = match_videos(track, paths, source="npz")
    assert [r.status for r in results] == ["ok", "ok", "ambiguous"]
    assert results[0].offset_seconds == 100
    assert results[1].offset_seconds == pytest.approx(130.3)
    assert results[2].offset_seconds is None
    assert results[2].to_dict()["session"]["verdict"] == "contradictory"
    assert results[2].message == "Insufficient varying, overlapping motion (minimum 90 s). Continuity candidate has no approved common mapping"
    assert results[2].shot["status"] == "ambiguous"


def test_chapter_creation_disagreement_is_contradictory(track, tmp_path):
    first, _ = recording(tmp_path, "chapter-1", 5, offset=100, origin=5)
    second, _ = recording(tmp_path, "chapter-2", 37, origin=35, counter=100)
    other, _ = recording(tmp_path, "other", 405, offset=500)
    results = match_videos(track, [first, second, other], source="npz")
    assert [r.status for r in results] == ["ok", "ambiguous", "ok"]
    assert results[1].offset_seconds is None
    assert results[1].message == "Insufficient varying, overlapping motion (minimum 90 s). Chapter creation times disagree by more than 1 s"
    assert results[1].to_dict()["session"]["recordings"] == [str(first), str(second)]


@pytest.mark.parametrize("camera", [None, "different"])
def test_missing_and_different_serials_do_not_join_known_serial_anchors(track, tmp_path, camera):
    paths = [recording(tmp_path, "first", 0, offset=100, camera="known")[0],
             recording(tmp_path, "second", 400, offset=500, camera="known")[0],
             recording(tmp_path, "target", 200, camera=camera)[0]]
    results = match_videos(track, paths, source="npz")
    assert [r.status for r in results] == ["ok", "ok", "ambiguous"]
    assert results[2].offset_seconds is None
    assert "Fewer than two accepted recordings from this camera" in results[2].message


def test_loose_session_bound_has_no_offset():
    values = [SessionMember("a.mp4", CameraStart(0, 1), 10, Anchor(100, .5, ("a.mp4",))),
              SessionMember("b.mp4", CameraStart(400, 1), 10, Anchor(500, .5, ("b.mp4",))),
              SessionMember("long.mp4", CameraStart(500, 1), 4000)]
    verdict = place_session(values)["long.mp4"]
    assert verdict.message == "Session timing does not constrain the clip within 10 s"


def test_cli_places_session_without_a_flag(track, tmp_path):
    paths = anchors(tmp_path)
    paths.append(recording(tmp_path, "short", 200, duration=20)[0])
    igc = tmp_path / "flight.igc"
    igc.write_text("HFDTE010126\nB0000000000000N00000000EA0000000000\nB0016400000000N00000000EA0000000000\n")
    output = tmp_path / "result.json"
    assert main(["sync", *map(str, paths), "--source", "npz", "--track", str(igc), "--json", str(output)]) == 0
    value = json.loads(output.read_text())["videos"][2]
    assert value["offset_seconds"] == 300.
    assert value["method"] == "session"
    assert value["source"]["supporting"] == list(map(str, paths[:2]))
