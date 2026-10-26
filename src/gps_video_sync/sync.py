from __future__ import annotations
from numbers import Real
import numpy as np
from scipy.ndimage import gaussian_filter1d as gf
from scipy.signal import correlate, correlation_lags
from scipy.stats import linregress, theilslopes
from dataclasses import replace
from .models import (Track, Telemetry, SyncResult, Accepted, Ambiguous, Failed,
                     TimeMapping, OwnMotion, GpsTimingPolicy, GpsEvaluation)
from .gps_timing import evaluate_gps, rejection_message


def _grid(time, values, grid, max_gap=None):
    time, values = np.asarray(time), np.asarray(values)
    if len(time) < 2 or np.any(np.diff(time) <= 0):
        raise ValueError("sample times must be increasing and contain at least two samples")
    if max_gap is None:
        max_gap = max(5., 3*float(np.median(np.diff(time))))
    out = np.interp(grid, time, values, left=np.nan, right=np.nan)
    right = np.searchsorted(time, grid).clip(1, len(time)-1)
    out[(time[right] - time[right-1]) > max_gap] = np.nan
    return out


def _smooth(a, sigma, order=0):
    out = np.full(len(a), np.nan)
    good = np.isfinite(a)
    starts = np.flatnonzero(good & ~np.r_[False, good[:-1]])
    ends = np.flatnonzero(good & ~np.r_[good[1:], False]) + 1
    for lo, hi in zip(starts, ends):
        if hi-lo < max(5, 2*sigma):
            continue
        out[lo:hi] = gf(a[lo:hi], sigma, order=order, mode="nearest")
        edge = max(2, int(2*sigma))
        out[lo:min(hi, lo+edge)] = np.nan
        out[max(lo, hi-edge):hi] = np.nan
    return out


def build_features(track: Track, telemetry: Telemetry):
    utc = np.arange(np.ceil(track.time[0]), np.floor(track.time[-1])+1)
    t = np.arange(np.ceil(telemetry.time[0]), np.floor(telemetry.time[-1])+1)
    if len(utc) < 2 or len(t) < 2:
        return utc, t, {}, {}
    lat = _grid(track.time, track.latitude, utc)
    lon = _grid(track.time, track.longitude, utc)
    alt = _grid(track.time, track.altitude, utc)
    north = np.deg2rad(lat-track.latitude[0])*6371000
    east = np.deg2rad(lon-track.longitude[0])*6371000*np.cos(np.deg2rad(np.nanmean(lat)))
    vn, ve = _smooth(north, 1.5, 1), _smooth(east, 1.5, 1)
    speed = np.hypot(vn, ve)
    heading = np.arctan2(ve, vn)
    good = np.isfinite(heading)
    for ix in np.split(np.flatnonzero(good), np.flatnonzero(np.diff(np.flatnonzero(good)) > 1)+1):
        if len(ix):
            heading[ix] = np.unwrap(heading[ix])
    turn = _smooth(heading, 2, 1)
    vertical = _smooth(alt - _smooth(alt, 15), 1, 2)
    tf = {"yaw": turn, "gyro_norm": abs(turn), "gload": turn**2, "vertical": vertical}
    for a in tf.values():
        a[speed < 3] = np.nan
    vf = {}
    dt = np.median(np.diff(telemetry.time))
    channels = telemetry.channels
    if "GYRO" in channels:
        gyro = channels["GYRO"]
        vf["gyro_norm"] = _grid(telemetry.time, gf(np.linalg.norm(gyro, axis=1), 2/dt, mode="nearest"), t)
        if "ACCL" in channels:
            gravity = gf(channels["ACCL"], 2/dt, axis=0, mode="nearest")
            gravity /= np.maximum(np.linalg.norm(gravity, axis=1)[:, None], 1e-12)
            vf["yaw"] = _grid(telemetry.time, gf(np.sum(gyro*gravity, axis=1), 2/dt, mode="nearest"), t)
    if "ACCL" in channels:
        norm = np.linalg.norm(channels["ACCL"], axis=1)
        vf["gload"] = _grid(telemetry.time, gf(norm, 3/dt, mode="nearest"), t)
        vf["vertical"] = _grid(telemetry.time, gf(norm, 1/dt, mode="nearest")-gf(norm, 15/dt, mode="nearest"), t)
    for key, a in vf.items():
        edge = 30 if key == "vertical" else 6
        a[:edge] = np.nan
        a[-edge:] = np.nan
    return utc, t, tf, vf


