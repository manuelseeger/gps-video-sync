import numpy as np
import pytest

from mp4_builder import Track, build, video
from test_gopro import clock_packets
from gps_video_sync import read_video
from gps_video_sync import gopro, mp4


@pytest.mark.parametrize("version,created", [(0, 123456), (1, 2**33), (0, 0), (1, 0)])
def test_creation_time_and_camera_start_survive_cache(tmp_path, monkeypatch, version, created):
    path = tmp_path / "recording.mp4"
    path.write_bytes(build([video(deltas=[3000]), Track(deltas=[1000] * 3,
                           payloads=clock_packets())], version=version, created=created))
    assert mp4.read_timeline(path).created_seconds == (created or None)
    cold = read_video(path, cache_dir=tmp_path / "cache")
    if created:
        assert cold.camera_start.seconds == created + 5
        assert cold.camera_start.bound_seconds == 1.002
        assert cold.camera_start.camera == "camera"
    else:
        assert cold.camera_start is None
    from gps_video_sync.cli import _export
    archive = tmp_path / "telemetry.npz"
    _export(cold, archive)
    restored = read_video(archive, source="npz")
    assert restored.camera_start == cold.camera_start
    monkeypatch.setattr(gopro, "_recording", lambda *a: pytest.fail("cache did not retain creation time"))
    warm = read_video(path, cache_dir=tmp_path / "cache")
    assert warm.camera_start == cold.camera_start
    np.testing.assert_array_equal(warm.channels["ACCL"], cold.channels["ACCL"])
    cropped = warm.slice(1, 2)
    assert cropped.clock is None
    assert cropped.camera_start is None


def test_creation_without_recording_clock_is_not_camera_start(tmp_path):
    path = tmp_path / "recording.mp4"
    path.write_bytes(build([video(deltas=[3000]), Track(deltas=[1000] * 3,
                           payloads=clock_packets(defect="missing_stamp"))], created=123456))
    result = read_video(path, cache_dir=tmp_path / "cache")
    assert result.duration == 3
    assert result.camera_start is None
