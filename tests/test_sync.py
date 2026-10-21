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
    from gps_video_sync.models import Accepted, TimeMapping, OwnGps
    result = SyncResult(Accepted(TimeMapping(86410., .5, 1., OwnGps())), date=date(2026, 1, 1))
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
    telemetry.gps_utc[5::20] += 100
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
    (np.arange(600.), 30010 + np.arange(600.) + np.r_[np.zeros(300), np.ones(300)], "step"),
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
    for noise in (0, np.random.default_rng(4).normal(scale=.005, size=12)):
        result = _gps_case(gt, 30010 + 1.001 * gt + noise)
        assert result.method == "motion" and result.offset_seconds is None
        assert "constrain" in result.confidence["gps_rejected"]
    gt = 10000 + np.arange(0., 3600.)
    result = _gps_case(gt, 30010 + 1.001 * gt)
    assert result.method == "gps" and result.drift["detected"]
    assert result.offset_seconds == pytest.approx(30010, abs=.01)
    assert result.uncertainty_seconds <= 2


@pytest.mark.parametrize("start", [0, 10000])
@pytest.mark.parametrize("at,size", [(500, 1), (100, 1), (900, 1), (500, .2), (40, .3), (960, -.3),
                                     (10, 1), (990, 1)])
@pytest.mark.parametrize("noise", [0, .02])
def test_gps_utc_steps_are_rejected(start, at, size, noise):
    gt = start + np.arange(1000.)
    jitter = np.random.default_rng(at).normal(scale=noise, size=1000)
    result = _gps_case(gt, 30010 + gt + np.r_[np.zeros(at), np.full(1000 - at, size)] + jitter)
    assert result.method == "motion" and result.offset_seconds is None
    assert "step" in result.confidence["gps_rejected"]


@pytest.mark.parametrize("start", [0, 300])
def test_gps_single_outliers_are_not_steps(start):
    gt = start + np.arange(1000.)
    utc = 30010 + gt + np.random.default_rng(9).normal(scale=.02, size=1000)
    utc[[50, 400, 950]] += 3
    result = _gps_case(gt, utc)
    assert result.method == "gps"
    assert result.offset_seconds == pytest.approx(30010, abs=.05)


def test_gps_out_of_range_utc_without_track_date():
    track = Track(np.arange(30000., 32000.), np.ones(2000), np.ones(2000), np.ones(2000))
    gt = np.arange(60.)
    for stamp in (1e20, 253402214400., 253370764799.):
        result = synchronize(track, Telemetry(gt, {}, gps_time=gt, gps_utc=stamp + gt))
        assert result.status == "ambiguous" and result.offset_seconds is None


@pytest.mark.parametrize("lock", [0, 300, 600])
def test_clean_noisy_gps_is_not_a_step(lock):
    for seed in range(40):
        rng = np.random.default_rng(seed)
        gt = lock + np.arange(1200. - lock)
        result = _gps_case(gt, 30010 + gt + rng.uniform(-.05, .05, len(gt)))
        assert result.method == "gps", (seed, result.confidence)
        assert abs(result.offset_seconds - 30010) <= result.uncertainty_seconds


@pytest.mark.parametrize("n,seed", [(60, 61), (120, 16), (600, 3)])
def test_short_clean_gps_is_not_a_step(n, seed):
    gt = np.arange(float(n))
    result = _gps_case(gt, 30010 + gt + np.random.default_rng(seed).uniform(-.05, .05, n))
    assert result.method == "gps"
    assert abs(result.offset_seconds - 30010) <= result.uncertainty_seconds


def test_deviating_first_observation_is_a_step():
    gt = np.arange(1000.)
    result = _gps_case(gt, 30010 + gt + (gt >= 1))
    assert result.method == "motion" and "step" in result.confidence["gps_rejected"]


def test_slowly_wandering_gps_is_not_a_step():
    gt = np.arange(600.)
    noise = .045*np.sin(np.linspace(0, 6*np.pi, 600)) + np.random.default_rng(1).uniform(-.005, .005, 600)
    result = _gps_case(gt, 30010 + gt + noise)
    assert result.method == "gps"
    assert abs(result.offset_seconds - 30010) <= result.uncertainty_seconds


