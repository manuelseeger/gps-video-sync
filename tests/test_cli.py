import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from gps_video_sync import cli
from gps_video_sync.models import SyncResult, Telemetry, Track
from gps_video_sync.verify import _recording_frame


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "telemetry.npz"
    np.savez(path, time=np.arange(8, dtype=float), gyro=np.ones((8, 3)),
             accel=np.ones((8, 3)))
    return path


def fake_matcher(monkeypatch, status="ok"):
    monkeypatch.setattr(cli, "read_track", lambda *a, **kw: Track(
        np.arange(8), np.zeros(8), np.zeros(8), np.arange(8)))
    def match(track, telemetry):
        return SyncResult(status=status, offset_seconds=3600, video=telemetry.video, message="test result")
    monkeypatch.setattr(cli, "synchronize", match)
    monkeypatch.setattr(cli, "match_videos", lambda track, paths, **kw: [
        match(track, cli.read_video(p, source="npz")) for p in paths])


def test_sync_json(sample, tmp_path, monkeypatch):
    fake_matcher(monkeypatch)
    out = tmp_path / "result.json"
    assert cli.main(["sync", str(sample), "--source", "npz", "--track", "unused.igc",
                     "--json", str(out)]) == 0
    result = json.loads(out.read_text())
    assert result["schema_version"] == "2.0.0"
    assert result["videos"][0]["offset_seconds"] == 3600
    assert "clockRate" in result["videos"][0]


def test_ambiguous_writes_result(sample, tmp_path, monkeypatch, capsys):
    fake_matcher(monkeypatch, "ambiguous")
    out = tmp_path / "ambiguous.json"
    assert cli.main(["sync", str(sample), "--source", "npz", "--track", "unused",
                     "--json", str(out)]) == 2
    assert json.loads(out.read_text())["videos"][0]["status"] == "ambiguous"
    assert "ambiguous" in capsys.readouterr().err


def test_npz_roundtrip(sample, tmp_path):
    out = tmp_path / "export.npz"
    assert cli.main(["telemetry", str(sample), "--source", "npz", "--out", str(out)]) == 0
    np.testing.assert_equal(cli.read_video(out, source="npz").channels["gyro"], np.ones((8, 3)))
    js = tmp_path / "export.json"
    assert cli.main(["telemetry", str(out), "--source", "npz", "--out", str(js)]) == 0
    assert len(json.loads(js.read_text())["time"]) == 8


def test_npz_metadata_roundtrip(tmp_path):
    path = tmp_path / "metadata.npz"
    telemetry = Telemetry(np.arange(3), {"gyro": np.zeros((3, 3))},
                          "recording.mp4", 3,
                          np.array([0., 2.]), np.array([1700000000., 1700000002.]))
    cli._export(telemetry, path)
    loaded = cli.read_video(path, source="npz")
    assert loaded.video == telemetry.video and loaded.duration == 3
    np.testing.assert_equal(loaded.gps_utc, telemetry.gps_utc)
    np.testing.assert_equal(loaded.gps_time, telemetry.gps_time)


def test_json_missing_telemetry_is_null(tmp_path):
    path = tmp_path / "missing.json"
    cli._export(Telemetry(np.arange(2.), {"GYRO": np.array([[1., 2., 3.], [np.nan]*3])}), path)
    assert json.loads(path.read_text())["channels"]["GYRO"][1] == [None, None, None]


def test_invalid_canonical_shape_is_clean(tmp_path, capsys):
    path = tmp_path / "bad-shape.npz"
    np.savez(path, time=np.arange(10.), GYRO=np.ones(10))
    assert cli.main(["info", str(path), "--source", "npz"]) == 1
    assert "shape" in capsys.readouterr().err


