from __future__ import annotations

from datetime import datetime, timezone, timedelta
from dataclasses import replace
from enum import Enum

import numpy as np
from scipy.optimize import linprog
from scipy.stats import linregress

from .models import (GpsTimingPolicy, GpsEvaluation, GpsFit, GpsEnvelope, GpsRun,
                     TimeMapping, OwnGps, Track, Telemetry)


MIN_GPS_SPAN_SECONDS = 10.
MAX_GPS_RATE_ERROR = 2e-3
MAX_UNIX_SECONDS = 253370764800.
GPS_STEP_SECONDS = .1
GPS_AFFINE_TOLERANCE_SECONDS = 1e-8


GPS_STEP_WINDOWS = (3, 5, 10, 20, 30)
GPS_STEP_NOISE_CAP = .15
GPS_GAP_SECONDS = 2.
MAX_NON_AFFINE_EXCURSION_SECONDS = .5
MAX_RESIDUAL_STD_SECONDS = .5
MIN_UNCERTAINTY_SECONDS = .5
HARD_LIMITS = dict(detector_threshold_seconds=GPS_STEP_SECONDS,
                   max_non_affine_excursion_seconds=MAX_NON_AFFINE_EXCURSION_SECONDS,
                   max_rate_error=MAX_GPS_RATE_ERROR,
                   min_inlier_fraction=.8,
                   min_observations=3,
                   min_uncertainty_seconds=MIN_UNCERTAINTY_SECONDS,
                   max_residual_std_seconds=MAX_RESIDUAL_STD_SECONDS,
                   min_span_seconds=MIN_GPS_SPAN_SECONDS)