def test_short_span_high_rate_gps_wander_is_accepted():
    gt = np.arange(600.) / 18
    noise = .045*np.cos(np.linspace(0, np.pi, 600)) + np.random.default_rng(1).uniform(-.005, .005, 600)
    result = _gps_case(gt, 30010 + gt + noise)
    assert result.method == "gps"
    assert abs(result.offset_seconds - 30010) <= result.uncertainty_seconds


@pytest.mark.parametrize("deviation", [
    lambda t, noise: noise + .3*(t >= 300),
    lambda t, noise: ((t >= 120) & (t < 240)).astype(float),
    lambda t, noise: noise + .3*((t >= 200) & (t < 230)),
])
def test_noisy_steps_and_plateaus_are_rejected(deviation):
    t = np.arange(600.)
    noise = np.random.default_rng(0).uniform(-.05, .05, 600)
    result = _gps_case(t, 30010 + t + deviation(t, noise))
    assert result.method == "motion" and "step" in result.confidence["gps_rejected"]


def test_short_plateau_in_short_sequence_is_rejected():
    t = np.arange(60.)
    utc = 30010 + t + np.random.default_rng(636).uniform(-.05, .05, 60)
    utc[30:35] += .3
    result = _gps_case(t, utc)
    assert result.method == "motion" and "step" in result.confidence["gps_rejected"]


def test_smooth_bounded_wander_is_not_a_step():
    t = np.arange(100.)
    noise = .05*np.sin(np.linspace(0, 3.5*np.pi, 100) + np.pi/4)
    result = _gps_case(t, 30010 + t + noise)
    assert result.method == "gps"
    assert abs(result.offset_seconds - 30010) <= result.uncertainty_seconds


def test_irregular_sampling_with_bounded_noise_is_not_a_step():
    t = np.arange(60.)
    t[15:] += 5
    noise = .049*np.sign(np.sin(np.linspace(0, 1.5*np.pi, 60) + 2.8))
    result = _gps_case(t, 30010 + 1.0001*t + noise)
    assert result.method == "gps"
    assert abs(result.offset_seconds - 30010) <= result.uncertainty_seconds


def test_plateau_under_alternating_jitter_is_rejected():
    t = np.arange(100.)
    utc = 30010 + t + .04*(-1.)**np.arange(100)
    utc[50:55] += .3
    result = _gps_case(t, utc)
    assert result.method == "motion" and "step" in result.confidence["gps_rejected"]


def test_gps_after_early_gap_keeps_early_observations():
    t = np.r_[[0., .1, .2], 30 + np.arange(57)*.1]
    result = _gps_case(t, 30010 + t + .049*np.cos(np.linspace(0, np.pi, 60)))
    assert result.method == "gps"
    assert abs(result.offset_seconds - 30010) <= result.uncertainty_seconds


def test_step_at_gps_gap_is_rejected_or_covered():
    t = np.arange(600.)
    t[300:] += 40
    result = _gps_case(t, 30010 + t + .3*(np.arange(600) >= 300))
    assert result.method == "motion" and "step" in result.confidence["gps_rejected"]
    t = np.arange(60.)
    t[30:] += 40
    result = _gps_case(t, 30010 + t + .3*(np.arange(60) >= 30))
    if result.method == "gps":
        for truth in (30010, 30010.3):
            assert abs(result.offset_seconds - truth) <= result.uncertainty_seconds


def test_large_step_at_long_gps_gap_is_rejected_or_covered():
    t = np.arange(200.)
    t[100:] += 400
    utc = 30010 + t + .049*np.cos(np.linspace(0, 10, 200))
    utc[100:] -= 1
    result = _gps_case(t, utc)
    if result.method == "gps":
        for truth in (30010, 30009):
            assert abs(result.offset_seconds - truth) <= result.uncertainty_seconds
