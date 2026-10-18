from datetime import date, datetime, timezone
from itertools import permutations
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest

from gps_video_sync import read_video, synchronize
from gps_video_sync.cli import read_npz
from gps_video_sync.models import Track
from gps_video_sync.verify import _chapter_frame
from test_gopro import gps5, gps9, numeric, packet, record, stream, video

EPOCH = 1704110400.


def make_chapter(tmp_path, name, *, gps_start=None, stmp_start=None, duration=12.,
                 times=None, gps_errors=None, stmp_errors=None, gps_kind=5, marker=10,
                 sensor="CORI", malformed_stmp=False, extra_stmp=None):
    times = np.arange(0, duration) if times is None else np.asarray(times)
    payloads = []
    for i, t in enumerate(times):
        fields = [stream(numeric("ACCL", [[marker, 0, 0]]))]
        if gps_start is not None:
            utc = EPOCH + gps_start + t + (gps_errors[i] if gps_errors is not None else 0)
            if gps_kind == 5:
                stamp = datetime.fromtimestamp(utc, timezone.utc).strftime("%y%m%d%H%M%S.%f")
                fields.append(gps5(stamp, count=1))
            else:
                elapsed = utc - 946684800
                days, seconds = divmod(elapsed, 86400)
                fields.append(gps9([[450000000, 70000000, 1200000, 2000, 3000,
                                     int(days), round(seconds * 1000), 150, 3]]))
        if stmp_start is not None:
            stamp = stmp_start + t + (stmp_errors[i] if stmp_errors is not None else 0)
            stmp = (record("STMP", "c", 1, 1, b"x") if malformed_stmp
                    else numeric("STMP", [round(stamp * 1e6)], "J"))
            values = [[1, 0, 0, 0]] if sensor in ("CORI", "IORI") else [[0, 0, 1]]
            fields.append(stream(stmp, numeric(sensor, values)))
        if extra_stmp is not None:
            fields.append(stream(numeric("STMP", [round((extra_stmp + t) * 1e6)], "J"),
                                 numeric("GRAV", [[0, 0, 1]])))
        payloads.append(packet(*fields))
    intervals = list(np.diff(times)) + [duration - float(times[-1])] if len(times) else []
    return video(tmp_path, name, payloads, duration=duration, pts=times, intervals=intervals)


