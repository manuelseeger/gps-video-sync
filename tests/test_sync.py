from datetime import date
import numpy as np
import pytest
from scipy.ndimage import gaussian_filter1d as gf
from gps_video_sync.models import Track, Telemetry
from gps_video_sync.sync import correlation_curve, match_features, estimate_drift, synchronize, build_features, _grid


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


def test_resampling_does_not_bridge_gap():
    time = np.r_[np.arange(0, 100, 2), np.arange(150, 250, 2)]
    out = _grid(time, np.sin(time), np.arange(250.))
    assert np.isnan(out[100:150]).all()
    assert np.isfinite(out[10:90]).all()
    slow = _grid(np.arange(0, 100, 10), np.arange(10.), np.arange(90.))
    assert np.isfinite(slow).all()


def test_straight_glide_rejected():
    t = np.arange(1000.)
    track = Track(30000+t, 45+t*.00005, np.full(1000, 11.), 1000-t*.5)
    telemetry = Telemetry(t, {"GYRO": np.zeros((1000, 3)), "ACCL": np.tile([0., 0., 9.81], (1000, 1))})
    assert synchronize(track, telemetry).status == "ambiguous"


def test_gps_primary_and_date():
    track = Track(np.arange(100.)+30000, np.ones(100), np.ones(100), np.ones(100), date(2026, 1, 1))
    gt = np.arange(20.)
    telem = Telemetry(gt, {}, gps_time=gt, gps_utc=1767225600+30010+gt)
    result = synchronize(track, telem)
    assert result.method == "gps"
    assert result.offset_seconds == 30010
    assert result.to_dict()["video_start_utc"] == "2026-01-01T08:20:10+00:00"


def test_result_midnight_timebase():
    from gps_video_sync import SyncResult
    result = SyncResult("ok", 86410., date=date(2026, 1, 1))
    assert result.video_start_utc == "2026-01-02T00:00:10+00:00"
    obj = result.to_dict()
    assert obj["dateUtc"] == "2026-01-02"
    assert obj["utc_origin_date"] == "2026-01-01"
    assert obj["dateUtc"]+"T"+obj["videoStartUtc"]+"+00:00" == obj["video_start_utc"]
    assert obj["offset_seconds"] == obj["videoStartUtcSeconds"] == 86410.


@pytest.mark.parametrize("rate", [1.0003, 1.0008, 1.0015])
def test_gps_drift_uses_fitted_residual(rate):
    from datetime import datetime, timezone
    t = np.arange(0., 7200, 10)
    track = Track(np.array([30000., 38000.]), np.array([45., 46.]), np.array([11., 12.]),
                  np.array([100., 200.]), date(2026, 1, 1))
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    telemetry = Telemetry(t, {}, gps_time=t, gps_utc=epoch+30010+t*rate)
    result = synchronize(track, telemetry)
    assert result.status == "ok"
    assert result.method == "gps"
    assert result.drift["detected"]
    assert result.offset_seconds == pytest.approx(30010, abs=.1)
    assert result.drift["clock_rate"] == pytest.approx(rate, abs=1e-8)
    assert result.uncertainty_seconds <= 1
    telemetry.gps_utc[::20] += 100
    result = synchronize(track, telemetry)
    assert result.method == "gps"
    assert result.offset_seconds == pytest.approx(30010, abs=.1)
    assert result.confidence["gps_inlier_fraction"] == pytest.approx(.95)


def test_gps_establishes_missing_date_and_rejects_split_clock():
    from datetime import datetime, timezone
    t = np.arange(100.)
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    track = Track(np.array([30000., 31000.]), np.array([45., 46.]), np.array([11., 12.]), np.array([100., 200.]))
    telemetry = Telemetry(t, {}, gps_time=t, gps_utc=epoch+30010+t)
    result = synchronize(track, telemetry)
    assert result.method == "gps" and result.date == date(2026, 1, 1)
    telemetry.gps_utc[:60] += 30
    result = synchronize(track, telemetry)
    assert result.status == "ambiguous" and result.offset_seconds is None


