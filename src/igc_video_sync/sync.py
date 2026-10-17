"""Whole-track, masked FFT correlation and clock diagnostics.

All filters are zero phase. Motion proxy correlations are not sensor identities.
"""
from __future__ import annotations
from datetime import datetime, timezone
import numpy as np
from scipy.ndimage import gaussian_filter1d as gf
from scipy.signal import correlate, correlation_lags
from scipy.stats import linregress
from .models import Track, Telemetry, SyncResult


def _grid(time, values, grid, max_gap=None):
    """Interpolate without bridging gaps or extrapolating."""
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
    """Filter each finite run separately; never smear a track gap."""
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
    """Return UTC/video 1 Hz grids and paired yaw, norm, load, vertical proxies."""
    utc = np.arange(np.ceil(track.time[0]), np.floor(track.time[-1])+1)
    t = np.arange(np.ceil(telemetry.time[0]), np.floor(telemetry.time[-1])+1)
    lat = _grid(track.time, track.latitude, utc)
    lon = _grid(track.time, track.longitude, utc)
    alt = _grid(track.time, track.altitude, utc)
    north = np.deg2rad(lat-track.latitude[0])*6371000
    east = np.deg2rad(lon-track.longitude[0])*6371000*np.cos(np.deg2rad(np.nanmean(lat)))
    vn, ve = _smooth(north, 1.5, 1), _smooth(east, 1.5, 1)
    speed = np.hypot(vn, ve)
    # Unwrap independent finite runs (NaNs must not propagate through unwrap).
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
        vf["gyro_norm"] = _grid(telemetry.time, gf(np.linalg.norm(gyro, axis=1), 2/dt), t)
        if "ACCL" in channels:
            gravity = gf(channels["ACCL"], 2/dt, axis=0)
            gravity /= np.maximum(np.linalg.norm(gravity, axis=1)[:, None], 1e-12)
            vf["yaw"] = _grid(telemetry.time, gf(np.sum(gyro*gravity, axis=1), 2/dt), t)
    if "ACCL" in channels:
        norm = np.linalg.norm(channels["ACCL"], axis=1)
        vf["gload"] = _grid(telemetry.time, gf(norm, 3/dt), t)
        vf["vertical"] = _grid(telemetry.time, gf(norm, 1/dt)-gf(norm, 15/dt), t)
    # Avoid filter transients at clip boundaries.
    for a in vf.values():
        a[:5] = np.nan
        a[-5:] = np.nan
    return utc, t, tf, vf