def run_cli(command, paths, cache, *options):
    proc = subprocess.run([sys.executable, "-m", "gps_video_sync", command, *map(str, paths),
                           "--cache-dir", str(cache), *map(str, options)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout else None


def assert_failure(paths, tmp_path, message):
    errors = []
    for order in permutations(paths):
        with pytest.raises(ValueError, match=message) as exc:
            read_video(order, cache_dir=tmp_path / "cache")
        assert str(exc.value).startswith("Cannot establish chapter order: ")
        errors.append(str(exc.value))
    assert len(set(errors)) == 1


@pytest.mark.parametrize("gps_kind", [5, 9])
def test_gps_chapter_permutations(tmp_path, gps_kind):
    paths = [make_chapter(tmp_path, name, gps_start=start, gps_kind=gps_kind, duration=4, marker=value)
             for name, start, value in [("z.mp4", 0, 10), ("a.mp4", 4, 20), ("m.mp4", 8, 30)]]
    track = Track(np.arange(43200, 43213), np.zeros(13), np.zeros(13), np.zeros(13), date(2024, 1, 1))
    for index, order in enumerate(permutations(paths)):
        result = read_video(order, hz=1, cache_dir=tmp_path / f"cache-{index}")
        assert [c["file"] for c in result.chapters] == list(map(str, paths))
        assert [c["start"] for c in result.chapters] == [0, 4, 8]
        assert [c["duration"] for c in result.chapters] == [4, 4, 4]
        assert [c["utc_start"] for c in result.chapters] == [EPOCH, EPOCH + 4, EPOCH + 8]
        np.testing.assert_array_equal(result.time, np.arange(12))
        np.testing.assert_array_equal(result.channels["ACCL"][:, 0], [10] * 4 + [20] * 4 + [30] * 4)
        np.testing.assert_array_equal(result.gps_time, np.arange(12))
        np.testing.assert_array_equal(result.gps_utc - EPOCH, np.arange(12))
        alignment = synchronize(track, result)
        assert alignment.status == "ok"
        assert alignment.method == "gps"
        assert alignment.offset_seconds == pytest.approx(43200, abs=1e-6)


def test_three_chapter_cli_cold_and_warm_permutations(tmp_path):
    paths = [make_chapter(tmp_path, name, gps_start=start, duration=4, marker=value)
             for name, start, value in [("z.mp4", 0, 10), ("a.mp4", 4, 20), ("m.mp4", 8, 30)]]
    track = tmp_path / "flight.igc"
    track.write_text("HFDTE010124\nB1200004500000N00700000EA0120001200\n"
                     "B1200204500000N00700000EA0120001200\n")
    for index, order in enumerate(permutations(paths)):
        cache = tmp_path / f"cold-{index}"
        for warm in (False, True):
            if warm:
                cache = tmp_path / "shared"
            info = run_cli("info", order, cache, "--hz", "1")
            assert [c["file"] for c in info["chapters"]] == list(map(str, paths))
            assert [c["start"] for c in info["chapters"]] == [0, 4, 8]
            assert info["gps_samples"] == 12
            assert info["samples"] == 12
            result = run_cli("sync", order, cache, "--track", track, "--hz", "1")
            assert result["status"] == "ok"
            assert result["offset_seconds"] == pytest.approx(43200, abs=1e-6)
            assert [c["file"] for c in result["chapters"]] == list(map(str, paths))
            output = tmp_path / "export.npz"
            run_cli("telemetry", order, cache, "--out", output, "--hz", "1")
            telemetry = read_npz(output)
            assert telemetry.chapters == info["chapters"]
            np.testing.assert_array_equal(telemetry.channels["ACCL"][:, 0], [10] * 4 + [20] * 4 + [30] * 4)
            np.testing.assert_array_equal(telemetry.gps_time, np.arange(12))
            np.testing.assert_array_equal(telemetry.gps_utc - EPOCH, np.arange(12))
    output = tmp_path / "export.json"
    run_cli("telemetry", paths[::-1], tmp_path / "shared", "--out", output, "--hz", "1")
    exported = json.loads(output.read_text())
    assert exported["chapters"] == telemetry.chapters
    assert exported["channels"]["ACCL"] == [[10, 0, 0]] * 4 + [[20, 0, 0]] * 4 + [[30, 0, 0]] * 4
    assert exported["gps_utc"] == list(EPOCH + np.arange(12))
    assert len(list((tmp_path / "shared").glob("*.npz"))) == 1


def test_filename_and_mtime_do_not_establish_order(tmp_path):
    first = make_chapter(tmp_path, "aaa.mp4", gps_start=0)
    second = make_chapter(tmp_path, "zzz.mp4", gps_start=12)
    renamed_first = tmp_path / "zzz-renamed.mp4"
    renamed_second = tmp_path / "aaa-renamed.mp4"
    first.rename(renamed_first)
    second.rename(renamed_second)
    os.utime(renamed_first, (2000000000, 2000000000))
    os.utime(renamed_second, (1000000000, 1000000000))
    result = read_video([renamed_second, renamed_first], cache_dir=tmp_path / "cache")
    assert [c["file"] for c in result.chapters] == [str(renamed_first), str(renamed_second)]
    assert [c["start"] for c in result.chapters] == [0, 12]
    np.testing.assert_array_equal(result.gps_utc - EPOCH, np.arange(24))


@pytest.mark.parametrize("kind", ["absent", "single", "short-span", "sparse", "step", "reset", "outlier", "rate"])
def test_unusable_gps_diagnostics(tmp_path, kind):
    times, errors, start, duration = None, None, 0, 12
    if kind == "absent":
        start = None
    elif kind == "single":
        times = [0]
    elif kind == "short-span":
        times = [0, .01]
    elif kind == "sparse":
        times, duration = [0, 1], 300
    elif kind == "step":
        errors = [0] * 6 + [.3] * 6
    elif kind == "reset":
        errors = [0] * 6 + [-6] * 6
    elif kind == "outlier":
        errors = [0] * 11 + [1]
    elif kind == "rate":
        errors = list(np.arange(12) * .02)
    first = make_chapter(tmp_path, "a.mp4", gps_start=start, times=times, gps_errors=errors, duration=duration)
    second = make_chapter(tmp_path, "b.mp4", gps_start=duration)
    assert_failure([first, second], tmp_path, "no common usable recording clock")


@pytest.mark.parametrize("kind", ["absent", "two", "short-span", "sparse-boundary", "sparse-gap", "spread", "reset", "malformed"])
def test_unusable_stmp_diagnostics(tmp_path, kind):
    times, errors, start, malformed = None, None, 100, False
    if kind == "absent":
        start = None
    elif kind == "two":
        times = [0, 1]
    elif kind == "short-span":
        times = [0, .01, .02]
    elif kind == "sparse-boundary":
        times = [0, 1, 2]
    elif kind == "sparse-gap":
        times = [0, 1, 11]
    elif kind == "spread":
        errors = [0] * 6 + [.021] * 6
    elif kind == "reset":
        errors = [0] * 6 + [-6] * 6
    elif kind == "malformed":
        malformed = True
    first = make_chapter(tmp_path, "a.mp4", stmp_start=start, times=times,
                         stmp_errors=errors, malformed_stmp=malformed)
    second = make_chapter(tmp_path, "b.mp4", stmp_start=112)
    assert_failure([first, second], tmp_path, "no common usable recording clock")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -100.])
