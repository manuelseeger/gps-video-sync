"""Check native STMP chapter order through the API, CLI, exports, and frames."""
from itertools import permutations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

import numpy as np

from gps_video_sync import read_track, read_video, synchronize
from gps_video_sync.cli import read_npz
from gps_video_sync.verify import _chapter_frame

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def cli(command, paths, cache, *options):
    result = subprocess.run([sys.executable, "-m", "gps_video_sync", command, *map(str, paths),
                             "--cache-dir", str(cache), *map(str, options)],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(result.stdout) if result.stdout else None


def equal_arrays(actual, expected):
    np.testing.assert_array_equal(actual.time, expected.time)
    np.testing.assert_array_equal(actual.gps_time, expected.gps_time)
    np.testing.assert_array_equal(actual.gps_utc, expected.gps_utc)
    assert actual.channels.keys() == expected.channels.keys()
    for key in expected.channels:
        np.testing.assert_array_equal(actual.channels[key], expected.channels[key])


def main():
    track = read_track(FIXTURES / "flight.igc")
    paths = [FIXTURES / "chapter-01.mp4", FIXTURES / "chapter-02.mp4"]
    with TemporaryDirectory(prefix="chapter-order-") as directory:
        work = Path(directory)
        expected = read_video(paths, cache_dir=work / "baseline")
        assert len(expected.gps_time) == 0
        assert [c["start"] for c in expected.chapters] == [0, 8999991 / 30000]
        np.testing.assert_allclose([c["stmp_start"] for c in expected.chapters], [1814.151224, 2114.151224], atol=1e-6, rtol=0)
        baseline = synchronize(track, expected)
        assert baseline.status == "ok"
        assert abs(baseline.offset_seconds - 36160.112) <= 2
        renamed = [work / "z.mp4", work / "a.mp4"]
        for index, (source, target) in enumerate(zip(paths, renamed)):
            shutil.copyfile(source, target)
            stamp = 2000000000 - index * 1000000000
            os.utime(target, (stamp, stamp))
        runs = 0
        for label, files in (("native", paths), ("renamed", renamed)):
            for index, order in enumerate(permutations(files)):
                for mode in ("cold", "shared"):
                    cache = work / f"{label}-cli-{index if mode == 'cold' else 'shared'}"
                    result = cli("sync", order, cache, "--track", FIXTURES / "flight.igc")
                    assert result["status"] == "ok"
                    assert abs(result["offset_seconds"] - 36160.112) <= 2
                    assert result["offset_seconds"] == baseline.offset_seconds
                    info = cli("info", order, cache)
                    assert [c["file"] for c in info["chapters"]] == list(map(str, files))
                    api_cache = work / f"{label}-api-{index if mode == 'cold' else 'shared'}"
                    api = read_video(order, cache_dir=api_cache)
                    assert api.chapters == info["chapters"]
                    equal_arrays(api, expected)
                    assert synchronize(track, api).offset_seconds == baseline.offset_seconds
                    output = work / "telemetry.npz"
                    cli("telemetry", order, cache, "--out", output)
                    exported = read_npz(output)
                    assert exported.chapters == api.chapters
                    equal_arrays(exported, expected)
                    output = work / "telemetry.json"
                    cli("telemetry", order, cache, "--out", output)
                    document = json.loads(output.read_text())
                    assert document["chapters"] == api.chapters
                    assert document["time"] == api.time.tolist()
                    assert document["gps_time"] == []
                    assert document["gps_utc"] == []
                    for key in api.channels:
                        values = np.where(np.isfinite(api.channels[key]), api.channels[key], None).tolist()
                        assert document["channels"][key] == values
                    runs += 1
            assert len(list((work / f"{label}-cli-shared").glob("*.npz"))) == 1
        reverse = renamed[::-1]
        telemetry = read_video(reverse, cache_dir=work / "renamed-api-shared")
        boundary = 8999991 / 30000
        for seconds, path, local in ((boundary - 1, renamed[0], boundary - 1),
                                     (boundary, renamed[1], 0), (boundary + 1, renamed[1], 1)):
            assert _chapter_frame(telemetry, reverse, seconds) == (path, local)
        result_file = work / "result.json"
        saved = baseline.to_dict()
        saved["chapters"] = [{"file": "missing.mp4", "start": 0, "duration": 1000}]
        result_file.write_text(json.dumps(saved))
        plot = work / "alignment.png"
        seconds = [boundary - 1, boundary, boundary + 1]
        cli("verify", reverse, work / "renamed-cli-shared", "--track", FIXTURES / "flight.igc",
            "--result", result_file, "--output", plot, "--frames", *seconds)
        assert plot.read_bytes().startswith(b"\x89PNG")
        for index, (time, path, local) in enumerate(zip(seconds, [renamed[0], renamed[1], renamed[1]],
                                                       [boundary - 1, 0, 1])):
            direct = work / "direct.png"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{local:.6f}",
                            "-protocol_whitelist", "file", "-i", f"file:{path}",
                            "-frames:v", "1", str(direct)], capture_output=True, check=True)
            frame = work / f"alignment-frame-{index:03d}-{time:.6f}s.png"
            assert frame.read_bytes() == direct.read_bytes()
        print(f"chapter order verified: {runs} API/CLI cases, NPZ/JSON exports, plot, and 3 boundary frames")


if __name__ == "__main__":
    main()
