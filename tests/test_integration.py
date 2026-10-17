import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from gps_video_sync import read_track, read_video, synchronize
from gps_video_sync.cli import main

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_OFFSET = 36160.112


@pytest.fixture(scope="module")
def flight(tmp_path_factory):
    if not shutil.which("ffprobe"):
        pytest.skip("GoPro extraction requires ffprobe")
    paths = [FIXTURES / "chapter-01.mp4", FIXTURES / "chapter-02.mp4"]
    cache = tmp_path_factory.mktemp("telemetry-cache")
    return read_track(FIXTURES / "flight.igc"), read_video(paths, cache_dir=cache), paths


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
    (0, 180, "ok"), (120, 180, "ok"), (240, 180, "ambiguous"), (300, 180, "ok"),
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
                      frames=[boundary - 1, boundary + 1], videos=paths)
    assert output.stat().st_size > 0
    frames = list(tmp_path.glob("alignment-frame-*.png"))
    assert len(frames) == 2
    assert all(path.stat().st_size > 0 for path in frames)