def test_nonfinite_or_negative_stmp_is_not_ordering_evidence(tmp_path, bad):
    first = video(tmp_path, "a.mp4", [packet(stream(numeric("STMP", [bad + i * 1e6], "d"),
                  numeric("CORI", [[1, 0, 0, 0]]))) for i in range(3)], duration=3)
    second = make_chapter(tmp_path, "b.mp4", stmp_start=103, duration=3)
    assert_failure([first, second], tmp_path, "nonfinite|negative")


@pytest.mark.parametrize("clock,spread,accepted", [
    ("gps", .099, True), ("gps", .101, False),
    ("stmp", .019, True), ("stmp", .021, False),
])
def test_clock_scatter_bounds(tmp_path, clock, spread, accepted):
    key = "gps_start" if clock == "gps" else "stmp_start"
    error_key = "gps_errors" if clock == "gps" else "stmp_errors"
    errors = [0, spread] * 6
    first = make_chapter(tmp_path, "a.mp4", **{key: 100, error_key: errors})
    second = make_chapter(tmp_path, "b.mp4", **{key: 112, error_key: errors})
    if accepted:
        telemetry = read_video([second, first], cache_dir=tmp_path / "cache")
        assert [c["file"] for c in telemetry.chapters] == [str(first), str(second)]
        assert [c["start"] for c in telemetry.chapters] == [0, 12]
    else:
        assert_failure([first, second], tmp_path, "offset spread exceeds")


def test_gps_rate_bound_checks_all_observations(tmp_path):
    first = make_chapter(tmp_path, "a.mp4", gps_start=0, gps_errors=[0] * 6 + [.09] * 6)
    second = make_chapter(tmp_path, "b.mp4", gps_start=12)
    assert_failure([first, second], tmp_path, "clock rate is inconsistent")


def test_single_chapter_retains_samples_without_usable_clocks(tmp_path):
    path = make_chapter(tmp_path, "single.mp4", gps_start=0, gps_errors=[0] * 6 + [.3] * 6,
                        stmp_start=100, malformed_stmp=True)
    telemetry = read_video([path], hz=1, cache_dir=tmp_path / "cache")
    assert telemetry.chapters[0]["utc_start"] is None
    assert telemetry.chapters[0]["stmp_start"] is None
    np.testing.assert_array_equal(telemetry.channels["ACCL"], [[10, 0, 0]] * 12)
    np.testing.assert_allclose(telemetry.gps_utc - EPOCH, [0, 1, 2, 3, 4, 5, 6.3, 7.3, 8.3, 9.3, 10.3, 11.3])


