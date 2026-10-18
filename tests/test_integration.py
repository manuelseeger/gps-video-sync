from fractions import Fraction
import json
import re
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from gps_video_sync import read_track, read_video, synchronize
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
        telemetry = read_video(paths, cache_dir=cache)
    return read_track(FIXTURES / "flight.igc"), telemetry, paths


def test_extraction(flight):
    _, telemetry, paths = flight
    assert set(telemetry.channels) == {"GYRO", "ACCL", "GRAV", "CORI", "IORI"}
    assert len(telemetry.time) == pytest.approx(6000, abs=2)
    assert np.all(np.diff(telemetry.time) > 0)
    for channel in telemetry.channels.values():
        assert channel.shape[0] == len(telemetry.time)
        assert np.isfinite(channel).mean() > 0.99
    assert len(telemetry.gps_time) == 0
    assert len(telemetry.chapters) == len(paths)
    assert telemetry.chapters[1]["start"] == pytest.approx(telemetry.chapters[0]["duration"])
    assert sum(c["duration"] for c in telemetry.chapters) == pytest.approx(600, abs=0.1)


def test_exact_chapter_timeline(flight):
    _, telemetry, _ = flight
    assert [c["duration"] for c in telemetry.chapters] == pytest.approx(
        [8999991 / 30000, 9000992 / 30000], rel=0, abs=1e-9)
    assert telemetry.chapters[1]["start"] == pytest.approx(8999991 / 30000, rel=0, abs=1e-9)
    assert sum(c["duration"] for c in telemetry.chapters) == pytest.approx(18000983 / 30000, rel=0, abs=1e-9)


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


def test_camera_clock_continuity():
    clocks, base = [], 0
    for chapter in (1, 2):
        path = FIXTURES / f"chapter-{chapter:02}.mp4"
        timeline = mp4.read_timeline(path)
        with path.open("rb") as handle:
            for sample in timeline.samples:
                handle.seek(sample.offset)
                for stream in streams(handle.read(sample.size)):
                    fields = {r.key: r for r in stream}
                    if "CORI" in fields:
                        stamp = int(number(fields["STMP"]).ravel()[0])
                        clocks.append(stamp / 1e6 - (base + sample.time))
        base += timeline.duration
    assert len(clocks) == 600
    assert max(clocks) - min(clocks) < .005


def test_alignment(flight):
    track, telemetry, paths = flight
    result = synchronize(track, telemetry)
    assert result.status == "ok"
    assert result.offset_seconds == pytest.approx(EXPECTED_OFFSET, abs=2)
    assert not result.drift.get("detected", False)
    assert len(result.chapters) == len(paths)


@pytest.mark.parametrize("chapter", [0, 1])
def test_individual_chapters(flight, tmp_path, chapter):
    track, telemetry, paths = flight
    result = synchronize(track, read_video([paths[chapter]], cache_dir=tmp_path))
    assert result.status == "ok"
    expected = EXPECTED_OFFSET + telemetry.chapters[chapter]["start"]
    assert result.offset_seconds == pytest.approx(expected, abs=2)


@pytest.mark.parametrize("start,duration,status", [
    (0, 180, "ok"), (120, 180, "ok"), (240, 180, "ok"), (300, 180, "ok"),
    (0, 30, "ambiguous"), (300, 30, "ambiguous"),
])
def test_crops(flight, start, duration, status):
    track, telemetry, _ = flight
    result = synchronize(track, telemetry.slice(start, start + duration))
    assert result.status == status
    if status == "ambiguous":
        assert result.offset_seconds is None
    else:
        assert result.offset_seconds == pytest.approx(EXPECTED_OFFSET + start, abs=2)


def test_cli_sync(flight, tmp_path):
    _, _, paths = flight
    output = tmp_path / "sync.json"
    assert main(["sync", *map(str, paths), "--track", str(FIXTURES / "flight.igc"),
                 "--cache-dir", str(tmp_path / "cache"), "--json", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["status"] == "ok"
    assert result["offset_seconds"] == pytest.approx(EXPECTED_OFFSET, abs=2)


def test_verification_frames(flight, tmp_path):
    pytest.importorskip("matplotlib")
    if not shutil.which("ffmpeg"):
        pytest.skip("Frame extraction requires ffmpeg")
    from gps_video_sync.verify import plot_verification

    track, telemetry, paths = flight
    result = synchronize(track, telemetry)
    output = tmp_path / "alignment.png"
    boundary = telemetry.chapters[1]["start"]
    plot_verification(track, telemetry, result.to_dict(), output,
                      frames=[boundary - 1, boundary + 1, 0.00001, boundary + 1e-7], videos=paths)
    assert output.stat().st_size > 0
    frames = sorted(tmp_path.glob("alignment-frame-*.png"))
    assert len(frames) == 4
    assert all(path.read_bytes().startswith(b"\x89PNG") for path in frames)
    assert not any(re.search(r"\de[-+]\d", path.name) for path in frames)
