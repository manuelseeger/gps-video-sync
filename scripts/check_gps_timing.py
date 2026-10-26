#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import subprocess
import zipfile

import numpy as np

from gps_video_sync import GpsTimingPolicy, Telemetry, Track, read_video, synchronize


def timing_input(path, duration):
    with np.load(path, allow_pickle=False) as data:
        if "recording" in data:
            telemetry = read_video(path, source="npz")
        else:
            telemetry = Telemetry(np.asarray(data["time"], float), {},
                                  duration=float(data["duration"]) if "duration" in data else None,
                                  gps_time=np.asarray(data["gps_time"], float),
                                  gps_utc=np.asarray(data["gps_utc"], float))
    if duration is not None:
        telemetry.duration = duration
    return telemetry


def analyze(telemetry, budget):
    stamps = telemetry.gps_utc
    good = stamps[np.isfinite(stamps) & (stamps >= 0) & (stamps < 253370764800.)]
    if len(good) < 3:
        raise ValueError("At least three supported finite GPS UTC observations are required")
    origin = datetime.fromtimestamp(float(np.median(good)), timezone.utc).date()
    midnight = datetime.combine(origin, datetime.min.time(), timezone.utc).timestamp()
    support = np.array([float(good.min()-midnight-1), float(good.max()-midnight+1)])
    track = Track(support, np.zeros(2), np.zeros(2), np.zeros(2), origin)
    policies = {
        "strict": GpsTimingPolicy(max_uncertainty_seconds=budget),
        "bounded": GpsTimingPolicy(max_uncertainty_seconds=budget, allow_bounded_discontinuities=True),
    }
    output = {}
    for name, policy in policies.items():
        result = synchronize(track, telemetry, gps_policy=policy)
        gps = result.gps
        output[name] = dict(
            verdict=gps.verdict, reason_codes=list(gps.reason_codes), policy=asdict(policy),
            selection=result.to_dict()["selection"],
            discontinuity_detected=gps.discontinuity_detected,
            window_step_detected=gps.window_step_detected,
            gap_rate_mismatch_detected=gps.gap_rate_mismatch_detected,
            rate_regime_change_detected=gps.rate_regime_change_detected,
            finite_count=gps.finite_count, inlier_count=gps.inlier_count,
            isolated_outlier_count=gps.isolated_outlier_count,
            support_start_seconds=gps.support_start_seconds,
            support_end_seconds=gps.support_end_seconds,
            envelope=asdict(gps.fit.envelope) if gps.fit else None)
    return dict(analysis="timing consistency with a synthetic overlapping track",
                assumptions="common rate and bounded residual error; no flight ground truth or motion validation",
                duration_seconds=telemetry.duration, policies=output)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check GPS timing consistency with a synthetic overlapping track.")
    parser.add_argument("--video", type=Path)
    parser.add_argument("--timing-npz", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--max-uncertainty-seconds", type=float, default=1.)
    args = parser.parse_args(argv)
    if not args.video and not args.timing_npz:
        parser.error("Supply --video and/or --timing-npz")
    try:
        GpsTimingPolicy(max_uncertainty_seconds=args.max_uncertainty_seconds)
        inputs = {}
        if args.video:
            telemetry = read_video(args.video, cache_dir=args.cache_dir)
            if args.duration_seconds is not None:
                telemetry.duration = args.duration_seconds
            inputs["video"] = telemetry
        if args.timing_npz:
            inputs["timing_npz"] = timing_input(args.timing_npz, args.duration_seconds)
        output = {name: analyze(value, args.max_uncertainty_seconds) for name, value in inputs.items()}
        print(json.dumps(output, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, RuntimeError, EOFError,
            zipfile.BadZipFile, subprocess.SubprocessError):
        print("Timing analysis failed. Check input arrays, duration, and policy.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