def _gps_step(time, delta):
    """True when the clock offset changes abruptly between adjacent runs.

    The offsets are detrended with the rate between the medians of the first
    and last thirds. For each window size in GPS_STEP_WINDOWS up to n/2 and
    each split, the median of the window before the split is compared with the
    median of the window after it. Near the ends a window shrinks to the
    available observations, down to one.

    Noise within +-GPS_STEP_SECONDS/2 changes a median by at most
    GPS_STEP_SECONDS/2. The detrending rate error is then at most
    (GPS_STEP_SECONDS + |rate| * time range of both thirds) / span. Over the
    time extent of a window pair, the rate error adds at most that rate error
    times the extent. A difference greater than GPS_STEP_SECONDS plus this
    allowance is therefore not caused by such noise. The threshold is also at
    least six standard errors of the white noise between adjacent observations,
    capped at GPS_STEP_NOISE_CAP, so that noisier data needs more evidence but
    a step of 3 * GPS_STEP_SECONDS stays detectable.
    """
    order = np.argsort(time, kind="stable")
    time, delta = time[order], delta[order]
    n = len(time)
    if n < 2:
        return False
    third = max(1, n//3)
    span = float(np.median(time[-third:])-np.median(time[:third]))
    if span <= 0:
        return False
    rate = (np.median(delta[-third:])-np.median(delta[:third]))/span
    level = delta-rate*time
    ranges = float(np.ptp(time[:third])+np.ptp(time[-third:]))
    rate_error = (GPS_STEP_SECONDS+abs(rate)*ranges)/span
    noise = 1.4826*float(np.median(abs(np.diff(level))))/np.sqrt(2)

    def stepped(before, after, size_before, size_after, extent):
        error = min(GPS_STEP_NOISE_CAP, 6*1.25*noise*np.sqrt(1/size_before+1/size_after))
        return np.any(abs(after-before)-rate_error*extent > max(GPS_STEP_SECONDS, error))

    for window in [w for w in GPS_STEP_WINDOWS if w <= n//2] or [1]:
        if n >= 2*window:
            medians = np.median(np.lib.stride_tricks.sliding_window_view(level, window), axis=1)
            extent = time[2*window-1:]-time[:n-2*window+1]
            if stepped(medians[:-window], medians[window:], window, window, extent):
                return True
        for split in range(1, n):
            if window <= split <= n-window:
                continue
            lo, hi = max(0, split-window), min(n, split+window)
            if stepped(np.median(level[lo:split]), np.median(level[split:hi]),
                       split-lo, hi-split, float(time[hi-1]-time[lo])):
                return True
    return False


def _within_run_rate(time, utc):
    """Rate of UTC against video time inside runs separated by gaps.

    A gap is longer than GPS_GAP_SECONDS and five median sample intervals.
    Each run has its own offset, so a step at a gap does not change the rate.
    Returns the rate, its bound for noise within +-GPS_STEP_SECONDS/2, its
    standard error, and the run index of each observation.
    """
    steps = np.diff(time)
    gap = max(GPS_GAP_SECONDS, 5*float(np.median(steps)))
    run = np.r_[0, np.cumsum(steps > gap)]
    counts = np.bincount(run)
    centered_time = time-(np.bincount(run, time)/counts)[run]
    centered_utc = utc-(np.bincount(run, utc)/counts)[run]
    weight = float(np.sum(centered_time**2))
    if weight <= 0:
        return 1., np.inf, np.inf, run
    rate = float(np.sum(centered_time*centered_utc))/weight
    residual = centered_utc-rate*centered_time
    freedom = max(1, len(time)-len(counts)-1)
    stderr = float(np.sqrt(np.sum(residual**2)/freedom/weight))
    bound = GPS_STEP_SECONDS/2*float(np.sum(abs(centered_time)))/weight
    return rate, bound, stderr, run


class _Consistency(Enum):
    CONSISTENT = 'consistent'
    INCONSISTENT = 'inconsistent'
    UNRESOLVED = 'unresolved'


def _affine_consistency(time, delta, half_noise=GPS_STEP_SECONDS/2+GPS_AFFINE_TOLERANCE_SECONDS):
    x = (time-time.mean())/np.ptp(time)
    center = float(delta.mean())
    y = delta-center
    # Reserve the numerical floor for validation of the solver's boundary solution.
    solver_noise = half_noise-GPS_AFFINE_TOLERANCE_SECONDS
    selected = np.unique([0, len(x)-1, int(np.argmin(y)), int(np.argmax(y))])
    while True:
        rows = np.column_stack((np.ones(len(selected)), x[selected]))
        try:
            fit = linprog([0., 0.], A_ub=np.vstack((rows, -rows)),
                          b_ub=np.r_[y[selected]+solver_noise, -y[selected]+solver_noise],
                          bounds=[(None, None), (None, None)], method='highs',
                          options={'primal_feasibility_tolerance': 1e-9,
                                   'dual_feasibility_tolerance': 1e-9})
        except (ValueError, RuntimeError, FloatingPointError):
            return _Consistency.UNRESOLVED
        if fit.status == 2:
            return _Consistency.INCONSISTENT
        if fit.status != 0 or not np.all(np.isfinite(fit.x)):
            return _Consistency.UNRESOLVED
        residual = delta-(center+fit.x[0]+fit.x[1]*x)
        if np.max(abs(residual)) <= half_noise:
            return _Consistency.CONSISTENT
        expanded = np.union1d(selected, [int(np.argmin(residual)), int(np.argmax(residual))])
        if len(expanded) == len(selected):
            return _Consistency.UNRESOLVED
        selected = expanded


def _rate_regime_consistency(time, utc, runs, half_noise):
    intervals = []
    edges = np.r_[0, np.flatnonzero(np.diff(runs)) + 1, len(time)]
    for first, last in zip(edges[:-1], edges[1:]):
        rt, ru = time[first:last], utc[first:last]
        if len(rt) < 6 or np.ptp(rt) < MIN_GPS_SPAN_SECONDS:
            continue
        span = float(rt[-1]-rt[0])
        minimum_span = float(np.min(rt[5:]-rt[:-5]))
        width = span
        while width >= minimum_span:
            starts = np.r_[np.arange(0., span-width, width/2), span-width]
            lo = np.searchsorted(rt, rt[0]+starts, side='left')
            hi = np.searchsorted(rt, rt[0]+starts+width, side='right')
            hi[-1] = len(rt)
            for left, right in zip(lo, hi):
                x, y = rt[left:right], ru[left:right]-rt[left:right]
                if len(x) < 6 or x[-1]-x[0] < MIN_GPS_SPAN_SECONDS:
                    continue
                z = x-x.mean()
                weight = float(np.sum(z*z))
                drift = float(np.sum(z*(y-y.mean())))/weight
                residual = y-y.mean()-drift*z
                if np.ptp(residual) > 2*half_noise:
                    consistency = _affine_consistency(x, y, half_noise)
                    if consistency is _Consistency.UNRESOLVED:
                        return consistency
                    if consistency is _Consistency.INCONSISTENT:
                        continue
                bound = half_noise*float(np.sum(abs(z)))/weight
                intervals.append((float(x[0]), float(x[-1]), 1.+drift, float(bound)))
            if width == MIN_GPS_SPAN_SECONDS:
                break
            width = max(MIN_GPS_SPAN_SECONDS, width/2)
    ended = sorted(intervals, key=lambda item: item[1])
    position, lower, upper = 0, -np.inf, np.inf
    for start, _, rate, bound in sorted(intervals):
        while position < len(ended) and ended[position][1] < start:
            _, _, prior_rate, prior_bound = ended[position]
            lower = max(lower, prior_rate-prior_bound)
            upper = min(upper, prior_rate+prior_bound)
            position += 1
        if lower > rate+bound or upper < rate-bound:
            return _Consistency.INCONSISTENT
    return _Consistency.CONSISTENT


def evaluate_gps(track: Track, telemetry: Telemetry, *, policy: GpsTimingPolicy) -> GpsEvaluation:
    finite = (np.isfinite(telemetry.gps_time) & np.isfinite(telemetry.gps_utc)
              & (telemetry.gps_utc >= 0) & (telemetry.gps_utc < MAX_UNIX_SECONDS))
    count = int(finite.sum())
    rounding = GPS_AFFINE_TOLERANCE_SECONDS
    if count:
        rounding += float(np.max(abs(np.spacing(telemetry.gps_utc[finite]))))
        rounding += float(np.max(abs(np.spacing(telemetry.gps_time[finite]))))
    half_noise = GPS_STEP_SECONDS/2+rounding
    origin = track.date
    if origin is None and count >= 3:
        stamps = telemetry.gps_utc[finite]
        center = datetime.fromtimestamp(float(np.median(stamps)), timezone.utc).date()
        candidates = [center+timedelta(days=i) for i in (-1, 0, 1)]
        def overlap(day):
            epoch = datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()
            return min(track.time[-1], stamps.max()-epoch)-max(track.time[0], stamps.min()-epoch)
        origin = max(candidates, key=overlap)
    evaluation = GpsEvaluation(policy=policy, origin_date=origin, finite_count=count,
                               timestamp_rounding_seconds=rounding)
    if not len(telemetry.gps_time):
        return replace(evaluation, reason_codes=("no_observations",))
    if origin is None:
        return replace(evaluation, reason_codes=("date_unresolved",))
    if count < 3:
        return replace(evaluation, verdict="rejected", reason_codes=("insufficient_observations",))

    midnight = datetime.combine(origin, datetime.min.time(), timezone.utc).timestamp()
    order = np.argsort(telemetry.gps_time[finite], kind="stable")
    t = telemetry.gps_time[finite][order]
    stamps = telemetry.gps_utc[finite][order]
    # Fit relative UTC to avoid losing precision in intercept/rate arithmetic.
    base = float(stamps[0])
    u = stamps-base
    delta = u-t
    stepped = bool(_gps_step(t, delta))
    third = max(1, len(t)//3)
    lo, hi = np.arange(third), np.arange(len(t)-third, len(t))
    early, late = t <= t.min()+np.ptp(t)/3, t >= t.max()-np.ptp(t)/3
    if early.sum() >= 3 and late.sum() >= 3:
        lo, hi = np.flatnonzero(early), np.flatnonzero(late)
    span = np.median(t[hi])-np.median(t[lo])
    slope = (np.median(delta[hi])-np.median(delta[lo]))/span if span > 0 else 0.
    intercept = np.median(delta-slope*t)
    residual = delta-intercept-slope*t
    clip = max(.5, 6*float(np.median(abs(residual))))
    inliers = abs(residual) <= clip
    isolated = np.zeros(len(t), bool)
    gap_limit = max(GPS_GAP_SECONDS, 5*float(np.median(np.diff(t))))
    for i in np.flatnonzero(~inliers):
        if (0 < i < len(t)-1 and inliers[i-1] and inliers[i+1]
                and t[i]-t[i-1] <= gap_limit and t[i+1]-t[i] <= gap_limit
                and abs(residual[i-1]-residual[i+1]) <= GPS_STEP_SECONDS):
            isolated[i] = True
    st, su = t[~isolated], u[~isolated]
    gt, gu = t[inliers], u[inliers]
    reasons = []
    if stepped and not policy.allow_bounded_discontinuities:
        reasons.append("discontinuity_not_allowed")
    if len(gt) < 3:
        reasons.append("insufficient_observations")
    if inliers.mean() < .8:
        reasons.append("low_inlier_fraction")
    if len(gt) and np.ptp(gt) < MIN_GPS_SPAN_SECONDS:
        reasons.append("span_too_short")
    if np.any(t < 0):
        reasons.append("invalid_observation_time")
    if (np.any(np.diff(t) <= 0) or (len(gu) >= 2 and gu[-1] <= gu[0])
            or np.any(np.diff(gu) < -GPS_STEP_SECONDS)):
        reasons.append("non_increasing_clock")
    duration = float(telemetry.duration) if telemetry.duration is not None else None
    if duration is None:
        reasons.append("duration_unknown")
    elif t[0] < 0 or t[-1] >= duration:
        reasons.append("observations_outside_duration")
    evaluation = replace(evaluation, verdict="rejected", inlier_count=int(inliers.sum()),
                         isolated_outlier_count=int(isolated.sum()),
                         support_start_seconds=float(t[0]), support_end_seconds=float(t[-1]),
                         window_step_detected=stepped)
    if len(gt) < 3 or np.ptp(gt) <= 0 or np.any(np.diff(t) <= 0):
        return replace(evaluation, reason_codes=tuple(reasons))
    fit = linregress(gt, gu)
    if not all(np.isfinite(v) for v in (fit.slope, fit.intercept, fit.stderr)):
        return replace(evaluation, reason_codes=tuple([*reasons, "non_finite_estimate"]))
    if abs(fit.slope-1) > MAX_GPS_RATE_ERROR + 3*fit.stderr + 2*GPS_STEP_SECONDS/float(np.ptp(gt)):
        reasons.append("rate_out_of_range")
    old_within, old_bound, old_stderr, _ = _within_run_rate(gt, gu)
    z = gt-gt.mean()
    global_bound = GPS_STEP_SECONDS/2*float(np.sum(abs(z))/np.sum(z*z))
    gap_step = bool(abs(fit.slope-old_within) > old_bound+global_bound+3*np.hypot(old_stderr, fit.stderr))
    if gap_step and not policy.allow_bounded_discontinuities and not stepped:
        reasons.insert(0, "discontinuity_not_allowed")
    within, _, stderr, runs = _within_run_rate(st, su)
    if gap_step and any(np.sum(runs == index) < 2 for index in np.unique(runs)):
        reasons.append("clock_model_unbounded")
    consistency = _rate_regime_consistency(st, su, runs, half_noise)
    evaluation = replace(evaluation, gap_rate_mismatch_detected=gap_step,
                         rate_regime_change_detected=consistency is _Consistency.INCONSISTENT)
    if consistency is _Consistency.INCONSISTENT:
        reasons.append("rate_regime_inconsistent")
    elif consistency is _Consistency.UNRESOLVED and "clock_model_unbounded" not in reasons:
        reasons.append("clock_model_unbounded")
    if not np.isfinite(stderr):
        reasons.append("rate_unconstrained")
        return replace(evaluation, reason_codes=tuple(reasons))
    detected = bool(duration is not None and abs(fit.slope-1) > 3*fit.stderr
                    and abs(fit.slope-1)*duration > .5)
    rate = float(fit.slope) if detected else 1.
    offset = float(fit.intercept) if detected else float(np.median(gu-gt))
    safety_residual = su-(offset+rate*st)
    scatter = float(np.std(safety_residual))
    radius = float(np.max(abs(safety_residual)))
    excursion = float(np.ptp(su-within*st)) + 2*half_noise
    if scatter >= MAX_RESIDUAL_STD_SECONDS:
        reasons.append("residual_scatter_too_large")
    if excursion > MAX_NON_AFFINE_EXCURSION_SECONDS:
        reasons.append("non_affine_excursion_too_large")
    if min(track.time[-1], float(gu.max()+(base-midnight)))-max(track.time[0], float(gu.min()+(base-midnight))) < 0:
        reasons.append("no_track_overlap")
    run_details, levels, weight, numerator = [], [], 0., 0.
    gaps = []
    for index in np.unique(runs):
        rt, ru = st[runs == index], su[runs == index]
        center = float(rt.mean())
        level = float(np.median(ru-within*rt))
        amplitude = float(np.max(abs(ru-level-within*rt)))
        centered = rt-center
        weight += float(np.sum(centered**2))
        numerator += (half_noise+amplitude)*float(np.sum(abs(centered)))
        if run_details:
            gaps.append((run_details[-1].end_seconds, float(rt[0])))
        run_details.append(GpsRun(float(rt[0]), float(rt[-1]), center, len(rt), amplitude))
        levels.append(level)
    if weight <= 0:
        reasons.append("rate_unconstrained")
        return replace(evaluation, reason_codes=tuple(reasons))
    bound = numerator/weight
    error = abs(rate-within)+bound+3*stderr
    if duration is None:
        return replace(evaluation, reason_codes=tuple(reasons))
    def endpoint(end):
        return max(abs(offset+rate*end-level-within*end)+2*half_noise
                   + run.max_level_residual_seconds+error*abs(end-run.center_seconds)
                   for run, level in zip(run_details, levels))
    start_error, end_error = endpoint(0.), endpoint(duration)
    uncertainty = max(MIN_UNCERTAINTY_SECONDS, 3*scatter, radius, start_error, end_error)
    values = (uncertainty, start_error, end_error, scatter, radius, within, bound, stderr, error, excursion)
    if not all(np.isfinite(v) for v in values):
        reasons.append("non_finite_estimate")
        return replace(evaluation, reason_codes=tuple(reasons))
    if uncertainty > policy.max_uncertainty_seconds:
        reasons.append("whole_video_uncertainty_exceeded")
    envelope = GpsEnvelope(float(duration), start_error, end_error, uncertainty, radius, scatter,
                           within, bound, stderr, error, excursion, tuple(run_details), tuple(gaps))
    candidate = GpsFit(TimeMapping(float(base-midnight+offset), uncertainty, rate, OwnGps()),
                       envelope, float(fit.slope), float(fit.stderr), detected)
    return replace(evaluation, verdict="rejected" if reasons else "accepted",
                   reason_codes=tuple(reasons), fit=candidate)


_REASONS = {
    "discontinuity_not_allowed": "GPS UTC step detector triggered at 0.1 s",
    "insufficient_observations": "GPS UTC observations are inconsistent",
    "low_inlier_fraction": "GPS UTC observations are inconsistent",
    "span_too_short": "GPS UTC observations span less than 10 s",
    "non_increasing_clock": "GPS UTC does not increase with video time",
    "rate_out_of_range": "GPS UTC rate is outside the accepted camera clock range",
    "residual_scatter_too_large": "GPS UTC residuals exceed 0.5 s",
    "no_track_overlap": "GPS UTC does not overlap the track",
    "whole_video_uncertainty_exceeded": "GPS UTC observations do not constrain the clock over the whole video",
}


def rejection_message(evaluation: GpsEvaluation) -> str:
    return _REASONS.get(evaluation.reason_codes[0], "GPS timing rejected: " + ", ".join(evaluation.reason_codes))
