from fractions import Fraction
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest

from gps_video_sync import read_track, read_video, synchronize, match_videos
from gps_video_sync.cli import main
from gps_video_sync import mp4
from gps_video_sync.gpmf import number, streams

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_OFFSET = 36160.112
FFPROBE_PARITY_MAJOR = 9


@pytest.fixture(scope="module")
def flight(tmp_path_factory):
    paths = [FIXTURES / "chapter-01.mp4", FIXTURES / "chapter-02.mp4"]
    cache = tmp_path_factory.mktemp("telemetry-cache")

    def forbidden(*args, **kwargs):
        pytest.fail("cold extraction started a subprocess")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess, "run", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        telemetry = [read_video(p, cache_dir=cache) for p in paths]
    return read_track(FIXTURES / "flight.igc"), telemetry, paths


@pytest.mark.parametrize("index", [0, 1])
def test_extraction(flight, index):
    _, recordings, paths = flight
    telemetry = recordings[index]
    assert telemetry.video == str(paths[index].resolve())
    assert set(telemetry.channels) == {"GYRO", "ACCL", "GRAV", "CORI", "IORI"}
    assert len(telemetry.time) == pytest.approx(3000, abs=2)
    assert telemetry.time[0] == 0
    assert np.all(np.diff(telemetry.time) > 0)
    for channel in telemetry.channels.values():
        assert channel.shape[0] == len(telemetry.time)
        assert np.isfinite(channel).mean() > .99
    assert len(telemetry.gps_time) == 0
    assert telemetry.duration == pytest.approx([8999991 / 30000, 9000992 / 30000][index], abs=1e-9)


@pytest.mark.ffprobe
@pytest.mark.parametrize("chapter", [1, 2])
def test_ffprobe_parity(chapter):
    if not shutil.which("ffprobe"):
        pytest.skip("optional parity requires ffprobe")
    version = subprocess.run(["ffprobe", "-version"], capture_output=True, text=True).stdout
    major = re.match(r"ffprobe version n?(\d+)\.", version)
    if not major or int(major[1]) < FFPROBE_PARITY_MAJOR:
        pytest.skip(f"parity values are verified with FFmpeg {FFPROBE_PARITY_MAJOR} or later")
    path = FIXTURES / f"chapter-{chapter:02}.mp4"
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_packets", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout)
    raw_probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-ignore_editlist", "1", "-show_packets", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout)
    for stream in probe["streams"]:
        fmt = stream["codec_tag_string"]
        if stream["codec_type"] != "video" and fmt != "gpmd":
            continue
        timeline = mp4.read_timeline(path, fmt)
        packets = [p for p in probe["packets"] if p["stream_index"] == stream["index"]]
        samples = {s.offset: s for s in timeline.samples}
        assert len(samples) == len(packets)
        scale = Fraction(stream["time_base"])
        shift = .701 if chapter == 1 and fmt == "gpmd" else 0
        for p in packets:
            sample = samples[int(p["pos"])]
            assert sample.size == int(p["size"])
            assert sample.duration == float(int(p["duration"]) * scale)
            assert sample.time == pytest.approx(float(p["pts_time"]) + shift, rel=0, abs=1e-6)
        media_time = Fraction(2002, 30000) if fmt != "gpmd" else Fraction(300 if chapter == 1 else 0, 1000)
        raw_packets = [p for p in raw_probe["packets"] if p["stream_index"] == stream["index"]]
        assert len(raw_packets) == len(samples)
        for p in raw_packets:
            sample = samples[int(p["pos"])]
            assert sample.size == int(p["size"])
            assert sample.time == pytest.approx(float(int(p["pts"]) * scale - media_time), rel=0, abs=1e-12)
        if stream["codec_type"] == "video":
            assert timeline.duration == pytest.approx(float(stream["duration"]), rel=0, abs=1e-6)


@pytest.mark.parametrize("chapter,times,locations,total_bytes", [
    (1, [-.3, .701], [(48, 6256), (8424, 6272), (15678, 6272)], 1880692),
    (2, [0, 1.001], [(974, 6272), (8284, 6256), (15444, 6272)], 1880116),
])
def test_fixture_samples_without_ffprobe(chapter, times, locations, total_bytes):
    timeline = mp4.read_timeline(FIXTURES / f"chapter-{chapter:02}.mp4")
    assert [s.time for s in timeline.samples[:2]] == pytest.approx(times, rel=0, abs=1e-12)
    assert [(s.offset, s.size) for s in timeline.samples[:3]] == locations
    assert sum(s.size for s in timeline.samples) == total_bytes


@pytest.mark.parametrize("index", [0, 1])
def test_alignment(flight, index):
    track, telemetry, paths = flight
    result = synchronize(track, telemetry[index])
    assert result.status == "ok"
    assert result.video == str(paths[index].resolve())
    assert result.offset_seconds == pytest.approx([36160.112, 36460.112][index], abs=2)
    assert not result.drift.get("detected", False)


@pytest.mark.parametrize("start,duration,status", [
    (0, 180, "ok"), (119, 180, "ok"),
    (0, 30, "ambiguous"), (200, 30, "ambiguous"),
])
def test_crops(flight, start, duration, status):
    track, telemetry, _ = flight
    result = synchronize(track, telemetry[0].slice(start, start + duration))
    assert result.status == status
    assert result.media_offset_seconds == start
    assert result.duration == duration
    if status == "ambiguous":
        assert result.offset_seconds is None
    else:
        assert result.offset_seconds == pytest.approx(EXPECTED_OFFSET + start, abs=2)


def test_permutations_subsets_api_and_cli(flight, tmp_path):
    track, _, paths = flight
    baseline = {r.video: r.to_dict() for r in match_videos(track, paths, cache_dir=tmp_path / "cache")}
    for order in (paths[::-1], paths, paths[:1], paths[1:]):
        results = match_videos(track, order, cache_dir=tmp_path / "cache")
        assert [r.to_dict() for r in results] == [baseline[str(p.resolve())] for p in order]
        proc = subprocess.run([sys.executable, "-m", "gps_video_sync", "sync", *map(str, order),
                               "--track", str(FIXTURES / "flight.igc"),
                               "--cache-dir", str(tmp_path / "cli-cache")], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        document = json.loads(proc.stdout)
        assert document["schema_version"] == "2.0.0"
        assert document["videos"] == [baseline[str(p.resolve())] for p in order]
    assert len(list((tmp_path / "cache").glob("*.npz"))) == 2
    assert len(list((tmp_path / "cli-cache").glob("*.npz"))) == 2


def test_verification_frames(flight, tmp_path):
    pytest.importorskip("matplotlib")
    if not shutil.which("ffmpeg"):
        pytest.skip("Frame extraction requires ffmpeg")
    from gps_video_sync.verify import plot_verification, _recording_frame

    track, recordings, paths = flight
    for index, telemetry in enumerate(recordings):
        assert _recording_frame(telemetry, 1) == (paths[index].resolve(), 1)
        with pytest.raises(ValueError, match="outside"):
            _recording_frame(telemetry, telemetry.duration)
        result = synchronize(track, telemetry)
        output = tmp_path / f"alignment-{index}.png"
        plot_verification(track, telemetry, result.to_dict(), output, frames=[1, .00001])
        assert output.read_bytes().startswith(b"\x89PNG")
        frames = sorted(tmp_path.glob(f"alignment-{index}-*-frame-*.png"))
        assert len(frames) == 2
        assert all(path.read_bytes().startswith(b"\x89PNG") for path in frames)
        assert not any(re.search(r"\de[-+]\d", path.name) for path in frames)