def correlation_curve(a, b, *, min_samples=90):
    """Pearson correlation at all lags with finite-pair normalization (FFT)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    ma, mb = np.isfinite(a).astype(float), np.isfinite(b).astype(float)
    # Center globally first to keep the variance subtraction numerically stable.
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
    """Match paired features on uniform 1 Hz grids, with no offset prior."""
    weights = {"yaw": .4, "vertical": .5, "gyro_norm": .05, "gload": .05}
    curves, details = {}, {}
    for key in track_features.keys() & video_features.keys():
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
        return SyncResult("ambiguous", message="Insufficient varying, overlapping motion (minimum 90 s)")
    # Fixed weights: an absent/constant feature cannot make a single weak cue strong.
    numerator = np.zeros_like(offsets, float)
    denominator = np.zeros_like(offsets, float)
    for key, curve in curves.items():
        weight = weights.get(key, 1.)
        numerator += weight*np.nan_to_num(curve)
        denominator += weight*np.isfinite(curve)
    combined = np.divide(numerator, denominator, out=np.full_like(numerator, np.nan), where=denominator > 0)
    # Demand the primary available cues at a candidate, not only a weak edge cue.
    for key in ("yaw", "vertical"):
        if key in curves:
            combined[~np.isfinite(curves[key])] = np.nan
    if not np.isfinite(combined).any():
        return SyncResult("ambiguous", message="No common valid feature overlap")
    i = int(np.nanargmax(combined))
    candidate, best = float(offsets[i]), float(combined[i])
    alternatives = combined[abs(offsets-candidate) > exclusion_seconds]
    alt = float(np.nanmax(alternatives)) if np.isfinite(alternatives).any() else 0.
    margin = best-alt
    ratio = best/max(alt, .01)
    confidence = {"best_score": best, "alternative_score": alt, "margin": margin,
                  "margin_ratio": ratio, "exclusion_seconds": exclusion_seconds,
                  "candidate_offset_seconds": candidate}
    # Fine alignment uses the sharper acceleration proxy, only if it agrees
    # with the independent whole-track solution. Otherwise use yaw.
    refine = "vertical" if "vertical" in curves and curves["vertical"][i] > .35 else ("yaw" if "yaw" in curves else max(curves, key=lambda k: weights.get(k, 1)))
    fine = np.arange(candidate-3, candidate+3.001, .1)
    scores = _local_scores(utc, t, track_features[refine], video_features[refine], fine, refine == "yaw")
    if np.isfinite(scores).any():
        candidate = float(fine[np.nanargmax(scores)])
    agree = 0
    for key, row in details.items():
        row["agreement_seconds"] = row["offset_seconds"]-candidate
        row["agrees"] = abs(row["agreement_seconds"]) <= 8
        agree += row["agrees"] and row["score"] > .3
    confidence["agreeing_features"] = int(agree)
    confidence["refinement_feature"] = refine
    # Strong lone features are accepted only with a much larger unique margin.
    accepted = best >= .38 and margin >= .08 and ratio >= 1.2 and (agree >= 2 or (best > .7 and margin > .25))
    return SyncResult("ok" if accepted else "ambiguous", candidate if accepted else None,
                      2. if accepted else None, confidence=confidence, per_feature=details,
                      message="" if accepted else "Weak or competing correlation peaks")


def estimate_drift(utc, t, a, b, offset, *, absolute=False, window_seconds=400, search_seconds=8):
    """Independent local window offsets and regression; return clock rate + stderr."""
    windows = []
    offsets = np.arange(offset-search_seconds, offset+search_seconds+.01, .1)
    for start in np.arange(t[0], t[-1]-window_seconds+1, window_seconds):
        use = (t >= start) & (t < start+window_seconds)
        scores = _local_scores(utc, t[use], a, b[use], offsets, absolute, minimum=window_seconds*.65)
        if not np.isfinite(scores).any():
            continue
        i = int(np.nanargmax(scores))
        if i in (0, len(offsets)-1) or scores[i] < .35:
            continue
        windows.append({"video_midpoint": float(start+window_seconds/2),
                        "offset_seconds": float(offsets[i]), "score": float(scores[i])})
    obj = {"detected": False, "clock_rate": 1., "seconds_per_hour": None,
           "stderr_seconds_per_hour": None, "windows": windows, "status": "insufficient_windows"}
    if len(windows) >= 4:
        x = np.array([w["video_midpoint"] for w in windows])
        fit = linregress(x, [w["offset_seconds"] for w in windows])
        detected = abs(fit.slope) > 3*fit.stderr and abs(fit.slope)*np.ptp(x) > .5
        obj.update(detected=bool(detected), clock_rate=float(1+fit.slope) if detected else 1.,
                   measured_clock_rate=float(1+fit.slope), seconds_per_hour=float(fit.slope*3600),
                   stderr_seconds_per_hour=float(fit.stderr*3600),
                   intercept_seconds=float(fit.intercept), status="measured")
    return obj


def synchronize(track: Track, telemetry: Telemetry) -> SyncResult:
    if len(telemetry.time) < 2:
        return SyncResult("failed", message="No telemetry samples", date=track.date)
    if len(telemetry.gps_time) >= 3 and track.date is not None:
        midnight = datetime.combine(track.date, datetime.min.time(), timezone.utc).timestamp()
        delta = telemetry.gps_utc-midnight-telemetry.gps_time
        valid = np.isfinite(delta)
        center = np.median(delta[valid]) if valid.any() else np.nan
        valid &= abs(delta-center) < 5
        if valid.sum() >= 3:
            gt = telemetry.gps_time[valid]
            gu = telemetry.gps_utc[valid]-midnight
            fit = linregress(gt, gu) if np.ptp(gt) > 0 else None
            scatter = float(np.std(delta[valid]))
            # GPS is primary, but must refer to the supplied track's UTC span.
            overlap = min(track.time[-1], gu.max())-max(track.time[0], gu.min())
            if overlap >= 0 and scatter < 2:
                detected = bool(fit and abs(fit.slope-1) > 3*fit.stderr and abs(fit.slope-1)*np.ptp(gt) > .5)
                offset = float(fit.intercept if detected else np.median(delta[valid]))
                return SyncResult("ok", offset, max(.5, scatter*3), "gps", confidence={"gps_samples": int(valid.sum()), "residual_std_seconds": scatter},
                                  drift={"detected": detected, "clock_rate": float(fit.slope) if detected else 1.,
                                         "seconds_per_hour": float((fit.slope-1)*3600) if fit else None,
                                         "stderr_seconds_per_hour": float(fit.stderr*3600) if fit else None},
                                  chapters=telemetry.chapters, date=track.date)
    utc, t, tf, vf = build_features(track, telemetry)
    result = match_features(utc, t, tf, vf)
    result.date, result.chapters = track.date, telemetry.chapters
    if result.status == "ok":
        key = result.confidence["refinement_feature"]
        result.drift = estimate_drift(utc, t, tf[key], vf[key], result.offset_seconds, absolute=key == "yaw")
        if result.drift["detected"]:
            # UTC = video * rate + intercept, not midpoint offset + rate.
            result.offset_seconds = result.drift["intercept_seconds"]
    return result
