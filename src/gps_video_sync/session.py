from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations
from typing import Sequence

from .models import (
    Accepted, Ambiguous, CameraSession, CameraStart, JoinedShot, SessionContradiction,
    SessionInsufficient, SessionVerdict, SyncResult, Telemetry, TimeMapping, Track,
)


@dataclass(frozen=True)
class Anchor:
    offset_seconds: float
    uncertainty_seconds: float
    group: tuple[str, ...]


@dataclass(frozen=True)
class SessionMember:
    input_file: str
    start: CameraStart
    duration: float
    anchor: Anchor | None = None
    blocked: SessionVerdict | None = None


@dataclass(frozen=True)
class SessionMapping:
    offset_seconds: float
    uncertainty_seconds: float
    support: str
    other_supports: tuple[str, ...]

    @property
    def supporting(self) -> tuple[str, ...]:
        return (self.support, *self.other_supports)


SessionPlacement = SessionMapping | SessionVerdict


def place_session(members: Sequence[SessionMember]) -> dict[str, SessionPlacement]:
    cameras: dict[str | None, list[SessionMember]] = {}
    for member in members:
        cameras.setdefault(member.start.camera, []).append(member)
    placements: dict[str, SessionPlacement] = {}
    for camera in cameras.values():
        ordered = sorted(camera, key=lambda m: (m.start.seconds, m.input_file))
        units: dict[tuple[str, ...], SessionMember] = {}
        for member in ordered:
            if member.anchor is not None:
                units.setdefault(member.anchor.group, member)
        anchors = list(units.values())
        targets = [m for m in ordered if m.anchor is None]
        if len(anchors) < 2:
            for target in targets:
                placements[target.input_file] = target.blocked or SessionInsufficient("too_few_anchors")
            continue
        conflict = None
        for a, b in combinations(anchors, 2):
            delta = ((a.anchor.offset_seconds - a.start.seconds)
                     - (b.anchor.offset_seconds - b.start.seconds))
            bound = (a.anchor.uncertainty_seconds + a.start.bound_seconds
                     + b.anchor.uncertainty_seconds + b.start.bound_seconds
                     + .0002 * abs(a.start.seconds - b.start.seconds))
            if abs(delta) > bound:
                names = tuple(sorted((*a.anchor.group, *b.anchor.group)))
                conflict = SessionContradiction("anchors_disagree", names)
                break
        if conflict is None:
            for a, b in combinations(ordered, 2):
                overlap = (min(a.start.seconds + a.duration, b.start.seconds + b.duration)
                           - max(a.start.seconds, b.start.seconds))
                if overlap > a.start.bound_seconds + b.start.bound_seconds:
                    conflict = SessionContradiction("recordings_overlap", tuple(sorted((a.input_file, b.input_file))))
                    break
        supporting = tuple(sorted({name for a in anchors for name in a.anchor.group}))
        for target in targets:
            if conflict is not None:
                placements[target.input_file] = conflict
                continue
            if target.blocked is not None:
                placements[target.input_file] = target.blocked
                continue
            intervals = []
            for anchor in anchors:
                delta = target.start.seconds - anchor.start.seconds
                center = anchor.anchor.offset_seconds + delta
                width = (anchor.anchor.uncertainty_seconds + anchor.start.bound_seconds
                         + .0002 * abs(delta))
                intervals.append((center - width, center + width))
            lo, hi = max(a for a, _ in intervals), min(b for _, b in intervals)
            uncertainty = (hi - lo) / 2 + target.start.bound_seconds + .002 * target.duration
            if lo > hi:
                placement = SessionContradiction("anchors_disagree", supporting)
            elif uncertainty > 10.:
                placement = SessionInsufficient("loose_bound")
            else:
                placement = SessionMapping((lo + hi) / 2, uncertainty, supporting[0], supporting[1:])
            placements[target.input_file] = placement
    return placements


def match_session(track: Track, recordings: Sequence[Telemetry | None],
                  results: Sequence[SyncResult]) -> list[SyncResult]:
    dates = sorted({r.date for r in results if r.mapping is not None and r.date is not None})
    origin = track.date if track.date is not None else (dates[0] if dates else None)
    members = []
    updated = list(results)
    for index, (result, telemetry) in enumerate(zip(results, recordings)):
        if telemetry is None or result.status == "failed":
            continue
        start = telemetry.camera_start
        if start is None or telemetry.duration is None:
            if isinstance(result.outcome, Ambiguous):
                updated[index] = replace(result, outcome=replace(result.outcome,
                    session=SessionInsufficient("no_camera_start")))
            continue
        anchor = None
        if result.mapping is not None and not result.gps.blocks_transfer:
            source = result.mapping.source
            if isinstance(source, CameraSession) or (result.date is None) != (origin is None):
                continue
            day = (result.date - origin).days * 86400 if origin is not None else 0.
            anchor = Anchor(result.offset_seconds + day, result.uncertainty_seconds,
                            source.members if isinstance(source, JoinedShot) else (result.input_file,))
        blocked = result.shot_candidate.session_block if result.shot_candidate else None
        if result.gps.blocks_transfer:
            blocked = SessionContradiction("gps_step", (result.input_file,))
        members.append(SessionMember(result.input_file, start, telemetry.duration, anchor, blocked))
    placements = place_session(members)
    for index, (result, telemetry) in enumerate(zip(results, recordings)):
        if not isinstance(result.outcome, Ambiguous) or result.input_file not in placements:
            continue
        placement = placements[result.input_file]
        if isinstance(placement, SessionMapping):
            day = (result.date - origin).days * 86400 if result.date is not None and origin is not None else 0.
            offset = placement.offset_seconds - day
            overlap = max(0., min(offset + telemetry.duration, track.time[-1]) - max(offset, track.time[0]))
            source = CameraSession(placement.support, placement.other_supports, telemetry.camera_start,
                                   result.outcome.independent, float(overlap))
            outcome = Accepted(TimeMapping(offset, placement.uncertainty_seconds, 1., source))
            updated[index] = replace(result, outcome=outcome, date=result.date or origin)
        else:
            updated[index] = replace(result, outcome=replace(result.outcome, session=placement))
    return updated