def test_correlated_turn_proxies_are_one_agreement_group():
    a, b = signals()
    rng = np.random.default_rng(77)
    noisy = a[1000:1600]+gf(rng.normal(size=600), 3)
    result = match_features(np.arange(5000.), np.arange(600.),
                            {"yaw": a, "gyro_norm": a, "gload": a},
                            {"yaw": noisy, "gyro_norm": noisy, "gload": noisy})
    assert result.confidence["best_score"] < .7
    assert result.confidence["agreeing_groups"] == ["turn"]
    assert result.status == "ambiguous"


def _gps_case(gps_time, gps_utc):
    track = Track(np.arange(30000., 45000.), np.ones(15000), np.ones(15000), np.ones(15000), date(2026, 1, 1))
    time = np.arange(0., max(2., float(np.max(gps_time)) + 1), .1)
    return synchronize(track, Telemetry(time, {}, gps_time=np.asarray(gps_time, float),
                                        gps_utc=1767225600 + np.asarray(gps_utc, float)))


@pytest.mark.parametrize("gps_time,gps_utc,reason", [
    (np.arange(4) * .1, 30010 + np.arange(4) * .6, "span"),
    (np.arange(60.), np.full(60, 30010.), "increase"),
    (np.arange(60.), 30070 - np.arange(60.), "increase"),
    (np.arange(60.), 30010 + 6 * np.arange(60.), "rate"),
    (np.arange(600.), 30010 + np.arange(600.) + np.r_[np.zeros(300), np.ones(300)], "rate"),
    (np.arange(600.), 30010 + np.arange(600.) + np.r_[np.zeros(300), np.full(300, 20.)], "GPS UTC"),
])
def test_gps_rejects_unusable_clock_mapping(gps_time, gps_utc, reason):
    result = _gps_case(gps_time, gps_utc)
    assert result.status == "ambiguous" and result.offset_seconds is None
    assert result.method == "motion"
    assert reason in result.confidence["gps_rejected"]
    assert result.confidence["gps_rejected"] in result.message


def test_gps_accepts_slow_one_hertz_clock():
    result = _gps_case(np.arange(12.), 30010 + np.arange(12.))
    assert result.status == "ok" and result.method == "gps"
    assert result.offset_seconds == pytest.approx(30010)
    assert result.to_dict()["clockRate"] == 1


def test_track_without_whole_second_grid_is_ambiguous():
    track = Track(np.array([10.2, 10.8]), np.zeros(2), np.zeros(2), np.zeros(2))
    rng = np.random.default_rng(3)
    telemetry = Telemetry(np.arange(2000.) / 10, {"GYRO": rng.normal(size=(2000, 3)),
                                                    "ACCL": rng.normal(size=(2000, 3))})
    result = synchronize(track, telemetry)
    assert result.status == "ambiguous" and result.offset_seconds is None
    assert "two whole seconds" in result.message


def test_gps_far_from_video_start_uses_rate_or_rejects():
    gt = 10000 + np.arange(12.)
    result = _gps_case(gt, 30010 + 1.001 * gt)
    assert result.method == "gps" and result.drift["detected"]
    assert result.offset_seconds == pytest.approx(30010, abs=.01)
    noise = np.random.default_rng(4).normal(scale=.005, size=12)
    result = _gps_case(gt, 30010 + 1.001 * gt + noise)
    assert result.method == "motion" and result.offset_seconds is None
    assert "constrain" in result.confidence["gps_rejected"]
    gt = 10000 + np.arange(0., 3600.)
    result = _gps_case(gt, 30010 + 1.001 * gt)
    assert result.method == "gps" and result.drift["detected"]
    assert result.offset_seconds == pytest.approx(30010, abs=.01)


def test_gps_long_utc_step_is_rejected():
    gt = np.arange(1000.)
    result = _gps_case(gt, 30010 + gt + np.r_[np.zeros(500), np.ones(500)])
    assert result.method == "motion" and result.offset_seconds is None
    assert "step" in result.confidence["gps_rejected"]


def test_gps_out_of_range_utc_without_track_date():
    track = Track(np.arange(30000., 32000.), np.ones(2000), np.ones(2000), np.ones(2000))
    gt = np.arange(60.)
    result = synchronize(track, Telemetry(gt, {}, gps_time=gt, gps_utc=np.full(60, 1e20)))
    assert result.status == "ambiguous" and result.offset_seconds is None