@pytest.mark.parametrize("clock,delta,diagnostic", [
    ("gps", 2, "GPS gap"), ("gps", -2, "GPS overlap"),
    ("stmp", 2, "STMP gap"), ("stmp", -2, "STMP overlap"),
    ("stmp", -12, "STMP overlap"), ("stmp", -100, "STMP gap"),
])
def test_noncontinuous_recordings(tmp_path, clock, delta, diagnostic):
    key = "gps_start" if clock == "gps" else "stmp_start"
    first = make_chapter(tmp_path, "a.mp4", **{key: 100})
    second = make_chapter(tmp_path, "b.mp4", **{key: 112 + delta})
    assert_failure([first, second], tmp_path, diagnostic)


@pytest.mark.parametrize("clock,delta,accepted", [
    ("gps", .399, True), ("gps", .401, False), ("gps", -.399, True), ("gps", -.401, False),
    ("stmp", .099, True), ("stmp", .101, False), ("stmp", -.099, True), ("stmp", -.101, False),
])
def test_junction_tolerances_include_clock_reach(tmp_path, clock, delta, accepted):
    key = "gps_start" if clock == "gps" else "stmp_start"
    first = make_chapter(tmp_path, "a.mp4", duration=3, times=[0, 1, 2.999], **{key: 100})
    second = make_chapter(tmp_path, "b.mp4", duration=3, times=[0, 1, 2.999], **{key: 103 + delta})
    if accepted:
        telemetry = read_video([second, first], cache_dir=tmp_path / "cache")
        assert [c["file"] for c in telemetry.chapters] == [str(first), str(second)]
        assert [c["start"] for c in telemetry.chapters] == [0, 3]
    else:
        assert_failure([first, second], tmp_path, "boundary|gap|overlap")


@pytest.mark.parametrize("clock,duration", [("gps", 1), ("stmp", .2)])
def test_too_short_to_prove_unique_tiling(tmp_path, clock, duration):
    key = "gps_start" if clock == "gps" else "stmp_start"
    first = make_chapter(tmp_path, "a.mp4", duration=duration, times=[0, duration / 3, 2 * duration / 3], **{key: 100})
    second = make_chapter(tmp_path, "b.mp4", duration=duration, times=[0, duration / 3, 2 * duration / 3], **{key: 100 + duration})
    assert_failure([first, second], tmp_path, "durations must exceed")


@pytest.mark.parametrize("second_stmp,diagnostic", [(100, "clocks disagree"), (114, "STMP gap")])
def test_both_usable_clocks_must_agree(tmp_path, second_stmp, diagnostic):
    first = make_chapter(tmp_path, "a.mp4", gps_start=0, stmp_start=112 if second_stmp == 100 else 100)
    second = make_chapter(tmp_path, "b.mp4", gps_start=12, stmp_start=second_stmp)
    assert_failure([first, second], tmp_path, diagnostic)


def test_both_clocks_agree_and_partial_clock_does_not_mix_domains(tmp_path):
    first = make_chapter(tmp_path, "a.mp4", gps_start=0, stmp_start=100)
    second = make_chapter(tmp_path, "b.mp4", gps_start=12, stmp_start=112)
    telemetry = read_video([second, first], cache_dir=tmp_path / "cache")
    assert [c["stmp_start"] for c in telemetry.chapters] == [100, 112]
    assert [c["utc_start"] for c in telemetry.chapters] == [EPOCH, EPOCH + 12]
    third = make_chapter(tmp_path, "c.mp4", stmp_start=124)
    result = read_video([third, second, first], cache_dir=tmp_path / "cache")
    assert [c["start"] for c in result.chapters] == [0, 12, 24]
    gps_only = make_chapter(tmp_path, "gps.mp4", gps_start=0)
    assert_failure([gps_only, third], tmp_path, "no common usable recording clock")


