from datetime import date
import json

import pytest

from gps_video_sync import cli
from gps_video_sync.models import Accepted, Ambiguous, Failed, OwnGps, SyncResult, TimeMapping
from mp4_builder import build, video


def setup_batch(tmp_path, monkeypatch, outcomes=None):
    paths = [tmp_path / f"clip-{i}.mp4" for i in range(3)]
    data = build([video()], created=123)
    for path in paths:
        path.write_bytes(data)
    outcomes = outcomes or [Accepted(TimeMapping(100 + i, .1, 1.0001, OwnGps())) for i in range(3)]
    monkeypatch.setattr(cli, "read_track", lambda *a, **kw: None)
    def match(*args, **kwargs):
        for path, outcome in zip(paths, outcomes):
            assert all(p.read_bytes() == data for p in paths), "must finish matching before writing"
            yield SyncResult(outcome, date=date(2026, 1, 1), video=str(path), input_file=str(path))
    monkeypatch.setattr(cli, "match_videos", match)
    return paths, data


def run(paths, *options):
    return cli.main(["sync", *map(str, paths), "--track", "unused.igc", *options])


@pytest.mark.parametrize("options,state", [([], None), (["--in-place", "--dry-run"], "planned"),
                                           (["--in-place"], "written")])
def test_default_preview_and_apply(tmp_path, monkeypatch, capsys, options, state):
    paths, data = setup_batch(tmp_path, monkeypatch)
    assert run(paths, *options) == 0
    results = json.loads(capsys.readouterr().out)["videos"]
    assert [r["status"] for r in results] == ["ok"] * 3
    if state is None:
        assert all("in_place" not in r for r in results)
    else:
        assert [r["in_place"]["state"] for r in results] == [state] * 3
    assert all((p.read_bytes() == data) == (state != "written") for p in paths)
    assert [r["clockRate"] for r in results] == [1.0001] * 3


def test_skips_ambiguous_and_failed(tmp_path, monkeypatch, capsys):
    outcomes = [Accepted(TimeMapping(100, .1, 1, OwnGps())), Ambiguous("weak"), Failed("bad")]
    paths, data = setup_batch(tmp_path, monkeypatch, outcomes)
    assert run(paths, "--in-place") == 1
    results = json.loads(capsys.readouterr().out)["videos"]
    assert [r["in_place"]["state"] for r in results] == ["written", "skipped", "skipped"]
    assert [p.read_bytes() == data for p in paths] == [False, True, True]


def test_write_failure_does_not_stop_batch(tmp_path, monkeypatch, capsys):
    import gps_video_sync.in_place as writer
    paths, data = setup_batch(tmp_path, monkeypatch)
    replace = writer.os.replace
    def fail_one(source, target):
        if target == paths[0]:
            raise OSError("test replace failure")
        replace(source, target)
    monkeypatch.setattr(writer.os, "replace", fail_one)
    assert run(paths, "--in-place") == 1
    output = capsys.readouterr()
    results = json.loads(output.out)["videos"]
    assert [r["status"] for r in results] == ["ok"] * 3
    assert [r["in_place"]["state"] for r in results] == ["failed", "written", "written"]
    assert "test replace failure" in output.err
    assert paths[0].read_bytes() == data
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("options", [["--dry-run"], ["--in-place", "--source", "npz"],
                                      ["--in-place", "--source", "custom"]])
def test_invalid_options_rejected_before_matching(monkeypatch, capsys, options):
    monkeypatch.setattr(cli, "match_videos", lambda *a, **kw: pytest.fail("must reject before matching"))
    assert run(["missing.npz"], *options) == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["npz", "symlink", "hardlink", "json_alias"])
def test_indirect_inputs_and_output_alias_rejected(tmp_path, monkeypatch, capsys, kind):
    paths, data = setup_batch(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "read_track", lambda *a, **kw: pytest.fail("must validate input first"))
    options = ["--in-place"]
    path = paths[0]
    if kind == "npz":
        path = tmp_path / "telemetry.npz"
        path.write_bytes(b"not a video")
    elif kind == "symlink":
        path = tmp_path / "link.mp4"
        path.symlink_to(paths[0])
    elif kind == "hardlink":
        import os
        os.link(path, tmp_path / "shared.mp4")
    else:
        options.extend(["--json", str(path)])
    assert run([path], *options) == 1
    assert "error:" in capsys.readouterr().err
    assert all(p.read_bytes() == data for p in paths)


@pytest.mark.parametrize("kind", ["missing_parent", "directory", "read_only_input"])
def test_unwritable_paths_rejected_before_matching(tmp_path, monkeypatch, capsys, kind):
    paths, data = setup_batch(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "read_track", lambda *a, **kw: pytest.fail("must validate paths first"))
    options = ["--in-place"]
    if kind == "missing_parent":
        options.extend(["--json", str(tmp_path / "missing" / "result.json")])
    elif kind == "directory":
        options.extend(["--json", str(tmp_path)])
    else:
        paths[0].chmod(0o444)
    assert run(paths, *options) == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert captured.out == ""
    assert all(p.read_bytes() == data for p in paths)


def test_late_json_failure_keeps_write_report_on_stdout(tmp_path, monkeypatch, capsys):
    paths, data = setup_batch(tmp_path, monkeypatch)

    def fail_output(*args):
        raise OSError("output disk full")

    monkeypatch.setattr(cli, "write_json", fail_output)
    assert run(paths, "--in-place", "--json", str(tmp_path / "result.json")) == 1
    captured = capsys.readouterr()
    assert "output disk full" in captured.err
    results = json.loads(captured.out)["videos"]
    assert [r["in_place"]["state"] for r in results] == ["written"] * 3
    assert all(p.read_bytes() != data for p in paths)