def correlation_curve(a, b, *, min_samples=90):
    a, b = np.asarray(a, float), np.asarray(b, float)
    ma, mb = np.isfinite(a).astype(float), np.isfinite(b).astype(float)
    a = np.where(ma, a - np.nanmean(a) if ma.any() else 0, 0)
    b = np.where(mb, b - np.nanmean(b) if mb.any() else 0, 0)
    def corr(x, y):
        return correlate(x, y, mode="full", method="fft")
    n = np.maximum(np.rint(corr(ma, mb)), 0)
    sa, sb = corr(a, mb), corr(ma, b)
    with np.errstate(divide="ignore", invalid="ignore"):
        va = np.maximum(corr(a*a, mb)-sa*sa/np.maximum(n, 1), 0)
        vb = np.maximum(corr(ma, b*b)-sb*sb/np.maximum(n, 1), 0)
        r = (corr(a, b)-sa*sb/np.maximum(n, 1))/np.sqrt(va*vb)
    valid = (n >= min_samples) & (va > 1e-10) & (vb > 1e-10)
    return correlation_lags(len(a), len(b)), np.where(valid, np.clip(r, -1, 1), np.nan), n


def _local_scores(utc, t, a, b, offsets, absolute=False, minimum=60):
    scores = []
    for offset in offsets:
        y = np.interp(utc-offset, t, b, left=np.nan, right=np.nan)
        good = np.isfinite(a) & np.isfinite(y)
        if good.sum() < minimum or np.std(a[good]) < 1e-8 or np.std(y[good]) < 1e-8:
            scores.append(np.nan)
        else:
            r = float(np.corrcoef(a[good], y[good])[0, 1])
            scores.append(abs(r) if absolute else r)
    return np.asarray(scores)


def match_features(utc, t, track_features, video_features, *, exclusion_seconds=25.) -> SyncResult:
    weights = {"yaw": .4, "vertical": .5, "gyro_norm": .05, "gload": .05}
    curves, details = {}, {}
    for key in sorted(track_features.keys() & video_features.keys()):
        a, b = track_features[key], video_features[key]
        required = max(90, .65*min(np.isfinite(a).sum(), np.isfinite(b).sum()))
        lags, r, n = correlation_curve(a, b, min_samples=required)
        offsets = utc[0]-t[0]+lags
        if key == "yaw":
            r = abs(r)
        if np.isfinite(r).any():
            curves[key] = r
            i = np.nanargmax(r)
            details[key] = {"offset_seconds": float(offsets[i]), "score": float(r[i]), "overlap_seconds": int(n[i])}
    if not curves:
        return SyncResult(Ambiguous("Insufficient varying, overlapping motion (minimum 90 s)"))
    numerator = np.zeros_like(offsets, float)
    denominator = np.zeros_like(offsets, float)
    for key, curve in curves.items():
        weight = weights.get(key, 1.)
        numerator += weight*np.nan_to_num(curve)
        denominator += weight*np.isfinite(curve)
    combined = np.divide(numerator, denominator, out=np.full_like(numerator, np.nan), where=denominator > 0)
    for key in ("yaw", "vertical"):
        if key in curves:
            combined[~np.isfinite(curves[key])] = np.nan
    if not np.isfinite(combined).any():
        return SyncResult(Ambiguous("No common valid feature overlap"))
    i = int(np.nanargmax(combined))
    candidate, best = float(offsets[i]), float(combined[i])
    alternatives = combined[abs(offsets-candidate) > exclusion_seconds]
    alt = float(np.nanmax(alternatives)) if np.isfinite(alternatives).any() else 0.
    margin = best-alt
    ratio = best/max(alt, .01)
    confidence = {"best_score": best, "alternative_score": alt, "margin": margin,
                  "margin_ratio": ratio, "exclusion_seconds": exclusion_seconds,
                  "candidate_offset_seconds": candidate}
    refine = "vertical" if "vertical" in curves and curves["vertical"][i] > .35 else ("yaw" if "yaw" in curves else max(curves, key=lambda k: weights.get(k, 1)))
    fine = np.arange(candidate-3, candidate+3.001, .1)
    scores = _local_scores(utc, t, track_features[refine], video_features[refine], fine, refine == "yaw")
    boundary = False
    if np.isfinite(scores).any():
        index = int(np.nanargmax(scores))
        candidate = round(float(fine[index]), 3)
        boundary = index in (0, len(fine)-1)
    agree = 0
    agreeing_groups = set()
    for key, row in details.items():
        row["agreement_seconds"] = row["offset_seconds"]-candidate
        row["agrees"] = abs(row["agreement_seconds"]) <= 8
        supports = row["agrees"] and row["score"] > .3
        agree += supports
        if supports:
            agreeing_groups.add("turn" if key in {"yaw", "gyro_norm", "gload"} else key)
    confidence["agreeing_features"] = int(agree)
    confidence["agreeing_groups"] = sorted(agreeing_groups)
    confidence["fine_refinement_at_boundary"] = boundary
    confidence["refinement_feature"] = refine
    accepted = (not boundary and best >= .38 and margin >= .08 and ratio >= 1.2
                and (len(agreeing_groups) >= 2 or (best > .7 and margin > .25)))
    outcome = (Accepted(TimeMapping(candidate, 2., 1., OwnMotion())) if accepted
               else Ambiguous("Weak or competing correlation peaks"))
    return SyncResult(outcome, confidence=confidence, per_feature=details)


