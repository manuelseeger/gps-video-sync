from datetime import date
import numpy as np
import pytest
from igc_video_sync.igc import IGCReader


def fix(clock, validity="A", pressure=500, gps=700):
    return f"B{clock}4549386N01146150E{validity}{pressure:05d}{gps:05d}"


def test_header_midnight_invalid_gaps(tmp_path):
    path = tmp_path / "track.igc"
    path.write_text("HFDTE161026\n" + "\n".join([
        fix("235957"), fix("235959", "V"), fix("000001", gps=710),
        fix("000003", gps=712), fix("000003"), fix("000002"), fix("000030", gps=720)]))
    track = IGCReader().read(path)
    assert track.date == date(2026, 10, 16)
    np.testing.assert_equal(track.time, [86397, 86401, 86403, 86430])
    assert track.altitude_source == "gps"
    np.testing.assert_equal(track.altitude, [700, 710, 712, 720])
    assert IGCReader().read(path, altitude="pressure").altitude_source == "pressure"


def test_pressure_auto_and_malformed(tmp_path):
    path = tmp_path / "track.igc"
    path.write_text("HFDTE DATE:161026\nBbad\n"+fix("120000", pressure=300)+"\n"+fix("120010", pressure=301))
    track = IGCReader().read(path)
    assert track.date == date(2026, 10, 16)
    assert track.altitude_source == "pressure"
    assert track.time[1]-track.time[0] == 10
    with pytest.raises(ValueError):
        IGCReader().read(path, altitude="bogus")


def test_no_valid_fixes(tmp_path):
    path = tmp_path / "bad.igc"
    path.write_text(fix("120000", "V"))
    with pytest.raises(ValueError, match="valid"):
        IGCReader().read(path)
