"""Real media is external and read-only. No fixture data enters the repository."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import warnings
import pytest
from igc_video_sync import read_track, read_video, synchronize

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def flight():
    root = Path(os.environ.get("IGC_VIDEO_SYNC_FIXTURES", "~/dev/fliew/fixtures")).expanduser()
    tracks = sorted(root.glob("*.igc"))
    if not tracks or not shutil.which("ffprobe"):
        pytest.skip("External IGC/video fixtures or ffprobe absent")
    candidates = sorted(root.glob("GL0?0351.LRV"))
    valid = []
    for path in candidates:
        # A transfer can leave a file without a complete MP4 index. Never use it.
        try:
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_packets",
                                    "-select_streams", "v:0", "-show_entries", "packet=pos,size:stream=duration",
                                    "-of", "json", str(path)], capture_output=True, check=True, timeout=60)
            meta = json.loads(probe.stdout)
            packets = meta.get("packets", [])
            if not packets or any(int(p.get("pos", 0))+int(p.get("size", 0)) > path.stat().st_size for p in packets):
                raise ValueError("truncated video samples")
            if probe.stderr:
                raise ValueError(probe.stderr.decode(errors="replace"))
        except (subprocess.SubprocessError, ValueError):
            warnings.warn(f"Ignoring incomplete fixture {path.name}")
            continue
        valid.append(path)
    if len(valid) < 2 or valid[0].name != "GL010351.LRV" or valid[1].name != "GL020351.LRV":
        pytest.skip("Need complete first two chapters")
    return read_track(tracks[0]), read_video(valid), valid


def test_full_flight(flight):
    track, telemetry, paths = flight
    result = synchronize(track, telemetry)
    print("FULL", json.dumps(result.to_dict()))
    assert result.status == "ok"
    assert result.offset_seconds == pytest.approx(34346, abs=2)
    assert not result.drift["detected"]
    assert len(result.chapters) == len(paths)
    assert result.chapters[1]["start"] == pytest.approx(result.chapters[0]["duration"])


def test_first_two_chapters(flight):
    track, _, paths = flight
    result = synchronize(track, read_video(paths[:2]))
    print("TWO CHAPTERS", result.offset_seconds, result.confidence)
    assert result.status == "ok"
    assert result.offset_seconds == pytest.approx(34346, abs=2)
    assert not result.drift["detected"]


@pytest.mark.parametrize("duration", [600, 180, 30])
def test_crop_sweep(flight, duration):
    track, telemetry, _ = flight
    accepted, declined, errors = 0, 0, []
    for start in range(300, 3901, 300):
        if start+duration > telemetry.time[-1]:
            continue
        result = synchronize(track, telemetry.slice(start, start+duration))
        assert result.status in {"ok", "ambiguous"}
        if result.status == "ok":
            accepted += 1
            error = result.offset_seconds-34346-start
            errors.append(abs(error))
            assert abs(error) <= 2
        else:
            declined += 1
            assert result.offset_seconds is None
    print("SWEEP", duration, "accepted", accepted, "declined", declined, "max_error", max(errors, default=None))


@pytest.mark.parametrize("start,duration", [(1200, 600), (1800, 180), (1800, 30),
                                                (2400, 600), (2400, 180), (2400, 30)])
def test_middle_clips(flight, start, duration):
    track, telemetry, _ = flight
    result = synchronize(track, telemetry.slice(start, start+duration))
    print("CLIP", start, duration, json.dumps(result.to_dict()))
    assert result.status in {"ok", "ambiguous"}
    if result.status == "ok":
        assert result.offset_seconds == pytest.approx(34346+start, abs=2)
    else:
        assert result.offset_seconds is None
