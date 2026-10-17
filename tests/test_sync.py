from datetime import date
import numpy as np
import pytest
from scipy.ndimage import gaussian_filter1d as gf
from igc_video_sync.models import Track, Telemetry
from igc_video_sync.sync import correlation_curve, match_features, estimate_drift, synchronize


def signals(n=5000):
    rng = np.random.default_rng(21)
    return gf(rng.normal(size=n), 3), gf(rng.normal(size=n), 1)


def test_correlation_sign_and_entire_track_search():
    a, v = signals()
    utc, t = np.arange(5000)+70000., np.arange(600.)
    result = match_features(utc, t, {"yaw": a, "vertical": v},
                            {"yaw": -a[3200:3800], "vertical": v[3200:3800]})
    assert result.status == "ok"
    assert result.offset_seconds == pytest.approx(73200, abs=.15)
    assert result.confidence["margin_ratio"] > 2


def test_masked_correlation_matches_direct():
    rng = np.random.default_rng(5)
    a, b = rng.normal(size=100), rng.normal(size=50)
    a[10:15], b[20] = np.nan, np.nan
    lags, r, _ = correlation_curve(a, b, min_samples=20)
    for lag, score in zip(lags, r):
        if not np.isfinite(score):
            continue
        i = np.arange(len(b))
        valid = (i+lag >= 0) & (i+lag < len(a))
        x, y = a[(i+lag)[valid]], b[i[valid]]
        good = np.isfinite(x) & np.isfinite(y)
        assert score == pytest.approx(np.corrcoef(x[good], y[good])[0, 1], abs=1e-10)


@pytest.mark.parametrize("length", [30, 300])
def test_constant_and_short_rejected(length):
    a, v = signals()
    result = match_features(np.arange(5000.), np.arange(float(length)),
                            {"yaw": a, "vertical": v},
                            {"yaw": np.ones(length), "vertical": np.ones(length)})
    assert result.status == "ambiguous"
    assert result.offset_seconds is None
    if length == 30:
        result = match_features(np.arange(5000.), np.arange(30.),
                                {"yaw": a, "vertical": v},
                                {"yaw": a[100:130], "vertical": v[100:130]})
        assert result.status == "ambiguous"


def test_periodic_ambiguity():
    a = np.sin(np.arange(4000)/15)
    result = match_features(np.arange(4000.), np.arange(300.), {"yaw": a}, {"yaw": a[1000:1300]})
    assert result.status == "ambiguous"


def test_drift_synthetic():
    a, _ = signals(8000)
    utc = np.arange(8000.)+10000
    t = np.arange(4000.)
    b = np.interp(10300+t*1.0015, utc, a)
    drift = estimate_drift(utc, t, a, b, 10303, window_seconds=400)
    assert drift["detected"]
    assert drift["seconds_per_hour"] == pytest.approx(5.4, abs=.3)
    assert drift["stderr_seconds_per_hour"] < .2
    zero = estimate_drift(utc, t, a, a[300:4300], 10300)
    assert not zero["detected"]


def test_gps_primary_and_date():
    track = Track(np.arange(100.)+30000, np.ones(100), np.ones(100), np.ones(100), date(2026, 1, 1))
    gt = np.arange(10.)
    telem = Telemetry(gt, {}, gps_time=gt, gps_utc=1767225600+30010+gt)
    result = synchronize(track, telem)
    assert result.method == "gps"
    assert result.offset_seconds == 30010
    assert result.to_dict()["video_start_utc"] == "2026-01-01T08:20:10+00:00"
