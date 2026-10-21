from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone, timedelta
from typing import Any
from numbers import Real
import math
import numpy as np

@dataclass
class Track:
    time: np.ndarray
    latitude: np.ndarray
    longitude: np.ndarray
    altitude: np.ndarray
    date: date | None = None
    altitude_source: str = "gps"

@dataclass(frozen=True)
class RecordingClock:
    origin_seconds: float
    reach_seconds: float
    sample_counters: dict[str, tuple[int, int]]
    camera: str | None = None

    def __post_init__(self):
        for value in (self.origin_seconds, self.reach_seconds):
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
                raise ValueError("Recording clock needs finite origin and error bound")
        if self.reach_seconds < 0:
            raise ValueError("Recording clock error bound must be nonnegative")
        if self.camera is not None and (not isinstance(self.camera, str) or not self.camera):
            raise ValueError("Recording clock camera must be a nonempty string")
        if not isinstance(self.sample_counters, dict) or not self.sample_counters:
            raise ValueError("Recording clock needs sample counters")
        counters = {}
        for stream, endpoints in self.sample_counters.items():
            if (not isinstance(stream, str) or not stream
                    or not isinstance(endpoints, (list, tuple)) or len(endpoints) != 2
                    or any(type(v) is not int or v < 0 for v in endpoints)
                    or endpoints[1] <= endpoints[0]):
                raise ValueError("Recording clock needs exact increasing integer counter endpoints")
            counters[stream] = tuple(endpoints)
        object.__setattr__(self, "sample_counters", counters)

    @classmethod
    def from_dict(cls, value) -> RecordingClock | None:
        if not isinstance(value, dict):
            return None
        try:
            return cls(**value)
        except (ValueError, TypeError, OverflowError):
            return None

@dataclass(frozen=True)
class CameraStart:
    """Camera-clock time at local video zero, not UTC."""
    seconds: float
    bound_seconds: float
    camera: str | None = None

    def __post_init__(self):
        if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v)
               for v in (self.seconds, self.bound_seconds)) or self.bound_seconds < 0:
            raise ValueError("Camera start needs finite seconds and a nonnegative bound")
        if self.camera is not None and (not isinstance(self.camera, str) or not self.camera):
            raise ValueError("Camera start camera must be a nonempty string")

    @classmethod
    def from_dict(cls, value) -> CameraStart | None:
        if not isinstance(value, dict):
            return None
        try:
            return cls(**value)
        except (ValueError, TypeError, OverflowError):
            return None


@dataclass
class Telemetry:
    time: np.ndarray
    channels: dict[str, np.ndarray]
    video: str | None = None
    duration: float | None = None
    gps_time: np.ndarray = field(default_factory=lambda: np.empty(0))
    gps_utc: np.ndarray = field(default_factory=lambda: np.empty(0))

    media_offset_seconds: float = 0.
    clock: RecordingClock | None = None
    camera_start: CameraStart | None = None

    def slice(self, start: float, end: float) -> Telemetry:
        if not np.isfinite(start) or not np.isfinite(end) or start < 0 or end <= start:
            raise ValueError("Slice needs finite bounds with 0 <= start < end")
        if self.duration is not None and end > self.duration:
            raise ValueError("Slice end exceeds recording duration")
        duration = end - start
        time, gps_time = self.time - start, self.gps_time - start
        mask = ((self.time >= start) & (self.time < end)
                & (time >= 0) & (time < duration))
        gps = ((self.gps_time >= start) & (self.gps_time < end)
               & (gps_time >= 0) & (gps_time < duration))
        return Telemetry(time[mask],
                         {k: v[mask] for k, v in self.channels.items()}, self.video,
                         duration, gps_time[gps], self.gps_utc[gps],
                         self.media_offset_seconds + start)

@dataclass
class SyncResult:
    status: str
    offset_seconds: float | None = None
    uncertainty_seconds: float | None = None
    method: str = "motion"
    confidence: dict[str, Any] = field(default_factory=dict)
    per_feature: dict[str, Any] = field(default_factory=dict)
    drift: dict[str, Any] = field(default_factory=dict)
    video: str | None = None
    input_file: str | None = None
    date: date | None = None
    message: str = ""
    media_offset_seconds: float | None = None
    duration: float | None = None
    shot: dict[str, Any] = field(default_factory=lambda: {"status": "single"})

    @property
    def video_start_utc(self) -> str | None:
        if self.date is None or self.offset_seconds is None:
            return None
        return (datetime.combine(self.date, datetime.min.time(), timezone.utc)
                + timedelta(seconds=self.offset_seconds)).isoformat()

    def to_dict(self) -> dict[str, Any]:
        obj = asdict(self)
        obj.pop("date")
        iso = self.video_start_utc
        rate = self.drift.get("clock_rate", 1.0)
        obj.update(schema_version="2.0.0", mapping="utc_seconds = video_seconds * clockRate + offset_seconds",
                   video_start_utc=iso, videoStartUtcSeconds=self.offset_seconds,
                   videoStartUtc=iso.split("T")[1].replace("+00:00", "") if iso else None,
                   dateUtc=iso[:10] if iso else (self.date.isoformat() if self.date else None),
                   utc_origin_date=self.date.isoformat() if self.date else None,
                   clockDriftDetected=self.drift.get("detected", False), clockRate=rate,
                   estimatedAbsoluteUncertaintySeconds=self.uncertainty_seconds)
        return obj