@pytest.mark.parametrize("clock,last_start", [("gps", 1000), ("gps", -24), ("stmp", 1000)])
def test_partial_clock_contradictions_reject_all_permutations_and_cli(tmp_path, clock, last_start):
    if clock == "gps":
        paths = [make_chapter(tmp_path, "z.mp4", gps_start=0, stmp_start=100),
                 make_chapter(tmp_path, "a.mp4", stmp_start=112),
                 make_chapter(tmp_path, "m.mp4", gps_start=last_start, stmp_start=124)]
    else:
        paths = [make_chapter(tmp_path, "z.mp4", gps_start=0, stmp_start=100),
                 make_chapter(tmp_path, "a.mp4", gps_start=12, stmp_start=112, malformed_stmp=True),
                 make_chapter(tmp_path, "m.mp4", gps_start=24, stmp_start=last_start)]
    diagnostic = f"{clock.upper()} contradicts resolved chapter timeline"
    assert_failure(paths, tmp_path, diagnostic)
    errors = []
    for order in permutations(paths):
        proc = subprocess.run([sys.executable, "-m", "gps_video_sync", "info", *map(str, order),
                               "--cache-dir", str(tmp_path / "cli-cache")], capture_output=True, text=True)
        assert proc.returncode == 1
        assert "error: Cannot establish chapter order: " + diagnostic in proc.stderr
        assert "Traceback" not in proc.stderr
        errors.append(proc.stderr)
    assert len(set(errors)) == 1


@pytest.mark.parametrize("clock,delta,accepted", [
    ("gps", 0, True), ("gps", .55, True), ("gps", .62, True), ("gps", .63, False),
    ("stmp", 0, True), ("stmp", .11, True), ("stmp", .12, True), ("stmp", .13, False),
])
def test_partial_clock_consistency_does_not_require_corroboration(tmp_path, clock, delta, accepted):
    if clock == "gps":
        paths = [make_chapter(tmp_path, "z.mp4", gps_start=0, stmp_start=100),
                 make_chapter(tmp_path, "a.mp4", stmp_start=112),
                 make_chapter(tmp_path, "m.mp4", gps_start=24 + delta, stmp_start=124)]
    else:
        paths = [make_chapter(tmp_path, "z.mp4", gps_start=0, stmp_start=100,
                              stmp_errors=[0, .018] * 6, extra_stmp=900),
                 make_chapter(tmp_path, "a.mp4", gps_start=12, stmp_start=112,
                              malformed_stmp=True, extra_stmp=1800),
                 make_chapter(tmp_path, "m.mp4", gps_start=24, stmp_start=124 + delta,
                              stmp_errors=[0, .018] * 6, extra_stmp=2700)]
    if not accepted:
        assert_failure(paths, tmp_path, f"{clock.upper()} contradicts resolved chapter timeline")
        return
    for index, order in enumerate(permutations(paths)):
        result = read_video(order, hz=1, cache_dir=tmp_path / f"cache-{index}")
        assert [c["file"] for c in result.chapters] == list(map(str, paths))
        assert [c["start"] for c in result.chapters] == [0, 12, 24]
        np.testing.assert_array_equal(result.channels["ACCL"][:, 0], [10] * 36)


def test_partial_clock_origins_must_share_one_consistent_range(tmp_path):
    paths = [make_chapter(tmp_path, "z.mp4", gps_start=0, stmp_start=100),
             make_chapter(tmp_path, "a.mp4", gps_start=12, stmp_start=112.08),
             make_chapter(tmp_path, "m.mp4", gps_start=24, stmp_start=124.16),
             make_chapter(tmp_path, "b.mp4", gps_start=36, stmp_start=136, malformed_stmp=True)]
    assert_failure(paths, tmp_path, "STMP contradicts resolved chapter timeline")


@pytest.mark.parametrize("clock", ["gps", "stmp"])
def test_partial_clocks_cannot_fill_an_unresolved_chapter(tmp_path, clock):
    paths = [make_chapter(tmp_path, "z.mp4", gps_start=0, stmp_start=100),
             make_chapter(tmp_path, "a.mp4", gps_start=12 if clock == "gps" else None,
                          stmp_start=112 if clock == "stmp" else None),
             make_chapter(tmp_path, "m.mp4", gps_start=24 if clock == "stmp" else None,
                          stmp_start=124 if clock == "gps" else None)]
    assert_failure(paths, tmp_path, "no common usable recording clock")