def estimate_drift(utc, t, a, b, offset, *, absolute=False, window_seconds=400, search_seconds=8):
    windows = []
    search_seconds = max(search_seconds, .01*(t[-1]-t[0]))
    for start in np.arange(t[0], t[-1]-window_seconds+1, window_seconds):
        use = (t >= start) & (t < start+window_seconds)
        lags, coarse, _ = correlation_curve(a, b[use], min_samples=window_seconds*.65)
        candidates = utc[0]-t[use][0]+lags
        if absolute:
            coarse = abs(coarse)
        allowed = (abs(candidates-offset) <= search_seconds) & np.isfinite(coarse)
        if not allowed.any():
            continue
        peak = float(candidates[np.nanargmax(np.where(allowed, coarse, np.nan))])
        offsets = np.arange(peak-1.5, peak+1.501, .1)
        scores = _local_scores(utc, t[use], a, b[use], offsets, absolute, minimum=window_seconds*.65)
        if not np.isfinite(scores).any():
            continue
        i = int(np.nanargmax(scores))
        if i in (0, len(offsets)-1) or scores[i] < .35 or abs(offsets[i]-offset) >= search_seconds-.2:
            continue
        windows.append({"video_midpoint": float(start+window_seconds/2),
                        "offset_seconds": round(float(offsets[i]), 3), "score": float(scores[i])})
    obj = {"detected": False, "clock_rate": 1., "seconds_per_hour": None,
           "stderr_seconds_per_hour": None, "windows": windows, "status": "insufficient_windows"}
    if len(windows) >= 4:
        x = np.array([w["video_midpoint"] for w in windows])
        y = np.array([w["offset_seconds"] for w in windows])
        robust = theilslopes(y, x)
        residual = y-(robust.intercept+robust.slope*x)
        residual -= np.median(residual)
        good = abs(residual) <= max(.2, 5*np.median(abs(residual)))
        if good.sum() < 4 or good.mean() < .8:
            obj["status"] = "inconsistent_windows"
            return obj
        fit = linregress(x[good], y[good])
        scatter = float(np.std(y[good]-(fit.intercept+fit.slope*x[good])))
        detected = abs(fit.slope) > 3*fit.stderr and abs(fit.slope)*np.ptp(x[good]) > .5
        obj.update(detected=bool(detected), clock_rate=float(1+fit.slope) if detected else 1.,
                   measured_clock_rate=float(1+fit.slope), seconds_per_hour=float(fit.slope*3600),
                   stderr_seconds_per_hour=float(fit.stderr*3600),
                   intercept_seconds=float(fit.intercept), intercept_stderr_seconds=float(fit.intercept_stderr),
                   residual_std_seconds=scatter, inlier_windows=int(good.sum()), status="measured")
    return obj


def validate_track(track: Track) -> None:
    if np.ndim(track.time) != 1 or not np.all(np.isfinite(track.time)) or np.any(np.diff(track.time) <= 0):
        raise ValueError("track times must be finite, strictly increasing 1-D arrays")
    if len(track.time) < 2 or any(np.shape(a) != np.shape(track.time) for a in
                                (track.latitude, track.longitude, track.altitude)):
        raise ValueError("Track needs at least two fixes and matching 1-D arrays")
    if not all(np.all(np.isfinite(a)) for a in (track.latitude, track.longitude, track.altitude)):
        raise ValueError("Track coordinates and altitude must be finite")


def _finite_seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = np.isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be a finite number")


