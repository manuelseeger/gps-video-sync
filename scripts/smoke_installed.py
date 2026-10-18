import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
import gps_video_sync
from gps_video_sync import Track, Telemetry, synchronize, read_video, match_videos


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


parser = argparse.ArgumentParser(description='Run with an installed wheel, outside the checkout: python -I /path/to/this.py.')
parser.add_argument("--verify", action="store_true")
parser.add_argument("--fixtures", type=Path, help="Repository real-media fixture directory")
args = parser.parse_args()

assert gps_video_sync.__version__ == version("gps-video-sync")
assert "site-packages" in str(Path(gps_video_sync.__file__).resolve())
for command in ("gps-video-sync",):
    run(command, "--help")
    for subcommand in ("sync", "info", "telemetry", "verify"):
        run(command, subcommand, "--help")
run(sys.executable, "-I", "-m", "gps_video_sync", "--help")

time = np.arange(120, dtype=float)
epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
track = Track(time + 3600, np.zeros(120), np.zeros(120), np.zeros(120),
              date=datetime(2026, 1, 1).date())
telemetry = Telemetry(time, {}, gps_time=time, gps_utc=epoch + 3600 + time)
result = synchronize(track, telemetry)
assert result.status == "ok" and abs(result.offset_seconds - 3600) < 0.01
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    igc = root / "synthetic.igc"
    igc.write_text("HFDTE010126\nB0100000000000N00000000EA0000000000\n"
                   "B0102000000000N00000000EA0000000000\n")
    path = root / "synthetic.npz"
    np.savez(path, time=time, gps_time=time, gps_utc=epoch + 3600 + time)
    output = json.loads(run("gps-video-sync", "sync", str(path), "--source", "npz",
                            "--track", str(igc)))
    assert output["schema_version"] == "2.0.0" and len(output["videos"]) == 1
    item = output["videos"][0]
    assert item["status"] == "ok" and abs(item["offset_seconds"] - 3600) < 0.01
    assert item["video"] == str(path) and item["input_file"] == str(path)
    assert read_video(path, source="npz").video == str(path)
    assert match_videos(track, [path], source="npz")[0].to_dict() == item
    if args.fixtures:
        fixtures = args.fixtures.resolve()
        media = json.loads(run(
            "gps-video-sync", "sync", str(fixtures / "chapter-01.mp4"),
            str(fixtures / "chapter-02.mp4"), "--track", str(fixtures / "flight.igc"),
            "--cache-dir", str(root / "cache")))
        assert media["schema_version"] == "2.0.0" and len(media["videos"]) == 2
        for item, expected in zip(media["videos"], [36160.112, 36460.112]):
            assert item["status"] == "ok" and item["method"] == "motion"
            assert abs(item["offset_seconds"] - expected) < 2
    if args.verify:
        import matplotlib
        from gps_video_sync.verify import plot_verification
        matplotlib.use("Agg")
        plot = root / "verification.png"
        plot_verification(track, telemetry, result.to_dict(), plot)
        assert plot.read_bytes().startswith(b"\x89PNG")
print(f"Installed API and CLI smoke tests passed: {gps_video_sync.__version__}")