def test_three_no_gps_stmp_chapters_api_and_cli_permutations(tmp_path):
    paths = [make_chapter(tmp_path, name, stmp_start=start, duration=4, marker=value)
             for name, start, value in [("z.mp4", 100, 10), ("a.mp4", 104, 20), ("m.mp4", 108, 30)]]
    track = Track(np.arange(43200, 43213), np.zeros(13), np.zeros(13), np.zeros(13), date(2024, 1, 1))
    track_file = tmp_path / "flight.igc"
    track_file.write_text("HFDTE010124\nB1200004500000N00700000EA0120001200\n"
                          "B1200124500000N00700000EA0120001200\n")
    for index, order in enumerate(permutations(paths)):
        result = read_video(order, hz=1, cache_dir=tmp_path / f"api-{index}")
        assert [c["file"] for c in result.chapters] == list(map(str, paths))
        assert [c["start"] for c in result.chapters] == [0, 4, 8]
        assert [c["stmp_start"] for c in result.chapters] == [100, 104, 108]
        assert [c["utc_start"] for c in result.chapters] == [None, None, None]
        np.testing.assert_array_equal(result.time, np.arange(12))
        np.testing.assert_array_equal(result.channels["ACCL"], [[10, 0, 0]] * 4 + [[20, 0, 0]] * 4 + [[30, 0, 0]] * 4)
        alignment = synchronize(track, result)
        assert alignment.status == "ambiguous"
        assert alignment.method == "motion"
        assert alignment.offset_seconds is None
        assert alignment.message == "Insufficient varying, overlapping motion (minimum 90 s)"
        cache = tmp_path / "cli-shared"
        info = run_cli("info", order, cache, "--hz", "1")
        assert [c["file"] for c in info["chapters"]] == [
            str(tmp_path / "z.mp4"), str(tmp_path / "a.mp4"), str(tmp_path / "m.mp4")]
        assert [c["start"] for c in info["chapters"]] == [0, 4, 8]
        assert info["chapters"] == result.chapters
        assert info["gps_samples"] == 0
        assert info["samples"] == 12
        proc = subprocess.run([sys.executable, "-m", "gps_video_sync", "sync", *map(str, order),
                               "--track", str(track_file), "--hz", "1", "--cache-dir", str(cache)],
                              capture_output=True, text=True)
        assert proc.returncode == 2
        aligned = json.loads(proc.stdout)
        assert aligned["status"] == "ambiguous"
        assert aligned["method"] == "motion"
        assert aligned["offset_seconds"] is None
        assert aligned["message"] == "Insufficient varying, overlapping motion (minimum 90 s)"
        assert aligned["chapters"] == result.chapters
        assert proc.stderr == "ambiguous: Insufficient varying, overlapping motion (minimum 90 s)\n"
        output = tmp_path / "no-gps.npz"
        run_cli("telemetry", order, cache, "--out", output, "--hz", "1")
        exported = read_npz(output)
        assert exported.chapters == result.chapters
        np.testing.assert_array_equal(exported.time, np.arange(12))
        np.testing.assert_array_equal(exported.channels["ACCL"], [[10, 0, 0]] * 4 + [[20, 0, 0]] * 4 + [[30, 0, 0]] * 4)
    assert len(list((tmp_path / "cli-shared").glob("*.npz"))) == 1


def test_one_common_stmp_domain_and_stationary_chapters(tmp_path):
    first = make_chapter(tmp_path, "a.mp4", stmp_start=100, extra_stmp=900)
    second = make_chapter(tmp_path, "b.mp4", stmp_start=112, extra_stmp=1800)
    result = read_video([second, first], hz=1, cache_dir=tmp_path / "cache")
    assert [c["stmp_start"] for c in result.chapters] == [100, 112]
    np.testing.assert_array_equal(result.channels["ACCL"], [[10, 0, 0]] * 24)
    different_stream = make_chapter(tmp_path, "c.mp4", stmp_start=124, sensor="IORI")
    assert_failure([first, different_stream], tmp_path, "no common STMP sensor stream")