def validate_telemetry(telemetry: Telemetry) -> None:
    time = telemetry.time
    if np.ndim(time) != 1 or not np.all(np.isfinite(time)) or np.any(np.diff(time) <= 0) or np.any(time < 0):
        raise ValueError("video times must be nonnegative, finite, strictly increasing 1-D arrays")
    if telemetry.video is not None and (not isinstance(telemetry.video, str) or not telemetry.video):
        raise ValueError("Recording identity must be a nonempty string")
    if telemetry.duration is not None:
        _finite_seconds(telemetry.duration, "Recording duration")
        if telemetry.duration <= 0:
            raise ValueError("Recording duration must be finite and positive")
        if len(time) and time[-1] >= telemetry.duration:
            raise ValueError("Telemetry samples must be inside recording duration")
    _finite_seconds(telemetry.media_offset_seconds, "Media offset")
    if telemetry.media_offset_seconds < 0:
        raise ValueError("Media offset must be finite and nonnegative")
    for key, values in telemetry.channels.items():
        if np.ndim(values) == 0 or len(values) != len(time):
            raise ValueError(f"Channel {key!r} must match time length")
        if key in {"GYRO", "ACCL", "GRAV", "CORI", "IORI"}:
            width = 4 if key in {"CORI", "IORI"} else 3
            if np.shape(values) != (len(telemetry.time), width):
                raise ValueError(f"{key} must have shape (video samples, {width})")
    if np.ndim(telemetry.gps_time) != 1 or np.shape(telemetry.gps_utc) != np.shape(telemetry.gps_time):
        raise ValueError("GPS video and UTC times must be matching 1-D arrays")


def synchronize(track: Track, telemetry: Telemetry, *,
                gps_policy: GpsTimingPolicy = GpsTimingPolicy()) -> SyncResult:
    if not isinstance(gps_policy, GpsTimingPolicy):
        raise ValueError("gps_policy must be a GpsTimingPolicy")
    validate_track(track)
    validate_telemetry(telemetry)
    return _synchronize(track, telemetry, gps_policy=gps_policy)


def _synchronize(track: Track, telemetry: Telemetry, *,
                 gps_policy: GpsTimingPolicy = GpsTimingPolicy()) -> SyncResult:
    timeline = dict(media_offset_seconds=telemetry.media_offset_seconds, duration=telemetry.duration)
    if len(telemetry.time) < 2:
        return SyncResult(Failed("No telemetry samples"), date=track.date,
                          video=telemetry.video, gps=GpsEvaluation(policy=gps_policy), **timeline)
    gps = evaluate_gps(track, telemetry, policy=gps_policy)
    if gps.verdict == "accepted":
        fit = gps.fit
        return SyncResult(Accepted(fit.mapping), gps=gps, date=gps.origin_date,
                          confidence={"gps_samples": gps.inlier_count,
                                      "gps_inlier_fraction": gps.inlier_count/gps.finite_count,
                                      "gps_span_seconds": gps.support_end_seconds-gps.support_start_seconds,
                                      "residual_std_seconds": fit.envelope.residual_std_seconds},
                          drift={"detected": fit.drift_detected, "clock_rate": fit.mapping.clock_rate,
                                 "measured_clock_rate": fit.measured_clock_rate,
                                 "seconds_per_hour": (fit.measured_clock_rate-1)*3600,
                                 "stderr_seconds_per_hour": fit.rate_stderr*3600},
                          video=telemetry.video, **timeline)
    gps_rejection = (rejection_message(gps) if gps.verdict == "rejected"
                     or gps.reason_codes == ("date_unresolved",) else "")
    utc, t, tf, vf = build_features(track, telemetry)
    if len(utc) < 2 or len(t) < 2:
        result = SyncResult(Ambiguous("Track or video spans less than two whole seconds"))
    else:
        result = match_features(utc, t, tf, vf)
    result.gps, result.motion_attempted = gps, True
    result.date, result.video = track.date, telemetry.video
    result.media_offset_seconds, result.duration = telemetry.media_offset_seconds, telemetry.duration
    if gps_rejection:
        result.confidence["gps_rejected"] = gps_rejection
        if result.status != "ok":
            result.outcome = Ambiguous(f"{gps_rejection}; motion {result.message}")
    if result.status == "ok":
        key = result.confidence["refinement_feature"]
        result.drift = estimate_drift(utc, t, tf[key], vf[key], result.offset_seconds, absolute=key == "yaw")
        if result.drift["detected"]:
            result.outcome = Accepted(replace(result.mapping,
                offset_seconds=round(result.drift["intercept_seconds"], 3),
                uncertainty_seconds=max(result.uncertainty_seconds,
                                        3*result.drift["intercept_stderr_seconds"],
                                        3*result.drift["residual_std_seconds"]),
                clock_rate=result.drift["clock_rate"]))
    return result
