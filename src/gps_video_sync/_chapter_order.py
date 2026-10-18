"""Recording-clock evidence for a continuous set of video chapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

GPS_JOIN = .5
GPS_MAX_REACH = 1.
GPS_MAX_SPREAD = .1
MAX_RATE_ERROR = .002
MIN_SPAN = .1
STMP_JOIN = .1
STMP_MAX_SPREAD = .02
STMP_MAX_GAP = 2.
STMP_MAX_EXTRAPOLATION = 1.1
STMP_KEYS = ("CORI", "IORI", "GRAV", "ACCL", "GYRO")
PREFIX = "Cannot establish chapter order: "


@dataclass(frozen=True)
class Clock:
    start: float
    reach: float


@dataclass(frozen=True)
class Evidence:
    clock: Clock | None
    reason: str = ""


@dataclass
class Chapter:
    path: Path
    duration: float
    packets: int
    motion: dict[str, list[np.ndarray]]
    gps: list[tuple[np.ndarray, np.ndarray]]
    clocks: dict[str, Evidence]


def clock_evidence(time: np.ndarray, stamps: np.ndarray, duration: float, *, gps: bool) -> Evidence:
    minimum = 2 if gps else 3
    spread_limit = GPS_MAX_SPREAD if gps else STMP_MAX_SPREAD
    if len(time) < minimum:
        return Evidence(None, f"fewer than {minimum} observations")
    if not np.isfinite(time).all() or not np.isfinite(stamps).all():
        return Evidence(None, "malformed or nonfinite timestamps")
    if np.any(stamps < 0):
        return Evidence(None, "negative recording-clock timestamps")
    if gps and np.any(stamps >= 253370764800.):
        return Evidence(None, "UTC outside supported years 1970 to 9998")
    if np.any(np.diff(time) <= 0) or np.any(np.diff(stamps) <= 0):
        return Evidence(None, "timestamps reset or do not strictly increase")
    span = float(time[-1] - time[0])
    if span < MIN_SPAN:
        return Evidence(None, f"observations span less than {MIN_SPAN:g} s")
    if not gps and (np.max(np.diff(time)) > STMP_MAX_GAP
                    or max(0., float(time[0]), duration - float(time[-1])) > STMP_MAX_EXTRAPOLATION):
        return Evidence(None, "sparse STMP packets leave an unobserved boundary or gap")
    offsets = stamps - time
    if float(np.ptp(offsets)) > spread_limit + 1e-6:
        return Evidence(None, f"clock offset spread exceeds {spread_limit:g} s")
    centered = time - np.mean(time)
    rate_error = float(np.dot(centered, offsets - np.mean(offsets)) / np.dot(centered, centered))
    if abs(rate_error) > MAX_RATE_ERROR + spread_limit / span:
        return Evidence(None, "clock rate is inconsistent with MP4 time")
    start = float(np.median(offsets))
    extrapolation = max(0., float(time[0]), duration - float(time[-1]))
    reach = float(np.max(abs(offsets - start))) + (MAX_RATE_ERROR + spread_limit / span) * extrapolation
    if gps:
        reach += GPS_MAX_SPREAD / 2
        if reach > GPS_MAX_REACH:
            return Evidence(None, f"sparse observations need more than {GPS_MAX_REACH:g} s boundary reach")
    return Evidence(Clock(start, reach))


def order_chapters(chapters: list[Chapter]) -> tuple[list[Chapter], str | None]:
    common = set.intersection(*(set(c.clocks) for c in chapters))
    stmp = next((key for key in STMP_KEYS if key in common), None)
    if stmp is None:
        stmp = next((key for key in STMP_KEYS
                     if sum(key in c.clocks and c.clocks[key].clock is not None for c in chapters) >= 2), None)
    if len(chapters) == 1:
        return chapters, stmp
    evidence = {
        "GPS": [c.clocks["GPS"] for c in chapters],
        "STMP": [c.clocks.get(stmp, Evidence(None, "no common STMP sensor stream"))
                 if stmp is not None else Evidence(None, "no common STMP sensor stream") for c in chapters],
    }
    covering = {}
    for kind, values in evidence.items():
        clocks = [e.clock for e in values if e.clock is not None]
        if len(clocks) == len(chapters):
            covering[kind] = clocks
    if not covering:
        details = "; ".join(
            f"{c.path}: " + ", ".join(f"{kind} {evidence[kind][i].reason or 'usable'}" for kind in evidence)
            for i, c in sorted(enumerate(chapters), key=lambda pair: str(pair[1].path))
        )
        raise ValueError(PREFIX + "no common usable recording clock; " + details)
    orders = {}
    failures = []
    for kind, clocks in covering.items():
        join = GPS_JOIN if kind == "GPS" else STMP_JOIN
        order = sorted(range(len(chapters)), key=lambda i: (clocks[i].start, str(chapters[i].path)))
        orders[kind] = order
        if any(c.duration <= 2 * join for c in chapters):
            failures.append(f"{kind} uncertain: chapter durations must exceed {2 * join:g} s")
            continue
        for left, right in zip(order, order[1:]):
            delta = clocks[right].start - (clocks[left].start + chapters[left].duration)
            reach = clocks[left].reach + clocks[right].reach
            if abs(delta) + reach <= join + 1e-6:
                continue
            if delta - reach > join:
                reason = "gap (separate recordings or missing chapter)"
            elif delta + reach < -join:
                reason = "overlap (duplicate or simultaneous recording)"
            else:
                reason = "uncertain boundary"
            failures.append(f"{kind} {reason}: {chapters[left].path} -> {chapters[right].path} "
                            f"(junction {delta:.6f} s, reach {reach:.6f} s, tolerance {join:g} s)")
    if len(orders) == 2 and orders["GPS"] != orders["STMP"]:
        failures.insert(0, "camera clocks disagree on chapter order")
    if failures:
        raise ValueError(PREFIX + "; ".join(failures))
    order = next(iter(orders.values()))
    prefix = np.cumsum([0.] + [chapters[i].duration for i in order])
    for kind, values in evidence.items():
        if kind in covering:
            continue
        bounds = []
        for position, i in enumerate(order):
            clock = values[i].clock
            if clock is not None:
                origin = clock.start - prefix[position]
                bounds.append((origin - clock.reach, origin + clock.reach, chapters[i].path))
        if len(bounds) < 2:
            continue
        lower = max(bounds, key=lambda bound: bound[0])
        upper = min(bounds, key=lambda bound: bound[1])
        separation = lower[0] - upper[1]
        join = GPS_JOIN if kind == "GPS" else STMP_JOIN
        if separation > join + 1e-6:
            raise ValueError(PREFIX + f"{kind} contradicts resolved chapter timeline: "
                             f"{upper[2]} -> {lower[2]} "
                             f"(clock-origin separation {separation:.6f} s, tolerance {join:g} s)")
    return [chapters[i] for i in order], stmp