@pytest.mark.parametrize("alias_kind", ["same", "symlink", "hardlink"])
def test_duplicate_paths_before_warm_cache(tmp_path, alias_kind):
    first = make_chapter(tmp_path, "a.mp4", gps_start=0)
    read_video([first], cache_dir=tmp_path / "cache")
    alias = first
    if alias_kind != "same":
        alias = tmp_path / "alias.mp4"
        if alias_kind == "symlink":
            alias.symlink_to(first)
        else:
            os.link(first, alias)
    assert_failure([first, alias], tmp_path, "duplicate input file or alias")


@pytest.mark.parametrize("clock", ["gps", "stmp", "none"])
def test_copied_files(tmp_path, clock):
    first = make_chapter(tmp_path, "a.mp4", gps_start=0 if clock == "gps" else None,
                         stmp_start=100 if clock == "stmp" else None)
    copy = tmp_path / "copy.mp4"
    shutil.copyfile(first, copy)
    assert_failure([first, copy], tmp_path, "overlap.*duplicate or simultaneous" if clock != "none"
                   else "no common usable recording clock")


def test_reader_frames_use_resolved_paths_and_half_open_bounds(tmp_path):
    first = make_chapter(tmp_path, "z.mp4", gps_start=0, duration=3.125)
    second = make_chapter(tmp_path, "a.mp4", gps_start=3.125, duration=3.875)
    result = read_video([second, first], hz=1, cache_dir=tmp_path / "cache")
    assert _chapter_frame(result, [second, first], 3) == (first, 3)
    assert _chapter_frame(result, [second, first], 3.125) == (second, 0)
    assert _chapter_frame(result, [second, first], 3.25) == (second, .125)
    with pytest.raises(ValueError, match="outside"):
        _chapter_frame(result, [second, first], 7)


def test_cli_no_evidence_is_explicit_and_permutation_invariant(tmp_path):
    paths = [make_chapter(tmp_path, name) for name in ("a.mp4", "b.mp4")]
    errors = []
    for order in permutations(paths):
        proc = subprocess.run([sys.executable, "-m", "gps_video_sync", "info", *map(str, order),
                               "--cache-dir", str(tmp_path / "cache")], capture_output=True, text=True)
        assert proc.returncode == 1
        assert "error: Cannot establish chapter order: no common usable recording clock" in proc.stderr
        assert "Traceback" not in proc.stderr
        errors.append(proc.stderr)
    assert errors[0] == errors[1]


@pytest.mark.parametrize("last_start,accepted", [(124, True), (1000, False)])
def test_absent_stmp_stream_preserves_partial_clock_checks(tmp_path, last_start, accepted):
    paths = [make_chapter(tmp_path, "a.mp4", gps_start=0, stmp_start=100),
             make_chapter(tmp_path, "b.mp4", gps_start=12),
             make_chapter(tmp_path, "c.mp4", gps_start=24, stmp_start=last_start)]
    if not accepted:
        assert_failure(paths, tmp_path, "STMP contradicts resolved chapter timeline")
        return
    for order in permutations(paths):
        result = read_video(order, cache_dir=tmp_path / "cache")
        assert [c["file"] for c in result.chapters] == list(map(str, paths))
        assert [c["start"] for c in result.chapters] == [0, 12, 24]
        assert [c["stmp_start"] for c in result.chapters] == [100, None, 124]


def test_stmp_boundary_reach_covers_unobserved_clock_rate(tmp_path):
    first = make_chapter(tmp_path, "a.mp4", duration=1.2, times=[0, .05, .1],
                         stmp_start=100, stmp_errors=[0, .01, .02])
    second = make_chapter(tmp_path, "b.mp4", stmp_start=101.3)
    assert_failure([first, second], tmp_path, "STMP uncertain boundary")


def test_malformed_chapter_diagnostic_is_permutation_invariant(tmp_path):
    first = video(tmp_path, "a.mp4", [packet(stream(numeric("ACCL", [[1, 2]])))])
    second = video(tmp_path, "b.mp4", [packet(stream(numeric("GYRO", [[1, 2]])))])
    for order in permutations([first, second]):
        with pytest.raises(ValueError, match="^ACCL requires 3 columns$"):
            read_video(order, cache_dir=tmp_path / "cache")
