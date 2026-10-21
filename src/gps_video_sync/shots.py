from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Literal, Sequence

import numpy as np

from .models import (SyncResult, Telemetry, Track, Accepted, JoinedShot, ShotCandidate,
                     SessionContradiction)

JOIN_SECONDS = .05


@dataclass(frozen=True)
class Shot:
    members: tuple[int, ...]
    status: Literal["single", "joined", "ambiguous"]
    reason: str = ""


def _continuous(a: Telemetry, b: Telemetry) -> bool:
    ac, bc = a.clock, b.clock
    if (ac is None or bc is None or a.duration is None or b.duration is None
            or a.media_offset_seconds != 0 or b.media_offset_seconds != 0):
        return False
    if ac.camera is not None and bc.camera is not None and ac.camera != bc.camera:
        return False
    if bc.origin_seconds <= ac.origin_seconds:
        return False
    junction = abs(bc.origin_seconds - (ac.origin_seconds + a.duration))
    if junction + ac.reach_seconds + bc.reach_seconds > JOIN_SECONDS:
        return False
    shared = ac.sample_counters.keys() & bc.sample_counters.keys()
    return bool(shared) and all(ac.sample_counters[k][1] == bc.sample_counters[k][0] for k in shared)


def find_shots(recordings: Sequence[Telemetry | None]) -> list[Shot]:
    available = [i for i, r in enumerate(recordings) if r is not None]
    successors = {i: [] for i in available}
    predecessors = {i: [] for i in available}
    for i in available:
        for j in available:
            if i != j and _continuous(recordings[i], recordings[j]):
                successors[i].append(j)
                predecessors[j].append(i)
    remaining, shots = set(available), []
    while remaining:
        pending, component = [min(remaining)], set()
        while pending:
            i = pending.pop()
            if i not in component:
                component.add(i)
                pending.extend(successors[i] + predecessors[i])
        remaining -= component
        if len(component) == 1:
            shots.append(Shot(tuple(component), "single"))
            continue
        cameras = {recordings[i].clock.camera for i in component} - {None}
        if (any(len(successors[i]) > 1 or len(predecessors[i]) > 1 for i in component)
                or len(cameras) > 1):
            reason = "Continuity branches or has conflicting camera serials: " + ", ".join(
                sorted(recordings[i].video or str(i) for i in component))
            members = tuple(sorted(component, key=lambda i: (
                recordings[i].clock.origin_seconds, recordings[i].video or "")))
            shots.append(Shot(members, "ambiguous", reason))
            continue
        root = next(i for i in component if not predecessors[i])
        members = [root]
        while successors[members[-1]]:
            members.append(successors[members[-1]][0])
        shots.append(Shot(tuple(members), "joined"))
    return shots


def concatenate(members: Sequence[Telemetry]) -> Telemetry:
    starts, duration = [], 0.
    for member in members:
        starts.append(duration)
        duration += member.duration
    channels = {}
    for key in set().union(*(m.channels for m in members)):
        shape = next(m.channels[key].shape[1:] for m in members if key in m.channels)
        if any(m.channels[key].shape[1:] != shape for m in members if key in m.channels):
            continue
        channels[key] = np.concatenate([
            m.channels[key] if key in m.channels else np.full((len(m.time), *shape), np.nan)
            for m in members])
    return Telemetry(np.concatenate([m.time + s for m, s in zip(members, starts)]),
                     channels, duration=duration,
                     gps_time=np.concatenate([m.gps_time + s for m, s in zip(members, starts)]),
                     gps_utc=np.concatenate([m.gps_utc for m in members]))


def project(result: SyncResult, members: Sequence[Telemetry]) -> list[SyncResult]:
    offset, rate = result.offset_seconds, result.mapping.clock_rate
    projected = []
    for member in members:
        projected.append(replace(result, outcome=Accepted(replace(result.mapping, offset_seconds=offset)), video=member.video,
                                 media_offset_seconds=member.media_offset_seconds,
                                 duration=member.duration, confidence=deepcopy(result.confidence),
                                 per_feature=deepcopy(result.per_feature), drift=deepcopy(result.drift)))
        offset = offset + rate * member.duration
    return projected


def _gps_step(result: SyncResult) -> bool:
    return "step" in result.confidence.get("gps_rejected", "")


def _contradiction(baseline: SyncResult, projected: SyncResult) -> bool:
    if baseline.status != "ok":
        return False
    day = ((projected.date - baseline.date).days * 86400
           if projected.date is not None and baseline.date is not None else 0.)
    delta = projected.offset_seconds - baseline.offset_seconds + day
    rate_delta = projected.mapping.clock_rate - baseline.mapping.clock_rate
    bound = baseline.uncertainty_seconds + projected.uncertainty_seconds
    return any(abs(delta + rate_delta * end) > bound for end in (0., baseline.duration))


def match_shots(track: Track, recordings: Sequence[Telemetry | None],
                baseline: list[SyncResult]) -> list[SyncResult]:
    from .sync import _synchronize

    results = list(baseline)
    for shot in find_shots(recordings):
        if shot.status == "single":
            continue
        members = [recordings[i] for i in shot.members]
        reason, projected = shot.reason, None
        identities = tuple(baseline[j].input_file for j in shot.members)
        block = (SessionContradiction("continuity_conflict", identities)
                 if shot.status == "ambiguous" else None)
        creations = [m.camera_start.seconds - m.clock.origin_seconds for m in members
                     if m.camera_start is not None and m.clock is not None]
        if creations and max(creations) - min(creations) > 1.:
            block = SessionContradiction("chapter_creation", tuple(sorted(identities)))
            reason = block.message
        elif shot.status == "joined":
            if any(baseline[i].status == "failed" for i in shot.members):
                reason = "A member failed independent synchronization"
            elif any(_gps_step(baseline[i]) for i in shot.members):
                reason = "A GPS UTC step blocks shot synchronization"
                block = SessionContradiction("gps_step", identities)
            else:
                solved = _synchronize(track, concatenate(members))
                if solved.status != "ok":
                    reason = "Shot synchronization was not accepted: " + solved.message
                elif _gps_step(solved) and not any(_gps_step(baseline[i]) for i in shot.members):
                    reason = "Grouping introduced a GPS UTC step"
                else:
                    projected = project(solved, members)
                    if any(_contradiction(baseline[i], p) for i, p in zip(shot.members, projected)):
                        reason = "Shot mapping contradicts an accepted independent mapping"
        if reason:
            shot = replace(shot, status="ambiguous", reason=reason)
            if block is None and any(baseline[i].status in {"ok", "failed"} for i in shot.members):
                block = SessionContradiction("continuity_conflict", identities)
        start = 0.
        for index, i in enumerate(shot.members):
            result = (replace(projected[index], input_file=baseline[i].input_file)
                      if shot.status == "joined" else replace(baseline[i]))
            if shot.status == "joined":
                result.outcome = Accepted(replace(result.mapping, source=JoinedShot(
                    result.method, identities, index, start)))
            else:
                result.shot_candidate = ShotCandidate(identities, index, start, shot.reason, block)
            results[i] = result
            start += recordings[i].duration
    return results