def test_module_subprocess(sample):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.run([sys.executable, "-m", "gps_video_sync", "info", str(sample),
                           "--source", "npz"], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["channels"]["gyro"] == [8, 3]


@pytest.mark.parametrize("args", [
    ["info", "missing.npz", "--source", "npz"],
    ["info", "missing", "--source", "not-registered"],
    ["info", "missing", "--hz", "0"],
])
def test_input_errors(args, capsys):
    assert cli.main(args) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback" not in err


def test_syntax_error_exit_one():
    proc = subprocess.run([sys.executable, "-m", "gps_video_sync", "sync", "video.mp4"],
                          capture_output=True, text=True)
    assert proc.returncode == 1
    assert "--track" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_invalid_npz(tmp_path, capsys):
    path = tmp_path / "bad.npz"
    np.savez(path, time=[0, 1], gyro=[1])
    assert cli.main(["info", str(path), "--source", "npz"]) == 1
    assert "match time" in capsys.readouterr().err



def test_verify_ambiguous_exit_code(sample, tmp_path, monkeypatch, capsys):
    fake_matcher(monkeypatch, "ambiguous")
    output = tmp_path / "plot.png"
    assert cli.main(["verify", str(sample), "--source", "npz", "--track", "unused",
                     "--output", str(output)]) == 2
    assert not output.exists()
    assert "ambiguous" in capsys.readouterr().err


def test_plot_from_result(sample, tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    fake_matcher(monkeypatch)
    result = tmp_path / "result.json"
    _ok_result(result, video=str(sample.resolve()))
    monkeypatch.setattr(cli, "synchronize", lambda *a: pytest.fail("must use saved result"))
    output = tmp_path / "plot.png"
    assert cli.main(["verify", str(sample), "--source", "npz", "--track", "unused",
                     "--result", str(result), "--output", str(output)]) == 0
    assert output.read_bytes().startswith(b"\x89PNG")



def test_sync_stdout_and_real_gps_matcher(tmp_path):
    from datetime import datetime, timezone
    track = tmp_path / "flight.igc"
    track.write_text("HFDTE010126\nB0820004549386N01146150EA0000000700\n"
                     "B0821004549387N01146151EA0000000701\n")
    path = tmp_path / "gps.npz"
    gt = np.arange(20.)
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    np.savez(path, time=gt, gps_time=gt, gps_utc=epoch+30010+gt)
    proc = subprocess.run([sys.executable, "-m", "gps_video_sync", "sync", str(path),
                           "--source", "npz", "--track", str(track)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["videos"][0]["method"] == "gps"
    assert result["videos"][0]["offset_seconds"] == 30010
    np.savez(path, time=gt, GYRO=np.zeros((20, 3)))
    ambiguous = subprocess.run([sys.executable, "-m", "gps_video_sync", "sync", str(path),
                                "--source", "npz", "--track", str(track)], capture_output=True, text=True)
    assert ambiguous.returncode == 2, ambiguous.stderr
    assert json.loads(ambiguous.stdout)["videos"][0]["offset_seconds"] is None


def test_missing_ffmpeg_is_clean_error(sample, tmp_path, monkeypatch, capsys):
    fake_matcher(monkeypatch)
    import gps_video_sync.verify as verify
    monkeypatch.setattr(verify, "plot_verification", lambda *a: (_ for _ in ()).throw(
        FileNotFoundError("ffmpeg not found")))
    assert cli.main(["verify", str(sample), "--source", "npz", "--track", "unused",
                     "--output", str(tmp_path / "out.png")]) == 1
    assert "ffmpeg not found" in capsys.readouterr().err


def _ok_result(path, **changes):
    result = SyncResult("ok", offset_seconds=3600).to_dict()
    result.update(changes)
    path.write_text(json.dumps(dict(schema_version="2.0.0", videos=[result])))
    return path


@pytest.mark.parametrize("changes,expected", [
    ({"clockRate": None, "drift": None}, 0),
    ({"clockRate": 1, "drift": None}, 0),
    ({"clockRate": None, "drift": {"clock_rate": None}}, 0),
    ({"clockRate": "fast"}, 1),
    ({"clockRate": True}, 1),
    ({"clockRate": 10**400}, 1),
    ({"offset_seconds": 10**400}, 1),
    ({"offset_seconds": {}}, 1),
    ({"drift": [], "clockRate": None}, 1),
])
def test_verify_saved_result_boundaries(sample, tmp_path, monkeypatch, capsys, changes, expected):
    pytest.importorskip("matplotlib")
    fake_matcher(monkeypatch)
    result = _ok_result(tmp_path / "result.json", video=str(sample.resolve()), **changes)
    code = cli.main(["verify", str(sample), "--source", "npz", "--track", "unused",
                     "--result", str(result), "--output", str(tmp_path / "plot.png")])
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert code == expected, err


def test_npy_passed_as_npz_is_clean_error(tmp_path):
    path = tmp_path / "time.npy"
    np.save(path, np.arange(5.))
    proc = subprocess.run([sys.executable, "-m", "gps_video_sync", "info", str(path), "--source", "npz"],
                          capture_output=True, text=True)
    assert proc.returncode == 1
    assert "not an NPZ archive" in proc.stderr and "Traceback" not in proc.stderr
    np.savez(tmp_path / "empty.npz", other=np.arange(3.))
    proc = subprocess.run([sys.executable, "-m", "gps_video_sync", "info", str(tmp_path / "empty.npz"),
                           "--source", "npz"], capture_output=True, text=True)
    assert proc.returncode == 1 and "needs a time array" in proc.stderr


@pytest.mark.parametrize("location", ["http://127.0.0.1:9/x.mp4", "file:///etc/hostname",
                                      "missing.mp4", "concat:a.mp4|b.mp4"])
def test_frame_paths_from_npz_must_be_local_files(tmp_path, monkeypatch, capsys, location):
    pytest.importorskip("matplotlib")
    import gps_video_sync.verify as verify
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verify.subprocess, "run", lambda *a, **k: pytest.fail("ffmpeg must not run"))
    path = tmp_path / "telemetry.npz"
    cli._export(Telemetry(np.arange(20.), {}, video=location, duration=20), path)
    track = tmp_path / "flight.igc"
    track.write_text("HFDTE010126\nB0100000000000N00000000EA0000000000\n"
                     "B0102000000000N00000000EA0000000000\n")
    result = _ok_result(tmp_path / "result.json", video=location)
    code = cli.main(["verify", str(path), "--source", "npz", "--track", str(track), "--result",
                     str(result), "--output", str(tmp_path / "plot.png"), "--frames", "1"])
    assert code == 1
    assert "local video file" in capsys.readouterr().err
